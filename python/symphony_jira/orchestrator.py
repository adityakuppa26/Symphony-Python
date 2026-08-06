from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .codex_runner import CodexRunner, CodexRunResult
from .automation_plan import (
    AutomationPlan,
    AutomationPlanError,
    automation_result_content_hash,
    automation_plan_json_schema,
    parse_automation_plan,
)
from .config import CodexConfig, WorkflowConfig
from .human_review import (
    HumanReviewContextError,
    build_human_review_implementation_prompt,
    build_human_review_triage_prompt,
    capture_workspace_diff,
    classify_human_review_triage,
    hash_runtime_verification_log,
    hash_verification_evidence,
    issue_from_frozen_snapshot,
    read_frozen_text_artifact,
    read_only_codex_config,
    validate_frozen_snapshot_artifacts,
    write_frozen_text_artifact,
)
from .logging import redact_text
from .models import (
    Issue,
    RequirementsSnapshot,
    RunRecord,
    diff_requirements_snapshots,
    issue_description_fingerprint,
    requirements_planning_authority_equivalent,
    requirements_planning_authority_projection,
    utc_now,
)
from .plan_spec import (
    EpicStrategy,
    PlanSpec,
    PlanSpecError,
    parse_frozen_legacy_plan_spec,
    parse_plan_spec,
    plan_spec_json_schema,
    validate_plan_spec_context,
    validate_plan_precedent_paths,
)
from .requirements_artifacts import (
    canonical_requirements_snapshot_json,
    write_requirements_snapshot_artifacts,
)
from .runtime import (
    RuntimeManager,
    RuntimeVerificationResult,
    write_runtime_artifact_bytes,
)
from .store import HUMAN_RESUME_HANDOFF_LEASE, Store, StoreIntegrityError
from .workflow import WorkflowDefinition, render_prompt
from .workspace import HookResult, WorkspaceError, WorkspaceInfo, WorkspaceManager


class JiraLike(Protocol):
    async def search_issues(self, jql: str, limit: int) -> list[Issue]: ...

    async def get_issue(self, key: str, include_comments: bool = True) -> Issue: ...

    async def add_comment(self, key: str, body: str) -> None: ...

    async def transition_issue(self, key: str, target_status: str) -> bool: ...


@dataclass
class OnceResult:
    issue: Issue
    prompt: str
    run: RunRecord | None
    workspace: WorkspaceInfo | None
    dry_run: bool = False


@dataclass
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at: float
    error: str | None = None

    def seconds_until_due(self) -> float:
        return max(0.0, self.due_at - time.monotonic())


@dataclass
class RunningIssue:
    issue_id: str
    identifier: str
    attempt: int
    task: asyncio.Task[OnceResult]
    started_at: datetime
    human_resume: bool = False
    completed_review: bool = False


@dataclass(frozen=True)
class LegacyVerificationResumeBinding:
    """Exact frozen binding retained across the v1-v3 to v4 migration."""

    snapshot: RequirementsSnapshot
    requirements_snapshot_hash: str


@dataclass(frozen=True)
class VerificationBypassBinding:
    approver_identity: str
    workspace_diff_hash: str
    verification_evidence_sha256: str
    verification_evidence_path: str
    original_verification_status: str


@dataclass(frozen=True)
class AutomationStageResult:
    status: str
    final_message: str | None
    error: str | None
    blocked_phase: str | None
    plan: AutomationPlan | None
    plan_message: str | None
    result_message: str | None
    event_offset: int
    trusted_repository_diff_hash: str | None = None


@dataclass(frozen=True)
class AutomationRepositoryState:
    head_sha: str
    branch_name: str
    dirty: bool
    changed_paths: tuple[str, ...]
    changed_file_types: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AutomationIgnoredFile:
    path: str
    content: bytes
    mode: int


@dataclass(frozen=True)
class AutomationMutationGuard:
    ignored_files: tuple[AutomationIgnoredFile, ...]
    existing_dirty_files: tuple[AutomationIgnoredFile, ...]
    git_metadata: tuple[tuple[str, bytes | None, int | None], ...]


class OrchestratorError(Exception):
    """Raised when an issue cannot be dispatched."""


class PlanningSafetyGateBlocked(Exception):
    """Stops a run before Codex when requirement evidence is not implementable."""


class AutomationBindingConfigurationError(Exception):
    """A retained automation run cannot be validated by the active workflow."""


PRIORITY_RANKS = {
    "highest": 0,
    "blocker": 0,
    "critical": 0,
    "high": 1,
    "major": 1,
    "medium": 2,
    "normal": 2,
    "low": 3,
    "minor": 3,
    "lowest": 4,
    "trivial": 4,
}

NON_RETRYABLE_ERROR_MARKERS = (
    "prompt rendering failed",
    "missing required labels",
    "status is not active",
    "workflow",
    "codex command not found",
    "workspace.source_repo",
    "source_repo",
    "jira token",
    "jira email",
)

PLAN_APPROVAL_RESPONSES = {"approved", "approved.", "approve", "approve."}
VERIFICATION_BYPASS_PHASES = frozenset({"verification", "verification_environment"})
GIT_BASELINE_TIMEOUT_SECONDS = 5.0
MAX_PLAN_SPEC_REPAIR_ATTEMPTS = 2
MAX_AUTOMATION_IGNORED_FILES = 4096
MAX_AUTOMATION_IGNORED_BYTES = 16 * 1024 * 1024
MAX_AUTOMATION_GIT_METADATA_BYTES = 1024 * 1024
AUTOMATION_GIT_METADATA_PATHS = (
    "config",
    "config.worktree",
    "info/exclude",
    "info/attributes",
    "info/sparse-checkout",
)


TRUTHY_STRINGS = {"true", "yes", "y", "1", "required", "needs_human"}


def verification_bypass_binding(
    previous_run: RunRecord | None,
    human_input: dict[str, Any] | None,
) -> VerificationBypassBinding | None:
    if str((human_input or {}).get("action") or "") != "verification_bypass":
        return None
    if (
        previous_run is None
        or previous_run.status != "blocked"
        or previous_run.blocked_phase not in VERIFICATION_BYPASS_PHASES
        or previous_run.verification_status in {None, "passed", "not_configured"}
        or not str(previous_run.verification_output_path or "").strip()
    ):
        raise OrchestratorError(
            "structured verification bypass does not match a failed verification run"
        )
    assert human_input is not None
    input_run_id = str(human_input.get("run_id") or "")
    if input_run_id != previous_run.id:
        raise OrchestratorError(
            "structured verification bypass belongs to another run"
        )
    identity = " ".join(str(human_input.get("approver_identity") or "").split())
    if not identity:
        raise OrchestratorError(
            "structured verification bypass is missing its approver identity"
        )
    workspace_diff_hash = require_sha256_binding(
        human_input.get("workspace_diff_hash"),
        "workspace diff hash",
    )
    evidence_hash = require_sha256_binding(
        human_input.get("verification_evidence_sha256"),
        "verification evidence SHA-256",
    )
    persisted_diff_hash = require_sha256_binding(
        previous_run.verification_workspace_diff_hash,
        "persisted verification workspace diff hash",
    )
    persisted_evidence_hash = require_sha256_binding(
        previous_run.verification_evidence_sha256,
        "persisted verification evidence SHA-256",
    )
    if (
        workspace_diff_hash != persisted_diff_hash
        or evidence_hash != persisted_evidence_hash
    ):
        raise OrchestratorError(
            "structured verification bypass does not match the failed run integrity binding"
        )
    return VerificationBypassBinding(
        approver_identity=identity,
        workspace_diff_hash=workspace_diff_hash,
        verification_evidence_sha256=evidence_hash,
        verification_evidence_path=str(previous_run.verification_output_path),
        original_verification_status=str(previous_run.verification_status),
    )


