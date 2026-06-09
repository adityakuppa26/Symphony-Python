from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from symphony_jira.dashboard import build_state, render_dashboard_html
from symphony_jira.models import Issue
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
            self.assertEqual(state["recent_runs"][0]["issue_identifier"], "T-1")
            self.assertEqual(state["recent_runs"][0]["current_phase"], "failed")
            self.assertNotIn("codex_event_count", state["recent_runs"][0])
            self.assertNotIn("<th>Events</th>", html)
            self.assertIn("<th>Plan</th>", html)
            self.assertIn("<th>Blocked Phase</th>", html)
            self.assertIn("<th>Error</th>", html)
            self.assertTrue(state["recent_runs"][0]["plan_exists"])
            self.assertEqual(state["recent_runs"][0]["blocked_phase"], "implementation")
            self.assertIn("T-1", html)
            self.assertIn("implementation", html)
            self.assertIn("codex-plan.md", html)
            self.assertIn("Codex could not run verification", html)
            self.assertIn("done", html)

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
            self.assertIn("Confirm the plan", html)
            self.assertIn("Confirm Plan", html)
            self.assertIn("placeholder=\"Confirm the plan or enter requested adjustments\"", html)
            self.assertIn("plan completed", html)
            self.assertNotIn("Which repo should change?", html)
            row = html.split(f"/api/v1/runs/{blocked.id}/human-input", 1)[0].rsplit("<tr>", 1)[1]
            self.assertIn("<td>plan completed</td>", row)
            self.assertNotIn("<td>blocked</td>", row)
            self.assertIn(f"/api/v1/runs/{blocked.id}/human-input", html)

            store.add_human_input("T-1", run_id=blocked.id, response="Change foyr2 only.")
            html = render_dashboard_html(build_state(workflow, store))
            self.assertIn("queued for resume", html)
            self.assertIn("Change foyr2 only.", html)

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
