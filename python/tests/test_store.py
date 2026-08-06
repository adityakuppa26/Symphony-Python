from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from symphony_jira.automation_plan import (
    AutomationPlan,
    automation_result_content_hash,
)
from symphony_jira.models import Issue, RequirementsSnapshot
from symphony_jira.store import (
    HUMAN_INPUT_CLAIM_LEASE,
    HUMAN_RESUME_HANDOFF_LEASE,
    Store,
    StoreIntegrityError,
)


class StoreHumanInputTests(unittest.TestCase):
    def test_verification_bypass_input_persists_structured_integrity_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = make_issue()
            run = store.create_run(issue, root / "workspace", branch_name=None)
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="verification",
                verification_status="test_failed",
                verification_output_path=str(root / "workspace" / "verification.json"),
                verification_workspace_diff_hash="a" * 64,
                verification_evidence_sha256="b" * 64,
            )

            pending = store.add_verification_bypass_input(
                "T-1",
                run_id=run.id,
                approver_identity="  operator@example.test  ",
                workspace_diff_hash="a" * 64,
                verification_evidence_sha256="B" * 64,
                question="Tests failed",
            )
            claimed = store.claim_human_input(pending["id"])

            self.assertEqual(pending["action"], "verification_bypass")
            self.assertEqual(
                pending["approver_identity"],
                "operator@example.test",
            )
            self.assertEqual(pending["workspace_diff_hash"], "a" * 64)
            self.assertEqual(
                pending["verification_evidence_sha256"],
                "b" * 64,
            )
            assert claimed is not None
            self.assertEqual(claimed["action"], "verification_bypass")
            self.assertEqual(
                claimed["verification_evidence_sha256"],
                "b" * 64,
            )

    def test_verification_bypass_input_is_transactionally_fenced_to_run_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            run = store.create_run(
                make_issue(),
                root / "workspace",
                branch_name=None,
            )
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="verification",
                verification_status="test_failed",
                verification_output_path=str(root / "workspace" / "verification.json"),
                verification_workspace_diff_hash="a" * 64,
                verification_evidence_sha256="b" * 64,
            )

            with self.assertRaisesRegex(
                ValueError,
                "does not match the failed run integrity binding",
            ):
                store.add_verification_bypass_input(
                    "T-1",
                    run_id=run.id,
                    approver_identity="operator@example.test",
                    workspace_diff_hash="c" * 64,
                    verification_evidence_sha256="b" * 64,
                )

            self.assertEqual(store.list_human_inputs(run_id=run.id), [])

    def test_store_migrates_verification_integrity_binding_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE runs (
                      id TEXT PRIMARY KEY,
                      issue_id TEXT NOT NULL,
                      issue_identifier TEXT NOT NULL,
                      issue_fingerprint TEXT,
                      workspace_path TEXT NOT NULL,
                      status TEXT NOT NULL,
                      attempt INTEGER NOT NULL,
                      started_at TEXT NOT NULL,
                      plan_spec_hash TEXT,
                      plan_approval_id TEXT,
                      finished_at TEXT,
                      final_message TEXT,
                      error TEXT,
                      blocked_phase TEXT,
                      branch_name TEXT,
                      verification_status TEXT,
                      verification_output_path TEXT
                    )
                    """
                )

            Store(db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(runs)").fetchall()
                }
                human_input_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(human_inputs)"
                    ).fetchall()
                }
                automation_approval_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(automation_plan_approvals)"
                    ).fetchall()
                }
            self.assertIn("verification_workspace_diff_hash", columns)
            self.assertIn("verification_evidence_sha256", columns)
            self.assertIn("automation_plan_hash", columns)
            self.assertIn("automation_development_diff_hash", columns)
            self.assertIn("automation_repository_diff_hash", columns)
            self.assertIn("automation_result_hash", columns)
            self.assertIn("automation_plan_approval_id", columns)
            self.assertIn(
                "automation_plan_approval_id",
                human_input_columns,
            )
            self.assertTrue(
                {
                    "automation_plan_hash",
                    "requirements_snapshot_hash",
                    "development_plan_spec_hash",
                    "development_plan_approval_id",
                    "development_workspace_diff_hash",
                    "automation_repository_diff_hash",
                    "invalidated_at",
                    "invalidation_reason",
                }.issubset(automation_approval_columns)
            )

    def test_automation_approval_is_atomic_exact_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = make_issue()
            run = store.create_run(issue, root / "workspace", branch_name=None)
            requirements_hash = run.issue_fingerprint or ""
            development_approval = store.add_plan_approval(
                issue.identifier,
                run_id=run.id,
                approver_identity="dev-owner@example.test",
                plan_spec_hash="a" * 64,
                requirements_snapshot_hash=requirements_hash,
            )
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="automation_planning_approval",
                automation_plan_hash="b" * 64,
                automation_development_diff_hash="c" * 64,
                automation_repository_diff_hash="d" * 64,
                automation_result_hash="e" * 64,
            )

            pending, approval = store.add_approved_automation_human_input(
                issue.identifier,
                run_id=run.id,
                approver_identity="  automation-owner@example.test  ",
                automation_plan_hash="B" * 64,
                requirements_snapshot_hash=requirements_hash,
                development_plan_spec_hash="a" * 64,
                development_plan_approval_id=development_approval["id"],
                development_workspace_diff_hash="c" * 64,
                automation_repository_diff_hash="d" * 64,
                question="Approve this exact AutomationPlan?",
            )

            persisted_run = store.get_run(run.id)
            assert persisted_run is not None
            self.assertEqual(
                persisted_run.plan_approval_id,
                development_approval["id"],
            )
            self.assertEqual(
                persisted_run.automation_plan_approval_id,
                approval["id"],
            )
            self.assertIsNone(persisted_run.automation_result_hash)
            self.assertEqual(pending["action"], "automation_plan_approval")
            self.assertIsNone(pending["approval_id"])
            self.assertEqual(
                pending["automation_plan_approval_id"],
                approval["id"],
            )
            self.assertEqual(
                approval["development_plan_approval_id"],
                development_approval["id"],
            )
            self.assertEqual(
                store.get_automation_plan_approval(approval["id"]),
                approval,
            )
            self.assertEqual(
                store.latest_automation_plan_approval_for_run(
                    run.id,
                    active_only=True,
                ),
                approval,
            )
            listed_input = store.list_human_inputs(run_id=run.id)[0]
            self.assertEqual(
                listed_input["automation_plan_hash"],
                "b" * 64,
            )
            self.assertEqual(
                listed_input["automation_plan_approval_id"],
                approval["id"],
            )
            claimed = store.claim_human_input(pending["id"])
            assert claimed is not None
            reserved, status = store.reserve_human_resume(
                issue,
                root / "workspace",
                input_id=pending["id"],
                claim_token=claimed["claim_token"],
                expected_predecessor_run_id=run.id,
                branch_name=None,
                attempt=2,
            )
            self.assertEqual(status, "reserved")
            assert reserved is not None
            self.assertEqual(
                reserved.automation_plan_approval_id,
                approval["id"],
            )
            self.assertEqual(
                store.resolve_active_automation_plan_approval(
                    reserved.id,
                    automation_plan_hash="b" * 64,
                    requirements_snapshot_hash=requirements_hash,
                    development_plan_spec_hash="a" * 64,
                    development_plan_approval_id=development_approval["id"],
                    development_workspace_diff_hash="c" * 64,
                    automation_repository_diff_hash="d" * 64,
                ),
                approval,
            )
            self.assertIsNone(
                store.resolve_active_automation_plan_approval(
                    reserved.id,
                    automation_plan_hash="f" * 64,
                    requirements_snapshot_hash=requirements_hash,
                    development_plan_spec_hash="a" * 64,
                    development_plan_approval_id=development_approval["id"],
                    development_workspace_diff_hash="c" * 64,
                    automation_repository_diff_hash="d" * 64,
                )
            )
            invalidated = store.get_automation_plan_approval(approval["id"])
            assert invalidated is not None
            self.assertIn(
                "AutomationPlan changed after approval",
                invalidated["invalidation_reason"],
            )

    def test_automation_approval_rejects_the_wrong_phase_without_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            run = store.create_run(
                make_issue(),
                root / "workspace",
                branch_name=None,
            )
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="automation_planning",
                plan_spec_hash="a" * 64,
                automation_plan_hash="b" * 64,
                automation_development_diff_hash="c" * 64,
                automation_repository_diff_hash="d" * 64,
            )

            with self.assertRaisesRegex(
                ValueError,
                "not blocked for automation plan approval",
            ):
                store.add_approved_automation_human_input(
                    "T-1",
                    run_id=run.id,
                    approver_identity="automation-owner@example.test",
                    automation_plan_hash="b" * 64,
                    requirements_snapshot_hash=run.issue_fingerprint or "",
                    development_plan_spec_hash="a" * 64,
                    development_plan_approval_id=None,
                    development_workspace_diff_hash="c" * 64,
                    automation_repository_diff_hash="d" * 64,
                )

            self.assertEqual(
                store.list_automation_plan_approvals(run_id=run.id),
                [],
            )
            self.assertEqual(store.list_human_inputs(run_id=run.id), [])
            persisted_run = store.get_run(run.id)
            assert persisted_run is not None
            self.assertIsNone(persisted_run.automation_plan_approval_id)

    def test_only_latest_run_overall_is_actionable_even_if_older_run_finishes_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = make_issue()
            older = store.create_run(issue, root / "workspace", branch_name=None)
            older = store.update_run(
                older.id,
                status="blocked",
                finished_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
            newer = store.create_run(issue, root / "workspace", branch_name=None)
            newer = store.update_run(newer.id, status="blocked")

            self.assertEqual(store.latest_run_for_issue("T-1").id, newer.id)
            self.assertFalse(store.is_latest_actionable_blocked_run(older.id))
            self.assertTrue(store.is_latest_actionable_blocked_run(newer.id))
            with self.assertRaisesRegex(ValueError, "latest actionable blocked run"):
                store.add_human_input("T-1", run_id=older.id, response="stale")

    def test_pending_input_is_unique_and_claim_is_atomic_and_requeueable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = make_issue()
            run = store.create_run(issue, root / "workspace", branch_name=None)
            run = store.update_run(run.id, status="blocked")
            pending = store.add_human_input("T-1", run_id=run.id, response="Use option A")

            with self.assertRaisesRegex(ValueError, "already pending"):
                store.add_human_input("T-1", run_id=run.id, response="Use option B")

            other_connection = Store(root / "db.sqlite3")
            claimed = store.claim_human_input(pending["id"])
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertTrue(claimed["claimed_at"])
            self.assertIsNone(other_connection.claim_human_input(pending["id"]))

            self.assertTrue(
                store.release_human_input_claim(
                    pending["id"],
                    claimed["claim_token"],
                )
            )
            reclaimed = other_connection.claim_human_input(pending["id"])
            self.assertIsNotNone(reclaimed)
            assert reclaimed is not None
            self.assertTrue(
                other_connection.mark_human_input_consumed(
                    pending["id"],
                    reclaimed["claim_token"],
                )
            )
            persisted = store.list_human_inputs(run_id=run.id)[0]
            self.assertIsNone(persisted["claimed_at"])
            self.assertIsNone(persisted["claim_token"])
            self.assertTrue(persisted["consumed_at"])

    def test_newer_blocked_run_retires_stale_pending_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = make_issue()
            older = store.create_run(issue, root / "workspace", branch_name=None)
            older = store.update_run(older.id, status="blocked")
            stale = store.add_human_input(
                "T-1",
                run_id=older.id,
                response="Response for the older attempt",
            )
            newer = store.create_run(issue, root / "workspace", branch_name=None)
            newer = store.update_run(newer.id, status="blocked")

            current = store.add_human_input(
                "T-1",
                run_id=newer.id,
                response="Response for the current attempt",
            )

            old_record = store.list_human_inputs(run_id=older.id)[0]
            self.assertEqual(old_record["id"], stale["id"])
            self.assertIsNotNone(old_record["consumed_at"])
            self.assertIsNone(old_record["claimed_at"])
            self.assertIsNone(old_record["claim_token"])
            self.assertEqual(
                store.latest_unconsumed_human_input_for_issue("T-1")["id"],
                current["id"],
            )

    def test_stale_claim_is_recovered_without_giving_old_owner_mutation_rights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_store = Store(root / "db.sqlite3")
            second_store = Store(root / "db.sqlite3")
            issue = make_issue()
            run = first_store.create_run(issue, root / "workspace", branch_name=None)
            run = first_store.update_run(run.id, status="blocked")
            pending = first_store.add_human_input(
                "T-1",
                run_id=run.id,
                response="Use option A",
            )
            first_claimed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

            first_claim = first_store.claim_human_input(
                pending["id"],
                now=first_claimed_at,
            )
            self.assertIsNotNone(first_claim)
            assert first_claim is not None
            fresh_attempt = second_store.claim_human_input(
                pending["id"],
                now=first_claimed_at + HUMAN_INPUT_CLAIM_LEASE - timedelta(seconds=1),
            )
            self.assertIsNone(fresh_attempt)

            recovered = second_store.claim_human_input(
                pending["id"],
                now=first_claimed_at + HUMAN_INPUT_CLAIM_LEASE + timedelta(seconds=1),
            )
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertNotEqual(first_claim["claim_token"], recovered["claim_token"])
            self.assertFalse(
                first_store.renew_human_input_claim(
                    pending["id"],
                    first_claim["claim_token"],
                )
            )
            self.assertFalse(
                first_store.release_human_input_claim(
                    pending["id"],
                    first_claim["claim_token"],
                )
            )
            self.assertFalse(
                first_store.mark_human_input_consumed(
                    pending["id"],
                    first_claim["claim_token"],
                )
            )
            self.assertTrue(
                second_store.mark_human_input_consumed(
                    pending["id"],
                    recovered["claim_token"],
                )
            )

    def test_rejected_duplicate_approval_leaves_no_orphan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = make_issue()
            run = store.create_run(issue, root / "workspace", branch_name=None)
            run = store.update_run(run.id, status="blocked", blocked_phase="planning_approval")
            store.add_human_input("T-1", run_id=run.id, response="Please adjust the plan")

            with self.assertRaisesRegex(ValueError, "already pending"):
                store.add_approved_human_input(
                    "T-1",
                    run_id=run.id,
                    approver_identity="ada@example.test",
                    plan_spec_hash="a" * 64,
                    requirements_snapshot_hash="b" * 64,
                )

            self.assertEqual(store.list_plan_approvals(run_id=run.id), [])

    def test_atomic_resume_reservation_inherits_lineage_and_consumes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = make_issue()
            predecessor = store.create_run(issue, root / "workspace", branch_name="codex/T-1")
            predecessor = store.update_run(
                predecessor.id,
                status="blocked",
                blocked_phase="implementation",
                plan_spec_hash="a" * 64,
                plan_approval_id="approval-1",
                automation_plan_hash="b" * 64,
                automation_development_diff_hash="c" * 64,
                automation_repository_diff_hash="d" * 64,
                automation_result_hash="e" * 64,
            )
            pending = store.add_human_input(
                "T-1",
                run_id=predecessor.id,
                response="Use option A",
            )
            claimed = store.claim_human_input(pending["id"])
            assert claimed is not None

            reserved, status = store.reserve_human_resume(
                issue,
                root / "workspace",
                input_id=pending["id"],
                claim_token=claimed["claim_token"],
                expected_predecessor_run_id=predecessor.id,
                branch_name="codex/T-1",
                attempt=2,
            )

            self.assertEqual(status, "reserved")
            assert reserved is not None
            self.assertEqual(reserved.status, "queued")
            self.assertEqual(reserved.plan_spec_hash, "a" * 64)
            self.assertEqual(reserved.plan_approval_id, "approval-1")
            self.assertEqual(reserved.automation_plan_hash, "b" * 64)
            self.assertEqual(
                reserved.automation_development_diff_hash,
                "c" * 64,
            )
            self.assertEqual(
                reserved.automation_repository_diff_hash,
                "d" * 64,
            )
            self.assertEqual(reserved.automation_result_hash, "e" * 64)
            self.assertEqual(store.latest_run_for_issue("T-1").id, reserved.id)
            persisted_input = store.list_human_inputs(run_id=predecessor.id)[0]
            self.assertIsNotNone(persisted_input["consumed_at"])
            self.assertIsNone(persisted_input["claim_token"])
            self.assertIsNone(store.claim_human_input(pending["id"]))
            self.assertEqual(store.list_recoverable_human_resume_run_ids(), [reserved.id])

    def test_reserved_resume_is_recoverable_after_restart_and_stale_owner_is_fenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_store = Store(root / "db.sqlite3")
            restarted_store = Store(root / "db.sqlite3")
            issue = make_issue()
            predecessor = first_store.create_run(issue, root / "workspace", branch_name=None)
            predecessor = first_store.update_run(
                predecessor.id,
                status="blocked",
                blocked_phase="implementation",
                plan_spec_hash="a" * 64,
                plan_approval_id="approval-1",
                automation_plan_hash="b" * 64,
                automation_development_diff_hash="c" * 64,
                automation_repository_diff_hash="d" * 64,
                automation_result_hash="e" * 64,
            )
            pending = first_store.add_human_input(
                "T-1", run_id=predecessor.id, response="recover this exact response"
            )
            claimed = first_store.claim_human_input(pending["id"])
            assert claimed is not None
            reserved, status = first_store.reserve_human_resume(
                issue,
                root / "workspace",
                input_id=pending["id"],
                claim_token=claimed["claim_token"],
                expected_predecessor_run_id=predecessor.id,
                branch_name=None,
                attempt=2,
            )
            self.assertEqual(status, "reserved")
            assert reserved is not None
            first_claimed_at = datetime.now(timezone.utc)
            first_handoff = first_store.claim_human_resume_handoff(
                reserved.id, now=first_claimed_at
            )
            assert first_handoff is not None
            first_started = first_store.start_human_resume_run(
                reserved.id,
                first_handoff["handoff_claim_token"],
                now=first_claimed_at,
            )
            assert first_started is not None
            self.assertEqual(first_started.status, "running")
            advanced = first_store.update_owned_human_resume_run(
                reserved.id,
                first_handoff["handoff_claim_token"],
                now=first_claimed_at,
                plan_spec_hash="f" * 64,
                automation_plan_hash="1" * 64,
                automation_development_diff_hash="2" * 64,
                automation_repository_diff_hash="3" * 64,
                automation_result_hash="4" * 64,
                plan_approval_id="approval-2",
            )
            assert advanced is not None
            self.assertEqual(advanced.plan_spec_hash, "f" * 64)
            self.assertEqual(advanced.automation_plan_hash, "1" * 64)
            self.assertEqual(advanced.automation_development_diff_hash, "2" * 64)
            self.assertEqual(advanced.automation_repository_diff_hash, "3" * 64)
            self.assertEqual(advanced.automation_result_hash, "4" * 64)
            self.assertEqual(advanced.plan_approval_id, "approval-2")

            self.assertIsNone(
                restarted_store.claim_human_resume_handoff(
                    reserved.id,
                    now=first_claimed_at + HUMAN_RESUME_HANDOFF_LEASE - timedelta(seconds=1),
                )
            )
            expired_at = (
                first_claimed_at + HUMAN_RESUME_HANDOFF_LEASE + timedelta(seconds=1)
            )
            self.assertFalse(
                first_store.renew_human_resume_handoff(
                    reserved.id,
                    first_handoff["handoff_claim_token"],
                    now=expired_at,
                )
            )
            self.assertIsNone(
                first_store.update_owned_human_resume_run(
                    reserved.id,
                    first_handoff["handoff_claim_token"],
                    now=expired_at,
                    status="completed",
                    finished_at=expired_at,
                )
            )
            self.assertEqual(first_store.get_run(reserved.id).status, "running")
            recovered = restarted_store.claim_human_resume_handoff(
                reserved.id,
                now=expired_at,
            )
            assert recovered is not None
            self.assertEqual(recovered["response"], "recover this exact response")
            self.assertEqual(recovered["predecessor_run_id"], predecessor.id)
            self.assertEqual(recovered["resume_run_id"], reserved.id)
            self.assertNotEqual(
                recovered["handoff_claim_token"], first_handoff["handoff_claim_token"]
            )
            recovered_run = restarted_store.get_run(reserved.id)
            assert recovered_run is not None
            self.assertEqual(recovered_run.status, "queued")
            self.assertEqual(recovered_run.plan_spec_hash, predecessor.plan_spec_hash)
            self.assertEqual(
                recovered_run.automation_plan_hash,
                predecessor.automation_plan_hash,
            )
            self.assertEqual(
                recovered_run.automation_development_diff_hash,
                predecessor.automation_development_diff_hash,
            )
            self.assertEqual(
                recovered_run.automation_repository_diff_hash,
                predecessor.automation_repository_diff_hash,
            )
            self.assertEqual(
                recovered_run.automation_result_hash,
                predecessor.automation_result_hash,
            )
            self.assertEqual(
                recovered_run.plan_approval_id,
                predecessor.plan_approval_id,
            )
            self.assertFalse(
                first_store.renew_human_resume_handoff(
                    reserved.id, first_handoff["handoff_claim_token"]
                )
            )
            self.assertIsNone(
                first_store.update_owned_human_resume_run(
                    reserved.id,
                    first_handoff["handoff_claim_token"],
                    status="completed",
                    finished_at=datetime.now(timezone.utc),
                )
            )
            recovered_started = restarted_store.start_human_resume_run(
                reserved.id,
                recovered["handoff_claim_token"],
                now=expired_at,
            )
            assert recovered_started is not None
            self.assertEqual(recovered_started.status, "running")
            completed = restarted_store.update_owned_human_resume_run(
                reserved.id,
                recovered["handoff_claim_token"],
                now=expired_at,
                status="completed",
                finished_at=expired_at,
            )
            assert completed is not None
            self.assertEqual(completed.status, "completed")
            self.assertIsNone(
                restarted_store.update_owned_human_resume_run(
                    reserved.id,
                    recovered["handoff_claim_token"],
                    now=expired_at,
                    final_message="must not overwrite terminal state",
                )
            )
            persisted = restarted_store.get_run(reserved.id)
            assert persisted is not None
            self.assertIsNone(persisted.final_message)
            self.assertEqual(restarted_store.list_recoverable_human_resume_run_ids(), [])
            retired_handoff = restarted_store.get_human_resume_handoff(reserved.id)
            assert retired_handoff is not None
            self.assertIsNone(retired_handoff["claim_token"])

    def test_atomic_resume_reservation_discards_stale_predecessor_without_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_store = Store(root / "db.sqlite3")
            other_process = Store(root / "db.sqlite3")
            issue = make_issue()
            predecessor = first_store.create_run(issue, root / "workspace", branch_name=None)
            predecessor = first_store.update_run(predecessor.id, status="blocked")
            pending = first_store.add_human_input(
                "T-1", run_id=predecessor.id, response="stale response"
            )
            claimed = first_store.claim_human_input(pending["id"])
            assert claimed is not None
            newer = other_process.create_run(issue, root / "workspace", branch_name=None)
            other_process.update_run(newer.id, status="blocked")

            reserved, status = first_store.reserve_human_resume(
                issue,
                root / "workspace",
                input_id=pending["id"],
                claim_token=claimed["claim_token"],
                expected_predecessor_run_id=predecessor.id,
                branch_name=None,
                attempt=2,
            )

            self.assertIsNone(reserved)
            self.assertEqual(status, "stale_predecessor")
            self.assertEqual(first_store.latest_run_for_issue("T-1").id, newer.id)
            persisted_input = first_store.list_human_inputs(run_id=predecessor.id)[0]
            self.assertIsNotNone(persisted_input["consumed_at"])

    def test_active_run_creation_fence_is_atomic_across_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_store = Store(root / "db.sqlite3")
            other_process = Store(root / "db.sqlite3")
            issue = make_issue()
            active = first_store.create_run(
                issue,
                root / "workspace",
                branch_name=None,
                require_no_active_run=True,
            )

            with self.assertRaisesRegex(ValueError, "already has an active"):
                other_process.create_run(
                    issue,
                    root / "workspace",
                    branch_name=None,
                    require_no_active_run=True,
                )
            self.assertEqual(first_store.latest_run_for_issue("T-1").id, active.id)


class StoreHumanReviewTests(unittest.TestCase):
    def test_store_migrates_automation_context_columns_for_review_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE human_review_actions (
                      id TEXT PRIMARY KEY,
                      issue_identifier TEXT NOT NULL,
                      source_run_id TEXT NOT NULL UNIQUE,
                      result_run_id TEXT NOT NULL UNIQUE,
                      reviewer_identity TEXT NOT NULL,
                      source_url TEXT NOT NULL,
                      comments TEXT NOT NULL,
                      requirements_snapshot_hash TEXT NOT NULL,
                      plan_spec_hash TEXT,
                      plan_spec TEXT,
                      plan_approval_id TEXT,
                      approval_json TEXT,
                      source_final_message TEXT,
                      source_review TEXT,
                      source_review_history TEXT,
                      workspace_diff TEXT NOT NULL,
                      workspace_diff_hash TEXT NOT NULL,
                      triage_decision TEXT,
                      triage_output TEXT,
                      status TEXT NOT NULL,
                      claimed_at TEXT,
                      claim_token TEXT,
                      started_at TEXT,
                      finished_at TEXT,
                      created_at TEXT NOT NULL
                    )
                    """
                )

            Store(db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(human_review_actions)"
                    ).fetchall()
                }
            self.assertTrue(
                {
                    "automation_plan_hash",
                    "automation_development_diff_hash",
                    "automation_repository_diff_hash",
                    "automation_result_hash",
                    "automation_plan",
                    "automation_result",
                    "automation_plan_approval_id",
                }.issubset(columns)
            )

    @staticmethod
    def _create_completed_source(
        store: Store,
        root: Path,
        *,
        attempt: int = 3,
    ):
        issue = make_issue()
        source = store.create_run(
            issue,
            root / "workspace",
            branch_name="codex/T-1",
            attempt=attempt,
        )
        approval = store.add_plan_approval(
            "T-1",
            run_id=source.id,
            approver_identity="plan-owner@example.test",
            plan_spec_hash="a" * 64,
            requirements_snapshot_hash=source.issue_fingerprint or "",
        )
        source = store.update_run(
            source.id,
            status="completed",
            finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            final_message="Original implementation completed.",
            verification_status="passed",
            verification_output_path=".symphony/verification.log",
        )
        return source, approval

    @staticmethod
    def _create_action(
        store: Store,
        source,
        approval,
        *,
        automation_plan: AutomationPlan | None = None,
        automation_result: str | None = None,
    ):
        return store.create_human_review_action(
            source.id,
            reviewer_identity="reviewer@example.test",
            source_url="https://github.example.test/org/repo/pull/42",
            comments="Please rename the local variable and add the missing test.",
            plan_spec="# Plan\n- Keep the approved behavior stable.",
            approval=approval,
            source_review="APPROVE\nThe original review passed.",
            source_review_history="## Attempt 1\nAPPROVE",
            workspace_diff="diff --git a/app.py b/app.py\n",
            workspace_diff_hash="d" * 64,
            automation_plan_hash=(
                automation_plan.content_hash() if automation_plan else None
            ),
            automation_plan=(
                automation_plan.canonical_json(indent=2) if automation_plan else None
            ),
            automation_result=automation_result,
        )

    def test_action_and_child_are_created_atomically_with_frozen_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            source, approval = self._create_completed_source(store, root)

            action, child = self._create_action(store, source, approval)

            self.assertEqual(action["issue_identifier"], source.issue_identifier)
            self.assertEqual(action["source_run_id"], source.id)
            self.assertEqual(action["result_run_id"], child.id)
            self.assertEqual(action["reviewer_identity"], "reviewer@example.test")
            self.assertEqual(
                action["source_url"],
                "https://github.example.test/org/repo/pull/42",
            )
            self.assertEqual(
                action["comments"],
                "Please rename the local variable and add the missing test.",
            )
            self.assertEqual(
                action["requirements_snapshot_hash"], source.issue_fingerprint
            )
            self.assertEqual(action["plan_spec_hash"], source.plan_spec_hash)
            self.assertEqual(
                action["plan_spec"],
                "# Plan\n- Keep the approved behavior stable.",
            )
            self.assertEqual(action["plan_approval_id"], source.plan_approval_id)
            self.assertEqual(action["approval"], approval)
            self.assertEqual(action["source_final_message"], source.final_message)
            self.assertEqual(
                action["source_review"], "APPROVE\nThe original review passed."
            )
            self.assertEqual(
                action["source_review_history"], "## Attempt 1\nAPPROVE"
            )
            self.assertEqual(
                action["workspace_diff"], "diff --git a/app.py b/app.py\n"
            )
            self.assertEqual(action["workspace_diff_hash"], "d" * 64)
            self.assertEqual(action["status"], "queued")
            self.assertIsNone(action["claimed_at"])
            self.assertIsNone(action["claim_token"])

            self.assertEqual(child.issue_id, source.issue_id)
            self.assertEqual(child.issue_identifier, source.issue_identifier)
            self.assertEqual(child.issue_fingerprint, source.issue_fingerprint)
            self.assertEqual(child.workspace_path, source.workspace_path)
            self.assertEqual(child.branch_name, source.branch_name)
            self.assertEqual(child.attempt, source.attempt + 1)
            self.assertEqual(child.plan_spec_hash, source.plan_spec_hash)
            self.assertEqual(child.plan_approval_id, source.plan_approval_id)
            self.assertEqual(child.status, "queued")
            self.assertEqual(store.latest_run_for_issue("T-1").id, child.id)
            self.assertEqual(
                store.human_review_action_for_result_run(child.id), action
            )
            self.assertEqual(
                store.list_human_review_actions_for_source_run(source.id), [action]
            )
            self.assertEqual(
                store.list_recoverable_human_review_action_ids(), [action["id"]]
            )

    def test_action_freezes_bound_automation_artifacts_and_copies_hash_to_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            source, approval = self._create_completed_source(store, root)
            automation_plan = AutomationPlan.model_validate(
                store_automation_plan_payload(
                    issue_key=source.issue_identifier,
                    requirements_snapshot_hash=source.issue_fingerprint or "",
                )
            )
            source = store.update_run(
                source.id,
                automation_plan_hash=automation_plan.content_hash(),
                automation_development_diff_hash=(
                    automation_plan.development_workspace_diff_hash
                ),
                automation_repository_diff_hash="e" * 64,
                automation_result_hash=automation_result_content_hash(
                    "Added the focused automation scenario."
                ),
            )

            action, child = store.create_human_review_action(
                source.id,
                reviewer_identity="reviewer@example.test",
                source_url="https://github.example.test/org/repo/pull/42",
                comments="Please tighten the automation assertion.",
                plan_spec="# Plan\n- Keep the approved behavior stable.",
                approval=approval,
                source_review="APPROVE",
                source_review_history="## Attempt 1\nAPPROVE",
                workspace_diff="diff --git a/app.py b/app.py\n",
                workspace_diff_hash="d" * 64,
                automation_plan_hash=automation_plan.content_hash(),
                automation_plan=automation_plan.canonical_json(indent=2),
                automation_result="Added the focused automation scenario.",
            )

            self.assertEqual(
                action["automation_plan_hash"],
                automation_plan.content_hash(),
            )
            self.assertEqual(
                action["automation_development_diff_hash"],
                source.automation_development_diff_hash,
            )
            self.assertEqual(
                action["automation_repository_diff_hash"],
                source.automation_repository_diff_hash,
            )
            self.assertEqual(
                action["automation_result_hash"],
                source.automation_result_hash,
            )
            self.assertEqual(
                action["automation_plan"],
                automation_plan.canonical_json(indent=2),
            )
            self.assertEqual(
                action["automation_result"],
                "Added the focused automation scenario.",
            )
            self.assertEqual(
                child.automation_plan_hash,
                source.automation_plan_hash,
            )
            self.assertEqual(
                child.automation_development_diff_hash,
                source.automation_development_diff_hash,
            )
            self.assertEqual(
                child.automation_repository_diff_hash,
                source.automation_repository_diff_hash,
            )
            self.assertEqual(
                child.automation_result_hash,
                source.automation_result_hash,
            )

    def test_automation_context_mismatch_leaves_no_action_or_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            source, approval = self._create_completed_source(store, root)
            automation_plan = AutomationPlan.model_validate(
                store_automation_plan_payload(
                    issue_key=source.issue_identifier,
                    requirements_snapshot_hash=source.issue_fingerprint or "",
                )
            )
            source = store.update_run(
                source.id,
                automation_plan_hash=automation_plan.content_hash(),
            )

            with self.assertRaisesRegex(
                ValueError,
                "AutomationPlan hash does not match",
            ):
                store.create_human_review_action(
                    source.id,
                    reviewer_identity="reviewer@example.test",
                    source_url="https://github.example.test/org/repo/pull/42",
                    comments="Please tighten the automation assertion.",
                    plan_spec="# Plan\n- Keep the approved behavior stable.",
                    approval=approval,
                    source_review=None,
                    source_review_history=None,
                    workspace_diff="diff --git a/app.py b/app.py\n",
                    workspace_diff_hash="d" * 64,
                    automation_plan_hash="f" * 64,
                    automation_plan=automation_plan.canonical_json(indent=2),
                    automation_result="Added the focused automation scenario.",
                )

            self.assertEqual(
                [run.id for run in store.list_runs_for_issue("T-1")],
                [source.id],
            )
            self.assertEqual(
                store.list_human_review_actions_for_issue("T-1"),
                [],
            )

    def test_rejections_leave_no_orphan_action_or_child_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            issue = make_issue()

            def submit(run_id: str):
                return store.create_human_review_action(
                    run_id,
                    reviewer_identity="reviewer@example.test",
                    source_url="https://github.example.test/org/repo/pull/42",
                    comments="Please add a regression test.",
                    plan_spec="",
                    approval=None,
                    source_review=None,
                    source_review_history=None,
                    workspace_diff="",
                    workspace_diff_hash="d" * 64,
                )

            non_completed = store.create_run(
                issue, root / "workspace", branch_name=None
            )
            with self.assertRaisesRegex(
                ValueError, "latest actionable completed run"
            ):
                submit(non_completed.id)
            self.assertEqual(len(store.list_runs_for_issue("T-1")), 1)
            self.assertEqual(store.list_human_review_actions_for_issue("T-1"), [])

            store.update_run(non_completed.id, status="completed")
            latest = store.create_run(issue, root / "workspace", branch_name=None)
            latest = store.update_run(latest.id, status="completed")
            with self.assertRaisesRegex(
                ValueError, "latest actionable completed run"
            ):
                submit(non_completed.id)
            self.assertEqual(len(store.list_runs_for_issue("T-1")), 2)
            self.assertEqual(store.list_human_review_actions_for_issue("T-1"), [])

            action, child = submit(latest.id)
            self.assertEqual(len(store.list_runs_for_issue("T-1")), 3)
            self.assertEqual(
                store.list_human_review_actions_for_issue("T-1"), [action]
            )
            with self.assertRaisesRegex(
                ValueError, "latest actionable completed run"
            ):
                submit(latest.id)
            self.assertEqual(
                [run.id for run in store.list_runs_for_issue("T-1")],
                [child.id, latest.id, non_completed.id],
            )
            self.assertEqual(
                store.list_human_review_actions_for_issue("T-1"), [action]
            )

    def test_claim_start_triage_and_terminal_updates_are_token_fenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_store = Store(root / "db.sqlite3")
            restarted_store = Store(root / "db.sqlite3")
            source, approval = self._create_completed_source(first_store, root)
            automation_plan = AutomationPlan.model_validate(
                store_automation_plan_payload(
                    issue_key=source.issue_identifier,
                    requirements_snapshot_hash=source.issue_fingerprint or "",
                )
            )
            automation_result = "Added the focused automation scenario."
            source = first_store.update_run(
                source.id,
                automation_plan_hash=automation_plan.content_hash(),
                automation_development_diff_hash=(
                    automation_plan.development_workspace_diff_hash
                ),
                automation_repository_diff_hash="e" * 64,
                automation_result_hash=automation_result_content_hash(
                    automation_result
                ),
            )
            action, child = self._create_action(
                first_store,
                source,
                approval,
                automation_plan=automation_plan,
                automation_result=automation_result,
            )
            first_claimed_at = datetime(2026, 2, 1, tzinfo=timezone.utc)

            first_claim = first_store.claim_human_review_action(
                action["id"], now=first_claimed_at
            )
            self.assertIsNotNone(first_claim)
            assert first_claim is not None
            self.assertEqual(first_claim["status"], "queued")
            self.assertTrue(first_claim["claim_token"])
            self.assertIsNone(
                restarted_store.claim_human_review_action(
                    action["id"],
                    now=(
                        first_claimed_at
                        + HUMAN_RESUME_HANDOFF_LEASE
                        - timedelta(seconds=1)
                    ),
                )
            )

            started = first_store.start_human_review_run(
                action["id"],
                child.id,
                first_claim["claim_token"],
                now=first_claimed_at,
            )
            self.assertIsNotNone(started)
            assert started is not None
            self.assertEqual(started.status, "running")
            persisted_running = first_store.get_human_review_action(action["id"])
            assert persisted_running is not None
            self.assertEqual(persisted_running["status"], "running")
            self.assertTrue(
                first_store.record_owned_human_review_triage(
                    action["id"],
                    first_claim["claim_token"],
                    decision="code_changes",
                    output='{"decision":"code_changes","reason":"test only"}',
                    now=first_claimed_at,
                )
            )
            advanced = first_store.update_owned_human_review_run(
                action["id"],
                child.id,
                first_claim["claim_token"],
                now=first_claimed_at,
                plan_spec_hash="f" * 64,
                automation_plan_hash="1" * 64,
                automation_development_diff_hash="2" * 64,
                automation_repository_diff_hash="3" * 64,
                automation_result_hash="4" * 64,
                plan_approval_id="approval-2",
            )
            assert advanced is not None
            self.assertEqual(advanced.plan_spec_hash, "f" * 64)
            self.assertEqual(advanced.automation_plan_hash, "1" * 64)
            self.assertEqual(advanced.automation_development_diff_hash, "2" * 64)
            self.assertEqual(advanced.automation_repository_diff_hash, "3" * 64)
            self.assertEqual(advanced.automation_result_hash, "4" * 64)
            self.assertEqual(advanced.plan_approval_id, "approval-2")

            expired_at = (
                first_claimed_at
                + HUMAN_RESUME_HANDOFF_LEASE
                + timedelta(seconds=1)
            )
            self.assertFalse(
                first_store.renew_human_review_action(
                    action["id"], first_claim["claim_token"], now=expired_at
                )
            )
            self.assertFalse(
                first_store.record_owned_human_review_triage(
                    action["id"],
                    first_claim["claim_token"],
                    decision="plan_changes_required",
                    output="stale owner must not overwrite triage",
                    now=expired_at,
                )
            )
            self.assertIsNone(
                first_store.update_owned_human_review_run(
                    action["id"],
                    child.id,
                    first_claim["claim_token"],
                    now=expired_at,
                    status="completed",
                    final_message="stale owner must not complete",
                )
            )

            recovered = restarted_store.claim_human_review_action(
                action["id"], now=expired_at
            )
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertNotEqual(
                recovered["claim_token"], first_claim["claim_token"]
            )
            self.assertEqual(recovered["status"], "queued")
            recovered_child = restarted_store.get_run(child.id)
            assert recovered_child is not None
            self.assertEqual(recovered_child.status, "queued")
            self.assertEqual(recovered_child.plan_spec_hash, action["plan_spec_hash"])
            self.assertEqual(
                recovered_child.automation_plan_hash,
                action["automation_plan_hash"],
            )
            self.assertEqual(
                recovered_child.automation_development_diff_hash,
                action["automation_development_diff_hash"],
            )
            self.assertEqual(
                recovered_child.automation_repository_diff_hash,
                action["automation_repository_diff_hash"],
            )
            self.assertEqual(
                recovered_child.automation_result_hash,
                action["automation_result_hash"],
            )
            self.assertEqual(
                recovered_child.plan_approval_id,
                action["plan_approval_id"],
            )
            self.assertFalse(
                first_store.release_human_review_action(
                    action["id"], first_claim["claim_token"]
                )
            )
            self.assertIsNone(
                first_store.start_human_review_run(
                    action["id"],
                    child.id,
                    first_claim["claim_token"],
                    now=expired_at,
                )
            )

            recovered_started = restarted_store.start_human_review_run(
                action["id"],
                child.id,
                recovered["claim_token"],
                now=expired_at,
            )
            self.assertIsNotNone(recovered_started)
            self.assertTrue(
                restarted_store.record_owned_human_review_triage(
                    action["id"],
                    recovered["claim_token"],
                    decision="code_changes",
                    output="recovered owner triage",
                    now=expired_at,
                )
            )
            completed = restarted_store.update_owned_human_review_run(
                action["id"],
                child.id,
                recovered["claim_token"],
                now=expired_at,
                status="completed",
                finished_at=expired_at,
                final_message="Human review addressed.",
                verification_status="passed",
            )
            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.final_message, "Human review addressed.")
            self.assertEqual(completed.verification_status, "passed")

            persisted_action = restarted_store.get_human_review_action(action["id"])
            assert persisted_action is not None
            self.assertEqual(persisted_action["status"], "completed")
            self.assertEqual(persisted_action["triage_decision"], "code_changes")
            self.assertEqual(persisted_action["triage_output"], "recovered owner triage")
            self.assertEqual(persisted_action["finished_at"], expired_at.isoformat())
            self.assertIsNone(persisted_action["claimed_at"])
            self.assertIsNone(persisted_action["claim_token"])
            self.assertEqual(
                restarted_store.list_recoverable_human_review_action_ids(), []
            )
            self.assertIsNone(
                restarted_store.update_owned_human_review_run(
                    action["id"],
                    child.id,
                    recovered["claim_token"],
                    now=expired_at,
                    final_message="terminal state must not be overwritten",
                )
            )
            terminal_child = restarted_store.get_run(child.id)
            assert terminal_child is not None
            self.assertEqual(
                terminal_child.final_message,
                "Human review addressed.",
            )

    def test_completed_child_becomes_the_next_actionable_review_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            source, approval = self._create_completed_source(store, root)
            first_action, first_child = self._create_action(store, source, approval)
            claimed_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
            claim = store.claim_human_review_action(
                first_action["id"], now=claimed_at
            )
            assert claim is not None
            started = store.start_human_review_run(
                first_action["id"],
                first_child.id,
                claim["claim_token"],
                now=claimed_at,
            )
            assert started is not None
            completed_child = store.update_owned_human_review_run(
                first_action["id"],
                first_child.id,
                claim["claim_token"],
                now=claimed_at,
                status="completed",
                finished_at=claimed_at,
                final_message="First human review addressed.",
            )
            assert completed_child is not None

            self.assertFalse(store.is_latest_actionable_completed_run(source.id))
            self.assertTrue(
                store.is_latest_actionable_completed_run(completed_child.id)
            )

            next_action, next_child = self._create_action(
                store, completed_child, approval
            )

            self.assertEqual(next_action["source_run_id"], completed_child.id)
            self.assertEqual(
                next_action["source_final_message"], completed_child.final_message
            )
            self.assertEqual(next_action["result_run_id"], next_child.id)
            self.assertEqual(
                next_child.issue_fingerprint, completed_child.issue_fingerprint
            )
            self.assertEqual(next_child.workspace_path, completed_child.workspace_path)
            self.assertEqual(next_child.branch_name, completed_child.branch_name)
            self.assertEqual(next_child.attempt, completed_child.attempt + 1)
            self.assertEqual(next_child.plan_spec_hash, completed_child.plan_spec_hash)
            self.assertEqual(
                next_child.plan_approval_id, completed_child.plan_approval_id
            )
            self.assertFalse(
                store.is_latest_actionable_completed_run(completed_child.id)
            )
            result_action = store.human_review_action_for_result_run(
                completed_child.id
            )
            assert result_action is not None
            self.assertEqual(result_action["id"], first_action["id"])
            self.assertEqual(
                store.list_human_review_actions_for_source_run(completed_child.id),
                [next_action],
            )
            self.assertEqual(
                {
                    action["id"]
                    for action in store.list_human_review_actions_for_issue("T-1")
                },
                {first_action["id"], next_action["id"]},
            )


