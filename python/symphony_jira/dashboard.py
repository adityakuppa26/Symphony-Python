import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .human_review import (
    HumanReviewContextError,
    capture_workspace_diff,
    read_frozen_text_artifact,
    validate_frozen_snapshot_artifacts,
)
from .models import RequirementsSnapshot, RunRecord
from .orchestrator import (
    managed_diff_repositories,
    validate_plan_repository_baselines,
    validate_planning_workspace,
)
from .plan_spec import (
    PlanSpec,
    PlanSpecError,
    parse_frozen_legacy_plan_spec,
    parse_plan_spec,
)
from .store import Store, StoreIntegrityError
from .workflow import WorkflowDefinition

MAX_HUMAN_REVIEW_REQUEST_BYTES = 1024 * 1024
SUMMARY_ITEM_LIMIT = 3
SUMMARY_ITEM_MAX_CHARACTERS = 180
SUMMARY_GOAL_MAX_CHARACTERS = 240
SUMMARY_APPROACH_MAX_CHARACTERS = 360
SUMMARY_REPOSITORIES_MAX_CHARACTERS = 180
PLAN_SUMMARY_UNAVAILABLE = (
    "Plan summary unavailable because the plan could not be validated for this run. "
    "Open the full plan file for details."
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
        from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
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

    @app.get("/api/v1/runs/{run_id}/plan", response_class=PlainTextResponse)
    async def run_plan(run_id: str):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        try:
            content = read_frozen_text_artifact(
                Path(run.workspace_path),
                workflow.config.codex.output_plan_file,
                label="dashboard PlanSpec artifact",
                required=True,
            )
            snapshot = (
                store.get_requirements_snapshot(
                    run.issue_identifier,
                    run.issue_fingerprint,
                )
                if run.issue_fingerprint
                else None
            )
            if not content:
                raise PlanSpecError("dashboard PlanSpec artifact is empty")
            plan = parse_dashboard_plan_spec(
                content,
                run=run,
                requirements_snapshot=snapshot,
            )
            validate_dashboard_plan_binding(
                plan,
                run=run,
                requirements_snapshot=snapshot,
            )
        except (HumanReviewContextError, PlanSpecError, StoreIntegrityError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"the plan is unavailable: {exc}",
            ) from exc
        return PlainTextResponse(content, media_type="application/json")

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
        elif action not in {"", "feedback"}:
            raise HTTPException(status_code=400, detail="unsupported human input action")
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
    latest_run_ids_by_issue = latest_run_ids(enriched)
    running = [run for run in enriched if run["status"] == "running"]
    queued = [run for run in enriched if run["status"] == "queued"]
    blocked = [run for run in enriched if is_actionable_blocked_run(run, latest_run_ids_by_issue)]
    completed_or_failed = [run for run in enriched if run["status"] in {"completed", "failed", "cancelled"}]

    return {
        "workflow_path": str(workflow.path),
        "workflow_kind": workflow.config.kind,
        "jira_jql": workflow.config.tracker.jql,
        "poll_interval_seconds": workflow.config.polling.interval_seconds,
        "workspace_root": str(workflow.config.workspace.root),
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
    baseline_error = validate_planning_workspace(
        plan_spec, Path(run.workspace_path), run.planning_baseline,
    )
    if baseline_error:
        raise PlanSpecError(
            f"PlanSpec repository baseline validation failed: {baseline_error}"
        )
    return plan_hash


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
    return {
        "plan_spec": plan_content,
        "approval": approval,
        "source_review": source_review,
        "source_review_history": source_review_history,
        "workspace_diff": workspace_diff.content,
        "workspace_diff_hash": workspace_diff.content_hash,
    }


def enrich_run(run: RunRecord, store: Store, workflow: WorkflowDefinition) -> dict[str, Any]:
    data = run_to_dict(run)
    data["workflow_kind"] = workflow.config.kind
    data["verification_advisory"] = True
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
    human_input_actionable = bool(
        run.status == "blocked"
        and not human_inputs
        and store.is_latest_actionable_blocked_run(run.id)
    )
    data.update(
        {
            "current_phase": current_phase,
            "workflow_progress": development_workflow_progress(
                run,
                current_phase=current_phase,
                event_types=tuple(event.event_type for event in events),
                approval_required=workflow.config.codex.require_plan_approval,
                review_required=workflow.config.codex.review_after_run,
            ),
            "elapsed_seconds": elapsed_seconds(run),
            "plan_path": str(plan_path),
            "plan_url": f"/api/v1/runs/{run.id}/plan",
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
            "human_inputs": human_inputs,
            "plan_approvals": plan_approvals,
            "active_plan_approval": active_plan_approval,
            "resolved_plan_approval": resolved_plan_approval,
            "requirements_snapshot_hash": run.issue_fingerprint,
            "human_input_actionable": human_input_actionable,
            "human_input_pending": human_input_actionable,
            "human_input_submitted": any(item.get("consumed_at") is None for item in human_inputs),
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
                store.is_latest_actionable_completed_run(run.id)
            ),
        }
    )
    return data


def infer_phase(run: RunRecord, latest_event_type: str | None) -> str:
    if run.status == "queued":
        return "queued"
    if run.status == "running":
        event_type = str(latest_event_type or "").strip().lower()
        if event_type.startswith("development_review."):
            return "Development Review"
        if event_type.startswith("plan"):
            return "Development Planning"
        if event_type.startswith("development_implementation."):
            return "Development Implementation"
        if event_type.startswith(("review", "human_review.")):
            return "Development Review"
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
        }.get(str(run.blocked_phase or ""), "blocked")
    if run.status == "cancelled":
        return "cancelled"
    return "failed"


