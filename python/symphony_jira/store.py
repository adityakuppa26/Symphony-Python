from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import CodexEvent, Issue, RunRecord, issue_description_fingerprint, utc_now


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
                  finished_at TEXT,
                  final_message TEXT,
                  error TEXT,
                  blocked_phase TEXT,
                  branch_name TEXT,
                  verification_status TEXT,
                  verification_output_path TEXT
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
                  consumed_at TEXT,
                  created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "runs", "issue_fingerprint", "TEXT")
            self._ensure_column(conn, "runs", "blocked_phase", "TEXT")

    def create_run(
        self,
        issue: Issue,
        workspace_path: Path,
        *,
        branch_name: str | None,
        attempt: int = 1,
        status: str = "queued",
    ) -> RunRecord:
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
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                  id, issue_id, issue_identifier, issue_fingerprint,
                  workspace_path, status, attempt,
                  started_at, finished_at, final_message, error, blocked_phase, branch_name,
                  verification_status, verification_output_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                run_values(record),
            )
        return record

    def update_run(self, run_id: str, **fields: Any) -> RunRecord:
        allowed = {
            "status",
            "finished_at",
            "final_message",
            "error",
            "blocked_phase",
            "verification_status",
            "verification_output_path",
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
                "SELECT * FROM runs WHERE issue_identifier = ? ORDER BY started_at DESC LIMIT ?",
                (issue_identifier, limit),
            ).fetchall()
        return [run_from_row(row) for row in rows]

    def latest_run_for_issue(self, issue_identifier: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE issue_identifier = ?
                ORDER BY COALESCE(finished_at, started_at) DESC
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
                ORDER BY COALESCE(finished_at, started_at) DESC
                LIMIT 1
                """,
                (issue_identifier,),
            ).fetchone()
        return run_from_row(row) if row else None

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
    ) -> dict[str, Any]:
        record = {
            "id": str(uuid.uuid4()),
            "issue_identifier": issue_identifier,
            "run_id": run_id,
            "question": question,
            "response": response,
            "consumed_at": None,
            "created_at": utc_now().isoformat(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO human_inputs (
                  id, issue_identifier, run_id, question, response, consumed_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"],
                    record["issue_identifier"],
                    record["run_id"],
                    record["question"],
                    record["response"],
                    record["consumed_at"],
                    record["created_at"],
                ),
            )
        return record

    def list_human_inputs(self, run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM human_inputs WHERE run_id = ? ORDER BY created_at DESC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM human_inputs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def list_unconsumed_human_inputs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM human_inputs
                WHERE consumed_at IS NULL
                ORDER BY created_at
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_unconsumed_human_input_for_issue(self, issue_identifier: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM human_inputs
                WHERE issue_identifier = ?
                  AND consumed_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (issue_identifier,),
            ).fetchone()
        return dict(row) if row else None

    def mark_human_input_consumed(self, input_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE human_inputs SET consumed_at = ? WHERE id = ?",
                (utc_now().isoformat(), input_id),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


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
        serialize_value(record.finished_at),
        record.final_message,
        record.error,
        record.blocked_phase,
        record.branch_name,
        record.verification_status,
        record.verification_output_path,
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
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        final_message=row["final_message"],
        error=row["error"],
        blocked_phase=row["blocked_phase"],
        branch_name=row["branch_name"],
        verification_status=row["verification_status"],
        verification_output_path=row["verification_output_path"],
    )
