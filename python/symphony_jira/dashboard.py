import html
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
from .models import RunRecord
from .orchestrator import (
    validate_plan_repository_baselines,
)
from .plan_spec import PlanSpecError, parse_plan_spec
from .store import Store, StoreIntegrityError
from .workflow import WorkflowDefinition

MAX_HUMAN_REVIEW_REQUEST_BYTES = 1024 * 1024



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
        if store.latest_unconsumed_human_input_for_issue(run.issue_identifier) is not None:
            raise HTTPException(status_code=409, detail="human input is already pending for this issue")

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
    baseline_error = validate_plan_repository_baselines(
        plan_spec, Path(run.workspace_path), require_clean=True
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

    workspace_diff = capture_workspace_diff(workspace_path, plan_spec)
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
    events = store.list_codex_events(run.id)
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
    review_path = Path(run.workspace_path) / workflow.config.codex.output_review_file
    review_history_path = Path(run.workspace_path) / workflow.config.codex.output_review_history_file
    data.update(
        {
            "current_phase": infer_phase(run, bool(events), review_path.exists()),
            "elapsed_seconds": elapsed_seconds(run),
            "plan_path": str(plan_path),
            "plan_exists": plan_path.exists(),
            "plan_content": read_text_if_exists(plan_path),
            "review_path": str(review_path),
            "review_exists": review_path.exists(),
            "review_content": read_text_if_exists(review_path),
            "review_history_path": str(review_history_path),
            "review_history_exists": review_history_path.exists(),
            "review_history_content": read_text_if_exists(review_history_path),
            "human_inputs": human_inputs,
            "plan_approvals": plan_approvals,
            "active_plan_approval": active_plan_approval,
            "resolved_plan_approval": resolved_plan_approval,
            "requirements_snapshot_hash": run.issue_fingerprint,
            "human_input_pending": run.status == "blocked" and not human_inputs,
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
            "human_review_actionable": store.is_latest_actionable_completed_run(
                run.id
            ),
        }
    )
    return data


def infer_phase(run: RunRecord, has_events: bool, has_review: bool) -> str:
    if run.status == "queued":
        return "queued"
    if run.status == "running":
        if has_review:
            return "review/regeneration"
        if has_events:
            return "codex"
        return "setup"
    if run.status == "completed":
        return "completed"
    if run.status == "blocked":
        if run.blocked_phase == "planning_approval":
            return "plan completed"
        return "blocked"
    if run.status == "cancelled":
        return "cancelled"
    return "failed"


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
    rows = "\n".join(render_run_row(run) for run in visible_runs)
    running = ", ".join(run["issue_identifier"] for run in state["running_issues"]) or "none"
    queued = ", ".join(run["issue_identifier"] for run in state["queued_issues"]) or "none"
    blocked = ", ".join(run["issue_identifier"] for run in state["blocked_issues"]) or "none"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
    <thead><tr><th>Issue</th><th>Status</th><th>Phase</th><th>Blocked Phase</th><th>Elapsed</th><th>Workspace</th><th>Verification</th><th>Plan</th><th>Review</th><th>Error</th><th>Human Input</th><th>Final Message</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>"""


def render_run_row(run: dict[str, Any]) -> str:
    final_message = run.get("final_message") or ""
    return (
        "<tr>"
        f"<td>{escape(run.get('issue_identifier'))}</td>"
        f"<td>{escape(display_status(run))}</td>"
        f"<td>{escape(run.get('current_phase'))}</td>"
        f"<td>{escape(display_blocked_phase(run))}</td>"
        f"<td>{format_elapsed(run.get('elapsed_seconds'))}</td>"
        f"<td><code>{escape(run.get('workspace_path'))}</code></td>"
        f"<td>{escape(run.get('verification_status'))}</td>"
        f"<td>{render_plan_cell(run)}</td>"
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
    if not run.get("review_exists"):
        return "none"
    content = str(run.get("review_content") or "")
    return (
        f"<code>{escape(run.get('review_path'))}</code>"
        f"{render_long_text_cell(content, 'Show review', force_details=True) if content else ''}"
    )


def render_plan_cell(run: dict[str, Any]) -> str:
    if not run.get("plan_exists"):
        return "none"
    content = str(run.get("plan_content") or "")
    return (
        f"<code>{escape(run.get('plan_path'))}</code>"
        f"{render_long_text_cell(content, 'Show plan', force_details=True) if content else ''}"
    )


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


def display_blocked_phase(run: dict[str, Any]) -> str:
    if run.get("blocked_phase") == "planning_approval":
        return "plan completed"
    return str(run.get("blocked_phase") or "")


def display_status(run: dict[str, Any]) -> str:
    if run.get("status") == "blocked" and run.get("blocked_phase") == "planning_approval":
        return "plan completed"
    return str(run.get("status") or "")


def display_error(run: dict[str, Any]) -> str:
    if run.get("blocked_phase") == "planning_approval":
        return ""
    return str(run.get("error") or "")


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
    return review_lineage + (
        f"<form method=\"post\" action=\"{action_url}\">"
        "<textarea name=\"response\" required rows=\"4\" cols=\"36\" "
        "placeholder=\"Add clarification for Codex\"></textarea><br>"
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