def require_sha256_binding(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise OrchestratorError(
            f"structured verification bypass {label} is invalid"
        )
    return normalized


def managed_workspace_repositories(config: WorkflowConfig) -> tuple[str, ...]:
    """Return every checkout managed by the configured shared runtime."""

    return tuple(
        sorted(
            {
                repository.workspace_subdir.as_posix()
                for repository in config.runtime.repositories.values()
            }
        )
    )


def managed_diff_repositories(config: WorkflowConfig) -> tuple[str, ...]:
    """Return every checkout whose diff must be retained for review/integrity."""

    repositories = set(managed_workspace_repositories(config))
    if config.automation.enabled:
        repositories.add(config.automation.workspace_subdir.as_posix())
    return tuple(sorted(repositories))


def capture_automation_repository_diff(
    workspace_path: Path,
    development_plan: PlanSpec,
    config: WorkflowConfig,
):
    """Capture only the isolated automation checkout's exact worktree state."""

    return capture_workspace_diff(
        workspace_path,
        development_plan,
        managed_repositories=(config.automation.workspace_subdir.as_posix(),),
        include_plan_repositories=False,
    )


def runtime_repository_keys(
    config: WorkflowConfig,
    workspace_repositories: tuple[str, ...],
) -> tuple[str, ...]:
    """Translate PlanSpec workspace paths to runtime configuration keys."""

    return tuple(
        runtime_key
        for _, runtime_key in runtime_repository_bindings(
            config,
            workspace_repositories,
        )
    )


def runtime_repository_bindings(
    config: WorkflowConfig,
    workspace_repositories: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Return unique PlanSpec workspace paths and their runtime keys."""

    bindings: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for workspace_subdir in workspace_repositories:
        runtime_key = config.runtime.repository_key_for_workspace_subdir(
            workspace_subdir
        )
        if runtime_key in seen_keys:
            continue
        seen_keys.add(runtime_key)
        bindings.append((workspace_subdir, runtime_key))
    return tuple(bindings)


def capture_failed_verification_binding(
    *,
    workspace_path: Path,
    output_plan_file: str,
    expected_plan_spec_hash: str | None,
    issue: Issue,
    requirements_snapshot_hash: str,
    evidence_path: str | None,
    config: WorkflowConfig,
    legacy_frozen_plan: bool,
) -> tuple[str, str]:
    """Freeze the exact code and evidence state that produced a failed verification."""

    if not expected_plan_spec_hash:
        raise PlanSpecError(
            "failed verification has no exact trusted PlanSpec binding"
        )
    plan_text = read_frozen_text_artifact(
        workspace_path,
        output_plan_file,
        label="failed verification PlanSpec",
        required=True,
    )
    if plan_text is None:
        raise PlanSpecError("failed verification PlanSpec is missing")
    if legacy_frozen_plan:
        if issue.requirements_snapshot is None:
            raise PlanSpecError("Frozen legacy requirements snapshot is missing")
        plan_spec = parse_frozen_legacy_plan_spec(
            plan_text,
            expected_issue_key=issue.identifier,
            expected_snapshot_hash=requirements_snapshot_hash,
            issue_type=issue.issue_type,
            requirements_snapshot=issue.requirements_snapshot,
        )
    else:
        plan_spec = parse_plan_spec(
            plan_text,
            expected_issue_key=issue.identifier,
            expected_snapshot_hash=requirements_snapshot_hash,
            issue_type=issue.issue_type,
            requirements_snapshot=issue.requirements_snapshot,
        )
    if plan_spec.content_hash() != expected_plan_spec_hash:
        raise PlanSpecError(
            "failed verification PlanSpec does not match the trusted plan hash"
        )
    workspace_diff_hash = capture_workspace_diff(
        workspace_path,
        plan_spec,
        managed_repositories=managed_diff_repositories(config),
    ).content_hash
    evidence_sha256 = hash_verification_evidence(
        workspace_path,
        evidence_path,
    )
    return workspace_diff_hash, evidence_sha256


def verification_bypass_integrity_error(
    binding: VerificationBypassBinding,
    *,
    workspace_path: Path,
    plan_spec: PlanSpec,
    managed_repositories: tuple[str, ...],
    checkpoint: str,
) -> str | None:
    try:
        current_diff_hash = capture_workspace_diff(
            workspace_path,
            plan_spec,
            managed_repositories=managed_repositories,
        ).content_hash
        current_evidence_hash = hash_verification_evidence(
            workspace_path,
            binding.verification_evidence_path,
        )
    except HumanReviewContextError as exc:
        return (
            f"Verification bypass binding could not be validated at {checkpoint}: {exc}. "
            "Run verification again or approve a new override for the current state."
        )
    if current_diff_hash != binding.workspace_diff_hash:
        return (
            f"Verification bypass workspace diff changed after approval at {checkpoint}. "
            "Run verification again or approve a new override for the current code."
        )
    if current_evidence_hash != binding.verification_evidence_sha256:
        return (
            f"Verification bypass evidence changed after approval at {checkpoint}. "
            "Run verification again or approve a new override against the retained evidence."
        )
    return None


def legacy_verification_resume_binding(
    issue: Issue,
    previous_run: RunRecord | None,
    store: Store,
) -> LegacyVerificationResumeBinding | None:
    """Resolve the sole safe pre-v4 approval compatibility case.

    This does not authorize implementation or reinterpret the historical plan.
    It only retains the frozen binding for an environment-blocked verification
    retry when the newly ingested v4 root planning evidence is exactly
    equivalent and the persisted approval is still active.
    """

    current_snapshot = issue.requirements_snapshot
    if (
        previous_run is None
        or previous_run.status != "blocked"
        or previous_run.blocked_phase != "verification_environment"
        or not previous_run.issue_fingerprint
        or not previous_run.plan_spec_hash
        or not previous_run.plan_approval_id
        or current_snapshot is None
        or current_snapshot.schema_version != "jira-requirements/v4"
    ):
        return None
    try:
        frozen_snapshot = store.get_requirements_snapshot(
            issue.identifier,
            previous_run.issue_fingerprint,
        )
    except StoreIntegrityError:
        return None
    if (
        frozen_snapshot is None
        or frozen_snapshot.schema_version
        not in {
            "jira-requirements/v1",
            "jira-requirements/v2",
            "jira-requirements/v3",
        }
    ):
        return None
    approval = store.get_plan_approval(previous_run.plan_approval_id)
    if (
        approval is None
        or approval.get("invalidated_at")
        or approval.get("issue_identifier") != issue.identifier
        or approval.get("plan_spec_hash") != previous_run.plan_spec_hash
        or approval.get("requirements_snapshot_hash")
        != previous_run.issue_fingerprint
    ):
        return None
    if not requirements_planning_authority_equivalent(
        frozen_snapshot,
        current_snapshot,
    ):
        return None
    return LegacyVerificationResumeBinding(
        snapshot=frozen_snapshot,
        requirements_snapshot_hash=previous_run.issue_fingerprint,
    )


class SingleIssueOrchestrator:
    def __init__(
        self,
        workflow: WorkflowDefinition,
        jira: JiraLike,
        store: Store,
        *,
        workspace_manager: WorkspaceManager | None = None,
        codex_runner: CodexRunner | None = None,
        runtime_manager: RuntimeManager | None = None,
        secret_values: list[str | None] | None = None,
    ) -> None:
        self.workflow = workflow
        self.config = workflow.config
        self.jira = jira
        self.store = store
        self.workspace_manager = workspace_manager or WorkspaceManager(self.config.workspace, self.config.hooks)
        self.codex_runner = codex_runner or CodexRunner(
            excluded_environment_names={
                self.config.tracker.auth.token_env,
                self.config.tracker.auth.email_env,
            }
        )
        self.runtime_manager = runtime_manager
        if self.runtime_manager is None and self.config.runtime.enabled:
            self.runtime_manager = RuntimeManager(
                self.config.runtime,
                excluded_environment_names={
                    self.config.tracker.auth.token_env,
                    self.config.tracker.auth.email_env,
                },
            )
        self.secret_values = secret_values or []

    async def run_once(
        self,
        issue_key: str,
        *,
        force: bool = False,
        dry_run: bool = False,
        attempt: int = 1,
        human_input: dict[str, Any] | None = None,
        previous_run: RunRecord | None = None,
        prepared_issue: Issue | None = None,
        precreated_run: RunRecord | None = None,
        resume_handoff_claim_token: str | None = None,
        completed_review_action: dict[str, Any] | None = None,
        review_action_claim_token: str | None = None,
    ) -> OnceResult:
        completed_review = completed_review_action is not None
        if completed_review != bool(review_action_claim_token):
            raise OrchestratorError(
                "completed-review action and claim token must be provided together"
            )
        if completed_review and (
            human_input is not None
            or resume_handoff_claim_token is not None
            or previous_run is None
            or precreated_run is None
            or prepared_issue is None
        ):
            raise OrchestratorError(
                "completed-review execution requires its frozen issue, source run, "
                "reserved result run, and exclusive review-action ownership"
            )
        live_issue = prepared_issue or await self.jira.get_issue(issue_key, include_comments=True)
        if live_issue.identifier != issue_key:
            raise OrchestratorError("prepared Jira issue does not match the requested issue key")
        if completed_review:
            assert completed_review_action is not None
            assert previous_run is not None
            assert precreated_run is not None
            if (
                str(completed_review_action.get("source_run_id") or "")
                != previous_run.id
                or str(completed_review_action.get("result_run_id") or "")
                != precreated_run.id
                or previous_run.status != "completed"
                or precreated_run.issue_identifier != previous_run.issue_identifier
                or precreated_run.workspace_path != previous_run.workspace_path
                or precreated_run.issue_fingerprint != previous_run.issue_fingerprint
                or precreated_run.plan_spec_hash != previous_run.plan_spec_hash
                or precreated_run.automation_plan_hash
                != previous_run.automation_plan_hash
                or precreated_run.automation_development_diff_hash
                != previous_run.automation_development_diff_hash
                or precreated_run.automation_repository_diff_hash
                != previous_run.automation_repository_diff_hash
                or precreated_run.automation_result_hash
                != previous_run.automation_result_hash
                or precreated_run.plan_approval_id != previous_run.plan_approval_id
                or str(
                    completed_review_action.get("automation_plan_hash") or ""
                ).strip()
                != str(previous_run.automation_plan_hash or "").strip()
                or str(
                    completed_review_action.get(
                        "automation_development_diff_hash"
                    )
                    or ""
                ).strip()
                != str(
                    previous_run.automation_development_diff_hash or ""
                ).strip()
                or str(
                    completed_review_action.get(
                        "automation_repository_diff_hash"
                    )
                    or ""
                ).strip()
                != str(
                    previous_run.automation_repository_diff_hash or ""
                ).strip()
                or str(
                    completed_review_action.get("automation_result_hash") or ""
                ).strip()
                != str(previous_run.automation_result_hash or "").strip()
            ):
                raise OrchestratorError(
                    "completed-review action does not match its source and result runs"
                )
        legacy_verification_binding = legacy_verification_resume_binding(
            live_issue,
            previous_run,
            self.store,
        )
        if legacy_verification_binding is not None:
            issue = live_issue.model_copy(
                update={
                    "requirements_snapshot": legacy_verification_binding.snapshot,
                }
            )
            requirements_snapshot_hash = (
                legacy_verification_binding.requirements_snapshot_hash
            )
        else:
            issue = live_issue
            requirements_snapshot_hash = issue_description_fingerprint(issue)
        if not force and not completed_review:
            assert_issue_eligible(live_issue, self.config)

        prompt = render_prompt(self.workflow, issue)
        generation_prompt = prompt
        previous_phase = previous_run.blocked_phase if previous_run else None
        retained_automation_configuration_error = (
            "This continuation retains an automation-plan binding, but automation "
            "is disabled in the active workflow. Re-enable the same automation "
            "configuration before resuming."
            if (
                previous_run is not None
                and previous_run.automation_plan_hash
                and (human_input is not None or completed_review)
                and not self.config.automation.enabled
            )
            else None
        )
        automation_resume = bool(
            self.config.automation.enabled
            and human_input is not None
            and previous_run is not None
            and previous_phase
            in {"automation_planning", "automation_implementation"}
        )
        resume_after_automation = bool(
            self.config.automation.enabled
            and human_input is not None
            and previous_run is not None
            and previous_phase
            in {"review", "verification", "verification_environment"}
        )
        execution_continuation = (
            completed_review
            or automation_resume
            or (
                human_input is not None
                and previous_run is not None
                and previous_phase
                in {
                    "implementation",
                    "review",
                    "verification",
                    "verification_environment",
                }
            )
        )
        if completed_review_action is not None:
            previous_plan_message = str(
                completed_review_action.get("plan_spec") or ""
            ) or None
        else:
            previous_plan_message = (
                read_plan_message_for_run(previous_run, self.config.codex.output_plan_file)
                if previous_run is not None
                else None
            )
        human_response = str((human_input or {}).get("response") or "")
        verification_bypass = verification_bypass_binding(
            previous_run,
            human_input,
        )
        requirements_safety_retry = is_requirements_safety_retry(
            previous_run,
            human_input,
        )
        contradiction_resolution_retry = is_contradiction_resolution_retry(
            previous_run,
            human_input,
        )
        plan_approval_requested = (
            human_input is not None
            and previous_run is not None
            and previous_phase == "planning_approval"
            and is_plan_approval_response(human_response)
        )
        plan_approval_error: str | None = None
        if plan_approval_requested:
            plan_approval_error = validate_bound_plan_approval(
                issue=issue,
                previous_run=previous_run,
                human_input=human_input or {},
                output_plan_file=self.config.codex.output_plan_file,
                requirements_snapshot_hash=requirements_snapshot_hash,
                store=self.store,
            )
        plan_approved_by_human = plan_approval_requested and plan_approval_error is None
        plan_refinement_requested = (
            human_input is not None
            and previous_run is not None
            and previous_phase in {"planning", "planning_approval"}
            and not plan_approved_by_human
            and not requirements_safety_retry
        )
        effective_human_input = dict(human_input or {})
        if plan_approval_error:
            effective_human_input["response"] = (
                "The submitted approval cannot be used because its exact plan/snapshot binding is invalid: "
                f"{plan_approval_error}. Rebuild the PlanSpec against the current requirements snapshot."
            )
        expected_plan_spec_hash: str | None = None
        active_plan_approval_id: str | None = None
        continuation_binding_error: str | None = None
        if plan_approved_by_human:
            expected_plan_spec_hash = (
                str(effective_human_input.get("plan_spec_hash") or "") or None
            )
            active_plan_approval_id = (
                str(effective_human_input.get("approval_id") or "") or None
            )
        elif execution_continuation and previous_run is not None:
            expected_plan_spec_hash = previous_run.plan_spec_hash
            active_plan_approval_id = previous_run.plan_approval_id
            continuation_binding_error = validate_plan_continuation_binding(
                previous_run=previous_run,
                plan_message=previous_plan_message,
                expected_plan_spec_hash=expected_plan_spec_hash,
                plan_approval_id=active_plan_approval_id,
                issue=issue,
                requirements_snapshot_hash=requirements_snapshot_hash,
                approval_required=self.config.codex.require_plan_approval,
                plan_required=(
                    self.config.codex.plan_before_implementation
                    or self.config.runtime.enabled
                    or self.config.automation.enabled
                    or (issue.issue_type or "").strip().lower() == "epic"
                    or contradiction_resolution_retry
                ),
                store=self.store,
                legacy_frozen_plan=legacy_verification_binding is not None,
            )
        if precreated_run is not None:
            if expected_plan_spec_hash is None:
                expected_plan_spec_hash = precreated_run.plan_spec_hash
            if active_plan_approval_id is None:
                active_plan_approval_id = precreated_run.plan_approval_id
            if (
                execution_continuation
                and precreated_run.issue_fingerprint != requirements_snapshot_hash
            ):
                continuation_binding_error = (
                    "Jira requirements changed after the durable human resume was reserved; "
                    "return to planning and obtain a new exact approval."
                )
        if completed_review_action is not None:
            generation_prompt = build_human_review_implementation_prompt(
                issue=issue,
                action=completed_review_action,
                original_prompt=prompt,
            )
        elif plan_approved_by_human:
            generation_prompt = build_approved_plan_implementation_prompt(
                issue=issue,
                original_prompt=prompt,
                previous_run=previous_run,
                human_input=effective_human_input,
                plan_message=previous_plan_message,
            )
        elif plan_refinement_requested:
            generation_prompt = build_plan_refinement_prompt(
                issue=issue,
                original_prompt=prompt,
                previous_run=previous_run,
                human_input=effective_human_input,
                previous_plan_message=previous_plan_message,
            )
        elif verification_bypass is not None:
            generation_prompt = f"""{prompt}

This resume carries a structured human override of the retained failed verification result.
The override is operational authorization only, not a product requirement or permission to edit code.
Preserve the exact approved workspace diff for configured review. If review requires code changes,
the override is consumed and every subsequent change must pass normal verification."""
        elif requirements_safety_retry:
            generation_prompt = f"""{prompt}

Symphony is retrying because refreshed Jira evidence cleared a requirements safety gate.
Use only the canonical Jira requirements snapshot as product authority. The prior dashboard response
was a retry trigger, not a requirement or implementation instruction."""
        elif human_input is not None:
            generation_prompt = build_human_resume_prompt(
                issue=issue,
                original_prompt=prompt,
                previous_run=previous_run,
                human_input=human_input,
            )
            if execution_continuation and previous_plan_message:
                generation_prompt = build_continuation_prompt_with_plan(
                    continuation_prompt=generation_prompt,
                    plan_message=previous_plan_message,
                    plan_spec_hash=expected_plan_spec_hash,
                )
        generation_prompt = add_human_request_contract(generation_prompt)
        if dry_run:
            return OnceResult(issue=issue, prompt=generation_prompt, run=None, workspace=None, dry_run=True)

        if completed_review:
            assert precreated_run is not None
            workspace_path = Path(precreated_run.workspace_path)
            branch_name = precreated_run.branch_name
        else:
            workspace_path = self.workspace_manager.workspace_path_for(issue.identifier)
            branch_name = self.workspace_manager.branch_name_for(issue.identifier)
        if precreated_run is None:
            run = self.store.create_run(
                issue,
                workspace_path,
                branch_name=branch_name,
                attempt=attempt,
                status="queued",
                plan_spec_hash=expected_plan_spec_hash,
                automation_plan_hash=(
                    previous_run.automation_plan_hash
                    if execution_continuation and previous_run is not None
                    else None
                ),
                automation_development_diff_hash=(
                    previous_run.automation_development_diff_hash
                    if execution_continuation and previous_run is not None
                    else None
                ),
                automation_repository_diff_hash=(
                    previous_run.automation_repository_diff_hash
                    if execution_continuation and previous_run is not None
                    else None
                ),
                automation_result_hash=(
                    previous_run.automation_result_hash
                    if execution_continuation and previous_run is not None
                    else None
                ),
                plan_approval_id=active_plan_approval_id,
                require_no_active_run=True,
            )
        else:
            run = precreated_run
            if (
                run.issue_id != issue.id
                or run.issue_identifier != issue.identifier
                or Path(run.workspace_path) != workspace_path
                or run.attempt != attempt
                or run.status != "queued"
                or run.plan_spec_hash != expected_plan_spec_hash
                or run.automation_plan_hash
                != (
                    previous_run.automation_plan_hash
                    if execution_continuation and previous_run is not None
                    else None
                )
                or run.automation_development_diff_hash
                != (
                    previous_run.automation_development_diff_hash
                    if execution_continuation and previous_run is not None
                    else None
                )
                or run.automation_repository_diff_hash
                != (
                    previous_run.automation_repository_diff_hash
                    if execution_continuation and previous_run is not None
                    else None
                )
                or run.automation_result_hash
                != (
                    previous_run.automation_result_hash
                    if execution_continuation and previous_run is not None
                    else None
                )
                or run.plan_approval_id != active_plan_approval_id
            ):
                raise OrchestratorError(
                    "reserved human-resume run does not match its durable handoff"
                )
        if precreated_run is None:
            if resume_handoff_claim_token or review_action_claim_token:
                raise OrchestratorError(
                    "fresh run cannot use a durable resume/action claim"
                )
            run = self.store.update_run(run.id, status="running")
        else:
            if completed_review:
                assert completed_review_action is not None
                assert review_action_claim_token is not None
                started_run = self.store.start_human_review_run(
                    str(completed_review_action["id"]),
                    run.id,
                    review_action_claim_token,
                )
            else:
                if not resume_handoff_claim_token:
                    raise OrchestratorError(
                        "reserved human-resume run is missing its handoff claim"
                    )
                started_run = self.store.start_human_resume_run(
                    run.id,
                    resume_handoff_claim_token,
                )
            if started_run is None:
                raise OrchestratorError(
                    "durable review/resume ownership changed before start"
                )
            run = started_run
        def update_current_run(target_run_id: str, **fields: Any) -> RunRecord:
            if target_run_id != run.id:
                raise OrchestratorError("run update target does not match the active run")
            if review_action_claim_token:
                assert completed_review_action is not None
                owned_run = self.store.update_owned_human_review_run(
                    str(completed_review_action["id"]),
                    target_run_id,
                    review_action_claim_token,
                    **fields,
                )
                if owned_run is None:
                    raise OrchestratorError(
                        "durable human-review ownership changed before run update"
                    )
                return owned_run
            if resume_handoff_claim_token:
                owned_run = self.store.update_owned_human_resume_run(
                    target_run_id, resume_handoff_claim_token, **fields
                )
                if owned_run is None:
                    raise OrchestratorError(
                        "durable human-resume handoff ownership changed before run update"
                    )
                return owned_run
            return self.store.update_run(target_run_id, **fields)

        workspace: WorkspaceInfo | None = None
        status = "failed"
        final_message: str | None = None
        error: str | None = None
        blocked_phase: str | None = None
        verification_status: str | None = None
        verification_output_path: str | None = None
        verification_workspace_diff_hash: str | None = None
        verification_evidence_sha256: str | None = None
        runtime_affected_repositories: tuple[str, ...] = ()
        review_message: str | None = None
        automation_plan: AutomationPlan | None = None
        automation_plan_message: str | None = None
        automation_result_message: str | None = None
        trusted_development_plan: PlanSpec | None = None
        automation_plan_hash_for_run: str | None = run.automation_plan_hash
        automation_development_diff_hash_for_run: str | None = (
            run.automation_development_diff_hash
        )
        automation_repository_diff_hash_for_run: str | None = (
            run.automation_repository_diff_hash
        )
        automation_result_hash_for_run: str | None = run.automation_result_hash
        completed_review_automation_replan = False
        verification_bypass_active = verification_bypass is not None
        verification_bypass_review_consumed = False
        verification_bypass_plan: PlanSpec | None = None
        previous_review_history = str(
            (completed_review_action or {}).get("source_review_history") or ""
        ).strip()
        previous_review = str(
            (completed_review_action or {}).get("source_review") or ""
        ).strip()
        if previous_review_history:
            review_history: list[str] = [previous_review_history]
        elif previous_review:
            review_history = [
                "## Review retained from source completed run\n\n"
                + previous_review
            ]
        else:
            review_history = []

        def freeze_failed_verification_binding() -> None:
            nonlocal error
            nonlocal verification_workspace_diff_hash
            nonlocal verification_evidence_sha256

            if workspace is None:
                binding_error = "failed verification workspace is unavailable"
            else:
                try:
                    (
                        verification_workspace_diff_hash,
                        verification_evidence_sha256,
                    ) = capture_failed_verification_binding(
                        workspace_path=workspace.path,
                        output_plan_file=self.config.codex.output_plan_file,
                        expected_plan_spec_hash=expected_plan_spec_hash,
                        issue=issue,
                        requirements_snapshot_hash=requirements_snapshot_hash,
                        evidence_path=verification_output_path,
                        config=self.config,
                        legacy_frozen_plan=legacy_verification_binding is not None,
                    )
                    return
                except (HumanReviewContextError, PlanSpecError) as exc:
                    binding_error = str(exc)
            redacted_binding_error = self.redact(binding_error) or binding_error
            error = (
                f"{error}. Verification-time integrity binding unavailable: "
                f"{redacted_binding_error}"
            )
            self.store.add_log(
                run.id,
                "error",
                "Failed to freeze verification-time code and evidence binding: "
                f"{redacted_binding_error}",
                verification_output_path,
            )

        try:
            if retained_automation_configuration_error:
                raise AutomationBindingConfigurationError(
                    retained_automation_configuration_error
                )
            if self.config.tracker.comment_on_start and not completed_review:
                await self._post_start_comment(issue, run.id, workspace_path, branch_name)

            safety_error = requirements_planning_safety_error(live_issue)
            if safety_error:
                raise PlanningSafetyGateBlocked(safety_error)
            if completed_review:
                if not workspace_path.is_dir():
                    raise WorkspaceError(
                        f"Persisted completed-review workspace is missing: {workspace_path}"
                    )
                workspace = WorkspaceInfo(
                    issue_identifier=issue.identifier,
                    path=workspace_path,
                    created=False,
                    branch_name=branch_name,
                )
            else:
                workspace = await self.workspace_manager.prepare(
                    issue.identifier,
                    hook_context=self.hook_context(issue),
                )
            if (
                legacy_verification_binding is not None
                and live_issue.requirements_snapshot is not None
            ):
                # Retain the current v4 version for audit without changing the
                # active workspace link away from the exact approved snapshot.
                self.store.save_requirements_snapshot(
                    live_issue.requirements_snapshot
                )
            self._persist_requirements_snapshot(issue, workspace.path)

            if self.config.hooks.before_run and not completed_review:
                before = await self.workspace_manager.run_hook(
                    "before_run",
                    self.config.hooks.before_run,
                    workspace.path,
                    hook_context=self.hook_context(issue, workspace),
                )
                if not before.succeeded:
                    raise WorkspaceError(f"before_run hook failed; see {before.log_path}")
            elif self.config.hooks.before_run and completed_review:
                self.store.add_log(
                    run.id,
                    "info",
                    "Skipped before_run hook to preserve the frozen completed-review workspace diff.",
                )

            total_event_offset = 0
            plan_message: str | None = (
                previous_plan_message if (plan_approved_by_human or execution_continuation) else None
            )
            run_implementation = True
            issue_requires_plan = (
                self.config.runtime.enabled
                or self.config.automation.enabled
                or (issue.issue_type or "").strip().lower() == "epic"
                or contradiction_resolution_retry
            )
            should_run_planning = (
                (self.config.codex.plan_before_implementation or issue_requires_plan)
                and not plan_approved_by_human
                and not completed_review
                and (human_input is None or plan_refinement_requested or requirements_safety_retry)
            )
            if should_run_planning:
                if plan_refinement_requested:
                    plan_prompt = generation_prompt
                else:
                    plan_prompt = build_planning_prompt(
                        issue=issue,
                        implementation_prompt=generation_prompt,
                        planning_instructions=self.config.codex.planning_prompt,
                        requirements_snapshot_hash=requirements_snapshot_hash,
                        automation_repository=(
                            self.config.automation.workspace_subdir.as_posix()
                            if self.config.automation.enabled
                            else None
                        ),
                    )
                plan_config = self.config.codex.model_copy(
                    update={"output_last_message_file": self.config.codex.output_plan_file}
                )
                plan_result, total_event_offset = await self._run_codex_pass(
                    prompt=plan_prompt,
                    workspace_path=workspace.path,
                    config=plan_config,
                    run_id=run.id,
                    event_offset=total_event_offset,
                    event_prefix="plan",
                )
                status = plan_result.status
                plan_message = plan_result.final_message
                error = self.redact(plan_result.error)
                plan_human_request = parse_human_request(plan_message or plan_result.error)
                if plan_human_request:
                    status = "blocked"
                    final_message = plan_message
                    error = plan_human_request
                    blocked_phase = "planning"
                    run_implementation = False
                elif status != "completed":
                    final_message = plan_message
                    blocked_phase = "planning"
                    run_implementation = False
                else:
                    plan_requires_approval = self.config.codex.require_plan_approval
                    plan_spec: PlanSpec | None = None
                    repair_attempt = 0
                    while plan_spec is None and run_implementation:
                        try:
                            plan_spec = validate_and_normalize_generated_plan_spec(
                                plan_message or "",
                                issue=issue,
                                requirements_snapshot_hash=requirements_snapshot_hash,
                                workspace_path=workspace.path,
                            )
                            if (
                                self.config.automation.enabled
                                and self.config.automation.workspace_subdir.as_posix()
                                in plan_spec.affected_surface.repositories
                            ):
                                raise PlanSpecError(
                                    "Development PlanSpec must not include the separately "
                                    "managed automation repository; Symphony plans automation "
                                    "only after the development pass."
                                )
                        except PlanSpecError as exc:
                            if (
                                repair_attempt >= MAX_PLAN_SPEC_REPAIR_ATTEMPTS
                                or not is_repairable_plan_spec_error(exc)
                            ):
                                status = "blocked"
                                blocked_phase = "planning"
                                final_message = plan_message
                                error = str(exc)
                                if repair_attempt:
                                    error += (
                                        ". Automatic PlanSpec repair remained invalid "
                                        f"after {repair_attempt} attempt(s)."
                                    )
                                run_implementation = False
                                break

                            repair_attempt += 1
                            self.store.add_log(
                                run.id,
                                "warning",
                                "PlanSpec validation found a repairable model-output "
                                f"error; running automatic repair attempt "
                                f"{repair_attempt}/{MAX_PLAN_SPEC_REPAIR_ATTEMPTS}: "
                                f"{self.redact(str(exc))}",
                            )
                            repair_prompt = build_plan_spec_repair_prompt(
                                issue=issue,
                                invalid_plan_message=plan_message or "",
                                validation_error=exc,
                                requirements_snapshot_hash=requirements_snapshot_hash,
                                attempt=repair_attempt,
                            )
                            plan_result, total_event_offset = await self._run_codex_pass(
                                prompt=repair_prompt,
                                workspace_path=workspace.path,
                                config=plan_config,
                                run_id=run.id,
                                event_offset=total_event_offset,
                                event_prefix=f"plan-repair-{repair_attempt}",
                            )
                            status = plan_result.status
                            plan_message = plan_result.final_message
                            error = self.redact(plan_result.error)
                            plan_human_request = parse_human_request(
                                plan_message or plan_result.error
                            )
                            if plan_human_request:
                                status = "blocked"
                                final_message = plan_message
                                error = plan_human_request
                                blocked_phase = "planning"
                                run_implementation = False
                            elif status != "completed":
                                final_message = plan_message
                                blocked_phase = "planning"
                                run_implementation = False

                    if plan_spec is not None:
                        plan_message = plan_spec.canonical_json(indent=2)
                        expected_plan_spec_hash = plan_spec.content_hash()
                        if active_plan_approval_id:
                            self.store.invalidate_plan_approval(
                                active_plan_approval_id,
                                "a new PlanSpec was generated during replanning",
                            )
                        active_plan_approval_id = None
                        update_current_run(
                            run.id,
                            plan_spec_hash=expected_plan_spec_hash,
                            plan_approval_id=None,
                        )
                        write_plan_spec_file(
                            workspace.path,
                            self.config.codex.output_plan_file,
                            plan_message,
                        )
                        blocking_question = plan_spec.blocking_question()
                        if blocking_question:
                            status = "blocked"
                            blocked_phase = "planning"
                            final_message = plan_message
                            error = blocking_question
                            run_implementation = False
                        if plan_spec.epic_strategy and plan_spec.epic_strategy.mode == "single_change":
                            plan_requires_approval = True
                        if plan_spec.epic_strategy and plan_spec.epic_strategy.mode == "decomposed":
                            child_issue_keys = ", ".join(
                                child.issue_key
                                for child in plan_spec.epic_strategy.bounded_child_plans
                            )
                            status = "blocked"
                            blocked_phase = "planning"
                            final_message = plan_message
                            error = (
                                "Epic PlanSpec was decomposed into validated bounded child issues: "
                                f"{child_issue_keys}. The root Epic is non-executable. Launch separate "
                                "Symphony runs for each listed child issue and plan/approve each run "
                                "independently."
                            )
                            run_implementation = False
                    if run_implementation and plan_requires_approval:
                        status = "blocked"
                        blocked_phase = "planning_approval"
                        final_message = plan_message
                        error = "Plan/spec is ready. Confirm the plan in the dashboard or provide adjustments before implementation."
                        run_implementation = False
                    elif run_implementation:
                        generation_prompt = build_implementation_prompt_with_plan(
                            implementation_prompt=generation_prompt,
                            plan_message=plan_message,
                        )

            if run_implementation and continuation_binding_error:
                status = "blocked"
                if active_plan_approval_id:
                    self.store.invalidate_plan_approval(
                        active_plan_approval_id, continuation_binding_error
                    )
                error = continuation_binding_error
                blocked_phase = "planning"
                run_implementation = False

            if run_implementation and completed_review_action is not None:
                assert review_action_claim_token is not None
                artifact_error = validate_frozen_snapshot_artifacts(
                    workspace.path,
                    requirements_snapshot_hash,
                )
                if artifact_error:
                    status = "blocked"
                    error = artifact_error
                    blocked_phase = "review"
                    run_implementation = False
                else:
                    frozen_plan = parse_plan_spec(
                        previous_plan_message or "",
                        expected_issue_key=issue.identifier,
                        expected_snapshot_hash=requirements_snapshot_hash,
                        requirements_snapshot=issue.requirements_snapshot,
                    )
                    if frozen_plan.content_hash() != expected_plan_spec_hash:
                        raise HumanReviewContextError(
                            "Frozen human-review PlanSpec content does not match "
                            "the source run's trusted PlanSpec hash"
                        )
                    frozen_approval = completed_review_action.get("approval")
                    if active_plan_approval_id:
                        current_approval = self.store.get_plan_approval(
                            active_plan_approval_id
                        )
                        if current_approval != frozen_approval:
                            raise HumanReviewContextError(
                                "Frozen human-review approval no longer matches "
                                "the exact persisted approval record"
                            )
                    elif frozen_approval is not None:
                        raise HumanReviewContextError(
                            "Frozen human-review context contains an unexpected approval"
                        )
                    submitted_diff_hash = str(
                        completed_review_action.get("workspace_diff_hash") or ""
                    )
                    submitted_diff_content = str(
                        completed_review_action.get("workspace_diff") or ""
                    )
                    current_diff = capture_workspace_diff(
                        workspace.path,
                        frozen_plan,
                        managed_repositories=managed_diff_repositories(
                            self.config
                        ),
                    )
                    if (
                        current_diff.content_hash != submitted_diff_hash
                        or current_diff.content != submitted_diff_content
                    ):
                        status = "blocked"
                        error = (
                            "Workspace diff changed after the human review was "
                            "submitted. Reconcile the retained workspace before "
                            "addressing the frozen review."
                        )
                        blocked_phase = "review"
                        run_implementation = False

            if run_implementation and completed_review_action is not None:
                triage_prompt = build_human_review_triage_prompt(
                    issue=issue,
                    action=completed_review_action,
                    triage_instructions=(
                        self.config.codex.human_review_triage_prompt
                    ),
                )
                triage_result, total_event_offset = await self._run_codex_pass(
                    prompt=triage_prompt,
                    workspace_path=workspace.path,
                    config=read_only_codex_config(self.config.codex),
                    run_id=run.id,
                    event_offset=total_event_offset,
                    event_prefix="human_review.triage",
                )
                triage_output = (
                    triage_result.final_message
                    or triage_result.error
                    or ""
                )
                if triage_result.status != "completed":
                    triage_decision = "invalid"
                    triage_reason = self.redact(
                        triage_result.error
                        or "Human review triage pass failed"
                    ) or "Human review triage pass failed"
                    status = triage_result.status
                    error = triage_reason
                    blocked_phase = "review"
                    run_implementation = False
                else:
                    after_triage_diff = capture_workspace_diff(
                        workspace.path,
                        frozen_plan,
                        managed_repositories=managed_diff_repositories(
                            self.config
                        ),
                    )
                    if (
                        after_triage_diff.content_hash != submitted_diff_hash
                        or after_triage_diff.content != submitted_diff_content
                    ):
                        triage_decision = "invalid"
                        triage_reason = (
                            "Read-only human review triage changed the workspace; "
                            "execution stopped."
                        )
                        status = "blocked"
                        error = triage_reason
                        blocked_phase = "review"
                        run_implementation = False
                    else:
                        triage_decision, triage_reason = (
                            classify_human_review_triage(triage_output)
                        )
                        if triage_decision == "automation_plan_changes_required":
                            if not completed_review_action.get(
                                "automation_plan_hash"
                            ):
                                status = "blocked"
                                error = (
                                    "Human review requested automation replanning, "
                                    "but the source run has no automation plan."
                                )
                                blocked_phase = "review"
                                run_implementation = False
                            else:
                                completed_review_automation_replan = True
                        elif triage_decision == "plan_changes_required":
                            reason = (
                                triage_reason
                                or "Human review changes the approved PlanSpec."
                            )
                            if active_plan_approval_id:
                                self.store.invalidate_plan_approval(
                                    active_plan_approval_id,
                                    reason,
                                )
                            status = "blocked"
                            error = (
                                "Human review requires replanning and a new "
                                f"approval before code changes: {reason}"
                            )
                            blocked_phase = "planning"
                            run_implementation = False
                        elif triage_decision in {"needs_human", "invalid"}:
                            status = "blocked"
                            error = triage_reason
                            blocked_phase = "review"
                            run_implementation = False

                recorded = self.store.record_owned_human_review_triage(
                    str(completed_review_action["id"]),
                    review_action_claim_token,
                    decision=triage_decision,
                    output=triage_output,
                )
                if not recorded:
                    raise OrchestratorError(
                        "human-review action ownership changed before triage "
                        "could be recorded"
                    )
            development_review_scope_prompt = generation_prompt
            review_scope_prompt = development_review_scope_prompt
            if self.config.automation.enabled and run_implementation:
                trusted_development_plan = parse_plan_spec(
                    plan_message or "",
                    expected_issue_key=issue.identifier,
                    expected_snapshot_hash=requirements_snapshot_hash,
                    issue_type=issue.issue_type,
                    requirements_snapshot=issue.requirements_snapshot,
                )
                if trusted_development_plan.content_hash() != expected_plan_spec_hash:
                    raise PlanSpecError(
                        "Automation requires the exact trusted development PlanSpec"
                    )
                retained_automation_hash: str | None = None
                retained_automation_content: str | None = None
                retained_automation_result: str | None = None
                if completed_review_action is not None:
                    retained_automation_hash = str(
                        completed_review_action.get("automation_plan_hash") or ""
                    ).strip() or None
                    retained_automation_content = str(
                        completed_review_action.get("automation_plan") or ""
                    ).strip() or None
                    retained_automation_result = str(
                        completed_review_action.get("automation_result") or ""
                    ).strip() or None
                elif resume_after_automation:
                    assert previous_run is not None
                    retained_automation_hash = str(
                        previous_run.automation_plan_hash or ""
                    ).strip() or None

                if resume_after_automation and not retained_automation_hash:
                    status = "blocked"
                    error = (
                        "The retained post-automation run has no exact automation-plan "
                        "binding. Return to automation planning before continuing."
                    )
                    blocked_phase = "automation_planning"
                    run_implementation = False
                elif retained_automation_hash:
                    if not (
                        automation_development_diff_hash_for_run
                        and automation_repository_diff_hash_for_run
                        and automation_result_hash_for_run
                    ):
                        status = "blocked"
                        error = (
                            "The retained automation run is missing its exact "
                            "development, repository, or result hash binding."
                        )
                        blocked_phase = (
                            "review" if completed_review else "automation_planning"
                        )
                        run_implementation = False
                        retained_automation_hash = None
                if retained_automation_hash and run_implementation:
                    try:
                        (
                            automation_plan,
                            automation_plan_message,
                            automation_result_message,
                        ) = load_bound_automation_context(
                            workspace_path=workspace.path,
                            config=self.config,
                            issue=issue,
                            requirements_snapshot_hash=requirements_snapshot_hash,
                            development_plan=trusted_development_plan,
                            development_plan_spec_hash=expected_plan_spec_hash or "",
                            expected_automation_plan_hash=retained_automation_hash,
                            expected_development_diff_hash=(
                                automation_development_diff_hash_for_run or ""
                            ),
                            expected_repository_diff_hash=(
                                automation_repository_diff_hash_for_run or ""
                            ),
                            expected_result_hash=(
                                automation_result_hash_for_run or ""
                            ),
                            plan_content=retained_automation_content,
                            result_content=retained_automation_result,
                        )
                    except (AutomationPlanError, HumanReviewContextError) as exc:
                        status = "blocked"
                        error = self.redact(str(exc))
                        blocked_phase = (
                            "review" if completed_review else "automation_planning"
                        )
                        run_implementation = False
                    else:
                        automation_plan_hash_for_run = automation_plan.content_hash()
                        review_scope_prompt = build_combined_implementation_scope_prompt(
                            development_prompt=development_review_scope_prompt,
                            automation_plan_message=automation_plan_message,
                            automation_result_message=automation_result_message,
                            automation_repository=(
                                self.config.automation.workspace_subdir.as_posix()
                            ),
                        )
            generation_pass = 1
            force_automation_refresh = completed_review_automation_replan
            automation_refresh_feedback: str | None = (
                str(completed_review_action.get("comments") or "")
                if completed_review_automation_replan
                and completed_review_action is not None
                else None
            )
            skip_development_for_automation_replan = (
                completed_review_automation_replan
            )
            while run_implementation:
                requirements_change = await self._requirements_checkpoint_error(
                    issue,
                    requirements_snapshot_hash,
                    checkpoint="implementation",
                    workspace_path=workspace.path,
                    frozen=completed_review,
                    legacy_verification_binding=legacy_verification_binding,
                )
                if requirements_change:
                    status = "blocked"
                    error = requirements_change
                    blocked_phase = "planning"
                    break
                plan_change = validate_plan_artifact(
                    workspace.path,
                    self.config.codex.output_plan_file,
                    expected_hash=expected_plan_spec_hash,
                    issue=issue,
                    requirements_snapshot_hash=requirements_snapshot_hash,
                    legacy_frozen_plan=legacy_verification_binding is not None,
                )
                if plan_change:
                    if active_plan_approval_id:
                        self.store.invalidate_plan_approval(active_plan_approval_id, plan_change)
                    status = "blocked"
                    error = plan_change
                    blocked_phase = "planning"
                    break
                approval_change = validate_active_plan_approval_binding(
                    store=self.store,
                    approval_id=active_plan_approval_id,
                    issue=issue,
                    expected_plan_spec_hash=expected_plan_spec_hash,
                    requirements_snapshot_hash=requirements_snapshot_hash,
                )
                if approval_change:
                    status = "blocked"
                    error = approval_change
                    blocked_phase = "planning"
                    break
                verification_environment_retry = bool(
                    human_input is not None
                    and previous_run is not None
                    and previous_phase == "verification_environment"
                    and generation_pass == 1
                    and not verification_bypass_active
                )
                verification_resume_without_codex = bool(
                    verification_environment_retry
                    or (
                        verification_bypass_active
                        and generation_pass == 1
                    )
                )
                automation_resume_without_development = bool(
                    automation_resume and generation_pass == 1
                )
                review_resume_without_development = bool(
                    human_input is not None
                    and previous_run is not None
                    and previous_phase == "review"
                    and generation_pass == 1
                )
                implementation_resume_without_codex = bool(
                    verification_resume_without_codex
                    or automation_resume_without_development
                    or review_resume_without_development
                    or skip_development_for_automation_replan
                )
                codex_result: CodexRunResult | None = None
                if implementation_resume_without_codex:
                    status = "completed"
                    if not skip_development_for_automation_replan:
                        assert previous_run is not None
                        final_message = read_plan_message_for_run(
                            previous_run,
                            self.config.codex.output_last_message_file,
                        ) or previous_run.final_message
                    error = None
                    if verification_environment_retry:
                        self.store.add_log(
                            run.id,
                            "info",
                            "Skipped Codex implementation for an environment-only "
                            "resume; rerunning the configured verification against "
                            "the unchanged workspace.",
                        )
                else:
                    codex_result, total_event_offset = await self._run_codex_pass(
                        prompt=generation_prompt,
                        workspace_path=workspace.path,
                        config=self.config.codex,
                        run_id=run.id,
                        event_offset=total_event_offset,
                    )
                    status = codex_result.status
                    final_message = codex_result.final_message
                    error = self.redact(codex_result.error)
                    if status != "completed":
                        blocked_phase = "implementation"
                        break

                completed_review_implementation_decision = (
                    classify_review_decision(final_message or "")
                    if not implementation_resume_without_codex and completed_review
                    else "invalid"
                )
                if (
                    completed_review_implementation_decision
                    == "automation_plan_changes_required"
                ):
                    force_automation_refresh = True
                    automation_refresh_feedback = final_message
                if (
                    completed_review_implementation_decision
                    == "plan_changes_required"
                ):
                    review_payload = parse_review_json(final_message or "") or {}
                    reason = str(
                        review_payload.get("reason")
                        or "Implementation discovered that the human feedback "
                        "requires changing the approved PlanSpec."
                    )
                    if active_plan_approval_id:
                        self.store.invalidate_plan_approval(
                            active_plan_approval_id,
                            reason,
                        )
                    status = "blocked"
                    error = (
                        "Human review requires replanning and a new approval "
                        f"before code changes: {reason}"
                    )
                    blocked_phase = "planning"
                    break
                binding_change = await self._execution_binding_error(
                    issue,
                    requirements_snapshot_hash,
                    checkpoint="completion after implementation pass",
                    workspace_path=workspace.path,
                    expected_plan_spec_hash=expected_plan_spec_hash,
                    active_plan_approval_id=active_plan_approval_id,
                    frozen_requirements=completed_review,
                    legacy_verification_binding=legacy_verification_binding,
                )
                if binding_change:
                    status = "blocked"
                    error = binding_change
                    blocked_phase = "planning"
                    break

                implementation_human_request = (
                    None
                    if implementation_resume_without_codex
                    else parse_human_request(
                        final_message or (codex_result.error if codex_result else None)
                    )
                )
                if implementation_human_request:
                    status = "blocked"
                    error = implementation_human_request
                    blocked_phase = "implementation"
                    break

                current_development_diff = None
                current_automation_repository_diff = None
                refresh_automation = False
                if self.config.automation.enabled:
                    assert trusted_development_plan is not None
                    current_development_diff = capture_workspace_diff(
                        workspace.path,
                        trusted_development_plan,
                        managed_repositories=managed_workspace_repositories(
                            self.config
                        ),
                    )
                    current_automation_repository_diff = (
                        capture_automation_repository_diff(
                            workspace.path,
                            trusted_development_plan,
                            self.config,
                        )
                    )
                    refresh_automation = bool(
                        force_automation_refresh
                        or automation_plan is None
                        or automation_plan.development_workspace_diff_hash
                        != current_development_diff.content_hash
                    )

                if (
                    self.config.automation.enabled
                    and not verification_resume_without_codex
                    and refresh_automation
                ):
                    assert trusted_development_plan is not None
                    refresh_human_input: dict[str, Any] | None = None
                    if automation_refresh_feedback:
                        refresh_human_input = {
                            "question": "Independent review requested code changes.",
                            "response": automation_refresh_feedback,
                        }
                    elif automation_resume:
                        refresh_human_input = human_input
                    elif completed_review_action is not None:
                        refresh_human_input = {
                            "question": "Completed-work human review requested changes.",
                            "response": str(
                                completed_review_action.get("comments") or ""
                            ),
                        }
                    retained_automation_changes = bool(
                        automation_plan is not None
                        or resume_after_automation
                        or completed_review_action is not None
                    )
                    prior_automation_plan_hash = (
                        automation_plan.content_hash()
                        if automation_plan is not None
                        else (
                            str(previous_run.automation_plan_hash or "").strip()
                            if automation_resume and previous_run is not None
                            else None
                        )
                    )
                    if automation_resume:
                        assert previous_run is not None
                        if not (
                            previous_run.automation_development_diff_hash
                            and previous_run.automation_repository_diff_hash
                        ):
                            status = "blocked"
                            error = (
                                "The blocked automation attempt has no exact "
                                "development/repository diff binding and cannot be "
                                "resumed safely."
                            )
                            blocked_phase = "automation_planning"
                            break
                    if not automation_resume:
                        automation_development_diff_hash_for_run = (
                            current_development_diff.content_hash
                        )
                    if automation_repository_diff_hash_for_run is None:
                        assert current_automation_repository_diff is not None
                        automation_repository_diff_hash_for_run = (
                            current_automation_repository_diff.content_hash
                        )
                    update_current_run(
                        run.id,
                        automation_development_diff_hash=(
                            automation_development_diff_hash_for_run
                        ),
                        automation_repository_diff_hash=(
                            automation_repository_diff_hash_for_run
                        ),
                    )
                    automation_stage = await self._run_automation_stage(
                        issue=issue,
                        run_id=run.id,
                        workspace_path=workspace.path,
                        development_plan=trusted_development_plan,
                        development_plan_message=plan_message or "",
                        development_plan_spec_hash=expected_plan_spec_hash or "",
                        active_plan_approval_id=active_plan_approval_id,
                        development_final_message=final_message,
                        requirements_snapshot_hash=requirements_snapshot_hash,
                        event_offset=total_event_offset,
                        human_input=refresh_human_input,
                        previous_phase=(
                            previous_phase
                            if automation_resume
                            else (
                                "automation_implementation"
                                if retained_automation_changes
                                else None
                            )
                        ),
                        frozen_requirements=completed_review,
                        expected_prior_plan_hash=(
                            prior_automation_plan_hash or None
                        ),
                        expected_prior_development_diff_hash=(
                            previous_run.automation_development_diff_hash
                            if automation_resume and previous_run is not None
                            else None
                        ),
                        expected_prior_repository_diff_hash=(
                            automation_repository_diff_hash_for_run
                        ),
                    )
                    total_event_offset = automation_stage.event_offset
                    automation_plan = automation_stage.plan
                    automation_plan_message = automation_stage.plan_message
                    automation_result_message = automation_stage.result_message
                    if automation_plan is not None:
                        automation_plan_hash_for_run = automation_plan.content_hash()
                        if automation_stage.trusted_repository_diff_hash:
                            automation_repository_diff_hash_for_run = (
                                automation_stage.trusted_repository_diff_hash
                            )
                        if (
                            automation_stage.status == "completed"
                            or automation_stage.trusted_repository_diff_hash is None
                        ):
                            try:
                                current_automation_repository_diff = (
                                    capture_automation_repository_diff(
                                        workspace.path,
                                        trusted_development_plan,
                                        self.config,
                                    )
                                )
                                current_automation_repository_state = (
                                    inspect_automation_repository(
                                        workspace.path,
                                        self.config.automation.workspace_subdir.as_posix(),
                                        expected_head_sha=(
                                            automation_plan.repository_baseline_sha
                                        ),
                                        expected_branch_name=issue.identifier,
                                        require_clean=False,
                                    )
                                )
                            except (AutomationPlanError, HumanReviewContextError):
                                # Preserve the stage's trusted pre-pass binding. The
                                # completed-path validator below will surface any race;
                                # a blocked stage keeps its original actionable error.
                                pass
                            else:
                                planned_automation_file_types = {
                                    change.path: change.change_type
                                    for change in automation_plan.affected_file_changes
                                }
                                repository_state_is_bindable = (
                                    not current_automation_repository_state.dirty
                                    if automation_plan.decision
                                    == "no_update_required"
                                    else all(
                                        planned_automation_file_types.get(path)
                                        == change_type
                                        for path, change_type in (
                                            current_automation_repository_state.changed_file_types
                                        )
                                    )
                                )
                                if repository_state_is_bindable:
                                    automation_repository_diff_hash_for_run = (
                                        current_automation_repository_diff.content_hash
                                    )
                        automation_development_diff_hash_for_run = (
                            current_development_diff.content_hash
                        )
                        automation_result_hash_for_run = (
                            automation_result_content_hash(
                                automation_result_message or ""
                            )
                            if automation_stage.status == "completed"
                            else None
                        )
                        update_current_run(
                            run.id,
                            automation_plan_hash=automation_plan_hash_for_run,
                            automation_development_diff_hash=(
                                automation_development_diff_hash_for_run
                            ),
                            automation_repository_diff_hash=(
                                automation_repository_diff_hash_for_run
                            ),
                            automation_result_hash=automation_result_hash_for_run,
                        )
                    if automation_stage.status != "completed":
                        status = automation_stage.status
                        error = automation_stage.error
                        blocked_phase = automation_stage.blocked_phase
                        break
                    force_automation_refresh = False
                    automation_refresh_feedback = None
                    skip_development_for_automation_replan = False

                if self.config.automation.enabled and automation_plan is not None:
                    assert trusted_development_plan is not None
                    current_development_diff = current_development_diff or capture_workspace_diff(
                        workspace.path,
                        trusted_development_plan,
                        managed_repositories=managed_workspace_repositories(
                            self.config
                        ),
                    )
                    automation_state_error = validate_bound_automation_state(
                        workspace_path=workspace.path,
                        config=self.config,
                        issue=issue,
                        requirements_snapshot_hash=requirements_snapshot_hash,
                        development_plan=trusted_development_plan,
                        development_plan_spec_hash=expected_plan_spec_hash or "",
                        development_diff_hash=current_development_diff.content_hash,
                        automation_plan=automation_plan,
                        expected_repository_diff_hash=(
                            automation_repository_diff_hash_for_run
                        ),
                        expected_result_hash=automation_result_hash_for_run,
                    )
                    if automation_state_error:
                        status = "blocked"
                        error = automation_state_error
                        blocked_phase = "automation_planning"
                        break
                    final_message = append_automation_to_final(
                        final_message,
                        automation_plan,
                        automation_result_message,
                    )
                    review_scope_prompt = build_combined_implementation_scope_prompt(
                        development_prompt=generation_prompt,
                        automation_plan_message=automation_plan_message,
                        automation_result_message=automation_result_message,
                        automation_repository=(
                            self.config.automation.workspace_subdir.as_posix()
                        ),
                    )

                if verification_bypass_active:
                    assert previous_run is not None
                    assert verification_bypass is not None
                    verification_status = previous_run.verification_status
                    verification_output_path = previous_run.verification_output_path
                    if legacy_verification_binding is not None:
                        verification_bypass_plan = parse_frozen_legacy_plan_spec(
                            previous_plan_message or "",
                            expected_issue_key=issue.identifier,
                            expected_snapshot_hash=requirements_snapshot_hash,
                            issue_type=issue.issue_type,
                            requirements_snapshot=issue.requirements_snapshot,
                        )
                    else:
                        verification_bypass_plan = parse_plan_spec(
                            previous_plan_message or "",
                            expected_issue_key=issue.identifier,
                            expected_snapshot_hash=requirements_snapshot_hash,
                            issue_type=issue.issue_type,
                            requirements_snapshot=issue.requirements_snapshot,
                        )
                    if (
                        verification_bypass_plan.content_hash()
                        != expected_plan_spec_hash
                    ):
                        raise PlanSpecError(
                            "Verification bypass PlanSpec does not match the trusted plan hash"
                        )
                    bypass_integrity_error = verification_bypass_integrity_error(
                        verification_bypass,
                        workspace_path=workspace.path,
                        plan_spec=verification_bypass_plan,
                        managed_repositories=managed_diff_repositories(
                            self.config
                        ),
                        checkpoint="resume before review",
                    )
                    if bypass_integrity_error:
                        status = "blocked"
                        error = bypass_integrity_error
                        blocked_phase = "verification"
                        break
                    if self.config.runtime.enabled:
                        runtime_affected_repositories = tuple(
                            dict.fromkeys(
                                verification_bypass_plan.affected_surface.repositories
                            )
                        )
                    self.store.add_log(
                        run.id,
                        "warning",
                        "Verification failure was explicitly overridden by "
                        f"{verification_bypass.approver_identity}. The approved workspace "
                        "diff and verification evidence were revalidated; configured review "
                        "will still run before handoff.",
                        verification_output_path,
                    )

                hook_verification_status = "not_configured"
                hook_verification_output_path: str | None = None
                if not verification_bypass_active and self.config.hooks.verify:
                    verify = await self.workspace_manager.run_hook(
                        "verify",
                        self.config.hooks.verify,
                        workspace.path,
                        hook_context=self.hook_context(issue, workspace),
                    )
                    hook_verification_status = (
                        "passed" if verify.succeeded else "failed"
                    )
                    hook_verification_output_path = str(verify.log_path)
                    verification_status = hook_verification_status
                    verification_output_path = hook_verification_output_path
                    if not verify.succeeded:
                        if self.config.hooks.verify_required:
                            status = "blocked"
                            error = self.redact(
                                "Required verification hook failed; see "
                                f"{hook_verification_output_path}"
                            )
                            blocked_phase = "verification"
                            freeze_failed_verification_binding()
                            break
                        self.store.add_log(
                            run.id,
                            "warning",
                            "Verification hook failed; continuing because verification is advisory.",
                            verification_output_path,
                        )

                if not verification_bypass_active and self.config.runtime.enabled:
                    (
                        runtime_verification_status,
                        runtime_manifest_path,
                        runtime_error,
                        runtime_affected_repositories,
                    ) = await self._run_runtime_verification(
                        run_id=run.id,
                        issue=issue,
                        workspace_path=workspace.path,
                        expected_plan_spec_hash=expected_plan_spec_hash,
                        requirements_snapshot_hash=requirements_snapshot_hash,
                        hook_status=hook_verification_status,
                        hook_output_path=hook_verification_output_path,
                        legacy_frozen_plan=legacy_verification_binding is not None,
                    )
                    verification_status = aggregate_verification_status(
                        hook_verification_status,
                        runtime_verification_status,
                    )
                    verification_output_path = runtime_manifest_path
                    if runtime_verification_status != "passed":
                        runtime_error = self.redact(
                            runtime_error
                            or "Runtime verification did not pass; see "
                            f"{runtime_manifest_path or 'the run log'}"
                        )
                        if self.config.runtime.required:
                            status = "blocked"
                            error = runtime_error
                            blocked_phase = (
                                "verification_environment"
                                if runtime_verification_status
                                == "environment_blocked"
                                else "verification"
                            )
                            freeze_failed_verification_binding()
                            break
                        self.store.add_log(
                            run.id,
                            "warning",
                            "Runtime verification failed; continuing because "
                            "runtime verification is advisory. " + runtime_error,
                            runtime_manifest_path,
                        )
                elif (
                    not verification_bypass_active
                    and not self.config.hooks.verify
                ):
                    verification_status = "not_configured"

                binding_change = await self._execution_binding_error(
                    issue,
                    requirements_snapshot_hash,
                    checkpoint="finalization after verification",
                    workspace_path=workspace.path,
                    expected_plan_spec_hash=expected_plan_spec_hash,
                    active_plan_approval_id=active_plan_approval_id,
                    frozen_requirements=completed_review,
                    legacy_verification_binding=legacy_verification_binding,
                )
                if binding_change:
                    status = "blocked"
                    error = binding_change
                    blocked_phase = "planning"
                    break

                if not self.config.codex.review_after_run and not completed_review:
                    break

                review_iteration_limit = max(
                    self.config.codex.max_review_iterations,
                    1 if completed_review else 0,
                )
                if generation_pass > review_iteration_limit:
                    status = "blocked"
                    error = (
                        "Implementation changes remain unreviewed because the maximum review "
                        "iterations were reached. Obtain an explicit review decision before completion."
                    )
                    blocked_phase = "review"
                    break

                requirements_change = await self._requirements_checkpoint_error(
                    issue,
                    requirements_snapshot_hash,
                    checkpoint="review",
                    workspace_path=workspace.path,
                    frozen=completed_review,
                    legacy_verification_binding=legacy_verification_binding,
                )
                if requirements_change:
                    status = "blocked"
                    error = requirements_change
                    blocked_phase = "planning"
                    break
                plan_change = validate_plan_artifact(
                    workspace.path,
                    self.config.codex.output_plan_file,
                    expected_hash=expected_plan_spec_hash,
                    issue=issue,
                    requirements_snapshot_hash=requirements_snapshot_hash,
                    legacy_frozen_plan=legacy_verification_binding is not None,
                )
                if plan_change:
                    if active_plan_approval_id:
                        self.store.invalidate_plan_approval(active_plan_approval_id, plan_change)
                    status = "blocked"
                    error = plan_change
                    blocked_phase = "planning"
                    break
                approval_change = validate_active_plan_approval_binding(
                    store=self.store,
                    approval_id=active_plan_approval_id,
                    issue=issue,
                    expected_plan_spec_hash=expected_plan_spec_hash,
                    requirements_snapshot_hash=requirements_snapshot_hash,
                )
                if approval_change:
                    status = "blocked"
                    error = approval_change
                    blocked_phase = "planning"
                    break
                if automation_plan is not None:
                    assert trusted_development_plan is not None
                    review_development_diff = capture_workspace_diff(
                        workspace.path,
                        trusted_development_plan,
                        managed_repositories=managed_workspace_repositories(
                            self.config
                        ),
                    )
                    automation_artifact_error = validate_bound_automation_state(
                        workspace_path=workspace.path,
                        config=self.config,
                        issue=issue,
                        requirements_snapshot_hash=requirements_snapshot_hash,
                        development_plan=trusted_development_plan,
                        development_plan_spec_hash=expected_plan_spec_hash or "",
                        development_diff_hash=review_development_diff.content_hash,
                        automation_plan=automation_plan,
                        expected_repository_diff_hash=(
                            automation_repository_diff_hash_for_run
                        ),
                        expected_result_hash=automation_result_hash_for_run,
                    )
                    if automation_artifact_error:
                        status = "blocked"
                        error = automation_artifact_error
                        blocked_phase = "automation_planning"
                        break

                review_workspace_before = None
                if trusted_development_plan is not None:
                    review_workspace_before = capture_workspace_diff(
                        workspace.path,
                        trusted_development_plan,
                        managed_repositories=managed_diff_repositories(self.config),
                    )

                review_prompt = build_review_prompt(
                    issue=issue,
                    workspace_path=workspace.path,
                    implementation_prompt=review_scope_prompt,
                    implementation_message=final_message,
                    review_instructions=self.config.codex.review_prompt,
                    plan_message=plan_message,
                    requirements_snapshot_hash=requirements_snapshot_hash,
                    plan_artifact_path=self.config.codex.output_plan_file if plan_message else None,
                    automation_plan_message=automation_plan_message,
                    automation_plan_artifact_path=(
                        str(self.config.automation.output_plan_file)
                        if automation_plan_message
                        else None
                    ),
                )
                review_config = read_only_codex_config(self.config.codex).model_copy(
                    update={"output_last_message_file": self.config.codex.output_review_file}
                )
                review_result, total_event_offset = await self._run_codex_pass(
                    prompt=review_prompt,
                    workspace_path=workspace.path,
                    config=review_config,
                    run_id=run.id,
                    event_offset=total_event_offset,
                    event_prefix="review",
                )
                review_message = review_result.final_message or review_result.error or ""
                if completed_review_action is not None:
                    review_heading = (
                        f"## Human review {completed_review_action['id']} "
                        f"automated pass {generation_pass}\n\n"
                        f"Reviewer: {completed_review_action['reviewer_identity']}\n\n"
                        f"Source: {completed_review_action['source_url']}\n\n"
                    )
                else:
                    review_heading = f"## Review pass {generation_pass}\n\n"
                review_history.append(
                    f"{review_heading}{review_message}".strip()
                )
                write_review_files(workspace.path, self.config.codex, review_message, review_history)

                if review_result.status != "completed":
                    status = review_result.status
                    error = self.redact(review_result.error or "Codex review pass failed")
                    blocked_phase = "review"
                    break

                if review_workspace_before is not None:
                    assert trusted_development_plan is not None
                    review_workspace_after = capture_workspace_diff(
                        workspace.path,
                        trusted_development_plan,
                        managed_repositories=managed_diff_repositories(self.config),
                    )
                    if (
                        review_workspace_after.content_hash
                        != review_workspace_before.content_hash
                        or review_workspace_after.content
                        != review_workspace_before.content
                    ):
                        status = "blocked"
                        error = (
                            "Read-only review changed the workspace; the run was "
                            "stopped before handoff."
                        )
                        blocked_phase = "review"
                        break

                binding_change = await self._execution_binding_error(
                    issue,
                    requirements_snapshot_hash,
                    checkpoint="completion after review pass",
                    workspace_path=workspace.path,
                    expected_plan_spec_hash=expected_plan_spec_hash,
                    active_plan_approval_id=active_plan_approval_id,
                    frozen_requirements=completed_review,
                    legacy_verification_binding=legacy_verification_binding,
                )
                if binding_change:
                    status = "blocked"
                    error = binding_change
                    blocked_phase = "planning"
                    break
                if automation_plan is not None:
                    assert trusted_development_plan is not None
                    post_review_development_diff = capture_workspace_diff(
                        workspace.path,
                        trusted_development_plan,
                        managed_repositories=managed_workspace_repositories(
                            self.config
                        ),
                    )
                    automation_artifact_error = validate_bound_automation_state(
                        workspace_path=workspace.path,
                        config=self.config,
                        issue=issue,
                        requirements_snapshot_hash=requirements_snapshot_hash,
                        development_plan=trusted_development_plan,
                        development_plan_spec_hash=expected_plan_spec_hash or "",
                        development_diff_hash=(
                            post_review_development_diff.content_hash
                        ),
                        automation_plan=automation_plan,
                        expected_repository_diff_hash=(
                            automation_repository_diff_hash_for_run
                        ),
                        expected_result_hash=automation_result_hash_for_run,
                    )
                    if automation_artifact_error:
                        status = "blocked"
                        error = automation_artifact_error
                        blocked_phase = "automation_planning"
                        break

                if verification_bypass_active:
                    assert verification_bypass is not None
                    assert verification_bypass_plan is not None
                    bypass_integrity_error = verification_bypass_integrity_error(
                        verification_bypass,
                        workspace_path=workspace.path,
                        plan_spec=verification_bypass_plan,
                        managed_repositories=managed_diff_repositories(
                            self.config
                        ),
                        checkpoint="completion after review",
                    )
                    if bypass_integrity_error:
                        status = "blocked"
                        error = bypass_integrity_error
                        blocked_phase = "verification"
                        break

                decision = classify_review_decision(review_message)
                if decision == "automation_plan_changes_required":
                    if automation_plan is None:
                        status = "blocked"
                        error = (
                            "Review requested automation replanning, but no automation "
                            "plan is active for this run."
                        )
                        blocked_phase = "review"
                        break
                    if verification_bypass_active:
                        assert verification_bypass is not None
                        verification_bypass_active = False
                        verification_bypass_review_consumed = True
                        self.store.add_log(
                            run.id,
                            "info",
                            "Review requested automation-plan changes, so the "
                            "verification override approved by "
                            f"{verification_bypass.approver_identity} was consumed. "
                            "The updated automation must pass normal verification.",
                            verification_bypass.verification_evidence_path,
                        )
                    generation_pass += 1
                    force_automation_refresh = True
                    automation_refresh_feedback = review_message
                    skip_development_for_automation_replan = True
                    continue
                if decision == "plan_changes_required":
                    reason = (
                        "Review requires changing the validated PlanSpec. The exact-plan approval "
                        "is no longer valid; return to planning and obtain a new approval."
                    )
                    if active_plan_approval_id:
                        self.store.invalidate_plan_approval(active_plan_approval_id, reason)
                    status = "blocked"
                    error = reason
                    blocked_phase = "planning"
                    break
                review_human_request = parse_human_request(review_message)
                if review_human_request:
                    status = "blocked"
                    error = review_human_request
                    blocked_phase = "review"
                    break

                if decision == "invalid":
                    status = "blocked"
                    error = (
                        "Review output did not contain a recognized explicit decision. Return "
                        "approve, changes_required, automation_plan_changes_required, "
                        "plan_changes_required, or needs_human."
                    )
                    blocked_phase = "review"
                    break
                if decision == "changes_required":
                    if verification_bypass_active:
                        assert verification_bypass is not None
                        verification_bypass_active = False
                        verification_bypass_review_consumed = True
                        self.store.add_log(
                            run.id,
                            "info",
                            "Review requested code changes, so the verification override "
                            f"approved by {verification_bypass.approver_identity} was consumed. "
                            "The next implementation pass must run normal verification.",
                            verification_bypass.verification_evidence_path,
                        )
                    if legacy_verification_binding is not None:
                        status = "blocked"
                        error = (
                            "Review requested code changes after a pre-v4 "
                            "verification-only resume. Return to planning against "
                            "the current v4 snapshot before further implementation."
                        )
                        blocked_phase = "review"
                        break
                    generation_pass += 1
                    if automation_plan is not None:
                        force_automation_refresh = True
                        automation_refresh_feedback = review_message
                    generation_prompt = build_regeneration_prompt(
                        issue=issue,
                        original_prompt=development_review_scope_prompt,
                        plan_message=plan_message,
                        plan_spec_hash=expected_plan_spec_hash,
                        review_message=review_message,
                        automation_plan_message=automation_plan_message,
                    )
                    continue

                if review_message:
                    final_message = append_review_to_final(final_message, review_message)
                break

        except PlanningSafetyGateBlocked as exc:
            status = "blocked"
            error = self.redact(str(exc))
            blocked_phase = "planning"
        except AutomationBindingConfigurationError as exc:
            status = "blocked"
            error = self.redact(str(exc))
            blocked_phase = "automation_planning"
        except asyncio.CancelledError:
            status = "cancelled"
            error = "Run cancelled by orchestrator"
            blocked_phase = "orchestration"
            raise
        except Exception as exc:
            status = "failed"
            error = self.redact(str(exc))
            blocked_phase = "setup"
        finally:
            if (
                status == "blocked"
                and blocked_phase == "planning"
                and workspace is not None
                and trusted_development_plan is not None
                and automation_plan is not None
            ):
                try:
                    if (
                        not automation_plan_hash_for_run
                        or automation_plan.content_hash()
                        != automation_plan_hash_for_run
                    ):
                        raise AutomationPlanError(
                            "Automation plan identity changed before development replanning"
                        )
                    if not automation_repository_diff_hash_for_run:
                        raise AutomationPlanError(
                            "Automation checkout has no exact repository-diff binding"
                        )
                    bound_repository_diff = capture_automation_repository_diff(
                        workspace.path,
                        trusted_development_plan,
                        self.config,
                    )
                    if (
                        bound_repository_diff.content_hash
                        != automation_repository_diff_hash_for_run
                    ):
                        raise AutomationPlanError(
                            "Automation checkout changed before development replanning"
                        )
                    automation_repository_state = inspect_automation_repository(
                        workspace.path,
                        self.config.automation.workspace_subdir.as_posix(),
                        expected_head_sha=automation_plan.repository_baseline_sha,
                        expected_branch_name=issue.identifier,
                        require_clean=False,
                    )
                    if automation_plan.decision == "update_required":
                        reconcile_retained_automation_changes(
                            workspace.path,
                            self.config.automation.workspace_subdir.as_posix(),
                            automation_plan,
                            expected_branch_name=issue.identifier,
                        )
                    elif automation_repository_state.dirty:
                        raise AutomationPlanError(
                            "No-op automation plan has unexpected checkout changes"
                        )
                    inspect_automation_repository(
                        workspace.path,
                        self.config.automation.workspace_subdir.as_posix(),
                        expected_head_sha=automation_plan.repository_baseline_sha,
                        expected_branch_name=issue.identifier,
                        require_clean=True,
                    )
                except (AutomationPlanError, HumanReviewContextError) as exc:
                    cleanup_error = self.redact(str(exc))
                    error = (
                        f"{error}. " if error else ""
                    ) + (
                        "Derived automation changes could not be invalidated safely "
                        f"before development replanning: {cleanup_error}"
                    )
                    blocked_phase = "automation_planning"
                else:
                    self.store.add_log(
                        run.id,
                        "info",
                        "Invalidated the derived automation plan and restored its "
                        "isolated checkout before development replanning.",
                    )
                    automation_plan = None
                    automation_plan_message = None
                    automation_result_message = None
                    automation_plan_hash_for_run = None
                    automation_development_diff_hash_for_run = None
                    automation_repository_diff_hash_for_run = None
                    automation_result_hash_for_run = None

            after_run_workspace_before = None
            if (
                status == "completed"
                and workspace is not None
                and trusted_development_plan is not None
            ):
                try:
                    after_run_workspace_before = capture_workspace_diff(
                        workspace.path,
                        trusted_development_plan,
                        managed_repositories=managed_diff_repositories(self.config),
                    )
                except HumanReviewContextError as exc:
                    status = "blocked"
                    error = self.redact(str(exc))
                    blocked_phase = "automation_planning"
            if workspace and self.config.hooks.after_run:
                after = await self._run_after_run_best_effort(issue, workspace)
                if after and not after.succeeded:
                    self.store.add_log(run.id, "warning", "after_run hook failed", str(after.log_path))

            if (
                status == "completed"
                and workspace is not None
                and trusted_development_plan is not None
                and after_run_workspace_before is not None
            ):
                try:
                    after_run_workspace_after = capture_workspace_diff(
                        workspace.path,
                        trusted_development_plan,
                        managed_repositories=managed_diff_repositories(self.config),
                    )
                    if (
                        after_run_workspace_after.content_hash
                        != after_run_workspace_before.content_hash
                        or after_run_workspace_after.content
                        != after_run_workspace_before.content
                    ):
                        raise HumanReviewContextError(
                            "after_run changed the verified workspace before handoff"
                        )
                    if automation_plan is not None:
                        development_diff = capture_workspace_diff(
                            workspace.path,
                            trusted_development_plan,
                            managed_repositories=managed_workspace_repositories(
                                self.config
                            ),
                        )
                        automation_error = validate_bound_automation_state(
                            workspace_path=workspace.path,
                            config=self.config,
                            issue=issue,
                            requirements_snapshot_hash=requirements_snapshot_hash,
                            development_plan=trusted_development_plan,
                            development_plan_spec_hash=expected_plan_spec_hash or "",
                            development_diff_hash=development_diff.content_hash,
                            automation_plan=automation_plan,
                            expected_repository_diff_hash=(
                                automation_repository_diff_hash_for_run
                            ),
                            expected_result_hash=automation_result_hash_for_run,
                        )
                        if automation_error:
                            raise HumanReviewContextError(automation_error)
                except HumanReviewContextError as exc:
                    status = "blocked"
                    error = self.redact(str(exc))
                    blocked_phase = "automation_planning"

            if status == "completed" and verification_bypass_active:
                assert verification_bypass is not None
                if workspace is None or verification_bypass_plan is None:
                    bypass_integrity_error = (
                        "Verification bypass binding was unavailable after after_run. "
                        "Run verification again or approve a new override."
                    )
                else:
                    bypass_integrity_error = verification_bypass_integrity_error(
                        verification_bypass,
                        workspace_path=workspace.path,
                        plan_spec=verification_bypass_plan,
                        managed_repositories=managed_diff_repositories(
                            self.config
                        ),
                        checkpoint="after after_run before Jira handoff",
                    )
                if bypass_integrity_error:
                    status = "blocked"
                    error = bypass_integrity_error
                    blocked_phase = "verification"

            if status == "completed" and verification_bypass is not None:
                final_message = append_verification_bypass_to_final(
                    final_message,
                    verification_bypass,
                    consumed_by_review=verification_bypass_review_consumed,
                    final_verification_status=verification_status,
                )

            updated = update_current_run(
                run.id,
                status=status,
                finished_at=utc_now(),
                final_message=final_message,
                error=error,
                blocked_phase=blocked_phase if status in {"blocked", "failed", "cancelled"} else None,
                verification_status=verification_status,
                verification_output_path=verification_output_path,
                verification_workspace_diff_hash=verification_workspace_diff_hash,
                verification_evidence_sha256=verification_evidence_sha256,
                automation_plan_hash=(
                    automation_plan_hash_for_run
                ),
                automation_development_diff_hash=(
                    automation_development_diff_hash_for_run
                ),
                automation_repository_diff_hash=(
                    automation_repository_diff_hash_for_run
                ),
                automation_result_hash=automation_result_hash_for_run,
            )

            if self.config.tracker.comment_on_finish and not completed_review:
                await self._post_finish_comment(issue, updated)

            handoff_succeeded = completed_review or not bool(
                self.config.tracker.handoff_status
            )
            if (
                status == "completed"
                and self.config.tracker.handoff_status
                and not completed_review
            ):
                handoff_succeeded = await self._transition_best_effort(
                    issue,
                    updated,
                )

            if (
                status == "completed"
                and self.config.runtime.enabled
                and self.config.runtime.shutdown_after_handoff
            ):
                if handoff_succeeded:
                    await self._shutdown_runtime_after_handoff_best_effort(
                        run_id=run.id,
                        workspace_path=(
                            workspace.path if workspace else workspace_path
                        ),
                        repositories=runtime_affected_repositories,
                    )
                else:
                    self.store.add_log(
                        run.id,
                        "warning",
                        "Runtime services were retained because the configured "
                        "Jira handoff did not succeed.",
                    )

        stored_run = self.store.get_run(run.id)
        assert stored_run is not None
        return OnceResult(issue=issue, prompt=prompt, run=stored_run, workspace=workspace, dry_run=False)

    async def _run_automation_stage(
        self,
        *,
        issue: Issue,
        run_id: str,
        workspace_path: Path,
        development_plan: PlanSpec,
        development_plan_message: str,
        development_plan_spec_hash: str,
        active_plan_approval_id: str | None,
        development_final_message: str | None,
        requirements_snapshot_hash: str,
        event_offset: int,
        human_input: dict[str, Any] | None,
        previous_phase: str | None,
        frozen_requirements: bool = False,
        expected_prior_plan_hash: str | None = None,
        expected_prior_development_diff_hash: str | None = None,
        expected_prior_repository_diff_hash: str | None = None,
    ) -> AutomationStageResult:
        """Plan and conditionally apply automation changes after development."""

        automation = self.config.automation
        repository = automation.workspace_subdir.as_posix()
        blocked_phase = (
            "automation_implementation"
            if previous_phase == "automation_implementation"
            else "automation_planning"
        )

        retained_plan_content: str | None = None
        retained_result_content: str | None = None
        retained_plan: AutomationPlan | None = None
        trusted_repository_diff_hash: str | None = None

        def restore_retained_artifacts() -> None:
            if retained_plan_content is not None:
                write_plan_spec_file(
                    workspace_path,
                    str(automation.output_plan_file),
                    retained_plan_content,
                )
            if retained_result_content is not None:
                write_plan_spec_file(
                    workspace_path,
                    str(automation.output_result_file),
                    retained_result_content,
                )

        def block_for_development_replanning(
            *,
            reason: str,
            bound_plan: AutomationPlan,
            bound_plan_message: str,
            bound_result_message: str | None,
            repository_diff_hash: str,
        ) -> AutomationStageResult:
            if active_plan_approval_id:
                self.store.invalidate_plan_approval(
                    active_plan_approval_id,
                    reason,
                )
            self.store.add_log(
                run_id,
                "warning",
                reason,
            )
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=reason,
                blocked_phase="planning",
                plan=bound_plan,
                plan_message=bound_plan_message,
                result_message=bound_result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=repository_diff_hash,
            )

        try:
            expected_resume_head: str | None = None
            retained_plan_resume = bool(
                previous_phase == "automation_implementation"
                or (
                    previous_phase == "automation_planning"
                    and expected_prior_plan_hash
                )
            )
            if retained_plan_resume:
                if not expected_prior_plan_hash:
                    raise AutomationPlanError(
                        "Automation implementation resume has no retained plan hash"
                    )
                retained_plan_content = read_frozen_text_artifact(
                    workspace_path,
                    automation.output_plan_file,
                    label="retained automation plan artifact",
                    required=True,
                )
                retained_result_content = read_frozen_text_artifact(
                    workspace_path,
                    automation.output_result_file,
                    label="retained automation result artifact",
                    required=True,
                )
                if not str(retained_result_content or "").strip():
                    raise AutomationPlanError(
                        "Retained automation result artifact is empty"
                    )
                try:
                    retained_plan = AutomationPlan.model_validate_json(
                        retained_plan_content or ""
                    )
                except ValueError as exc:
                    raise AutomationPlanError(
                        "Retained automation plan artifact is invalid"
                    ) from exc
                if retained_plan.content_hash() != expected_prior_plan_hash:
                    raise AutomationPlanError(
                        "Retained automation plan artifact does not match its run hash"
                    )
                if (
                    retained_plan.issue_key != issue.identifier
                    or retained_plan.requirements_snapshot_hash
                    != requirements_snapshot_hash
                    or retained_plan.development_plan_spec_hash
                    != development_plan_spec_hash
                    or retained_plan.automation_repository != repository
                ):
                    raise AutomationPlanError(
                        "Retained automation plan artifact does not match the current "
                        "issue, requirements, development plan, and repository"
                    )
                expected_resume_head = retained_plan.repository_baseline_sha
            repository_state = inspect_automation_repository(
                workspace_path,
                repository,
                expected_head_sha=expected_resume_head,
                expected_branch_name=issue.identifier,
                require_clean=not retained_plan_resume,
            )
            development_diff = capture_workspace_diff(
                workspace_path,
                development_plan,
                managed_repositories=managed_workspace_repositories(self.config),
            )
            automation_repository_diff = capture_automation_repository_diff(
                workspace_path,
                development_plan,
                self.config,
            )
            if expected_prior_repository_diff_hash:
                if (
                    automation_repository_diff.content_hash
                    != expected_prior_repository_diff_hash
                ):
                    raise AutomationPlanError(
                        "Automation checkout changed after the prior attempt; "
                        "reconcile the retained workspace before replanning."
                    )
            elif retained_plan_resume:
                raise AutomationPlanError(
                    "Retained automation plan has no exact repository-diff binding"
                )
            if expected_prior_development_diff_hash:
                if (
                    development_diff.content_hash
                    != expected_prior_development_diff_hash
                ):
                    if retained_plan is None or retained_plan_content is None:
                        raise AutomationPlanError(
                            "Development workspace changed after the blocked automation "
                            "attempt, but the retained automation plan is unavailable "
                            "for safe invalidation."
                        )
                    return block_for_development_replanning(
                        reason=(
                            "Development workspace changed after the blocked automation "
                            "attempt. The changed development state was not accepted as "
                            "automation output; the derived automation state must be "
                            "invalidated. Reconcile the development checkout to a clean "
                            "trusted baseline, then resume development replanning."
                        ),
                        bound_plan=retained_plan,
                        bound_plan_message=retained_plan_content,
                        bound_result_message=retained_result_content,
                        repository_diff_hash=automation_repository_diff.content_hash,
                    )
                if (
                    retained_plan_resume
                    and previous_phase == "automation_implementation"
                    and retained_plan.development_workspace_diff_hash
                    != expected_prior_development_diff_hash
                ):
                    raise AutomationPlanError(
                        "Retained AutomationPlan does not match the blocked run's "
                        "development-diff binding"
                    )
            if retained_plan_resume:
                retained_file_types = {
                    change.path: change.change_type
                    for change in retained_plan.affected_file_changes
                }
                invalid_retained_changes = tuple(
                    (path, change_type)
                    for path, change_type in repository_state.changed_file_types
                    if retained_file_types.get(path) != change_type
                )
                if invalid_retained_changes:
                    raise AutomationPlanError(
                        automation_file_scope_error(
                            tuple(sorted(retained_file_types.items())),
                            repository_state.changed_file_types,
                        )
                    )
                if (
                    retained_plan.decision == "no_update_required"
                    and repository_state.dirty
                ):
                    raise AutomationPlanError(
                        "Retained no-op AutomationPlan has automation checkout changes"
                    )
            trusted_repository_diff_hash = automation_repository_diff.content_hash
            planning_workspace_diff = capture_workspace_diff(
                workspace_path,
                development_plan,
                managed_repositories=managed_diff_repositories(self.config),
            )
            planning_mutation_guard = capture_automation_mutation_guard(
                workspace_path,
                repository,
            )
        except (AutomationPlanError, HumanReviewContextError) as exc:
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=self.redact(str(exc)),
                blocked_phase=blocked_phase,
                plan=None,
                plan_message=None,
                result_message=None,
                event_offset=event_offset,
            )

        plan_prompt = build_automation_planning_prompt(
            issue=issue,
            planning_instructions=automation.planning_prompt,
            requirements_snapshot_hash=requirements_snapshot_hash,
            development_plan_message=development_plan_message,
            development_plan_spec_hash=development_plan_spec_hash,
            development_diff=development_diff.content,
            development_diff_hash=development_diff.content_hash,
            development_final_message=development_final_message,
            automation_repository=repository,
            automation_repository_baseline_sha=repository_state.head_sha,
            retained_automation_plan_message=(
                retained_plan_content
                if retained_plan is not None and repository_state.dirty
                else None
            ),
            human_input=human_input,
        )
        plan_config = read_only_codex_config(self.config.codex).model_copy(
            update={
                "output_last_message_file": str(automation.output_plan_file),
            }
        )
        try:
            plan_result, event_offset = await self._run_codex_pass(
                prompt=plan_prompt,
                workspace_path=workspace_path,
                config=plan_config,
                run_id=run_id,
                event_offset=event_offset,
                event_prefix="automation_planning",
            )
        except BaseException:
            guard_error = restore_automation_mutation_guard(
                workspace_path,
                repository,
                planning_mutation_guard,
            )
            if guard_error:
                self.store.add_log(run_id, "error", guard_error)
            restore_retained_artifacts()
            raise
        plan_message = plan_result.final_message
        guard_error = restore_automation_mutation_guard(
            workspace_path,
            repository,
            planning_mutation_guard,
        )
        if guard_error:
            restore_retained_artifacts()
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=guard_error,
                blocked_phase="automation_planning",
                plan=None,
                plan_message=plan_message,
                result_message=None,
                event_offset=event_offset,
            )
        try:
            after_plan_state = inspect_automation_repository(
                workspace_path,
                repository,
                expected_head_sha=repository_state.head_sha,
                expected_branch_name=issue.identifier,
                require_clean=not retained_plan_resume,
            )
            after_plan_workspace_diff = capture_workspace_diff(
                workspace_path,
                development_plan,
                managed_repositories=managed_diff_repositories(self.config),
            )
        except (AutomationPlanError, HumanReviewContextError) as exc:
            restore_retained_artifacts()
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=self.redact(str(exc)),
                blocked_phase="automation_planning",
                plan=None,
                plan_message=plan_message,
                result_message=None,
                event_offset=event_offset,
            )
        if (
            after_plan_workspace_diff.content_hash
            != planning_workspace_diff.content_hash
            or after_plan_workspace_diff.content != planning_workspace_diff.content
        ):
            restore_retained_artifacts()
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=(
                    "Read-only automation planning changed the workspace; the "
                    "automation pass was stopped."
                ),
                blocked_phase="automation_planning",
                plan=None,
                plan_message=plan_message,
                result_message=None,
                event_offset=event_offset,
            )
        if plan_result.status != "completed":
            restore_retained_artifacts()
            return AutomationStageResult(
                status=plan_result.status,
                final_message=development_final_message,
                error=self.redact(
                    plan_result.error or "Codex automation planning pass failed"
                ),
                blocked_phase="automation_planning",
                plan=None,
                plan_message=plan_message,
                result_message=None,
                event_offset=event_offset,
            )
        plan_human_request = parse_human_request(
            plan_message or plan_result.error
        )
        if plan_human_request:
            restore_retained_artifacts()
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=plan_human_request,
                blocked_phase="automation_planning",
                plan=None,
                plan_message=plan_message,
                result_message=None,
                event_offset=event_offset,
            )

        try:
            automation_plan = parse_automation_plan(
                plan_message or "",
                expected_issue_key=issue.identifier,
                expected_requirements_snapshot_hash=requirements_snapshot_hash,
                expected_development_plan_spec_hash=development_plan_spec_hash,
                expected_development_diff_hash=development_diff.content_hash,
                expected_repository=repository,
                expected_repository_baseline_sha=repository_state.head_sha,
                development_plan_spec=development_plan,
            )
            plan_message = automation_plan.canonical_json(indent=2)
            write_plan_spec_file(
                workspace_path,
                str(automation.output_plan_file),
                plan_message,
            )
            pending_result_message = (
                "Automation plan validated as "
                f"{automation_plan.content_hash()}; implementation has not completed."
            )
            write_plan_spec_file(
                workspace_path,
                str(automation.output_result_file),
                pending_result_message,
            )
        except (AutomationPlanError, HumanReviewContextError) as exc:
            restore_retained_artifacts()
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=self.redact(str(exc)),
                blocked_phase="automation_planning",
                plan=None,
                plan_message=plan_message,
                result_message=None,
                event_offset=event_offset,
            )
        blocking_question = automation_plan.blocking_question()
        if blocking_question:
            if retained_plan is not None and after_plan_state.dirty:
                restore_retained_artifacts()
                return AutomationStageResult(
                    status="blocked",
                    final_message=development_final_message,
                    error=blocking_question,
                    blocked_phase="automation_planning",
                    plan=retained_plan,
                    plan_message=retained_plan_content,
                    result_message=retained_result_content,
                    event_offset=event_offset,
                )
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=blocking_question,
                blocked_phase="automation_planning",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=pending_result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        if (
            automation_plan.decision == "update_required"
            and after_plan_state.dirty
        ):
            replacement_file_types = {
                change.path: change.change_type
                for change in automation_plan.affected_file_changes
            }
            invalid_retained_scope = tuple(
                (path, change_type)
                for path, change_type in after_plan_state.changed_file_types
                if replacement_file_types.get(path) != change_type
            )
            if invalid_retained_scope:
                if retained_plan is None:
                    return AutomationStageResult(
                        status="blocked",
                        final_message=development_final_message,
                        error=(
                            "Replacement AutomationPlan does not preserve the exact "
                            "existing automation file scope."
                        ),
                        blocked_phase="automation_planning",
                        plan=None,
                        plan_message=None,
                        result_message=None,
                        event_offset=event_offset,
                    )
                retained_paths = frozenset(
                    path
                    for path, change_type in after_plan_state.changed_file_types
                    if replacement_file_types.get(path) == change_type
                )
                try:
                    reconcile_retained_automation_changes(
                        workspace_path,
                        repository,
                        retained_plan,
                        expected_branch_name=issue.identifier,
                        retain_paths=retained_paths,
                    )
                    after_plan_state = inspect_automation_repository(
                        workspace_path,
                        repository,
                        expected_head_sha=repository_state.head_sha,
                        expected_branch_name=issue.identifier,
                        require_clean=False,
                    )
                    trusted_repository_diff_hash = (
                        capture_automation_repository_diff(
                            workspace_path,
                            development_plan,
                            self.config,
                        ).content_hash
                    )
                except (AutomationPlanError, HumanReviewContextError) as exc:
                    try:
                        restore_retained_artifacts()
                    except HumanReviewContextError as restore_error:
                        exc = AutomationPlanError(
                            f"{exc}. Retained automation artifacts could not be "
                            f"restored safely: {restore_error}"
                        )
                    return AutomationStageResult(
                        status="blocked",
                        final_message=development_final_message,
                        error=self.redact(str(exc)),
                        blocked_phase="automation_planning",
                        plan=retained_plan,
                        plan_message=retained_plan_content,
                        result_message=retained_result_content,
                        event_offset=event_offset,
                    )
                self.store.add_log(
                    run_id,
                    "info",
                    "Reconciled obsolete retained automation paths before applying "
                    "the replacement AutomationPlan.",
                )
        if automation_plan.decision == "no_update_required":
            if after_plan_state.dirty:
                if (
                    retained_plan is None
                    or retained_plan.decision != "update_required"
                ):
                    return AutomationStageResult(
                        status="blocked",
                        final_message=development_final_message,
                        error=(
                            "Automation planning reported no update required, but the "
                            f"{repository!r} checkout contains unbound changes."
                        ),
                        blocked_phase="automation_planning",
                        plan=automation_plan,
                        plan_message=plan_message,
                        result_message=pending_result_message,
                        event_offset=event_offset,
                    )
                try:
                    reconcile_retained_automation_changes(
                        workspace_path,
                        repository,
                        retained_plan,
                        expected_branch_name=issue.identifier,
                    )
                except AutomationPlanError as exc:
                    try:
                        restore_retained_artifacts()
                    except HumanReviewContextError as restore_error:
                        exc = AutomationPlanError(
                            f"{exc}. Retained automation artifacts could not be "
                            f"restored safely: {restore_error}"
                        )
                    return AutomationStageResult(
                        status="blocked",
                        final_message=development_final_message,
                        error=self.redact(str(exc)),
                        blocked_phase="automation_planning",
                        plan=retained_plan,
                        plan_message=retained_plan_content,
                        result_message=retained_result_content,
                        event_offset=event_offset,
                    )
                self.store.add_log(
                    run_id,
                    "info",
                    "Removed the exact retained automation changes after the "
                    "replacement AutomationPlan determined no update is required.",
                )
            no_update_message = (
                "No automation update was required: " + automation_plan.rationale
            )
            write_plan_spec_file(
                workspace_path,
                str(automation.output_result_file),
                no_update_message,
            )
            return AutomationStageResult(
                status="completed",
                final_message=development_final_message,
                error=None,
                blocked_phase=None,
                plan=automation_plan,
                plan_message=plan_message,
                result_message=no_update_message,
                event_offset=event_offset,
            )

        path_safety_error = automation_plan_path_safety_error(
            workspace_path,
            repository,
            automation_plan,
        )
        if path_safety_error:
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=path_safety_error,
                blocked_phase="automation_planning",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=pending_result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )

        binding_change = await self._execution_binding_error(
            issue,
            requirements_snapshot_hash,
            checkpoint="automation implementation",
            workspace_path=workspace_path,
            expected_plan_spec_hash=development_plan_spec_hash,
            active_plan_approval_id=active_plan_approval_id,
            frozen_requirements=frozen_requirements,
        )
        if binding_change:
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=binding_change,
                blocked_phase="planning",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=pending_result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )

        implementation_prompt = build_automation_implementation_prompt(
            issue=issue,
            implementation_instructions=automation.implementation_prompt,
            development_plan_spec_hash=development_plan_spec_hash,
            development_diff_hash=development_diff.content_hash,
            automation_plan_message=plan_message,
            automation_plan_hash=automation_plan.content_hash(),
            automation_repository=repository,
        )
        implementation_config = self.config.codex.model_copy(
            update={
                "output_last_message_file": str(automation.output_result_file),
            }
        )
        try:
            implementation_mutation_guard = capture_automation_mutation_guard(
                workspace_path,
                repository,
            )
        except AutomationPlanError as exc:
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=self.redact(str(exc)),
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=pending_result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        try:
            implementation_result, event_offset = await self._run_codex_pass(
                prompt=implementation_prompt,
                workspace_path=workspace_path,
                config=implementation_config,
                run_id=run_id,
                event_offset=event_offset,
                event_prefix="automation_implementation",
            )
        except BaseException:
            guard_error = restore_automation_mutation_guard(
                workspace_path,
                repository,
                implementation_mutation_guard,
            )
            if guard_error:
                self.store.add_log(run_id, "error", guard_error)
            raise
        guard_error = restore_automation_mutation_guard(
            workspace_path,
            repository,
            implementation_mutation_guard,
        )
        result_message = str(implementation_result.final_message or "").strip() or None
        if guard_error:
            cleanup_error: str | None = None
            try:
                reconcile_retained_automation_changes(
                    workspace_path,
                    repository,
                    automation_plan,
                    expected_branch_name=issue.identifier,
                    allow_unplanned_changes=True,
                )
                trusted_repository_diff_hash = capture_automation_repository_diff(
                    workspace_path,
                    development_plan,
                    self.config,
                ).content_hash
                self.store.add_log(
                    run_id,
                    "warning",
                    "Restored the isolated automation checkout to its trusted "
                    "baseline after an automation pass changed ignored or local "
                    "Git state.",
                )
            except (AutomationPlanError, HumanReviewContextError) as exc:
                cleanup_error = self.redact(str(exc))
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=(
                    guard_error
                    + (
                        " The isolated checkout could not be restored fully: "
                        + cleanup_error
                        if cleanup_error
                        else " The isolated checkout was restored to its baseline."
                    )
                ),
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        if result_message is None and implementation_result.status != "completed":
            result_message = (
                "Automation implementation did not complete; a retry is required."
            )
        if str(result_message or "").strip():
            write_plan_spec_file(
                workspace_path,
                str(automation.output_result_file),
                str(result_message).strip(),
            )
        try:
            after_implementation_diff = capture_workspace_diff(
                workspace_path,
                development_plan,
                managed_repositories=managed_workspace_repositories(self.config),
            )
            final_automation_state = inspect_automation_repository(
                workspace_path,
                repository,
                expected_head_sha=repository_state.head_sha,
                expected_branch_name=issue.identifier,
                require_clean=False,
            )
        except (AutomationPlanError, HumanReviewContextError) as exc:
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=self.redact(str(exc)),
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        planned_file_type_map = {
            change.path: change.change_type
            for change in automation_plan.affected_file_changes
        }
        invalid_partial_changes = sorted(
            f"{path} ({change_type})"
            for path, change_type in final_automation_state.changed_file_types
            if planned_file_type_map.get(path) != change_type
        )
        if invalid_partial_changes:
            cleanup_error: str | None = None
            try:
                reconcile_retained_automation_changes(
                    workspace_path,
                    repository,
                    automation_plan,
                    expected_branch_name=issue.identifier,
                    allow_unplanned_changes=True,
                )
                trusted_repository_diff_hash = capture_automation_repository_diff(
                    workspace_path,
                    development_plan,
                    self.config,
                ).content_hash
                self.store.add_log(
                    run_id,
                    "warning",
                    "Restored the isolated automation checkout to its trusted "
                    "baseline after implementation changed unplanned files.",
                )
            except (AutomationPlanError, HumanReviewContextError) as exc:
                cleanup_error = self.redact(str(exc))
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=(
                    automation_file_scope_error(
                        tuple(sorted(planned_file_type_map.items())),
                        final_automation_state.changed_file_types,
                    )
                    + (
                        " The isolated checkout could not be restored safely: "
                        + cleanup_error
                        if cleanup_error
                        else " The isolated checkout was restored to its baseline; retry automation implementation."
                    )
                ),
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        try:
            trusted_repository_diff_hash = capture_automation_repository_diff(
                workspace_path,
                development_plan,
                self.config,
            ).content_hash
        except HumanReviewContextError as exc:
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=self.redact(str(exc)),
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        if after_implementation_diff.content_hash != development_diff.content_hash:
            return block_for_development_replanning(
                reason=(
                    "Automation implementation changed a development repository. "
                    "Those edits were not accepted as automation output; the derived "
                    "automation state must be invalidated. Reconcile the development "
                    "checkout to a clean trusted baseline, then resume development "
                    "replanning."
                ),
                bound_plan=automation_plan,
                bound_plan_message=plan_message,
                bound_result_message=result_message,
                repository_diff_hash=trusted_repository_diff_hash,
            )
        if implementation_result.status != "completed":
            return AutomationStageResult(
                status=implementation_result.status,
                final_message=development_final_message,
                error=self.redact(
                    implementation_result.error
                    or "Codex automation implementation pass failed"
                ),
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        implementation_human_request = parse_human_request(
            result_message or implementation_result.error
        )
        if implementation_human_request:
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=implementation_human_request,
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        result_message = str(result_message or "").strip() or None
        if result_message is None:
            incomplete_result = (
                "Automation implementation returned successfully without a completion "
                "report; Symphony treated the attempt as incomplete."
            )
            write_plan_spec_file(
                workspace_path,
                str(automation.output_result_file),
                incomplete_result,
            )
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=(
                    "Automation implementation completed without a non-empty "
                    "completion result."
                ),
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=None,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )

        if not final_automation_state.dirty:
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=(
                    "Automation plan required an update, but the automation checkout "
                    "has no resulting changes."
                ),
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        planned_file_types = tuple(
            sorted(
                (change.path, change.change_type)
                for change in automation_plan.affected_file_changes
            )
        )
        if final_automation_state.changed_file_types != planned_file_types:
            planned_paths = {path for path, _ in planned_file_types}
            actual_paths = {
                path for path, _ in final_automation_state.changed_file_types
            }
            unexpected = sorted(
                actual_paths.difference(planned_paths)
            )
            missing = sorted(
                planned_paths.difference(actual_paths)
            )
            wrong_types = sorted(
                f"{path} (planned {planned}, actual {actual})"
                for path, planned in planned_file_types
                for actual_path, actual in final_automation_state.changed_file_types
                if path == actual_path and planned != actual
            )
            details: list[str] = []
            if unexpected:
                details.append("unplanned: " + ", ".join(unexpected))
            if missing:
                details.append("planned but unchanged: " + ", ".join(missing))
            if wrong_types:
                details.append("wrong change type: " + ", ".join(wrong_types))
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=(
                    "Automation implementation does not match the exact planned "
                    "file scope (" + "; ".join(details) + ")."
                ),
                blocked_phase="automation_implementation",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )
        artifact_error = validate_automation_plan_artifact(
            workspace_path,
            str(automation.output_plan_file),
            expected_hash=automation_plan.content_hash(),
            issue=issue,
            requirements_snapshot_hash=requirements_snapshot_hash,
            development_plan=development_plan,
            development_plan_spec_hash=development_plan_spec_hash,
            development_diff_hash=development_diff.content_hash,
            repository=repository,
            repository_baseline_sha=repository_state.head_sha,
        )
        if artifact_error:
            try:
                write_plan_spec_file(
                    workspace_path,
                    str(automation.output_plan_file),
                    plan_message,
                )
            except HumanReviewContextError as restore_error:
                artifact_error += (
                    " The trusted artifact could not be restored safely: "
                    f"{restore_error}"
                )
            return AutomationStageResult(
                status="blocked",
                final_message=development_final_message,
                error=artifact_error,
                blocked_phase="automation_planning",
                plan=automation_plan,
                plan_message=plan_message,
                result_message=result_message,
                event_offset=event_offset,
                trusted_repository_diff_hash=trusted_repository_diff_hash,
            )

        binding_change = await self._execution_binding_error(
            issue,
            requirements_snapshot_hash,
            checkpoint="completion after automation implementation",
            workspace_path=workspace_path,
            expected_plan_spec_hash=development_plan_spec_hash,
            active_plan_approval_id=active_plan_approval_id,
            frozen_requirements=frozen_requirements,
        )
        return AutomationStageResult(
            status="blocked" if binding_change else "completed",
            final_message=development_final_message,
            error=binding_change,
            blocked_phase="planning" if binding_change else None,
            plan=automation_plan,
            plan_message=plan_message,
            result_message=result_message,
            event_offset=event_offset,
            trusted_repository_diff_hash=trusted_repository_diff_hash,
        )

    async def _run_runtime_verification(
        self,
        *,
        run_id: str,
        issue: Issue,
        workspace_path: Path,
        expected_plan_spec_hash: str | None,
        requirements_snapshot_hash: str,
        hook_status: str,
        hook_output_path: str | None,
        legacy_frozen_plan: bool = False,
    ) -> tuple[str, str | None, str | None, tuple[str, ...]]:
        """Verify only repositories named by the exact trusted PlanSpec."""

        manifest_path = (
            workspace_path
            / ".symphony"
            / "runtime"
            / f"{run_id}-verification.json"
        )
        affected_repositories: tuple[str, ...] = ()
        results: tuple[RuntimeVerificationResult, ...] = ()
        runtime_status = "environment_blocked"
        runtime_error: str | None = None

        try:
            if self.runtime_manager is None:
                raise OrchestratorError(
                    "Runtime verification is enabled but no runtime manager is available"
                )
            if not expected_plan_spec_hash:
                raise PlanSpecError(
                    "Runtime verification requires an exact, trusted PlanSpec binding"
                )
            plan_text = read_frozen_text_artifact(
                workspace_path,
                self.config.codex.output_plan_file,
                label="runtime verification PlanSpec",
                required=True,
            )
            if plan_text is None:
                raise PlanSpecError(
                    "Runtime verification PlanSpec is missing"
                )
            if legacy_frozen_plan:
                if issue.requirements_snapshot is None:
                    raise PlanSpecError(
                        "Frozen legacy requirements snapshot is missing"
                    )
                plan_spec = parse_frozen_legacy_plan_spec(
                    plan_text,
                    expected_issue_key=issue.identifier,
                    expected_snapshot_hash=requirements_snapshot_hash,
                    issue_type=issue.issue_type,
                    requirements_snapshot=issue.requirements_snapshot,
                )
            else:
                plan_spec = parse_plan_spec(
                    plan_text,
                    expected_issue_key=issue.identifier,
                    expected_snapshot_hash=requirements_snapshot_hash,
                    issue_type=issue.issue_type,
                    requirements_snapshot=issue.requirements_snapshot,
                )
            if plan_spec.content_hash() != expected_plan_spec_hash:
                raise PlanSpecError(
                    "Runtime verification PlanSpec does not match the trusted plan hash"
                )
            affected_repositories = tuple(
                dict.fromkeys(plan_spec.affected_surface.repositories)
            )
            repository_bindings = runtime_repository_bindings(
                self.config,
                affected_repositories,
            )
            configured_repositories = tuple(
                runtime_key for _, runtime_key in repository_bindings
            )
            workspace_subdirs_by_runtime_key = {
                runtime_key: workspace_subdir
                for workspace_subdir, runtime_key in repository_bindings
            }
            results = await self.runtime_manager.verify_many(
                workspace_path,
                configured_repositories,
                source_repositories=configured_repositories,
            )
            runtime_status = aggregate_runtime_verification_status(results)
            if runtime_status != "passed":
                failures = [
                    runtime_verification_failure_summary(result)
                    for result in results
                    if result.status != "passed"
                ]
                runtime_error = (
                    "Runtime verification did not pass: "
                    + ("; ".join(failures) if failures else "no checks ran")
                )
                runtime_error = self.redact(runtime_error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime_status = "environment_blocked"
            runtime_error = self.redact(str(exc))

        hook_output_sha256: str | None = None
        try:
            if hook_output_path is not None:
                hook_output_sha256 = hash_runtime_verification_log(
                    workspace_path,
                    hook_output_path,
                )
            runtime_checks = [
                runtime_verification_manifest_entry(
                    result,
                    self.redact,
                    workspace_path=workspace_path,
                    workspace_subdir=workspace_subdirs_by_runtime_key[
                        result.repository
                    ],
                )
                for result in results
            ]
        except HumanReviewContextError as exc:
            runtime_status = "environment_blocked"
            log_binding_error = self.redact(
                f"Could not bind verification log evidence: {exc}"
            )
            runtime_error = (
                f"{runtime_error}; {log_binding_error}"
                if runtime_error
                else log_binding_error
            )
            runtime_checks = [
                runtime_verification_manifest_entry(
                    result,
                    self.redact,
                    workspace_path=None,
                    workspace_subdir=workspace_subdirs_by_runtime_key[
                        result.repository
                    ],
                )
                for result in results
            ]

        manifest = {
            "schema_version": "1.0",
            "generated_at": utc_now().isoformat(),
            "issue_identifier": issue.identifier,
            "plan_spec_hash": expected_plan_spec_hash,
            "affected_repositories": list(affected_repositories),
            "hook": {
                "status": hook_status,
                "required": self.config.hooks.verify_required,
                "output_path": hook_output_path,
                "output_sha256": hook_output_sha256,
            },
            "runtime": {
                "status": runtime_status,
                "required": self.config.runtime.required,
                "checks": runtime_checks,
            },
            "error": runtime_error,
        }
        try:
            await asyncio.to_thread(
                write_runtime_verification_manifest,
                manifest_path,
                manifest,
            )
        except OSError as exc:
            runtime_status = "environment_blocked"
            persistence_error = self.redact(
                f"Could not persist runtime verification manifest: {exc}"
            )
            runtime_error = (
                f"{runtime_error}; {persistence_error}"
                if runtime_error
                else persistence_error
            )
            return (
                runtime_status,
                None,
                runtime_error,
                affected_repositories,
            )

        if runtime_error:
            runtime_error = (
                f"{runtime_error}. Verification manifest: {manifest_path}"
            )
        return (
            runtime_status,
            str(manifest_path),
            runtime_error,
            affected_repositories,
        )

    async def _run_codex_pass(
        self,
        *,
        prompt: str,
        workspace_path: Path,
        config: CodexConfig,
        run_id: str,
        event_offset: int,
        event_prefix: str | None = None,
    ) -> tuple[CodexRunResult, int]:
        def add_event(seq: int, event_type: str, raw: dict[str, Any], offset: int = event_offset) -> None:
            stored_event_type = f"{event_prefix}.{event_type}" if event_prefix else event_type
            self.store.add_codex_event(run_id, offset + seq, stored_event_type, raw)

        def add_log(level: str, message: str) -> None:
            self.store.add_log(run_id, level, self.redact(message) or "")

        result = await self.codex_runner.run(
            prompt,
            workspace_path,
            config,
            timeout_seconds=self.config.agent.timeout_seconds,
            event_callback=add_event,
            log_callback=add_log,
        )
        return result, event_offset + max(len(result.events), 1)

    async def _run_after_run_best_effort(self, issue: Issue, workspace: WorkspaceInfo) -> HookResult | None:
        try:
            return await self.workspace_manager.run_hook(
                "after_run",
                self.config.hooks.after_run or "",
                workspace.path,
                hook_context=self.hook_context(issue, workspace),
            )
        except Exception as exc:
            self.store.add_log(None, "warning", f"after_run hook failed to execute: {exc}")
            return None

    async def _requirements_checkpoint_error(
        self,
        baseline_issue: Issue,
        expected_hash: str,
        *,
        checkpoint: str,
        workspace_path: Path,
        frozen: bool,
        legacy_verification_binding: LegacyVerificationResumeBinding | None = None,
    ) -> str | None:
        if legacy_verification_binding is not None:
            return await self._legacy_verification_requirements_change_error(
                baseline_issue,
                expected_hash,
                checkpoint=checkpoint,
                workspace_path=workspace_path,
                binding=legacy_verification_binding,
            )
        if not frozen:
            return await self._requirements_change_error(
                baseline_issue,
                expected_hash,
                checkpoint=checkpoint,
                workspace_path=workspace_path,
            )
        try:
            stored_snapshot = self.store.get_requirements_snapshot(
                baseline_issue.identifier,
                expected_hash,
            )
        except StoreIntegrityError as exc:
            return (
                f"Frozen requirements snapshot failed integrity validation "
                f"before {checkpoint}: {exc}"
            )
        if stored_snapshot is None:
            return (
                f"Frozen requirements snapshot {expected_hash} is missing "
                f"before {checkpoint}."
            )
        if (
            baseline_issue.requirements_snapshot is None
            or baseline_issue.requirements_snapshot.calculate_content_hash()
            != expected_hash
        ):
            return (
                f"In-memory frozen requirements snapshot changed before "
                f"{checkpoint}."
            )
        return validate_frozen_snapshot_artifacts(
            workspace_path,
            expected_hash,
        )

    async def _legacy_verification_requirements_change_error(
        self,
        baseline_issue: Issue,
        expected_hash: str,
        *,
        checkpoint: str,
        workspace_path: Path,
        binding: LegacyVerificationResumeBinding,
    ) -> str | None:
        """Recheck live v4 authority while retaining the exact frozen hash."""

        if expected_hash != binding.requirements_snapshot_hash:
            return (
                "Frozen legacy verification binding changed before "
                f"{checkpoint}. Return to planning."
            )
        try:
            stored_snapshot = self.store.get_requirements_snapshot(
                baseline_issue.identifier,
                expected_hash,
            )
        except StoreIntegrityError as exc:
            return (
                "Frozen requirements snapshot failed integrity validation "
                f"before {checkpoint}: {exc}"
            )
        if (
            stored_snapshot is None
            or stored_snapshot.canonical_content()
            != binding.snapshot.canonical_content()
        ):
            return (
                f"Frozen requirements snapshot {expected_hash} is missing or "
                f"changed before {checkpoint}."
            )
        artifact_error = validate_frozen_snapshot_artifacts(
            workspace_path,
            expected_hash,
        )
        if artifact_error:
            return artifact_error

        current_issue = await self.jira.get_issue(
            baseline_issue.identifier,
            include_comments=True,
        )
        current_snapshot = current_issue.requirements_snapshot
        if (
            current_snapshot is None
            or current_snapshot.schema_version != "jira-requirements/v4"
        ):
            return (
                "Current Jira requirements are not available as a complete v4 "
                f"snapshot before {checkpoint}; verification cannot reuse the "
                "frozen pre-v4 approval."
            )
        safety_error = requirements_planning_safety_error(current_issue)
        if safety_error:
            return safety_error
        if requirements_planning_authority_equivalent(
            binding.snapshot,
            current_snapshot,
        ):
            return None

        before = requirements_planning_authority_projection(binding.snapshot)
        after = requirements_planning_authority_projection(current_snapshot)
        changed_sections = sorted(
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        )
        detail = (
            f" Changed authoritative sections: {', '.join(changed_sections)}."
            if changed_sections
            else ""
        )
        reason = (
            f"Jira root planning evidence changed before {checkpoint}; the "
            f"frozen pre-v4 PlanSpec and approval are invalid.{detail} Replan "
            f"against snapshot {current_snapshot.calculate_content_hash()}."
        )
        write_requirements_snapshot_artifacts(
            workspace_path,
            current_snapshot,
        )
        self.store.save_requirements_snapshot(current_snapshot)
        self.store.invalidate_active_plan_approvals_for_issue(
            baseline_issue.identifier,
            reason,
        )
        return reason

    async def _requirements_change_error(
        self,
        baseline_issue: Issue,
        expected_hash: str,
        *,
        checkpoint: str,
        workspace_path: Path,
    ) -> str | None:
        current_issue = await self.jira.get_issue(baseline_issue.identifier, include_comments=True)
        current_hash = issue_description_fingerprint(current_issue)
        if current_hash == expected_hash:
            return None
        changed_sections: list[str] = []
        if baseline_issue.requirements_snapshot and current_issue.requirements_snapshot:
            changed_sections = diff_requirements_snapshots(
                baseline_issue.requirements_snapshot,
                current_issue.requirements_snapshot,
            ).changed_sections
        detail = f" Changed sections: {', '.join(changed_sections)}." if changed_sections else ""
        reason = (
            f"Jira requirements changed before {checkpoint}; the prior PlanSpec and approval are invalid."
            f"{detail} Replan against snapshot {current_hash}."
        )
        if current_issue.requirements_snapshot is not None:
            write_requirements_snapshot_artifacts(
                workspace_path,
                current_issue.requirements_snapshot,
            )
            self.store.save_requirements_snapshot(current_issue.requirements_snapshot)
        self.store.invalidate_active_plan_approvals_for_issue(
            baseline_issue.identifier,
            reason,
        )
        return reason

    async def _execution_binding_error(
        self,
        issue: Issue,
        requirements_snapshot_hash: str,
        *,
        checkpoint: str,
        workspace_path: Path,
        expected_plan_spec_hash: str | None,
        active_plan_approval_id: str | None,
        frozen_requirements: bool = False,
        legacy_verification_binding: LegacyVerificationResumeBinding | None = None,
    ) -> str | None:
        requirements_change = await self._requirements_checkpoint_error(
            issue,
            requirements_snapshot_hash,
            checkpoint=checkpoint,
            workspace_path=workspace_path,
            frozen=frozen_requirements,
            legacy_verification_binding=legacy_verification_binding,
        )
        if requirements_change:
            return requirements_change
        plan_change = validate_plan_artifact(
            workspace_path,
            self.config.codex.output_plan_file,
            expected_hash=expected_plan_spec_hash,
            issue=issue,
            requirements_snapshot_hash=requirements_snapshot_hash,
            legacy_frozen_plan=legacy_verification_binding is not None,
        )
        if plan_change:
            if active_plan_approval_id:
                self.store.invalidate_plan_approval(active_plan_approval_id, plan_change)
            return plan_change
        return validate_active_plan_approval_binding(
            store=self.store,
            approval_id=active_plan_approval_id,
            issue=issue,
            expected_plan_spec_hash=expected_plan_spec_hash,
            requirements_snapshot_hash=requirements_snapshot_hash,
        )

    def _persist_requirements_snapshot(self, issue: Issue, workspace_path: Path) -> None:
        if issue.requirements_snapshot is None:
            return
        write_requirements_snapshot_artifacts(workspace_path, issue.requirements_snapshot)
        self.store.save_requirements_snapshot(issue.requirements_snapshot)

    def redact(self, text: str | None) -> str | None:
        return redact_text(text, self.secret_values)

    def hook_context(self, issue: Issue, workspace: WorkspaceInfo | None = None) -> dict[str, Any]:
        context: dict[str, Any] = {
            "issue": issue,
            "config": self.config,
            "workflow": self.workflow,
        }
        if workspace:
            context.update(
                {
                    "workspace": workspace,
                    "workspace_path": str(workspace.path),
                    "branch_name": workspace.branch_name,
                }
            )
        return context

    async def _post_start_comment(
        self,
        issue: Issue,
        run_id: str,
        workspace_path: Path,
        branch_name: str | None,
    ) -> None:
        body = start_comment(issue, workspace_path, branch_name)
        try:
            await self.jira.add_comment(issue.identifier, body)
            self.store.add_jira_action(issue.identifier, run_id=run_id, action="comment_start", body=body, status="completed")
        except Exception as exc:
            self.store.add_jira_action(
                issue.identifier,
                run_id=run_id,
                action="comment_start",
                body=body,
                status="failed",
                error=self.redact(str(exc)),
            )
            raise

    async def _shutdown_runtime_after_handoff_best_effort(
        self,
        *,
        run_id: str,
        workspace_path: Path,
        repositories: tuple[str, ...],
    ) -> None:
        if self.runtime_manager is None:
            self.store.add_log(
                run_id,
                "warning",
                "Runtime shutdown was configured after handoff, but no runtime "
                "manager is available.",
            )
            return
        try:
            configured_repositories = runtime_repository_keys(
                self.config,
                repositories,
            )
            result = await self.runtime_manager.shutdown(
                workspace_path,
                configured_repositories,
                source_repositories=configured_repositories,
            )
        except asyncio.CancelledError:
            self.store.add_log(
                run_id,
                "warning",
                "Runtime shutdown was cancelled after the run had already been "
                "persisted and handed off; the completed run was retained.",
            )
            return
        except Exception as exc:
            self.store.add_log(
                run_id,
                "warning",
                self.redact(
                    "Runtime shutdown failed after the run had already been "
                    f"persisted and handed off: {exc}"
                )
                or "Runtime shutdown failed after handoff.",
            )
            return

        log_path = str(result.log_path) if result.log_path is not None else None
        repository_list = ", ".join(result.repositories) or "none"
        service_list = ", ".join(result.services) or "none"
        if result.status == "stopped":
            self.store.add_log(
                run_id,
                "info",
                "Runtime shutdown completed after handoff for repositories "
                f"[{repository_list}] and services [{service_list}].",
                log_path,
            )
            return
        self.store.add_log(
            run_id,
            "warning",
            self.redact(
                "Runtime shutdown was blocked after the completed run was "
                f"handed off: {result.message}"
            )
            or "Runtime shutdown was blocked after handoff.",
            log_path,
        )

    async def _post_finish_comment(self, issue: Issue, run: RunRecord) -> None:
        body = finish_comment(issue, run)
        try:
            await self.jira.add_comment(issue.identifier, body)
            self.store.add_jira_action(issue.identifier, run_id=run.id, action="comment_finish", body=body, status="completed")
        except Exception as exc:
            self.store.add_jira_action(
                issue.identifier,
                run_id=run.id,
                action="comment_finish",
                body=body,
                status="failed",
                error=self.redact(str(exc)),
            )
            self.store.add_log(run.id, "error", self.redact(f"Failed to post Jira finish comment: {exc}") or "")

    async def _transition_best_effort(
        self,
        issue: Issue,
        run: RunRecord,
    ) -> bool:
        target = self.config.tracker.handoff_status
        if not target:
            return True
        try:
            transitioned = await self.jira.transition_issue(issue.identifier, target)
            status = "completed" if transitioned else "skipped"
            error = None if transitioned else f"No Jira transition found for target status {target}"
            self.store.add_jira_action(
                issue.identifier,
                run_id=run.id,
                action="transition",
                body=target,
                status=status,
                error=error,
            )
            return transitioned
        except Exception as exc:
            self.store.add_jira_action(
                issue.identifier,
                run_id=run.id,
                action="transition",
                body=target,
                status="failed",
                error=self.redact(str(exc)),
            )
            self.store.add_log(run.id, "warning", self.redact(f"Jira transition failed: {exc}") or "")
            return False


def aggregate_runtime_verification_status(
    results: tuple[RuntimeVerificationResult, ...],
) -> str:
    if not results:
        return "environment_blocked"
    statuses = {result.status for result in results}
    if "environment_blocked" in statuses:
        return "environment_blocked"
    if "test_failed" in statuses:
        return "test_failed"
    if statuses == {"passed"}:
        return "passed"
    return "environment_blocked"


def aggregate_verification_status(hook_status: str, runtime_status: str) -> str:
    if runtime_status == "environment_blocked":
        return "environment_blocked"
    if runtime_status == "test_failed":
        return "test_failed"
    if hook_status == "failed":
        return "failed"
    return "passed"


def runtime_verification_failure_summary(
    result: RuntimeVerificationResult,
) -> str:
    summary = f"{result.repository} ({result.status}): {result.message}"
    if result.log_path is not None:
        summary += f" [log: {result.log_path}]"
    return summary


def runtime_verification_manifest_entry(
    result: RuntimeVerificationResult,
    redact,
    *,
    workspace_path: Path | None,
    workspace_subdir: str,
) -> dict[str, Any]:
    log_sha256 = (
        hash_runtime_verification_log(workspace_path, result.log_path)
        if workspace_path is not None and result.log_path is not None
        else None
    )
    return {
        "repository": result.repository,
        "workspace_subdir": workspace_subdir,
        "profile": result.profile,
        "status": result.status,
        "argv": list(result.argv),
        "repository_path": (
            str(result.repository_path)
            if result.repository_path is not None
            else None
        ),
        "started_at": result.started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "returncode": result.returncode,
        "log_path": str(result.log_path) if result.log_path is not None else None,
        "log_sha256": log_sha256,
        "message": redact(result.message),
    }


def write_runtime_verification_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    write_runtime_artifact_bytes(
        manifest_path,
        (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8"),
    )


class PollingOrchestrator:
    def __init__(
        self,
        workflow: WorkflowDefinition,
        jira: JiraLike,
        store: Store,
        *,
        workspace_manager: WorkspaceManager | None = None,
        codex_runner: CodexRunner | None = None,
        runtime_manager: RuntimeManager | None = None,
        secret_values: list[str | None] | None = None,
        search_limit: int = 50,
    ) -> None:
        self.workflow = workflow
        self.config = workflow.config
        self.jira = jira
        self.store = store
        self.workspace_manager = workspace_manager or WorkspaceManager(self.config.workspace, self.config.hooks)
        self.codex_runner = codex_runner
        self.runtime_manager = runtime_manager
        self.secret_values = secret_values or []
        self.search_limit = search_limit
        self.claimed: set[str] = set()
        self.running: dict[str, RunningIssue] = {}
        self.retry_queue: dict[str, RetryEntry] = {}
        self.completed: dict[str, str] = {}
        self.blocked: set[str] = set()
        self.last_poll_error: str | None = None
        self.last_poll_at: datetime | None = None
        self._stop = asyncio.Event()
        self._poll_lock = asyncio.Lock()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.polling.interval_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def poll_once(self) -> None:
        async with self._poll_lock:
            await self._poll_once()

    async def _poll_once(self) -> None:
        self.last_poll_at = utc_now()
        await self.reap_finished()
        await self.dispatch_human_reviews()
        try:
            issues = await self.jira.search_issues(self.config.tracker.jql, limit=self.search_limit)
            self.last_poll_error = None
        except Exception as exc:
            self.last_poll_error = redact_text(str(exc), self.secret_values)
            self.store.add_log(None, "error", redact_text(f"Jira search failed: {exc}", self.secret_values) or "")
            return

        await self.reconcile_running()
        await self.dispatch_human_resumes()
        await self.dispatch_due_retries()
        self.dispatch_fresh_issues(issues)

    async def reconcile_running(self) -> None:
        for issue_id, running in list(self.running.items()):
            if running.task.done():
                continue
            if running.completed_review:
                continue
            try:
                current = await self.jira.get_issue(running.identifier, include_comments=False)
            except Exception as exc:
                self.store.add_log(
                    None,
                    "warning",
                    redact_text(f"Could not reconcile {running.identifier}: {exc}", self.secret_values) or "",
                )
                continue

            if current.status in self.config.tracker.terminal_statuses:
                running.task.cancel()
                self.store.add_log(None, "info", f"Cancelled {running.identifier}: terminal status {current.status}")
            elif current.status not in self.config.tracker.active_statuses:
                running.task.cancel()
                self.store.add_log(None, "info", f"Cancelled {running.identifier}: inactive status {current.status}")

    async def reap_finished(self) -> None:
        for issue_id, running in list(self.running.items()):
            if not running.task.done():
                continue
            self.running.pop(issue_id, None)
            try:
                result = running.task.result()
            except asyncio.CancelledError:
                self.claimed.discard(issue_id)
                continue
            except Exception as exc:
                if running.human_resume or running.completed_review:
                    self.claimed.discard(issue_id)
                    self.retry_queue.pop(issue_id, None)
                    self.store.add_log(
                        None,
                        "error",
                        redact_text(
                            f"Durable human action failed for {running.identifier}: {exc}",
                            self.secret_values,
                        )
                        or "",
                    )
                else:
                    await self._schedule_retry_or_release(running, str(exc))
                continue

            run = result.run
            status = run.status if run else "failed"
            if status == "completed":
                self.completed[issue_id] = issue_description_fingerprint(result.issue)
                self.claimed.discard(issue_id)
                self.retry_queue.pop(issue_id, None)
            elif status == "blocked":
                self.blocked.add(issue_id)
                self.claimed.discard(issue_id)
                self.retry_queue.pop(issue_id, None)
            elif status == "cancelled":
                self.claimed.discard(issue_id)
                self.retry_queue.pop(issue_id, None)
            elif (
                run
                and not running.human_resume
                and not running.completed_review
                and is_retryable_error(run.error)
            ):
                await self._schedule_retry_or_release(running, run.error)
            else:
                self.claimed.discard(issue_id)
                self.retry_queue.pop(issue_id, None)

    async def dispatch_due_retries(self) -> None:
        now = time.monotonic()
        due_entries = [entry for entry in self.retry_queue.values() if entry.due_at <= now]
        for entry in due_entries:
            if self.available_slots() <= 0:
                return
            self.retry_queue.pop(entry.issue_id, None)
            try:
                issue = await self.jira.get_issue(entry.identifier, include_comments=False)
                assert_issue_eligible(issue, self.config)
            except Exception as exc:
                self.claimed.discard(entry.issue_id)
                self.store.add_log(
                    None,
                    "warning",
                    redact_text(f"Retry skipped for {entry.identifier}: {exc}", self.secret_values) or "",
                )
                continue
            self._start_issue(issue, entry.attempt)

    async def dispatch_human_reviews(self) -> None:
        for action_id in self.store.list_recoverable_human_review_action_ids():
            if self.available_slots() <= 0:
                return
            action = self.store.claim_human_review_action(action_id)
            if action is None:
                continue
            claim_token = str(action["claim_token"])
            source_run = self.store.get_run(str(action["source_run_id"]))
            result_run = self.store.get_run(str(action["result_run_id"]))
            invalid_action = (
                source_run is None
                or result_run is None
                or (
                    source_run is not None
                    and result_run is not None
                    and (
                        source_run.status != "completed"
                        or source_run.issue_id != result_run.issue_id
                        or source_run.issue_identifier
                        != result_run.issue_identifier
                        or source_run.workspace_path != result_run.workspace_path
                        or source_run.issue_fingerprint
                        != result_run.issue_fingerprint
                        or source_run.plan_spec_hash
                        != result_run.plan_spec_hash
                        or source_run.automation_plan_hash
                        != result_run.automation_plan_hash
                        or source_run.automation_development_diff_hash
                        != result_run.automation_development_diff_hash
                        or source_run.automation_repository_diff_hash
                        != result_run.automation_repository_diff_hash
                        or source_run.automation_result_hash
                        != result_run.automation_result_hash
                        or source_run.plan_approval_id
                        != result_run.plan_approval_id
                        or result_run.attempt != source_run.attempt + 1
                        or str(action["requirements_snapshot_hash"])
                        != str(source_run.issue_fingerprint or "")
                        or action.get("plan_spec_hash")
                        != source_run.plan_spec_hash
                        or str(action.get("automation_plan_hash") or "")
                        != str(source_run.automation_plan_hash or "")
                        or str(
                            action.get("automation_development_diff_hash") or ""
                        )
                        != str(
                            source_run.automation_development_diff_hash or ""
                        )
                        or str(
                            action.get("automation_repository_diff_hash") or ""
                        )
                        != str(
                            source_run.automation_repository_diff_hash or ""
                        )
                        or str(action.get("automation_result_hash") or "")
                        != str(source_run.automation_result_hash or "")
                        or action.get("plan_approval_id")
                        != source_run.plan_approval_id
                    )
                )
            )
            if invalid_action:
                if result_run is not None:
                    self.store.update_owned_human_review_run(
                        action_id,
                        result_run.id,
                        claim_token,
                        status="failed",
                        finished_at=utc_now(),
                        error=(
                            "Durable human-review action metadata is inconsistent"
                        ),
                        blocked_phase="setup",
                    )
                else:
                    self.store.release_human_review_action(
                        action_id,
                        claim_token,
                    )
                self.store.add_log(
                    result_run.id if result_run else None,
                    "error",
                    "Durable human-review action metadata is inconsistent",
                )
                continue

            assert source_run is not None
            assert result_run is not None
            if source_run.issue_id in self.claimed:
                self.store.release_human_review_action(
                    action_id,
                    claim_token,
                )
                continue
            try:
                snapshot = self.store.get_requirements_snapshot(
                    source_run.issue_identifier,
                    str(source_run.issue_fingerprint or ""),
                )
                if snapshot is None:
                    raise HumanReviewContextError(
                        "frozen requirements snapshot is missing"
                    )
                issue = issue_from_frozen_snapshot(
                    source_run,
                    snapshot,
                )
            except Exception as exc:
                self.store.update_owned_human_review_run(
                    action_id,
                    result_run.id,
                    claim_token,
                    status="failed",
                    finished_at=utc_now(),
                    error=redact_text(
                        f"Human review context could not be loaded: {exc}",
                        self.secret_values,
                    ),
                    blocked_phase="setup",
                )
                continue

            try:
                self._start_issue(
                    issue,
                    attempt=result_run.attempt,
                    previous_run=source_run,
                    precreated_run=result_run,
                    completed_review_action=action,
                    review_action_claim_token=claim_token,
                )
            except Exception as exc:
                self.store.update_owned_human_review_run(
                    action_id,
                    result_run.id,
                    claim_token,
                    status="failed",
                    finished_at=utc_now(),
                    error=redact_text(
                        f"Human review scheduling failed: {exc}",
                        self.secret_values,
                    ),
                    blocked_phase="setup",
                )
                self.store.add_log(
                    result_run.id,
                    "error",
                    redact_text(
                        f"Human review could not start for "
                        f"{source_run.issue_identifier}: {exc}",
                        self.secret_values,
                    )
                    or "",
                )

    async def dispatch_recoverable_human_resumes(self) -> None:
        for resume_run_id in self.store.list_recoverable_human_resume_run_ids():
            if self.available_slots() <= 0:
                return
            handoff = self.store.claim_human_resume_handoff(resume_run_id)
            if handoff is None:
                continue
            handoff_claim_token = str(handoff["handoff_claim_token"])
            reserved_run = self.store.get_run(resume_run_id)
            previous_run = self.store.get_run(str(handoff["predecessor_run_id"]))
            invalid_handoff = (
                reserved_run is None
                or previous_run is None
                or str(handoff["run_id"]) != str(handoff["predecessor_run_id"])
                or (
                    reserved_run is not None
                    and previous_run is not None
                    and (
                        reserved_run.issue_id != previous_run.issue_id
                        or reserved_run.issue_identifier != previous_run.issue_identifier
                        or reserved_run.attempt != previous_run.attempt + 1
                        or reserved_run.plan_spec_hash != previous_run.plan_spec_hash
                        or reserved_run.automation_plan_hash
                        != previous_run.automation_plan_hash
                        or reserved_run.automation_development_diff_hash
                        != previous_run.automation_development_diff_hash
                        or reserved_run.automation_repository_diff_hash
                        != previous_run.automation_repository_diff_hash
                        or reserved_run.automation_result_hash
                        != previous_run.automation_result_hash
                        or reserved_run.plan_approval_id != previous_run.plan_approval_id
                        or previous_run.status != "blocked"
                    )
                )
            )
            if invalid_handoff:
                self.store.update_owned_human_resume_run(
                    resume_run_id,
                    handoff_claim_token,
                    status="failed",
                    finished_at=utc_now(),
                    error="Durable human-resume handoff metadata is inconsistent",
                    blocked_phase="setup",
                )
                self.store.add_log(
                    resume_run_id,
                    "error",
                    "Durable human-resume handoff metadata is inconsistent",
                )
                continue
            assert reserved_run is not None
            assert previous_run is not None
            if reserved_run.issue_id in self.claimed:
                self.store.release_human_resume_handoff(
                    resume_run_id, handoff_claim_token
                )
                continue
            try:
                issue = await self.jira.get_issue(
                    reserved_run.issue_identifier, include_comments=True
                )
                assert_issue_eligible(issue, self.config)
            except asyncio.CancelledError:
                self.store.release_human_resume_handoff(
                    resume_run_id, handoff_claim_token
                )
                raise
            except Exception as exc:
                self.store.release_human_resume_handoff(
                    resume_run_id, handoff_claim_token
                )
                self.store.add_log(
                    resume_run_id,
                    "warning",
                    redact_text(
                        f"Durable human resume deferred for "
                        f"{reserved_run.issue_identifier}: {exc}",
                        self.secret_values,
                    )
                    or "",
                )
                continue
            try:
                self._start_issue(
                    issue,
                    attempt=reserved_run.attempt,
                    human_input=handoff,
                    previous_run=previous_run,
                    precreated_run=reserved_run,
                    handoff_claim_token=handoff_claim_token,
                )
            except Exception as exc:
                self.store.update_owned_human_resume_run(
                    resume_run_id,
                    handoff_claim_token,
                    status="failed",
                    finished_at=utc_now(),
                    error=redact_text(
                        f"Human resume scheduling failed: {exc}", self.secret_values
                    ),
                    blocked_phase="setup",
                )
                self.store.add_log(
                    resume_run_id,
                    "error",
                    redact_text(
                        f"Durable human resume could not start for "
                        f"{reserved_run.issue_identifier}: {exc}",
                        self.secret_values,
                    )
                    or "",
                )

    async def dispatch_human_resumes(self) -> None:
        await self.dispatch_recoverable_human_resumes()
        for candidate in self.store.list_unconsumed_human_inputs():
            if self.available_slots() <= 0:
                return
            input_id = str(candidate["id"])
            human_input = self.store.claim_human_input(input_id)
            if human_input is None:
                continue
            claim_token = str(human_input["claim_token"])
            previous_run = self.store.get_run(str(human_input["run_id"]))
            if (
                previous_run is None
                or not self.store.is_latest_actionable_blocked_run(previous_run.id)
            ):
                self.store.mark_human_input_consumed(input_id, claim_token)
                self.store.add_log(
                    previous_run.id if previous_run else None,
                    "warning",
                    "Discarded human input for a run that is no longer the latest actionable blocked run",
                )
                continue
            if previous_run.issue_id in self.claimed:
                self.store.release_human_input_claim(input_id, claim_token)
                continue
            try:
                issue = await self.jira.get_issue(previous_run.issue_identifier, include_comments=True)
                assert_issue_eligible(issue, self.config)
            except asyncio.CancelledError:
                self.store.release_human_input_claim(input_id, claim_token)
                raise
            except Exception as exc:
                self.store.release_human_input_claim(input_id, claim_token)
                self.store.add_log(
                    previous_run.id,
                    "warning",
                    redact_text(
                        f"Human clarification resume deferred for {previous_run.issue_identifier}: {exc}",
                        self.secret_values,
                    )
                    or "",
                )
                continue
            if not self.store.renew_human_input_claim(input_id, claim_token):
                self.store.add_log(
                    previous_run.id,
                    "warning",
                    "Human clarification resume deferred because its claim lease changed ownership",
                )
                continue
            try:
                reserved_run, reservation_status = self.store.reserve_human_resume(
                    issue,
                    self.workspace_manager.workspace_path_for(issue.identifier),
                    input_id=input_id,
                    claim_token=claim_token,
                    expected_predecessor_run_id=previous_run.id,
                    branch_name=self.workspace_manager.branch_name_for(issue.identifier),
                    attempt=previous_run.attempt + 1,
                )
            except Exception as exc:
                self.store.release_human_input_claim(input_id, claim_token)
                self.store.add_log(
                    previous_run.id,
                    "error",
                    redact_text(
                        f"Human clarification resume reservation failed for "
                        f"{previous_run.issue_identifier}: {exc}",
                        self.secret_values,
                    )
                    or "",
                )
                continue
            if reservation_status == "claim_lost":
                self.store.add_log(
                    previous_run.id,
                    "warning",
                    "Human clarification resume deferred because its claim lease changed ownership",
                )
                continue
            if reservation_status == "stale_predecessor":
                self.store.add_log(
                    previous_run.id,
                    "warning",
                    "Discarded human input because a newer run was created before atomic reservation",
                )
                continue
            if reserved_run is None:
                self.store.add_log(previous_run.id, "error", "Human resume reservation returned no run")
                continue
            handoff = self.store.claim_human_resume_handoff(reserved_run.id)
            if handoff is None:
                self.store.add_log(
                    reserved_run.id,
                    "warning",
                    "Durable human resume was reserved but another dispatcher owns its handoff",
                )
                continue
            handoff_claim_token = str(handoff["handoff_claim_token"])
            try:
                self._start_issue(
                    issue,
                    attempt=previous_run.attempt + 1,
                    human_input=handoff,
                    previous_run=previous_run,
                    precreated_run=reserved_run,
                    handoff_claim_token=handoff_claim_token,
                )
            except Exception as exc:
                self.store.update_owned_human_resume_run(
                    reserved_run.id,
                    handoff_claim_token,
                    status="failed",
                    finished_at=utc_now(),
                    error=redact_text(f"Human resume scheduling failed: {exc}", self.secret_values),
                    blocked_phase="setup",
                )
                self.store.add_log(
                    reserved_run.id,
                    "error",
                    redact_text(
                        f"Human clarification resume could not start for "
                        f"{previous_run.issue_identifier}: {exc}",
                        self.secret_values,
                    )
                    or "",
                )
                continue

    def dispatch_fresh_issues(self, issues: list[Issue]) -> None:
        if self.available_slots() <= 0:
            return
        for issue in sort_issues_for_dispatch(issues):
            if self.available_slots() <= 0:
                return
            if issue.id in self.claimed:
                continue
            if self.blocked_waiting_for_human(issue):
                continue
            if self.already_completed(issue):
                continue
            try:
                assert_issue_eligible(issue, self.config)
            except OrchestratorError:
                continue
            self._start_issue(issue, attempt=1)

    def _start_issue(
        self,
        issue: Issue,
        attempt: int,
        *,
        human_input: dict[str, Any] | None = None,
        previous_run: RunRecord | None = None,
        precreated_run: RunRecord | None = None,
        handoff_claim_token: str | None = None,
        completed_review_action: dict[str, Any] | None = None,
        review_action_claim_token: str | None = None,
    ) -> None:
        human_resume = handoff_claim_token is not None
        completed_review = completed_review_action is not None
        if completed_review != bool(review_action_claim_token):
            raise OrchestratorError(
                "completed-review action and claim must be provided together"
            )
        durable_modes = int(human_resume) + int(completed_review)
        if (precreated_run is None and durable_modes) or (
            precreated_run is not None and durable_modes != 1
        ):
            raise OrchestratorError(
                "a reserved run requires exactly one durable ownership mode"
            )
        self.claimed.add(issue.id)
        self.blocked.discard(issue.id)
        self.completed.pop(issue.id, None)
        single = SingleIssueOrchestrator(
            self.workflow,
            self.jira,
            self.store,
            workspace_manager=self.workspace_manager,
            codex_runner=self.codex_runner,
            runtime_manager=self.runtime_manager,
            secret_values=self.secret_values,
        )

        async def run_single() -> OnceResult:
            return await single.run_once(
                issue.identifier,
                attempt=attempt,
                human_input=human_input,
                previous_run=previous_run,
                prepared_issue=issue if precreated_run is not None else None,
                precreated_run=precreated_run,
                resume_handoff_claim_token=handoff_claim_token,
                completed_review_action=completed_review_action,
                review_action_claim_token=review_action_claim_token,
            )

        async def execute() -> OnceResult:
            child_task: asyncio.Task[OnceResult] | None = None

            async def cancel_child() -> None:
                if child_task is None or child_task.done():
                    return
                child_task.cancel()
                try:
                    await child_task
                except (asyncio.CancelledError, Exception):
                    pass

            try:
                if precreated_run is None:
                    return await run_single()
                child_task = asyncio.create_task(run_single())
                heartbeat_seconds = min(
                    60.0,
                    max(1.0, HUMAN_RESUME_HANDOFF_LEASE.total_seconds() / 3),
                )
                while True:
                    done, _ = await asyncio.wait(
                        {child_task}, timeout=heartbeat_seconds
                    )
                    if child_task in done:
                        return child_task.result()
                    if completed_review:
                        assert completed_review_action is not None
                        assert review_action_claim_token is not None
                        renewed = self.store.renew_human_review_action(
                            str(completed_review_action["id"]),
                            review_action_claim_token,
                        )
                    else:
                        assert handoff_claim_token is not None
                        renewed = self.store.renew_human_resume_handoff(
                            precreated_run.id,
                            handoff_claim_token,
                        )
                    if not renewed:
                        await cancel_child()
                        raise OrchestratorError(
                            "durable human action ownership changed during execution"
                        )
            except asyncio.CancelledError:
                await cancel_child()
                if completed_review:
                    assert completed_review_action is not None
                    assert review_action_claim_token is not None
                    self.store.update_owned_human_review_run(
                        str(completed_review_action["id"]),
                        precreated_run.id,
                        review_action_claim_token,
                        status="cancelled",
                        finished_at=utc_now(),
                        error="Durable completed-review run was cancelled",
                        blocked_phase="orchestration",
                    )
                elif precreated_run is not None and handoff_claim_token is not None:
                    self.store.update_owned_human_resume_run(
                        precreated_run.id,
                        handoff_claim_token,
                        status="cancelled",
                        finished_at=utc_now(),
                        error="Durable human resume was cancelled",
                        blocked_phase="orchestration",
                    )
                raise
            except Exception as exc:
                if completed_review:
                    assert completed_review_action is not None
                    assert review_action_claim_token is not None
                    updated = self.store.update_owned_human_review_run(
                        str(completed_review_action["id"]),
                        precreated_run.id,
                        review_action_claim_token,
                        status="failed",
                        finished_at=utc_now(),
                        error=redact_text(
                            f"Durable completed-review setup failed: {exc}",
                            self.secret_values,
                        ),
                        blocked_phase="setup",
                    )
                    if updated is None:
                        raise OrchestratorError(
                            "stale completed-review owner was fenced after takeover"
                        ) from exc
                    return OnceResult(
                        issue=issue,
                        prompt="",
                        run=updated,
                        workspace=None,
                    )
                if precreated_run is None or handoff_claim_token is None:
                    raise
                updated = self.store.update_owned_human_resume_run(
                    precreated_run.id,
                    handoff_claim_token,
                    status="failed",
                    finished_at=utc_now(),
                    error=redact_text(
                        f"Durable human resume setup failed: {exc}", self.secret_values
                    ),
                    blocked_phase="setup",
                )
                if updated is None:
                    raise OrchestratorError(
                        "stale durable human-resume owner was fenced after takeover"
                    ) from exc
                return OnceResult(
                    issue=issue,
                    prompt="",
                    run=updated,
                    workspace=None,
                )

        try:
            task = asyncio.create_task(execute())
        except Exception as exc:
            self.claimed.discard(issue.id)
            if completed_review:
                assert completed_review_action is not None
                assert review_action_claim_token is not None
                self.store.update_owned_human_review_run(
                    str(completed_review_action["id"]),
                    precreated_run.id,
                    review_action_claim_token,
                    status="failed",
                    finished_at=utc_now(),
                    error=redact_text(
                        f"Completed-review scheduling failed: {exc}",
                        self.secret_values,
                    ),
                    blocked_phase="setup",
                )
            elif precreated_run is not None and handoff_claim_token is not None:
                self.store.update_owned_human_resume_run(
                    precreated_run.id,
                    handoff_claim_token,
                    status="failed",
                    finished_at=utc_now(),
                    error=redact_text(f"Human resume scheduling failed: {exc}", self.secret_values),
                    blocked_phase="setup",
                )
            raise
        self.running[issue.id] = RunningIssue(
            issue_id=issue.id,
            identifier=issue.identifier,
            attempt=attempt,
            task=task,
            started_at=utc_now(),
            human_resume=human_resume,
            completed_review=completed_review,
        )

    async def _schedule_retry_or_release(self, running: RunningIssue, error: str | None) -> None:
        if running.attempt <= self.config.agent.max_retries:
            next_attempt = running.attempt + 1
            delay = retry_backoff_seconds(running.attempt, self.config)
            self.retry_queue[running.issue_id] = RetryEntry(
                issue_id=running.issue_id,
                identifier=running.identifier,
                attempt=next_attempt,
                due_at=time.monotonic() + delay,
                error=error,
            )
            self.claimed.add(running.issue_id)
            self.store.add_log(
                None,
                "warning",
                redact_text(f"Retry scheduled for {running.identifier} in {delay}s: {error}", self.secret_values) or "",
            )
            return
        self.claimed.discard(running.issue_id)

    def available_slots(self) -> int:
        return max(0, self.config.agent.max_concurrent_agents - len(self.running))

    def snapshot(self) -> dict[str, Any]:
        return {
            "workflow_path": str(self.workflow.path),
            "jql": self.config.tracker.jql,
            "poll_interval_seconds": self.config.polling.interval_seconds,
            "max_concurrent_agents": self.config.agent.max_concurrent_agents,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "last_poll_error": self.last_poll_error,
            "running": [
                {
                    "issue_id": item.issue_id,
                    "identifier": item.identifier,
                    "attempt": item.attempt,
                    "started_at": item.started_at.isoformat(),
                }
                for item in self.running.values()
            ],
            "retry_queue": [
                {
                    "issue_id": item.issue_id,
                    "identifier": item.identifier,
                    "attempt": item.attempt,
                    "seconds_until_due": item.seconds_until_due(),
                    "error": item.error,
                }
                for item in self.retry_queue.values()
            ],
            "blocked_issue_ids": sorted(self.blocked),
            "completed_issue_ids": sorted(self.completed),
        }

    def already_completed(self, issue: Issue) -> bool:
        fingerprint = issue_description_fingerprint(issue)
        if self.completed.get(issue.id) == fingerprint:
            return True
        previous = self.store.latest_completed_run_for_issue_fingerprint(issue.identifier, fingerprint)
        if previous:
            self.completed[issue.id] = fingerprint
            return True
        current_snapshot = issue.requirements_snapshot
        latest = self.store.latest_run_for_issue(issue.identifier)
        if (
            current_snapshot is not None
            and current_snapshot.schema_version == "jira-requirements/v4"
            and requirements_planning_safety_error(issue) is None
            and latest is not None
            and latest.status == "completed"
            and latest.issue_fingerprint
            and latest.plan_spec_hash
            and latest.plan_approval_id
        ):
            try:
                frozen_snapshot = self.store.get_requirements_snapshot(
                    issue.identifier,
                    latest.issue_fingerprint,
                )
            except StoreIntegrityError:
                frozen_snapshot = None
            if (
                frozen_snapshot is not None
                and frozen_snapshot.schema_version
                in {
                    "jira-requirements/v1",
                    "jira-requirements/v2",
                    "jira-requirements/v3",
                }
                and requirements_planning_authority_equivalent(
                    frozen_snapshot,
                    current_snapshot,
                )
            ):
                approval = self.store.get_plan_approval(
                    latest.plan_approval_id
                )
                plan_message = read_plan_message_for_run(
                    latest,
                    self.config.codex.output_plan_file,
                )
                try:
                    frozen_plan = (
                        parse_frozen_legacy_plan_spec(
                            plan_message or "",
                            expected_issue_key=issue.identifier,
                            expected_snapshot_hash=latest.issue_fingerprint,
                            issue_type=issue.issue_type,
                            requirements_snapshot=frozen_snapshot,
                        )
                        if approval is not None
                        and not approval.get("invalidated_at")
                        and approval.get("issue_identifier")
                        == issue.identifier
                        and approval.get("plan_spec_hash")
                        == latest.plan_spec_hash
                        and approval.get("requirements_snapshot_hash")
                        == latest.issue_fingerprint
                        else None
                    )
                except PlanSpecError:
                    frozen_plan = None
                if (
                    frozen_plan is not None
                    and frozen_plan.content_hash() == latest.plan_spec_hash
                ):
                    # Avoid duplicate implementation after the snapshot schema
                    # migration. Keep the cache keyed by the live v4 identity.
                    self.completed[issue.id] = fingerprint
                    return True
        return False

    def blocked_waiting_for_human(self, issue: Issue) -> bool:
        latest_run = self.store.latest_run_for_issue(issue.identifier)
        return bool(latest_run and latest_run.status == "blocked")


def is_requirements_safety_retry(
    previous_run: RunRecord | None,
    human_input: dict[str, Any] | None,
) -> bool:
    if previous_run is None or previous_run.blocked_phase != "planning":
        return False
    previous_error = str(previous_run.error or "")
    gate_prefixes = (
        "Required attachment analysis is incomplete",
        "Unresolved Jira requirement contradictions",
        "Requirements snapshot is incomplete",
        "Canonical Jira requirements snapshot is missing",
    )
    if not previous_error.startswith(gate_prefixes):
        return False
    return bool(str((human_input or {}).get("response") or "").strip())


def is_contradiction_resolution_retry(
    previous_run: RunRecord | None,
    human_input: dict[str, Any] | None,
) -> bool:
    if previous_run is None or not is_requirements_safety_retry(previous_run, human_input):
        return False
    return str(previous_run.error or "").startswith("Unresolved Jira requirement contradictions")


def requirements_planning_safety_error(issue: Issue) -> str | None:
    snapshot = issue.requirements_snapshot
    if snapshot is None:
        return (
            "Canonical Jira requirements snapshot is missing; planning and implementation are blocked. "
            "Refresh the Jira issue through the requirements adapter and retry. Human approval cannot "
            "waive the canonical evidence requirement."
        )

    if snapshot.unresolved_contradictions:
        contradictions = "; ".join(
            f"{decision.id}: {decision.text}"
            for decision in snapshot.unresolved_contradictions
        )
        return (
            "Unresolved Jira requirement contradictions must be resolved before planning. "
            f"Contradictions: {contradictions}. "
            "Update Jira with the authoritative decision so its source, author, timestamp, and "
            "authority are captured in a refreshed requirements snapshot, then retry. "
            "A dashboard response or prior approval cannot waive this gate."
        )

    blocking_incomplete_reasons = [
        reason
        for reason in snapshot.incomplete_reasons
        if not _is_attachment_only_incomplete_reason(reason, snapshot)
    ]
    if blocking_incomplete_reasons:
        incomplete_reasons = "; ".join(blocking_incomplete_reasons)
        return (
            "Requirements snapshot is incomplete; planning and implementation are blocked. "
            f"Incomplete evidence: {incomplete_reasons}. "
            "Repair the Jira ingestion or analyzer source, refresh the versioned requirements "
            "snapshot, and retry. Human approval cannot waive this evidence gate."
        )
    return None


def _is_attachment_only_incomplete_reason(reason: str, snapshot: Any) -> bool:
    """Ignore legacy analyzer failures for evidence that planning never consumes."""

    normalized = reason.casefold()
    related_issues = (
        ([snapshot.parent] if snapshot.parent is not None else [])
        + list(snapshot.children)
        + list(snapshot.linked_issues)
        + list(snapshot.dependencies)
    )
    attachment_filenames = {
        attachment.filename.casefold()
        for attachment in snapshot.attachments
    }
    attachment_filenames.update(
        attachment.filename.casefold()
        for related in related_issues
        for attachment in related.attachments
    )
    text_without_filenames = normalized
    filename_matched = False
    for filename in attachment_filenames:
        if filename and filename in text_without_filenames:
            filename_matched = True
            text_without_filenames = text_without_filenames.replace(filename, "")

    attachment_markers = (
        "attachment",
        "ocr",
        "vision analyzer",
        "vision summary",
        "image analyzer",
        "image analysis",
    )
    mandatory_markers = (
        "description",
        "acceptance criteria",
        "acceptance criterion",
        "comment",
    )
    return (
        (filename_matched or any(marker in normalized for marker in attachment_markers))
        and not any(marker in text_without_filenames for marker in mandatory_markers)
    )


def assert_issue_eligible(issue: Issue, config: WorkflowConfig) -> None:
    if config.tracker.active_statuses and issue.status not in config.tracker.active_statuses:
        raise OrchestratorError(f"Issue {issue.identifier} status is not active: {issue.status}")
    required = config.tracker.required_label_set
    if required:
        labels = {label.lower() for label in issue.labels}
        missing = sorted(required - labels)
        if missing:
            raise OrchestratorError(f"Issue {issue.identifier} is missing required labels: {', '.join(missing)}")
    unresolved = [
        blocker.identifier or blocker.id or "unknown"
        for blocker in issue.blocked_by
        if (blocker.status or "") not in config.tracker.terminal_statuses
    ]
    if unresolved:
        raise OrchestratorError(f"Issue {issue.identifier} is blocked by unresolved issues: {', '.join(unresolved)}")


def select_dispatchable_issues(
    issues: list[Issue],
    claimed_issue_ids: set[str],
    config: WorkflowConfig,
) -> list[Issue]:
    selected: list[Issue] = []
    for issue in sort_issues_for_dispatch(issues):
        if issue.id in claimed_issue_ids:
            continue
        try:
            assert_issue_eligible(issue, config)
        except OrchestratorError:
            continue
        selected.append(issue)
        if len(selected) >= config.agent.max_concurrent_agents:
            break
    return selected


def sort_issues_for_dispatch(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=lambda issue: (priority_rank(issue.priority), updated_timestamp(issue)))


def priority_rank(priority: str | None) -> int:
    return PRIORITY_RANKS.get((priority or "").lower(), 5)


def updated_timestamp(issue: Issue) -> float:
    updated = issue.updated_at
    if updated is None:
        return float("inf")
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return updated.timestamp()


def retry_backoff_seconds(attempt: int, config: WorkflowConfig) -> int:
    if attempt <= 0:
        return 0
    return min(2 ** (attempt - 1), config.agent.max_retry_backoff_seconds)


def is_retryable_error(error: str | None) -> bool:
    if not error:
        return True
    lowered = error.lower()
    return not any(marker in lowered for marker in NON_RETRYABLE_ERROR_MARKERS)


def _run_automation_git(
    repository_path: Path,
    *arguments: str,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository_path), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_BASELINE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AutomationPlanError(
            f"Git {' '.join(arguments)} failed for automation repository: {exc}"
        ) from exc
    if result.returncode not in allowed_returncodes:
        detail = (result.stderr or result.stdout).strip()
        raise AutomationPlanError(
            f"Git {' '.join(arguments)} failed for automation repository: "
            f"{detail[:300]}"
        )
    return result


def _automation_git_directory(repository_path: Path) -> Path:
    git_directory = repository_path / ".git"
    try:
        metadata = git_directory.lstat()
    except OSError as exc:
        raise AutomationPlanError(
            "Automation repository Git metadata is unavailable"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or git_directory.is_symlink():
        raise AutomationPlanError(
            "Automation repository must use an in-workspace .git directory"
        )
    info_directory = git_directory / "info"
    if info_directory.exists() or info_directory.is_symlink():
        try:
            info_metadata = info_directory.lstat()
        except OSError as exc:
            raise AutomationPlanError(
                "Automation repository Git info directory is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(info_metadata.st_mode)
            or info_directory.is_symlink()
        ):
            raise AutomationPlanError(
                "Automation repository .git/info must be a real directory"
            )
    return git_directory


def _safe_automation_relative_target(
    repository_path: Path,
    relative_name: str,
) -> Path:
    relative_path = Path(relative_name)
    if (
        not relative_name
        or relative_path.is_absolute()
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise AutomationPlanError(
            f"Automation repository returned an unsafe path: {relative_name!r}"
        )
    current = repository_path
    for component in relative_path.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise AutomationPlanError(
                "Automation ignored-file path traverses a symbolic link: "
                f"{relative_name}"
            )
    target = repository_path / relative_path
    try:
        target.parent.resolve(strict=False).relative_to(repository_path)
    except (OSError, ValueError) as exc:
        raise AutomationPlanError(
            f"Automation repository path resolves outside its checkout: {relative_name}"
        ) from exc
    return target


def _read_guard_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
) -> tuple[bytes, int]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        raise AutomationPlanError(
            "Secure automation mutation-guard reads are unavailable"
        )
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | nofollow | nonblock)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AutomationPlanError(f"{label} is not a regular file")
        if metadata.st_nlink != 1 or metadata.st_uid != os.geteuid():
            raise AutomationPlanError(
                f"{label} must be a private file owned by the current user"
            )
        if metadata.st_size > max_bytes:
            raise AutomationPlanError(
                f"{label} exceeds the {max_bytes}-byte mutation-guard limit"
            )
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > max_bytes:
            raise AutomationPlanError(
                f"{label} exceeds the {max_bytes}-byte mutation-guard limit"
            )
        return content, stat.S_IMODE(metadata.st_mode)
    except AutomationPlanError:
        raise
    except OSError as exc:
        raise AutomationPlanError(f"Could not read {label}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_guard_file(path: Path, content: bytes, mode: int, *, label: str) -> None:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise AutomationPlanError(f"Could not restore {label}: unsafe parent directory")
    temporary = parent / f".{path.name}.symphony-{os.getpid()}-{time.time_ns()}"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise AutomationPlanError(f"Could not restore {label}")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
    except AutomationPlanError:
        raise
    except OSError as exc:
        raise AutomationPlanError(f"Could not restore {label}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _ignored_automation_paths(repository_path: Path) -> tuple[str, ...]:
    output = _run_automation_git(
        repository_path,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    ).stdout
    return tuple(sorted(path for path in output.split("\0") if path))


def _dirty_automation_paths(
    repository_path: Path,
) -> tuple[str, ...]:
    tracked_output = _run_automation_git(
        repository_path,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        "HEAD",
        "--",
    ).stdout
    untracked_output = _run_automation_git(
        repository_path,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    ).stdout
    return tuple(
        sorted(
            {
                path
                for output in (tracked_output, untracked_output)
                for path in output.split("\0")
                if path
            }
        )
    )


def _hidden_automation_index_paths(repository_path: Path) -> tuple[str, ...]:
    output = _run_automation_git(
        repository_path,
        "ls-files",
        "-v",
        "-z",
    ).stdout
    hidden: list[str] = []
    for entry in output.split("\0"):
        if not entry:
            continue
        if len(entry) < 3 or entry[1] != " ":
            raise AutomationPlanError(
                "Git returned malformed automation index state"
            )
        tag, path = entry[0], entry[2:]
        if tag == "S" or tag.islower():
            hidden.append(path)
    return tuple(sorted(hidden))


def _automation_local_git_hiding_error(repository_path: Path) -> str | None:
    git_directory = _automation_git_directory(repository_path)
    hidden_index_paths = _hidden_automation_index_paths(repository_path)
    if hidden_index_paths:
        return (
            "Automation repository index uses assume-unchanged or skip-worktree "
            "flags: " + ", ".join(hidden_index_paths[:10])
        )

    filemode = _run_automation_git(
        repository_path,
        "config",
        "--local",
        "--bool",
        "--get",
        "core.filemode",
        allowed_returncodes=(0, 1),
    )
    if filemode.returncode == 0 and filemode.stdout.strip() != "true":
        return (
            "Automation repository local Git config disables core.filemode and "
            "can hide executable-bit changes"
        )

    config_names = {
        name.casefold()
        for name in _run_automation_git(
            repository_path,
            "config",
            "--local",
            "--name-only",
            "--null",
            "--list",
        ).stdout.split("\0")
        if name
    }
    exact_forbidden = {
        "core.attributesfile",
        "core.excludesfile",
        "core.fsmonitor",
        "core.sparsecheckout",
        "core.sparsecheckoutcone",
        "diff.external",
        "extensions.worktreeconfig",
        "index.sparse",
        "interactive.difffilter",
        "status.showuntrackedfiles",
    }
    forbidden = sorted(
        name
        for name in config_names
        if name in exact_forbidden
        or name.startswith(("include.", "includeif.", "filter."))
        or (
            name.startswith("diff.")
            and name.endswith((".command", ".textconv"))
        )
        or (name.startswith("submodule.") and name.endswith(".ignore"))
    )
    if forbidden:
        return (
            "Automation repository local Git config can hide or transform changes: "
            + ", ".join(forbidden[:10])
        )

    for relative_name in (
        "config.worktree",
        "info/exclude",
        "info/attributes",
        "info/sparse-checkout",
    ):
        path = git_directory / relative_name
        if not path.exists() and not path.is_symlink():
            continue
        try:
            content, _ = _read_guard_file(
                path,
                label=f"automation Git metadata {relative_name!r}",
                max_bytes=MAX_AUTOMATION_GIT_METADATA_BYTES,
            )
            text = content.decode("utf-8")
        except (AutomationPlanError, UnicodeDecodeError) as exc:
            return f"Automation repository has unsafe local Git metadata: {exc}"
        active_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if active_lines:
            return (
                f"Automation repository local Git metadata {relative_name!r} "
                "can hide or transform changes"
            )
    return None


def capture_automation_mutation_guard(
    workspace_path: Path,
    repository: str,
) -> AutomationMutationGuard:
    repository_path = (workspace_path.resolve() / repository).resolve()
    try:
        repository_path.relative_to(workspace_path.resolve())
    except ValueError as exc:
        raise AutomationPlanError(
            "Automation mutation-guard repository resolves outside the workspace"
        ) from exc
    hiding_error = _automation_local_git_hiding_error(repository_path)
    if hiding_error:
        raise AutomationPlanError(hiding_error)

    ignored_paths = _ignored_automation_paths(repository_path)
    if len(ignored_paths) > MAX_AUTOMATION_IGNORED_FILES:
        raise AutomationPlanError(
            "Automation repository has too many ignored files for a bounded "
            "mutation guard"
        )
    ignored_files: list[AutomationIgnoredFile] = []
    total_bytes = 0
    for relative_name in ignored_paths:
        target = _safe_automation_relative_target(
            repository_path,
            relative_name,
        )
        content, mode = _read_guard_file(
            target,
            label=f"ignored automation file {relative_name!r}",
            max_bytes=MAX_AUTOMATION_IGNORED_BYTES,
        )
        total_bytes += len(content)
        if total_bytes > MAX_AUTOMATION_IGNORED_BYTES:
            raise AutomationPlanError(
                "Automation repository ignored files exceed the bounded "
                "mutation-guard byte limit"
            )
        ignored_files.append(
            AutomationIgnoredFile(relative_name, content, mode)
        )

    dirty_paths = _dirty_automation_paths(repository_path)
    if len(dirty_paths) > MAX_AUTOMATION_IGNORED_FILES:
        raise AutomationPlanError(
            "Automation repository has too many dirty files for a "
            "bounded mutation guard"
        )
    existing_dirty_files: list[AutomationIgnoredFile] = []
    existing_dirty_bytes = 0
    for relative_name in dirty_paths:
        target = _safe_automation_relative_target(
            repository_path,
            relative_name,
        )
        if not target.exists() and not target.is_symlink():
            # A retained deletion has no working-tree bytes to protect. If a pass
            # recreates it under an ignore rule, restoring the deletion means
            # removing that new ignored file.
            continue
        content, mode = _read_guard_file(
            target,
            label=f"dirty automation file {relative_name!r}",
            max_bytes=MAX_AUTOMATION_IGNORED_BYTES,
        )
        existing_dirty_bytes += len(content)
        if existing_dirty_bytes > MAX_AUTOMATION_IGNORED_BYTES:
            raise AutomationPlanError(
                "Automation repository existing dirty files exceed the "
                "bounded mutation-guard byte limit"
            )
        existing_dirty_files.append(
            AutomationIgnoredFile(relative_name, content, mode)
        )

    git_directory = _automation_git_directory(repository_path)
    git_metadata: list[tuple[str, bytes | None, int | None]] = []
    for relative_name in AUTOMATION_GIT_METADATA_PATHS:
        path = git_directory / relative_name
        if not path.exists() and not path.is_symlink():
            git_metadata.append((relative_name, None, None))
            continue
        content, mode = _read_guard_file(
            path,
            label=f"automation Git metadata {relative_name!r}",
            max_bytes=MAX_AUTOMATION_GIT_METADATA_BYTES,
        )
        git_metadata.append((relative_name, content, mode))
    return AutomationMutationGuard(
        ignored_files=tuple(ignored_files),
        existing_dirty_files=tuple(existing_dirty_files),
        git_metadata=tuple(git_metadata),
    )


def restore_automation_mutation_guard(
    workspace_path: Path,
    repository: str,
    baseline: AutomationMutationGuard,
) -> str | None:
    """Restore ignored/local-Git drift and report any attempted scope bypass."""

    workspace_root = workspace_path.resolve()
    repository_entry = workspace_root / repository
    try:
        entry_metadata = repository_entry.lstat()
        if (
            not stat.S_ISDIR(entry_metadata.st_mode)
            or repository_entry.is_symlink()
        ):
            raise AutomationPlanError(
                "automation repository was replaced by a non-directory or symlink"
            )
        repository_path = repository_entry.resolve()
        repository_path.relative_to(workspace_root)
        _automation_git_directory(repository_path)
    except (OSError, ValueError, AutomationPlanError) as exc:
        return (
            "Automation pass replaced or redirected the isolated automation "
            "checkout; Symphony refused to follow it and could not restore the "
            f"bounded pre-pass state: {exc}"
        )
    baseline_ignored = {item.path: item for item in baseline.ignored_files}
    baseline_existing_dirty = {
        item.path: item for item in baseline.existing_dirty_files
    }
    drift: list[str] = []
    restoration_errors: list[str] = []

    try:
        current_ignored_paths = _ignored_automation_paths(repository_path)
    except AutomationPlanError as exc:
        current_ignored_paths = ()
        drift.append("ignored-file inventory became unreadable")
        restoration_errors.append(str(exc))

    current_ignored = set(current_ignored_paths)
    if current_ignored != set(baseline_ignored):
        drift.append("ignored file set changed")
    for relative_name in current_ignored_paths:
        try:
            target = _safe_automation_relative_target(
                repository_path,
                relative_name,
            )
        except AutomationPlanError as exc:
            restoration_errors.append(str(exc))
            continue
        expected = baseline_ignored.get(relative_name)
        if expected is None:
            previously_dirty = baseline_existing_dirty.get(relative_name)
            if previously_dirty is not None:
                drift.append(
                    "pre-existing dirty file became ignored: "
                    + relative_name
                )
                try:
                    metadata = target.lstat()
                    if stat.S_ISDIR(metadata.st_mode):
                        raise AutomationPlanError(
                            "refusing to replace directory at pre-existing "
                            f"untracked path {relative_name!r}"
                        )
                    _write_guard_file(
                        target,
                        previously_dirty.content,
                        previously_dirty.mode,
                        label=(
                            "pre-existing dirty automation file "
                            f"{relative_name!r}"
                        ),
                    )
                except (OSError, AutomationPlanError) as exc:
                    restoration_errors.append(str(exc))
                continue
            try:
                metadata = target.lstat()
                if stat.S_ISDIR(metadata.st_mode):
                    raise AutomationPlanError(
                        f"refusing to remove ignored directory {relative_name!r}"
                    )
                target.unlink()
            except (OSError, AutomationPlanError) as exc:
                restoration_errors.append(str(exc))
            continue
        try:
            content, mode = _read_guard_file(
                target,
                label=f"ignored automation file {relative_name!r}",
                max_bytes=MAX_AUTOMATION_IGNORED_BYTES,
            )
        except AutomationPlanError:
            content, mode = b"", -1
        if content != expected.content or mode != expected.mode:
            drift.append(f"ignored file changed: {relative_name}")

    for relative_name, expected in baseline_ignored.items():
        try:
            target = _safe_automation_relative_target(
                repository_path,
                relative_name,
            )
            current = target.lstat() if (target.exists() or target.is_symlink()) else None
            if current is not None and stat.S_ISDIR(current.st_mode):
                raise AutomationPlanError(
                    f"refusing to replace directory at ignored path {relative_name!r}"
                )
            content, mode = (
                _read_guard_file(
                    target,
                    label=f"ignored automation file {relative_name!r}",
                    max_bytes=MAX_AUTOMATION_IGNORED_BYTES,
                )
                if current is not None and stat.S_ISREG(current.st_mode)
                else (b"", -1)
            )
            if content != expected.content or mode != expected.mode:
                _write_guard_file(
                    target,
                    expected.content,
                    expected.mode,
                    label=f"ignored automation file {relative_name!r}",
                )
        except (OSError, AutomationPlanError) as exc:
            restoration_errors.append(str(exc))

    try:
        hidden_paths = _hidden_automation_index_paths(repository_path)
    except AutomationPlanError as exc:
        hidden_paths = ()
        restoration_errors.append(str(exc))
    if hidden_paths:
        drift.append("automation index hiding flags changed")
        for offset in range(0, len(hidden_paths), 128):
            batch = hidden_paths[offset : offset + 128]
            try:
                _run_automation_git(
                    repository_path,
                    "update-index",
                    "--no-assume-unchanged",
                    "--",
                    *batch,
                )
                _run_automation_git(
                    repository_path,
                    "update-index",
                    "--no-skip-worktree",
                    "--",
                    *batch,
                )
            except AutomationPlanError as exc:
                restoration_errors.append(str(exc))
        try:
            remaining_hidden_paths = _hidden_automation_index_paths(
                repository_path
            )
        except AutomationPlanError as exc:
            restoration_errors.append(str(exc))
        else:
            if remaining_hidden_paths:
                restoration_errors.append(
                    "could not clear automation index hiding flags from: "
                    + ", ".join(remaining_hidden_paths[:10])
                )

    try:
        git_directory = _automation_git_directory(repository_path)
        for relative_name, expected_content, expected_mode in baseline.git_metadata:
            target = git_directory / relative_name
            current_content: bytes | None = None
            current_mode: int | None = None
            if target.exists() or target.is_symlink():
                try:
                    current_content, current_mode = _read_guard_file(
                        target,
                        label=f"automation Git metadata {relative_name!r}",
                        max_bytes=MAX_AUTOMATION_GIT_METADATA_BYTES,
                    )
                except AutomationPlanError:
                    current_content, current_mode = b"", -1
            if (
                current_content == expected_content
                and current_mode == expected_mode
            ):
                continue
            drift.append(f"local Git metadata changed: {relative_name}")
            try:
                if expected_content is None:
                    metadata = target.lstat()
                    if stat.S_ISDIR(metadata.st_mode):
                        raise AutomationPlanError(
                            f"refusing to remove Git metadata directory {relative_name!r}"
                        )
                    target.unlink()
                else:
                    assert expected_mode is not None
                    _write_guard_file(
                        target,
                        expected_content,
                        expected_mode,
                        label=f"automation Git metadata {relative_name!r}",
                    )
            except (FileNotFoundError, OSError, AutomationPlanError) as exc:
                if not isinstance(exc, FileNotFoundError):
                    restoration_errors.append(str(exc))
    except AutomationPlanError as exc:
        restoration_errors.append(str(exc))

    if not drift and not restoration_errors:
        return None
    message = (
        "Automation pass changed Git-ignored files or local Git hiding state; "
        "Symphony restored the bounded pre-pass state."
    )
    if drift:
        message += " Detected: " + "; ".join(sorted(set(drift))[:10]) + "."
    if restoration_errors:
        message += (
            " Some state could not be restored safely: "
            + "; ".join(restoration_errors[:5])
        )
    return message


def inspect_automation_repository(
    workspace_path: Path,
    repository: str,
    *,
    expected_head_sha: str | None = None,
    expected_branch_name: str | None = None,
    require_clean: bool,
) -> AutomationRepositoryState:
    """Validate the isolated automation checkout without touching its source repo."""

    workspace_root = workspace_path.resolve()
    repository_path = (workspace_root / repository).resolve()
    try:
        repository_path.relative_to(workspace_root)
    except ValueError as exc:
        raise AutomationPlanError(
            f"Automation repository {repository!r} resolves outside the workspace"
        ) from exc
    if not repository_path.is_dir():
        raise AutomationPlanError(
            f"Automation repository {repository!r} is missing at {repository_path}"
        )
    git_metadata = repository_path / ".git"
    if git_metadata.is_symlink() or not git_metadata.is_dir():
        raise AutomationPlanError(
            f"Automation repository {repository!r} must be an isolated clone with "
            "an in-workspace .git directory"
        )

    def git(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(repository_path), *arguments],
                capture_output=True,
                text=True,
                check=False,
                timeout=GIT_BASELINE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutomationPlanError(
                f"Git {' '.join(arguments)} failed for automation repository "
                f"{repository!r}: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise AutomationPlanError(
                f"Git {' '.join(arguments)} failed for automation repository "
                f"{repository!r}: {detail[:300]}"
            )
        return result.stdout.strip()

    reported_root = Path(git("rev-parse", "--show-toplevel")).resolve()
    if reported_root != repository_path:
        raise AutomationPlanError(
            f"Automation repository {repository!r} must identify Git worktree root "
            f"{reported_root}, not {repository_path}"
        )
    head_sha = git("rev-parse", "HEAD")
    if expected_head_sha and head_sha != expected_head_sha:
        raise AutomationPlanError(
            f"Automation repository {repository!r} moved from baseline "
            f"{expected_head_sha} to {head_sha}"
        )
    branch_name = git("symbolic-ref", "--quiet", "--short", "HEAD")
    if expected_branch_name and branch_name != expected_branch_name:
        raise AutomationPlanError(
            f"Automation repository {repository!r} is on branch {branch_name!r}; "
            f"expected the exact Jira branch {expected_branch_name!r}"
        )
    remotes = tuple(remote for remote in git("remote").splitlines() if remote)
    if remotes:
        raise AutomationPlanError(
            f"Automation repository {repository!r} must have no configured remotes; "
            f"found {', '.join(remotes)}"
        )
    hiding_error = _automation_local_git_hiding_error(repository_path)
    if hiding_error:
        raise AutomationPlanError(hiding_error)
    dirty = bool(git("status", "--porcelain=v1", "--untracked-files=all"))
    tracked_paths = git(
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        "HEAD",
        "--",
    ).split("\0")
    untracked_paths = git(
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    ).split("\0")
    changed_paths = tuple(
        sorted(
            {
                path
                for path in (*tracked_paths, *untracked_paths)
                if path
            }
        )
    )
    name_status_parts = [
        value
        for value in git(
            "diff",
            "--name-status",
            "--no-renames",
            "-z",
            "HEAD",
            "--",
        ).split("\0")
        if value
    ]
    if len(name_status_parts) % 2:
        raise AutomationPlanError(
            f"Git returned malformed changed-file status for automation repository {repository!r}"
        )
    changed_file_types: dict[str, str] = {}
    for index in range(0, len(name_status_parts), 2):
        status = name_status_parts[index]
        path = name_status_parts[index + 1]
        changed_file_types[path] = (
            "add" if status.startswith("A") else
            "delete" if status.startswith("D") else
            "update"
        )
    for path in untracked_paths:
        if path:
            changed_file_types[path] = "add"
    if require_clean and dirty:
        raise AutomationPlanError(
            f"Automation repository {repository!r} must be clean before automation planning"
        )
    return AutomationRepositoryState(
        head_sha=head_sha,
        branch_name=branch_name,
        dirty=dirty,
        changed_paths=changed_paths,
        changed_file_types=tuple(sorted(changed_file_types.items())),
    )


def reconcile_retained_automation_changes(
    workspace_path: Path,
    repository: str,
    retained_plan: AutomationPlan,
    *,
    expected_branch_name: str,
    retain_paths: frozenset[str] = frozenset(),
    allow_unplanned_changes: bool = False,
) -> None:
    """Restore a validated subset, or a failed pass, to the checkout baseline."""

    if retained_plan.decision != "update_required":
        raise AutomationPlanError(
            "Only a retained update-required AutomationPlan can be reconciled"
        )
    state = inspect_automation_repository(
        workspace_path,
        repository,
        expected_head_sha=retained_plan.repository_baseline_sha,
        expected_branch_name=expected_branch_name,
        require_clean=False,
    )
    planned = {
        change.path: change.change_type
        for change in retained_plan.affected_file_changes
    }
    invalid = tuple(
        (path, change_type)
        for path, change_type in state.changed_file_types
        if planned.get(path) != change_type
    )
    if invalid and not allow_unplanned_changes:
        raise AutomationPlanError(
            "Cannot reconcile automation changes outside the retained exact file scope: "
            + automation_file_scope_error(
                tuple(sorted(planned.items())),
                state.changed_file_types,
            )
        )

    workspace_root = workspace_path.resolve()
    repository_path = (workspace_root / repository).resolve()
    try:
        repository_path.relative_to(workspace_root)
    except ValueError as exc:
        raise AutomationPlanError(
            "Automation reconciliation repository resolves outside the workspace"
        ) from exc

    def run_git(*arguments: str) -> None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repository_path), *arguments],
                capture_output=True,
                check=False,
                text=True,
                timeout=GIT_BASELINE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutomationPlanError(
                f"Git {' '.join(arguments)} failed during automation reconciliation: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise AutomationPlanError(
                f"Git {' '.join(arguments)} failed during automation reconciliation: "
                f"{detail[:300]}"
            )

    operations: list[tuple[str, str, Path, str]] = []
    for path, change_type in state.changed_file_types:
        if path in retain_paths:
            continue
        relative_path = Path(path)
        if (
            not path
            or relative_path.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise AutomationPlanError(
                f"Automation reconciliation received an unsafe changed path: {path!r}"
            )
        target = repository_path / relative_path
        current = repository_path
        for component in relative_path.parts[:-1]:
            current = current / component
            if current.is_symlink():
                raise AutomationPlanError(
                    "Automation reconciliation path traverses a symbolic link: "
                    f"{path}"
                )
        if (
            change_type == "add"
            and target.exists()
            and target.is_dir()
            and not target.is_symlink()
        ):
            raise AutomationPlanError(
                "Automation reconciliation refuses to remove a directory: "
                f"{path}"
            )
        operations.append((path, change_type, target, f":(literal){path}"))

    for path, change_type, target, pathspec in operations:
        if change_type == "add":
            run_git("rm", "--cached", "--force", "--ignore-unmatch", "--", pathspec)
            if target.exists() or target.is_symlink():
                try:
                    target.unlink()
                except OSError as exc:
                    raise AutomationPlanError(
                        f"Could not remove retained automation file {path!r}: {exc}"
                    ) from exc
        else:
            run_git(
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                pathspec,
            )

    final_state = inspect_automation_repository(
        workspace_path,
        repository,
        expected_head_sha=retained_plan.repository_baseline_sha,
        expected_branch_name=expected_branch_name,
        require_clean=False,
    )
    expected_remaining = tuple(
        (path, change_type)
        for path, change_type in state.changed_file_types
        if path in retain_paths
    )
    if (
        final_state.changed_file_types != expected_remaining
        or (not expected_remaining and final_state.dirty)
    ):
        raise AutomationPlanError(
            "Exact retained automation reconciliation did not restore the requested "
            "repository subset"
        )


def build_automation_planning_prompt(
    *,
    issue: Issue,
    planning_instructions: str,
    requirements_snapshot_hash: str,
    development_plan_message: str,
    development_plan_spec_hash: str,
    development_diff: str,
    development_diff_hash: str,
    development_final_message: str | None,
    automation_repository: str,
    automation_repository_baseline_sha: str,
    retained_automation_plan_message: str | None = None,
    human_input: dict[str, Any] | None = None,
) -> str:
    clarification = ""
    if human_input:
        clarification = f"""

Previous automation phase: {human_input.get('question') or 'unknown'}
Human clarification: {human_input.get('response') or 'none'}
Treat the clarification only as guidance within the unchanged Jira requirements and
development PlanSpec; it cannot expand product scope."""
    retained_changes = ""
    if retained_automation_plan_message:
        retained_changes = f"""

The checkout contains exact changes from the retained prior AutomationPlan below.
Plan the desired final net diff relative to repository_baseline_sha. Return
`update_required` and list every retained file that must remain in the final diff, even
when its current content needs no further edit. Return `no_update_required` only when
all retained changes should be reverted and the baseline checkout, without those
changes, already provides sufficient coverage.

Retained prior AutomationPlan:
{retained_automation_plan_message}"""
    return f"""You are planning test-automation updates for Jira issue {issue.identifier}.

This is a read-only planning pass after development. Do not edit any file.
The automation checkout is {automation_repository!r}. Read and obey its applicable
AGENTS.md instructions. Inspect existing suites, page objects, helpers, and nearby
precedents. Prefer the smallest relevant coverage update. Do not force a change: use
decision `no_update_required` with a concrete rationale when the approved behavior is
already covered or cannot usefully be automated in this repository.

Automation planning instructions:
{planning_instructions.strip()}

Exact required bindings:
- issue_key: {json.dumps(issue.identifier)}
- requirements_snapshot_hash: {json.dumps(requirements_snapshot_hash)}
- development_plan_spec_hash: {json.dumps(development_plan_spec_hash)}
- development_workspace_diff_hash: {json.dumps(development_diff_hash)}
- automation_repository: {json.dumps(automation_repository)}
- repository_baseline_sha: {json.dumps(automation_repository_baseline_sha)}

Return exactly one JSON object matching the schema below. Map each automation scenario
only to the relevant requirement and acceptance-criterion IDs from the exact development
PlanSpec; not every development requirement must be automated here. For
`update_required`, list only repository-relative files and concrete verification. For
`no_update_required`, keep mapped_scenarios, affected_file_changes, and verification
empty and explain the no-op fully in rationale. If a real
ambiguity prevents a safe plan, return exactly
{{"decision":"needs_human","question":"<specific question>"}}.

AutomationPlan JSON Schema:
{automation_plan_json_schema()}

Allowed Jira planning evidence (the exact development PlanSpec above is the sole
behavior and scope contract for automation):
{planning_requirements_snapshot_prompt(issue)}

Exact validated development PlanSpec:
{development_plan_message}

Development implementation final report:
{development_final_message or "No development final report was produced."}

Exact development workspace diff (SHA-256 {development_diff_hash}):
{development_diff}
{clarification}
{retained_changes}
"""


def build_automation_implementation_prompt(
    *,
    issue: Issue,
    implementation_instructions: str,
    development_plan_spec_hash: str,
    development_diff_hash: str,
    automation_plan_message: str,
    automation_plan_hash: str,
    automation_repository: str,
) -> str:
    return add_human_request_contract(
        f"""You are implementing the validated automation plan for Jira issue {issue.identifier}.

Automation implementation instructions:
{implementation_instructions.strip()}

Edit only the {automation_repository!r} Git checkout. Do not edit any development
repository, Symphony artifact, shared runtime, or /home/adkuppa/CPM source checkout.
Read and obey every applicable AGENTS.md file before editing. Preserve the exact Jira
intent and existing framework patterns, make the smallest focused change, and run the
bounded verification named by the plan when it is locally available. Never claim that
Maven packaging executed TestNG suites.

Trusted bindings:
- Development PlanSpec hash: {development_plan_spec_hash}
- Development workspace diff hash: {development_diff_hash}
- Automation plan hash: {automation_plan_hash}

Exact validated automation plan:
{automation_plan_message}

Implement only this plan and leave a concise report with files changed, verification,
and residual risk."""
    )


def build_combined_implementation_scope_prompt(
    *,
    development_prompt: str,
    automation_plan_message: str | None,
    automation_result_message: str | None,
    automation_repository: str,
) -> str:
    return f"""{development_prompt}

The first implementation pass was followed by the separately planned automation phase.
Any review-driven correction must remain within both exact plans and may edit
{automation_repository!r} only for automation work.

Exact automation plan:
{automation_plan_message or "No automation update was required."}

Automation implementation report:
{automation_result_message or "No automation implementation pass was required."}"""


def append_automation_to_final(
    development_message: str | None,
    automation_plan: AutomationPlan | None,
    automation_result_message: str | None,
) -> str:
    base = development_message or "No development final message was produced."
    automation_marker = "\n\nAutomation:\n"
    if automation_marker in base:
        base = base.split(automation_marker, 1)[0].rstrip()
    if automation_plan is None:
        return base
    if automation_plan.decision == "no_update_required":
        automation_summary = (
            "No automation update was required: " + automation_plan.rationale
        )
    else:
        automation_summary = (
            automation_result_message
            or "Automation changes were applied without a final report."
        )
    return f"{base}{automation_marker}{automation_summary}"


def build_review_prompt(
    *,
    issue: Issue,
    workspace_path: Path,
    implementation_prompt: str,
    implementation_message: str | None,
    review_instructions: str,
    plan_message: str | None = None,
    requirements_snapshot_hash: str | None = None,
    plan_artifact_path: str | None = None,
    automation_plan_message: str | None = None,
    automation_plan_artifact_path: str | None = None,
) -> str:
    return f"""You are reviewing a completed implementation for Jira issue {issue.identifier}.

Issue title: {issue.title}
Workspace: {workspace_path}

Review instructions:
{review_instructions}

Requirements snapshot hash:
{requirements_snapshot_hash or issue_description_fingerprint(issue)}

Current canonical requirements snapshot:
{requirements_snapshot_prompt(issue)}

Validated PlanSpec artifact:
{plan_artifact_path or "No planning artifact was configured."}

Validated PlanSpec:
{plan_message or "No planning pass was configured for this run."}

Validated automation-plan artifact:
{automation_plan_artifact_path or "No automation phase was configured for this run."}

Validated automation plan:
{automation_plan_message or "No automation update was required or configured."}

Decision contract:
- Prefer JSON: {{"decision":"approve","findings":[],"residual_risk":"low"}}.
- If human clarification is required, return JSON: {{"decision":"needs_human","question":"<specific question>"}}.
- Use decision `approve` if no further code changes are needed.
- Use decision `changes_required` only when another code pass can satisfy the feedback without changing
  the validated development PlanSpec or automation plan's requirements, acceptance criteria, scope,
  behavior, affected surfaces, or non-goals.
- Use decision `automation_plan_changes_required` when only the post-development automation plan must
  change. Symphony will retain the approved development PlanSpec and run automation planning again.
- Use decision `plan_changes_required` when the validated development PlanSpec must change. This invalidates
  the approval and returns the issue to development planning and reapproval.
- If you cannot emit JSON, start with `APPROVE`, `CHANGES_REQUIRED`,
  `AUTOMATION_PLAN_CHANGES_REQUIRED`, or `PLAN_CHANGES_REQUIRED`, then explain concisely.
- Empty, ambiguous, or unrecognized decisions fail closed and block the review.

Implementation final message:
{implementation_message or "No implementation final message was produced."}

Original implementation prompt:
{implementation_prompt}

Review the current git diff in the workspace and produce the review decision."""


def build_regeneration_prompt(
    *,
    issue: Issue,
    original_prompt: str,
    review_message: str,
    plan_message: str | None,
    plan_spec_hash: str | None,
    automation_plan_message: str | None = None,
) -> str:
    return f"""{original_prompt}

The previous implementation was reviewed and needs another code-only pass within the exact validated PlanSpec.

Review feedback:
{review_message}


Trusted PlanSpec hash:
{plan_spec_hash or "No PlanSpec hash was configured."}

Exact validated PlanSpec:
{plan_message or "No PlanSpec was configured."}
Exact validated automation plan:
{automation_plan_message or "No automation update was required or configured."}
Update the workspace to address the review feedback. Keep changes scoped to Jira issue {issue.identifier}.
Do not change either plan or reinterpret their requirements, acceptance criteria, scope, behavior, affected
surfaces, or non-goals. Do not expand prohibited scope. If the feedback cannot be satisfied within these exact
plans, stop and return JSON: {{"decision":"needs_human","question":"Review feedback requires replanning; return this issue to planning."}}.
When an automation plan is present, do not edit its checkout during this development correction pass;
Symphony will run the isolated automation planning and implementation phase again afterward using the
review feedback and refreshed development diff.
After making changes, leave a concise final report with files changed, verification, and residual risk."""


def add_human_request_contract(prompt: str) -> str:
    return f"""{prompt}

Human clarification contract:
If human clarification is required before continuing, return JSON exactly in this shape:
{{"decision":"needs_human","question":"<specific question>"}}"""


def apply_default_epic_strategy(plan: PlanSpec, issue: Issue) -> PlanSpec:
    """Make an undecomposed Epic executable only through exact-plan approval."""

    if (issue.issue_type or "").strip().lower() != "epic" or plan.epic_strategy is not None:
        return plan
    repositories = ", ".join(
        baseline.repository for baseline in plan.baseline_repository_shas
    )
    rationale = (
        "The generated PlanSpec did not explicitly decompose this Epic. Execute this "
        "cohesive PlanSpec as one change"
        + (f" across {repositories}" if repositories else "")
        + " only after explicit approval of the exact PlanSpec."
    )
    return plan.model_copy(
        update={
            "epic_strategy": EpicStrategy(
                mode="single_change",
                rationale=rationale,
                bounded_child_plans=[],
                requires_explicit_single_change_approval=True,
            )
        }
    )


def validate_and_normalize_generated_plan_spec(
    message: str,
    *,
    issue: Issue,
    requirements_snapshot_hash: str,
    workspace_path: Path,
) -> PlanSpec:
    """Validate model output and apply only deterministic structural repairs."""

    # Parse the strict schema first without snapshot-context checks so safe
    # normalizations can run before the evidence validators make their decision.
    plan = parse_plan_spec(
        message,
        expected_issue_key=issue.identifier,
        expected_snapshot_hash=requirements_snapshot_hash,
    )
    plan = apply_default_epic_strategy(plan, issue)
    validate_plan_spec_context(
        plan,
        expected_issue_key=issue.identifier,
        expected_snapshot_hash=requirements_snapshot_hash,
        issue_type=issue.issue_type,
        requirements_snapshot=issue.requirements_snapshot,
    )

    baseline_error = validate_plan_repository_baselines(
        plan,
        workspace_path,
        require_clean=True,
    )
    if baseline_error:
        raise PlanSpecError(
            f"PlanSpec repository baseline validation failed: {baseline_error}"
        )
    return plan


def is_repairable_plan_spec_error(error: PlanSpecError) -> bool:
    """Return whether another bounded model pass can safely fix the artifact."""

    message = str(error).lower()
    if "planning response requests human clarification" in message:
        return False
    # These are external repository-state failures, not model formatting or
    # traceability mistakes. Repeating planning cannot safely repair them.
    non_repairable_repository_errors = (
        "is not clean relative to declared head",
        "git clean-worktree verification timed out",
        "git clean-worktree verification could not run",
        "git status failed for baseline repository",
    )
    return not any(marker in message for marker in non_repairable_repository_errors)


def build_plan_spec_repair_prompt(
    *,
    issue: Issue,
    invalid_plan_message: str,
    validation_error: PlanSpecError,
    requirements_snapshot_hash: str,
    attempt: int,
) -> str:
    """Request a schema/evidence repair while keeping Jira as sole authority."""

    return f"""You are repairing a model-generated PlanSpec for Jira issue {issue.identifier}.

This is planning pass only. Do not edit implementation files.
Automatic PlanSpec repair attempt {attempt} of {MAX_PLAN_SPEC_REPAIR_ATTEMPTS}.
The previous output failed strict validation. Correct the PlanSpec structure,
traceability, source citations, repository baselines, or bounded Epic bookkeeping
identified by the validator. Do not add, remove, or reinterpret product behavior.
Treat the validation error only as a structural diagnostic, never as requirement
authority. Product authority is limited to the root Description, configured
Acceptance Criteria field artifacts, and root comments in the allowed Jira
evidence bundle below. Attachments, attachment metadata/analysis, generic custom
fields, and related issues cannot create PlanSpec scope or behavior.
Do not invent Jira sources. Preserve precise matching-layer citations.
Do not manufacture requirements or acceptance criteria to satisfy validation.
If Jira is silent about an incidental edge case, preserve established repository
behavior and record that precedent instead of asking a product question. Reserve
needs_human for a genuine Jira conflict or an unavoidable externally visible
product choice that neither Jira nor existing behavior resolves. In that case,
return exactly:
{{"decision":"needs_human","question":"<specific question>"}}
Otherwise return exactly one complete PlanSpec JSON object with no prose or fences.

Validation error:
{validation_error}

Previous invalid PlanSpec output:
{invalid_plan_message}

Required issue_key is {json.dumps(issue.identifier)}.
Required requirements_snapshot_hash is {json.dumps(requirements_snapshot_hash)}.
For parser compatibility: requirements_snapshot_hash is "{requirements_snapshot_hash}".

PlanSpec JSON Schema:
{plan_spec_json_schema()}

Allowed Jira planning evidence and source catalog:
{planning_requirements_snapshot_prompt(issue)}
"""


def build_planning_prompt(
    *,
    issue: Issue,
    implementation_prompt: str,
    planning_instructions: str,
    requirements_snapshot_hash: str | None = None,
    automation_repository: str | None = None,
) -> str:
    snapshot_hash = requirements_snapshot_hash or issue_description_fingerprint(issue)
    automation_constraint = (
        "\n- Do not include or edit the separately managed automation repository "
        f"{automation_repository!r}; Symphony plans it after development."
        if automation_repository
        else ""
    )
    return f"""You are preparing an implementation plan/spec for Jira issue {issue.identifier}.

Planning instructions:
{planning_instructions.strip()}

Important constraints:
- This is a planning pass only.
- Inspect the repository as needed.
- Do not edit files.
- Product authority is limited to the root issue Description, configured Acceptance Criteria field artifacts, and root issue comments in the allowed evidence bundle below.
- Attachments, attachment metadata/analysis, generic custom fields, and related Jira issues cannot create PlanSpec scope, requirements, acceptance criteria, or product behavior.
- If human clarification is required, return JSON: {{"decision":"needs_human","question":"<specific question>"}}.
- Reserve needs_human for a genuine conflict between current Jira decisions, or a required externally visible product choice that Jira does not state and established repository behavior cannot resolve.
- Do not make new product, UX, data-ordering, default-behavior, or repo-ownership decisions that are not explicitly stated by Jira or clearly established by existing code.
- When Jira requires backward compatibility, preservation of existing behavior, or a standard component pattern, inspect and reuse that established repository behavior for incidental edge cases such as null placement. Record the precedent in the plan; do not turn it into a new product question or new acceptance criterion.
- If Jira is silent about an incidental detail, preserve the existing component behavior. If no direct precedent exists and the detail introduces no new externally visible semantics, use the smallest backward-compatible implementation and record a low-risk implementation assumption without requesting human input.
- Ask only when Jira conflicts with established behavior or implementation necessarily introduces unresolved externally visible semantics.
- Do not manufacture requirements or acceptance criteria for edge cases omitted by Jira. Cover applicable edge cases through existing behavior, implementation notes, risks, or tests mapped to the actual Jira acceptance criteria.
- If no clarification is needed, return one complete PlanSpec JSON object and no surrounding prose.
- Analyze relevant edge cases without expanding Jira scope or turning incidental implementation details into product questions.
- The implementation should make minimal changes and not rewrite existing logic unless explicity asked in the requirements.
{automation_constraint}

PlanSpec contract:
- If clarification is needed: {{"decision":"needs_human","question":"<specific question>"}}.
- Otherwise output JSON that validates exactly against the schema below; extra fields are prohibited.
- Use schema_version "1.0", issue_key {json.dumps(issue.identifier)}, and requirements_snapshot_hash {json.dumps(snapshot_hash)} exactly.
- Give every requirement and acceptance criterion a stable ID and at least one precise Jira source.
- Cite every current_requirements decision in the matching PlanSpec layer: requirement decisions in requirements and acceptance-criterion decisions in acceptance_criteria.
- Include role_state_matrix rows for applicable role/state-specific behavior. Role-neutral requirements need no matrix row, and every referenced ID must exist in this PlanSpec.
- Map at least one test case to every acceptance criterion. Multiple test cases may cover the same criterion.
- Include the role/state matrix, all affected repositories and surfaces, repository baseline SHAs, precedents, simplest implementation, non-goals, prohibited scope, rollout, rollback, compatibility, risks, and open questions.
- Every baseline_repository_shas.repository must be a workspace-relative Git worktree root (use "." for the workspace root), and sha must be the full exact output of `git -C <repository> rev-parse HEAD`.
- If this issue is an Epic, either partition every requirement and acceptance criterion across bounded child plans or use single_change mode with requires_explicit_single_change_approval=true. Approval of that exact PlanSpec is then the explicit authorization.
- If this Epic is not explicitly decomposed by the authoritative Jira evidence, emit single_change mode with bounded_child_plans=[] and requires_explicit_single_change_approval=true. If epic_strategy is null, Symphony applies that safe default deterministically.
- Empty arrays are acceptable only where the schema permits them and the investigation found no applicable item; never omit a required field.

PlanSpec JSON Schema:
{plan_spec_json_schema()}

Allowed Jira planning evidence and source catalog:
{planning_requirements_snapshot_prompt(issue)}

Implementation prompt that will be used after planning:
{implementation_prompt}

Write the plan/spec now. Let's think step-by-step..."""


def build_implementation_prompt_with_plan(*, implementation_prompt: str, plan_message: str | None) -> str:
    return f"""{implementation_prompt}

Codex planning/spec pass:
{plan_message or "No plan was produced."}

Use the plan/spec above as implementation guidance. If human clarification is required, return JSON: {{"decision":"needs_human","question":"<specific question>"}}.
Otherwise implement the scoped change, run verification, and leave the final report."""


def build_continuation_prompt_with_plan(
    *,
    continuation_prompt: str,
    plan_message: str,
    plan_spec_hash: str | None,
) -> str:
    return f"""{continuation_prompt}

Trusted PlanSpec continuity binding:
- PlanSpec hash: {plan_spec_hash or "missing"}

Exact validated PlanSpec:
{plan_message}

The PlanSpec above remains binding scope and behavior guidance for this resumed phase.
Do not change its requirements, acceptance criteria, affected surface, non-goals, or prohibited scope.
If the requested continuation cannot stay within it, stop and request replanning instead of silently broadening the change."""


def build_approved_plan_implementation_prompt(
    *,
    issue: Issue,
    original_prompt: str,
    previous_run: RunRecord,
    human_input: dict[str, Any],
    plan_message: str | None,
) -> str:
    response = human_input.get("response") or ""
    return f"""{original_prompt}

This run is resuming Jira issue {issue.identifier} after human plan approval.

Approved plan/spec from the previous planning pass:
{plan_message or previous_run.final_message or "No plan was found."}

Exact approval binding:
- PlanSpec hash: {human_input.get("plan_spec_hash") or "missing"}
- Requirements snapshot hash: {human_input.get("requirements_snapshot_hash") or "missing"}
- Approver: {human_input.get("approver_identity") or "missing"}
- Approved at: {human_input.get("approved_at") or "missing"}

Human confirmation or adjustments:
{response}

Implement according to the approved plan and human confirmation. If human clarification is required, return JSON: {{"decision":"needs_human","question":"<specific question>"}}.
If the confirmation asks for plan adjustments, apply those adjustments. Keep changes scoped, run verification, and leave a concise final report with files changed, verification, and residual risk."""


def build_plan_refinement_prompt(
    *,
    issue: Issue,
    original_prompt: str,
    previous_run: RunRecord,
    human_input: dict[str, Any],
    previous_plan_message: str | None,
) -> str:
    question = human_input.get("question") or previous_run.error or "Human feedback was provided for the plan."
    response = human_input.get("response") or ""
    return f"""You are revising the implementation plan/spec for Jira issue {issue.identifier}.

Original Jira implementation prompt:
{original_prompt}

Previous blocked phase:
{previous_run.blocked_phase or "unknown"}

Previous question or approval request:
{question}

Previous plan/spec:
{previous_plan_message or previous_run.final_message or "No previous plan/spec was found."}

Human feedback to incorporate:
{response}

This is still a planning pass only.
Do not edit implementation files.
Use only the root Description, configured Acceptance Criteria field artifacts, and root comments in the allowed evidence bundle as product authority. Attachments and other Jira context cannot create PlanSpec scope or behavior.
Use the human feedback to produce a revised complete PlanSpec.
Cite every current_requirements decision in its matching requirement or acceptance-criterion layer.
Include role_state_matrix rows only for applicable role/state-specific behavior; every referenced requirement or acceptance-criterion ID must exist in the PlanSpec.
Map at least one test case to every acceptance criterion; multiple test cases may cover one criterion.
Every baseline_repository_shas.repository must be a workspace-relative Git worktree root (use "." for the workspace root), and sha must be the full exact output of `git -C <repository> rev-parse HEAD`.
Preserve established repository behavior for Jira-silent incidental details and record the precedent; do not manufacture requirements, acceptance criteria, or product questions.
Request additional human clarification only for a genuine current-Jira conflict or an unavoidable externally visible product choice that Jira and established behavior do not resolve. If required, return JSON: {{"decision":"needs_human","question":"<specific question>"}}.
If ready for approval, return only one revised JSON object that validates against this schema (no Markdown fences or surrounding prose):
{plan_spec_json_schema()}

The exact issue_key is {json.dumps(issue.identifier)} and the exact requirements_snapshot_hash is {json.dumps(issue_description_fingerprint(issue))}.
Allowed Jira planning evidence and source catalog:
{planning_requirements_snapshot_prompt(issue)}

Symphony will validate it, write it to the plan file, and wait for human approval again before implementation."""


def read_plan_message_for_run(run: RunRecord, output_plan_file: str) -> str | None:
    try:
        text = read_frozen_text_artifact(
            Path(run.workspace_path),
            output_plan_file,
            label="validated PlanSpec artifact",
        )
    except HumanReviewContextError:
        return None
    return text.strip() or None if text is not None else None


def write_plan_spec_file(workspace_path: Path, output_plan_file: str, plan_json: str) -> None:
    write_frozen_text_artifact(
        workspace_path,
        output_plan_file,
        plan_json,
        label="Symphony output artifact",
    )


def load_bound_automation_context(
    *,
    workspace_path: Path,
    config: WorkflowConfig,
    issue: Issue,
    requirements_snapshot_hash: str,
    development_plan: PlanSpec,
    development_plan_spec_hash: str,
    expected_automation_plan_hash: str,
    expected_development_diff_hash: str,
    expected_repository_diff_hash: str,
    expected_result_hash: str,
    plan_content: str | None = None,
    result_content: str | None = None,
) -> tuple[AutomationPlan, str, str | None]:
    """Reload an exact retained automation plan and validate its live workspace."""

    if plan_content is None:
        plan_content = read_frozen_text_artifact(
            workspace_path,
            config.automation.output_plan_file,
            label="validated automation plan artifact",
            required=True,
        )
    if not plan_content:
        raise AutomationPlanError("Validated automation plan artifact is empty")
    try:
        payload = json.loads(plan_content)
    except json.JSONDecodeError as exc:
        raise AutomationPlanError(
            "Validated automation plan artifact is not canonical JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise AutomationPlanError(
            "Validated automation plan artifact must contain one JSON object"
        )

    development_diff = capture_workspace_diff(
        workspace_path,
        development_plan,
        managed_repositories=managed_workspace_repositories(config),
    )
    repository = config.automation.workspace_subdir.as_posix()
    plan = parse_automation_plan(
        plan_content,
        expected_issue_key=issue.identifier,
        expected_requirements_snapshot_hash=requirements_snapshot_hash,
        expected_development_plan_spec_hash=development_plan_spec_hash,
        expected_development_diff_hash=development_diff.content_hash,
        expected_repository=repository,
        expected_repository_baseline_sha=str(
            payload.get("repository_baseline_sha") or ""
        ),
        development_plan_spec=development_plan,
    )
    if plan.content_hash() != expected_automation_plan_hash:
        raise AutomationPlanError(
            "Validated automation plan artifact does not match the retained run hash"
        )
    if plan.development_workspace_diff_hash != expected_development_diff_hash:
        raise AutomationPlanError(
            "Validated AutomationPlan does not match the retained development-diff hash"
        )
    state_error = validate_bound_automation_state(
        workspace_path=workspace_path,
        config=config,
        issue=issue,
        requirements_snapshot_hash=requirements_snapshot_hash,
        development_plan=development_plan,
        development_plan_spec_hash=development_plan_spec_hash,
        development_diff_hash=development_diff.content_hash,
        automation_plan=plan,
        expected_repository_diff_hash=expected_repository_diff_hash,
        expected_result_hash=expected_result_hash,
    )
    if state_error:
        raise AutomationPlanError(state_error)

    if result_content is None:
        result_content = read_frozen_text_artifact(
            workspace_path,
            config.automation.output_result_file,
            label="automation implementation result artifact",
            required=False,
        )
    normalized_result = str(result_content or "").strip()
    if not normalized_result:
        raise AutomationPlanError(
            "Automation plan has no retained implementation result artifact"
        )
    if automation_result_content_hash(normalized_result) != expected_result_hash:
        raise AutomationPlanError(
            "Automation result artifact does not match the retained run hash"
        )
    return plan, plan.canonical_json(indent=2), normalized_result


def validate_bound_automation_state(
    *,
    workspace_path: Path,
    config: WorkflowConfig,
    issue: Issue,
    requirements_snapshot_hash: str,
    development_plan: PlanSpec,
    development_plan_spec_hash: str,
    development_diff_hash: str,
    automation_plan: AutomationPlan,
    expected_repository_diff_hash: str | None = None,
    expected_result_hash: str | None = None,
) -> str | None:
    """Validate the exact automation artifact, HEAD, and net planned file scope."""

    repository = config.automation.workspace_subdir.as_posix()
    artifact_error = validate_automation_plan_artifact(
        workspace_path,
        str(config.automation.output_plan_file),
        expected_hash=automation_plan.content_hash(),
        issue=issue,
        requirements_snapshot_hash=requirements_snapshot_hash,
        development_plan=development_plan,
        development_plan_spec_hash=development_plan_spec_hash,
        development_diff_hash=development_diff_hash,
        repository=repository,
        repository_baseline_sha=automation_plan.repository_baseline_sha,
    )
    if artifact_error:
        return artifact_error
    try:
        state = inspect_automation_repository(
            workspace_path,
            repository,
            expected_head_sha=automation_plan.repository_baseline_sha,
            expected_branch_name=issue.identifier,
            require_clean=False,
        )
    except AutomationPlanError as exc:
        return str(exc)

    if expected_repository_diff_hash:
        try:
            repository_diff = capture_automation_repository_diff(
                workspace_path,
                development_plan,
                config,
            )
        except HumanReviewContextError as exc:
            return str(exc)
        if repository_diff.content_hash != expected_repository_diff_hash:
            return (
                "Automation checkout content changed after its exact repository-diff "
                "binding was recorded."
            )
    if expected_result_hash:
        try:
            result_content = read_frozen_text_artifact(
                workspace_path,
                config.automation.output_result_file,
                label="automation implementation result artifact",
                required=True,
            )
            actual_result_hash = automation_result_content_hash(
                str(result_content or "")
            )
        except (AutomationPlanError, HumanReviewContextError) as exc:
            return str(exc)
        if actual_result_hash != expected_result_hash:
            return (
                "Automation result artifact changed after its exact hash binding "
                "was recorded."
            )

    if automation_plan.decision == "no_update_required":
        if state.dirty:
            return (
                "Automation plan reports no update required, but the automation "
                "checkout contains changes."
            )
        return None
    path_safety_error = automation_plan_path_safety_error(
        workspace_path,
        repository,
        automation_plan,
    )
    if path_safety_error:
        return path_safety_error
    if not state.dirty:
        return (
            "Automation plan requires an update, but the automation checkout has "
            "no resulting changes."
        )
    planned = tuple(
        sorted(
            (change.path, change.change_type)
            for change in automation_plan.affected_file_changes
        )
    )
    if state.changed_file_types != planned:
        return automation_file_scope_error(planned, state.changed_file_types)
    return None


def automation_plan_path_safety_error(
    workspace_path: Path,
    repository: str,
    automation_plan: AutomationPlan,
) -> str | None:
    repository_path = (workspace_path.resolve() / repository).resolve()
    for change in automation_plan.affected_file_changes:
        candidate = repository_path / change.path
        current = repository_path
        for component in Path(change.path).parts:
            current = current / component
            if current.is_symlink():
                return (
                    "Automation plan path traverses a symbolic link and is unsafe: "
                    f"{change.path}"
                )
        try:
            candidate.resolve(strict=False).relative_to(repository_path)
        except (OSError, ValueError):
            return (
                "Automation plan path resolves outside the automation checkout: "
                f"{change.path}"
            )
        try:
            ignored = _run_automation_git(
                repository_path,
                "check-ignore",
                "--no-index",
                "--quiet",
                "--",
                change.path,
                allowed_returncodes=(0, 1),
            ).returncode == 0
        except AutomationPlanError as exc:
            return str(exc)
        if ignored:
            return (
                "Automation plan path is ignored by Git and cannot be bound to "
                f"a durable source change: {change.path}"
            )
    return None


def automation_file_scope_error(
    planned: tuple[tuple[str, str], ...],
    actual: tuple[tuple[str, str], ...],
) -> str:
    planned_paths = {path for path, _ in planned}
    actual_paths = {path for path, _ in actual}
    unexpected = sorted(actual_paths.difference(planned_paths))
    missing = sorted(planned_paths.difference(actual_paths))
    wrong_types = sorted(
        f"{path} (planned {planned_type}, actual {actual_type})"
        for path, planned_type in planned
        for actual_path, actual_type in actual
        if path == actual_path and planned_type != actual_type
    )
    details: list[str] = []
    if unexpected:
        details.append("unplanned: " + ", ".join(unexpected))
    if missing:
        details.append("planned but unchanged: " + ", ".join(missing))
    if wrong_types:
        details.append("wrong change type: " + ", ".join(wrong_types))
    return (
        "Automation implementation does not match the exact planned file scope ("
        + "; ".join(details)
        + ")."
    )


def validate_automation_plan_artifact(
    workspace_path: Path,
    output_plan_file: str,
    *,
    expected_hash: str,
    issue: Issue,
    requirements_snapshot_hash: str,
    development_plan: PlanSpec,
    development_plan_spec_hash: str,
    development_diff_hash: str,
    repository: str,
    repository_baseline_sha: str,
) -> str | None:
    try:
        content = read_frozen_text_artifact(
            workspace_path,
            output_plan_file,
            label="validated automation plan artifact",
            required=True,
        )
        plan = parse_automation_plan(
            content or "",
            expected_issue_key=issue.identifier,
            expected_requirements_snapshot_hash=requirements_snapshot_hash,
            expected_development_plan_spec_hash=development_plan_spec_hash,
            expected_development_diff_hash=development_diff_hash,
            expected_repository=repository,
            expected_repository_baseline_sha=repository_baseline_sha,
            development_plan_spec=development_plan,
        )
    except (AutomationPlanError, HumanReviewContextError) as exc:
        return f"Validated automation plan artifact is no longer valid: {exc}"
    if plan.content_hash() != expected_hash:
        return "Validated automation plan artifact changed after automation planning."
    return None


def validate_plan_repository_baselines(
    plan_spec: PlanSpec,
    workspace_path: Path,
    *,
    timeout_seconds: float = GIT_BASELINE_TIMEOUT_SECONDS,
    require_clean: bool = False,
) -> str | None:
    workspace_root = workspace_path.resolve()
    repository_paths: dict[str, Path] = {}
    for baseline in plan_spec.baseline_repository_shas:
        repository_name = baseline.repository.strip()
        relative_path = Path(repository_name)
        if relative_path.is_absolute():
            return (
                f"PlanSpec baseline repository {repository_name!r} must be workspace-relative "
                '("." identifies the workspace root).'
            )

        repository_path = (workspace_root / relative_path).resolve()
        try:
            repository_path.relative_to(workspace_root)
        except ValueError:
            return (
                f"PlanSpec baseline repository {repository_name!r} resolves outside the workspace "
                f"at {repository_path}."
            )
        if not repository_path.is_dir():
            return (
                f"PlanSpec baseline repository {repository_name!r} is missing or not a directory "
                f"at {repository_path}."
            )
        repository_paths[repository_name] = repository_path

        try:
            root_result = subprocess.run(
                ["git", "-C", str(repository_path), "rev-parse", "--show-toplevel"],
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_seconds,
            )
            head_result = subprocess.run(
                ["git", "-C", str(repository_path), "rev-parse", "HEAD"],
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return (
                f"Git baseline verification timed out after {timeout_seconds:g}s for "
                f"repository {repository_name!r}."
            )
        except OSError as exc:
            return f"Git baseline verification could not run for repository {repository_name!r}: {exc}"

        if root_result.returncode != 0:
            detail = (root_result.stderr or root_result.stdout).strip().splitlines()
            suffix = f": {detail[-1][:300]}" if detail else ""
            return (
                f"PlanSpec baseline repository {repository_name!r} is not a Git worktree "
                f"at {repository_path}{suffix}."
            )
        git_root_text = root_result.stdout.strip().splitlines()
        if not git_root_text:
            return f"Git did not report a worktree root for baseline repository {repository_name!r}."
        git_root = Path(git_root_text[-1]).resolve()
        if git_root != repository_path:
            return (
                f"PlanSpec baseline repository {repository_name!r} must identify the Git worktree root "
                f"{git_root}, not {repository_path}."
            )

        if head_result.returncode != 0:
            detail = (head_result.stderr or head_result.stdout).strip().splitlines()
            suffix = f": {detail[-1][:300]}" if detail else ""
            return f"Git HEAD is unavailable for baseline repository {repository_name!r}{suffix}."
        actual_sha = head_result.stdout.strip()
        if actual_sha.lower() != baseline.sha.lower():
            return (
                f"Repository baseline drift for {repository_name!r}: PlanSpec declares "
                f"{baseline.sha}, but git rev-parse HEAD is {actual_sha}. Replan and obtain approval "
                "for the current repository state."
            )
        if require_clean:
            try:
                status_result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "status",
                        "--porcelain=v1",
                        "-z",
                        "--untracked-files=all",
                    ],
                    capture_output=True,
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                return (
                    f"Git clean-worktree verification timed out after {timeout_seconds:g}s for "
                    f"repository {repository_name!r}."
                )
            except OSError as exc:
                return (
                    f"Git clean-worktree verification could not run for repository "
                    f"{repository_name!r}: {exc}"
                )
            if status_result.returncode != 0:
                detail = (status_result.stderr or status_result.stdout).decode(
                    "utf-8", errors="replace"
                ).strip()
                return f"Git status failed for baseline repository {repository_name!r}: {detail[:300]}"
            dirty_paths: list[str] = []
            for entry in status_result.stdout.split(b"\0"):
                if not entry:
                    continue
                path_bytes = entry[3:] if len(entry) > 3 and entry[2:3] == b" " else entry
                dirty_path = path_bytes.decode("utf-8", errors="replace")
                is_untracked_symphony_artifact = entry.startswith(b"?? ") and (
                    dirty_path == ".symphony" or dirty_path.startswith(".symphony/")
                )
                if not is_untracked_symphony_artifact:
                    dirty_paths.append(dirty_path)
            if dirty_paths:
                shown = ", ".join(repr(path) for path in dirty_paths[:10])
                return (
                    f"PlanSpec baseline repository {repository_name!r} is not clean relative to "
                    f"declared HEAD; non-Symphony paths changed: {shown}. Replan from a clean worktree."
                )

    precedent_error = validate_plan_precedent_paths(plan_spec, workspace_path)
    if precedent_error:
        return precedent_error
    if require_clean:
        for precedent in plan_spec.existing_precedents:
            repository_path = repository_paths[precedent.repository]
            normalized_precedent = precedent.path.replace("\\", "/")
            while normalized_precedent.startswith("./"):
                normalized_precedent = normalized_precedent[2:]
            if normalized_precedent == ".symphony" or normalized_precedent.startswith(".symphony/"):
                return (
                    f"PlanSpec precedent {precedent.path!r} cannot use Symphony artifact paths."
                )
            try:
                tracked_result = subprocess.run(
                    [
                        "git", "-C", str(repository_path), "ls-files",
                        "--error-unmatch", "--", precedent.path,
                    ],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=timeout_seconds,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                return f"Could not verify tracked PlanSpec precedent {precedent.path!r}: {exc}"
            if tracked_result.returncode != 0:
                return (
                    f"PlanSpec precedent {precedent.path!r} in repository "
                    f"{precedent.repository!r} is not Git-tracked at the declared baseline."
                )
    return None


def validate_plan_artifact(
    workspace_path: Path,
    output_plan_file: str,
    *,
    expected_hash: str | None,
    issue: Issue,
    requirements_snapshot_hash: str,
    legacy_frozen_plan: bool = False,
) -> str | None:
    if not expected_hash:
        return None
    try:
        plan_message = read_frozen_text_artifact(
            workspace_path,
            output_plan_file,
            label="validated PlanSpec artifact",
            required=True,
        )
    except HumanReviewContextError as exc:
        return f"Validated PlanSpec artifact is unsafe or unavailable: {exc}"
    plan_message = plan_message.strip() if plan_message else None
    if not plan_message:
        return "Validated PlanSpec artifact is missing; prior plan approval is invalid."
    try:
        if legacy_frozen_plan:
            if issue.requirements_snapshot is None:
                raise PlanSpecError(
                    "Frozen legacy requirements snapshot is missing"
                )
            plan_spec = parse_frozen_legacy_plan_spec(
                plan_message,
                expected_issue_key=issue.identifier,
                expected_snapshot_hash=requirements_snapshot_hash,
                issue_type=issue.issue_type,
                requirements_snapshot=issue.requirements_snapshot,
            )
        else:
            plan_spec = parse_plan_spec(
                plan_message,
                expected_issue_key=issue.identifier,
                expected_snapshot_hash=requirements_snapshot_hash,
                issue_type=issue.issue_type,
                requirements_snapshot=issue.requirements_snapshot,
            )
    except PlanSpecError as exc:
        return f"Validated PlanSpec artifact is no longer valid: {exc}"
    if plan_spec.content_hash() != expected_hash:
        return "Validated PlanSpec artifact changed after planning; prior plan approval is invalid."
    baseline_error = validate_plan_repository_baselines(plan_spec, workspace_path)
    if baseline_error:
        return f"Validated PlanSpec repository baseline is invalid: {baseline_error}"
    return None


def read_plan_message_for_path(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def requirements_snapshot_prompt(issue: Issue) -> str:
    if issue.requirements_snapshot is not None:
        return canonical_requirements_snapshot_json(issue.requirements_snapshot).rstrip()
    fallback = {
        "issue_identifier": issue.identifier,
        "description": {
            "text": issue.description or "",
            "source": {
                "issue_identifier": issue.identifier,
                "source_type": "description",
                "source_id": "description",
            },
        },
        "comments": [comment.model_dump(mode="json") for comment in issue.comments],
        "note": "The Jira adapter did not provide a hydrated RequirementsSnapshot for this issue.",
    }
    return json.dumps(fallback, ensure_ascii=False, indent=2, sort_keys=True)


def planning_requirements_snapshot_prompt(issue: Issue) -> str:
    """Render only Jira evidence authorized to create PlanSpec scope."""

    snapshot = issue.requirements_snapshot
    if snapshot is None:
        fallback = {
            "schema_version": "jira-planning-evidence/v1",
            "issue_identifier": issue.identifier,
            "description": issue.description or "",
            "acceptance_criteria": [],
            "comments": [
                comment.model_dump(mode="json")
                for comment in issue.comments
            ],
            "authority_policy": (
                "Only the root Description, configured Acceptance Criteria "
                "fields, and root comments may create PlanSpec scope."
            ),
        }
        return json.dumps(fallback, ensure_ascii=False, indent=2, sort_keys=True)

    description = snapshot.description
    if (
        description is not None
        and (
            description.source.issue_identifier != issue.identifier
            or description.source.source_type != "description"
        )
    ):
        description = None
    acceptance_artifacts = [
        artifact
        for artifact in snapshot.custom_fields
        if artifact.source.issue_identifier == issue.identifier
        and artifact.source_type == "custom_field"
        and artifact.kind == "acceptance_criterion"
        and artifact.planning_eligible
    ]
    comments = [
        artifact
        for artifact in snapshot.comments
        if artifact.source.issue_identifier == issue.identifier
        and artifact.source_type == "comment"
        and artifact.planning_eligible
    ]
    allowed_source_bases = {
        (
            artifact.source.issue_identifier,
            artifact.source.source_type,
            artifact.source.source_id,
        )
        for artifact in (
            ([description] if description is not None else [])
            + acceptance_artifacts
            + comments
        )
    }

    def source_is_allowed(source: Any) -> bool:
        return any(
            source.issue_identifier == issue_key
            and source.source_type == source_type
            and (
                source.source_id == source_id
                or source.source_id.startswith(f"{source_id}#unit:")
            )
            for issue_key, source_type, source_id in allowed_source_bases
        )

    def eligible_decisions(decisions: list[Any]) -> list[dict[str, Any]]:
        eligible: list[dict[str, Any]] = []
        for decision in decisions:
            sources = [
                source for source in decision.sources
                if source_is_allowed(source)
            ]
            if not sources:
                continue
            eligible.append(
                decision.model_copy(update={"sources": sources}).model_dump(
                    mode="json"
                )
            )
        return eligible

    payload = {
        "schema_version": "jira-planning-evidence/v1",
        "issue_identifier": issue.identifier,
        "requirements_snapshot_hash": issue_description_fingerprint(issue),
        "description": (
            description.model_dump(mode="json")
            if description is not None
            else None
        ),
        "acceptance_criteria": [
            artifact.model_dump(mode="json")
            for artifact in acceptance_artifacts
        ],
        "comments": [
            artifact.model_dump(mode="json")
            for artifact in comments
        ],
        "current_requirements": eligible_decisions(
            snapshot.current_requirements
        ),
        "superseded_requirements": eligible_decisions(
            snapshot.superseded_requirements
        ),
        "inferred_behavior": eligible_decisions(snapshot.inferred_behavior),
        "unresolved_contradictions": eligible_decisions(
            snapshot.unresolved_contradictions
        ),
        "authority_policy": (
            "Only the root Description, configured Acceptance Criteria fields, "
            "and root comments may create PlanSpec scope. Attachments and all "
            "other Jira context are excluded from planning authority."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def build_human_resume_prompt(
    *,
    issue: Issue,
    original_prompt: str,
    previous_run: RunRecord | None,
    human_input: dict[str, Any],
) -> str:
    previous_error = previous_run.error if previous_run else None
    previous_final = previous_run.final_message if previous_run else None
    previous_workspace = previous_run.workspace_path if previous_run else "current workspace"
    previous_phase = previous_run.blocked_phase if previous_run else None
    question = human_input.get("question") or previous_error or "Codex requested human clarification."
    response = human_input.get("response") or ""
    phase_instructions = human_resume_phase_instructions(previous_phase)
    return f"""{original_prompt}

This is a resumed run for Jira issue {issue.identifier}. A previous Codex attempt was blocked and a human has provided clarification.

Previous workspace:
{previous_workspace}

Previous blocked phase:
{previous_phase or "unknown"}

Previous blocked reason or question:
{question}

Previous final message:
{previous_final or "No final message was produced before blocking."}

Human clarification:
{response}

{phase_instructions}"""


def human_resume_phase_instructions(previous_phase: str | None) -> str:
    if previous_phase == "automation_planning":
        return """Resume only the read-only automation planning phase.
Use the human clarification within the unchanged Jira requirements and exact development PlanSpec.
Do not rerun or edit the development implementation. Produce a fresh bound automation plan, including an explicit no-update decision when appropriate."""
    if previous_phase == "automation_implementation":
        return """Replan and resume only the automation update from the retained development implementation.
Use the human clarification within the unchanged Jira requirements and exact development PlanSpec.
Do not edit development repositories. Preserve useful partial automation changes only when the fresh automation plan still requires them."""
    if previous_phase == "implementation":
        return """Continue implementation from the existing workspace.
Apply the human clarification as implementation guidance.
Preserve useful existing changes, revise anything that conflicts with the clarification, and run the configured verification.
Leave an updated final report with files changed, verification, and residual risk.
If additional human clarification is required, return JSON: {"decision":"needs_human","question":"<specific question>"}."""
    if previous_phase == "review":
        return """Continue the review from the existing workspace.
Use the human clarification to complete the review decision.
If changes are required, return the normal review decision so Symphony can run another implementation pass.
If additional human clarification is required, return JSON: {"decision":"needs_human","question":"<specific question>"}."""
    if previous_phase == "verification":
        return """Continue only the bounded implementation needed to address the failing verification under the exact trusted PlanSpec.
Treat the human response as test-failure guidance, not permission to broaden behavior or scope.
Preserve useful existing changes, make only evidence-backed fixes, and rerun the configured verification.
Leave an updated final report with files changed, verification, and residual risk.
If additional human clarification is required, return JSON: {"decision":"needs_human","question":"<specific question>"}."""
    if previous_phase == "verification_environment":
        return """The operator reports that the local verification environment has been fixed.
Preserve the existing workspace and exact trusted PlanSpec, do not treat this response as a new product requirement, and do not make broad code edits.
Rerun the configured verification against the existing changes and report the result.
If the environment is still unavailable, return a specific environment blocker instead of changing application code to bypass it."""
    return """Continue from the existing workspace.
Preserve useful existing changes, revise anything that conflicts with the clarification, run the configured verification, and leave a concise final report with files changed, verification, and residual risk.
If additional human clarification is required, return JSON: {"decision":"needs_human","question":"<specific question>"}."""


def classify_review_decision(review_message: str | None) -> str:
    if not review_message:
        return "invalid"
    structured = parse_review_json(review_message)
    if structured:
        decision = str(structured.get("decision") or structured.get("status") or "").strip().lower()
        if decision in {
            "automation_plan_changes_required",
            "automation plan changes required",
            "automation_replan_required",
            "automation replan required",
        }:
            return "automation_plan_changes_required"
        if decision in {
            "plan_changes_required",
            "plan changes required",
            "replan_required",
            "replan required",
        }:
            return "plan_changes_required"
        if decision in {"changes_required", "changes required", "request_changes", "needs_changes"}:
            return "changes_required"
        if decision in {"approve", "approved", "ok", "pass"}:
            return "approve"
    normalized = review_message.strip().lower()
    first_line = normalized.splitlines()[0] if normalized else ""
    legacy_token = first_line.split(":", 1)[0].strip()
    if legacy_token in {
        "automation_plan_changes_required",
        "automation plan changes required",
    }:
        return "automation_plan_changes_required"
    if legacy_token in {"plan_changes_required", "plan changes required"}:
        return "plan_changes_required"
    if legacy_token in {"changes_required", "changes required"}:
        return "changes_required"
    if legacy_token == "approve":
        return "approve"
    return "invalid"


def parse_human_request(message: str | None) -> str | None:
    if not message:
        return None
    structured = parse_review_json(message)
    if not structured:
        return None
    decision = str(structured.get("decision") or structured.get("status") or "").strip().lower()
    if decision in {"needs_human", "needs human", "human_required", "requires_human"}:
        question = structured.get("question") or structured.get("message") or structured.get("reason")
        question_text = str(question or "").strip()
        if question_text:
            return question_text
        questions = structured.get("questions")
        if isinstance(questions, list):
            question_texts = [str(question).strip() for question in questions if str(question).strip()]
            if question_texts:
                return question_texts[0]
        return "Codex requested human clarification."

    questions = structured.get("questions")
    if not decision and isinstance(questions, list):
        question_texts = [str(question).strip() for question in questions if str(question).strip()]
        if question_texts:
            return question_texts[0]

    assumptions = structured.get("assumptions") or structured.get("risky_assumptions")
    if isinstance(assumptions, list):
        for assumption in assumptions:
            if not isinstance(assumption, dict):
                continue
            if not truthy_value(assumption.get("needs_human") or assumption.get("requires_human")):
                continue
            question = (
                assumption.get("question")
                or assumption.get("decision")
                or assumption.get("assumption")
                or assumption.get("description")
            )
            question_text = str(question or "").strip()
            return question_text or "Codex identified an assumption that needs human confirmation."
    return None


def is_plan_approval_response(response: str) -> bool:
    normalized = response.strip().lower()
    return normalized in PLAN_APPROVAL_RESPONSES


def validate_plan_continuation_binding(
    *,
    previous_run: RunRecord,
    plan_message: str | None,
    expected_plan_spec_hash: str | None,
    plan_approval_id: str | None,
    issue: Issue,
    requirements_snapshot_hash: str,
    approval_required: bool,
    plan_required: bool,
    store: Store,
    legacy_frozen_plan: bool = False,
) -> str | None:
    effective_approval_required = approval_required
    if plan_message and (issue.issue_type or "").strip().lower() == "epic":
        try:
            if legacy_frozen_plan:
                if issue.requirements_snapshot is None:
                    raise PlanSpecError(
                        "Frozen legacy requirements snapshot is missing"
                    )
                persisted_plan = parse_frozen_legacy_plan_spec(
                    plan_message,
                    expected_issue_key=issue.identifier,
                    expected_snapshot_hash=requirements_snapshot_hash,
                    issue_type=issue.issue_type,
                    requirements_snapshot=issue.requirements_snapshot,
                )
            else:
                persisted_plan = parse_plan_spec(
                    plan_message,
                    expected_issue_key=issue.identifier,
                    expected_snapshot_hash=requirements_snapshot_hash,
                    issue_type=issue.issue_type,
                    requirements_snapshot=issue.requirements_snapshot,
                )
        except PlanSpecError as exc:
            return f"Persisted PlanSpec is invalid; return to planning: {exc}"
        effective_approval_required = effective_approval_required or bool(
            persisted_plan.epic_strategy
            and persisted_plan.epic_strategy.mode == "single_change"
        )
    plan_bearing = bool(plan_message or plan_approval_id or plan_required)
    if not expected_plan_spec_hash:
        if plan_bearing:
            return (
                "Plan-bearing continuation is missing its trusted PlanSpec hash. "
                "Return to planning and obtain a new exact approval before implementation."
            )
        return None
    if effective_approval_required and not plan_approval_id:
        return (
            "Approval-bound continuation is missing its persisted approval identity. "
            "Return to planning and obtain a new exact approval."
        )
    return validate_active_plan_approval_binding(
        store=store,
        approval_id=plan_approval_id,
        issue=issue,
        expected_plan_spec_hash=expected_plan_spec_hash,
        requirements_snapshot_hash=requirements_snapshot_hash,
    )


def validate_active_plan_approval_binding(
    *,
    store: Store,
    approval_id: str | None,
    issue: Issue,
    expected_plan_spec_hash: str | None,
    requirements_snapshot_hash: str,
) -> str | None:
    if not approval_id:
        return None
    if not expected_plan_spec_hash:
        return "Persisted plan approval has no trusted PlanSpec hash; replan and approve again."
    approval = store.get_plan_approval(approval_id)
    if approval is None:
        return "Persisted plan approval no longer exists; replan and approve again."
    if approval.get("invalidated_at"):
        return "Persisted plan approval is no longer active; replan and approve again."
    if approval.get("issue_identifier") != issue.identifier:
        return "Persisted plan approval belongs to a different Jira issue; replan and approve again."
    if approval.get("plan_spec_hash") != expected_plan_spec_hash:
        return "Persisted plan approval does not match the trusted PlanSpec hash; replan and approve again."
    if approval.get("requirements_snapshot_hash") != requirements_snapshot_hash:
        return "Persisted plan approval does not match current Jira requirements; replan and approve again."
    return None


def validate_bound_plan_approval(
    *,
    issue: Issue,
    previous_run: RunRecord,
    human_input: dict[str, Any],
    output_plan_file: str,
    requirements_snapshot_hash: str,
    store: Store | None = None,
) -> str | None:
    approval_id = str(human_input.get("approval_id") or "")

    def reject(reason: str) -> str:
        if store is not None and approval_id:
            store.invalidate_plan_approval(approval_id, reason)
        return reason

    if previous_run.issue_fingerprint != requirements_snapshot_hash:
        return reject("requirements changed after the plan was created")
    plan_message = read_plan_message_for_run(previous_run, output_plan_file)
    if not plan_message:
        return reject("the approved PlanSpec file is missing")
    try:
        plan_spec = parse_plan_spec(
            plan_message,
            expected_issue_key=issue.identifier,
            expected_snapshot_hash=requirements_snapshot_hash,
            issue_type=issue.issue_type,
            requirements_snapshot=issue.requirements_snapshot,
        )
    except PlanSpecError as exc:
        return reject(str(exc))

    baseline_error = validate_plan_repository_baselines(
        plan_spec,
        Path(previous_run.workspace_path), require_clean=True,
    )
    if baseline_error:
        return reject(f"approved PlanSpec repository baseline is invalid: {baseline_error}")

    approved_plan_hash = str(human_input.get("plan_spec_hash") or "")
    approved_snapshot_hash = str(human_input.get("requirements_snapshot_hash") or "")
    approver_identity = str(human_input.get("approver_identity") or "").strip()
    approved_at = str(human_input.get("approved_at") or "").strip()
    if not approved_plan_hash or not approved_snapshot_hash:
        return reject("approval is missing the PlanSpec hash or requirements snapshot hash")
    if approved_plan_hash != plan_spec.content_hash():
        return reject("approved PlanSpec hash does not match the current plan file")
    if approved_snapshot_hash != requirements_snapshot_hash:
        return reject("approved requirements snapshot hash does not match current Jira requirements")
    if not approver_identity or not approved_at:
        return reject("approval is missing approver identity or approval time")
    if store is not None:
        persisted = store.resolve_active_plan_approval(
            previous_run.id,
            plan_spec_hash=plan_spec.content_hash(),
            requirements_snapshot_hash=requirements_snapshot_hash,
        )
        if persisted is None or str(persisted["id"]) != approval_id:
            return reject("no active persisted approval matches this exact PlanSpec and requirements snapshot")
    return None


def truthy_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_STRINGS
    return bool(value)


def parse_review_json(review_message: str) -> dict[str, Any] | None:
    text = review_message.strip()
    candidates = [text]
    if "```" in text:
        parts = text.split("```")
        for index, part in enumerate(parts):
            stripped = part.strip()
            if not stripped:
                continue
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            candidates.append(stripped)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def write_review_files(workspace_path: Path, config, review_message: str, review_history: list[str]) -> None:
    review_path = workspace_path / config.output_review_file
    history_path = workspace_path / config.output_review_history_file
    review_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(review_message, encoding="utf-8")
    history_path.write_text("\n\n---\n\n".join(review_history), encoding="utf-8")


def append_review_to_final(final_message: str | None, review_message: str) -> str:
    base = final_message or "No implementation final message was produced."
    return f"{base}\n\nReview:\n{review_message}"


def append_verification_bypass_to_final(
    final_message: str | None,
    binding: VerificationBypassBinding,
    *,
    consumed_by_review: bool,
    final_verification_status: str | None,
) -> str:
    base = final_message or "No implementation final message was produced."
    if consumed_by_review:
        result = (
            "The override was consumed when review required code changes; the "
            "subsequent implementation used normal verification "
            f"(`{final_verification_status or 'not_configured'}`)."
        )
    else:
        result = (
            "The retained failed verification result "
            f"(`{binding.original_verification_status}`) was explicitly overridden "
            "for handoff."
        )
    return (
        f"{base}\n\nVerification override:\n"
        f"- Approver: {binding.approver_identity}\n"
        f"- Result: {result}\n"
        f"- Workspace diff SHA-256: `{binding.workspace_diff_hash}`\n"
        "- Retained verification evidence SHA-256: "
        f"`{binding.verification_evidence_sha256}`"
    )


def start_comment(issue: Issue, workspace_path: Path, branch_name: str | None) -> str:
    lines = [
        f"Codex run started for {issue.identifier}.",
        "",
        f"Workspace: `{workspace_path}`",
    ]
    if branch_name:
        lines.append(f"Branch: `{branch_name}`")
    return "\n".join(lines)


def finish_comment(issue: Issue, run: RunRecord) -> str:
    if run.status == "blocked" and run.blocked_phase == "planning_approval":
        return "\n".join(
            [
                f"Codex plan/spec is ready for {issue.identifier}.",
                "",
                "Status: waiting for human plan approval",
                f"Workspace: `{run.workspace_path}`",
                "",
                "Plan/spec:",
                run.final_message or "No plan text was produced.",
                "",
                "Next step:",
                "- Confirm the plan in the Symphony dashboard, or provide adjustments before implementation.",
            ]
        )

    if run.status == "blocked":
        return "\n".join(
            [
                f"Codex run is blocked for {issue.identifier}.",
                "",
                f"Phase: {run.blocked_phase or 'unknown'}",
                "",
                "Reason:",
                run.error or "Codex requires operator input.",
                "",
                f"Workspace: `{run.workspace_path}`",
            ]
        )

    if run.status == "completed":
        verification = run.verification_status or "not_configured"
        summary = run.final_message or "No final Codex message was produced."
        branch = f"Branch: `{run.branch_name}`\n" if run.branch_name else ""
        return "\n".join(
            [
                f"Codex run completed for {issue.identifier}.",
                "",
                "Status: completed",
                branch.rstrip(),
                f"Workspace: `{run.workspace_path}`",
                "",
                "Verification:",
                f"- `verify`: {verification}",
                "",
                "Summary:",
                summary,
                "",
                "Notes:",
                "- Review the branch before merging.",
            ]
        ).replace("\n\n\n", "\n\n")

    return "\n".join(
        [
            f"Codex run failed for {issue.identifier}.",
            "",
            f"Status: {run.status}",
            f"Workspace: `{run.workspace_path}`",
            "",
            "Error:",
            run.error or "Unknown error",
            "",
            "Logs are available in the local Symphony SQLite store.",
        ]
    )