def development_workflow_progress(
    run: RunRecord,
    *,
    current_phase: str,
    event_types: tuple[str, ...] = (),
    approval_required: bool = False,
    review_required: bool = False,
) -> str:
    """Keep the development gates visible, including the completed handoff."""
    review_seen = any(
        event.startswith(("development_review.", "review."))
        for event in event_types
    )
    completed = run.status == "completed"
    implemented = review_seen or current_phase == "Development Review" or completed
    approved = bool(run.plan_approval_id) or implemented or current_phase == "Development Implementation"
    planned = bool(run.plan_spec_hash) or approved or current_phase == "Dev Approval"
    stages = (
        ("Planning", "Development Planning", planned, True),
        ("Human approval", "Dev Approval", approved, approval_required),
        ("Implementation", "Development Implementation", implemented, True),
        ("Code review", "Development Review", completed and review_required, review_required),
        ("Handoff", "completed", completed, True),
    )
    result = []
    for label, phase, done, required in stages:
        if current_phase == phase and run.status == "blocked":
            status = "awaiting approval" if phase == "Dev Approval" else "blocked"
        elif current_phase == phase and run.status == "running":
            status = "running"
        elif not required:
            status = "not required"
        elif done:
            status = "done"
        else:
            status = "pending"
        result.append(f"{label}: {status}")
    return " → ".join(result)


def elapsed_seconds(run: RunRecord) -> float:
    end = run.finished_at or datetime.now(timezone.utc)
    start = run.started_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds())


