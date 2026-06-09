from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


RunStatus = Literal["queued", "running", "blocked", "failed", "completed", "cancelled"]


class IssueComment(BaseModel):
    id: str | None = None
    author: str | None = None
    body: str | None = None
    created: datetime | None = None


class IssueBlocker(BaseModel):
    id: str | None = None
    identifier: str | None = None
    status: str | None = None


class Issue(BaseModel):
    id: str
    identifier: str
    title: str
    description: str | None = None
    status: str
    priority: str | None = None
    issue_type: str | None = None
    assignee: str | None = None
    reporter: str | None = None
    labels: list[str] = Field(default_factory=list)
    url: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    comments: list[IssueComment] = Field(default_factory=list)
    blocked_by: list[IssueBlocker] = Field(default_factory=list)
    raw: dict[str, Any] | None = None


class RunRecord(BaseModel):
    id: str
    issue_id: str
    issue_identifier: str
    issue_fingerprint: str | None = None
    workspace_path: str
    status: RunStatus
    attempt: int
    started_at: datetime
    finished_at: datetime | None = None
    final_message: str | None = None
    error: str | None = None
    blocked_phase: str | None = None
    branch_name: str | None = None
    verification_status: str | None = None
    verification_output_path: str | None = None


class CodexEvent(BaseModel):
    run_id: str
    sequence: int
    event_type: str
    raw_json: dict[str, Any]
    created_at: datetime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def issue_description_fingerprint(issue: Issue) -> str:
    return hashlib.sha256((issue.description or "").encode("utf-8")).hexdigest()
