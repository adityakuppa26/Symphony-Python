import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .automation_plan import (
    AutomationPlan,
    AutomationPlanError,
    automation_result_content_hash,
    parse_automation_plan,
)
from .human_review import (
    HumanReviewContextError,
    capture_workspace_diff,
    hash_verification_evidence,
    read_frozen_text_artifact,
    validate_frozen_snapshot_artifacts,
)
from .models import RequirementsSnapshot, RunRecord
from .orchestrator import (
    VERIFICATION_BYPASS_PHASES,
    capture_automation_repository_diff,
    inspect_automation_repository,
    managed_diff_repositories,
    managed_workspace_repositories,
    validate_plan_repository_baselines,
)
from .plan_spec import (
    PlanSpec,
    PlanSpecError,
    parse_frozen_legacy_plan_spec,
    parse_plan_spec,
)
from .store import Store, StoreIntegrityError, normalize_sha256
from .workflow import WorkflowDefinition

MAX_HUMAN_REVIEW_REQUEST_BYTES = 1024 * 1024
SUMMARY_ITEM_LIMIT = 3
SUMMARY_ITEM_MAX_CHARACTERS = 180
SUMMARY_GOAL_MAX_CHARACTERS = 240
SUMMARY_APPROACH_MAX_CHARACTERS = 360
SUMMARY_REPOSITORIES_MAX_CHARACTERS = 180
SUMMARY_AUTOMATION_RESULT_MAX_CHARACTERS = 360
PLAN_SUMMARY_UNAVAILABLE = (
    "Plan summary unavailable because the plan could not be validated for this run. "
    "Open the full plan file for details."
)
AUTOMATION_PLAN_SUMMARY_UNAVAILABLE = (
    "Automation plan summary unavailable because the artifact could not be "
    "validated for this run. "
    "Open the full automation plan file for details."
)
AUTOMATION_RESULT_SUMMARY_UNAVAILABLE = (
    "Automation result unavailable because the artifact is missing or could not "
    "be validated for this run. Human review is disabled."
)



