from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


RunStatus = Literal["queued", "running", "blocked", "failed", "completed", "cancelled"]
RequirementClassification = Literal[
    "current",
    "superseded",
    "inferred",
    "unresolved_contradiction",
]
RequirementKind = Literal["requirement", "acceptance_criterion", "supporting_evidence"]


class RequirementSource(BaseModel):
    """The Jira provenance for one requirement-bearing value."""

    issue_identifier: str
    source_type: Literal["description", "custom_field", "comment", "attachment", "relation"]
    source_id: str
    field_id: str | None = None
    field_name: str | None = None
    url: str | None = None
    location: str | None = None
    author: str = "unknown"
    timestamp: datetime | None = None
    authority: str = "unknown"


class IssueComment(BaseModel):
    id: str | None = None
    author: str | None = None
    body: str | None = None
    created: datetime | None = None
    updated: datetime | None = None
    source: RequirementSource | None = None


class AttachmentAnalysis(BaseModel):
    """OCR/vision/text output. Binary analyzers are injected into the Jira client."""

    status: Literal["complete", "not_configured", "unsupported", "skipped", "error"]
    modality: Literal["ocr", "vision", "text", "metadata", "unknown"] = "unknown"
    summary: str
    analyzer: str | None = None
    generated_at: datetime | None = None


class IssueAttachment(BaseModel):
    id: str
    filename: str
    mime_type: str | None = None
    size: int | None = None
    content_url: str | None = None
    thumbnail_url: str | None = None
    author: str = "unknown"
    created_at: datetime | None = None
    content_sha256: str | None = None
    source: RequirementSource
    analysis: AttachmentAnalysis


class RequirementArtifact(BaseModel):
    artifact_id: str
    source_type: Literal["description", "custom_field", "comment", "attachment"]
    text: str
    value: Any = None
    source: RequirementSource
    kind: RequirementKind = "requirement"


class RelatedIssue(BaseModel):
    id: str | None = None
    identifier: str
    title: str | None = None
    status: str | None = None
    issue_type: str | None = None
    relation: str
    direction: Literal["inward", "outward", "parent", "child"]
    is_dependency: bool = False
    source: RequirementSource
    url: str | None = None
    description: str | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    comments: list[IssueComment] = Field(default_factory=list)
    attachments: list[IssueAttachment] = Field(default_factory=list)
    requirements: list[RequirementArtifact] = Field(default_factory=list)
    hydration_error: str | None = None
    provenance_incomplete_reasons: list[str] = Field(default_factory=list)


class JiraNamedValue(BaseModel):
    id: str | None = None
    name: str
    kind: Literal["component", "affects_version", "fix_version"]
    description: str | None = None
    archived: bool | None = None
    released: bool | None = None
    release_date: str | None = None


class RequirementDecision(BaseModel):
    """A stable, source-linked decision in one explicit requirement category."""

    id: str
    text: str
    kind: RequirementKind = "requirement"
    classification: RequirementClassification
    sources: list[RequirementSource] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: list[str] = Field(default_factory=list)


