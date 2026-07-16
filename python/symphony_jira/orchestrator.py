from __future__ import annotations

import asyncio
import subprocess
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .codex_runner import CodexRunner, CodexRunResult
from .config import CodexConfig, WorkflowConfig
from .human_review import (
    HumanReviewContextError,
    build_human_review_implementation_prompt,
    build_human_review_triage_prompt,
    capture_workspace_diff,
    classify_human_review_triage,
    issue_from_frozen_snapshot,
    read_frozen_text_artifact,
    read_only_codex_config,
    validate_frozen_snapshot_artifacts,
)
from .logging import redact_text
from .models import Issue, RunRecord, diff_requirements_snapshots, issue_description_fingerprint, utc_now
from .plan_spec import (
    PlanSpec,
    PlanSpecError,
    parse_plan_spec,
    plan_spec_json_schema,
    validate_plan_precedent_paths,
)
from .requirements_artifacts import (
    canonical_requirements_snapshot_json,
    write_requirements_snapshot_artifacts,
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


class OrchestratorError(Exception):
    """Raised when an issue cannot be dispatched."""


class PlanningSafetyGateBlocked(Exception):
    """Stops a run before Codex when requirement evidence is not implementable."""


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
GIT_BASELINE_TIMEOUT_SECONDS = 5.0


TRUTHY_STRINGS = {"true", "yes", "y", "1", "required", "needs_human"}


class SingleIssueOrchestrator:
    def __init__(
        self,
        workflow: WorkflowDefinition,
        jira: JiraLike,
        store: Store,
        *,
        workspace_manager: WorkspaceManager | None = None,
        codex_runner: CodexRunner | None = None,
        secret_values: list[str | None] | None = None,
    ) -> None:
        self.workflow = workflow
        self.config = workflow.config
        self.jira = jira
        self.store = store
        self.workspace_manager = workspace_manager or WorkspaceManager(self.config.workspace, self.config.hooks)
        self.codex_runner = codex_runner or CodexRunner()
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
        issue = prepared_issue or await self.jira.get_issue(issue_key, include_comments=True)
        if issue.identifier != issue_key:
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
                or precreated_run.plan_approval_id != previous_run.plan_approval_id
            ):
                raise OrchestratorError(
                    "completed-review action does not match its source and result runs"
                )
        requirements_snapshot_hash = issue_description_fingerprint(issue)
        if not force and not completed_review:
            assert_issue_eligible(issue, self.config)

        prompt = render_prompt(self.workflow, issue)
        generation_prompt = prompt
        previous_phase = previous_run.blocked_phase if previous_run else None
        execution_continuation = (
            completed_review
            or (
                human_input is not None
                and previous_run is not None
                and previous_phase in {"implementation", "review"}
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
                    or (issue.issue_type or "").strip().lower() == "epic"
                    or contradiction_resolution_retry
                ),
                store=self.store,
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
        review_message: str | None = None
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

        try:
            if self.config.tracker.comment_on_start and not completed_review:
                await self._post_start_comment(issue, run.id, workspace_path, branch_name)

            safety_error = requirements_planning_safety_error(
                issue,
                require_attachment_analysis=self.config.tracker.requirements.require_attachment_analysis,
            )
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
                (issue.issue_type or "").strip().lower() == "epic"
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
                    try:
                        plan_spec = parse_plan_spec(
                            plan_message or "",
                            expected_issue_key=issue.identifier,
                            expected_snapshot_hash=requirements_snapshot_hash,
                            issue_type=issue.issue_type,
                            requirements_snapshot=issue.requirements_snapshot,
                        )
                        baseline_error = validate_plan_repository_baselines(
                            plan_spec, workspace.path, require_clean=True
                        )
                        if baseline_error:
                            raise PlanSpecError(
                                f"PlanSpec repository baseline validation failed: {baseline_error}"
                            )
                    except PlanSpecError as exc:
                        status = "blocked"
                        blocked_phase = "planning"
                        final_message = plan_message
                        error = str(exc)
                        run_implementation = False
                    else:
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
                        if triage_decision == "plan_changes_required":
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
            review_scope_prompt = generation_prompt
            generation_pass = 1
            while run_implementation:
                requirements_change = await self._requirements_checkpoint_error(
                    issue,
                    requirements_snapshot_hash,
                    checkpoint="implementation",
                    workspace_path=workspace.path,
                    frozen=completed_review,
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

                if (
                    completed_review
                    and classify_review_decision(final_message or "")
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
                )
                if binding_change:
                    status = "blocked"
                    error = binding_change
                    blocked_phase = "planning"
                    break

                implementation_human_request = parse_human_request(
                    final_message or codex_result.error
                )
                if implementation_human_request:
                    status = "blocked"
                    error = implementation_human_request
                    blocked_phase = "implementation"
                    break

                if self.config.hooks.verify:
                    verify = await self.workspace_manager.run_hook(
                        "verify",
                        self.config.hooks.verify,
                        workspace.path,
                        hook_context=self.hook_context(issue, workspace),
                    )
                    verification_status = "passed" if verify.succeeded else "failed"
                    verification_output_path = str(verify.log_path)
                    if not verify.succeeded:
                        self.store.add_log(
                            run.id,
                            "warning",
                            "Verification hook failed; continuing because verification is advisory.",
                            verification_output_path,
                        )
                else:
                    verification_status = "not_configured"

                binding_change = await self._execution_binding_error(
                    issue,
                    requirements_snapshot_hash,
                    checkpoint="finalization after verification",
                    workspace_path=workspace.path,
                    expected_plan_spec_hash=expected_plan_spec_hash,
                    active_plan_approval_id=active_plan_approval_id,
                    frozen_requirements=completed_review,
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

                review_prompt = build_review_prompt(
                    issue=issue,
                    workspace_path=workspace.path,
                    implementation_prompt=review_scope_prompt,
                    implementation_message=final_message,
                    review_instructions=self.config.codex.review_prompt,
                    plan_message=plan_message,
                    requirements_snapshot_hash=requirements_snapshot_hash,
                    plan_artifact_path=self.config.codex.output_plan_file if plan_message else None,
                )
                review_config = self.config.codex.model_copy(
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

                binding_change = await self._execution_binding_error(
                    issue,
                    requirements_snapshot_hash,
                    checkpoint="completion after review pass",
                    workspace_path=workspace.path,
                    expected_plan_spec_hash=expected_plan_spec_hash,
                    active_plan_approval_id=active_plan_approval_id,
                    frozen_requirements=completed_review,
                )
                if binding_change:
                    status = "blocked"
                    error = binding_change
                    blocked_phase = "planning"
                    break

                decision = classify_review_decision(review_message)
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
                        "approve, changes_required, plan_changes_required, or needs_human."
                    )
                    blocked_phase = "review"
                    break
                if decision == "changes_required":
                    generation_pass += 1
                    generation_prompt = build_regeneration_prompt(
                        issue=issue,
                        original_prompt=review_scope_prompt,
                        plan_message=plan_message,
                        plan_spec_hash=expected_plan_spec_hash,
                        review_message=review_message,
                    )
                    continue

                if review_message:
                    final_message = append_review_to_final(final_message, review_message)
                break

        except PlanningSafetyGateBlocked as exc:
            status = "blocked"
            error = self.redact(str(exc))
            blocked_phase = "planning"
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
            if workspace and self.config.hooks.after_run:
                after = await self._run_after_run_best_effort(issue, workspace)
                if after and not after.succeeded:
                    self.store.add_log(run.id, "warning", "after_run hook failed", str(after.log_path))

            updated = update_current_run(
                run.id,
                status=status,
                finished_at=utc_now(),
                final_message=final_message,
                error=error,
                blocked_phase=blocked_phase if status in {"blocked", "failed", "cancelled"} else None,
                verification_status=verification_status,
                verification_output_path=verification_output_path,
            )

            if self.config.tracker.comment_on_finish and not completed_review:
                await self._post_finish_comment(issue, updated)

            if (
                status == "completed"
                and self.config.tracker.handoff_status
                and not completed_review
            ):
                await self._transition_best_effort(issue, updated)

        stored_run = self.store.get_run(run.id)
        assert stored_run is not None
        return OnceResult(issue=issue, prompt=prompt, run=stored_run, workspace=workspace, dry_run=False)

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
    ) -> str | None:
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
    ) -> str | None:
        requirements_change = await self._requirements_checkpoint_error(
            issue,
            requirements_snapshot_hash,
            checkpoint=checkpoint,
            workspace_path=workspace_path,
            frozen=frozen_requirements,
        )
        if requirements_change:
            return requirements_change
        plan_change = validate_plan_artifact(
            workspace_path,
            self.config.codex.output_plan_file,
            expected_hash=expected_plan_spec_hash,
            issue=issue,
            requirements_snapshot_hash=requirements_snapshot_hash,
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

    async def _transition_best_effort(self, issue: Issue, run: RunRecord) -> None:
        target = self.config.tracker.handoff_status
        if not target:
            return
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


class PollingOrchestrator:
    def __init__(
        self,
        workflow: WorkflowDefinition,
        jira: JiraLike,
        store: Store,
        *,
        workspace_manager: WorkspaceManager | None = None,
        codex_runner: CodexRunner | None = None,
        secret_values: list[str | None] | None = None,
        search_limit: int = 50,
    ) -> None:
        self.workflow = workflow
        self.config = workflow.config
        self.jira = jira
        self.store = store
        self.workspace_manager = workspace_manager or WorkspaceManager(self.config.workspace, self.config.hooks)
        self.codex_runner = codex_runner
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
                        or source_run.plan_approval_id
                        != result_run.plan_approval_id
                        or result_run.attempt != source_run.attempt + 1
                        or str(action["requirements_snapshot_hash"])
                        != str(source_run.issue_fingerprint or "")
                        or action.get("plan_spec_hash")
                        != source_run.plan_spec_hash
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


def requirements_planning_safety_error(
    issue: Issue,
    *,
    require_attachment_analysis: bool,
) -> str | None:
    snapshot = issue.requirements_snapshot
    if snapshot is None:
        return (
            "Canonical Jira requirements snapshot is missing; planning and implementation are blocked. "
            "Refresh the Jira issue through the requirements adapter and retry. Human approval cannot "
            "waive the canonical evidence requirement."
        )

    if require_attachment_analysis:
        incomplete_attachments = [
            f"{attachment.filename} ({attachment.analysis.status})"
            for attachment in snapshot.attachments
            if attachment.analysis.status != "complete"
        ]
        if incomplete_attachments:
            return (
                "Required attachment analysis is incomplete; planning and implementation are blocked. "
                f"Incomplete attachments: {', '.join(incomplete_attachments)}. "
                "Configure or repair the attachment OCR/vision analyzer, refresh the Jira requirements "
                "snapshot, and retry. Human approval cannot waive this evidence gate."
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

    if snapshot.incomplete_reasons:
        incomplete_reasons = "; ".join(snapshot.incomplete_reasons)
        return (
            "Requirements snapshot is incomplete; planning and implementation are blocked. "
            f"Incomplete evidence: {incomplete_reasons}. "
            "Repair the Jira ingestion or analyzer source, refresh the versioned requirements "
            "snapshot, and retry. Human approval cannot waive this evidence gate."
        )
    return None


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

Decision contract:
- Prefer JSON: {{"decision":"approve","findings":[],"residual_risk":"low"}}.
- If human clarification is required, return JSON: {{"decision":"needs_human","question":"<specific question>"}}.
- Use decision `approve` if no further code changes are needed.
- Use decision `changes_required` only when another code pass can satisfy the feedback without changing
  the validated PlanSpec's requirements, acceptance criteria, scope, behavior, affected surfaces, or non-goals.
- Use decision `plan_changes_required` if the approved PlanSpec itself must change. This invalidates the
  approval and returns the issue to planning and reapproval; do not disguise a plan change as code feedback.
- If you cannot emit JSON, start with `APPROVE`, `CHANGES_REQUIRED`, or `PLAN_CHANGES_REQUIRED`, then explain concisely.
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
) -> str:
    return f"""{original_prompt}

The previous implementation was reviewed and needs another code-only pass within the exact validated PlanSpec.

Review feedback:
{review_message}


Trusted PlanSpec hash:
{plan_spec_hash or "No PlanSpec hash was configured."}

Exact validated PlanSpec:
{plan_message or "No PlanSpec was configured."}
Update the workspace to address the review feedback. Keep changes scoped to Jira issue {issue.identifier}.
Do not change the PlanSpec or reinterpret its requirements, acceptance criteria, scope, behavior, affected
surfaces, or non-goals. Do not expand prohibited scope. If the feedback cannot be satisfied within this exact
plan, stop and return JSON: {{"decision":"needs_human","question":"Review feedback requires replanning; return this issue to planning."}}.
After making changes, leave a concise final report with files changed, verification, and residual risk."""


def add_human_request_contract(prompt: str) -> str:
    return f"""{prompt}

Human clarification contract:
If human clarification is required before continuing, return JSON exactly in this shape:
{{"decision":"needs_human","question":"<specific question>"}}"""


def build_planning_prompt(
    *,
    issue: Issue,
    implementation_prompt: str,
    planning_instructions: str,
    requirements_snapshot_hash: str | None = None,
) -> str:
    snapshot_hash = requirements_snapshot_hash or issue_description_fingerprint(issue)
    return f"""You are preparing an implementation plan/spec for Jira issue {issue.identifier}.

Planning instructions:
{planning_instructions.strip()}

Important constraints:
- This is a planning pass only.
- Inspect the repository as needed.
- Do not edit files.
- If human clarification is required, return JSON: {{"decision":"needs_human","question":"<specific question>"}}.
- Do not make product, UX, data-ordering, default-behavior, or repo-ownership decisions that are not explicitly stated by Jira or clearly established by existing code.
- If multiple reasonable choices exist, ask for clarification instead of choosing silently.
- Treat these as common ambiguity triggers: column ordering, default sorting, default visibility, label text, button placement, report/table grouping, translation requirements, migration/backward compatibility, API behavior, and which repo owns the change.
- If no clarification is needed, return one complete PlanSpec JSON object and no surrounding prose.
- Reason and cover all the edge cases (including the ones JIRA requirements might be missing) and ask the human whenever there is a confusion.
- The implementation should make minimal changes and not rewrite existing logic unless explicity asked in the requirements.

PlanSpec contract:
- If clarification is needed: {{"decision":"needs_human","question":"<specific question>"}}.
- Otherwise output JSON that validates exactly against the schema below; extra fields are prohibited.
- Use schema_version "1.0", issue_key {json.dumps(issue.identifier)}, and requirements_snapshot_hash {json.dumps(snapshot_hash)} exactly.
- Give every requirement and acceptance criterion a stable ID and at least one precise Jira source.
- Cite every current_requirements decision in the matching PlanSpec layer: requirement decisions in requirements and acceptance-criterion decisions in acceptance_criteria.
- Ensure the role_state_matrix collectively references every planned requirement ID and every acceptance-criterion ID.
- Map exactly one test case to every acceptance criterion. Do not map a criterion twice and do not leave one untested.
- Include the role/state matrix, all affected repositories and surfaces, repository baseline SHAs, precedents, simplest implementation, non-goals, prohibited scope, rollout, rollback, compatibility, risks, and open questions.
- Every baseline_repository_shas.repository must be a workspace-relative Git worktree root (use "." for the workspace root), and sha must be the full exact output of `git -C <repository> rev-parse HEAD`.
- If this issue is an Epic, either partition every requirement and acceptance criterion across bounded child plans or use single_change mode with requires_explicit_single_change_approval=true. Approval of that exact PlanSpec is then the explicit authorization.
- Empty arrays are acceptable only where the schema permits them and the investigation found no applicable item; never omit a required field.

PlanSpec JSON Schema:
{plan_spec_json_schema()}

Canonical Jira requirements snapshot and source catalog:
{requirements_snapshot_prompt(issue)}

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
Use the human feedback to produce a revised complete PlanSpec.
Cite every current_requirements decision in its matching requirement or acceptance-criterion layer.
Ensure the role_state_matrix collectively references every planned requirement and acceptance-criterion ID.
Every baseline_repository_shas.repository must be a workspace-relative Git worktree root (use "." for the workspace root), and sha must be the full exact output of `git -C <repository> rev-parse HEAD`.
If additional human clarification is required, return JSON: {{"decision":"needs_human","question":"<specific question>"}}.
If ready for approval, return only one revised JSON object that validates against this schema (no Markdown fences or surrounding prose):
{plan_spec_json_schema()}

The exact issue_key is {json.dumps(issue.identifier)} and the exact requirements_snapshot_hash is {json.dumps(issue_description_fingerprint(issue))}.
Canonical Jira requirements snapshot and source catalog:
{requirements_snapshot_prompt(issue)}

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
    path = workspace_path / output_plan_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan_json, encoding="utf-8")


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
) -> str | None:
    effective_approval_required = approval_required
    if plan_message and (issue.issue_type or "").strip().lower() == "epic":
        try:
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