def create_app(
    workflow: WorkflowDefinition,
    store: Store,
    *,
    orchestrator: Any | None = None,
    jira: Any | None = None,
):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import HTMLResponse, RedirectResponse
    except ImportError as exc:
        raise RuntimeError("FastAPI dashboard dependencies are not installed. Install with: pip install .[dashboard]") from exc

    app = FastAPI(title="Symphony Jira")

    @app.get("/api/v1/state")
    async def state() -> dict[str, Any]:
        return build_state(workflow, store, orchestrator_snapshot(orchestrator))

    @app.get("/api/v1/runs")
    async def runs(limit: int = 50) -> list[dict[str, Any]]:
        return [enrich_run(run, store, workflow) for run in store.list_runs(limit=limit)]

    @app.get("/api/v1/runs/{run_id}")
    async def run_detail(run_id: str) -> dict[str, Any]:
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        try:
            snapshot = (
                store.get_requirements_snapshot(
                    run.issue_identifier,
                    run.issue_fingerprint,
                )
                if run.issue_fingerprint
                else None
            )
        except StoreIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"stored requirements snapshot failed integrity validation: {exc}",
            ) from exc
        source_review_actions = store.list_human_review_actions_for_source_run(
            run.id
        )
        result_review_action = store.human_review_action_for_result_run(run.id)
        return {
            "run": enrich_run(run, store, workflow),
            "codex_events": [event.model_dump(mode="json") for event in store.list_codex_events(run_id)],
            "logs": store.list_logs(run_id=run_id),
            "jira_actions": store.list_jira_actions(run_id=run_id),
            "human_inputs": store.list_human_inputs(run_id=run_id),
            "requirements_snapshot": (
                snapshot.model_dump(mode="json") if snapshot else None
            ),
            "human_review_actions": [
                public_human_review_action(action)
                for action in source_review_actions
            ],
            "human_review_action": (
                public_human_review_action(result_review_action)
                if result_review_action
                else None
            ),
        }

    @app.post("/api/v1/runs/{run_id}/human-input")
    async def add_human_input(run_id: str, request: Request):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        if run.status != "blocked":
            raise HTTPException(status_code=409, detail="human input can only be added to blocked runs")
        if not store.is_latest_actionable_blocked_run(run.id):
            raise HTTPException(
                status_code=409,
                detail="this historical run is no longer the latest actionable blocked run",
            )
        body = await request.body()
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                raw_payload = await request.json()
            except (ValueError, UnicodeDecodeError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="request body must contain valid JSON",
                ) from exc
            if not isinstance(raw_payload, dict):
                raise HTTPException(status_code=400, detail="request body must be an object")
            payload = raw_payload
        else:
            form = parse_qs(body.decode(errors="replace"))
            payload = {key: values[0] if values else "" for key, values in form.items()}

        action = str(payload.get("action") or "").strip().lower()
        response = str(payload.get("response") or "").strip()
        approver_identity = str(payload.get("approver_identity") or "").strip()
        approval: dict[str, Any] | None = None
        if action == "approve":
            if run.blocked_phase != "planning_approval":
                raise HTTPException(status_code=409, detail="this run is not waiting for plan approval")
            if not approver_identity:
                raise HTTPException(status_code=400, detail="approver identity is required")
            requirements_snapshot_hash = str(run.issue_fingerprint or "").strip()
            if not requirements_snapshot_hash:
                raise HTTPException(
                    status_code=409,
                    detail="run has no requirements snapshot hash; regenerate the plan before approval",
                )
            try:
                plan_spec_hash = current_plan_spec_hash(run, workflow, store)
            except PlanSpecError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"the plan cannot be approved: {exc}",
                ) from exc
            try:
                record, approval = store.add_approved_human_input(
                    run.issue_identifier,
                    run_id=run.id,
                    question=run.error,
                    approver_identity=approver_identity,
                    plan_spec_hash=plan_spec_hash,
                    requirements_snapshot_hash=requirements_snapshot_hash,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        elif action == "approve_automation_plan":
            if run.blocked_phase != "automation_planning_approval":
                raise HTTPException(
                    status_code=409,
                    detail="this run is not waiting for automation plan approval",
                )
            if not (
                workflow.config.automation.enabled
                and workflow.config.automation.require_plan_approval
            ):
                raise HTTPException(
                    status_code=409,
                    detail="automation plan approval is not enabled for this workflow",
                )
            if not approver_identity:
                raise HTTPException(
                    status_code=400,
                    detail="approver identity is required",
                )
            try:
                binding = prepare_automation_plan_approval_context(
                    run,
                    workflow,
                    store,
                )
                record, approval = store.add_approved_automation_human_input(
                    run.issue_identifier,
                    run_id=run.id,
                    question=run.error,
                    approver_identity=approver_identity,
                    **binding,
                )
            except (
                HumanReviewContextError,
                PlanSpecError,
                StoreIntegrityError,
            ) as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"the automation plan cannot be approved: {exc}",
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        elif action == "bypass_verification":
            if (
                run.blocked_phase not in VERIFICATION_BYPASS_PHASES
                or run.verification_status in {None, "passed", "not_configured"}
            ):
                raise HTTPException(
                    status_code=409,
                    detail="this run is not blocked by a failed verification",
                )
            if not approver_identity:
                raise HTTPException(
                    status_code=400,
                    detail="approver identity is required",
                )
            try:
                binding = prepare_verification_bypass_context(
                    run,
                    workflow,
                    store,
                )
                record = store.add_verification_bypass_input(
                    run.issue_identifier,
                    run_id=run.id,
                    question=run.error,
                    approver_identity=approver_identity,
                    **binding,
                )
            except (HumanReviewContextError, PlanSpecError, StoreIntegrityError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"verification bypass context is not reusable: {exc}",
                ) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        elif not response:
            raise HTTPException(status_code=400, detail="response is required")
        else:
            try:
                record = store.add_human_input(
                    run.issue_identifier,
                    run_id=run.id,
                    question=run.error,
                    response=response,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if approval:
            record.update(
                {
                    key: approval[key]
                    for key in (
                        "approver_identity",
                        "approved_at",
                        "plan_spec_hash",
                        "requirements_snapshot_hash",
                        "automation_plan_hash",
                        "development_plan_spec_hash",
                        "development_plan_approval_id",
                        "development_workspace_diff_hash",
                        "automation_repository_diff_hash",
                    )
                    if key in approval
                }
            )
        if orchestrator is not None:
            await orchestrator.poll_once()
        if "text/html" in request.headers.get("accept", "") and "application/json" not in content_type:
            return RedirectResponse("/", status_code=303)
        return {"status": "ok", "human_input": record}

    @app.post("/api/v1/runs/{run_id}/human-review")
    async def address_human_review(run_id: str, request: Request):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        if run.status != "completed":
            raise HTTPException(
                status_code=409,
                detail="human review can only be addressed from completed runs",
            )
        if not store.is_latest_actionable_completed_run(run.id):
            raise HTTPException(
                status_code=409,
                detail="this completed run is no longer the latest actionable run",
            )

        body = await request.body()
        if len(body) > MAX_HUMAN_REVIEW_REQUEST_BYTES:
            raise HTTPException(
                status_code=413,
                detail="human review request is too large",
            )
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                raw_payload = await request.json()
            except (ValueError, UnicodeDecodeError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail="request body must contain valid JSON",
                ) from exc
            if not isinstance(raw_payload, dict):
                raise HTTPException(
                    status_code=400,
                    detail="request body must be an object",
                )
            payload = raw_payload
        else:
            form = parse_qs(body.decode(errors="replace"))
            payload = {
                key: values[0] if values else ""
                for key, values in form.items()
            }

        reviewer_value = payload.get("reviewer_identity")
        source_value = payload.get("source_url") or payload.get("source_link")
        comments_value = payload.get("comments")
        for field_name, field_value in (
            ("reviewer_identity", reviewer_value),
            ("source_url", source_value),
            ("comments", comments_value),
        ):
            if field_value is not None and not isinstance(field_value, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"{field_name} must be a string",
                )
        reviewer_identity = (reviewer_value or "").strip()
        source_url = (source_value or "").strip()
        comments = (comments_value or "").strip()
        if not reviewer_identity:
            raise HTTPException(
                status_code=400,
                detail="reviewer identity is required",
            )
        if not source_url:
            raise HTTPException(
                status_code=400,
                detail="review source/PR link is required",
            )
        parsed_source = urlparse(source_url)
        if parsed_source.scheme not in {"http", "https"} or not parsed_source.netloc:
            raise HTTPException(
                status_code=400,
                detail="review source/PR link must be an absolute HTTP(S) URL",
            )
        if not comments:
            raise HTTPException(
                status_code=400,
                detail="review comments are required",
            )

        try:
            context = prepare_human_review_context(run, workflow, store)
            action, result_run = store.create_human_review_action(
                run.id,
                reviewer_identity=reviewer_identity,
                source_url=source_url,
                comments=comments,
                **context,
            )
        except (HumanReviewContextError, PlanSpecError, StoreIntegrityError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"completed review context is not reusable: {exc}",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if orchestrator is not None:
            await orchestrator.poll_once()
            action = store.get_human_review_action(action["id"]) or action
            result_run = store.get_run(result_run.id) or result_run
        if (
            "text/html" in request.headers.get("accept", "")
            and "application/json" not in content_type
        ):
            return RedirectResponse("/", status_code=303)
        return {
            "status": action["status"],
            "human_review": summarize_human_review_action(action),
            "run": run_to_dict(result_run),
        }

    @app.get("/api/v1/issues/{issue_key}")
    async def issue_detail(issue_key: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "issue_key": issue_key,
            "runs": [run_to_dict(run) for run in store.list_runs_for_issue(issue_key)],
            "requirements_snapshot_versions": store.list_requirements_snapshot_versions(
                issue_key
            ),
        }
        if jira is not None:
            try:
                issue = await jira.get_issue(issue_key, include_comments=True)
                payload["issue"] = issue.model_dump(mode="json", exclude={"raw"})
            except Exception as exc:
                payload["jira_error"] = str(exc)
        return payload

    @app.post("/api/v1/refresh")
    async def refresh() -> dict[str, Any]:
        if orchestrator is None:
            return {"status": "skipped", "reason": "no polling orchestrator attached"}
        await orchestrator.poll_once()
        return {"status": "ok", "state": orchestrator.snapshot()}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return render_dashboard_html(build_state(workflow, store, orchestrator_snapshot(orchestrator)))

    return app


def build_state(
    workflow: WorkflowDefinition,
    store: Store,
    runtime: dict[str, Any] | None = None,
    *,
    recent_limit: int = 20,
) -> dict[str, Any]:
    runs = store.list_runs(limit=recent_limit)
    enriched = [enrich_run(run, store, workflow) for run in runs]
    automation_visible = workflow.config.automation.enabled or any(
        run.get("automation_plan_hash") for run in enriched
    )
    latest_run_ids_by_issue = latest_run_ids(enriched)
    running = [run for run in enriched if run["status"] == "running"]
    queued = [run for run in enriched if run["status"] == "queued"]
    blocked = [run for run in enriched if is_actionable_blocked_run(run, latest_run_ids_by_issue)]
    completed_or_failed = [run for run in enriched if run["status"] in {"completed", "failed", "cancelled"}]

    return {
        "workflow_path": str(workflow.path),
        "jira_jql": workflow.config.tracker.jql,
        "poll_interval_seconds": workflow.config.polling.interval_seconds,
        "workspace_root": str(workflow.config.workspace.root),
        "automation_enabled": automation_visible,
        "running_issues": running,
        "queued_issues": queued,
        "blocked_issues": blocked,
        "recent_runs": completed_or_failed,
        "all_runs": enriched,
        "runtime": runtime or {},
    }


def latest_run_ids(runs: list[dict[str, Any]]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for run in runs:
        issue_identifier = str(run.get("issue_identifier") or "")
        run_id = str(run.get("id") or "")
        if issue_identifier and run_id and issue_identifier not in latest:
            latest[issue_identifier] = run_id
    return latest


def is_actionable_blocked_run(run: dict[str, Any], latest_run_ids_by_issue: dict[str, str]) -> bool:
    if run.get("status") != "blocked":
        return False
    issue_identifier = str(run.get("issue_identifier") or "")
    if latest_run_ids_by_issue.get(issue_identifier) != run.get("id"):
        return False
    return not run.get("human_inputs")


def orchestrator_snapshot(orchestrator: Any | None) -> dict[str, Any] | None:
    if orchestrator is None:
        return None
    return orchestrator.snapshot()


def run_to_dict(run: RunRecord) -> dict[str, Any]:
    return run.model_dump(mode="json")


def public_human_review_action(action: dict[str, Any]) -> dict[str, Any]:
    """Return review audit data without its internal fencing credential."""

    return {
        key: value
        for key, value in action.items()
        if key != "claim_token"
    }


def summarize_human_review_action(
    action: dict[str, Any],
) -> dict[str, Any]:
    frozen_context_fields = {
        "approval",
        "automation_plan",
        "automation_result",
        "claim_token",
        "plan_spec",
        "source_final_message",
        "source_review",
        "source_review_history",
        "workspace_diff",
    }
    return {
        key: value
        for key, value in public_human_review_action(action).items()
        if key not in frozen_context_fields
    }


def current_plan_spec_hash(
    run: RunRecord,
    workflow: WorkflowDefinition,
    store: Store,
) -> str:
    plan_path = Path(run.workspace_path) / workflow.config.codex.output_plan_file
    try:
        plan_content = read_frozen_text_artifact(
            Path(run.workspace_path),
            workflow.config.codex.output_plan_file,
            label="validated PlanSpec artifact",
            required=True,
        )
    except HumanReviewContextError as exc:
        raise PlanSpecError(str(exc)) from exc
    if not plan_content:
        raise PlanSpecError(f"PlanSpec file is missing or empty: {plan_path}")
    snapshot_hash = str(run.issue_fingerprint or "").strip()
    if not snapshot_hash:
        raise PlanSpecError("run has no requirements snapshot hash")
    try:
        snapshot = store.get_requirements_snapshot(run.issue_identifier, snapshot_hash)
    except StoreIntegrityError as exc:
        raise PlanSpecError(
            f"stored requirements snapshot failed its integrity check: {exc}"
        ) from exc
    if snapshot is None:
        raise PlanSpecError(
            "the immutable requirements snapshot for this planning run is missing; "
            "regenerate the plan before approval"
        )
    plan_spec = parse_plan_spec(
        plan_content,
        expected_issue_key=run.issue_identifier,
        expected_snapshot_hash=snapshot_hash,
        requirements_snapshot=snapshot,
    )
    original_plan_content = str(run.final_message or "").strip()
    if not original_plan_content:
        raise PlanSpecError(
            "the validated PlanSpec produced by the planning run is missing; regenerate the plan"
        )
    original_plan_spec = parse_plan_spec(
        original_plan_content,
        expected_issue_key=run.issue_identifier,
        expected_snapshot_hash=snapshot_hash,
        requirements_snapshot=snapshot,
    )
    plan_hash = plan_spec.content_hash()
    if plan_hash != original_plan_spec.content_hash():
        raise PlanSpecError(
            "the PlanSpec file differs from the exact validated PlanSpec produced by planning; "
            "request adjustments and return to planning"
        )
    baseline_error = validate_plan_repository_baselines(
        plan_spec, Path(run.workspace_path), require_clean=True
    )
    if baseline_error:
        raise PlanSpecError(
            f"PlanSpec repository baseline validation failed: {baseline_error}"
        )
    return plan_hash


def prepare_verification_bypass_context(
    run: RunRecord,
    workflow: WorkflowDefinition,
    store: Store,
) -> dict[str, str]:
    """Bind an explicit override to the exact failed code and evidence state."""

    snapshot_hash = str(run.issue_fingerprint or "").strip()
    if not snapshot_hash:
        raise HumanReviewContextError(
            "failed verification run has no requirements snapshot hash"
        )
    snapshot = store.get_requirements_snapshot(
        run.issue_identifier,
        snapshot_hash,
    )
    if snapshot is None:
        raise HumanReviewContextError(
            "the immutable requirements snapshot for this run is missing"
        )
    workspace_path = Path(run.workspace_path)
    artifact_error = validate_frozen_snapshot_artifacts(
        workspace_path,
        snapshot_hash,
    )
    if artifact_error:
        raise HumanReviewContextError(artifact_error)
    plan_content = read_frozen_text_artifact(
        workspace_path,
        workflow.config.codex.output_plan_file,
        label="verification bypass PlanSpec",
        required=True,
    )
    if not plan_content:
        raise HumanReviewContextError(
            "verification bypass PlanSpec is missing or empty"
        )
    plan_spec = parse_dashboard_plan_spec(
        plan_content,
        run=run,
        requirements_snapshot=snapshot,
    )
    validate_dashboard_plan_binding(
        plan_spec,
        run=run,
        requirements_snapshot=snapshot,
    )
    baseline_error = validate_plan_repository_baselines(
        plan_spec,
        workspace_path,
        require_clean=False,
    )
    if baseline_error:
        raise HumanReviewContextError(
            f"verification bypass repository baseline is invalid: {baseline_error}"
        )
    try:
        persisted_diff_hash = normalize_sha256(
            str(run.verification_workspace_diff_hash or ""),
            "persisted verification workspace diff hash",
        )
        persisted_evidence_hash = normalize_sha256(
            str(run.verification_evidence_sha256 or ""),
            "persisted verification evidence SHA-256",
        )
    except ValueError as exc:
        raise HumanReviewContextError(
            "failed run has no valid verification-time integrity binding; "
            "rerun verification before requesting an override"
        ) from exc
    workspace_diff = capture_workspace_diff(
        workspace_path,
        plan_spec,
        managed_repositories=managed_diff_repositories(workflow.config),
    )
    evidence_hash = hash_verification_evidence(
        workspace_path,
        run.verification_output_path,
    )
    if workspace_diff.content_hash != persisted_diff_hash:
        raise HumanReviewContextError(
            "workspace changed after the failed verification; rerun verification "
            "before requesting an override"
        )
    if evidence_hash != persisted_evidence_hash:
        raise HumanReviewContextError(
            "verification evidence changed after the failed verification; rerun "
            "verification before requesting an override"
        )
    return {
        "workspace_diff_hash": persisted_diff_hash,
        "verification_evidence_sha256": persisted_evidence_hash,
    }


def prepare_bound_automation_context(
    run: RunRecord,
    workflow: WorkflowDefinition,
    development_plan: PlanSpec,
    *,
    require_result: bool,
    allow_partial_scope: bool = False,
    allow_repository_diff_drift: bool = False,
) -> dict[str, str | None]:
    """Read automation artifacts only when they match this run's durable binding."""

    raw_expected_hash = str(run.automation_plan_hash or "").strip()
    if not raw_expected_hash:
        return {
            "automation_plan_hash": None,
            "automation_plan": None,
            "automation_result": None,
        }
    try:
        expected_hash = normalize_sha256(
            raw_expected_hash,
            "completed run automation plan hash",
        )
    except ValueError as exc:
        raise HumanReviewContextError(
            "completed run has an invalid automation-plan hash"
        ) from exc
    try:
        expected_development_diff_hash = normalize_sha256(
            str(run.automation_development_diff_hash or ""),
            "completed run automation development-diff hash",
        )
        expected_repository_diff_hash = normalize_sha256(
            str(run.automation_repository_diff_hash or ""),
            "completed run automation repository-diff hash",
        )
    except ValueError as exc:
        raise HumanReviewContextError(
            "completed run has an invalid automation diff binding"
        ) from exc
    expected_result_hash: str | None = None
    if run.automation_result_hash:
        try:
            expected_result_hash = normalize_sha256(
                run.automation_result_hash,
                "completed run automation result hash",
            )
        except ValueError as exc:
            if require_result:
                raise HumanReviewContextError(
                    "completed run has an invalid automation-result hash"
                ) from exc
            expected_result_hash = None
    elif require_result:
        raise HumanReviewContextError(
            "completed run has no exact automation-result hash"
        )

    workspace_path = Path(run.workspace_path)
    plan_content = read_frozen_text_artifact(
        workspace_path,
        workflow.config.automation.output_plan_file,
        label="validated AutomationPlan artifact",
        required=True,
    )
    if not plan_content or not plan_content.strip():
        raise HumanReviewContextError(
            "completed run's validated AutomationPlan is missing or empty"
        )
    try:
        candidate = AutomationPlan.model_validate_json(plan_content)
        development_diff = capture_workspace_diff(
            workspace_path,
            development_plan,
            managed_repositories=managed_workspace_repositories(workflow.config),
        )
        repository_state = inspect_automation_repository(
            workspace_path,
            workflow.config.automation.workspace_subdir.as_posix(),
            expected_head_sha=candidate.repository_baseline_sha,
            expected_branch_name=run.issue_identifier,
            require_clean=False,
        )
        plan = parse_automation_plan(
            plan_content,
            expected_issue_key=run.issue_identifier,
            expected_requirements_snapshot_hash=str(run.issue_fingerprint or ""),
            expected_development_plan_spec_hash=development_plan.content_hash(),
            expected_development_diff_hash=development_diff.content_hash,
            expected_repository=(
                workflow.config.automation.workspace_subdir.as_posix()
            ),
            expected_repository_baseline_sha=candidate.repository_baseline_sha,
            development_plan_spec=development_plan,
        )
        repository_diff = capture_automation_repository_diff(
            workspace_path,
            development_plan,
            workflow.config,
        )
    except (AutomationPlanError, HumanReviewContextError, ValueError) as exc:
        raise HumanReviewContextError(
            f"completed run's validated AutomationPlan is not reusable: {exc}"
        ) from exc
    if plan.content_hash() != expected_hash:
        raise HumanReviewContextError(
            "validated AutomationPlan does not match the completed run's trusted hash"
        )
    if plan.development_workspace_diff_hash != expected_development_diff_hash:
        raise HumanReviewContextError(
            "validated AutomationPlan does not match the completed run's "
            "development-diff hash"
        )
    if (
        repository_diff.content_hash != expected_repository_diff_hash
        and not allow_repository_diff_drift
    ):
        raise HumanReviewContextError(
            "automation checkout does not match the completed run's exact "
            "repository-diff hash"
        )
    if plan.decision == "no_update_required":
        if repository_state.dirty:
            raise HumanReviewContextError(
                "validated no-op AutomationPlan has automation checkout changes"
            )
    else:
        planned_file_types = tuple(
            sorted(
                (change.path, change.change_type)
                for change in plan.affected_file_changes
            )
        )
        scope_matches = (
            all(
                dict(planned_file_types).get(path) == change_type
                for path, change_type in repository_state.changed_file_types
            )
            if allow_partial_scope
            else repository_state.changed_file_types == planned_file_types
        )
        if not scope_matches:
            raise HumanReviewContextError(
                "automation checkout changes do not match the validated "
                "AutomationPlan file scope"
            )

    normalized_result: str | None = None
    if expected_result_hash is not None:
        try:
            result_content = read_frozen_text_artifact(
                workspace_path,
                workflow.config.automation.output_result_file,
                label="automation result artifact",
                required=True,
            )
            normalized_result = (
                result_content.strip()
                if result_content is not None and result_content.strip()
                else None
            )
            if normalized_result is None:
                raise HumanReviewContextError(
                    "completed run's automation result is missing or empty"
                )
            if (
                automation_result_content_hash(normalized_result)
                != expected_result_hash
            ):
                raise HumanReviewContextError(
                    "automation result artifact does not match the completed run's "
                    "trusted hash"
                )
        except HumanReviewContextError:
            if require_result:
                raise
            normalized_result = None
    if require_result and normalized_result is None:
        raise HumanReviewContextError(
            "completed run's automation result is missing or empty"
        )
    return {
        "automation_plan_hash": expected_hash,
        "automation_plan": plan_content.strip(),
        "automation_result": normalized_result,
    }


def prepare_automation_plan_approval_context(
    run: RunRecord,
    workflow: WorkflowDefinition,
    store: Store,
) -> dict[str, str | None]:
    """Revalidate every frozen binding used by an automation-plan approval."""

    try:
        snapshot_hash = normalize_sha256(
            str(run.issue_fingerprint or ""),
            "requirements snapshot hash",
        )
        development_plan_hash = normalize_sha256(
            str(run.plan_spec_hash or ""),
            "development PlanSpec hash",
        )
        development_diff_hash = normalize_sha256(
            str(run.automation_development_diff_hash or ""),
            "development workspace diff hash",
        )
        repository_diff_hash = normalize_sha256(
            str(run.automation_repository_diff_hash or ""),
            "automation repository diff hash",
        )
    except ValueError as exc:
        raise HumanReviewContextError(str(exc)) from exc

    snapshot = store.get_requirements_snapshot(
        run.issue_identifier,
        snapshot_hash,
    )
    if snapshot is None:
        raise HumanReviewContextError(
            "the immutable requirements snapshot for this automation plan is missing"
        )
    artifact_error = validate_frozen_snapshot_artifacts(
        Path(run.workspace_path),
        snapshot_hash,
    )
    if artifact_error:
        raise HumanReviewContextError(artifact_error)

    development_plan_content = read_frozen_text_artifact(
        Path(run.workspace_path),
        workflow.config.codex.output_plan_file,
        label="automation approval development PlanSpec",
        required=True,
    )
    if not development_plan_content:
        raise HumanReviewContextError(
            "automation approval development PlanSpec is missing or empty"
        )
    development_plan = parse_dashboard_plan_spec(
        development_plan_content,
        run=run,
        requirements_snapshot=snapshot,
    )
    validate_dashboard_plan_binding(
        development_plan,
        run=run,
        requirements_snapshot=snapshot,
    )
    if development_plan.content_hash() != development_plan_hash:
        raise HumanReviewContextError(
            "development PlanSpec does not match the automation approval binding"
        )

    development_approval_id = str(run.plan_approval_id or "").strip() or None
    if workflow.config.codex.require_plan_approval and development_approval_id is None:
        raise HumanReviewContextError(
            "automation plan has no persisted development plan approval"
        )

    automation_context = prepare_bound_automation_context(
        run,
        workflow,
        development_plan,
        require_result=False,
        allow_partial_scope=True,
    )
    automation_plan_hash = automation_context.get("automation_plan_hash")
    if not automation_plan_hash or not automation_context.get("automation_plan"):
        raise HumanReviewContextError(
            "the exact validated AutomationPlan is missing"
        )

    return {
        "automation_plan_hash": automation_plan_hash,
        "requirements_snapshot_hash": snapshot_hash,
        "development_plan_spec_hash": development_plan_hash,
        "development_plan_approval_id": development_approval_id,
        "development_workspace_diff_hash": development_diff_hash,
        "automation_repository_diff_hash": repository_diff_hash,
    }


def prepare_human_review_context(
    run: RunRecord,
    workflow: WorkflowDefinition,
    store: Store,
) -> dict[str, Any]:
    snapshot_hash = str(run.issue_fingerprint or "").strip()
    if not snapshot_hash:
        raise HumanReviewContextError(
            "completed run has no requirements snapshot hash"
        )
    snapshot = store.get_requirements_snapshot(
        run.issue_identifier,
        snapshot_hash,
    )
    if snapshot is None:
        raise HumanReviewContextError(
            "the immutable requirements snapshot for this completed run is missing"
        )

    workspace_path = Path(run.workspace_path)
    artifact_error = validate_frozen_snapshot_artifacts(
        workspace_path,
        snapshot_hash,
    )
    if artifact_error:
        raise HumanReviewContextError(artifact_error)

    expected_plan_hash = str(run.plan_spec_hash or "").strip()
    if not expected_plan_hash:
        raise HumanReviewContextError(
            "completed run has no trusted PlanSpec hash"
        )
    plan_path = workspace_path / workflow.config.codex.output_plan_file
    plan_content = read_frozen_text_artifact(
        workspace_path,
        workflow.config.codex.output_plan_file,
        label="validated PlanSpec artifact",
        required=True,
    )
    if not plan_content:
        raise HumanReviewContextError(
            f"validated PlanSpec file is missing or empty: {plan_path}"
        )
    plan_spec = parse_plan_spec(
        plan_content,
        expected_issue_key=run.issue_identifier,
        expected_snapshot_hash=snapshot_hash,
        requirements_snapshot=snapshot,
    )
    if plan_spec.content_hash() != expected_plan_hash:
        raise HumanReviewContextError(
            "validated PlanSpec file does not match the completed run's trusted hash"
        )
    baseline_error = validate_plan_repository_baselines(
        plan_spec,
        workspace_path,
        require_clean=False,
    )
    if baseline_error:
        raise HumanReviewContextError(
            f"validated PlanSpec repository baseline is invalid: {baseline_error}"
        )

    approval: dict[str, Any] | None = None
    if run.plan_approval_id:
        approval = store.get_plan_approval(run.plan_approval_id)
        if approval is None:
            raise HumanReviewContextError(
                "completed run's exact plan approval is missing"
            )
        if approval.get("invalidated_at"):
            raise HumanReviewContextError(
                "completed run's exact plan approval is no longer active"
            )
        if approval.get("issue_identifier") != run.issue_identifier:
            raise HumanReviewContextError(
                "completed run's plan approval belongs to another Jira issue"
            )
        if approval.get("plan_spec_hash") != expected_plan_hash:
            raise HumanReviewContextError(
                "completed run's plan approval does not match its PlanSpec"
            )
        if approval.get("requirements_snapshot_hash") != snapshot_hash:
            raise HumanReviewContextError(
                "completed run's plan approval does not match its requirements snapshot"
            )
    elif workflow.config.codex.require_plan_approval:
        raise HumanReviewContextError(
            "completed run has no persisted plan approval"
        )

    review_path = workspace_path / workflow.config.codex.output_review_file
    review_history_path = (
        workspace_path / workflow.config.codex.output_review_history_file
    )
    source_review = read_frozen_text_artifact(
        workspace_path,
        workflow.config.codex.output_review_file,
        label="completed run review artifact",
        required=workflow.config.codex.review_after_run,
    )
    source_review_history = read_frozen_text_artifact(
        workspace_path,
        workflow.config.codex.output_review_history_file,
        label="completed run review-history artifact",
    )
    if workflow.config.codex.review_after_run and not source_review:
        raise HumanReviewContextError(
            f"completed run's review artifact is missing or empty: {review_path}"
        )

    workspace_diff = capture_workspace_diff(
        workspace_path,
        plan_spec,
        managed_repositories=managed_diff_repositories(workflow.config),
    )
    automation_context = prepare_bound_automation_context(
        run,
        workflow,
        plan_spec,
        require_result=bool(run.automation_plan_hash),
    )
    return {
        "plan_spec": plan_content,
        "approval": approval,
        "source_review": source_review,
        "source_review_history": source_review_history,
        "workspace_diff": workspace_diff.content,
        "workspace_diff_hash": workspace_diff.content_hash,
        **automation_context,
    }


def enrich_run(run: RunRecord, store: Store, workflow: WorkflowDefinition) -> dict[str, Any]:
    data = run_to_dict(run)
    events = store.list_codex_events(run.id)
    latest_event_type = events[-1].event_type if events else None
    current_phase = infer_phase(run, latest_event_type)
    human_inputs = store.list_human_inputs(run_id=run.id)
    plan_approvals = store.list_plan_approvals(run_id=run.id)
    active_plan_approval = store.latest_plan_approval_for_run(run.id, active_only=True)
    resolved_plan_approval = (
        store.get_plan_approval(run.plan_approval_id)
        if run.plan_approval_id
        else None
    )
    automation_plan_approvals = store.list_automation_plan_approvals(run_id=run.id)
    active_automation_plan_approval = (
        store.latest_automation_plan_approval_for_run(run.id, active_only=True)
    )
    resolved_automation_plan_approval = (
        store.get_automation_plan_approval(run.automation_plan_approval_id)
        if run.automation_plan_approval_id
        else None
    )
    source_review_actions = store.list_human_review_actions_for_source_run(run.id)
    result_review_action = store.human_review_action_for_result_run(run.id)
    plan_path = Path(run.workspace_path) / workflow.config.codex.output_plan_file
    plan_content = read_text_if_exists(plan_path)
    requirements_snapshot: RequirementsSnapshot | None = None
    requirements_summary_error: str | None = None
    snapshot_hash = str(run.issue_fingerprint or "").strip()
    if snapshot_hash:
        try:
            requirements_snapshot = store.get_requirements_snapshot(
                run.issue_identifier,
                snapshot_hash,
            )
        except StoreIntegrityError:
            requirements_summary_error = (
                "Requirements summary unavailable because the stored specification "
                "failed integrity validation."
            )
    requirements_path = requirements_artifact_path(run, snapshot_hash)
    requirements_exists = bool(
        requirements_path is not None and requirements_path.is_file()
    )
    review_path = Path(run.workspace_path) / workflow.config.codex.output_review_file
    review_history_path = (
        Path(run.workspace_path) / workflow.config.codex.output_review_history_file
    )
    automation_review_path = (
        Path(run.workspace_path) / workflow.config.automation.output_review_file
    )
    automation_review_history_path = (
        Path(run.workspace_path)
        / workflow.config.automation.output_review_history_file
    )
    automation_plan_path = (
        Path(run.workspace_path) / workflow.config.automation.output_plan_file
    )
    automation_result_path = (
        Path(run.workspace_path) / workflow.config.automation.output_result_file
    )
    automation_plan_content: str | None = None
    automation_result_content: str | None = None
    automation_plan_summary: str | None = None
    automation_binding_valid = False
    if run.automation_plan_hash:
        try:
            if not plan_content:
                raise HumanReviewContextError(
                    "the run's validated development PlanSpec is unavailable"
                )
            development_plan = parse_dashboard_plan_spec(
                plan_content,
                run=run,
                requirements_snapshot=requirements_snapshot,
            )
            validate_dashboard_plan_binding(
                development_plan,
                run=run,
                requirements_snapshot=requirements_snapshot,
            )
            automation_context = prepare_bound_automation_context(
                run,
                workflow,
                development_plan,
                require_result=False,
                allow_partial_scope=(
                    (
                        run.status == "blocked"
                        and run.blocked_phase
                        in {
                            "automation_planning",
                            "automation_planning_approval",
                            "automation_implementation",
                        }
                    )
                    or (
                        run.status == "running"
                        and current_phase
                        in {"Automation Planning", "Automation Implementation"}
                    )
                ),
                allow_repository_diff_drift=(
                    run.status == "running"
                    and current_phase == "Automation Implementation"
                ),
            )
            automation_plan_content = automation_context["automation_plan"]
            automation_result_content = automation_context["automation_result"]
            automation_plan_summary = summarize_automation_plan_content(
                automation_plan_content
            )
            automation_binding_valid = automation_plan_content is not None
        except (HumanReviewContextError, PlanSpecError, StoreIntegrityError):
            automation_plan_summary = AUTOMATION_PLAN_SUMMARY_UNAVAILABLE
    automation_review_context_valid = not run.automation_plan_hash or (
        automation_binding_valid and automation_result_content is not None
    )
    human_input_actionable = bool(
        run.status == "blocked"
        and not human_inputs
        and store.is_latest_actionable_blocked_run(run.id)
    )
    automation_plan_approval_enabled = bool(
        workflow.config.automation.enabled
        and workflow.config.automation.require_plan_approval
    )
    data.update(
        {
            "current_phase": current_phase,
            "workflow_progress": automation_workflow_progress(
                run,
                current_phase=current_phase,
                event_types=tuple(event.event_type for event in events),
                automation_enabled=(
                    workflow.config.automation.enabled
                    or bool(run.automation_plan_hash)
                    or bool(run.automation_development_diff_hash)
                ),
                development_approval_required=(
                    workflow.config.codex.require_plan_approval
                ),
                development_review_required=workflow.config.codex.review_after_run,
                automation_approval_required=(
                    workflow.config.automation.require_plan_approval
                ),
                automation_review_required=(
                    workflow.config.automation.review_after_run
                ),
            ),
            "elapsed_seconds": elapsed_seconds(run),
            "plan_path": str(plan_path),
            "plan_exists": plan_path.exists(),
            "plan_content": plan_content,
            "plan_summary": summarize_plan_content(
                plan_content,
                run=run,
                requirements_snapshot=requirements_snapshot,
            ),
            "requirements_path": (
                str(requirements_path) if requirements_path is not None else None
            ),
            "requirements_exists": requirements_exists,
            "requirements_summary": (
                requirements_summary_error
                or summarize_requirements_snapshot(requirements_snapshot)
            ),
            "review_path": str(review_path),
            "review_exists": review_path.exists(),
            "review_content": read_text_if_exists(review_path),
            "review_history_path": str(review_history_path),
            "review_history_exists": review_history_path.exists(),
            "review_history_content": read_text_if_exists(review_history_path),
            "development_review_path": str(review_path),
            "development_review_exists": review_path.exists(),
            "development_review_content": read_text_if_exists(review_path),
            "development_review_history_path": str(review_history_path),
            "development_review_history_exists": review_history_path.exists(),
            "development_review_history_content": read_text_if_exists(
                review_history_path
            ),
            "automation_review_path": str(automation_review_path),
            "automation_review_exists": automation_review_path.exists(),
            "automation_review_content": read_text_if_exists(
                automation_review_path
            ),
            "automation_review_history_path": str(
                automation_review_history_path
            ),
            "automation_review_history_exists": (
                automation_review_history_path.exists()
            ),
            "automation_review_history_content": read_text_if_exists(
                automation_review_history_path
            ),
            "automation_plan_path": str(automation_plan_path),
            "automation_plan_exists": automation_binding_valid,
            "automation_plan_content": automation_plan_content,
            "automation_plan_summary": automation_plan_summary,
            "automation_result_path": str(automation_result_path),
            "automation_result_exists": (
                automation_binding_valid and automation_result_content is not None
            ),
            "automation_result_content": automation_result_content,
            "automation_result_summary": summarize_automation_result_content(
                automation_result_content
            ),
            "human_inputs": human_inputs,
            "plan_approvals": plan_approvals,
            "active_plan_approval": active_plan_approval,
            "resolved_plan_approval": resolved_plan_approval,
            "automation_plan_approvals": automation_plan_approvals,
            "active_automation_plan_approval": active_automation_plan_approval,
            "resolved_automation_plan_approval": (
                resolved_automation_plan_approval
            ),
            "requirements_snapshot_hash": run.issue_fingerprint,
            "human_input_actionable": human_input_actionable,
            "human_input_pending": human_input_actionable,
            "human_input_submitted": any(item.get("consumed_at") is None for item in human_inputs),
            "automation_plan_approval_enabled": (
                automation_plan_approval_enabled
            ),
            "automation_plan_approval_actionable": bool(
                human_input_actionable
                and run.blocked_phase == "automation_planning_approval"
                and automation_plan_approval_enabled
                and automation_binding_valid
            ),
            "human_review_actions": [
                summarize_human_review_action(action)
                for action in source_review_actions
            ],
            "human_review_action": (
                summarize_human_review_action(result_review_action)
                if result_review_action
                else None
            ),
            "human_review_actionable": (
                automation_review_context_valid
                and store.is_latest_actionable_completed_run(run.id)
            ),
        }
    )
    return data


def infer_phase(run: RunRecord, latest_event_type: str | None) -> str:
    if run.status == "queued":
        return "queued"
    if run.status == "running":
        event_type = str(latest_event_type or "").strip().lower()
        if event_type.startswith("automation_review."):
            return "Automation Review"
        if event_type.startswith("automation_planning."):
            return "Automation Planning"
        if event_type.startswith("automation_implementation."):
            return "Automation Implementation"
        if event_type.startswith("development_review."):
            return "Development Review"
        if event_type.startswith("plan"):
            return "Development Planning"
        if event_type.startswith("development_implementation."):
            return "Development Implementation"
        if event_type.startswith(("review", "human_review.")):
            return (
                "Automation Review"
                if run.automation_result_hash
                else "Development Review"
            )
        if event_type:
            return "Development Implementation"
        return "setup"
    if run.status == "completed":
        return "completed"
    if run.status == "blocked":
        return {
            "planning": "Development Planning",
            "planning_approval": "Dev Approval",
            "implementation": "Development Implementation",
            "development_review": "Development Review",
            "automation_planning": "Automation Planning",
            "automation_planning_approval": "Automation Approval",
            "automation_implementation": "Automation Implementation",
            "automation_review": "Automation Review",
        }.get(str(run.blocked_phase or ""), "blocked")
    if run.status == "cancelled":
        return "cancelled"
    return "failed"


def automation_workflow_progress(
    run: RunRecord,
    *,
    current_phase: str,
    event_types: tuple[str, ...] = (),
    automation_enabled: bool,
    development_approval_required: bool = False,
    development_review_required: bool = False,
    automation_approval_required: bool = False,
    automation_review_required: bool = False,
) -> str:
    """Show durable progress through the development and automation gates."""

    if not automation_enabled:
        return ""

    active_stage = current_phase if run.status == "running" else None
    blocked_stage = {
        "planning": "Development Planning",
        "planning_approval": "Dev Approval",
        "implementation": "Development Implementation",
        "development_review": "Development Review",
        "automation_planning": "Automation Planning",
        "automation_planning_approval": "Automation Approval",
        "automation_implementation": "Automation Implementation",
        "automation_review": "Automation Review",
    }.get(str(run.blocked_phase or "")) if run.status == "blocked" else None

    normalized_event_types = tuple(
        str(event_type or "").strip().lower() for event_type in event_types
    )

    def event_seen(prefix: str) -> bool:
        return any(
            event_type.startswith(f"{prefix}.")
            for event_type in normalized_event_types
        )

    def event_completed(prefix: str) -> bool:
        return any(
            event_type.startswith(f"{prefix}.")
            and event_type.endswith(("turn.completed", "thread.completed"))
            for event_type in normalized_event_types
        )

    development_implementation_seen = event_seen("development_implementation")
    development_review_seen = event_seen("development_review")
    automation_planning_seen = event_seen("automation_planning")
    automation_implementation_seen = event_seen("automation_implementation")
    automation_review_seen = event_seen("automation_review")
    later_than_development = bool(
        run.automation_development_diff_hash
        or run.automation_plan_hash
        or run.automation_result_hash
        or automation_planning_seen
        or automation_implementation_seen
        or automation_review_seen
        or current_phase
        in {
            "Automation Planning",
            "Automation Approval",
            "Automation Implementation",
            "Automation Review",
        }
    )
    plan_complete = bool(run.plan_spec_hash) or later_than_development or (
        run.status == "completed"
    )
    development_approval_complete = bool(run.plan_approval_id) or bool(
        development_approval_required
        and (
            development_implementation_seen
            or development_review_seen
            or later_than_development
            or run.status == "completed"
        )
    )
    development_complete = bool(
        development_review_seen
        or later_than_development
        or run.status == "completed"
    )
    development_review_complete = bool(
        event_completed("development_review")
        or (
            development_review_required
            and (later_than_development or run.status == "completed")
        )
    )
    automation_plan_complete = bool(run.automation_plan_hash) or bool(
        run.automation_result_hash
    ) or automation_implementation_seen or automation_review_seen or (
        current_phase in {"Automation Approval", "Automation Implementation", "Automation Review"}
    )
    automation_approval_complete = bool(
        run.automation_plan_approval_id
        or (
            automation_approval_required
            and (
                automation_implementation_seen
                or automation_review_seen
                or run.automation_result_hash
            )
        )
    )
    automation_implementation_complete = bool(
        run.automation_result_hash or automation_review_seen
    )
    automation_review_complete = bool(
        event_completed("automation_review")
        or (
            automation_review_required
            and
            run.status == "completed"
            and bool(run.automation_result_hash)
        )
    )

    def stage(
        label: str,
        phase: str,
        complete: bool,
        *,
        required: bool = True,
    ) -> str:
        if active_stage == phase:
            status = "running"
        elif blocked_stage == phase:
            if not required and phase in {"Dev Approval", "Automation Approval"}:
                status = "not required"
            else:
                status = (
                    "awaiting approval"
                    if run.blocked_phase
                    in {"planning_approval", "automation_planning_approval"}
                    else "blocked"
                )
        elif complete:
            status = "done"
        elif not required:
            status = "not required"
        else:
            status = "pending"
        return f"{label}: {status}"

    return " → ".join(
        (
            stage("Development Planning", "Development Planning", plan_complete),
            stage(
                "Dev Approval",
                "Dev Approval",
                development_approval_complete,
                required=development_approval_required,
            ),
            stage(
                "Development Implementation",
                "Development Implementation",
                development_complete,
            ),
            stage(
                "Development Review",
                "Development Review",
                development_review_complete,
                required=development_review_required,
            ),
            stage(
                "Automation Planning",
                "Automation Planning",
                automation_plan_complete,
            ),
            stage(
                "Automation Approval",
                "Automation Approval",
                automation_approval_complete,
                required=automation_approval_required,
            ),
            stage(
                "Automation Implementation",
                "Automation Implementation",
                automation_implementation_complete,
            ),
            stage(
                "Automation Review",
                "Automation Review",
                automation_review_complete,
                required=automation_review_required,
            ),
        )
    )


def elapsed_seconds(run: RunRecord) -> float:
    end = run.finished_at or datetime.now(timezone.utc)
    start = run.started_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds())


def render_dashboard_html(state: dict[str, Any]) -> str:
    visible_runs = state["all_runs"][:20]
    automation_enabled = bool(state.get("automation_enabled"))
    rows = "\n".join(
        render_run_row(run, automation_enabled=automation_enabled)
        for run in visible_runs
    )
    automation_header = "<th>Automation</th>" if automation_enabled else ""
    running = ", ".join(run["issue_identifier"] for run in state["running_issues"]) or "none"
    queued = ", ".join(run["issue_identifier"] for run in state["queued_issues"]) or "none"
    blocked = ", ".join(run["issue_identifier"] for run in state["blocked_issues"]) or "none"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>Symphony Jira</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #111827; }}
    header {{ margin-bottom: 1.5rem; }}
    code {{ background: #f3f4f6; padding: 0.1rem 0.25rem; border-radius: 4px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
    th, td {{ text-align: left; border-bottom: 1px solid #e5e7eb; padding: 0.5rem; vertical-align: top; }}
    th {{ color: #374151; font-size: 0.875rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }}
    .panel {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem; }}
    .muted {{ color: #6b7280; }}
    pre {{ white-space: pre-wrap; max-height: 28rem; overflow: auto; }}
    details {{ max-width: 42rem; }}
    summary {{ cursor: pointer; color: #1f2937; font-weight: 600; }}
    .preview {{ color: #4b5563; margin-top: 0.35rem; }}
    .brief-summary {{ white-space: pre-line; line-height: 1.4; max-width: 34rem; }}
    .artifact-path {{ color: #6b7280; margin-top: 0.5rem; max-width: 34rem; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <header>
    <h1>Symphony Jira</h1>
    <div class="muted"><code>{escape(state["workflow_path"])}</code></div>
  </header>
  <div class="grid">
    <section class="panel"><strong>JQL</strong><br>{escape(state["jira_jql"])}</section>
    <section class="panel"><strong>Poll</strong><br>{state["poll_interval_seconds"]}s</section>
    <section class="panel"><strong>Workspace</strong><br><code>{escape(state["workspace_root"])}</code></section>
  </div>
  <div class="grid" style="margin-top: 1rem;">
    <section class="panel"><strong>Running</strong><br>{escape(running)}</section>
    <section class="panel"><strong>Queued</strong><br>{escape(queued)}</section>
    <section class="panel"><strong>Blocked</strong><br>{escape(blocked)}</section>
  </div>
  <h2>Recent Runs</h2>
  <table>
    <thead><tr><th>Issue</th><th>Status</th><th>Phase</th><th>Blocked Phase</th><th>Elapsed</th><th>Workspace</th><th>Verification</th><th>Requirements Spec</th><th>Plan</th>{automation_header}<th>Review</th><th>Error</th><th>Human Input</th><th>Final Message</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>"""


def render_run_row(
    run: dict[str, Any],
    *,
    automation_enabled: bool = False,
) -> str:
    final_message = display_final_message(run)
    automation_cell = (
        f"<td>{render_automation_cell(run)}</td>" if automation_enabled else ""
    )
    phase_progress = str(run.get("workflow_progress") or "")
    phase_cell = escape(run.get("current_phase"))
    if phase_progress:
        phase_cell += (
            f'<div class="muted phase-progress">{escape(phase_progress)}</div>'
        )
    return (
        "<tr>"
        f"<td>{escape(run.get('issue_identifier'))}</td>"
        f"<td>{escape(display_status(run))}</td>"
        f"<td>{phase_cell}</td>"
        f"<td>{escape(display_blocked_phase(run))}</td>"
        f"<td>{format_elapsed(run.get('elapsed_seconds'))}</td>"
        f"<td><code>{escape(run.get('workspace_path'))}</code></td>"
        f"<td>{escape(run.get('verification_status'))}</td>"
        f"<td>{render_requirements_cell(run)}</td>"
        f"<td>{render_plan_cell(run)}</td>"
        f"{automation_cell}"
        f"<td>{render_review_cell(run)}</td>"
        f"<td>{render_long_text_cell(display_error(run), 'Show full error')}</td>"
        f"<td>{render_human_input_cell(run)}</td>"
        f"<td>{render_long_text_cell(final_message, 'Show full final message')}</td>"
        "</tr>"
    )


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def format_elapsed(value: Any) -> str:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return ""
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def render_review_cell(run: dict[str, Any]) -> str:
    sections: list[str] = []
    for label, prefix in (
        ("Development Review", "development_review"),
        ("Automation Review", "automation_review"),
    ):
        artifacts: list[str] = []
        if run.get(f"{prefix}_exists"):
            content = str(run.get(f"{prefix}_content") or "")
            artifacts.append(
                "<div>Artifact: "
                f"<code>{escape(run.get(f'{prefix}_path'))}</code>"
                + (
                    render_long_text_cell(
                        content,
                        f"Show {label.lower()}",
                        force_details=True,
                    )
                    if content
                    else ""
                )
                + "</div>"
            )
        if run.get(f"{prefix}_history_exists"):
            history_content = str(
                run.get(f"{prefix}_history_content") or ""
            )
            artifacts.append(
                "<div>History: "
                f"<code>{escape(run.get(f'{prefix}_history_path'))}</code>"
                + (
                    render_long_text_cell(
                        history_content,
                        f"Show {label.lower()} history",
                        force_details=True,
                    )
                    if history_content
                    else ""
                )
                + "</div>"
            )
        if artifacts:
            sections.append(
                f"<div><strong>{escape(label)}</strong>{''.join(artifacts)}</div>"
            )
    return "".join(sections) or "none"


def render_plan_cell(run: dict[str, Any]) -> str:
    if not run.get("plan_exists"):
        return "none"
    summary = str(run.get("plan_summary") or "").strip() or (
        PLAN_SUMMARY_UNAVAILABLE
    )
    return render_brief_artifact(summary, run.get("plan_path"), "Full plan file")


def render_requirements_cell(run: dict[str, Any]) -> str:
    summary = str(run.get("requirements_summary") or "").strip()
    if not summary and not run.get("requirements_exists"):
        return "none"
    summary = summary or (
        "Requirements summary unavailable. Open the full requirements file for details."
    )
    label = (
        "Full requirements file"
        if run.get("requirements_exists")
        else "Expected requirements file (not present)"
    )
    return render_brief_artifact(
        summary,
        run.get("requirements_path"),
        label,
    )


def render_automation_cell(run: dict[str, Any]) -> str:
    artifacts: list[str] = []
    if run.get("automation_plan_exists"):
        plan_summary = str(run.get("automation_plan_summary") or "").strip()
        artifacts.append(
            "<div><strong>Plan</strong>"
            + render_brief_artifact(
                plan_summary or AUTOMATION_PLAN_SUMMARY_UNAVAILABLE,
                run.get("automation_plan_path"),
                "Automation plan file",
            )
            + "</div>"
        )
    elif run.get("automation_plan_hash"):
        artifacts.append(
            "<div><strong>Plan</strong>"
            + render_brief_artifact(
                str(run.get("automation_plan_summary") or "").strip()
                or AUTOMATION_PLAN_SUMMARY_UNAVAILABLE,
                None,
                "Automation plan file",
            )
            + "</div>"
        )
    if run.get("automation_result_exists"):
        result_summary = str(run.get("automation_result_summary") or "").strip()
        artifacts.append(
            "<div><strong>Result</strong>"
            + render_brief_artifact(
                result_summary or "Automation result is empty.",
                run.get("automation_result_path"),
                "Automation result file",
            )
            + "</div>"
        )
    elif (
        run.get("status") == "completed"
        and run.get("automation_plan_hash")
    ):
        artifacts.append(
            "<div><strong>Result</strong>"
            + render_brief_artifact(
                AUTOMATION_RESULT_SUMMARY_UNAVAILABLE,
                None,
                "Automation result file",
            )
            + "</div>"
        )
    return "".join(artifacts) or "none"


def render_brief_artifact(summary: str, path: Any, label: str) -> str:
    path_html = ""
    if path:
        path_html = (
            f'<div class="artifact-path">{escape(label)}:<br>'
            f"<code>{escape(path)}</code></div>"
        )
    return f'<div class="brief-summary">{escape(summary)}</div>{path_html}'


def render_long_text_cell(value: Any, summary: str, *, force_details: bool = False) -> str:
    text = str(value or "")
    if not text:
        return ""
    preview = text if len(text) <= 220 else f"{text[:220]}..."
    if len(text) <= 220 and not force_details:
        return f"<pre>{escape(text)}</pre>"
    return (
        f"<details><summary>{escape(summary)}</summary>"
        f"<div class=\"preview\">{escape(preview)}</div>"
        f"<pre>{escape(text)}</pre>"
        "</details>"
    )


def read_text_if_exists(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def requirements_artifact_path(
    run: RunRecord,
    snapshot_hash: str,
) -> Path | None:
    if not snapshot_hash:
        return None
    return (
        Path(run.workspace_path)
        / ".symphony"
        / "requirements-snapshots"
        / f"{snapshot_hash}.json"
    )


def summarize_automation_plan_content(content: str | None) -> str | None:
    if not content or not content.strip():
        return None
    try:
        plan = AutomationPlan.model_validate_json(content)
    except (TypeError, ValueError):
        return AUTOMATION_PLAN_SUMMARY_UNAVAILABLE

    blocking_questions = sum(
        question.blocks_implementation for question in plan.open_questions
    ) + sum(assumption.needs_human for assumption in plan.assumptions)
    rationale = compact_summary_text(
        plan.rationale,
        SUMMARY_APPROACH_MAX_CHARACTERS,
    )
    return "\n".join(
        (
            f"Decision: {str(plan.decision).replace('_', ' ')}.",
            f"Repository: {plan.automation_repository}.",
            f"Rationale: {rationale}",
            "Scope: "
            f"{counted_label(len(plan.mapped_scenarios), 'scenario')}, "
            f"{counted_label(len(plan.affected_file_changes), 'file change')}, "
            f"{counted_label(len(plan.verification), 'verification step')}.",
            f"Risks: {len(plan.risks)}. Blocking questions: "
            f"{'none' if blocking_questions == 0 else blocking_questions}.",
        )
    )


def summarize_automation_result_content(content: str | None) -> str | None:
    if not content or not content.strip():
        return None
    return compact_summary_text(content, SUMMARY_AUTOMATION_RESULT_MAX_CHARACTERS)


def summarize_plan_content(
    content: str | None,
    *,
    run: RunRecord,
    requirements_snapshot: RequirementsSnapshot | None,
) -> str | None:
    if not content or not content.strip():
        return None
    try:
        plan = parse_dashboard_plan_spec(
            content,
            run=run,
            requirements_snapshot=requirements_snapshot,
        )
        validate_dashboard_plan_binding(
            plan,
            run=run,
            requirements_snapshot=requirements_snapshot,
        )
    except PlanSpecError:
        return PLAN_SUMMARY_UNAVAILABLE

    acceptance_count = sum(
        len(requirement.acceptance_criteria)
        for requirement in plan.requirements
    )
    repository_names = compact_summary_text(
        ", ".join(plan.affected_surface.repositories),
        SUMMARY_REPOSITORIES_MAX_CHARACTERS,
    )
    blocking_questions = sum(
        question.blocks_implementation for question in plan.open_questions
    ) + sum(assumption.needs_human for assumption in plan.assumptions)
    goal = compact_summary_text(
        plan.requirements[0].statement,
        SUMMARY_GOAL_MAX_CHARACTERS,
    )
    approach = compact_summary_text(
        plan.simplest_implementation,
        SUMMARY_APPROACH_MAX_CHARACTERS,
    )
    question_summary = (
        "none" if blocking_questions == 0 else str(blocking_questions)
    )
    return "\n".join(
        (
            f"Goal: {goal}",
            f"Approach: {approach}",
            "Scope: "
            f"{counted_label(len(plan.requirements), 'requirement')}, "
            f"{counted_label(acceptance_count, 'acceptance criterion', 'acceptance criteria')}, "
            f"{counted_label(len(plan.test_cases), 'test')} across {repository_names}.",
            f"Risks: {len(plan.risks)}. Blocking questions: {question_summary}.",
        )
    )


def parse_dashboard_plan_spec(
    content: str,
    *,
    run: RunRecord,
    requirements_snapshot: RequirementsSnapshot | None,
) -> PlanSpec:
    snapshot_hash = str(run.issue_fingerprint or "").strip()
    if not snapshot_hash:
        raise PlanSpecError("run has no requirements snapshot hash")
    if requirements_snapshot is None:
        raise PlanSpecError("run's immutable requirements snapshot is unavailable")
    try:
        return parse_plan_spec(
            content,
            expected_issue_key=run.issue_identifier,
            expected_snapshot_hash=snapshot_hash,
            requirements_snapshot=requirements_snapshot,
        )
    except PlanSpecError:
        if requirements_snapshot.schema_version not in {
            "jira-requirements/v1",
            "jira-requirements/v2",
            "jira-requirements/v3",
        }:
            raise
        return parse_frozen_legacy_plan_spec(
            content,
            expected_issue_key=run.issue_identifier,
            expected_snapshot_hash=snapshot_hash,
            issue_type=None,
            requirements_snapshot=requirements_snapshot,
        )


def validate_dashboard_plan_binding(
    plan: PlanSpec,
    *,
    run: RunRecord,
    requirements_snapshot: RequirementsSnapshot | None,
) -> None:
    if requirements_snapshot is None:
        raise PlanSpecError("run's immutable requirements snapshot is unavailable")
    plan_hash = plan.content_hash()
    trusted_hash = str(run.plan_spec_hash or "").strip()
    trusted = False
    if trusted_hash:
        if plan_hash != trusted_hash:
            raise PlanSpecError("PlanSpec does not match the run's trusted plan hash")
        trusted = True

    original_content = str(run.final_message or "").strip()
    if run.blocked_phase == "planning_approval":
        if not original_content:
            raise PlanSpecError("planning run has no original validated PlanSpec")
        original_plan = parse_dashboard_plan_spec(
            original_content,
            run=run,
            requirements_snapshot=requirements_snapshot,
        )
        if plan_hash != original_plan.content_hash():
            raise PlanSpecError("PlanSpec file differs from the planning result")
        trusted = True
    elif not trusted and original_content:
        try:
            original_plan = parse_dashboard_plan_spec(
                original_content,
                run=run,
                requirements_snapshot=requirements_snapshot,
            )
        except PlanSpecError:
            pass
        else:
            if plan_hash == original_plan.content_hash():
                trusted = True

    if not trusted:
        raise PlanSpecError("PlanSpec has no trusted binding for this run")


def summarize_requirements_snapshot(
    snapshot: RequirementsSnapshot | None,
) -> str | None:
    if snapshot is None:
        return None
    requirements = [
        decision
        for decision in snapshot.current_requirements
        if decision.kind == "requirement"
    ]
    acceptance_criteria = [
        decision
        for decision in snapshot.current_requirements
        if decision.kind == "acceptance_criterion"
    ]
    lines = [
        f"{counted_label(len(requirements), 'requirement')} and "
        f"{counted_label(len(acceptance_criteria), 'acceptance criterion', 'acceptance criteria')}."
    ]
    for decision in requirements[:SUMMARY_ITEM_LIMIT]:
        lines.append(
            "- "
            + compact_summary_text(
                decision.text,
                SUMMARY_ITEM_MAX_CHARACTERS,
            )
        )
    remaining = len(requirements) - SUMMARY_ITEM_LIMIT
    if remaining > 0:
        lines.append(f"+{remaining} more requirements; open the full file.")

    sources = requirements_source_labels(snapshot)
    if sources:
        lines.append(f"Sources: {', '.join(sources)}.")
    completeness = (
        "complete"
        if not snapshot.incomplete_reasons
        else f"incomplete ({len(snapshot.incomplete_reasons)} issues)"
    )
    contradictions = len(snapshot.unresolved_contradictions)
    contradiction_summary = (
        "no unresolved contradictions"
        if contradictions == 0
        else f"{contradictions} unresolved contradictions"
    )
    lines.append(f"Status: {completeness}; {contradiction_summary}.")
    return "\n".join(lines)


def requirements_source_labels(snapshot: RequirementsSnapshot) -> list[str]:
    sources = [
        source
        for decision in snapshot.current_requirements
        for source in decision.sources
    ]
    source_types = {source.source_type for source in sources}
    labels_by_type = {
        "comment": "Comments",
        "attachment": "Attachments",
        "relation": "Related issues",
    }
    labels = ["Description"] if "description" in source_types else []
    custom_field_labels = sorted(
        {
            (source.field_name or "Custom fields").strip()
            for source in sources
            if source.source_type == "custom_field"
        },
        key=str.casefold,
    )
    labels.extend(custom_field_labels)
    labels.extend(
        labels_by_type[source_type]
        for source_type in ("comment", "attachment", "relation")
        if source_type in source_types
    )
    return labels


def compact_summary_text(value: Any, max_characters: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_characters:
        return text
    return text[: max_characters - 1].rstrip() + "…"


def counted_label(count: int, singular: str, plural: str | None = None) -> str:
    label = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {label}"


def display_blocked_phase(run: dict[str, Any]) -> str:
    phase = str(run.get("blocked_phase") or "")
    return {
        "planning": "Development Planning",
        "planning_approval": "Dev Approval",
        "implementation": "Development Implementation",
        "development_review": "Development Review",
        "automation_planning": "Automation Planning",
        "automation_planning_approval": "Automation Approval",
        "automation_implementation": "Automation Implementation",
        "automation_review": "Automation Review",
    }.get(phase, phase)


def display_status(run: dict[str, Any]) -> str:
    if run.get("status") == "blocked" and run.get("blocked_phase") == "planning_approval":
        return "plan completed"
    if (
        run.get("status") == "blocked"
        and run.get("blocked_phase") == "automation_planning_approval"
    ):
        return "automation plan completed"
    return str(run.get("status") or "")


def display_error(run: dict[str, Any]) -> str:
    if run.get("blocked_phase") in {
        "planning_approval",
        "automation_planning_approval",
    }:
        return ""
    return str(run.get("error") or "")


def display_final_message(run: dict[str, Any]) -> str:
    final_message = str(run.get("final_message") or "")
    plan_content = str(run.get("plan_content") or "")
    if run.get("blocked_phase") == "automation_planning_approval":
        if str(run.get("automation_plan_summary") or "").strip() in {
            "",
            AUTOMATION_PLAN_SUMMARY_UNAVAILABLE,
        }:
            return "Automation plan details could not be validated for this run."
        return (
            "Automation plan ready for approval. See the brief Automation summary."
        )
    if run.get("blocked_phase") == "planning_approval" or (
        final_message.strip()
        and plan_content.strip()
        and final_message.strip() == plan_content.strip()
    ):
        if str(run.get("plan_summary") or "").strip() in {
            "",
            PLAN_SUMMARY_UNAVAILABLE,
        }:
            return "Plan details could not be validated for this run."
        return "Plan ready for approval. See the brief Plan summary."
    return final_message


def render_human_input_cell(run: dict[str, Any]) -> str:
    inputs = run.get("human_inputs") or []
    review_actions = run.get("human_review_actions") or []
    review_lineage = render_human_review_lineage(
        run.get("human_review_action")
    )
    if run.get("status") == "completed":
        if review_actions:
            latest_review = review_actions[0]
            return review_lineage + (
                f"<strong>{escape(latest_review.get('status'))}</strong>"
                f"<div>Reviewer: {escape(latest_review.get('reviewer_identity'))}</div>"
                f"<div><a href=\"{escape(latest_review.get('source_url'))}\">"
                "Review source / PR</a></div>"
                f"<pre>{escape((latest_review.get('comments') or '')[:500])}</pre>"
                f"<div class=\"muted\">Result run: "
                f"<code>{escape(latest_review.get('result_run_id'))}</code></div>"
            )
        if not run.get("human_review_actionable"):
            return review_lineage or "none"
        action_url = (
            f"/api/v1/runs/{escape(run.get('id'))}/human-review"
        )
        return review_lineage + (
            "<details><summary>Address Human Review</summary>"
            f"<form method=\"post\" action=\"{action_url}\">"
            "<input name=\"reviewer_identity\" required "
            "placeholder=\"Reviewer identity\"><br>"
            "<input name=\"source_url\" type=\"url\" required "
            "placeholder=\"PR or review URL\"><br>"
            "<textarea name=\"comments\" required rows=\"6\" cols=\"42\" "
            "placeholder=\"Paste human review comments\"></textarea><br>"
            "<button type=\"submit\">Address Human Review</button>"
            "</form></details>"
        )
    if run.get("status") != "blocked":
        return review_lineage or "none"
    if inputs:
        latest = inputs[0]
        state = "queued for resume" if latest.get("consumed_at") is None else "consumed"
        approval_details = ""
        if latest.get("automation_plan_approval_id"):
            approval_details = (
                f"<div>Automation plan approved by "
                f"{escape(latest.get('approver_identity'))} "
                f"at {escape(latest.get('automation_approved_at'))}</div>"
                f"<div class=\"muted\">AutomationPlan: "
                f"<code>{escape(latest.get('automation_plan_hash'))}</code><br>"
                "Development PlanSpec: "
                f"<code>{escape(latest.get('development_plan_spec_hash'))}</code><br>"
                "Requirements snapshot: "
                f"<code>{escape(latest.get('automation_requirements_snapshot_hash'))}</code><br>"
                "Development diff: "
                f"<code>{escape(latest.get('development_workspace_diff_hash'))}</code><br>"
                "Automation repository diff: "
                f"<code>{escape(latest.get('automation_repository_diff_hash'))}</code></div>"
            )
        elif latest.get("approval_id"):
            approval_details = (
                f"<div>Approved by {escape(latest.get('approver_identity'))} "
                f"at {escape(latest.get('approved_at'))}</div>"
                f"<div class=\"muted\">PlanSpec: <code>{escape(latest.get('plan_spec_hash'))}</code><br>"
                "Requirements snapshot: "
                f"<code>{escape(latest.get('requirements_snapshot_hash'))}</code></div>"
            )
        elif latest.get("action") == "verification_bypass":
            approval_details = (
                f"<div>Test/runtime verification override approved by "
                f"{escape(latest.get('approver_identity'))}</div>"
                f"<div class=\"muted\">Original verification status: "
                f"<code>{escape(run.get('verification_status'))}</code><br>"
                "Original verification evidence: "
                f"<code>{escape(run.get('verification_output_path'))}</code><br>"
                "Workspace diff: "
                f"<code>{escape(latest.get('workspace_diff_hash'))}</code><br>"
                "Verification evidence hash: "
                f"<code>{escape(latest.get('verification_evidence_sha256'))}</code></div>"
            )
        return (
            review_lineage
            + f"<strong>{escape(state)}</strong>{approval_details}"
            f"<pre>{escape((latest.get('response') or '')[:500])}</pre>"
        )
    if not run.get("human_input_actionable"):
        return review_lineage or "none"
    action_url = f"/api/v1/runs/{escape(run.get('id'))}/human-input"
    if run.get("blocked_phase") == "planning_approval":
        snapshot_hash = escape(run.get("requirements_snapshot_hash"))
        return review_lineage + (
            "<div><strong>Approve the exact validated PlanSpec</strong>"
            f"<div class=\"muted\">Requirements snapshot: <code>{snapshot_hash}</code></div>"
            f"<form method=\"post\" action=\"{action_url}\">"
            "<input type=\"hidden\" name=\"action\" value=\"approve\">"
            "<input name=\"approver_identity\" required placeholder=\"Approver identity\">"
            "<button type=\"submit\">Approve Exact Plan</button>"
            "</form>"
            f"<form method=\"post\" action=\"{action_url}\">"
            "<input type=\"hidden\" name=\"action\" value=\"feedback\">"
            "<textarea name=\"response\" required rows=\"4\" cols=\"36\" "
            "placeholder=\"Describe requested adjustments\"></textarea><br>"
            "<button type=\"submit\">Request Adjustments</button>"
            "</form></div>"
        )
    if run.get("blocked_phase") == "automation_planning_approval":
        adjustment_form = (
            f"<form method=\"post\" action=\"{action_url}\">"
            "<input type=\"hidden\" name=\"action\" value=\"feedback\">"
            "<textarea name=\"response\" required rows=\"4\" cols=\"36\" "
            "placeholder=\"Describe requested automation-plan adjustments\"></textarea><br>"
            "<button type=\"submit\">Request Automation Plan Adjustments</button>"
            "</form>"
        )
        if not run.get("automation_plan_approval_actionable"):
            reason = (
                "The automation approval gate is disabled in the active workflow."
                if not run.get("automation_plan_approval_enabled")
                else "The exact AutomationPlan binding could not be validated."
            )
            return review_lineage + (
                "<div><strong>Automation plan approval unavailable</strong>"
                f"<div class=\"muted\">{escape(reason)} Request adjustments "
                "to return the run to automation planning.</div>"
                f"{adjustment_form}</div>"
            )
        return review_lineage + (
            "<div><strong>Approve the exact validated AutomationPlan</strong>"
            f"<div class=\"muted\">AutomationPlan: "
            f"<code>{escape(run.get('automation_plan_hash'))}</code><br>"
            "Requirements snapshot: "
            f"<code>{escape(run.get('requirements_snapshot_hash'))}</code><br>"
            "Development PlanSpec: "
            f"<code>{escape(run.get('plan_spec_hash'))}</code><br>"
            "Development diff: "
            f"<code>{escape(run.get('automation_development_diff_hash'))}</code><br>"
            "Automation repository diff: "
            f"<code>{escape(run.get('automation_repository_diff_hash'))}</code></div>"
            f"<form method=\"post\" action=\"{action_url}\">"
            "<input type=\"hidden\" name=\"action\" "
            "value=\"approve_automation_plan\">"
            "<input name=\"approver_identity\" required "
            "placeholder=\"Authenticated reviewer identity\">"
            "<button type=\"submit\">Approve Exact Automation Plan</button>"
            "</form>"
            f"{adjustment_form}</div>"
        )
    if (
        run.get("blocked_phase") in VERIFICATION_BYPASS_PHASES
        and run.get("verification_status") not in {None, "passed", "not_configured"}
    ):
        retry_form = (
            f"<form method=\"post\" action=\"{action_url}\">"
            "<input type=\"hidden\" name=\"action\" value=\"retry_verification\">"
            "<textarea name=\"response\" required rows=\"4\" cols=\"36\" "
            "placeholder=\"Describe what was fixed before retrying\"></textarea><br>"
            "<button type=\"submit\">Retry Verification</button>"
            "</form>"
        )
        if not (
            is_sha256(run.get("verification_workspace_diff_hash"))
            and is_sha256(run.get("verification_evidence_sha256"))
        ):
            return review_lineage + (
                "<div><strong>Required test/runtime verification did not pass</strong>"
                f"<div class=\"muted\">Original status: "
                f"<code>{escape(run.get('verification_status'))}</code><br>"
                "Evidence: "
                f"<code>{escape(run.get('verification_output_path'))}</code><br>"
                "This run predates verification-time "
                "integrity binding and cannot be safely bypassed. Retry Verification "
                "to establish the code and evidence binding first.</div>"
                f"{retry_form}</div>"
            )
        return review_lineage + (
            "<div><strong>Required test/runtime verification did not pass</strong>"
            f"<div class=\"muted\">Original status: "
            f"<code>{escape(run.get('verification_status'))}</code><br>"
            "Evidence: "
            f"<code>{escape(run.get('verification_output_path'))}</code><br>"
            "An explicit human approval records a test/runtime override and "
            "continues to the configured review. It does not mark verification "
            "as passed; the original status and evidence remain visible.</div>"
            f"<form method=\"post\" action=\"{action_url}\">"
            "<input type=\"hidden\" name=\"action\" value=\"bypass_verification\">"
            "<input name=\"approver_identity\" required "
            "placeholder=\"Authenticated reviewer identity\">"
            "<button type=\"submit\">Approve Test/Runtime Override and Continue to Review</button>"
            "</form>"
            f"{retry_form}</div>"
        )
    return review_lineage + (
        f"<form method=\"post\" action=\"{action_url}\">"
        "<textarea name=\"response\" required rows=\"4\" cols=\"36\" "
        "placeholder=\"Add clarification for Codex\"></textarea><br>"
        "<button type=\"submit\">Resume</button>"
        "</form>"
    )


def is_sha256(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    return len(normalized) == 64 and all(
        character in "0123456789abcdef" for character in normalized
    )


def render_human_review_lineage(action: dict[str, Any] | None) -> str:
    if not action:
        return ""
    source_url = str(action.get("source_url") or "")
    parsed_source = urlparse(source_url)
    if parsed_source.scheme in {"http", "https"} and parsed_source.netloc:
        source = (
            f'<a href="{escape(source_url)}">Review source / PR</a>'
        )
    else:
        source = "Review source unavailable"
    decision = str(action.get("triage_decision") or "")
    planning_notice = ""
    if decision == "plan_changes_required":
        planning_notice = (
            '<div class="muted">Requires a new PlanSpec and approval. Reopen the '
            "issue to an active status for replanning; update authoritative Jira "
            "evidence first if product requirements changed."
            "</div>"
        )
    return (
        '<div class="review-lineage"><strong>Human-review continuation</strong>'
        f"<div>Reviewer: {escape(action.get('reviewer_identity'))}</div>"
        f"<div>{source}</div>"
        f"<div class=\"muted\">Action: <code>{escape(action.get('id'))}</code><br>"
        f"Source run: <code>{escape(action.get('source_run_id'))}</code></div>"
        f"{planning_notice}</div>"
    )
