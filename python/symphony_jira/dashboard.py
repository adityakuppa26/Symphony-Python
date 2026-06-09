import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .models import RunRecord
from .store import Store
from .workflow import WorkflowDefinition


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
        return {
            "run": enrich_run(run, store, workflow),
            "codex_events": [event.model_dump(mode="json") for event in store.list_codex_events(run_id)],
            "logs": store.list_logs(run_id=run_id),
            "jira_actions": store.list_jira_actions(run_id=run_id),
            "human_inputs": store.list_human_inputs(run_id=run_id),
        }

    @app.post("/api/v1/runs/{run_id}/human-input")
    async def add_human_input(run_id: str, request: Request):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        if run.status != "blocked":
            raise HTTPException(status_code=409, detail="human input can only be added to blocked runs")

        body = await request.body()
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
            response = str(payload.get("response") or "").strip()
        else:
            form = parse_qs(body.decode(errors="replace"))
            response = str((form.get("response") or [""])[0]).strip()
        if not response and run.blocked_phase == "planning_approval":
            response = "Approved."
        if not response:
            raise HTTPException(status_code=400, detail="response is required")

        record = store.add_human_input(
            run.issue_identifier,
            run_id=run.id,
            question=run.error,
            response=response,
        )
        if orchestrator is not None:
            await orchestrator.poll_once()
        if "text/html" in request.headers.get("accept", "") and "application/json" not in content_type:
            return RedirectResponse("/", status_code=303)
        return {"status": "ok", "human_input": record}

    @app.get("/api/v1/issues/{issue_key}")
    async def issue_detail(issue_key: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "issue_key": issue_key,
            "runs": [run_to_dict(run) for run in store.list_runs_for_issue(issue_key)],
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
    running = [run for run in enriched if run["status"] == "running"]
    queued = [run for run in enriched if run["status"] == "queued"]
    blocked = [run for run in enriched if run["status"] == "blocked"]
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


def orchestrator_snapshot(orchestrator: Any | None) -> dict[str, Any] | None:
    if orchestrator is None:
        return None
    return orchestrator.snapshot()


def run_to_dict(run: RunRecord) -> dict[str, Any]:
    return run.model_dump(mode="json")


def enrich_run(run: RunRecord, store: Store, workflow: WorkflowDefinition) -> dict[str, Any]:
    data = run_to_dict(run)
    events = store.list_codex_events(run.id)
    human_inputs = store.list_human_inputs(run_id=run.id)
    plan_path = Path(run.workspace_path) / workflow.config.codex.output_plan_file
    review_path = Path(run.workspace_path) / workflow.config.codex.output_review_file
    review_history_path = Path(run.workspace_path) / workflow.config.codex.output_review_history_file
    data.update(
        {
            "current_phase": infer_phase(run, bool(events), review_path.exists()),
            "elapsed_seconds": elapsed_seconds(run),
            "plan_path": str(plan_path),
            "plan_exists": plan_path.exists(),
            "review_path": str(review_path),
            "review_exists": review_path.exists(),
            "review_history_path": str(review_history_path),
            "review_history_exists": review_history_path.exists(),
            "human_inputs": human_inputs,
            "human_input_pending": run.status == "blocked" and not human_inputs,
            "human_input_submitted": any(item.get("consumed_at") is None for item in human_inputs),
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
    pre {{ white-space: pre-wrap; max-height: 12rem; overflow: auto; }}
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
        f"<td><pre>{escape(display_error(run)[:1000])}</pre></td>"
        f"<td>{render_human_input_cell(run)}</td>"
        f"<td><pre>{escape(final_message[:1000])}</pre></td>"
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
    return f"<code>{escape(run.get('review_path'))}</code>"


def render_plan_cell(run: dict[str, Any]) -> str:
    if not run.get("plan_exists"):
        return "none"
    return f"<code>{escape(run.get('plan_path'))}</code>"


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
    if run.get("status") != "blocked":
        return "none"
    if inputs:
        latest = inputs[0]
        state = "queued for resume" if latest.get("consumed_at") is None else "consumed"
        return f"<strong>{escape(state)}</strong><pre>{escape((latest.get('response') or '')[:500])}</pre>"
    if run.get("blocked_phase") == "planning_approval":
        placeholder = "Confirm the plan or enter requested adjustments"
        button = "Confirm Plan"
    else:
        placeholder = "Add clarification for Codex"
        button = "Resume"
    return (
        f"<form method=\"post\" action=\"/api/v1/runs/{escape(run.get('id'))}/human-input\">"
        f"<textarea name=\"response\" rows=\"4\" cols=\"36\" placeholder=\"{escape(placeholder)}\"></textarea><br>"
        f"<button type=\"submit\">{escape(button)}</button>"
        "</form>"
    )
