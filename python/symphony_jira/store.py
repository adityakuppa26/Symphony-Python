from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .automation_plan import AutomationPlan, automation_result_content_hash
from .models import (
    CodexEvent,
    Issue,
    RequirementsSnapshot,
    RunRecord,
    issue_description_fingerprint,
    requirements_planning_authority_equivalent,
    utc_now,
)


HUMAN_INPUT_CLAIM_LEASE = timedelta(hours=6)
HUMAN_RESUME_HANDOFF_LEASE = timedelta(minutes=5)


class StoreIntegrityError(RuntimeError):
    """Persisted trusted state failed its canonical integrity contract."""


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY,
                  issue_id TEXT NOT NULL,
                  issue_identifier TEXT NOT NULL,
                  issue_fingerprint TEXT,
                  workspace_path TEXT NOT NULL,
                  status TEXT NOT NULL,
                  attempt INTEGER NOT NULL,
                  started_at TEXT NOT NULL,
                  plan_spec_hash TEXT,
                  automation_plan_hash TEXT,
                  automation_development_diff_hash TEXT,
                  automation_repository_diff_hash TEXT,
                  automation_result_hash TEXT,
                  plan_approval_id TEXT,
                  finished_at TEXT,
                  final_message TEXT,
                  error TEXT,
                  blocked_phase TEXT,
                  branch_name TEXT,
                  verification_status TEXT,
                  verification_output_path TEXT,
                  verification_workspace_diff_hash TEXT,
                  verification_evidence_sha256 TEXT
                );

                CREATE TABLE IF NOT EXISTS codex_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  sequence INTEGER NOT NULL,
                  event_type TEXT NOT NULL,
                  raw_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT,
                  level TEXT NOT NULL,
                  message TEXT NOT NULL,
                  path TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jira_actions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  issue_identifier TEXT NOT NULL,
                  run_id TEXT,
                  action TEXT NOT NULL,
                  body TEXT,
                  status TEXT NOT NULL,
                  error TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS human_inputs (
                  id TEXT PRIMARY KEY,
                  issue_identifier TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  question TEXT,
                  response TEXT NOT NULL,
                  approval_id TEXT,
                  action TEXT NOT NULL DEFAULT 'response',
                  approver_identity TEXT,
                  workspace_diff_hash TEXT,
                  verification_evidence_sha256 TEXT,
                  claimed_at TEXT,
                  claim_token TEXT,
                  consumed_at TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plan_approvals (
                  id TEXT PRIMARY KEY,
                  issue_identifier TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  approver_identity TEXT NOT NULL,
                  plan_spec_hash TEXT NOT NULL,
                  requirements_snapshot_hash TEXT NOT NULL,
                  approved_at TEXT NOT NULL,
                  invalidated_at TEXT,
                  invalidation_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS requirements_snapshots (
                  issue_identifier TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  schema_version TEXT NOT NULL,
                  snapshot_json TEXT NOT NULL,
                  captured_at TEXT NOT NULL,
                  stored_at TEXT NOT NULL,
                  PRIMARY KEY (issue_identifier, content_hash)
                );


                CREATE TABLE IF NOT EXISTS human_resume_handoffs (
                  resume_run_id TEXT PRIMARY KEY,
                  input_id TEXT NOT NULL UNIQUE,
                  predecessor_run_id TEXT NOT NULL,
                  claimed_at TEXT,
                  claim_token TEXT,
                  started_at TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS human_review_actions (
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
                  automation_plan_hash TEXT,
                  automation_development_diff_hash TEXT,
                  automation_repository_diff_hash TEXT,
                  automation_result_hash TEXT,
                  automation_plan TEXT,
                  automation_result TEXT,
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
                );

                CREATE INDEX IF NOT EXISTS idx_human_resume_handoffs_input
                  ON human_resume_handoffs (input_id);
                CREATE INDEX IF NOT EXISTS idx_human_review_actions_issue
                  ON human_review_actions (issue_identifier, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_human_review_actions_status
                  ON human_review_actions (status, created_at);
                CREATE INDEX IF NOT EXISTS idx_requirements_snapshots_version
                  ON requirements_snapshots (issue_identifier, captured_at DESC, stored_at DESC);

                CREATE INDEX IF NOT EXISTS idx_plan_approvals_run
                  ON plan_approvals (run_id, approved_at DESC);
                """
            )
            self._ensure_column(conn, "runs", "issue_fingerprint", "TEXT")
            self._ensure_column(conn, "runs", "blocked_phase", "TEXT")
            self._ensure_column(conn, "human_inputs", "approval_id", "TEXT")
            self._ensure_column(conn, "human_inputs", "claimed_at", "TEXT")
            self._ensure_column(conn, "human_inputs", "claim_token", "TEXT")
            self._ensure_column(
                conn,
                "human_inputs",
                "action",
                "TEXT NOT NULL DEFAULT 'response'",
            )
            self._ensure_column(conn, "human_inputs", "approver_identity", "TEXT")
            self._ensure_column(conn, "human_inputs", "workspace_diff_hash", "TEXT")
            self._ensure_column(
                conn,
                "human_inputs",
                "verification_evidence_sha256",
                "TEXT",
            )
            self._ensure_column(conn, "runs", "plan_spec_hash", "TEXT")
            self._ensure_column(conn, "runs", "automation_plan_hash", "TEXT")
            self._ensure_column(
                conn,
                "runs",
                "automation_development_diff_hash",
                "TEXT",
            )
            self._ensure_column(
                conn,
                "runs",
                "automation_repository_diff_hash",
                "TEXT",
            )
            self._ensure_column(conn, "runs", "automation_result_hash", "TEXT")
            self._ensure_column(conn, "runs", "plan_approval_id", "TEXT")
            self._ensure_column(
                conn,
                "runs",
                "verification_workspace_diff_hash",
                "TEXT",
            )
            self._ensure_column(
                conn,
                "runs",
                "verification_evidence_sha256",
                "TEXT",
            )
            self._ensure_column(conn, "human_review_actions", "plan_spec", "TEXT")
            self._ensure_column(
                conn,
                "human_review_actions",
                "automation_plan_hash",
                "TEXT",
            )
            self._ensure_column(
                conn,
                "human_review_actions",
                "automation_development_diff_hash",
                "TEXT",
            )
            self._ensure_column(
                conn,
                "human_review_actions",
                "automation_repository_diff_hash",
                "TEXT",
            )
            self._ensure_column(
                conn,
                "human_review_actions",
                "automation_result_hash",
                "TEXT",
            )
            self._ensure_column(
                conn,
                "human_review_actions",
                "automation_plan",
                "TEXT",
            )
            self._ensure_column(
                conn,
                "human_review_actions",
                "automation_result",
                "TEXT",
            )

    def save_requirements_snapshot(self, snapshot: RequirementsSnapshot) -> dict[str, Any]:
        issue_identifier = snapshot.issue_identifier.strip()
        if not issue_identifier:
            raise ValueError("requirements snapshot issue identifier is required")

        normalized = snapshot.with_content_hash()
        if not normalized.schema_version.strip():
            raise ValueError("requirements snapshot schema version is required")
        record = {
            "issue_identifier": issue_identifier,
            "content_hash": normalized.content_hash,
            "schema_version": normalized.schema_version,
            "snapshot_json": json.dumps(
                normalized.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "captured_at": normalized.captured_at.isoformat(),
            "stored_at": utc_now().isoformat(),
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT issue_identifier, content_hash, schema_version,
                       snapshot_json, captured_at, stored_at
                FROM requirements_snapshots
                WHERE issue_identifier = ? AND content_hash = ?
                """,
                (issue_identifier, normalized.content_hash),
            ).fetchone()
            if existing is not None:
                stored = self._validated_requirements_snapshot_row(
                    existing,
                    expected_issue_identifier=issue_identifier,
                    expected_content_hash=normalized.content_hash,
                )
                if stored.canonical_content() != normalized.canonical_content():
                    raise StoreIntegrityError(
                        "requirements snapshot hash collision or canonical content conflict"
                    )
                return dict(existing)
            conn.execute(
                """
                INSERT INTO requirements_snapshots (
                  issue_identifier, content_hash, schema_version,
                  snapshot_json, captured_at, stored_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record["issue_identifier"],
                    record["content_hash"],
                    record["schema_version"],
                    record["snapshot_json"],
                    record["captured_at"],
                    record["stored_at"],
                ),
            )
        return record

    def get_requirements_snapshot(
        self,
        issue_identifier: str,
        content_hash: str,
    ) -> RequirementsSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT issue_identifier, content_hash, schema_version,
                       snapshot_json, captured_at, stored_at
                FROM requirements_snapshots
                WHERE issue_identifier = ? AND content_hash = ?
                """,
                (issue_identifier, content_hash),
            ).fetchone()
        if row is None:
            return None
        return self._validated_requirements_snapshot_row(
            row,
            expected_issue_identifier=issue_identifier,
            expected_content_hash=content_hash,
        )

    def list_requirements_snapshot_versions(
        self,
        issue_identifier: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT issue_identifier, content_hash, schema_version, captured_at, stored_at
                FROM requirements_snapshots
                WHERE issue_identifier = ?
                ORDER BY captured_at DESC, stored_at DESC
                LIMIT ?
                """,
                (issue_identifier, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_run(
        self,
        issue: Issue,
        workspace_path: Path,
        *,
        branch_name: str | None,
        attempt: int = 1,
        status: str = "queued",
        plan_spec_hash: str | None = None,
        automation_plan_hash: str | None = None,
        automation_development_diff_hash: str | None = None,
        automation_repository_diff_hash: str | None = None,
        automation_result_hash: str | None = None,
        plan_approval_id: str | None = None,
        require_no_active_run: bool = False,
    ) -> RunRecord:
        if issue.requirements_snapshot is not None:
            if issue.requirements_snapshot.issue_identifier != issue.identifier:
                raise ValueError(
                    "requirements snapshot issue identifier does not match the run issue"
                )
            self.save_requirements_snapshot(issue.requirements_snapshot)
        record = RunRecord(
            id=str(uuid.uuid4()),
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            issue_fingerprint=issue_description_fingerprint(issue),
            workspace_path=str(workspace_path),
            status=status,
            attempt=attempt,
            started_at=utc_now(),
            branch_name=branch_name,
            plan_spec_hash=plan_spec_hash,
            automation_plan_hash=automation_plan_hash,
            automation_development_diff_hash=automation_development_diff_hash,
            automation_repository_diff_hash=automation_repository_diff_hash,
            automation_result_hash=automation_result_hash,
            plan_approval_id=plan_approval_id,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if require_no_active_run:
                active = conn.execute(
                    """
                    SELECT id FROM runs
                    WHERE issue_identifier = ? AND status IN ('queued', 'running')
                    ORDER BY started_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (issue.identifier,),
                ).fetchone()
                if active is not None:
                    raise ValueError(
                        f"issue {issue.identifier} already has an active queued/running run"
                    )
            self._insert_run(conn, record)
        return record

    def create_human_review_action(
        self,
        source_run_id: str,
        *,
        reviewer_identity: str,
        source_url: str,
        comments: str,
        plan_spec: str,
        approval: dict[str, Any] | None,
        source_review: str | None,
        source_review_history: str | None,
        workspace_diff: str,
        workspace_diff_hash: str,
        automation_plan_hash: str | None = None,
        automation_plan: str | None = None,
        automation_result: str | None = None,
    ) -> tuple[dict[str, Any], RunRecord]:
        """Atomically freeze a completed review request and reserve its child run."""

        reviewer = reviewer_identity.strip()
        review_source = source_url.strip()
        review_comments = comments.strip()
        diff_hash = workspace_diff_hash.strip()
        supplied_automation_hash = str(automation_plan_hash or "").strip().lower() or None
        automation_plan_text = str(automation_plan or "").strip()
        automation_result_text = str(automation_result or "").strip()
        if not reviewer:
            raise ValueError("reviewer identity is required")
        if not review_source:
            raise ValueError("review source/PR link is required")
        if not review_comments:
            raise ValueError("review comments are required")
        if not diff_hash:
            raise ValueError("workspace diff hash is required")

        now = utc_now()
        action_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source_row = conn.execute(
                "SELECT * FROM runs WHERE id = ?",
                (source_run_id,),
            ).fetchone()
            if source_row is None:
                raise ValueError("source run does not exist")
            if not self._is_latest_actionable_completed_run(conn, source_run_id):
                raise ValueError(
                    "run is not the latest actionable completed run for this issue"
                )

            source_run = run_from_row(source_row)
            requirements_snapshot_hash = str(source_run.issue_fingerprint or "").strip()
            if not requirements_snapshot_hash:
                raise ValueError("completed run has no requirements snapshot hash")
            plan_hash = str(source_run.plan_spec_hash or "").strip() or None
            plan_text = plan_spec.strip()
            if plan_hash and not plan_text:
                raise ValueError("completed run has no frozen PlanSpec content")
            approval_id = str(source_run.plan_approval_id or "").strip() or None
            frozen_approval: dict[str, Any] | None = None
            if approval_id:
                approval_row = conn.execute(
                    "SELECT * FROM plan_approvals WHERE id = ?",
                    (approval_id,),
                ).fetchone()
                if approval_row is None:
                    raise ValueError(
                        "completed run's persisted plan approval is missing"
                    )
                frozen_approval = dict(approval_row)
                if approval is None or frozen_approval != approval:
                    raise ValueError(
                        "completed run approval does not match the frozen approval record"
                    )
                if frozen_approval.get("invalidated_at"):
                    raise ValueError(
                        "completed run's persisted plan approval is no longer active"
                    )
                if (
                    frozen_approval.get("issue_identifier")
                    != source_run.issue_identifier
                    or frozen_approval.get("plan_spec_hash") != plan_hash
                    or frozen_approval.get("requirements_snapshot_hash")
                    != requirements_snapshot_hash
                ):
                    raise ValueError(
                        "completed run's persisted plan approval does not match "
                        "its issue, PlanSpec, and requirements snapshot"
                    )
            elif approval is not None:
                raise ValueError("completed run has no approval identity")

            source_automation_hash = (
                str(source_run.automation_plan_hash or "").strip().lower() or None
            )
            source_development_diff_hash: str | None = None
            source_repository_diff_hash: str | None = None
            source_result_hash: str | None = None
            if source_automation_hash is not None:
                source_automation_hash = normalize_sha256(
                    source_automation_hash,
                    "completed run automation plan hash",
                )
                if supplied_automation_hash is None:
                    raise ValueError(
                        "completed run has no frozen AutomationPlan hash"
                    )
                supplied_automation_hash = normalize_sha256(
                    supplied_automation_hash,
                    "frozen AutomationPlan hash",
                )
                if supplied_automation_hash != source_automation_hash:
                    raise ValueError(
                        "frozen AutomationPlan hash does not match the completed run"
                    )
                if not automation_plan_text:
                    raise ValueError(
                        "completed run has no frozen AutomationPlan content"
                    )
                try:
                    frozen_automation_plan = AutomationPlan.model_validate_json(
                        automation_plan_text
                    )
                except ValueError as exc:
                    raise ValueError(
                        "completed run frozen AutomationPlan content is invalid"
                    ) from exc
                if frozen_automation_plan.content_hash() != source_automation_hash:
                    raise ValueError(
                        "frozen AutomationPlan content does not match the completed run"
                    )
                source_development_diff_hash = normalize_sha256(
                    str(source_run.automation_development_diff_hash or ""),
                    "completed run automation development-diff hash",
                )
                if (
                    frozen_automation_plan.development_workspace_diff_hash
                    != source_development_diff_hash
                ):
                    raise ValueError(
                        "frozen AutomationPlan development diff does not match the "
                        "completed run"
                    )
                source_repository_diff_hash = normalize_sha256(
                    str(source_run.automation_repository_diff_hash or ""),
                    "completed run automation repository-diff hash",
                )
                if not automation_result_text:
                    raise ValueError(
                        "completed run has no frozen automation result"
                    )
                source_result_hash = normalize_sha256(
                    str(source_run.automation_result_hash or ""),
                    "completed run automation result hash",
                )
                if (
                    automation_result_content_hash(automation_result_text)
                    != source_result_hash
                ):
                    raise ValueError(
                        "frozen automation result does not match the completed run"
                    )
            elif (
                supplied_automation_hash is not None
                or automation_plan_text
                or automation_result_text
                or source_run.automation_development_diff_hash is not None
                or source_run.automation_repository_diff_hash is not None
                or source_run.automation_result_hash is not None
            ):
                raise ValueError(
                    "completed run has no automation-plan identity"
                )

            result_run = RunRecord(
                id=str(uuid.uuid4()),
                issue_id=source_run.issue_id,
                issue_identifier=source_run.issue_identifier,
                issue_fingerprint=requirements_snapshot_hash,
                workspace_path=source_run.workspace_path,
                status="queued",
                attempt=source_run.attempt + 1,
                started_at=now,
                branch_name=source_run.branch_name,
                plan_spec_hash=plan_hash,
                automation_plan_hash=source_automation_hash,
                automation_development_diff_hash=(
                    source_development_diff_hash
                ),
                automation_repository_diff_hash=(
                    source_repository_diff_hash
                ),
                automation_result_hash=source_result_hash,
                plan_approval_id=approval_id,
            )
            self._insert_run(conn, result_run)
            conn.execute(
                """
                INSERT INTO human_review_actions (
                  id, issue_identifier, source_run_id, result_run_id,
                  reviewer_identity, source_url, comments,
                  requirements_snapshot_hash, plan_spec_hash, plan_spec,
                  automation_plan_hash, automation_development_diff_hash,
                  automation_repository_diff_hash, automation_result_hash,
                  automation_plan, automation_result,
                  plan_approval_id, approval_json, source_final_message,
                  source_review, source_review_history, workspace_diff,
                  workspace_diff_hash, triage_decision, triage_output,
                  status, claimed_at, claim_token, started_at, finished_at,
                  created_at
                )
                VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    action_id,
                    source_run.issue_identifier,
                    source_run.id,
                    result_run.id,
                    reviewer,
                    review_source,
                    review_comments,
                    requirements_snapshot_hash,
                    plan_hash,
                    plan_text or None,
                    source_automation_hash,
                    source_development_diff_hash,
                    source_repository_diff_hash,
                    source_result_hash,
                    automation_plan_text or None,
                    automation_result_text or None,
                    approval_id,
                    json.dumps(
                        frozen_approval,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if frozen_approval is not None
                    else None,
                    source_run.final_message,
                    source_review,
                    source_review_history,
                    workspace_diff,
                    diff_hash,
                    None,
                    None,
                    "queued",
                    None,
                    None,
                    None,
                    None,
                    now.isoformat(),
                ),
            )

        action = self.get_human_review_action(action_id)
        if action is None:
            raise StoreIntegrityError("human review action disappeared after creation")
        return action, result_run

    def get_human_review_action(self, action_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM human_review_actions WHERE id = ?",
                (action_id,),
            ).fetchone()
        return human_review_action_from_row(row) if row else None

    def human_review_action_for_result_run(
        self,
        result_run_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM human_review_actions WHERE result_run_id = ?",
                (result_run_id,),
            ).fetchone()
        return human_review_action_from_row(row) if row else None

    def list_human_review_actions_for_source_run(
        self,
        source_run_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM human_review_actions
                WHERE source_run_id = ?
                ORDER BY created_at DESC
                """,
                (source_run_id,),
            ).fetchall()
        return [human_review_action_from_row(row) for row in rows]

    def list_human_review_actions_for_issue(
        self,
        issue_identifier: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM human_review_actions
                WHERE issue_identifier = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (issue_identifier, limit),
            ).fetchall()
        return [human_review_action_from_row(row) for row in rows]

    def is_latest_actionable_completed_run(self, run_id: str) -> bool:
        with self._connect() as conn:
            return self._is_latest_actionable_completed_run(conn, run_id)

    def reserve_human_resume(
        self,
        issue: Issue,
        workspace_path: Path,
        *,
        input_id: str,
        claim_token: str,
        expected_predecessor_run_id: str,
        branch_name: str | None,
        attempt: int,
    ) -> tuple[RunRecord | None, str]:
        """Atomically consume one claimed input and create its fenced queued resume run."""

        if issue.requirements_snapshot is not None:
            if issue.requirements_snapshot.issue_identifier != issue.identifier:
                raise ValueError(
                    "requirements snapshot issue identifier does not match the run issue"
                )
            self.save_requirements_snapshot(issue.requirements_snapshot)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            human_input = conn.execute(
                """
                SELECT id, issue_identifier, run_id, approval_id, claim_token, consumed_at
                FROM human_inputs
                WHERE id = ?
                """,
                (input_id,),
            ).fetchone()
            if (
                human_input is None
                or human_input["claim_token"] != claim_token
                or human_input["consumed_at"] is not None
            ):
                return None, "claim_lost"

            predecessor_row = conn.execute(
                "SELECT * FROM runs WHERE id = ?",
                (expected_predecessor_run_id,),
            ).fetchone()
            if (
                predecessor_row is None
                or human_input["run_id"] != expected_predecessor_run_id
                or human_input["issue_identifier"] != issue.identifier
                or predecessor_row["issue_identifier"] != issue.identifier
                or predecessor_row["issue_id"] != issue.id
            ):
                raise ValueError("human resume identity does not match its predecessor run")

            if not self._is_latest_actionable_blocked_run(conn, expected_predecessor_run_id):
                cursor = conn.execute(
                    """
                    UPDATE human_inputs
                    SET consumed_at = ?, claimed_at = NULL, claim_token = NULL
                    WHERE id = ? AND claim_token = ? AND consumed_at IS NULL
                    """,
                    (utc_now().isoformat(), input_id, claim_token),
                )
                if cursor.rowcount != 1:
                    return None, "claim_lost"
                return None, "stale_predecessor"

            predecessor = run_from_row(predecessor_row)
            approval_id = human_input["approval_id"]
            if approval_id and approval_id != predecessor.plan_approval_id:
                raise StoreIntegrityError(
                    "human resume approval identity does not match its predecessor run"
                )
            requirements_fingerprint = issue_description_fingerprint(issue)
            if (
                predecessor.blocked_phase == "verification_environment"
                and predecessor.plan_spec_hash
                and predecessor.plan_approval_id
                and predecessor.issue_fingerprint
                and issue.requirements_snapshot is not None
                and issue.requirements_snapshot.schema_version
                == "jira-requirements/v4"
            ):
                frozen_row = conn.execute(
                    """
                    SELECT issue_identifier, content_hash, schema_version,
                           snapshot_json, captured_at, stored_at
                    FROM requirements_snapshots
                    WHERE issue_identifier = ? AND content_hash = ?
                    """,
                    (
                        predecessor.issue_identifier,
                        predecessor.issue_fingerprint,
                    ),
                ).fetchone()
                approval_row = conn.execute(
                    "SELECT * FROM plan_approvals WHERE id = ?",
                    (predecessor.plan_approval_id,),
                ).fetchone()
                if frozen_row is not None and approval_row is not None:
                    frozen_snapshot = self._validated_requirements_snapshot_row(
                        frozen_row,
                        expected_issue_identifier=predecessor.issue_identifier,
                        expected_content_hash=predecessor.issue_fingerprint,
                    )
                    frozen_approval = dict(approval_row)
                    if (
                        frozen_snapshot.schema_version
                        in {
                            "jira-requirements/v1",
                            "jira-requirements/v2",
                            "jira-requirements/v3",
                        }
                        and not frozen_approval.get("invalidated_at")
                        and frozen_approval.get("issue_identifier")
                        == predecessor.issue_identifier
                        and frozen_approval.get("plan_spec_hash")
                        == predecessor.plan_spec_hash
                        and frozen_approval.get("requirements_snapshot_hash")
                        == predecessor.issue_fingerprint
                        and requirements_planning_authority_equivalent(
                            frozen_snapshot,
                            issue.requirements_snapshot,
                        )
                    ):
                        # Keep the queued resume fenced to the exact historical
                        # PlanSpec/approval pair. The current v4 snapshot was
                        # saved above for audit and is rechecked by the runner.
                        requirements_fingerprint = predecessor.issue_fingerprint
            record = RunRecord(
                id=str(uuid.uuid4()),
                issue_id=issue.id,
                issue_identifier=issue.identifier,
                issue_fingerprint=requirements_fingerprint,
                workspace_path=str(workspace_path),
                status="queued",
                attempt=attempt,
                started_at=utc_now(),
                branch_name=branch_name,
                plan_spec_hash=predecessor.plan_spec_hash,
                automation_plan_hash=predecessor.automation_plan_hash,
                automation_development_diff_hash=(
                    predecessor.automation_development_diff_hash
                ),
                automation_repository_diff_hash=(
                    predecessor.automation_repository_diff_hash
                ),
                automation_result_hash=predecessor.automation_result_hash,
                plan_approval_id=predecessor.plan_approval_id,
            )
            self._insert_run(conn, record)
            conn.execute(
                """
                INSERT INTO human_resume_handoffs (
                  resume_run_id, input_id, predecessor_run_id,
                  claimed_at, claim_token, started_at, created_at
                )
                VALUES (?, ?, ?, NULL, NULL, NULL, ?)
                """,
                (
                    record.id, input_id, expected_predecessor_run_id,
                    record.started_at.isoformat(),
                ),
            )
            cursor = conn.execute(
                """
                UPDATE human_inputs
                SET consumed_at = ?, claimed_at = NULL, claim_token = NULL
                WHERE id = ? AND claim_token = ? AND consumed_at IS NULL
                """,
                (utc_now().isoformat(), input_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise StoreIntegrityError("human resume claim changed during atomic reservation")
        return record, "reserved"

    def list_recoverable_human_resume_run_ids(self, limit: int = 100) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT h.resume_run_id
                FROM human_resume_handoffs AS h
                JOIN runs AS r ON r.id = h.resume_run_id
                WHERE r.status IN ('queued', 'running')
                ORDER BY h.created_at
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(row["resume_run_id"]) for row in rows]

    def claim_human_resume_handoff(
        self,
        resume_run_id: str,
        *,
        now: datetime | None = None,
        lease: timedelta = HUMAN_RESUME_HANDOFF_LEASE,
    ) -> dict[str, Any] | None:
        if lease <= timedelta(0):
            raise ValueError("human resume handoff claim lease must be positive")
        claim_time = now or utc_now()
        claimed_at = claim_time.isoformat()
        stale_before = (claim_time - lease).isoformat()
        claim_token = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE human_resume_handoffs
                SET claimed_at = ?, claim_token = ?
                WHERE resume_run_id = ?
                  AND (claimed_at IS NULL OR claimed_at <= ?)
                  AND EXISTS (
                    SELECT 1 FROM runs AS r
                    WHERE r.id = human_resume_handoffs.resume_run_id
                      AND r.status IN ('queued', 'running')
                  )
                """,
                (claimed_at, claim_token, resume_run_id, stale_before),
            )
            if cursor.rowcount != 1:
                return None
            predecessor = conn.execute(
                """
                SELECT p.plan_spec_hash, p.automation_plan_hash,
                       p.automation_development_diff_hash,
                       p.automation_repository_diff_hash,
                       p.automation_result_hash, p.plan_approval_id
                FROM human_resume_handoffs AS h
                JOIN runs AS p ON p.id = h.predecessor_run_id
                WHERE h.resume_run_id = ?
                """,
                (resume_run_id,),
            ).fetchone()
            if predecessor is None:
                raise StoreIntegrityError(
                    "human resume predecessor disappeared during stale reclaim"
                )
            conn.execute(
                """
                UPDATE runs SET status = 'queued', finished_at = NULL, error = NULL,
                                blocked_phase = NULL, plan_spec_hash = ?,
                                automation_plan_hash = ?,
                                automation_development_diff_hash = ?,
                                automation_repository_diff_hash = ?,
                                automation_result_hash = ?, plan_approval_id = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    predecessor["plan_spec_hash"],
                    predecessor["automation_plan_hash"],
                    predecessor["automation_development_diff_hash"],
                    predecessor["automation_repository_diff_hash"],
                    predecessor["automation_result_hash"],
                    predecessor["plan_approval_id"],
                    resume_run_id,
                ),
            )
            row = conn.execute(
                """
                SELECT i.*, h.resume_run_id, h.predecessor_run_id,
                       h.claimed_at AS handoff_claimed_at,
                       h.claim_token AS handoff_claim_token,
                       a.plan_spec_hash, a.requirements_snapshot_hash,
                       a.approver_identity AS plan_approver_identity,
                       a.approved_at,
                       a.invalidated_at AS approval_invalidated_at,
                       a.invalidation_reason AS approval_invalidation_reason
                FROM human_resume_handoffs AS h
                JOIN human_inputs AS i ON i.id = h.input_id
                LEFT JOIN plan_approvals AS a ON a.id = i.approval_id
                WHERE h.resume_run_id = ?
                """,
                (resume_run_id,),
            ).fetchone()
        return human_input_from_row(row) if row else None

    def release_human_resume_handoff(self, resume_run_id: str, claim_token: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE human_resume_handoffs
                SET claimed_at = NULL, claim_token = NULL
                WHERE resume_run_id = ? AND claim_token = ?
                  AND EXISTS (
                    SELECT 1 FROM runs AS r
                    WHERE r.id = human_resume_handoffs.resume_run_id
                      AND r.status = 'queued'
                  )
                """,
                (resume_run_id, claim_token),
            )
        return cursor.rowcount == 1

    def start_human_resume_run(
        self,
        resume_run_id: str,
        claim_token: str,
        *,
        now: datetime | None = None,
        lease: timedelta = HUMAN_RESUME_HANDOFF_LEASE,
    ) -> RunRecord | None:
        if lease <= timedelta(0):
            raise ValueError("human resume handoff claim lease must be positive")
        claim_time = now or utc_now()
        claimed_at = claim_time.isoformat()
        stale_before = (claim_time - lease).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            handoff_cursor = conn.execute(
                """
                UPDATE human_resume_handoffs
                SET started_at = COALESCE(started_at, ?), claimed_at = ?
                WHERE resume_run_id = ? AND claim_token = ?
                  AND claimed_at > ?
                  AND EXISTS (
                    SELECT 1 FROM runs AS r
                    WHERE r.id = human_resume_handoffs.resume_run_id
                      AND r.status = 'queued'
                  )
                """,
                (claimed_at, claimed_at, resume_run_id, claim_token, stale_before),
            )
            if handoff_cursor.rowcount != 1:
                return None
            run_cursor = conn.execute(
                "UPDATE runs SET status = 'running' WHERE id = ? AND status = 'queued'",
                (resume_run_id,),
            )
            if run_cursor.rowcount != 1:
                raise StoreIntegrityError(
                    "human resume run changed during its fenced start"
                )
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ?",
                (resume_run_id,),
            ).fetchone()
        return run_from_row(row) if row else None

    def renew_human_resume_handoff(
        self,
        resume_run_id: str,
        claim_token: str,
        *,
        now: datetime | None = None,
        lease: timedelta = HUMAN_RESUME_HANDOFF_LEASE,
    ) -> bool:
        if lease <= timedelta(0):
            raise ValueError("human resume handoff claim lease must be positive")
        claim_time = now or utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE human_resume_handoffs
                SET claimed_at = ?
                WHERE resume_run_id = ? AND claim_token = ?
                  AND claimed_at > ?
                  AND EXISTS (
                    SELECT 1 FROM runs AS r
                    WHERE r.id = human_resume_handoffs.resume_run_id
                      AND r.status IN ('queued', 'running')
                  )
                """,
                (
                    claim_time.isoformat(),
                    resume_run_id,
                    claim_token,
                    (claim_time - lease).isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def update_owned_human_resume_run(
        self,
        resume_run_id: str,
        claim_token: str,
        *,
        now: datetime | None = None,
        lease: timedelta = HUMAN_RESUME_HANDOFF_LEASE,
        **fields: Any,
    ) -> RunRecord | None:
        if lease <= timedelta(0):
            raise ValueError("human resume handoff claim lease must be positive")
        claim_time = now or utc_now()
        allowed = {
            "status",
            "finished_at",
            "final_message",
            "error",
            "blocked_phase",
            "verification_status",
            "verification_output_path",
            "verification_workspace_diff_hash",
            "verification_evidence_sha256",
            "plan_spec_hash",
            "automation_plan_hash",
            "automation_development_diff_hash",
            "automation_repository_diff_hash",
            "automation_result_hash",
            "plan_approval_id",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ownership = conn.execute(
                """
                SELECT 1 FROM human_resume_handoffs
                WHERE resume_run_id = ? AND claim_token = ? AND claimed_at > ?
                """,
                (
                    resume_run_id,
                    claim_token,
                    (claim_time - lease).isoformat(),
                ),
            ).fetchone()
            if ownership is None:
                return None
            if updates:
                assignments = ", ".join(f"{key} = ?" for key in updates)
                values = [serialize_value(value) for value in updates.values()]
                values.append(resume_run_id)
                cursor = conn.execute(
                    f"UPDATE runs SET {assignments} WHERE id = ?",
                    values,
                )
                if cursor.rowcount != 1:
                    raise StoreIntegrityError(
                        "owned human resume run disappeared during update"
                    )
            if updates.get("status") in {"completed", "blocked", "failed", "cancelled"}:
                retired = conn.execute(
                    """
                    UPDATE human_resume_handoffs
                    SET claimed_at = NULL, claim_token = NULL
                    WHERE resume_run_id = ? AND claim_token = ?
                    """,
                    (resume_run_id, claim_token),
                )
                if retired.rowcount != 1:
                    raise StoreIntegrityError(
                        "human resume handoff ownership changed during terminal update"
                    )
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ?",
                (resume_run_id,),
            ).fetchone()
        return run_from_row(row) if row else None

    def get_human_resume_handoff(self, resume_run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM human_resume_handoffs WHERE resume_run_id = ?",
                (resume_run_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_recoverable_human_review_action_ids(
        self,
        limit: int = 100,
    ) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.id
                FROM human_review_actions AS a
                JOIN runs AS r ON r.id = a.result_run_id
                WHERE a.status IN ('queued', 'running')
                  AND r.status IN ('queued', 'running')
                ORDER BY a.created_at
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def claim_human_review_action(
        self,
        action_id: str,
        *,
        now: datetime | None = None,
        lease: timedelta = HUMAN_RESUME_HANDOFF_LEASE,
    ) -> dict[str, Any] | None:
        if lease <= timedelta(0):
            raise ValueError("human review action claim lease must be positive")
        claim_time = now or utc_now()
        claimed_at = claim_time.isoformat()
        stale_before = (claim_time - lease).isoformat()
        claim_token = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE human_review_actions
                SET claimed_at = ?, claim_token = ?, status = 'queued'
                WHERE id = ?
                  AND status IN ('queued', 'running')
                  AND (claimed_at IS NULL OR claimed_at <= ?)
                  AND EXISTS (
                    SELECT 1 FROM runs AS r
                    WHERE r.id = human_review_actions.result_run_id
                      AND r.status IN ('queued', 'running')
                  )
                """,
                (claimed_at, claim_token, action_id, stale_before),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM human_review_actions WHERE id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise StoreIntegrityError(
                    "human review action disappeared during stale reclaim"
                )
            conn.execute(
                """
                UPDATE runs
                SET status = 'queued', finished_at = NULL, error = NULL,
                    blocked_phase = NULL, plan_spec_hash = ?,
                    automation_plan_hash = ?,
                    automation_development_diff_hash = ?,
                    automation_repository_diff_hash = ?,
                    automation_result_hash = ?, plan_approval_id = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    row["plan_spec_hash"],
                    row["automation_plan_hash"],
                    row["automation_development_diff_hash"],
                    row["automation_repository_diff_hash"],
                    row["automation_result_hash"],
                    row["plan_approval_id"],
                    row["result_run_id"],
                ),
            )
        return human_review_action_from_row(row) if row else None

    def release_human_review_action(
        self,
        action_id: str,
        claim_token: str,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE human_review_actions
                SET claimed_at = NULL, claim_token = NULL
                WHERE id = ? AND claim_token = ? AND status = 'queued'
                  AND EXISTS (
                    SELECT 1 FROM runs AS r
                    WHERE r.id = human_review_actions.result_run_id
                      AND r.status = 'queued'
                  )
                """,
                (action_id, claim_token),
            )
        return cursor.rowcount == 1

    def start_human_review_run(
        self,
        action_id: str,
        result_run_id: str,
        claim_token: str,
        *,
        now: datetime | None = None,
        lease: timedelta = HUMAN_RESUME_HANDOFF_LEASE,
    ) -> RunRecord | None:
        if lease <= timedelta(0):
            raise ValueError("human review action claim lease must be positive")
        claim_time = now or utc_now()
        claimed_at = claim_time.isoformat()
        stale_before = (claim_time - lease).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            action_cursor = conn.execute(
                """
                UPDATE human_review_actions
                SET status = 'running', started_at = COALESCE(started_at, ?),
                    claimed_at = ?
                WHERE id = ? AND result_run_id = ? AND claim_token = ?
                  AND status = 'queued' AND claimed_at > ?
                  AND EXISTS (
                    SELECT 1 FROM runs AS r
                    WHERE r.id = human_review_actions.result_run_id
                      AND r.status = 'queued'
                  )
                """,
                (
                    claimed_at,
                    claimed_at,
                    action_id,
                    result_run_id,
                    claim_token,
                    stale_before,
                ),
            )
            if action_cursor.rowcount != 1:
                return None
            run_cursor = conn.execute(
                "UPDATE runs SET status = 'running' WHERE id = ? AND status = 'queued'",
                (result_run_id,),
            )
            if run_cursor.rowcount != 1:
                raise StoreIntegrityError(
                    "human review result run changed during its fenced start"
                )
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ?",
                (result_run_id,),
            ).fetchone()
        return run_from_row(row) if row else None

    def renew_human_review_action(
        self,
        action_id: str,
        claim_token: str,
        *,
        now: datetime | None = None,
        lease: timedelta = HUMAN_RESUME_HANDOFF_LEASE,
    ) -> bool:
        if lease <= timedelta(0):
            raise ValueError("human review action claim lease must be positive")
        claim_time = now or utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE human_review_actions
                SET claimed_at = ?
                WHERE id = ? AND claim_token = ? AND claimed_at > ?
                  AND status IN ('queued', 'running')
                  AND EXISTS (
                    SELECT 1 FROM runs AS r
                    WHERE r.id = human_review_actions.result_run_id
                      AND r.status IN ('queued', 'running')
                  )
                """,
                (
                    claim_time.isoformat(),
                    action_id,
                    claim_token,
                    (claim_time - lease).isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def record_owned_human_review_triage(
        self,
        action_id: str,
        claim_token: str,
        *,
        decision: str,
        output: str,
        now: datetime | None = None,
        lease: timedelta = HUMAN_RESUME_HANDOFF_LEASE,
    ) -> bool:
        if lease <= timedelta(0):
            raise ValueError("human review action claim lease must be positive")
        claim_time = now or utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE human_review_actions
                SET triage_decision = ?, triage_output = ?
                WHERE id = ? AND claim_token = ? AND claimed_at > ?
                  AND status = 'running'
                """,
                (
                    decision,
                    output,
                    action_id,
                    claim_token,
                    (claim_time - lease).isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def update_owned_human_review_run(
        self,
        action_id: str,
        result_run_id: str,
        claim_token: str,
        *,
        now: datetime | None = None,
        lease: timedelta = HUMAN_RESUME_HANDOFF_LEASE,
        **fields: Any,
    ) -> RunRecord | None:
        if lease <= timedelta(0):
            raise ValueError("human review action claim lease must be positive")
        claim_time = now or utc_now()
        allowed = {
            "status",
            "finished_at",
            "final_message",
            "error",
            "blocked_phase",
            "verification_status",
            "verification_output_path",
            "verification_workspace_diff_hash",
            "verification_evidence_sha256",
            "plan_spec_hash",
            "automation_plan_hash",
            "automation_development_diff_hash",
            "automation_repository_diff_hash",
            "automation_result_hash",
            "plan_approval_id",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            ownership = conn.execute(
                """
                SELECT 1 FROM human_review_actions
                WHERE id = ? AND result_run_id = ? AND claim_token = ?
                  AND claimed_at > ? AND status IN ('queued', 'running')
                """,
                (
                    action_id,
                    result_run_id,
                    claim_token,
                    (claim_time - lease).isoformat(),
                ),
            ).fetchone()
            if ownership is None:
                return None
            if updates:
                assignments = ", ".join(f"{key} = ?" for key in updates)
                values = [serialize_value(value) for value in updates.values()]
                values.append(result_run_id)
                cursor = conn.execute(
                    f"UPDATE runs SET {assignments} WHERE id = ?",
                    values,
                )
                if cursor.rowcount != 1:
                    raise StoreIntegrityError(
                        "owned human review result run disappeared during update"
                    )

            terminal_status = updates.get("status")
            if terminal_status in {"completed", "blocked", "failed", "cancelled"}:
                finished_at = serialize_value(
                    updates.get("finished_at") or claim_time
                )
                retired = conn.execute(
                    """
                    UPDATE human_review_actions
                    SET status = ?, finished_at = ?,
                        claimed_at = NULL, claim_token = NULL
                    WHERE id = ? AND result_run_id = ? AND claim_token = ?
                    """,
                    (
                        terminal_status,
                        finished_at,
                        action_id,
                        result_run_id,
                        claim_token,
                    ),
                )
                if retired.rowcount != 1:
                    raise StoreIntegrityError(
                        "human review action ownership changed during terminal update"
                    )
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ?",
                (result_run_id,),
            ).fetchone()
        return run_from_row(row) if row else None

    def update_run(self, run_id: str, **fields: Any) -> RunRecord:
        allowed = {
            "status",
            "finished_at",
            "final_message",
            "error",
            "blocked_phase",
            "verification_status",
            "verification_output_path",
            "verification_workspace_diff_hash",
            "verification_evidence_sha256",
            "plan_spec_hash",
            "automation_plan_hash",
            "automation_development_diff_hash",
            "automation_repository_diff_hash",
            "automation_result_hash",
            "plan_approval_id",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            record = self.get_run(run_id)
            assert record is not None
            return record

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [serialize_value(value) for value in updates.values()]
        values.append(run_id)
        with self._connect() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", values)
        record = self.get_run(run_id)
        assert record is not None
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return run_from_row(row) if row else None

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [run_from_row(row) for row in rows]

    def list_runs_for_issue(self, issue_identifier: str, limit: int = 20) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE issue_identifier = ? ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (issue_identifier, limit),
            ).fetchall()
        return [run_from_row(row) for row in rows]

    def latest_run_for_issue(self, issue_identifier: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE issue_identifier = ?
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """,
                (issue_identifier,),
            ).fetchone()
        return run_from_row(row) if row else None

    def latest_blocked_run_for_issue(self, issue_identifier: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE issue_identifier = ?
                  AND status = 'blocked'
                ORDER BY started_at DESC, rowid DESC
                LIMIT 1
                """,
                (issue_identifier,),
            ).fetchone()
        return run_from_row(row) if row else None

    def is_latest_actionable_blocked_run(self, run_id: str) -> bool:
        with self._connect() as conn:
            return self._is_latest_actionable_blocked_run(conn, run_id)

    def latest_completed_run_for_issue_fingerprint(
        self,
        issue_identifier: str,
        issue_fingerprint: str,
    ) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE issue_identifier = ?
                  AND issue_fingerprint = ?
                  AND status = 'completed'
                ORDER BY COALESCE(finished_at, started_at) DESC
                LIMIT 1
                """,
                (issue_identifier, issue_fingerprint),
            ).fetchone()
        return run_from_row(row) if row else None

    def add_codex_event(self, run_id: str, sequence: int, event_type: str, raw_json: dict[str, Any]) -> CodexEvent:
        created_at = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO codex_events (run_id, sequence, event_type, raw_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, sequence, event_type, json.dumps(raw_json, sort_keys=True), created_at.isoformat()),
            )
        return CodexEvent(run_id=run_id, sequence=sequence, event_type=event_type, raw_json=raw_json, created_at=created_at)

    def list_codex_events(self, run_id: str) -> list[CodexEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, sequence, event_type, raw_json, created_at FROM codex_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        return [
            CodexEvent(
                run_id=row["run_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                raw_json=json.loads(row["raw_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def add_log(self, run_id: str | None, level: str, message: str, path: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO logs (run_id, level, message, path, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, level, message, path, utc_now().isoformat()),
            )

    def list_logs(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM logs WHERE run_id = ? ORDER BY created_at DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def add_jira_action(
        self,
        issue_identifier: str,
        *,
        run_id: str | None,
        action: str,
        body: str | None,
        status: str,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jira_actions (issue_identifier, run_id, action, body, status, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (issue_identifier, run_id, action, body, status, error, utc_now().isoformat()),
            )

    def list_jira_actions(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM jira_actions WHERE run_id = ? ORDER BY created_at DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM jira_actions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def add_human_input(
        self,
        issue_identifier: str,
        *,
        run_id: str,
        response: str,
        question: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": str(uuid.uuid4()),
            "issue_identifier": issue_identifier,
            "run_id": run_id,
            "question": question,
            "response": response,
            "approval_id": approval_id,
            "action": "response",
            "approver_identity": None,
            "workspace_diff_hash": None,
            "verification_evidence_sha256": None,
            "claimed_at": None,
            "claim_token": None,
            "consumed_at": None,
            "created_at": utc_now().isoformat(),
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_human_input_submission_allowed(
                conn,
                issue_identifier=issue_identifier,
                run_id=run_id,
            )
            conn.execute(
                """
                INSERT INTO human_inputs (
                  id, issue_identifier, run_id, question, response, approval_id,
                  action, approver_identity, workspace_diff_hash,
                  verification_evidence_sha256, claimed_at, claim_token,
                  consumed_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["issue_identifier"],
                    record["run_id"],
                    record["question"],
                    record["response"],
                    record["approval_id"],
                    record["action"],
                    record["approver_identity"],
                    record["workspace_diff_hash"],
                    record["verification_evidence_sha256"],
                    record["claimed_at"],
                    record["claim_token"],
                    record["consumed_at"],
                    record["created_at"],
                ),
            )
        return record

    def add_verification_bypass_input(
        self,
        issue_identifier: str,
        *,
        run_id: str,
        approver_identity: str,
        workspace_diff_hash: str,
        verification_evidence_sha256: str,
        question: str | None = None,
    ) -> dict[str, Any]:
        """Atomically queue an explicit override bound to code and verification evidence."""

        approver = " ".join(approver_identity.split())
        if not approver:
            raise ValueError("approver identity is required")
        diff_hash = normalize_sha256(
            workspace_diff_hash,
            "workspace diff hash",
        )
        evidence_hash = normalize_sha256(
            verification_evidence_sha256,
            "verification evidence SHA-256",
        )
        record = {
            "id": str(uuid.uuid4()),
            "issue_identifier": issue_identifier,
            "run_id": run_id,
            "question": question,
            "response": "Verification bypass approved.",
            "approval_id": None,
            "action": "verification_bypass",
            "approver_identity": approver,
            "workspace_diff_hash": diff_hash,
            "verification_evidence_sha256": evidence_hash,
            "claimed_at": None,
            "claim_token": None,
            "consumed_at": None,
            "created_at": utc_now().isoformat(),
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_human_input_submission_allowed(
                conn,
                issue_identifier=issue_identifier,
                run_id=run_id,
            )
            run = conn.execute(
                """
                SELECT status, blocked_phase, verification_status,
                       verification_output_path,
                       verification_workspace_diff_hash,
                       verification_evidence_sha256
                FROM runs
                WHERE id = ? AND issue_identifier = ?
                """,
                (run_id, issue_identifier),
            ).fetchone()
            if (
                run is None
                or run["status"] != "blocked"
                or run["blocked_phase"]
                not in {"verification", "verification_environment"}
                or run["verification_status"]
                in {None, "passed", "not_configured"}
                or not str(run["verification_output_path"] or "").strip()
            ):
                raise ValueError(
                    "run is not blocked with retained failed verification evidence"
                )
            try:
                persisted_diff_hash = normalize_sha256(
                    str(run["verification_workspace_diff_hash"] or ""),
                    "persisted verification workspace diff hash",
                )
                persisted_evidence_hash = normalize_sha256(
                    str(run["verification_evidence_sha256"] or ""),
                    "persisted verification evidence SHA-256",
                )
            except ValueError as exc:
                raise ValueError(
                    "failed run has no valid verification-time integrity binding"
                ) from exc
            if (
                diff_hash != persisted_diff_hash
                or evidence_hash != persisted_evidence_hash
            ):
                raise ValueError(
                    "verification bypass does not match the failed run integrity binding"
                )
            conn.execute(
                """
                INSERT INTO human_inputs (
                  id, issue_identifier, run_id, question, response, approval_id,
                  action, approver_identity, workspace_diff_hash,
                  verification_evidence_sha256, claimed_at, claim_token,
                  consumed_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(
                    record[key]
                    for key in (
                        "id",
                        "issue_identifier",
                        "run_id",
                        "question",
                        "response",
                        "approval_id",
                        "action",
                        "approver_identity",
                        "workspace_diff_hash",
                        "verification_evidence_sha256",
                        "claimed_at",
                        "claim_token",
                        "consumed_at",
                        "created_at",
                    )
                ),
            )
        return record

    def add_approved_human_input(
        self,
        issue_identifier: str,
        *,
        run_id: str,
        approver_identity: str,
        plan_spec_hash: str,
        requirements_snapshot_hash: str,
        question: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically bind an exact plan approval to its resume input."""
        approver = approver_identity.strip()
        plan_hash = plan_spec_hash.strip()
        requirements_hash = requirements_snapshot_hash.strip()
        if not approver:
            raise ValueError("approver identity is required")
        if not plan_hash:
            raise ValueError("plan spec hash is required")
        if not requirements_hash:
            raise ValueError("requirements snapshot hash is required")

        now = utc_now().isoformat()
        approval = {
            "id": str(uuid.uuid4()),
            "issue_identifier": issue_identifier,
            "run_id": run_id,
            "approver_identity": approver,
            "plan_spec_hash": plan_hash,
            "requirements_snapshot_hash": requirements_hash,
            "approved_at": now,
            "invalidated_at": None,
            "invalidation_reason": None,
        }
        human_input = {
            "id": str(uuid.uuid4()),
            "issue_identifier": issue_identifier,
            "run_id": run_id,
            "question": question,
            "response": "Approved.",
            "approval_id": approval["id"],
            "action": "plan_approval",
            "approver_identity": approver,
            "workspace_diff_hash": None,
            "verification_evidence_sha256": None,
            "claimed_at": None,
            "claim_token": None,
            "consumed_at": None,
            "created_at": now,
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_human_input_submission_allowed(
                conn,
                issue_identifier=issue_identifier,
                run_id=run_id,
            )
            conn.execute(
                """
                UPDATE plan_approvals
                SET invalidated_at = ?, invalidation_reason = ?
                WHERE run_id = ? AND invalidated_at IS NULL
                """,
                (now, "superseded by a newer approval", run_id),
            )
            conn.execute(
                """
                INSERT INTO plan_approvals (
                  id, issue_identifier, run_id, approver_identity,
                  plan_spec_hash, requirements_snapshot_hash, approved_at,
                  invalidated_at, invalidation_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(approval[key] for key in (
                    "id",
                    "issue_identifier",
                    "run_id",
                    "approver_identity",
                    "plan_spec_hash",
                    "requirements_snapshot_hash",
                    "approved_at",
                    "invalidated_at",
                    "invalidation_reason",
                )),
            )
            cursor = conn.execute(
                """
                UPDATE runs
                SET plan_spec_hash = ?, plan_approval_id = ?
                WHERE id = ? AND issue_identifier = ?
                """,
                (plan_hash, approval["id"], run_id, issue_identifier),
            )
            if cursor.rowcount != 1:
                raise ValueError("approval run does not exist or belongs to another issue")
            conn.execute(
                """
                INSERT INTO human_inputs (
                  id, issue_identifier, run_id, question, response, approval_id,
                  action, approver_identity, workspace_diff_hash,
                  verification_evidence_sha256, claimed_at, claim_token,
                  consumed_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(human_input[key] for key in (
                    "id",
                    "issue_identifier",
                    "run_id",
                    "question",
                    "response",
                    "approval_id",
                    "action",
                    "approver_identity",
                    "workspace_diff_hash",
                    "verification_evidence_sha256",
                    "claimed_at",
                    "claim_token",
                    "consumed_at",
                    "created_at",
                )),
            )
        return human_input, approval

    def add_plan_approval(
        self,
        issue_identifier: str,
        *,
        run_id: str,
        approver_identity: str,
        plan_spec_hash: str,
        requirements_snapshot_hash: str,
    ) -> dict[str, Any]:
        approver = approver_identity.strip()
        plan_hash = plan_spec_hash.strip()
        requirements_hash = requirements_snapshot_hash.strip()
        if not approver:
            raise ValueError("approver identity is required")
        if not plan_hash:
            raise ValueError("plan spec hash is required")
        if not requirements_hash:
            raise ValueError("requirements snapshot hash is required")

        now = utc_now().isoformat()
        record = {
            "id": str(uuid.uuid4()),
            "issue_identifier": issue_identifier,
            "run_id": run_id,
            "approver_identity": approver,
            "plan_spec_hash": plan_hash,
            "requirements_snapshot_hash": requirements_hash,
            "approved_at": now,
            "invalidated_at": None,
            "invalidation_reason": None,
        }
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE plan_approvals
                SET invalidated_at = ?, invalidation_reason = ?
                WHERE run_id = ? AND invalidated_at IS NULL
                """,
                (now, "superseded by a newer approval", run_id),
            )
            conn.execute(
                """
                INSERT INTO plan_approvals (
                  id, issue_identifier, run_id, approver_identity,
                  plan_spec_hash, requirements_snapshot_hash, approved_at,
                  invalidated_at, invalidation_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["issue_identifier"],
                    record["run_id"],
                    record["approver_identity"],
                    record["plan_spec_hash"],
                    record["requirements_snapshot_hash"],
                    record["approved_at"],
                    record["invalidated_at"],
                    record["invalidation_reason"],
                ),
            )
            conn.execute(
                """
                UPDATE runs SET plan_spec_hash = ?, plan_approval_id = ? WHERE id = ?
                """,
                (plan_hash, record["id"], run_id),
            )
        return record

    def list_plan_approvals(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM plan_approvals WHERE run_id = ? ORDER BY approved_at DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM plan_approvals ORDER BY approved_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def latest_plan_approval_for_run(self, run_id: str, *, active_only: bool = False) -> dict[str, Any] | None:
        predicate = " AND invalidated_at IS NULL" if active_only else ""
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM plan_approvals
                WHERE run_id = ?{predicate}
                ORDER BY approved_at DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_plan_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plan_approvals WHERE id = ?",
                (approval_id,),
            ).fetchone()
        return dict(row) if row else None

    def invalidate_plan_approval(self, approval_id: str, reason: str) -> dict[str, Any] | None:
        invalidation_reason = reason.strip()
        if not invalidation_reason:
            raise ValueError("invalidation reason is required")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE plan_approvals
                SET invalidated_at = ?, invalidation_reason = ?
                WHERE id = ? AND invalidated_at IS NULL
                """,
                (utc_now().isoformat(), invalidation_reason, approval_id),
            )
            row = conn.execute("SELECT * FROM plan_approvals WHERE id = ?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def invalidate_active_plan_approvals_for_issue(
        self,
        issue_identifier: str,
        reason: str,
    ) -> int:
        invalidation_reason = reason.strip()
        if not invalidation_reason:
            raise ValueError("invalidation reason is required")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE plan_approvals
                SET invalidated_at = ?, invalidation_reason = ?
                WHERE issue_identifier = ? AND invalidated_at IS NULL
                """,
                (utc_now().isoformat(), invalidation_reason, issue_identifier),
            )
        return cursor.rowcount
    def resolve_active_plan_approval(
        self,
        run_id: str,
        *,
        plan_spec_hash: str,
        requirements_snapshot_hash: str,
    ) -> dict[str, Any] | None:
        approval = self.latest_plan_approval_for_run(run_id, active_only=True)
        if approval is None:
            return None

        changed: list[str] = []
        if approval["plan_spec_hash"] != plan_spec_hash:
            changed.append("PlanSpec")
        if approval["requirements_snapshot_hash"] != requirements_snapshot_hash:
            changed.append("requirements snapshot")
        if not changed:
            return approval

        self.invalidate_plan_approval(
            str(approval["id"]),
            f"{' and '.join(changed)} changed after approval",
        )
        return None

    def list_human_inputs(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    """
                    SELECT h.*, a.plan_spec_hash, a.requirements_snapshot_hash,
                           a.approver_identity AS plan_approver_identity,
                           a.approved_at,
                           a.invalidated_at AS approval_invalidated_at,
                           a.invalidation_reason AS approval_invalidation_reason
                    FROM human_inputs AS h
                    LEFT JOIN plan_approvals AS a ON a.id = h.approval_id
                    WHERE h.run_id = ?
                    ORDER BY h.created_at DESC
                    LIMIT ?
                    """,
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT h.*, a.plan_spec_hash, a.requirements_snapshot_hash,
                           a.approver_identity AS plan_approver_identity,
                           a.approved_at,
                           a.invalidated_at AS approval_invalidated_at,
                           a.invalidation_reason AS approval_invalidation_reason
                    FROM human_inputs AS h
                    LEFT JOIN plan_approvals AS a ON a.id = h.approval_id
                    ORDER BY h.created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [human_input_from_row(row) for row in rows]

    def list_unconsumed_human_inputs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT h.*, a.plan_spec_hash, a.requirements_snapshot_hash,
                       a.approver_identity AS plan_approver_identity,
                       a.approved_at,
                       a.invalidated_at AS approval_invalidated_at,
                       a.invalidation_reason AS approval_invalidation_reason
                FROM human_inputs AS h
                LEFT JOIN plan_approvals AS a ON a.id = h.approval_id
                WHERE h.consumed_at IS NULL
                ORDER BY h.created_at
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [human_input_from_row(row) for row in rows]

    def latest_unconsumed_human_input_for_issue(self, issue_identifier: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT h.*, a.plan_spec_hash, a.requirements_snapshot_hash,
                       a.approver_identity AS plan_approver_identity,
                       a.approved_at,
                       a.invalidated_at AS approval_invalidated_at,
                       a.invalidation_reason AS approval_invalidation_reason
                FROM human_inputs AS h
                LEFT JOIN plan_approvals AS a ON a.id = h.approval_id
                WHERE h.issue_identifier = ?
                  AND h.consumed_at IS NULL
                ORDER BY h.created_at DESC
                LIMIT 1
                """,
                (issue_identifier,),
            ).fetchone()
        return human_input_from_row(row) if row else None

    def claim_human_input(
        self,
        input_id: str,
        *,
        now: datetime | None = None,
        lease: timedelta = HUMAN_INPUT_CLAIM_LEASE,
    ) -> dict[str, Any] | None:
        if lease <= timedelta(0):
            raise ValueError("human input claim lease must be positive")
        claim_time = now or utc_now()
        claimed_at = claim_time.isoformat()
        stale_before = (claim_time - lease).isoformat()
        claim_token = str(uuid.uuid4())
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE human_inputs
                SET claimed_at = ?, claim_token = ?
                WHERE id = ?
                  AND consumed_at IS NULL
                  AND (
                    claimed_at IS NULL
                    OR claimed_at <= ?
                  )
                """,
                (claimed_at, claim_token, input_id, stale_before),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                """
                SELECT h.*, a.plan_spec_hash, a.requirements_snapshot_hash,
                       a.approver_identity AS plan_approver_identity,
                       a.approved_at,
                       a.invalidated_at AS approval_invalidated_at,
                       a.invalidation_reason AS approval_invalidation_reason
                FROM human_inputs AS h
                LEFT JOIN plan_approvals AS a ON a.id = h.approval_id
                WHERE h.id = ?
                """,
                (input_id,),
            ).fetchone()
        return human_input_from_row(row) if row else None

    def renew_human_input_claim(
        self,
        input_id: str,
        claim_token: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE human_inputs
                SET claimed_at = ?
                WHERE id = ?
                  AND claim_token = ?
                  AND consumed_at IS NULL
                """,
                ((now or utc_now()).isoformat(), input_id, claim_token),
            )
        return cursor.rowcount == 1

    def release_human_input_claim(self, input_id: str, claim_token: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE human_inputs
                SET claimed_at = NULL, claim_token = NULL
                WHERE id = ?
                  AND claim_token = ?
                  AND consumed_at IS NULL
                """,
                (input_id, claim_token),
            )
        return cursor.rowcount == 1

    def mark_human_input_consumed(self, input_id: str, claim_token: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE human_inputs
                SET consumed_at = ?, claimed_at = NULL, claim_token = NULL
                WHERE id = ?
                  AND claim_token = ?
                  AND consumed_at IS NULL
                """,
                (utc_now().isoformat(), input_id, claim_token),
            )
        return cursor.rowcount == 1

    def _assert_human_input_submission_allowed(
        self,
        conn: sqlite3.Connection,
        *,
        issue_identifier: str,
        run_id: str,
    ) -> None:
        run = conn.execute(
            "SELECT issue_identifier FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if (
            run is None
            or run["issue_identifier"] != issue_identifier
            or not self._is_latest_actionable_blocked_run(conn, run_id)
        ):
            raise ValueError("run is not the latest actionable blocked run for this issue")
        pending = conn.execute(
            """
            SELECT id, run_id, approval_id FROM human_inputs
            WHERE issue_identifier = ? AND consumed_at IS NULL
            ORDER BY created_at
            """,
            (issue_identifier,),
        ).fetchall()
        if any(row["run_id"] == run_id for row in pending):
            raise ValueError("human input is already pending for this issue")
        if not pending:
            return

        # A newer blocked run supersedes responses queued for older attempts. Keep
        # the history, but retire those responses atomically before accepting input
        # for the current run. This also fences a dispatcher that claimed an old
        # response immediately before the newer run was created.
        retired_at = utc_now().isoformat()
        pending_ids = [str(row["id"]) for row in pending]
        placeholders = ",".join("?" for _ in pending_ids)
        conn.execute(
            f"""
            UPDATE human_inputs
            SET consumed_at = ?, claimed_at = NULL, claim_token = NULL
            WHERE id IN ({placeholders}) AND consumed_at IS NULL
            """,
            (retired_at, *pending_ids),
        )
        approval_ids = [
            str(row["approval_id"])
            for row in pending
            if row["approval_id"] is not None
        ]
        if approval_ids:
            approval_placeholders = ",".join("?" for _ in approval_ids)
            conn.execute(
                f"""
                UPDATE plan_approvals
                SET invalidated_at = ?, invalidation_reason = ?
                WHERE id IN ({approval_placeholders}) AND invalidated_at IS NULL
                """,
                (
                    retired_at,
                    "superseded by human input for a newer run",
                    *approval_ids,
                ),
            )

    def _is_latest_actionable_blocked_run(
        self,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT current.status,
                   (
                     SELECT latest.id
                     FROM runs AS latest
                     WHERE latest.issue_identifier = current.issue_identifier
                     ORDER BY latest.started_at DESC, latest.rowid DESC
                     LIMIT 1
                   ) AS latest_run_id
            FROM runs AS current
            WHERE current.id = ?
            """,
            (run_id,),
        ).fetchone()
        return bool(
            row
            and row["status"] == "blocked"
            and row["latest_run_id"] == run_id
        )

    def _is_latest_actionable_completed_run(
        self,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT current.status,
                   (
                     SELECT latest.id
                     FROM runs AS latest
                     WHERE latest.issue_identifier = current.issue_identifier
                     ORDER BY latest.started_at DESC, latest.rowid DESC
                     LIMIT 1
                   ) AS latest_run_id
            FROM runs AS current
            WHERE current.id = ?
            """,
            (run_id,),
        ).fetchone()
        return bool(
            row
            and row["status"] == "completed"
            and row["latest_run_id"] == run_id
        )

    def _validated_requirements_snapshot_row(
        self,
        row: sqlite3.Row,
        *,
        expected_issue_identifier: str,
        expected_content_hash: str,
    ) -> RequirementsSnapshot:
        row_issue_identifier = str(row["issue_identifier"] or "")
        row_content_hash = str(row["content_hash"] or "")
        if row_issue_identifier != expected_issue_identifier:
            raise StoreIntegrityError(
                "requirements snapshot integrity check failed: row issue key does not match requested issue"
            )
        if row_content_hash != expected_content_hash:
            raise StoreIntegrityError(
                "requirements snapshot integrity check failed: row hash does not match requested hash"
            )
        try:
            payload = json.loads(row["snapshot_json"])
            snapshot = RequirementsSnapshot.model_validate(payload)
        except (TypeError, ValueError) as exc:
            raise StoreIntegrityError(
                "requirements snapshot integrity check failed: stored JSON is invalid"
            ) from exc
        if snapshot.issue_identifier != expected_issue_identifier:
            raise StoreIntegrityError(
                "requirements snapshot integrity check failed: stored model issue key does not match row key"
            )
        if snapshot.content_hash != expected_content_hash:
            raise StoreIntegrityError(
                "requirements snapshot integrity check failed: stored model content_hash does not match row key"
            )
        if snapshot.schema_version != str(row["schema_version"] or ""):
            raise StoreIntegrityError(
                "requirements snapshot integrity check failed: stored schema version does not match row"
            )
        calculated_hash = snapshot.calculate_content_hash()
        if calculated_hash != expected_content_hash:
            raise StoreIntegrityError(
                "requirements snapshot integrity check failed: canonical content hash does not match row key"
            )
        return snapshot

    @staticmethod
    def _insert_run(conn: sqlite3.Connection, record: RunRecord) -> None:
        conn.execute(
            """
            INSERT INTO runs (
              id, issue_id, issue_identifier, issue_fingerprint,
              workspace_path, status, attempt,
              started_at, plan_spec_hash, automation_plan_hash,
              automation_development_diff_hash, automation_repository_diff_hash,
              automation_result_hash,
              plan_approval_id,
              finished_at, final_message, error, blocked_phase, branch_name,
              verification_status, verification_output_path,
              verification_workspace_diff_hash, verification_evidence_sha256
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            run_values(record),
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def human_review_action_from_row(row: sqlite3.Row) -> dict[str, Any]:
    action = dict(row)
    approval_json = action.pop("approval_json", None)
    if approval_json:
        try:
            approval = json.loads(approval_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StoreIntegrityError(
                "human review action contains invalid frozen approval JSON"
            ) from exc
        if not isinstance(approval, dict):
            raise StoreIntegrityError(
                "human review action frozen approval must be an object"
            )
        action["approval"] = approval
    else:
        action["approval"] = None
    return action


def human_input_from_row(row: sqlite3.Row) -> dict[str, Any]:
    human_input = dict(row)
    plan_approver = human_input.pop("plan_approver_identity", None)
    if not human_input.get("approver_identity"):
        human_input["approver_identity"] = plan_approver
    return human_input


def normalize_sha256(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be a 64-character hexadecimal SHA-256")
    return normalized


def run_values(record: RunRecord) -> tuple[Any, ...]:
    return (
        record.id,
        record.issue_id,
        record.issue_identifier,
        record.issue_fingerprint,
        record.workspace_path,
        record.status,
        record.attempt,
        record.started_at.isoformat(),
        record.plan_spec_hash,
        record.automation_plan_hash,
        record.automation_development_diff_hash,
        record.automation_repository_diff_hash,
        record.automation_result_hash,
        record.plan_approval_id,
        serialize_value(record.finished_at),
        record.final_message,
        record.error,
        record.blocked_phase,
        record.branch_name,
        record.verification_status,
        record.verification_output_path,
        record.verification_workspace_diff_hash,
        record.verification_evidence_sha256,
    )


def serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        issue_id=row["issue_id"],
        issue_identifier=row["issue_identifier"],
        issue_fingerprint=row["issue_fingerprint"],
        workspace_path=row["workspace_path"],
        status=row["status"],
        attempt=row["attempt"],
        started_at=datetime.fromisoformat(row["started_at"]),
        plan_spec_hash=row["plan_spec_hash"],
        automation_plan_hash=row["automation_plan_hash"],
        automation_development_diff_hash=row[
            "automation_development_diff_hash"
        ],
        automation_repository_diff_hash=row[
            "automation_repository_diff_hash"
        ],
        automation_result_hash=row["automation_result_hash"],
        plan_approval_id=row["plan_approval_id"],
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        final_message=row["final_message"],
        error=row["error"],
        blocked_phase=row["blocked_phase"],
        branch_name=row["branch_name"],
        verification_status=row["verification_status"],
        verification_output_path=row["verification_output_path"],
        verification_workspace_diff_hash=row[
            "verification_workspace_diff_hash"
        ],
        verification_evidence_sha256=row["verification_evidence_sha256"],
    )