class StoreRequirementsSnapshotIntegrityTests(unittest.TestCase):
    def test_get_rejects_tampered_snapshot_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            snapshot = RequirementsSnapshot(issue_id="1", issue_identifier="T-1", issue_url="u")
            record = store.save_requirements_snapshot(snapshot)
            with sqlite3.connect(store.db_path) as conn:
                row = conn.execute("SELECT snapshot_json FROM requirements_snapshots").fetchone()
                assert row is not None
                payload = json.loads(row[0])
                payload["issue_identifier"] = "T-2"
                conn.execute("UPDATE requirements_snapshots SET snapshot_json = ?", (json.dumps(payload),))
            with self.assertRaisesRegex(StoreIntegrityError, "stored model issue key"):
                store.get_requirements_snapshot("T-1", record["content_hash"])

    def test_save_rejects_conflicting_or_tampered_same_key_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "db.sqlite3")
            snapshot = RequirementsSnapshot(issue_id="1", issue_identifier="T-1", issue_url="u")
            store.save_requirements_snapshot(snapshot)
            with sqlite3.connect(store.db_path) as conn:
                row = conn.execute("SELECT snapshot_json FROM requirements_snapshots").fetchone()
                assert row is not None
                payload = json.loads(row[0])
                payload["issue_id"] = "tampered"
                conn.execute("UPDATE requirements_snapshots SET snapshot_json = ?", (json.dumps(payload),))

            with self.assertRaisesRegex(StoreIntegrityError, "canonical content hash"):
                store.save_requirements_snapshot(snapshot)


def store_automation_plan_payload(
    *,
    issue_key: str,
    requirements_snapshot_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "decision": "no_update_required",
        "issue_key": issue_key,
        "requirements_snapshot_hash": requirements_snapshot_hash,
        "development_plan_spec_hash": "b" * 64,
        "development_workspace_diff_hash": "c" * 64,
        "automation_repository": "automation",
        "repository_baseline_sha": "d" * 40,
        "rationale": "Existing automation already covers the behavior.",
        "mapped_scenarios": [],
        "affected_file_changes": [],
        "verification": [],
        "risks": [],
        "assumptions": [],
        "open_questions": [],
    }


def make_issue() -> Issue:
    return Issue(
        id="1",
        identifier="T-1",
        title="Title",
        status="To Do",
        labels=["codex-ready"],
        url="https://jira.example.test/browse/T-1",
    )


if __name__ == "__main__":
    unittest.main()