def render_dashboard_html(state: dict[str, Any]) -> str:
    # Store order is newest first. Consolidate presentation only; run records,
    # API responses, actionability, and phase transitions retain their identities.
    cases: dict[str, list[dict[str, Any]]] = {}
    for run in state["all_runs"][:20]:
        key = str(run.get("issue_identifier") or run.get("id") or "")
        cases.setdefault(key, []).append(run)
    rows = "\n".join(
        render_run_row(attempts[0], earlier_attempts=attempts[1:])
        for attempts in cases.values()
    )
    running = render_issue_chips(state["running_issues"])
    queued = render_issue_chips(state["queued_issues"])
    blocked = render_issue_chips(state["blocked_issues"])
    if not rows:
        rows = '<tr><td colspan="6" class="empty-state">No runs yet. Issues matching the workflow settings will appear here.</td></tr>'


    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Symphony Jira</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: #18212f; background: #f4f7fb; line-height: 1.55; }}
    main {{ width: min(1500px, calc(100% - 2rem)); margin: 0 auto 3rem; }}
    header {{ display: flex; justify-content: space-between; gap: 1rem; align-items: flex-end; padding: 2rem 0 1.25rem; }}
    h1 {{ margin: 0; font-size: clamp(1.7rem, 3vw, 2.35rem); letter-spacing: -0.04em; }}
    h2 {{ margin: 2rem 0 0.75rem; font-size: 1.15rem; }}
    code {{ background: #edf1f7; padding: 0.12rem 0.3rem; border-radius: 5px; font-size: 0.82em; overflow-wrap: anywhere; }}
    .eyebrow {{ color: #64748b; font-size: 0.75rem; font-weight: 750; letter-spacing: 0.12em; text-transform: uppercase; }}
    .refresh {{ color: #64748b; font-size: 0.8rem; white-space: nowrap; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.75rem; }}
    .panel {{ min-height: 7rem; border: 1px solid #dfe6ef; border-radius: 14px; padding: 1rem; background: #fff; box-shadow: 0 8px 24px rgba(30, 41, 59, 0.04); }}
    .panel-label {{ color: #64748b; font-size: 0.75rem; font-weight: 750; letter-spacing: 0.08em; text-transform: uppercase; }}
    .metric {{ display: block; margin: 0.2rem 0 0.65rem; font-size: 1.8rem; font-weight: 780; line-height: 1; }}
    .panel-running {{ border-top: 3px solid #2563eb; }}
    .panel-queued {{ border-top: 3px solid #94a3b8; }}
    .panel-blocked {{ border-top: 3px solid #dc2626; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 0.35rem; }}
    .chip {{ display: inline-flex; border-radius: 999px; padding: 0.2rem 0.48rem; background: #edf2f7; color: #334155; font-size: 0.76rem; font-weight: 700; }}
    .empty {{ color: #94a3b8; font-size: 0.82rem; }}
    .settings {{ margin-top: 0.8rem; color: #64748b; font-size: 0.82rem; }}
    .settings-grid {{ display: grid; grid-template-columns: 1fr auto 1fr; gap: 1rem; padding: 0.75rem 0; }}
    .table-shell {{ overflow-x: auto; border: 1px solid #dfe6ef; border-radius: 14px; background: #fff; box-shadow: 0 12px 30px rgba(30, 41, 59, 0.05); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ text-align: left; border-bottom: 1px solid #edf1f5; padding: 0.8rem; vertical-align: top; }}
    th {{ color: #64748b; background: #f8fafc; font-size: 0.7rem; letter-spacing: 0.08em; text-transform: uppercase; }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    tbody tr:hover {{ background: #fbfdff; }}
    .issue-key {{ font-size: 0.95rem; font-weight: 780; white-space: nowrap; }}
    .issue-meta {{ margin-top: 0.25rem; color: #94a3b8; font-size: 0.72rem; }}
    .badge {{ display: inline-flex; align-items: center; gap: 0.3rem; border-radius: 999px; padding: 0.22rem 0.52rem; font-size: 0.74rem; font-weight: 760; white-space: nowrap; background: #eef2f7; color: #475569; }}
    .badge::before {{ content: ""; width: 0.42rem; height: 0.42rem; border-radius: 50%; background: currentColor; }}
    .badge-completed, .badge-passed {{ background: #e8f7ee; color: #167a45; }}
    .badge-running {{ background: #e8f1ff; color: #1d63c6; }}
    .badge-blocked, .badge-failed {{ background: #feeeee; color: #c12b2b; }}
    .badge-awaiting-approval {{ background: #fff4d6; color: #9a5b00; }}
    .badge-queued, .badge-pending, .badge-not-configured {{ background: #f1f4f8; color: #64748b; }}
    .phase-name {{ font-size: 0.84rem; font-weight: 720; }}
    .blocked-label {{ margin-top: 0.35rem; color: #b42318; font-size: 0.73rem; font-weight: 700; }}
    .pipeline {{ margin-top: 0.4rem; }}
    .pipeline summary {{ color: #64748b; font-size: 0.72rem; font-weight: 650; }}
    .stage-list {{ display: grid; gap: 0.35rem; margin: 0.6rem 0 0; padding: 0; list-style: none; min-width: 11rem; }}
    .stage {{ display: flex; justify-content: space-between; gap: 0.5rem; border-radius: 6px; padding: 0.3rem 0.5rem; background: #f1f5f9; color: #475569; font-size: 0.75rem; }}
    .stage-done {{ background: #e8f7ee; color: #167a45; }}
    .stage-running {{ background: #e8f1ff; color: #1d63c6; }}
    .stage-blocked {{ background: #feeeee; color: #c12b2b; }}
    .stage-awaiting-approval {{ background: #fff4d6; color: #805000; }}
    .details-stack {{ display: grid; gap: 0.42rem; min-width: 16rem; }}
    .attempt-history-list {{ list-style: none; padding: 0; margin: 0.6rem 0 0; }}
    .attempt-history {{ margin-top: 0.5rem; border: 1px solid #dfe6ef; border-radius: 8px; padding: 0.5rem 0.65rem; }}
    .attempt-history-list > li {{ padding: 0.65rem 0; border-top: 1px solid #e2e8f0; }}
    .attempt-history-meta {{ display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; font-size: 0.78rem; }}
    .artifact-link {{ display: inline-flex; width: fit-content; align-items: center; gap: 0.35rem; border: 1px solid #cbd5e1; border-radius: 7px; padding: 0.38rem 0.58rem; color: #1d4ed8; background: #fff; font-size: 0.76rem; font-weight: 760; text-decoration: none; }}
    .artifact-link:hover {{ border-color: #93b4ef; background: #f5f9ff; }}
    .details-stack > details {{ border: 1px solid #e5eaf1; border-radius: 8px; padding: 0.42rem 0.55rem; }}
    .details-stack > details[open] {{ background: #fbfcfe; }}
    .detail-grid {{ display: grid; gap: 0.75rem; padding-top: 0.6rem; }}
    .detail-section > strong {{ display: block; margin-bottom: 0.25rem; color: #475569; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; }}
    .muted {{ color: #64748b; }}
    pre {{ white-space: pre-wrap; max-height: 28rem; overflow: auto; margin: 0.5rem 0 0; font-size: 0.85rem; line-height: 1.65; overflow-wrap: anywhere; }}
    details {{ max-width: 46rem; }}
    summary {{ cursor: pointer; color: #334155; font-weight: 680; font-size: 0.78rem; }}
    .preview {{ color: #64748b; margin-top: 0.35rem; font-size: 0.76rem; }}
    .brief-summary {{ white-space: pre-line; line-height: 1.65; max-width: 38rem; font-size: 0.87rem; }}
    .artifact-path {{ color: #94a3b8; margin-top: 0.45rem; max-width: 38rem; overflow-wrap: anywhere; font-size: 0.72rem; }}
    .workflow-guide {{ display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; padding: 1rem 1.25rem; border: 1px solid #dfe6ef; background: #fff; border-radius: 12px; margin: 0 0 1rem; font-size: 0.88rem; color: #334155; }}
    .workflow-guide span[aria-hidden] {{ color: #94a3b8; }}
    .subtitle {{ margin: 0.3rem 0 0; color: #475569; font-size: 0.9rem; }}
    .run-plan-completed {{ background: #fffdf5; }}
    .badge-plan-completed {{ background: #fff4d6; color: #805000; }}
    .verification-note, .handoff-note {{ margin-top: 0.45rem; color: #475569; font-size: 0.78rem; max-width: 19rem; }}
    .handoff-note {{ border-left: 3px solid #22a06b; padding-left: 0.65rem; }}
    .action-panel {{ border-color: #d4b66c !important; background: #fffdf5 !important; }}
    form {{ display: grid; gap: 0.65rem; margin: 0.8rem 0; }}
    form br {{ display: none; }}
    label {{ display: grid; gap: 0.3rem; color: #334155; font-size: 0.82rem; font-weight: 600; }}
    input, textarea, button {{ font: inherit; }}
    input, textarea {{ width: 100%; min-width: 0; padding: 0.6rem 0.7rem; color: #18212f; background: #fff; border: 1px solid #aab8c9; border-radius: 7px; font-size: 0.88rem; }}
    textarea {{ resize: vertical; line-height: 1.5; }}
    button {{ width: fit-content; border: 1px solid #1d4ed8; border-radius: 7px; padding: 0.55rem 0.85rem; background: #1d4ed8; color: white; cursor: pointer; font-size: 0.82rem; font-weight: 700; }}
    button:hover {{ background: #1e40af; }}
    .secondary-button {{ color: #1d4ed8; background: white; }}
    .secondary-button:hover {{ color: white; }}
    :focus-visible {{ outline: 3px solid #5b9aff; outline-offset: 3px; }}
    .empty-state {{ padding: 2.5rem; text-align: center; color: #475569; }}
    .refresh a {{ color: #1d4ed8; }}
    @media (max-width: 960px) {{
      main {{ width: min(100% - 1rem, 1500px); }}
      header {{ align-items: flex-start; flex-direction: column; }}
      .grid {{ grid-template-columns: 1fr; }}
      .settings-grid {{ grid-template-columns: 1fr; }}
      table, tbody, tr, td {{ display: block; width: 100%; }}
      thead {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }}
      tbody tr {{ padding: 0.75rem; border-bottom: 2px solid #dfe6ef; }}
      td {{ border: 0; padding: 0.45rem; }}
      td[data-label]::before {{ content: attr(data-label); display: block; margin-bottom: 0.25rem; color: #64748b; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; }}
      .details-stack, .stage-list {{ min-width: 0; }}
      .stage-list {{ grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); }}
      details {{ max-width: 100%; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div><div class="eyebrow">Development orchestrator</div><h1>Symphony</h1><p class="subtitle">From Jira requirements to reviewed code, with a clear human handoff.</p></div>
      <div class="refresh"><span id="refresh-status" role="status">Refreshes every 60 seconds while idle</span><br><a href="/">Refresh now</a></div>
    </header>
    <nav class="workflow-guide" aria-label="Development workflow">
      <strong>Planning</strong><span aria-hidden="true">→</span>
      <strong>Human approval</strong><span aria-hidden="true">→</span>
      <strong>Implementation</strong><span aria-hidden="true">↔</span>
      <strong>Code review</strong><span aria-hidden="true">→</span>
      <strong>Handoff</strong>
    </nav>
    <div class="grid">
      {render_queue_panel("Running", state["running_issues"], running, "running")}
      {render_queue_panel("Queued", state["queued_issues"], queued, "queued")}
      {render_queue_panel("Blocked", state["blocked_issues"], blocked, "blocked")}
    </div>
    <details class="settings">
      <summary>Workflow settings</summary>
      <div class="settings-grid">
        <div><strong>JQL</strong><br>{escape(state["jira_jql"])}</div>
        <div><strong>Workflow</strong><br>{escape(state.get("workflow_kind", "development"))}<br>Poll: {state["poll_interval_seconds"]}s</div>
        <div><strong>Workspace</strong><br><code>{escape(state["workspace_root"])}</code><br><code>{escape(state["workflow_path"])}</code></div>
      </div>
    </details>
    <h2>Recent cases</h2>
    <div class="table-shell"><table>
      <thead><tr><th>Issue</th><th>Status</th><th>Phase</th><th>Verification</th><th>Elapsed</th><th>Details</th></tr></thead>
      <tbody>{rows}</tbody>
    </table></div>
  </main>
  <script type="text/javascript">
    let interacting = false;
    const pauseRefresh = () => {{
      interacting = true;
      document.getElementById("refresh-status").textContent = "Refresh paused while you read or edit";
    }};
    document.addEventListener("input", pauseRefresh);
    document.addEventListener("click", (event) => {{
      if (event.target.closest("summary")) pauseRefresh();
    }});
    document.addEventListener("keydown", (event) => {{
      if (event.target.closest("summary") && ["Enter", " "].includes(event.key)) pauseRefresh();
    }});
    setInterval(() => {{ if (!interacting && !document.hidden) window.location.reload(); }}, 60000);
  </script>
</body>
</html>"""


def render_run_row(
    run: dict[str, Any],
    *,
    earlier_attempts: list[dict[str, Any]] | None = None,
) -> str:
    earlier_attempts = earlier_attempts or []
    final_message = display_final_message(run)
    planning_complete = bool(
        run.get("blocked_phase") == "planning_approval"
        or run.get("current_phase") == "Dev Approval"
    )
    if not planning_complete and is_plan_artifact_message(
        run.get("final_message")
    ):
        final_message = ""
    phase_progress = workflow_phase_label(run, str(run.get("workflow_progress") or ""))
    blocked_phase = workflow_phase_label(run, display_blocked_phase(run))
    current_phase = workflow_phase_label(run, str(run.get("current_phase") or ""))
    phase_cell = f'<div class="phase-name">{escape(current_phase)}</div>'
    if blocked_phase:
        phase_cell += f'<div class="blocked-label">Blocked: {escape(blocked_phase)}</div>'
    phase_cell += render_phase_progress(phase_progress)
    status = display_status(run)
    verification = str(run.get("verification_status") or "not configured")
    verification_note = '<div class="verification-note">Advisory · does not block handoff</div>'
    attempts_note = (
        f' · {len(earlier_attempts) + 1} recent attempts' if earlier_attempts else ""
    )
    details = render_run_details(run, final_message)
    if earlier_attempts:
        details += render_attempt_history(earlier_attempts)
    return (
        f'<tr class="run-{status_class(status)}">'
        f'<td data-label="Issue"><div class="issue-key">{escape(run.get("issue_identifier"))}</div>'
        f'<div class="issue-meta">attempt {escape(run.get("attempt"))}{attempts_note}</div></td>'
        f'<td data-label="Status">{render_badge(status)}</td>'
        f'<td data-label="Phase">{phase_cell}</td>'
        f'<td data-label="Verification">{render_badge(verification)}{verification_note}</td>'
        f"<td data-label=\"Elapsed\">{format_elapsed(run.get('elapsed_seconds'))}</td>"
        f'<td data-label="Details">{details}</td>'
        "</tr>"
    )


def render_attempt_history(attempts: list[dict[str, Any]]) -> str:
    """Historical attempts are inspectable, with no stale action forms."""
    items: list[str] = []
    for attempt in attempts:
        phase = workflow_phase_label(attempt, str(attempt.get("current_phase") or ""))
        output = str(attempt.get("final_message") or attempt.get("error") or "")
        historical = {
            **attempt,
            "human_input_actionable": False,
            "human_review_actionable": False,
        }
        saved_details = render_run_details(historical, output)
        items.append(
            '<li><div class="attempt-history-meta">'
            f'<strong>Attempt {escape(attempt.get("attempt"))}</strong>'
            f'{render_badge(display_status(attempt))}'
            f'<span>{escape(phase)}</span>'
            f'<span>{format_elapsed(attempt.get("elapsed_seconds"))}</span></div>'
            f'<div class="muted">Started {escape(attempt.get("started_at"))}</div>'
            f'<a href="/api/v1/runs/{escape(attempt.get("id"))}" '
            'target="_blank" rel="noopener noreferrer">View attempt details ↗</a>'
            '<details><summary>Attempt details</summary>'
            f'{saved_details}</details></li>'
        )
    return (
        '<details class="attempt-history">'
        f'<summary>Earlier attempts ({len(attempts)})</summary>'
        '<p class="muted">Historical snapshots from the recent run history. '
        'The current phase and available actions are shown above.</p>'
        f'<ol class="attempt-history-list">{"".join(items)}</ol></details>'
    )


def is_plan_artifact_message(value: Any) -> bool:
    text = str(value or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("decision") == "ready_for_approval"
        and "requirements" in payload
        and "affected_surface" in payload
    )


def render_queue_panel(
    label: str,
    runs: list[dict[str, Any]],
    chips: str,
    tone: str,
) -> str:
    return (
        f'<section class="panel panel-{tone}">'
        f'<div class="panel-label">{escape(label)}</div>'
        f'<span class="metric">{len(runs)}</span>{chips}</section>'
    )


def render_issue_chips(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return '<span class="empty">Nothing here</span>'
    return '<div class="chips">' + "".join(
        f'<span class="chip">{escape(run.get("issue_identifier"))}</span>'
        for run in runs
    ) + "</div>"


def status_class(value: Any) -> str:
    normalized = str(value or "unknown").strip().lower().replace("_", "-")
    return "".join(
        character if character.isalnum() or character == "-" else "-"
        for character in normalized
    )


def render_badge(value: Any) -> str:
    label = str(value or "unknown").replace("_", " ")
    return f'<span class="badge badge-{status_class(label)}">{escape(label)}</span>'


def render_phase_progress(progress: str) -> str:
    if not progress:
        return ""
    stages: list[str] = []
    for item in progress.split(" → "):
        label, separator, status = item.rpartition(": ")
        if not separator:
            label, status = item, "pending"
        stages.append(
            f'<li class="stage stage-{status_class(status)}" '
            f'title="{escape(label)}: {escape(status)}"><span>{escape(label)}</span><span>{escape(status)}</span></li>'
        )
    return (
        f'<ol class="stage-list" aria-label="{escape(progress)}">'
        f'{"".join(stages)}</ol>'
    )


def render_run_details(
    run: dict[str, Any],
    final_message: str,
) -> str:
    planning_complete = bool(
        run.get("blocked_phase") == "planning_approval"
        or run.get("current_phase") == "Dev Approval"
    )
    scope_sections = [
        '<div class="detail-section"><strong>Requirements</strong>'
        f'{render_requirements_cell(run)}</div>',
    ]
    if planning_complete and run.get("plan_exists"):
        plan_label = workflow_phase_label(run, "Development plan")
        scope_sections.append(
            f'<div class="detail-section"><strong>{escape(plan_label)}</strong>'
            f'{render_plan_cell(run, show_summary=True)}</div>'
        )
    details: list[str] = []
    if run.get("plan_exists"):
        details.append(render_plan_link(run))
    open_attribute = " open" if planning_complete else ""
    details.append(
        f'<details{open_attribute}><summary>Scope &amp; artifacts</summary>'
        f'<div class="detail-grid">{"".join(scope_sections)}</div></details>'
    )
    review = render_review_cell(run)
    if review != "none":
        details.append(
            '<details><summary>Reviews</summary>'
            f'<div class="detail-grid">{review}</div></details>'
        )
    if run.get("human_input_context"):
        details.append(
            '<details><summary>Accumulated human input</summary>'
            f'<pre>{escape(run["human_input_context"])}</pre></details>'
        )
    error = display_error(run)
    if error:
        details.append(
            '<details open><summary>Error</summary>'
            f'<pre>{escape(error)}</pre></details>'
        )
    human_input = render_human_input_cell(run)
    if human_input and human_input != "none":
        open_attribute = " open" if run.get("status") == "blocked" else ""
        action_label = "Action required" if run.get("human_input_actionable") else "Human feedback &amp; continuation"
        details.append(
            f'<details class="action-panel"{open_attribute}><summary>{action_label}</summary>'
            f'<div class="detail-grid">{human_input}</div></details>'
        )
    if run.get("status") == "completed" and run.get("human_review_actionable"):
        details.insert(0, '<div class="handoff-note">Handoff complete. Add later review feedback below to resume with the saved plan and code context.</div>')
    if final_message:
        details.append(
            '<details><summary>Final output</summary>'
            f'<pre>{escape(final_message)}</pre></details>'
        )
    details.append(
        '<details><summary>Workspace</summary>'
        f'<code>{escape(run.get("workspace_path"))}</code></details>'
    )
    return f'<div class="details-stack">{"".join(details)}</div>'


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
    ):
        label = workflow_phase_label(run, label)
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


def render_plan_cell(
    run: dict[str, Any],
    *,
    show_summary: bool = False,
) -> str:
    if not run.get("plan_exists"):
        return "none"
    link = render_plan_link(run)
    if not show_summary:
        return link
    summary = (
        str(run.get("plan_summary") or "").strip()
        or PLAN_SUMMARY_UNAVAILABLE
    )
    retained = run.get("planning_baseline")
    retained_details = ""
    if retained:
        retained_details = (
            '<div class="detail-section"><strong>Retained implementation</strong>'
            'This approval includes the existing changes shown below.'
            f'<div>Source run: <code>{escape(retained.get("source_run_id"))}</code></div>'
            f'<div>Diff: <code>{escape(retained.get("workspace_diff_hash"))}</code></div>'
            + render_long_text_cell(
                str(retained.get("workspace_diff") or ""),
                "Review retained implementation diff", force_details=True,
            )
            + "</div>"
        )
    return f'<div class="brief-summary">{escape(summary)}</div>{link}{retained_details}'


def render_plan_link(run: dict[str, Any]) -> str:
    return (
        f'<a class="artifact-link" href="{escape(run.get("plan_url"))}" '
        'target="_blank" rel="noopener noreferrer">Open plan ↗</a>'
    )


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


def workflow_phase_label(run: dict[str, Any], label: str) -> str:
    return label


def display_blocked_phase(run: dict[str, Any]) -> str:
    phase = str(run.get("blocked_phase") or "")
    return {
        "planning": "Development Planning",
        "planning_approval": "Dev Approval",
        "implementation": "Development Implementation",
        "development_review": "Development Review",
    }.get(phase, phase)


def display_status(run: dict[str, Any]) -> str:
    if run.get("status") == "blocked" and run.get("blocked_phase") == "planning_approval":
        return "plan completed"
    return str(run.get("status") or "")


def display_error(run: dict[str, Any]) -> str:
    if run.get("blocked_phase") in {
        "planning_approval",
    }:
        return ""
    return str(run.get("error") or "")


def display_final_message(run: dict[str, Any]) -> str:
    final_message = str(run.get("final_message") or "")
    plan_content = str(run.get("plan_content") or "")
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
            "<label>Reviewer identity<input name=\"reviewer_identity\" required "
            "placeholder=\"Reviewer identity\"></label>"
            "<label>PR or review URL<input name=\"source_url\" type=\"url\" required "
            "placeholder=\"PR or review URL\"></label>"
            "<label>Review feedback<textarea name=\"comments\" required rows=\"6\" cols=\"42\" "
            "placeholder=\"Paste human review comments\"></textarea></label>"
            "<button type=\"submit\">Address Human Review</button>"
            "</form></details>"
        )
    if run.get("status") != "blocked":
        return review_lineage or "none"
    if inputs:
        latest = inputs[0]
        state = "queued for resume" if latest.get("consumed_at") is None else "consumed"
        approval_details = ""
        if latest.get("approval_id"):
            approval_details = (
                f"<div>Approved by {escape(latest.get('approver_identity'))} "
                f"at {escape(latest.get('approved_at'))}</div>"
                f"<div class=\"muted\">PlanSpec: <code>{escape(latest.get('plan_spec_hash'))}</code><br>"
                "Requirements snapshot: "
                f"<code>{escape(latest.get('requirements_snapshot_hash'))}</code></div>"
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
            "<label>Approver identity<input name=\"approver_identity\" required placeholder=\"Approver identity\"></label>"
            "<button type=\"submit\">Approve Exact Plan</button>"
            "</form>"
            f"<form method=\"post\" action=\"{action_url}\">"
            "<input type=\"hidden\" name=\"action\" value=\"feedback\">"
            "<label>Feedback for Codex<textarea name=\"response\" required rows=\"4\" cols=\"36\" "
            "placeholder=\"Describe requested adjustments\"></textarea></label>"
            "<button class=\"secondary-button\" type=\"submit\">Request Adjustments</button>"
            "</form></div>"
        )
    return review_lineage + (
        f"<form method=\"post\" action=\"{action_url}\">"
        "<label>Feedback for Codex<textarea name=\"response\" required rows=\"4\" cols=\"36\" "
        "placeholder=\"Add clarification for Codex\"></textarea></label>"
        "<button type=\"submit\">Resume</button>"
        "</form>"
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