class RequirementsSnapshot(BaseModel):
    """Canonical, versioned Jira input used for planning and completed-work identity."""

    schema_version: str = "jira-requirements/v2"
    issue_id: str
    issue_identifier: str
    issue_url: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    description: RequirementArtifact | None = None
    custom_fields: list[RequirementArtifact] = Field(default_factory=list)
    comments: list[RequirementArtifact] = Field(default_factory=list)
    attachments: list[IssueAttachment] = Field(default_factory=list)
    parent: RelatedIssue | None = None
    children: list[RelatedIssue] = Field(default_factory=list)
    linked_issues: list[RelatedIssue] = Field(default_factory=list)
    dependencies: list[RelatedIssue] = Field(default_factory=list)
    components: list[JiraNamedValue] = Field(default_factory=list)
    versions: list[JiraNamedValue] = Field(default_factory=list)
    current_requirements: list[RequirementDecision] = Field(default_factory=list)
    superseded_requirements: list[RequirementDecision] = Field(default_factory=list)
    inferred_behavior: list[RequirementDecision] = Field(default_factory=list)
    unresolved_contradictions: list[RequirementDecision] = Field(default_factory=list)
    incomplete_reasons: list[str] = Field(default_factory=list)
    content_hash: str = ""

    @property
    def complete(self) -> bool:
        return not self.incomplete_reasons

    def canonical_content(self) -> dict[str, Any]:
        """Return material content only, normalized for deterministic serialization."""

        payload = self.model_dump(mode="json", exclude={"captured_at", "content_hash"})
        if self.schema_version == "jira-requirements/v1":
            _remove_absent_v2_source_locations(payload)
        for attachment in payload.get("attachments", []):
            analysis = attachment.get("analysis") or {}
            analysis.pop("generated_at", None)
            if analysis.get("status") == "error":
                # HTTP/library exception text is diagnostic, not requirement content.
                analysis["summary"] = "Attachment analysis failed."
        _sort_snapshot_lists(payload)
        return payload

    def calculate_content_hash(self) -> str:
        encoded = json.dumps(
            self.canonical_content(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def with_content_hash(self) -> "RequirementsSnapshot":
        return self.model_copy(update={"content_hash": self.calculate_content_hash()})


class RequirementsSnapshotDiff(BaseModel):
    previous_hash: str | None = None
    current_hash: str
    changed_sections: list[str] = Field(default_factory=list)
    material: bool


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
    creator: str | None = None
    labels: list[str] = Field(default_factory=list)
    url: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    comments: list[IssueComment] = Field(default_factory=list)
    blocked_by: list[IssueBlocker] = Field(default_factory=list)
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    attachments: list[IssueAttachment] = Field(default_factory=list)
    parent: RelatedIssue | None = None
    children: list[RelatedIssue] = Field(default_factory=list)
    linked_issues: list[RelatedIssue] = Field(default_factory=list)
    dependencies: list[RelatedIssue] = Field(default_factory=list)
    components: list[JiraNamedValue] = Field(default_factory=list)
    versions: list[JiraNamedValue] = Field(default_factory=list)
    provenance_incomplete_reasons: list[str] = Field(default_factory=list)
    requirements_snapshot: RequirementsSnapshot | None = None
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
    plan_spec_hash: str | None = None
    plan_approval_id: str | None = None
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


def issue_requirements_fingerprint(issue: Issue) -> str:
    """Hash all material requirements, retaining a fallback for lightweight issues."""

    if issue.requirements_snapshot is not None:
        return issue.requirements_snapshot.calculate_content_hash()
    fallback = {
        "description": issue.description or "",
        "comments": [
            {
                "id": comment.id,
                "author": comment.author,
                "body": comment.body,
                "created": comment.created.isoformat() if comment.created else None,
                "updated": comment.updated.isoformat() if comment.updated else None,
            }
            for comment in sorted(
                issue.comments,
                key=lambda item: (
                    item.created or datetime.min.replace(tzinfo=timezone.utc),
                    item.id or "",
                ),
            )
        ],
        "custom_fields": issue.custom_fields,
        "attachments": [attachment.model_dump(mode="json") for attachment in issue.attachments],
        "parent": issue.parent.model_dump(mode="json") if issue.parent else None,
        "children": [related.model_dump(mode="json") for related in issue.children],
        "linked_issues": [related.model_dump(mode="json") for related in issue.linked_issues],
        "dependencies": [related.model_dump(mode="json") for related in issue.dependencies],
        "components": [value.model_dump(mode="json") for value in issue.components],
        "versions": [value.model_dump(mode="json") for value in issue.versions],
    }
    encoded = json.dumps(fallback, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def issue_description_fingerprint(issue: Issue) -> str:
    """Backward-compatible name; completed-work identity now covers the full bundle."""

    return issue_requirements_fingerprint(issue)


def _remove_absent_v2_source_locations(value: Any) -> None:
    """Keep historical v1 canonical JSON free of the optional v2 source field."""

    if isinstance(value, dict):
        if value.get("location") is None:
            value.pop("location", None)
        for child in value.values():
            _remove_absent_v2_source_locations(child)
    elif isinstance(value, list):
        for child in value:
            _remove_absent_v2_source_locations(child)


def diff_requirements_snapshots(
    previous: RequirementsSnapshot | None,
    current: RequirementsSnapshot,
) -> RequirementsSnapshotDiff:
    current_hash = current.calculate_content_hash()
    if previous is None:
        return RequirementsSnapshotDiff(
            current_hash=current_hash,
            changed_sections=["initial_snapshot"],
            material=True,
        )

    before = previous.canonical_content()
    after = current.canonical_content()
    changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
    previous_hash = previous.calculate_content_hash()
    return RequirementsSnapshotDiff(
        previous_hash=previous_hash,
        current_hash=current_hash,
        changed_sections=changed,
        material=previous_hash != current_hash,
    )


def _sort_snapshot_lists(payload: dict[str, Any]) -> None:
    sort_keys: dict[str, tuple[str, ...]] = {
        "custom_fields": ("artifact_id",),
        "comments": ("artifact_id",),
        "attachments": ("id",),
        "children": ("identifier", "relation", "direction"),
        "linked_issues": ("identifier", "relation", "direction"),
        "dependencies": ("identifier", "relation", "direction"),
        "components": ("name", "id"),
        "versions": ("kind", "name", "id"),
        "current_requirements": ("id",),
        "superseded_requirements": ("id",),
        "inferred_behavior": ("id",),
        "unresolved_contradictions": ("id",),
    }
    for key, fields in sort_keys.items():
        values = payload.get(key)
        if isinstance(values, list):
            values.sort(key=lambda value: tuple(str((value or {}).get(field) or "") for field in fields))

    def sort_related(related: dict[str, Any]) -> None:
        related["requirements"] = sorted(
            related.get("requirements") or [],
            key=lambda artifact: str(artifact.get("artifact_id") or ""),
        )
        related["comments"] = sorted(
            related.get("comments") or [],
            key=lambda comment: (
                str(comment.get("created") or comment.get("updated") or ""),
                str(comment.get("id") or ""),
            ),
        )
        related["attachments"] = sorted(
            related.get("attachments") or [],
            key=lambda attachment: str(attachment.get("id") or ""),
        )
        for attachment in related["attachments"]:
            analysis = attachment.get("analysis") or {}
            analysis.pop("generated_at", None)
            if analysis.get("status") == "error":
                analysis["summary"] = "Attachment analysis failed."
        related["provenance_incomplete_reasons"] = sorted(
            related.get("provenance_incomplete_reasons") or []
        )

    for section in ("children", "linked_issues", "dependencies"):
        for related in payload.get(section, []):
            sort_related(related)
    parent = payload.get("parent")
    if parent:
        sort_related(parent)
    payload["incomplete_reasons"] = sorted(payload.get("incomplete_reasons") or [])
    for section in (
        "current_requirements",
        "superseded_requirements",
        "inferred_behavior",
        "unresolved_contradictions",
    ):
        for decision in payload.get(section, []):
            decision["sources"] = sorted(
                decision.get("sources") or [],
                key=lambda source: (
                    str(source.get("issue_identifier") or ""),
                    str(source.get("source_type") or ""),
                    str(source.get("source_id") or ""),
                    str(source.get("location") or ""),
                ),
            )
            decision["supersedes"] = sorted(decision.get("supersedes") or [])
            decision["superseded_by"] = sorted(decision.get("superseded_by") or [])
