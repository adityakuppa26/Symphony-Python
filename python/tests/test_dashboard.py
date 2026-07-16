from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from symphony_jira.dashboard import (
    MAX_HUMAN_REVIEW_REQUEST_BYTES,
    build_state,
    create_app,
    current_plan_spec_hash,
    render_dashboard_html,
)
from symphony_jira.models import Issue, RequirementsSnapshot
from symphony_jira.plan_spec import PlanSpecError
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

    def test_dashboard_expands_long_final_message_and_plan_content(self) -> None:
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
            long_final = "Plan summary\n" + ("x" * 1200) + "FULL-END"
            run = store.create_run(issue, root / "workspaces" / "T-1", branch_name="codex/T-1", status="queued")
            store.update_run(run.id, status="blocked", blocked_phase="planning_approval", final_message=long_final)
            plan_path = root / "workspaces" / "T-1" / ".symphony" / "codex-plan.md"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text("Plan content\nCOLUMN ORDER QUESTION", encoding="utf-8")

            state = build_state(workflow, store)
            html = render_dashboard_html(state)

            self.assertEqual(state["all_runs"][0]["plan_content"], "Plan content\nCOLUMN ORDER QUESTION")
            self.assertIn("<details", html)
            self.assertIn("Show full final message", html)
            self.assertIn("FULL-END", html)
            self.assertIn("Show plan", html)
            self.assertIn("COLUMN ORDER QUESTION", html)

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
            self.assertIn("plan completed", html)
            self.assertNotIn("Which repo should change?", html)
            row = html.split(f"/api/v1/runs/{blocked.id}/human-input", 1)[0].rsplit("<tr>", 1)[1]
            self.assertIn("<td>plan completed</td>", row)
            self.assertNotIn("<td>blocked</td>", row)
            self.assertIn(f"/api/v1/runs/{blocked.id}/human-input", html)

            store.add_human_input("T-1", run_id=blocked.id, response="Change foyr2 only.")
            state = build_state(workflow, store)
            html = render_dashboard_html(state)
            self.assertEqual(state["blocked_issues"], [])
            self.assertIn("queued for resume", html)
            self.assertIn("Change foyr2 only.", html)

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
