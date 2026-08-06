from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from symphony_jira.automation_plan import (
    AutomationPlan,
    automation_result_content_hash,
)
from symphony_jira.config import RuntimeRepositoryConfig
from symphony_jira.dashboard import (
    MAX_HUMAN_REVIEW_REQUEST_BYTES,
    build_state,
    create_app,
    current_plan_spec_hash,
    prepare_bound_automation_context,
    prepare_human_review_context,
    prepare_verification_bypass_context,
    render_dashboard_html,
)
from symphony_jira.human_review import (
    HumanReviewContextError,
    capture_workspace_diff,
)
from symphony_jira.models import (
    Issue,
    RequirementArtifact,
    RequirementDecision,
    RequirementSource,
    RequirementsSnapshot,
)
from symphony_jira.orchestrator import capture_automation_repository_diff
from symphony_jira.plan_spec import PlanSpecError, parse_plan_spec
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow


class DashboardTests(unittest.TestCase):
    def test_build_state_and_html_include_recent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            run = store.create_run(issue, root / "workspaces" / "T-1", branch_name="codex/T-1", status="queued")
            store.update_run(
                run.id,
                status="failed",
                final_message="done",
                error="Codex could not run verification",
                blocked_phase="implementation",
                verification_status="failed",
            )
            plan_path = root / "workspaces" / "T-1" / ".symphony" / "codex-plan.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text("Plan content", encoding="utf-8")

            state = build_state(workflow, store, runtime={"running": []})
            html = render_dashboard_html(state)

            self.assertEqual(state["jira_jql"], "project = T")
            self.assertFalse(state["automation_enabled"])
            self.assertEqual(state["recent_runs"][0]["issue_identifier"], "T-1")
            self.assertEqual(state["recent_runs"][0]["current_phase"], "failed")
            self.assertNotIn("codex_event_count", state["recent_runs"][0])
            self.assertNotIn("<th>Events</th>", html)
            self.assertIn('<meta http-equiv="refresh" content="60">', html)
            self.assertIn("<th>Requirements Spec</th>", html)
            self.assertIn("<th>Plan</th>", html)
            self.assertNotIn("<th>Automation</th>", html)
            self.assertIn("<th>Blocked Phase</th>", html)
            self.assertIn("<th>Error</th>", html)
            self.assertTrue(state["recent_runs"][0]["plan_exists"])
            self.assertEqual(state["recent_runs"][0]["blocked_phase"], "implementation")
            self.assertIn("T-1", html)
            self.assertIn("Development Implementation", html)
            self.assertIn("codex-plan.md", html)
            self.assertIn("Plan summary unavailable", html)
            self.assertNotIn("Plan content", html)
            self.assertIn("Codex could not run verification", html)
            self.assertIn("done", html)

    def test_dashboard_renders_brief_plan_and_requirements_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            snapshot = dashboard_requirements_snapshot()
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
                requirements_snapshot=snapshot,
            )
            plan_payload = dashboard_plan_payload(snapshot.content_hash)
            plan_content = json.dumps(plan_payload, indent=2)
            run = store.create_run(issue, root / "workspaces" / "T-1", branch_name="codex/T-1", status="queued")
            store.update_run(
                run.id,
                status="blocked",
                blocked_phase="planning_approval",
                final_message=plan_content,
            )
            plan_path = root / "workspaces" / "T-1" / ".symphony" / "codex-plan.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(plan_content, encoding="utf-8")
            requirements_path = (
                root
                / "workspaces"
                / "T-1"
                / ".symphony"
                / "requirements-snapshots"
                / f"{snapshot.content_hash}.json"
            )
            requirements_path.parent.mkdir(parents=True)
            requirements_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")

            state = build_state(workflow, store)
            html = render_dashboard_html(state)

            dashboard_run = state["all_runs"][0]
            self.assertEqual(dashboard_run["plan_content"], plan_content)
            self.assertEqual(dashboard_run["requirements_path"], str(requirements_path))
            self.assertIn("Goal: Show the concise planning result", dashboard_run["plan_summary"])
            self.assertIn(
                "5 requirements and 1 acceptance criterion",
                dashboard_run["requirements_summary"],
            )
            self.assertIn("<th>Requirements Spec</th>", html)
            self.assertIn("Full plan file", html)
            self.assertIn("Full requirements file", html)
            self.assertIn(
                "Scope: 1 requirement, 1 acceptance criterion, 1 test across foyr2.",
                html,
            )
            self.assertIn("+2 more requirements; open the full file.", html)
            self.assertIn("Sources: Description, Acceptance Criteria.", html)
            self.assertIn("Plan ready for approval. See the brief Plan summary.", html)
            self.assertNotIn("RAW-PLAN-DETAIL-SENTINEL", html)
            self.assertNotIn("RAW-REQUIREMENTS-DETAIL-SENTINEL", html)
            self.assertNotIn("Show plan", html)
            self.assertNotIn("<script>", html)
            self.assertIn("&lt;script&gt;alert(&#x27;plan&#x27;)&lt;/script&gt;", html)
            self.assertIn(
                "&lt;script&gt;alert(&#x27;requirements&#x27;)&lt;/script&gt;",
                html,
            )
            self.assertNotIn("Requirement statement 4", html)

            tampered_payload = json.loads(plan_content)
            tampered_payload["simplest_implementation"] = (
                "TAMPERED-VALID-PLAN-CONTENT"
            )
            plan_path.write_text(
                json.dumps(tampered_payload, indent=2),
                encoding="utf-8",
            )
            tampered_state = build_state(workflow, store)
            tampered_html = render_dashboard_html(tampered_state)

            self.assertIn(
                "could not be validated for this run",
                tampered_state["all_runs"][0]["plan_summary"],
            )
            self.assertNotIn("TAMPERED-VALID-PLAN-CONTENT", tampered_html)
            self.assertNotIn("Plan ready for approval", tampered_html)
            self.assertIn(
                "Plan details could not be validated for this run.",
                tampered_html,
            )

    def test_running_phase_uses_latest_event_prefix_not_existing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            latest_events = {
                "T-PLAN": ("plan.item.started", "Development Planning"),
                "T-REVIEW": ("review.item.started", "Development Review"),
                "T-AUTO-PLAN": (
                    "automation_planning.item.started",
                    "Automation Planning",
                ),
                "T-AUTO-IMPL": (
                    "automation_implementation.item.started",
                    "Automation Implementation",
                ),
                "T-DEV-IMPL": (
                    "development_implementation.item.started",
                    "Development Implementation",
                ),
                "T-LEGACY-IMPL": (
                    "item.started",
                    "Development Implementation",
                ),
                "T-SETUP": (None, "setup"),
            }
            for index, (identifier, (event_type, _)) in enumerate(
                latest_events.items(), start=1
            ):
                issue = Issue(
                    id=str(index),
                    identifier=identifier,
                    title=identifier,
                    status="To Do",
                    url=f"https://jira.example.test/browse/{identifier}",
                )
                workspace_path = root / "workspaces" / identifier
                run = store.create_run(
                    issue,
                    workspace_path,
                    branch_name=None,
                    status="running",
                )
                stale_review_path = workspace_path / ".symphony" / "codex-review.md"
                stale_review_path.parent.mkdir(parents=True)
                stale_review_path.write_text("stale review", encoding="utf-8")
                if event_type:
                    store.add_codex_event(
                        run.id,
                        1,
                        "plan.item.completed",
                        {"type": "item.completed"},
                    )
                    store.add_codex_event(
                        run.id,
                        2,
                        event_type,
                        {"type": "item.started"},
                    )

            runs_by_issue = {
                run["issue_identifier"]: run
                for run in build_state(workflow, store)["all_runs"]
            }
            for identifier, (_, expected_phase) in latest_events.items():
                self.assertEqual(
                    runs_by_issue[identifier]["current_phase"],
                    expected_phase,
                )
            self.assertEqual(
                runs_by_issue["T-AUTO-IMPL"]["workflow_progress"],
                (
                    "Development Planning: done → Dev Approval: not required → "
                    "Development Implementation: done → "
                    "Development Review: not required → "
                    "Automation Planning: done → "
                    "Automation Approval: not required → "
                    "Automation Implementation: running → "
                    "Automation Review: not required"
                ),
            )
            html = render_dashboard_html(build_state(workflow, store))
            self.assertIn(
                "Development Planning: done → Dev Approval: not required",
                html,
            )

    def test_automation_artifacts_are_enriched_and_safely_summarized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            result_content = (
                "Automation completed <script>alert('result')</script>. "
                + ("x" * 400)
                + " RAW-AUTOMATION-RESULT-TAIL"
            )
            (
                run,
                workspace_path,
                plan_content,
                automation_plan_content,
            ) = create_bound_automation_dashboard_run(
                root,
                workflow,
                store,
                automation_result=result_content,
            )
            plan_path = workspace_path / workflow.config.automation.output_plan_file
            result_path = (
                workspace_path / workflow.config.automation.output_result_file
            )

            state = build_state(workflow, store)
            html = render_dashboard_html(state)
            dashboard_run = state["all_runs"][0]

            self.assertTrue(state["automation_enabled"])
            self.assertEqual(dashboard_run["automation_plan_path"], str(plan_path))
            self.assertTrue(dashboard_run["automation_plan_exists"])
            self.assertEqual(
                dashboard_run["automation_plan_content"],
                automation_plan_content,
            )
            self.assertIn(
                "Decision: update required.",
                dashboard_run["automation_plan_summary"],
            )
            self.assertIn(
                "Scope: 1 scenario, 1 file change, 1 verification step.",
                dashboard_run["automation_plan_summary"],
            )
            self.assertEqual(
                dashboard_run["automation_result_path"],
                str(result_path),
            )
            self.assertTrue(dashboard_run["automation_result_exists"])
            self.assertEqual(
                dashboard_run["automation_result_content"],
                result_content,
            )
            self.assertLess(
                len(dashboard_run["automation_result_summary"]),
                len(result_content),
            )
            self.assertEqual(html.count("<th>Automation</th>"), 1)
            self.assertIn("Automation plan file", html)
            self.assertIn("codex-automation-plan.md", html)
            self.assertIn("Automation result file", html)
            self.assertIn("codex-automation-final.md", html)
            self.assertNotIn("<script>", html)
            self.assertIn(
                "&lt;script&gt;alert(&#x27;automation&#x27;)&lt;/script&gt;",
                html,
            )
            self.assertIn(
                "&lt;script&gt;alert(&#x27;result&#x27;)&lt;/script&gt;",
                html,
            )
            self.assertNotIn("RAW-AUTOMATION-PLAN-DETAIL", html)
            self.assertNotIn("RAW-AUTOMATION-RESULT-TAIL", html)

            plan_path.write_text('{"not": "an automation plan"}', encoding="utf-8")
            malformed_state = build_state(workflow, store)
            malformed_html = render_dashboard_html(malformed_state)
            self.assertFalse(
                malformed_state["all_runs"][0]["automation_plan_exists"]
            )
            self.assertFalse(
                malformed_state["all_runs"][0]["automation_result_exists"]
            )
            self.assertIn(
                "could not be validated for this run",
                malformed_html,
            )
            self.assertNotIn('{"not": "an automation plan"}', malformed_html)

    def test_historical_automation_artifacts_remain_visible_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            create_bound_automation_dashboard_run(
                root,
                workflow,
                store,
                automation_result="Bound historical automation result",
            )
            workflow.config.automation.enabled = False

            state = build_state(workflow, store)
            html = render_dashboard_html(state)

            self.assertTrue(state["automation_enabled"])
            self.assertTrue(state["all_runs"][0]["automation_plan_exists"])
            self.assertTrue(state["all_runs"][0]["automation_result_exists"])
            self.assertIn("<th>Automation</th>", html)
            self.assertIn("Bound historical automation result", html)

    def test_running_automation_implementation_shows_bound_plan_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            _, workspace_path, _, _ = create_bound_automation_dashboard_run(
                root,
                workflow,
                store,
                automation_result="Implementation has not completed.",
                implementation_complete=False,
            )
            automation_test = (
                workspace_path / "automation" / "src" / "test" / "DashboardTest.java"
            )
            automation_test.parent.mkdir(parents=True, exist_ok=True)
            automation_test.write_text(
                "final class DashboardTest { /* implementation in progress */ }\n",
                encoding="utf-8",
            )

            state = build_state(workflow, store)
            html = render_dashboard_html(state)
            dashboard_run = state["all_runs"][0]

            self.assertEqual(
                dashboard_run["current_phase"],
                "Automation Implementation",
            )
            self.assertTrue(dashboard_run["automation_plan_exists"])
            self.assertFalse(dashboard_run["automation_result_exists"])
            self.assertIn(
                "Automation Planning: done → Automation Approval: not required → "
                "Automation Implementation: running",
                dashboard_run["workflow_progress"],
            )
            self.assertIn("Automation plan file", html)
            self.assertIn("Automation Implementation: running", html)

    def test_blocked_automation_same_scope_repository_drift_rejects_bound_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            run, workspace_path, _, _ = create_bound_automation_dashboard_run(
                root,
                workflow,
                store,
                automation_result="Implementation completed.",
            )
            store.update_run(
                run.id,
                status="blocked",
                blocked_phase="automation_implementation",
                automation_result_hash=None,
            )
            automation_test = (
                workspace_path / "automation" / "src" / "test" / "DashboardTest.java"
            )
            automation_test.write_text(
                "final class DashboardTest { /* changed after block */ }\n",
                encoding="utf-8",
            )

            dashboard_run = build_state(workflow, store)["all_runs"][0]

            self.assertFalse(dashboard_run["automation_plan_exists"])
            self.assertIn(
                "artifact could not be validated",
                dashboard_run["automation_plan_summary"],
            )

    def test_historical_completed_run_does_not_claim_automation_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            create_completed_dashboard_run(root, store)

            dashboard_run = build_state(workflow, store)["all_runs"][0]

            self.assertIn(
                "Development Implementation: done",
                dashboard_run["workflow_progress"],
            )
            self.assertIn(
                "Automation Planning: pending",
                dashboard_run["workflow_progress"],
            )
            self.assertIn(
                "Automation Implementation: pending",
                dashboard_run["workflow_progress"],
            )
            self.assertNotIn(
                "Automation Planning: done",
                dashboard_run["workflow_progress"],
            )

    def test_invalid_automation_result_suppresses_human_review_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            run, workspace_path, _, _ = create_bound_automation_dashboard_run(
                root,
                workflow,
                store,
                automation_result="Trusted automation result",
            )
            result_path = (
                workspace_path / workflow.config.automation.output_result_file
            )
            result_path.write_text("Tampered automation result", encoding="utf-8")

            state = build_state(workflow, store)
            html = render_dashboard_html(state)
            dashboard_run = state["all_runs"][0]

            self.assertFalse(dashboard_run["automation_result_exists"])
            self.assertFalse(dashboard_run["human_review_actionable"])
            self.assertNotIn(
                f'<form method="post" action="/api/v1/runs/{run.id}/human-review">',
                html,
            )
            self.assertNotIn("Address Human Review", html)
            self.assertIn("Automation result unavailable", html)
            self.assertIn("Human review is disabled", html)
            self.assertNotIn("Tampered automation result", html)

    def test_dashboard_uses_run_hash_and_never_pairs_noop_with_stale_update_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            old_run, workspace_path, _, _ = create_bound_automation_dashboard_run(
                root,
                workflow,
                store,
                automation_result="STALE PRIOR UPDATE REPORT",
            )
            (
                workspace_path
                / "automation"
                / "src"
                / "test"
                / "DashboardTest.java"
            ).unlink()
            new_run, _, _, _ = create_bound_automation_dashboard_run(
                root,
                workflow,
                store,
                decision="no_update_required",
                automation_result=(
                    "No automation update was required; existing coverage is sufficient."
                ),
            )

            runs = {
                item["id"]: item for item in build_state(workflow, store)["all_runs"]
            }

            self.assertFalse(runs[old_run.id]["automation_plan_exists"])
            self.assertFalse(runs[old_run.id]["automation_result_exists"])
            self.assertTrue(runs[new_run.id]["automation_plan_exists"])
            self.assertTrue(runs[new_run.id]["automation_result_exists"])
            self.assertIn(
                "No automation update was required",
                runs[new_run.id]["automation_result_content"],
            )
            self.assertNotIn(
                "STALE PRIOR UPDATE REPORT",
                runs[new_run.id]["automation_result_content"],
            )

    def test_dashboard_never_pairs_same_plan_run_with_newer_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            old_run, _, _, _ = create_bound_automation_dashboard_run(
                root,
                workflow,
                store,
                automation_result="FIRST RUN RESULT",
            )
            new_run, _, _, _ = create_bound_automation_dashboard_run(
                root,
                workflow,
                store,
                automation_result="SECOND RUN RESULT",
            )
            self.assertEqual(
                old_run.automation_plan_hash,
                new_run.automation_plan_hash,
            )

            runs = {
                item["id"]: item for item in build_state(workflow, store)["all_runs"]
            }

            self.assertTrue(runs[old_run.id]["automation_plan_exists"])
            self.assertFalse(runs[old_run.id]["automation_result_exists"])
            self.assertIsNone(runs[old_run.id]["automation_result_content"])
            self.assertTrue(runs[new_run.id]["automation_result_exists"])
            self.assertEqual(
                runs[new_run.id]["automation_result_content"],
                "SECOND RUN RESULT",
            )

    def test_dashboard_shows_bound_plan_during_partial_automation_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            run, workspace_path, development_plan_content, _ = (
                create_bound_automation_dashboard_run(
                    root,
                    workflow,
                    store,
                    automation_result="Prior completed result",
                )
            )
            (
                workspace_path
                / "automation"
                / "src"
                / "test"
                / "DashboardTest.java"
            ).unlink()
            snapshot = dashboard_requirements_snapshot()
            development_plan = parse_plan_spec(
                development_plan_content,
                expected_issue_key="T-1",
                expected_snapshot_hash=snapshot.content_hash,
                requirements_snapshot=snapshot,
            )
            repository_diff = capture_automation_repository_diff(
                workspace_path,
                development_plan,
                workflow.config,
            )
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="automation_planning",
                automation_repository_diff_hash=repository_diff.content_hash,
                automation_result_hash=None,
            )

            dashboard_run = {
                item["id"]: item
                for item in build_state(workflow, store)["all_runs"]
            }[run.id]

            self.assertTrue(dashboard_run["automation_plan_exists"])
            self.assertFalse(dashboard_run["automation_result_exists"])
            self.assertIsNone(dashboard_run["automation_result_content"])

    def test_human_review_context_freezes_bound_automation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            run, workspace_path, _, automation_plan_content = (
                create_bound_automation_dashboard_run(
                    root,
                    workflow,
                    store,
                    decision="no_update_required",
                    automation_result=(
                        "No automation update was required; existing coverage is sufficient."
                    ),
                )
            )

            context = prepare_human_review_context(run, workflow, store)

            self.assertEqual(
                context["automation_plan_hash"],
                run.automation_plan_hash,
            )
            self.assertEqual(
                context["automation_plan"],
                automation_plan_content,
            )
            self.assertIn(
                "No automation update was required",
                context["automation_result"],
            )

            automation_plan_path = (
                workspace_path / workflow.config.automation.output_plan_file
            )
            automation_plan_path.write_text(
                automation_plan_content.replace(
                    "Existing automation already covers the behavior.",
                    "Tampered rationale.",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "trusted hash",
            ):
                prepare_human_review_context(run, workflow, store)

    def test_automation_plan_approval_uses_exact_frozen_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.codex.require_plan_approval = True
            workflow.config.codex.review_after_run = True
            workflow.config.automation.enabled = True
            workflow.config.automation.require_plan_approval = True
            workflow.config.automation.review_after_run = True
            store = Store(root / "db.sqlite3")
            run, _, _, development_approval = (
                create_automation_planning_approval_dashboard_run(
                    root,
                    workflow,
                    store,
                )
            )
            assert development_approval is not None

            state = build_state(workflow, store)
            dashboard_run = state["all_runs"][0]
            html = render_dashboard_html(state)

            self.assertEqual(
                dashboard_run["current_phase"],
                "Automation Approval",
            )
            for label in (
                "Development Planning",
                "Dev Approval",
                "Development Implementation",
                "Development Review",
                "Automation Planning",
                "Automation Approval",
                "Automation Implementation",
                "Automation Review",
            ):
                self.assertIn(label, dashboard_run["workflow_progress"])
            self.assertIn("Automation Approval: awaiting approval", html)
            self.assertIn("Approve the exact validated AutomationPlan", html)
            self.assertIn(
                'name="action" value="approve_automation_plan"',
                html,
            )
            self.assertIn("Approve Exact Automation Plan", html)
            self.assertIn('name="approver_identity" required', html)

            with patch(
                "symphony_jira.dashboard.prepare_bound_automation_context",
                wraps=prepare_bound_automation_context,
            ) as validate_automation_context:
                response = TestClient(create_app(workflow, store)).post(
                    f"/api/v1/runs/{run.id}/human-input",
                    json={
                        "action": "approve_automation_plan",
                        "approver_identity": " automation-reviewer@example.test ",
                    },
                )

            self.assertEqual(response.status_code, 200, response.text)
            validate_automation_context.assert_called_once()
            self.assertFalse(
                validate_automation_context.call_args.kwargs["require_result"]
            )
            self.assertTrue(
                validate_automation_context.call_args.kwargs["allow_partial_scope"]
            )
            payload = response.json()["human_input"]
            self.assertEqual(payload["action"], "automation_plan_approval")
            self.assertEqual(
                payload["approver_identity"],
                "automation-reviewer@example.test",
            )
            self.assertEqual(
                payload["automation_plan_hash"],
                run.automation_plan_hash,
            )
            self.assertEqual(
                payload["requirements_snapshot_hash"],
                run.issue_fingerprint,
            )
            self.assertEqual(
                payload["development_plan_spec_hash"],
                run.plan_spec_hash,
            )
            self.assertEqual(
                payload["development_plan_approval_id"],
                development_approval["id"],
            )
            self.assertEqual(
                payload["development_workspace_diff_hash"],
                run.automation_development_diff_hash,
            )
            self.assertEqual(
                payload["automation_repository_diff_hash"],
                run.automation_repository_diff_hash,
            )
            persisted = store.latest_automation_plan_approval_for_run(
                run.id,
                active_only=True,
            )
            self.assertIsNotNone(persisted)
            self.assertEqual(
                store.get_run(run.id).automation_plan_approval_id,
                persisted["id"],
            )

    def test_automation_plan_approval_rejects_tampered_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            workflow.config.automation.require_plan_approval = True
            store = Store(root / "db.sqlite3")
            run, workspace_path, automation_plan_content, _ = (
                create_automation_planning_approval_dashboard_run(
                    root,
                    workflow,
                    store,
                )
            )
            automation_plan_path = (
                workspace_path / workflow.config.automation.output_plan_file
            )
            automation_plan_path.write_text(
                automation_plan_content.replace(
                    "existing focused test suite",
                    "a tampered test suite",
                ),
                encoding="utf-8",
            )

            response = TestClient(create_app(workflow, store)).post(
                f"/api/v1/runs/{run.id}/human-input",
                json={
                    "action": "approve_automation_plan",
                    "approver_identity": "reviewer@example.test",
                },
            )

            self.assertEqual(response.status_code, 409)
            self.assertIn("cannot be approved", response.json()["detail"])
            self.assertEqual(
                store.list_automation_plan_approvals(run_id=run.id),
                [],
            )
            self.assertEqual(store.list_human_inputs(run_id=run.id), [])

    def test_disabled_automation_approval_gate_has_no_approval_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            workflow.config.automation.require_plan_approval = False
            store = Store(root / "db.sqlite3")
            run, _, _, _ = create_automation_planning_approval_dashboard_run(
                root,
                workflow,
                store,
            )

            html = render_dashboard_html(build_state(workflow, store))
            response = TestClient(create_app(workflow, store)).post(
                f"/api/v1/runs/{run.id}/human-input",
                json={
                    "action": "approve_automation_plan",
                    "approver_identity": "reviewer@example.test",
                },
            )

            self.assertNotIn('value="approve_automation_plan"', html)
            self.assertIn("approval gate is disabled", html)
            self.assertIn("Automation Approval: not required", html)
            self.assertEqual(response.status_code, 409)
            self.assertIn("not enabled", response.json()["detail"])

    def test_historical_blocked_run_renders_no_human_input_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                url="https://jira.example.test/browse/T-1",
            )
            historical = store.create_run(
                issue,
                root / "workspaces" / "T-1",
                branch_name=None,
            )
            historical = store.update_run(
                historical.id,
                status="blocked",
                blocked_phase="implementation",
                error="Old clarification",
            )
            time.sleep(0.01)
            store.create_run(
                issue,
                root / "workspaces" / "T-1",
                branch_name=None,
            )

            state = build_state(workflow, store)
            html = render_dashboard_html(state)
            historical_dashboard_run = next(
                item for item in state["all_runs"] if item["id"] == historical.id
            )

            self.assertFalse(historical_dashboard_run["human_input_actionable"])
            self.assertNotIn(
                f'/api/v1/runs/{historical.id}/human-input',
                html,
            )

    def test_dashboard_shows_separate_review_artifacts_and_histories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.codex.review_after_run = True
            workflow.config.automation.enabled = True
            workflow.config.automation.review_after_run = True
            store = Store(root / "db.sqlite3")
            run = create_completed_dashboard_run(root, store)
            workspace_path = Path(run.workspace_path)
            artifact_contents = (
                (
                    workflow.config.codex.output_review_file,
                    "Development review approved.",
                ),
                (
                    workflow.config.codex.output_review_history_file,
                    "Development review history.",
                ),
                (
                    workflow.config.automation.output_review_file,
                    "Automation review approved.",
                ),
                (
                    workflow.config.automation.output_review_history_file,
                    "Automation review history.",
                ),
            )
            for relative_path, content in artifact_contents:
                path = workspace_path / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            state = build_state(workflow, store)
            dashboard_run = state["all_runs"][0]
            html = render_dashboard_html(state)

            self.assertTrue(dashboard_run["development_review_exists"])
            self.assertTrue(dashboard_run["development_review_history_exists"])
            self.assertTrue(dashboard_run["automation_review_exists"])
            self.assertTrue(dashboard_run["automation_review_history_exists"])
            self.assertIn("<strong>Development Review</strong>", html)
            self.assertIn("<strong>Automation Review</strong>", html)
            for relative_path, content in artifact_contents:
                self.assertIn(relative_path, html)
                self.assertIn(content, html)

    def test_blocked_automation_phase_uses_standard_resume_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            workflow.config.automation.enabled = True
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                url="https://jira.example.test/browse/T-1",
            )
            run = store.create_run(
                issue,
                root / "workspaces" / "T-1",
                branch_name=None,
            )
            store.update_run(
                run.id,
                status="blocked",
                blocked_phase="automation_planning",
                error="Which automated scenario applies?",
            )

            html = render_dashboard_html(build_state(workflow, store))

            self.assertIn("<th>Automation</th>", html)
            self.assertIn("Which automated scenario applies?", html)
            self.assertIn("<button type=\"submit\">Resume</button>", html)
            self.assertNotIn("Approve Exact Plan", html)

    def test_dashboard_still_expands_long_non_planning_final_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                url="https://jira.example.test/browse/T-1",
            )
            run = store.create_run(
                issue,
                root / "workspaces" / "T-1",
                branch_name="codex/T-1",
            )
            long_final = "Implementation summary\n" + ("x" * 1200) + "FULL-END"
            store.update_run(run.id, status="completed", final_message=long_final)

            html = render_dashboard_html(build_state(workflow, store))

            self.assertIn("<details", html)
            self.assertIn("Show full final message", html)
            self.assertIn("FULL-END", html)

    def test_dashboard_survives_corrupt_stored_requirements_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            snapshot = dashboard_requirements_snapshot()
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                url="https://jira.example.test/browse/T-1",
                requirements_snapshot=snapshot,
            )
            store.create_run(issue, root / "workspaces" / "T-1", branch_name=None)
            with sqlite3.connect(store.db_path) as conn:
                row = conn.execute(
                    "SELECT snapshot_json FROM requirements_snapshots"
                ).fetchone()
                assert row is not None
                payload = json.loads(row[0])
                payload["issue_identifier"] = "T-2"
                conn.execute(
                    "UPDATE requirements_snapshots SET snapshot_json = ?",
                    (json.dumps(payload),),
                )

            state = build_state(workflow, store)
            html = render_dashboard_html(state)

            self.assertIn(
                "failed integrity validation",
                state["all_runs"][0]["requirements_summary"],
            )
            self.assertIn("failed integrity validation", html)
            self.assertIn("Expected requirements file (not present)", html)

    def test_blocked_run_dashboard_shows_human_input_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            run = store.create_run(issue, root / "workspaces" / "T-1", branch_name="codex/T-1", status="queued")
            blocked = store.update_run(
                run.id,
                status="blocked",
                error="Which repo should change?",
                blocked_phase="planning_approval",
            )

            html = render_dashboard_html(build_state(workflow, store))
            self.assertIn("Approve the exact validated PlanSpec", html)
            self.assertIn("Approve Exact Plan", html)
            self.assertIn('name="action" value="approve"', html)
            self.assertIn('name="approver_identity" required', html)
            self.assertIn("Request Adjustments", html)
            self.assertIn("Dev Approval", html)
            self.assertNotIn("Which repo should change?", html)
            row = html.split(f"/api/v1/runs/{blocked.id}/human-input", 1)[0].rsplit("<tr>", 1)[1]
            self.assertIn("<td>Dev Approval</td>", row)
            self.assertNotIn("<td>blocked</td>", row)
            self.assertIn(f"/api/v1/runs/{blocked.id}/human-input", html)

            store.add_human_input("T-1", run_id=blocked.id, response="Change foyr2 only.")
            state = build_state(workflow, store)
            html = render_dashboard_html(state)
            self.assertEqual(state["blocked_issues"], [])
            self.assertIn("queued for resume", html)
            self.assertIn("Change foyr2 only.", html)

    def test_failed_verification_dashboard_enqueues_explicit_handoff_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            run = store.create_run(
                issue,
                root / "workspaces" / "T-1",
                branch_name="codex/T-1",
            )
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="verification",
                verification_status="test_failed",
                verification_output_path=str(root / "verification.json"),
                verification_workspace_diff_hash="a" * 64,
                verification_evidence_sha256="b" * 64,
                error="Tests failed",
            )

            html = render_dashboard_html(build_state(workflow, store))
            self.assertIn(
                "Approve Test/Runtime Override and Continue to Review",
                html,
            )
            self.assertIn("Original status: <code>test_failed</code>", html)
            self.assertIn(str(root / "verification.json"), html)
            self.assertNotIn("Bypass Verification and Hand Off", html)
            self.assertIn('name="action" value="bypass_verification"', html)
            self.assertIn('name="approver_identity" required', html)
            self.assertIn("Retry Verification", html)

            client = TestClient(create_app(workflow, store))
            missing_identity = client.post(
                f"/api/v1/runs/{run.id}/human-input",
                json={"action": "bypass_verification"},
            )
            with patch(
                "symphony_jira.dashboard.prepare_verification_bypass_context",
                return_value={
                    "workspace_diff_hash": "a" * 64,
                    "verification_evidence_sha256": "b" * 64,
                },
            ) as prepare_context:
                response = client.post(
                    f"/api/v1/runs/{run.id}/human-input",
                    json={
                        "action": "bypass_verification",
                        "approver_identity": "operator@example.test",
                    },
                )

            self.assertEqual(missing_identity.status_code, 400)
            self.assertEqual(
                missing_identity.json()["detail"],
                "approver identity is required",
            )
            self.assertEqual(response.status_code, 200)
            prepare_context.assert_called_once_with(run, workflow, store)
            pending = store.list_human_inputs(run_id=run.id)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["action"], "verification_bypass")
            self.assertEqual(
                pending[0]["approver_identity"],
                "operator@example.test",
            )
            self.assertEqual(pending[0]["workspace_diff_hash"], "a" * 64)
            self.assertEqual(
                pending[0]["verification_evidence_sha256"],
                "b" * 64,
            )

    def test_legacy_failed_verification_requires_retry_before_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            run = store.create_run(
                issue,
                root / "workspaces" / "T-1",
                branch_name=None,
            )
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="verification_environment",
                verification_status="environment_blocked",
                verification_output_path=str(root / "legacy-verification.json"),
            )

            html = render_dashboard_html(build_state(workflow, store))

            self.assertIn("predates verification-time integrity binding", html)
            self.assertIn("Retry Verification", html)
            self.assertNotIn(
                "Approve Test/Runtime Override and Continue to Review",
                html,
            )

            with patch(
                "symphony_jira.dashboard.prepare_verification_bypass_context",
                return_value={
                    "workspace_diff_hash": "a" * 64,
                    "verification_evidence_sha256": "b" * 64,
                },
            ):
                response = TestClient(create_app(workflow, store)).post(
                    f"/api/v1/runs/{run.id}/human-input",
                    json={
                        "action": "bypass_verification",
                        "approver_identity": "operator@example.test",
                    },
                )

            self.assertEqual(response.status_code, 409)
            self.assertIn(
                "no valid verification-time integrity binding",
                response.json()["detail"],
            )
            self.assertEqual(store.list_human_inputs(run_id=run.id), [])

    def test_verification_bypass_context_hashes_exact_diff_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            snapshot = dashboard_requirements_snapshot()
            workspace_path = root / "workspaces" / "T-1"
            repositories = {
                name: workspace_path / name
                for name in ("foyr2", "cpm")
            }
            for repository_path in repositories.values():
                repository_path.mkdir(parents=True)
                subprocess.run(
                    ["git", "init", "-q", str(repository_path)],
                    check=True,
                )
                (repository_path / "baseline.txt").write_text(
                    "baseline\n",
                    encoding="utf-8",
                )
                subprocess.run(
                    ["git", "-C", str(repository_path), "add", "baseline.txt"],
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository_path),
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.test",
                        "commit",
                        "-q",
                        "-m",
                        "baseline",
                    ],
                    check=True,
                )
            workflow.config.runtime.repositories = {
                name: RuntimeRepositoryConfig(
                    workspace_subdir=Path(name),
                    source_env=f"{name.upper()}_SRC",
                    service=name,
                    mount_target=f"/{name}",
                    verification_profile="tests",
                )
                for name in repositories
            }
            baseline_sha = subprocess.run(
                ["git", "-C", str(repositories["foyr2"]), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            plan_payload = dashboard_plan_payload(snapshot.content_hash)
            plan_payload["baseline_repository_shas"] = [
                {"repository": "foyr2", "sha": baseline_sha}
            ]
            plan_content = json.dumps(plan_payload, indent=2)
            plan_spec = parse_plan_spec(
                plan_content,
                expected_issue_key="T-1",
                expected_snapshot_hash=snapshot.content_hash,
                requirements_snapshot=snapshot,
            )
            plan_hash = plan_spec.content_hash()
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url=snapshot.issue_url,
                requirements_snapshot=snapshot,
            )
            run = store.create_run(issue, workspace_path, branch_name="feature/T-1")
            evidence_path = workspace_path / ".symphony" / "runtime" / "verification.json"
            evidence_path.parent.mkdir(parents=True)
            runtime_log_path = evidence_path.with_name("foyr2-verify.log")
            runtime_log_content = b"runtime output\x00\xff\n"
            runtime_log_path.write_bytes(runtime_log_content)
            hook_log_path = workspace_path / ".symphony" / "hooks" / "verify.log"
            hook_log_content = b"hook output\x00\xff\n"
            hook_log_path.parent.mkdir(parents=True)
            hook_log_path.write_bytes(hook_log_content)
            evidence_content = json.dumps(
                {
                    "schema_version": "1.0",
                    "issue_identifier": "T-1",
                    "plan_spec_hash": plan_hash,
                    "affected_repositories": ["foyr2"],
                    "hook": {
                        "output_path": str(hook_log_path),
                        "output_sha256": hashlib.sha256(
                            hook_log_content
                        ).hexdigest(),
                    },
                    "runtime": {
                        "status": "test_failed",
                        "checks": [
                            {
                                "log_path": str(runtime_log_path),
                                "log_sha256": hashlib.sha256(
                                    runtime_log_content
                                ).hexdigest(),
                            }
                        ],
                    },
                },
                sort_keys=True,
            ) + "\n"
            evidence_path.write_text(evidence_content, encoding="utf-8")
            plan_path = workspace_path / workflow.config.codex.output_plan_file
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(plan_content, encoding="utf-8")
            for snapshot_path in (
                workspace_path / ".symphony" / "requirements-snapshot.json",
                workspace_path
                / ".symphony"
                / "requirements-snapshots"
                / f"{snapshot.content_hash}.json",
            ):
                snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                snapshot_path.write_text(
                    snapshot.model_dump_json(indent=2),
                    encoding="utf-8",
                )
            failed_diff_hash = capture_workspace_diff(
                workspace_path,
                plan_spec,
                managed_repositories=("foyr2", "cpm"),
            ).content_hash
            failed_evidence_hash = hashlib.sha256(
                evidence_content.encode("utf-8")
            ).hexdigest()
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="verification",
                plan_spec_hash=plan_hash,
                verification_status="test_failed",
                verification_output_path=str(evidence_path),
                verification_workspace_diff_hash=failed_diff_hash,
                verification_evidence_sha256=failed_evidence_hash,
            )

            context = prepare_verification_bypass_context(run, workflow, store)

            self.assertEqual(context["workspace_diff_hash"], failed_diff_hash)
            self.assertEqual(
                context["verification_evidence_sha256"],
                failed_evidence_hash,
            )

            runtime_log_path.write_bytes(b"rewritten runtime evidence\n")
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "runtime verification log changed after its manifest was written",
            ):
                prepare_verification_bypass_context(run, workflow, store)
            runtime_log_path.write_bytes(runtime_log_content)

            hook_log_path.write_bytes(b"rewritten hook evidence\n")
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "verification hook log changed after its manifest was written",
            ):
                prepare_verification_bypass_context(run, workflow, store)
            hook_log_path.write_bytes(hook_log_content)

            (repositories["cpm"] / "baseline.txt").write_text(
                "changed after failed verification\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "workspace changed after the failed verification",
            ):
                prepare_verification_bypass_context(run, workflow, store)

            (repositories["cpm"] / "baseline.txt").write_text(
                "baseline\n",
                encoding="utf-8",
            )
            evidence_path.write_text(
                '{"status":"rewritten"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "evidence changed after the failed verification",
            ):
                prepare_verification_bypass_context(run, workflow, store)

    def test_verification_bypass_route_rejects_non_verification_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            run = store.create_run(issue, root / "workspace", branch_name=None)
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="implementation",
                verification_status="test_failed",
            )

            response = TestClient(create_app(workflow, store)).post(
                f"/api/v1/runs/{run.id}/human-input",
                json={
                    "action": "bypass_verification",
                    "approver_identity": "operator@example.test",
                },
            )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["detail"],
                "this run is not blocked by a failed verification",
            )

    def test_latest_completed_run_dashboard_shows_human_review_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            run = create_completed_dashboard_run(root, store)

            state = build_state(workflow, store)
            html = render_dashboard_html(state)

            self.assertTrue(state["all_runs"][0]["human_review_actionable"])
            self.assertIn("Address Human Review", html)
            self.assertIn(
                f'<form method="post" action="/api/v1/runs/{run.id}/human-review">',
                html,
            )
            self.assertIn('name="reviewer_identity" required', html)
            self.assertIn('name="source_url" type="url" required', html)
            self.assertIn('name="comments" required', html)

    def test_human_review_route_rejects_historical_and_non_completed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            historical = create_completed_dashboard_run(root, store)
            time.sleep(0.01)
            create_completed_dashboard_run(root, store)
            queued = store.create_run(
                Issue(
                    id="2",
                    identifier="T-2",
                    title="Queued",
                    status="To Do",
                    url="https://jira.example.test/browse/T-2",
                ),
                root / "workspaces" / "T-2",
                branch_name="codex/T-2",
            )
            client = TestClient(create_app(workflow, store))
            payload = {
                "reviewer_identity": "reviewer@example.test",
                "source_url": "https://github.example.test/org/repo/pull/42",
                "comments": "Please add a regression test.",
            }

            historical_response = client.post(
                f"/api/v1/runs/{historical.id}/human-review",
                json=payload,
            )
            queued_response = client.post(
                f"/api/v1/runs/{queued.id}/human-review",
                json=payload,
            )

            self.assertEqual(historical_response.status_code, 409)
            self.assertIn(
                "no longer the latest actionable run",
                historical_response.json()["detail"],
            )
            self.assertEqual(queued_response.status_code, 409)
            self.assertEqual(
                queued_response.json()["detail"],
                "human review can only be addressed from completed runs",
            )
            self.assertEqual(
                store.list_human_review_actions_for_issue("T-1"), []
            )
            self.assertEqual(
                store.list_human_review_actions_for_issue("T-2"), []
            )

    def test_human_review_route_validates_request_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            run = create_completed_dashboard_run(root, store)
            client = TestClient(create_app(workflow, store))
            route = f"/api/v1/runs/{run.id}/human-review"
            valid_payload = {
                "reviewer_identity": "reviewer@example.test",
                "source_url": "https://github.example.test/org/repo/pull/42",
                "comments": "Please add a regression test.",
            }

            malformed = client.post(
                route,
                content=b"{",
                headers={"content-type": "application/json"},
            )
            self.assertEqual(malformed.status_code, 400)
            self.assertEqual(
                malformed.json()["detail"],
                "request body must contain valid JSON",
            )

            non_object = client.post(route, json=["review comment"])
            self.assertEqual(non_object.status_code, 400)
            self.assertEqual(
                non_object.json()["detail"],
                "request body must be an object",
            )

            for field_name in ("reviewer_identity", "source_url", "comments"):
                with self.subTest(non_string_field=field_name):
                    payload = dict(valid_payload)
                    payload[field_name] = {"unexpected": "object"}
                    response = client.post(route, json=payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(
                        response.json()["detail"],
                        f"{field_name} must be a string",
                    )

            for field_name, expected_detail in (
                ("reviewer_identity", "reviewer identity is required"),
                ("source_url", "review source/PR link is required"),
                ("comments", "review comments are required"),
            ):
                with self.subTest(missing_field=field_name):
                    payload = dict(valid_payload)
                    del payload[field_name]
                    response = client.post(route, json=payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["detail"], expected_detail)

            invalid_url = client.post(
                route,
                json={**valid_payload, "source_url": "javascript:alert(1)"},
            )
            self.assertEqual(invalid_url.status_code, 400)
            self.assertEqual(
                invalid_url.json()["detail"],
                "review source/PR link must be an absolute HTTP(S) URL",
            )

            oversized = client.post(
                route,
                content=b"x" * (MAX_HUMAN_REVIEW_REQUEST_BYTES + 1),
                headers={"content-type": "application/json"},
            )
            self.assertEqual(oversized.status_code, 413)
            self.assertEqual(
                oversized.json()["detail"],
                "human review request is too large",
            )
            self.assertEqual(
                store.list_human_review_actions_for_source_run(run.id), []
            )
            self.assertEqual(len(store.list_runs_for_issue("T-1")), 1)

    def test_human_review_post_creates_redacted_get_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            source = create_completed_dashboard_run(root, store)
            client = TestClient(create_app(workflow, store))

            with patch(
                "symphony_jira.dashboard.prepare_human_review_context",
                return_value=minimal_human_review_context(),
            ) as prepare_context:
                response = client.post(
                    f"/api/v1/runs/{source.id}/human-review",
                    json={
                        "reviewer_identity": "  reviewer@example.test  ",
                        "source_url": (
                            "  https://github.example.test/org/repo/pull/42  "
                        ),
                        "comments": "  Please add a regression test.  ",
                    },
                )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["status"], "queued")
            self.assertNotIn("claim_token", payload["human_review"])
            self.assertNotIn("claim_token", json.dumps(payload))
            self.assertEqual(
                payload["human_review"]["source_run_id"], source.id
            )
            self.assertEqual(
                payload["human_review"]["reviewer_identity"],
                "reviewer@example.test",
            )
            self.assertEqual(
                payload["human_review"]["source_url"],
                "https://github.example.test/org/repo/pull/42",
            )
            self.assertEqual(
                payload["human_review"]["comments"],
                "Please add a regression test.",
            )
            result_run_id = payload["human_review"]["result_run_id"]
            self.assertEqual(payload["run"]["id"], result_run_id)
            self.assertEqual(payload["run"]["status"], "queued")
            self.assertEqual(payload["run"]["workspace_path"], source.workspace_path)
            self.assertEqual(payload["run"]["attempt"], source.attempt + 1)
            prepare_context.assert_called_once_with(source, workflow, store)

            action_id = payload["human_review"]["id"]
            claimed = store.claim_human_review_action(action_id)
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertTrue(claimed["claim_token"])

            source_detail = client.get(f"/api/v1/runs/{source.id}")
            result_detail = client.get(f"/api/v1/runs/{result_run_id}")

            self.assertEqual(source_detail.status_code, 200)
            self.assertEqual(result_detail.status_code, 200)
            source_payload = source_detail.json()
            result_payload = result_detail.json()
            self.assertEqual(
                source_payload["human_review_actions"][0]["id"], action_id
            )
            self.assertEqual(
                source_payload["human_review_actions"][0]["result_run_id"],
                result_run_id,
            )
            self.assertEqual(
                result_payload["human_review_action"]["id"], action_id
            )
            self.assertEqual(
                result_payload["human_review_action"]["source_run_id"],
                source.id,
            )
            self.assertNotIn("claim_token", json.dumps(source_payload))
            self.assertNotIn("claim_token", json.dumps(result_payload))

    def test_human_review_history_is_html_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            source = create_completed_dashboard_run(root, store)
            reviewer = 'Reviewer <script>alert("reviewer")</script>'
            source_url = (
                'https://review.example.test/pr/42?q=<tag>&quote="yes"'
            )
            comments = '</pre><script>alert("comments")</script>'

            with patch(
                "symphony_jira.dashboard.prepare_human_review_context",
                return_value=minimal_human_review_context(),
            ):
                response = TestClient(create_app(workflow, store)).post(
                    f"/api/v1/runs/{source.id}/human-review",
                    json={
                        "reviewer_identity": reviewer,
                        "source_url": source_url,
                        "comments": comments,
                    },
                )

            self.assertEqual(response.status_code, 200)
            html = render_dashboard_html(build_state(workflow, store))
            self.assertIn(
                "Reviewer &lt;script&gt;alert(&quot;reviewer&quot;)&lt;/script&gt;",
                html,
            )
            self.assertIn(
                "https://review.example.test/pr/42?q=&lt;tag&gt;"
                "&amp;quote=&quot;yes&quot;",
                html,
            )
            self.assertIn(
                "&lt;/pre&gt;&lt;script&gt;alert(&quot;comments&quot;)&lt;/script&gt;",
                html,
            )
            self.assertNotIn(reviewer, html)
            self.assertNotIn(source_url, html)
            self.assertNotIn(comments, html)

    def test_empty_plan_approval_submission_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            run = store.create_run(issue, root / "workspaces" / "T-1", branch_name=None)
            run = store.update_run(run.id, status="blocked", blocked_phase="planning_approval")

            response = TestClient(create_app(workflow, store)).post(
                f"/api/v1/runs/{run.id}/human-input",
                json={},
            )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"], "response is required")
            self.assertEqual(store.list_human_inputs(run_id=run.id), [])
            self.assertEqual(store.list_plan_approvals(run_id=run.id), [])

    def test_explicit_plan_approval_records_exact_binding_and_approver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            run = store.create_run(issue, root / "workspaces" / "T-1", branch_name=None)
            run = store.update_run(run.id, status="blocked", blocked_phase="planning_approval")
            plan_hash = "a" * 64

            with patch("symphony_jira.dashboard.current_plan_spec_hash", return_value=plan_hash):
                response = TestClient(create_app(workflow, store)).post(
                    f"/api/v1/runs/{run.id}/human-input",
                    json={"action": "approve", "approver_identity": "  ada@example.test  "},
                )

            self.assertEqual(response.status_code, 200)
            payload = response.json()["human_input"]
            self.assertEqual(payload["response"], "Approved.")
            self.assertEqual(payload["approver_identity"], "ada@example.test")
            self.assertEqual(payload["plan_spec_hash"], plan_hash)
            self.assertEqual(payload["requirements_snapshot_hash"], run.issue_fingerprint)
            self.assertTrue(payload["approved_at"])

            approval = store.latest_plan_approval_for_run(run.id, active_only=True)
            self.assertIsNotNone(approval)
            assert approval is not None
            self.assertEqual(payload["approval_id"], approval["id"])
            human_input = store.list_unconsumed_human_inputs()[0]
            self.assertEqual(human_input["approval_id"], approval["id"])
            self.assertEqual(human_input["approver_identity"], "ada@example.test")
            self.assertEqual(human_input["approved_at"], approval["approved_at"])

    def test_current_plan_hash_uses_stored_snapshot_and_original_planning_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            run = create_planning_approval_run(root, store)
            current = Mock()
            current.content_hash.return_value = "a" * 64
            original = Mock()
            original.content_hash.return_value = "a" * 64

            with patch(
                "symphony_jira.dashboard.validate_plan_repository_baselines",
                return_value=None,
            ) as baseline_validator, patch(
                "symphony_jira.dashboard.parse_plan_spec",
                side_effect=[current, original],
            ) as parser:
                plan_hash = current_plan_spec_hash(run, workflow, store)

            self.assertEqual(plan_hash, "a" * 64)
            self.assertEqual(parser.call_count, 2)
            baseline_validator.assert_called_once_with(
                current, Path(run.workspace_path), require_clean=True
            )
            self.assertEqual(parser.call_args_list[0].args[0], "current plan")
            self.assertEqual(parser.call_args_list[1].args[0], "validated plan")
            for call in parser.call_args_list:
                self.assertEqual(
                    call.kwargs["requirements_snapshot"].content_hash,
                    run.issue_fingerprint,
                )
                self.assertEqual(
                    call.kwargs["expected_snapshot_hash"],
                    run.issue_fingerprint,
                )

    def test_dashboard_rejects_tampered_requirements_snapshot_with_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            run = create_planning_approval_run(root, store)
            with sqlite3.connect(store.db_path) as conn:
                row = conn.execute("SELECT snapshot_json FROM requirements_snapshots").fetchone()
                assert row is not None
                payload = json.loads(row[0])
                payload["issue_identifier"] = "T-2"
                conn.execute("UPDATE requirements_snapshots SET snapshot_json = ?", (json.dumps(payload),))

            response = TestClient(create_app(workflow, store)).post(
                f"/api/v1/runs/{run.id}/human-input",
                json={
                    "action": "approve",
                    "approver_identity": "ada@example.test",
                },
            )

            self.assertEqual(response.status_code, 409)
            self.assertIn("integrity", response.json()["detail"])
            self.assertEqual(store.list_plan_approvals(run_id=run.id), [])
            self.assertEqual(store.list_human_inputs(run_id=run.id), [])

    def test_dashboard_rejects_edited_plan_instead_of_recording_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            run = create_planning_approval_run(root, store)
            edited = Mock()
            edited.content_hash.return_value = "a" * 64
            original = Mock()
            original.content_hash.return_value = "b" * 64

            with patch(
                "symphony_jira.dashboard.parse_plan_spec",
                side_effect=[edited, original],
            ):
                response = TestClient(create_app(workflow, store)).post(
                    f"/api/v1/runs/{run.id}/human-input",
                    json={
                        "action": "approve",
                        "approver_identity": "ada@example.test",
                    },
                )

            self.assertEqual(response.status_code, 409)
            self.assertIn(
                "differs from the exact validated PlanSpec produced by planning",
                response.json()["detail"],
            )
            self.assertEqual(store.list_plan_approvals(run_id=run.id), [])
            self.assertEqual(store.list_human_inputs(run_id=run.id), [])

    def test_plan_approval_is_invalidated_when_exact_binding_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = Issue(id="1", identifier="T-1", title="Title", status="To Do", url="u")
            run = store.create_run(issue, root / "workspace", branch_name=None)
            approval = store.add_plan_approval(
                "T-1",
                run_id=run.id,
                approver_identity="ada@example.test",
                plan_spec_hash="a" * 64,
                requirements_snapshot_hash="b" * 64,
            )

            exact = store.resolve_active_plan_approval(
                run.id,
                plan_spec_hash="a" * 64,
                requirements_snapshot_hash="b" * 64,
            )
            stale = store.resolve_active_plan_approval(
                run.id,
                plan_spec_hash="c" * 64,
                requirements_snapshot_hash="b" * 64,
            )

            self.assertEqual(exact, approval)
            self.assertIsNone(stale)
            invalidated = store.latest_plan_approval_for_run(run.id)
            self.assertIsNotNone(invalidated)
            assert invalidated is not None
            self.assertTrue(invalidated["invalidated_at"])
            self.assertEqual(invalidated["invalidation_reason"], "PlanSpec changed after approval")
            self.assertIsNone(store.latest_plan_approval_for_run(run.id, active_only=True))

    def test_create_run_persists_immutable_requirements_snapshot_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            snapshot = RequirementsSnapshot(
                issue_id="1",
                issue_identifier="T-1",
                issue_url="https://jira.example.test/browse/T-1",
            )
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                url=snapshot.issue_url,
                requirements_snapshot=snapshot,
            )

            run = store.create_run(issue, root / "workspace", branch_name=None)
            expected_hash = snapshot.calculate_content_hash()
            versions = store.list_requirements_snapshot_versions("T-1")
            loaded = store.get_requirements_snapshot("T-1", expected_hash)

            self.assertEqual(run.issue_fingerprint, expected_hash)
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]["content_hash"], expected_hash)
            self.assertEqual(versions[0]["schema_version"], snapshot.schema_version)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.content_hash, expected_hash)
            self.assertEqual(loaded.canonical_content(), snapshot.canonical_content())

            duplicate = store.save_requirements_snapshot(snapshot)
            self.assertEqual(duplicate["stored_at"], versions[0]["stored_at"])
            self.assertEqual(len(store.list_requirements_snapshot_versions("T-1")), 1)

            changed = snapshot.model_copy(update={"incomplete_reasons": ["new requirement source pending"]})
            changed_record = store.save_requirements_snapshot(changed)
            version_hashes = {
                version["content_hash"]
                for version in store.list_requirements_snapshot_versions("T-1")
            }
            self.assertEqual(version_hashes, {expected_hash, changed_record["content_hash"]})

    def test_issue_wide_invalidation_clears_only_matching_active_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = Issue(id="1", identifier="T-1", title="Title", status="To Do", url="u")
            other_issue = Issue(id="2", identifier="T-2", title="Other", status="To Do", url="u")
            runs = [
                store.create_run(issue, root / f"workspace-{index}", branch_name=None)
                for index in range(2)
            ]
            other_run = store.create_run(other_issue, root / "workspace-other", branch_name=None)
            for run in runs:
                store.add_plan_approval(
                    "T-1",
                    run_id=run.id,
                    approver_identity="ada@example.test",
                    plan_spec_hash="a" * 64,
                    requirements_snapshot_hash="b" * 64,
                )
            store.add_plan_approval(
                "T-2",
                run_id=other_run.id,
                approver_identity="grace@example.test",
                plan_spec_hash="c" * 64,
                requirements_snapshot_hash="d" * 64,
            )

            invalidated_count = store.invalidate_active_plan_approvals_for_issue(
                "T-1",
                "Jira requirements changed before review",
            )

            self.assertEqual(invalidated_count, 2)
            for run in runs:
                approval = store.latest_plan_approval_for_run(run.id)
                self.assertIsNotNone(approval)
                assert approval is not None
                self.assertTrue(approval["invalidated_at"])
                self.assertEqual(
                    approval["invalidation_reason"],
                    "Jira requirements changed before review",
                )
                self.assertIsNone(store.latest_plan_approval_for_run(run.id, active_only=True))
            self.assertIsNotNone(store.latest_plan_approval_for_run(other_run.id, active_only=True))

    def test_human_input_route_rejects_historical_blocked_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="u",
            )
            historical = store.create_run(
                issue,
                root / "workspaces" / "T-1",
                branch_name=None,
            )
            historical = store.update_run(historical.id, status="blocked")
            time.sleep(0.01)
            current = store.create_run(
                issue,
                root / "workspaces" / "T-1",
                branch_name=None,
            )
            store.update_run(current.id, status="blocked")

            response = TestClient(create_app(workflow, store)).post(
                f"/api/v1/runs/{historical.id}/human-input",
                json={"response": "Replay the old attempt"},
            )

            self.assertEqual(response.status_code, 409)
            self.assertIn("historical run", response.json()["detail"])
            self.assertEqual(store.list_human_inputs(run_id=historical.id), [])

    def test_human_input_route_rejects_second_pending_submission_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="u",
            )
            run = store.create_run(
                issue,
                root / "workspaces" / "T-1",
                branch_name=None,
            )
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="planning_approval",
            )
            store.add_human_input(
                "T-1",
                run_id=run.id,
                response="Please adjust the plan",
            )

            with patch("symphony_jira.dashboard.current_plan_spec_hash", return_value="a" * 64):
                response = TestClient(create_app(workflow, store)).post(
                    f"/api/v1/runs/{run.id}/human-input",
                    json={
                        "action": "approve",
                        "approver_identity": "ada@example.test",
                    },
                )

            self.assertEqual(response.status_code, 409)
            self.assertIn("already pending", response.json()["detail"])
            self.assertEqual(len(store.list_human_inputs(run_id=run.id)), 1)
            self.assertEqual(store.list_plan_approvals(run_id=run.id), [])

    def test_human_input_route_accepts_current_run_when_older_input_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"}
            )
            store = Store(root / "db.sqlite3")
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=["codex-ready"],
                url="u",
            )
            older = store.create_run(
                issue, root / "workspaces" / "T-1", branch_name=None
            )
            older = store.update_run(older.id, status="blocked")
            stale = store.add_human_input(
                "T-1",
                run_id=older.id,
                response="Clarification for the old attempt",
            )
            newer = store.create_run(
                issue, root / "workspaces" / "T-1", branch_name=None
            )
            newer = store.update_run(newer.id, status="blocked")

            response = TestClient(create_app(workflow, store)).post(
                f"/api/v1/runs/{newer.id}/human-input",
                json={"response": "Use the existing grid behavior"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["human_input"]["run_id"],
                newer.id,
            )
            self.assertIsNotNone(
                store.list_human_inputs(run_id=older.id)[0]["consumed_at"]
            )
            self.assertEqual(
                store.list_human_inputs(run_id=older.id)[0]["id"], stale["id"]
            )

    def test_dashboard_table_uses_newest_first_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            older_issue = Issue(id="1", identifier="T-1", title="Old", status="To Do", labels=["codex-ready"], url="u")
            newer_issue = Issue(id="2", identifier="T-2", title="New", status="To Do", labels=["codex-ready"], url="u")
            store.update_run(
                store.create_run(older_issue, root / "workspaces" / "T-1", branch_name=None).id,
                status="blocked",
                blocked_phase="planning_approval",
            )
            time.sleep(0.01)
            store.update_run(
                store.create_run(newer_issue, root / "workspaces" / "T-2", branch_name=None).id,
                status="completed",
            )

            html = render_dashboard_html(build_state(workflow, store))
            table_body = html.split("<tbody>", 1)[1]

            self.assertLess(table_body.index("T-2"), table_body.index("T-1"))

    def test_blocked_summary_ignores_old_blocked_runs_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
            store = Store(root / "db.sqlite3")
            issue = Issue(id="1", identifier="T-1", title="Title", status="To Do", labels=["codex-ready"], url="u")
            store.update_run(
                store.create_run(issue, root / "workspaces" / "T-1", branch_name=None).id,
                status="blocked",
                blocked_phase="planning_approval",
            )
            time.sleep(0.01)
            store.update_run(
                store.create_run(issue, root / "workspaces" / "T-1", branch_name=None).id,
                status="completed",
            )

            state = build_state(workflow, store)
            html = render_dashboard_html(state)

            self.assertEqual(state["blocked_issues"], [])
            self.assertIn("<strong>Blocked</strong><br>none", html)


def dashboard_requirements_snapshot() -> RequirementsSnapshot:
    description_source = RequirementSource(
        issue_identifier="T-1",
        source_type="description",
        source_id="description",
        author="product-owner",
        authority="product",
    )
    acceptance_source = RequirementSource(
        issue_identifier="T-1",
        source_type="custom_field",
        source_id="field:acceptance-criteria",
        field_name="Acceptance Criteria",
        author="product-owner",
        authority="product",
    )
    requirements = [
        RequirementDecision(
            id="decision:requirement-1",
            text="Use the existing flow <script>alert('requirements')</script>.",
            classification="current",
            sources=[description_source],
        )
    ]
    requirements.extend(
        RequirementDecision(
            id=f"decision:requirement-{index}",
            text=f"Requirement statement {index}",
            classification="current",
            sources=[description_source],
        )
        for index in range(2, 6)
    )
    requirements.append(
        RequirementDecision(
            id="decision:acceptance-1",
            text="The brief dashboard summary is visible.",
            kind="acceptance_criterion",
            classification="current",
            sources=[acceptance_source],
        )
    )
    return RequirementsSnapshot(
        issue_id="1",
        issue_identifier="T-1",
        issue_url="https://jira.example.test/browse/T-1",
        description=RequirementArtifact(
            artifact_id="description",
            source_type="description",
            text="Use the existing dashboard flow.",
            source=description_source,
        ),
        custom_fields=[
            RequirementArtifact(
                artifact_id="field:acceptance-criteria",
                source_type="custom_field",
                text="The brief dashboard summary is visible.",
                source=acceptance_source,
                kind="acceptance_criterion",
            )
        ],
        current_requirements=requirements,
        context_warnings=["RAW-REQUIREMENTS-DETAIL-SENTINEL"],
    ).with_content_hash()


def dashboard_plan_payload(snapshot_hash: str) -> dict[str, object]:
    source = {
        "issue_key": "T-1",
        "source_type": "description",
        "source_id": "description",
    }
    acceptance_source = {
        "issue_key": "T-1",
        "source_type": "custom_field",
        "source_id": "field:acceptance-criteria",
    }
    return {
        "schema_version": "1.0",
        "decision": "ready_for_approval",
        "issue_key": "T-1",
        "requirements_snapshot_hash": snapshot_hash,
        "baseline_repository_shas": [
            {
                "repository": "foyr2",
                "sha": "1234567890abcdef1234567890abcdef12345678",
            }
        ],
        "requirements": [
            {
                "id": "R-dashboard-summary",
                "statement": "Show the concise planning result on the dashboard.",
                "jira_sources": [source],
                "acceptance_criteria": [
                    {
                        "id": "AC-dashboard-summary",
                        "statement": "The summary is readable without opening JSON.",
                        "jira_sources": [acceptance_source],
                    }
                ],
            }
        ],
        "role_state_matrix": [],
        "affected_surface": {
            "repositories": ["foyr2"],
            "files": [],
            "apis": [],
            "schemas": [],
            "migrations": [],
            "translations": [],
        },
        "existing_precedents": [],
        "simplest_implementation": (
            "Use the existing path <script>alert('plan')</script> and keep scope tight."
        ),
        "assumptions": [],
        "non_goals": ["RAW-PLAN-DETAIL-SENTINEL"],
        "prohibited_scope": [],
        "test_cases": [
            {
                "id": "TC-dashboard-summary",
                "acceptance_criterion_id": "AC-dashboard-summary",
                "level": "unit",
                "description": "Render the planning dashboard row.",
                "expected_result": "Only the concise summary is embedded.",
            }
        ],
        "rollout": "Deploy with the dashboard service.",
        "rollback": "Revert the presentation-only change.",
        "compatibility": "Stored artifacts and API state remain unchanged.",
        "risks": [
            {
                "id": "risk-summary-loss",
                "severity": "low",
                "description": "Important detail could be hidden.",
                "mitigation": "Keep the exact full artifact path visible.",
            }
        ],
        "open_questions": [],
        "epic_strategy": None,
    }


def dashboard_automation_plan_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "decision": "update_required",
        "issue_key": "T-1",
        "requirements_snapshot_hash": "a" * 64,
        "development_plan_spec_hash": "b" * 64,
        "development_workspace_diff_hash": "c" * 64,
        "automation_repository": "automation",
        "repository_baseline_sha": "d" * 40,
        "rationale": (
            "Cover the new behavior <script>alert('automation')</script> using the "
            "existing focused test suite."
        ),
        "mapped_scenarios": [
            {
                "id": "scenario-dashboard",
                "description": "Exercise the new dashboard behavior.",
                "requirement_ids": ["R-dashboard-summary"],
                "acceptance_criterion_ids": ["AC-dashboard-summary"],
            }
        ],
        "affected_file_changes": [
            {
                "path": "src/test/DashboardTest.java",
                "change_type": "add",
                "description": "RAW-AUTOMATION-PLAN-DETAIL",
                "scenario_ids": ["scenario-dashboard"],
            }
        ],
        "verification": [
            {
                "id": "verify-dashboard",
                "command": "mvn test -Dtest=DashboardTest",
                "expected_result": "The focused scenario passes.",
                "scenario_ids": ["scenario-dashboard"],
            }
        ],
        "risks": [],
        "assumptions": [],
        "open_questions": [],
    }


