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
    planning_eligible: bool = True


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

    schema_version: str = "jira-requirements/v4"
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
    context_warnings: list[str] = Field(default_factory=list)
    content_hash: str = ""

    @property
    def complete(self) -> bool:
        return not self.incomplete_reasons

    def canonical_content(self) -> dict[str, Any]:
        """Return material content only, normalized for deterministic serialization."""

        payload = self.model_dump(mode="json", exclude={"captured_at", "content_hash"})
        if self.schema_version == "jira-requirements/v1":
            _remove_absent_v2_source_locations(payload)
        if self.schema_version in {
            "jira-requirements/v1",
            "jira-requirements/v2",
            "jira-requirements/v3",
        }:
            # Preserve hashes for stored snapshots created before contextual
            # artifacts and warnings were explicitly represented.
            payload.pop("context_warnings", None)
            _remove_absent_v3_artifact_eligibility(payload)
        elif self.schema_version == "jira-requirements/v4":
            # Context remains in the serialized snapshot for inspection but
            # does not invalidate an approved plan. Only root Jira product
            # evidence and its classified decisions are material in v4.
            for key in (
                "attachments",
                "parent",
                "children",
                "linked_issues",
                "dependencies",
                "components",
                "versions",
                "context_warnings",
            ):
                payload.pop(key, None)
            payload["custom_fields"] = [
                artifact
                for artifact in payload.get("custom_fields", [])
                if artifact.get("planning_eligible") is True
            ]
            # Jira base URLs, field display names, and rendered unit locations
            # are derived presentation metadata. Stable issue/source IDs retain
            # provenance without letting those display-only values invalidate an
            # approval or completed-work identity.
            # ``canonical_content`` is also persisted as a round-trippable
            # RequirementsSnapshot document, so retain this required field with
            # a deterministic non-authoritative value.
            payload["issue_url"] = ""
            _remove_v4_source_presentation_metadata(payload)
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
    context_warnings: list[str] = Field(default_factory=list)
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
    automation_plan_hash: str | None = None
    automation_development_diff_hash: str | None = None
    automation_repository_diff_hash: str | None = None
    automation_result_hash: str | None = None
    plan_approval_id: str | None = None
    finished_at: datetime | None = None
    final_message: str | None = None
    error: str | None = None
    blocked_phase: str | None = None
    branch_name: str | None = None
    verification_status: str | None = None
    verification_output_path: str | None = None
    verification_workspace_diff_hash: str | None = None
    verification_evidence_sha256: str | None = None


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
    }
    encoded = json.dumps(fallback, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def issue_description_fingerprint(issue: Issue) -> str:
    """Backward-compatible name; completed-work identity now covers the full bundle."""

    return issue_requirements_fingerprint(issue)


def requirements_planning_authority_projection(
    snapshot: RequirementsSnapshot,
) -> dict[str, Any]:
    """Project any snapshot version onto the v4 root planning boundary.

    The projection is intentionally narrower than ``canonical_content``.  It is
    used only to bridge a frozen pre-v4 approval across the schema migration,
    so attachment and relationship context must disappear while every material
    root Description, configured Acceptance Criteria, comment, decision, and
    source-provenance change remains visible.
    """

    root_issue = snapshot.issue_identifier

    def source_is_root(source: RequirementSource, source_type: str) -> bool:
        return (
            source.issue_identifier == root_issue
            and source.source_type == source_type
        )

    description = (
        snapshot.description
        if snapshot.description is not None
        and snapshot.description.planning_eligible
        and source_is_root(snapshot.description.source, "description")
        else None
    )
    acceptance_criteria = [
        artifact
        for artifact in snapshot.custom_fields
        if artifact.planning_eligible
        and artifact.kind == "acceptance_criterion"
        and artifact.source_type == "custom_field"
        and source_is_root(artifact.source, "custom_field")
    ]
    comments = [
        artifact
        for artifact in snapshot.comments
        if artifact.planning_eligible
        and artifact.source_type == "comment"
        and source_is_root(artifact.source, "comment")
    ]
    artifacts = (
        ([description] if description is not None else [])
        + acceptance_criteria
        + comments
    )
    source_bases = {
        (artifact.source.source_type, artifact.source.source_id)
        for artifact in artifacts
    }

    def source_is_authoritative(source: RequirementSource) -> bool:
        return source.issue_identifier == root_issue and any(
            source.source_type == source_type
            and (
                source.source_id == source_id
                or source.source_id.startswith(f"{source_id}#unit:")
            )
            for source_type, source_id in source_bases
        )

    def source_payload(source: RequirementSource) -> dict[str, Any]:
        # URL, display name, and location are derived presentation metadata.
        # The stable source identity and its provenance remain material.
        return {
            "issue_identifier": source.issue_identifier,
            "source_type": source.source_type,
            "source_id": source.source_id,
            "field_id": source.field_id,
            "author": source.author,
            "timestamp": (
                source.timestamp.isoformat() if source.timestamp else None
            ),
            "authority": source.authority,
        }

    def artifact_payload(artifact: RequirementArtifact) -> dict[str, Any]:
        return {
            "artifact_id": artifact.artifact_id,
            "source_type": artifact.source_type,
            "text": artifact.text,
            "value": artifact.value,
            "source": source_payload(artifact.source),
            "kind": artifact.kind,
        }

    def decision_payload(decision: RequirementDecision) -> dict[str, Any] | None:
        sources = [
            source_payload(source)
            for source in decision.sources
            if source_is_authoritative(source)
        ]
        if not sources:
            return None
        sources.sort(
            key=lambda source: (
                str(source["issue_identifier"]),
                str(source["source_type"]),
                str(source["source_id"]),
            )
        )
        return {
            "id": decision.id,
            "text": decision.text,
            "kind": decision.kind,
            "classification": decision.classification,
            "sources": sources,
            "supersedes": sorted(decision.supersedes),
            "superseded_by": sorted(decision.superseded_by),
        }

    def decisions_payload(
        decisions: list[RequirementDecision],
    ) -> list[dict[str, Any]]:
        projected = [decision_payload(decision) for decision in decisions]
        return sorted(
            (decision for decision in projected if decision is not None),
            key=lambda decision: str(decision["id"]),
        )

    return {
        "schema_version": "jira-planning-authority/v4",
        "issue_id": snapshot.issue_id,
        "issue_identifier": root_issue,
        "description": (
            artifact_payload(description) if description is not None else None
        ),
        "acceptance_criteria": sorted(
            (artifact_payload(artifact) for artifact in acceptance_criteria),
            key=lambda artifact: str(artifact["artifact_id"]),
        ),
        "comments": sorted(
            (artifact_payload(artifact) for artifact in comments),
            key=lambda artifact: str(artifact["artifact_id"]),
        ),
        "current_requirements": decisions_payload(
            snapshot.current_requirements
        ),
        "superseded_requirements": decisions_payload(
            snapshot.superseded_requirements
        ),
        "inferred_behavior": decisions_payload(snapshot.inferred_behavior),
        "unresolved_contradictions": decisions_payload(
            snapshot.unresolved_contradictions
        ),
    }


def requirements_planning_authority_equivalent(
    previous: RequirementsSnapshot,
    current: RequirementsSnapshot,
) -> bool:
    """Return true only when both snapshots have identical v4 planning meaning."""

    return (
        requirements_planning_authority_projection(previous)
        == requirements_planning_authority_projection(current)
    )


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


def _remove_absent_v3_artifact_eligibility(value: Any) -> None:
    """Keep pre-v4 canonical JSON free of fields introduced in v4."""

    if isinstance(value, dict):
        value.pop("planning_eligible", None)
        for child in value.values():
            _remove_absent_v3_artifact_eligibility(child)
    elif isinstance(value, list):
        for child in value:
            _remove_absent_v3_artifact_eligibility(child)


def _remove_v4_source_presentation_metadata(value: Any) -> None:
    """Remove derived display fields from v4 requirement-source payloads."""

    if isinstance(value, dict):
        if {"issue_identifier", "source_type", "source_id"}.issubset(value):
            value.pop("field_name", None)
            value.pop("url", None)
            value.pop("location", None)
        for child in value.values():
            _remove_v4_source_presentation_metadata(child)
    elif isinstance(value, list):
        for child in value:
            _remove_v4_source_presentation_metadata(child)


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
    if "context_warnings" in payload:
        payload["context_warnings"] = sorted(payload.get("context_warnings") or [])
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