def create_bound_automation_dashboard_run(
    root: Path,
    workflow,
    store: Store,
    *,
    decision: str = "update_required",
    automation_result: str,
    implementation_complete: bool = True,
    record_implementation_event: bool = True,
):
    snapshot = dashboard_requirements_snapshot()
    workspace_path = root / "workspaces" / "T-1"
    development_repository = workspace_path / "foyr2"
    automation_repository = workspace_path / "automation"
    development_sha = initialize_dashboard_git_repository(development_repository)
    automation_sha = initialize_dashboard_git_repository(automation_repository)

    development_payload = dashboard_plan_payload(snapshot.content_hash)
    development_payload["baseline_repository_shas"] = [
        {"repository": "foyr2", "sha": development_sha}
    ]
    development_plan_content = json.dumps(development_payload, indent=2)
    development_plan = parse_plan_spec(
        development_plan_content,
        expected_issue_key="T-1",
        expected_snapshot_hash=snapshot.content_hash,
        requirements_snapshot=snapshot,
    )
    (development_repository / "dashboard.py").write_text(
        "BOUND_AUTOMATION_TEST = True\n",
        encoding="utf-8",
    )
    development_diff = capture_workspace_diff(
        workspace_path,
        development_plan,
    )

    automation_payload = dashboard_automation_plan_payload()
    automation_payload.update(
        {
            "decision": decision,
            "requirements_snapshot_hash": snapshot.content_hash,
            "development_plan_spec_hash": development_plan.content_hash(),
            "development_workspace_diff_hash": development_diff.content_hash,
            "repository_baseline_sha": automation_sha,
        }
    )
    if decision == "no_update_required":
        automation_payload.update(
            {
                "rationale": "Existing automation already covers the behavior.",
                "mapped_scenarios": [],
                "affected_file_changes": [],
                "verification": [],
            }
        )
    elif implementation_complete:
        automation_test = automation_repository / "src" / "test" / "DashboardTest.java"
        automation_test.parent.mkdir(parents=True, exist_ok=True)
        automation_test.write_text(
            "final class DashboardTest {}\n",
            encoding="utf-8",
        )
    automation_plan = AutomationPlan.model_validate(automation_payload)
    automation_plan_content = automation_plan.canonical_json(indent=2)
    automation_repository_diff = capture_automation_repository_diff(
        workspace_path,
        development_plan,
        workflow.config,
    )

    issue = Issue(
        id="1",
        identifier="T-1",
        title="Title",
        status="To Do",
        labels=["codex-ready"],
        url=snapshot.issue_url,
        requirements_snapshot=snapshot,
    )
    run = store.create_run(
        issue,
        workspace_path,
        branch_name="feature/T-1",
        status="completed" if implementation_complete else "running",
        plan_spec_hash=development_plan.content_hash(),
        automation_plan_hash=automation_plan.content_hash(),
        automation_development_diff_hash=development_diff.content_hash,
        automation_repository_diff_hash=automation_repository_diff.content_hash,
        automation_result_hash=(
            automation_result_content_hash(automation_result)
            if implementation_complete
            else None
        ),
    )
    plan_path = workspace_path / workflow.config.codex.output_plan_file
    automation_plan_path = (
        workspace_path / workflow.config.automation.output_plan_file
    )
    automation_result_path = (
        workspace_path / workflow.config.automation.output_result_file
    )
    for path, content in (
        (plan_path, development_plan.canonical_json(indent=2)),
        (automation_plan_path, automation_plan_content),
        (automation_result_path, automation_result),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if not implementation_complete and record_implementation_event:
        store.add_codex_event(
            run.id,
            1,
            "automation_implementation.item.started",
            {"type": "item.started"},
        )
    for path in (
        workspace_path / ".symphony" / "requirements-snapshot.json",
        workspace_path
        / ".symphony"
        / "requirements-snapshots"
        / f"{snapshot.content_hash}.json",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return (
        run,
        workspace_path,
        development_plan_content,
        automation_plan_content,
    )


def create_automation_planning_approval_dashboard_run(
    root: Path,
    workflow,
    store: Store,
):
    run, workspace_path, _, automation_plan_content = (
        create_bound_automation_dashboard_run(
            root,
            workflow,
            store,
            automation_result="Automation implementation has not started.",
            implementation_complete=False,
            record_implementation_event=False,
        )
    )
    development_approval = None
    if workflow.config.codex.require_plan_approval:
        development_approval = store.add_plan_approval(
            run.issue_identifier,
            run_id=run.id,
            approver_identity="development-reviewer@example.test",
            plan_spec_hash=str(run.plan_spec_hash),
            requirements_snapshot_hash=str(run.issue_fingerprint),
        )
    run = store.update_run(
        run.id,
        status="blocked",
        blocked_phase="automation_planning_approval",
        error="Automation plan is ready for approval.",
        plan_approval_id=(
            development_approval["id"] if development_approval else None
        ),
    )
    return run, workspace_path, automation_plan_content, development_approval


def initialize_dashboard_git_repository(repository_path: Path) -> str:
    if not (repository_path / ".git").is_dir():
        repository_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repository_path)], check=True)
        (repository_path / "baseline.txt").write_text(
            "baseline\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(repository_path), "add", "baseline.txt"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "-q",
                "-m",
                "baseline",
            ],
            check=True,
        )
    subprocess.run(
        ["git", "-C", str(repository_path), "checkout", "-q", "-B", "T-1"],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repository_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def create_planning_approval_run(root: Path, store: Store):
    snapshot = RequirementsSnapshot(
        issue_id="1",
        issue_identifier="T-1",
        issue_url="https://jira.example.test/browse/T-1",
    )
    issue = Issue(
        id="1",
        identifier="T-1",
        title="Title",
        status="To Do",
        url=snapshot.issue_url,
        requirements_snapshot=snapshot,
    )
    workspace_path = root / "workspaces" / "T-1"
    run = store.create_run(issue, workspace_path, branch_name=None)
    run = store.update_run(
        run.id,
        status="blocked",
        blocked_phase="planning_approval",
        final_message="validated plan",
    )
    plan_path = workspace_path / ".symphony" / "codex-plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("current plan", encoding="utf-8")
    return run


def create_completed_dashboard_run(
    root: Path,
    store: Store,
    *,
    issue_identifier: str = "T-1",
):
    issue = Issue(
        id=issue_identifier,
        identifier=issue_identifier,
        title="Completed work",
        status="Done",
        labels=["codex-ready"],
        url=f"https://jira.example.test/browse/{issue_identifier}",
    )
    run = store.create_run(
        issue,
        root / "workspaces" / issue_identifier,
        branch_name=f"codex/{issue_identifier}",
    )
    return store.update_run(
        run.id,
        status="completed",
        final_message="Implementation completed.",
        verification_status="passed",
    )


def minimal_human_review_context() -> dict[str, object]:
    return {
        "plan_spec": "",
        "approval": None,
        "source_review": None,
        "source_review_history": None,
        "workspace_diff": "",
        "workspace_diff_hash": "d" * 64,
    }


def write_workflow(root: Path) -> Path:
    path = root / "WORKFLOW.md"
    path.write_text(
        """---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
  jql: "project = T"
workspace:
  root: "./workspaces"
  strategy: hook_only
---
Prompt
""",
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
