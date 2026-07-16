from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from .config import JiraRequirementsConfig, TrackerConfig, resolve_configured_secret
from .models import (
    AttachmentAnalysis,
    Issue,
    IssueAttachment,
    IssueBlocker,
    IssueComment,
    JiraNamedValue,
    RelatedIssue,
    RequirementArtifact,
    RequirementClassification,
    RequirementDecision,
    RequirementSource,
    RequirementsSnapshot,
)


BASE_ISSUE_FIELDS = (
    "summary",
    "description",
    "status",
    "priority",
    "issuetype",
    "assignee",
    "reporter",
    "creator",
    "labels",
    "created",
    "updated",
    "comment",
    "attachment",
    "parent",
    "subtasks",
    "issuelinks",
    "components",
    "versions",
    "fixVersions",
)

ROOT_REQUIREMENT_PRESENCE_FIELDS = BASE_ISSUE_FIELDS

RELATED_ISSUE_FIELDS = (
    "summary",
    "description",
    "status",
    "issuetype",
    "creator",
    "created",
    "attachment",
)

RELATED_REQUIREMENT_PRESENCE_FIELDS = RELATED_ISSUE_FIELDS

_SAFE_JIRA_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-\d+")


class JiraError(Exception):
    """Base Jira adapter error."""


class JiraAuthError(JiraError):
    """Raised when Jira credentials cannot be resolved."""


class AttachmentAnalyzer(Protocol):
    """Adapter point for an OCR/vision service or an on-device implementation."""

    async def analyze(self, attachment: IssueAttachment, content: bytes) -> AttachmentAnalysis: ...


class BasicAttachmentAnalyzer:
    """Dependency-free analysis for text; binary evidence remains explicitly incomplete."""

    _TEXT_MIME_TYPES = {
        "application/csv",
        "application/json",
        "application/sql",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    }

    def __init__(self, max_summary_characters: int = 12_000) -> None:
        self.max_summary_characters = max_summary_characters

    async def analyze(self, attachment: IssueAttachment, content: bytes) -> AttachmentAnalysis:
        mime_type = (attachment.mime_type or "").lower()
        if mime_type.startswith("text/") or mime_type in self._TEXT_MIME_TYPES:
            text = content.decode("utf-8", errors="replace")
            if len(text) > self.max_summary_characters:
                text = f"{text[: self.max_summary_characters]}\n[truncated]"
            return AttachmentAnalysis(
                status="complete",
                modality="text",
                summary=text,
                analyzer="symphony-basic-text/v1",
            )

        modality = "vision" if mime_type.startswith("image/") else "ocr" if mime_type == "application/pdf" else "unknown"
        return AttachmentAnalysis(
            status="not_configured",
            modality=modality,
            summary=f"No OCR/vision analyzer is configured for {mime_type or 'this attachment type'}.",
            analyzer="symphony-basic-text/v1",
        )


class RequirementClassifier(Protocol):
    def classify(self, artifact: RequirementArtifact) -> RequirementClassification: ...

    def supersedes(self, artifact: RequirementArtifact) -> list[str]: ...


class MarkerRequirementClassifier:
    """Conservative deterministic annotations for common Jira requirement prose."""

    _CLASSIFICATION_MARKERS: tuple[tuple[re.Pattern[str], RequirementClassification], ...] = (
        (
            re.compile(r"\[(?:classification\s*:\s*)?(?:unresolved[_ -]?contradiction|contradiction)\]", re.I),
            "unresolved_contradiction",
        ),
        (
            re.compile(r"\[(?:classification\s*:\s*)?(?:inferred|assumption)\]", re.I),
            "inferred",
        ),
        (
            re.compile(r"\[(?:classification\s*:\s*)?(?:superseded|obsolete)\]", re.I),
            "superseded",
        ),
        (re.compile(r"\[classification\s*:\s*current\]", re.I), "current"),
    )
    _SUPERSEDES = re.compile(r"\[supersedes\s*:\s*([^\]]+)\]", re.I)
    _PLAIN_CLASSIFICATIONS: tuple[
        tuple[re.Pattern[str], RequirementClassification], ...
    ] = (
        (
            re.compile(
                r"(?:^|\n)\s*(?:unresolved\s+)?contradiction\s*:"
                r"|\b(?:there\s+is|this\s+(?:is|remains)|we\s+(?:found|have))\s+"
                r"(?:an?\s+)?unresolved\s+contradiction\b"
                r"|\b(?:this|that|the)(?:\s+"
                r"(?:requirement|decision|design|behavior|mockup))?\s+"
                r"(?:directly\s+)?(?:contradicts|conflicts\s+with)\s+"
                r"(?:the\s+)?(?:current|previous|prior)\s+"
                r"(?:requirement|decision|design|behavior|mockup)\b"
                r"|\b(?:requirements|decisions|designs|mockups)\s+"
                r"(?:directly\s+)?(?:contradict|conflict\s+with)\s+"
                r"(?:one\s+another|each\s+other)\b",
                re.I,
            ),
            "unresolved_contradiction",
        ),
        (
            re.compile(
                r"(?:^|\n)\s*(?:inferred(?:\s+behavior)?|assumption)\s*:"
                r"|\b(?:this|the)(?:\s+"
                r"(?:behavior|requirement|decision|design))?\s+"
                r"(?:is|was)\s+(?:explicitly\s+)?inferred\b"
                r"|\bthis\s+is\s+an?\s+inference\b"
                r"|\bwe\s+infer\s+that\b",
                re.I,
            ),
            "inferred",
        ),
        (
            re.compile(
                r"(?:^|\n)\s*(?:superseded|obsolete)"
                r"(?:\s+(?:requirement|decision|design|behavior))?\s*:"
                r"|(?:^|\n)\s*(?:superseded\s+by|obsolete(?:\s*[.!]|\s*$))"
                r"|\b(?:this|the)(?:\s+"
                r"(?:requirement|decision|design|behavior))?\s+"
                r"(?:is|was|has\s+been)\s+(?:explicitly\s+)?"
                r"(?:superseded|obsolete)\b"
                r"|\b(?:this|the)\s+"
                r"(?:requirement|decision|design|behavior)\s+no\s+longer\s+applies\b",
                re.I,
            ),
            "superseded",
        ),
    )
    _SUPERSEDES_PREVIOUS = re.compile(
        r"\b(?:replaces|supersedes|overrides)\s+"
        r"(?:the\s+)?(?:previous|prior)\s+"
        r"(?:requirements?|decisions?|designs?)\b",
        re.I,
    )

    def classify(self, artifact: RequirementArtifact) -> RequirementClassification:
        if (
            not self._SUPERSEDES.search(artifact.text)
            and self._has_ambiguous_supersession(artifact.text)
        ):
            return "unresolved_contradiction"
        for pattern, classification in self._CLASSIFICATION_MARKERS:
            if pattern.search(artifact.text):
                return classification
        for pattern, classification in self._PLAIN_CLASSIFICATIONS:
            if pattern.search(artifact.text):
                return classification
        return "current"

    def supersedes(self, artifact: RequirementArtifact) -> list[str]:
        references: list[str] = []
        for match in self._SUPERSEDES.finditer(artifact.text):
            references.extend(part.strip() for part in match.group(1).split(",") if part.strip())
        return list(dict.fromkeys(references))

    def _has_ambiguous_supersession(self, text: str) -> bool:
        for match in self._SUPERSEDES_PREVIOUS.finditer(text):
            prefix = text[max(0, match.start() - 48) : match.start()]
            if re.search(
                r"(?:\b(?:not|never)\s+|\bno\s+(?:requirement|decision|design)\s+)$",
                prefix,
                flags=re.I,
            ):
                continue
            return True
        return False


class JiraClient:
    def __init__(
        self,
        config: TrackerConfig,
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        attachment_analyzer: AttachmentAnalyzer | None = None,
        requirement_classifier: RequirementClassifier | None = None,
    ) -> None:
        self.config = config
        self.environ = environ or os.environ
        self.attachment_analyzer = attachment_analyzer or BasicAttachmentAnalyzer()
        self.requirement_classifier = requirement_classifier or MarkerRequirementClassifier()
        self._attachment_download_semaphore = asyncio.Semaphore(
            config.requirements.attachment_download_max_concurrency
        )
        headers, auth = self._auth()
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            auth=auth,
            transport=transport,
            timeout=timeout,
        )

    async def __aenter__(self) -> "JiraClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def search_issues(self, jql: str, limit: int) -> list[Issue]:
        response = await self._client.get(
            "/rest/api/2/search",
            params={
                "jql": jql,
                "maxResults": limit,
                "fields": ",".join(BASE_ISSUE_FIELDS),
            },
        )
        response.raise_for_status()
        payload = response.json()
        summaries = [
            normalize_issue(
                issue,
                self.config.base_url,
                requirements_config=self.config.requirements,
                requirement_classifier=self.requirement_classifier,
            )
            for issue in payload.get("issues", [])
        ]
        if not self.config.requirements.hydrate_search_results:
            return summaries

        # Completed-work identity must never compare a partial search result with a
        # previously approved full snapshot. Hydrate in bounded chunks so a large
        # poll cannot schedule every issue request at once.
        hydrated: list[Issue] = []
        chunk_size = self.config.requirements.related_issue_hydration_max_concurrency
        for offset in range(0, len(summaries), chunk_size):
            chunk = summaries[offset : offset + chunk_size]
            hydrated.extend(
                await asyncio.gather(
                    *(
                        self.get_issue(issue.identifier, include_comments=True)
                        for issue in chunk
                    )
                )
            )
        return hydrated

    async def get_issue(self, key: str, include_comments: bool = True) -> Issue:
        fields = list(BASE_ISSUE_FIELDS)
        if not include_comments:
            fields.remove("comment")
            fields.remove("attachment")
        configured_fields = list(
            dict.fromkeys(
                self.config.requirements.custom_fields
                + self.config.requirements.acceptance_criteria_fields
            )
        )
        fields.extend(field for field in configured_fields if field not in fields)
        params: dict[str, str] = {"fields": ",".join(fields), "expand": "names,changelog"}
        response = await self._client.get(f"/rest/api/2/issue/{key}", params=params)
        response.raise_for_status()
        payload = response.json()
        payload, provenance_incomplete_reasons = await self._complete_changelog(
            key, payload
        )

        comments: list[IssueComment] | None = None
        if include_comments:
            comments, comment_incomplete_reasons = await self._get_all_comments(key)
            provenance_incomplete_reasons = list(
                dict.fromkeys(
                    provenance_incomplete_reasons + comment_incomplete_reasons
                )
            )

        issue = normalize_issue(
            payload,
            self.config.base_url,
            requirements_config=self.config.requirements,
            comments=comments,
            requirement_classifier=self.requirement_classifier,
            provenance_incomplete_reasons=provenance_incomplete_reasons,
        )
        if include_comments:
            issue.attachments = await self._analyze_attachments(issue.attachments)
            if self._child_discovery_enabled(issue):
                configured_children, child_incomplete_reasons = (
                    await self._get_configured_children(issue)
                )
                issue.children = _deduplicate_related_issues(
                    issue.children + configured_children
                )
                issue.provenance_incomplete_reasons = list(
                    dict.fromkeys(
                        issue.provenance_incomplete_reasons + child_incomplete_reasons
                    )
                )
            await self._hydrate_related_issue_context(issue)
            issue.requirements_snapshot = build_requirements_snapshot(
                issue,
                payload,
                self.config.requirements,
                self.requirement_classifier,
            )
        else:
            issue.requirements_snapshot = None
        return issue

    async def _get_all_comments(
        self,
        key: str,
    ) -> tuple[list[IssueComment], list[str]]:
        comments: list[IssueComment] = []
        start_at = 0
        page_size = self.config.requirements.comment_page_size
        declared_total: int | None = None
        fetch_failed = False
        pagination_error: str | None = None
        requested_offsets: set[int] = set()
        while True:
            if start_at in requested_offsets:
                fetch_failed = True
                break
            requested_offsets.add(start_at)
            try:
                response = await self._client.get(
                    f"/rest/api/2/issue/{key}/comment",
                    params={"startAt": start_at, "maxResults": page_size},
                )
                response.raise_for_status()
                payload = response.json()
            except Exception:
                fetch_failed = True
                break
            if not isinstance(payload, Mapping):
                pagination_error = "returned a non-object page"
                break
            start_error = _pagination_start_error(payload, start_at)
            if start_error is not None:
                pagination_error = (
                    f"{start_error} for requested offset {start_at}"
                )
                break

            raw_page = payload.get("comments")
            if raw_page is None:
                raw_page = []
            if not isinstance(raw_page, list):
                fetch_failed = True
                break
            page = [comment for comment in raw_page if isinstance(comment, Mapping)]
            if len(page) != len(raw_page):
                fetch_failed = True
            comments.extend(
                normalize_comment(
                    comment,
                    issue_identifier=key,
                    issue_url=_issue_url(self.config.base_url, key),
                    authority=self.config.requirements.comment_authority,
                    authority_by_author=self.config.requirements.comment_authority_by_author,
                )
                for comment in page
            )
            received = len(raw_page)
            total = _optional_int(payload.get("total"))
            if total is not None:
                declared_total = max(declared_total or 0, total)
            next_start = start_at + received
            if fetch_failed or received == 0 or payload.get("isLast") is True:
                break
            if total is not None and next_start >= total:
                break
            if total is None and received < page_size:
                break
            if next_start <= start_at:
                fetch_failed = True
                break
            start_at = next_start

        deduplicated: dict[str, IssueComment] = {}
        anonymous: list[IssueComment] = []
        for comment in comments:
            if comment.id is None:
                anonymous.append(comment)
            else:
                deduplicated[comment.id] = comment
        all_comments = list(deduplicated.values()) + anonymous
        filtered = [
            comment
            for comment in all_comments
            if not is_symphony_status_comment(
                comment.body,
                self.config.requirements.symphony_comment_patterns,
            )
        ]
        incomplete_reasons: list[str] = []
        available_count = len(all_comments)
        if declared_total is not None and available_count < declared_total:
            incomplete_reasons.append(
                f"Jira comments for {key} declared {declared_total} comments but only "
                f"{available_count} were available; comment requirements are incomplete."
            )
        if pagination_error is not None:
            incomplete_reasons.append(
                f"Jira comments for {key} {pagination_error}; "
                "comment requirements are incomplete."
            )
        elif fetch_failed:
            incomplete_reasons.append(
                f"Jira comments for {key} could not be completely fetched; "
                "comment requirements are incomplete."
            )
        return sorted(filtered, key=_comment_sort_key), incomplete_reasons

    async def _complete_changelog(
        self,
        key: str,
        payload: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], list[str]]:
        changelog = payload.get("changelog")
        if not isinstance(changelog, Mapping):
            return payload, _declared_changelog_incomplete_reasons(payload, key)
        declared_total = _nonnegative_int(changelog.get("total"))
        if declared_total is None:
            return payload, _declared_changelog_incomplete_reasons(payload, key)
        raw_histories = changelog.get("histories")
        if not isinstance(raw_histories, list):
            return payload, _declared_changelog_incomplete_reasons(payload, key)
        embedded = [
            history
            for history in raw_histories
            if isinstance(history, Mapping)
        ]
        if len(embedded) != len(raw_histories):
            return payload, _declared_changelog_incomplete_reasons(payload, key)
        embedded_start = _strict_nonnegative_int(changelog.get("startAt", 0))
        if embedded_start is None:
            return payload, _declared_changelog_incomplete_reasons(payload, key)
        if embedded_start == 0 and declared_total == len(embedded):
            return payload, []

        histories = list(embedded) if embedded_start == 0 else []
        seen = {_mapping_digest(history) for history in histories}
        start_at = len(embedded) if embedded_start == 0 else 0
        page_size = 100
        pagination_reasons: list[str] = []
        while start_at < declared_total:
            try:
                response = await self._client.get(
                    f"/rest/api/2/issue/{key}/changelog",
                    params={"startAt": start_at, "maxResults": page_size},
                )
                response.raise_for_status()
                page_payload = response.json()
            except Exception:
                break
            if not isinstance(page_payload, Mapping):
                pagination_reasons.append(
                    f"Jira changelog for {key} returned a non-object page; "
                    "field provenance is incomplete."
                )
                break
            start_error = _pagination_start_error(page_payload, start_at)
            if start_error is not None:
                pagination_reasons.append(
                    f"Jira changelog for {key} {start_error} for requested offset "
                    f"{start_at}; field provenance is incomplete."
                )
                break

            raw_page = page_payload.get("values")
            if raw_page is None:
                raw_page = page_payload.get("histories")
            if not isinstance(raw_page, list):
                break
            page = [history for history in raw_page if isinstance(history, Mapping)]
            page_total = _optional_int(page_payload.get("total"))
            if page_total is not None:
                declared_total = max(declared_total, page_total)
            for history in page:
                identity = _mapping_digest(history)
                if identity not in seen:
                    histories.append(history)
                    seen.add(identity)
            next_start = start_at + len(raw_page)
            if not raw_page or next_start <= start_at:
                break
            start_at = next_start

        merged_payload = dict(payload)
        merged_changelog = dict(changelog)
        merged_changelog.update(
            {
                "startAt": 0,
                "total": declared_total,
                "histories": histories,
            }
        )
        merged_payload["changelog"] = merged_changelog
        reasons = _declared_changelog_incomplete_reasons(merged_payload, key)
        reasons.extend(pagination_reasons)
        return merged_payload, list(dict.fromkeys(reasons))

    async def _analyze_attachments(
        self,
        attachments: list[IssueAttachment],
    ) -> list[IssueAttachment]:
        analyzed: list[IssueAttachment] = []
        limit = self.config.requirements.attachment_download_max_concurrency
        for offset in range(0, len(attachments), limit):
            chunk = attachments[offset : offset + limit]
            analyzed.extend(
                await asyncio.gather(*(self._analyze_attachment(item) for item in chunk))
            )
        return analyzed

    async def _analyze_attachment(self, attachment: IssueAttachment) -> IssueAttachment:
        config = self.config.requirements
        if not config.download_attachments:
            return attachment.model_copy(
                update={
                    "analysis": AttachmentAnalysis(
                        status="skipped",
                        modality="metadata",
                        summary="Attachment downloading is disabled by tracker.requirements.download_attachments.",
                    )
                }
            )
        if attachment.size is not None and attachment.size > config.max_attachment_bytes:
            return attachment.model_copy(
                update={
                    "analysis": AttachmentAnalysis(
                        status="skipped",
                        modality="metadata",
                        summary=(
                            f"Attachment is {attachment.size} bytes; the configured limit is "
                            f"{config.max_attachment_bytes} bytes."
                        ),
                    )
                }
            )
        if not attachment.content_url:
            return attachment.model_copy(
                update={
                    "analysis": AttachmentAnalysis(
                        status="error",
                        modality="metadata",
                        summary="Jira did not provide an attachment content URL.",
                    )
                }
            )

        async with self._attachment_download_semaphore:
            return await self._download_and_analyze_attachment(attachment)

    async def _download_and_analyze_attachment(
        self,
        attachment: IssueAttachment,
    ) -> IssueAttachment:
        config = self.config.requirements
        try:
            content_url = _same_origin_attachment_url(
                self.config.base_url,
                attachment.content_url or "",
            )
            chunks: list[bytes] = []
            received = 0
            async with self._client.stream("GET", content_url) as response:
                response.raise_for_status()
                content_length = _nonnegative_int(response.headers.get("Content-Length"))
                if content_length is not None and content_length > config.max_attachment_bytes:
                    return _oversized_attachment(
                        attachment,
                        config.max_attachment_bytes,
                        declared_size=content_length,
                    )
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > config.max_attachment_bytes:
                        return _oversized_attachment(
                            attachment,
                            config.max_attachment_bytes,
                        )
                    chunks.append(chunk)
            content = b"".join(chunks)
            content_hash = hashlib.sha256(content).hexdigest()
            analysis = await self.attachment_analyzer.analyze(attachment, content)
            return attachment.model_copy(
                update={"content_sha256": content_hash, "analysis": analysis}
            )
        except Exception as exc:
            return attachment.model_copy(
                update={
                    "analysis": AttachmentAnalysis(
                        status="error",
                        modality="unknown",
                        summary=f"Attachment analysis failed: {type(exc).__name__}: {exc}",
                    )
                }
            )

    def _child_discovery_enabled(self, issue: Issue) -> bool:
        template = self.config.requirements.child_issue_jql
        if template is not None:
            return bool(template.strip())
        return (
            self.config.requirements.discover_epic_children
            and (issue.issue_type or "").strip().casefold() == "epic"
        )

    async def _get_configured_children(
        self,
        issue: Issue,
    ) -> tuple[list[RelatedIssue], list[str]]:
        template = self.config.requirements.child_issue_jql
        if not _SAFE_JIRA_KEY.fullmatch(issue.identifier):
            return [], [
                _canonical_child_discovery_error(
                    issue.identifier,
                    "the root issue key is not a safe Jira key",
                )
            ]
        if template is None:
            jql = f"parent = {_jira_jql_literal(issue.identifier)}"
        else:
            try:
                jql = template.format(issue_key=issue.identifier)
            except (IndexError, KeyError, ValueError) as exc:
                return [], [
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"the configured JQL template is invalid ({type(exc).__name__})",
                    )
                ]

        children: dict[str, RelatedIssue] = {}
        incomplete_reasons: list[str] = []
        start_at = 0
        page_size = 100
        declared_total: int | None = None
        requested_offsets: set[int] = set()
        pages = 0
        while True:
            if pages >= self.config.requirements.child_issue_max_pages:
                incomplete_reasons.append(
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"the {self.config.requirements.child_issue_max_pages}-page limit was reached",
                    )
                )
                break
            if start_at in requested_offsets:
                incomplete_reasons.append(
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"pagination repeated offset {start_at}",
                    )
                )
                break
            requested_offsets.add(start_at)
            pages += 1
            try:
                response = await self._client.get(
                    "/rest/api/2/search",
                    params={
                        "jql": jql,
                        "startAt": start_at,
                        "maxResults": page_size,
                        "fields": "summary,status,issuetype",
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                incomplete_reasons.append(
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"page {pages} failed ({type(exc).__name__})",
                    )
                )
                break
            if not isinstance(payload, Mapping):
                incomplete_reasons.append(
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"page {pages} was not an object",
                    )
                )
                break
            start_error = _pagination_start_error(payload, start_at)
            if start_error is not None:
                incomplete_reasons.append(
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"{start_error} for requested offset {start_at}",
                    )
                )
                break
            raw_page = payload.get("issues")
            if not isinstance(raw_page, list):
                incomplete_reasons.append(
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"page {pages} did not contain an issues list",
                    )
                )
                break
            for child in raw_page:
                if not isinstance(child, Mapping):
                    incomplete_reasons.append(
                        _canonical_child_discovery_error(
                            issue.identifier,
                            f"page {pages} contained a malformed child",
                        )
                    )
                    continue
                identity = str(child.get("key") or child.get("id") or "")
                if not identity:
                    incomplete_reasons.append(
                        _canonical_child_discovery_error(
                            issue.identifier,
                            f"page {pages} contained a child without a key or id",
                        )
                    )
                    continue
                children[identity] = normalize_related_issue(
                    child,
                    issue.identifier,
                    relation="child",
                    direction="child",
                    source_id=f"child-query:{identity}",
                    authority=self.config.requirements.relation_authority,
                    author=issue.creator or "unknown",
                    timestamp=None,
                    url=issue.url,
                )

            received = len(raw_page)
            total = _nonnegative_int(payload.get("total"))
            if total is None:
                incomplete_reasons.append(
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"page {pages} did not provide a valid non-negative total",
                    )
                )
            else:
                declared_total = max(declared_total or 0, total)
            next_start = start_at + received
            is_last = payload.get("isLast")
            if is_last is not None and not isinstance(is_last, bool):
                incomplete_reasons.append(
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"page {pages} provided an invalid isLast value",
                    )
                )
            if is_last is True:
                if declared_total is not None and next_start < declared_total:
                    incomplete_reasons.append(
                        _canonical_child_discovery_error(
                            issue.identifier,
                            "isLast was true before the declared total was reached",
                        )
                    )
                break
            if received == 0:
                if declared_total is not None and start_at < declared_total:
                    incomplete_reasons.append(
                        _canonical_child_discovery_error(
                            issue.identifier,
                            "pagination ended before the declared total was reached",
                        )
                    )
                break
            if declared_total is not None and next_start >= declared_total:
                if is_last is False:
                    incomplete_reasons.append(
                        _canonical_child_discovery_error(
                            issue.identifier,
                            "isLast was false after the declared total was reached",
                        )
                    )
                break
            if declared_total is None and received < page_size:
                break
            if next_start <= start_at:
                incomplete_reasons.append(
                    _canonical_child_discovery_error(
                        issue.identifier,
                        f"pagination did not advance beyond offset {start_at}",
                    )
                )
                break
            start_at = next_start

        if declared_total is not None and len(children) != declared_total:
            incomplete_reasons.append(
                _canonical_child_discovery_error(
                    issue.identifier,
                    f"Jira declared {declared_total} children but {len(children)} unique children were available",
                )
            )
        return (
            sorted(children.values(), key=lambda child: child.identifier),
            list(dict.fromkeys(incomplete_reasons)),
        )

    async def _hydrate_related_issue_context(self, issue: Issue) -> None:
        """Fetch one hop of requirement artifacts without traversing its relations."""

        related = (
            ([issue.parent] if issue.parent is not None else [])
            + issue.children
            + issue.linked_issues
            + issue.dependencies
        )
        identifiers = sorted(
            {
                item.identifier
                for item in related
                if item.identifier and item.identifier != issue.identifier
            }
        )
        if not identifiers:
            return
        configured_fields = list(
            dict.fromkeys(
                self.config.requirements.custom_fields
                + self.config.requirements.acceptance_criteria_fields
            )
        )
        fields = list(RELATED_ISSUE_FIELDS) + configured_fields
        hydration_limit = self.config.requirements.related_issue_hydration_max_concurrency
        semaphore = asyncio.Semaphore(hydration_limit)

        async def fetch(
            identifier: str,
        ) -> tuple[
            str,
            Mapping[str, Any] | None,
            list[IssueComment],
            list[IssueAttachment],
            list[str],
            str | None,
        ]:
            async with semaphore:
                try:
                    response = await self._client.get(
                        f"/rest/api/2/issue/{identifier}",
                        params={
                            "fields": ",".join(fields),
                            "expand": "names,changelog",
                        },
                    )
                    response.raise_for_status()
                    payload, provenance_reasons = await self._complete_changelog(
                        identifier,
                        response.json(),
                    )
                    issue_url = _issue_url(self.config.base_url, identifier)
                    raw_attachments = normalize_attachments(
                        (payload.get("fields") or {}).get("attachment") or [],
                        issue_identifier=identifier,
                        issue_url=issue_url,
                        authority=self.config.requirements.attachment_authority,
                    )
                    comment_result, attachments = await asyncio.gather(
                        self._get_all_comments(identifier),
                        self._analyze_attachments(raw_attachments),
                    )
                    comments, comment_reasons = comment_result
                    provenance_reasons = list(
                        dict.fromkeys(
                            provenance_reasons + comment_reasons
                        )
                    )
                    return (
                        identifier,
                        payload,
                        comments,
                        attachments,
                        provenance_reasons,
                        None,
                    )
                except Exception as exc:
                    return (
                        identifier,
                        None,
                        [],
                        [],
                        [],
                        _canonical_related_hydration_error(identifier, exc),
                    )

        results = []
        for offset in range(0, len(identifiers), hydration_limit):
            chunk = identifiers[offset : offset + hydration_limit]
            results.extend(
                await asyncio.gather(*(fetch(identifier) for identifier in chunk))
            )
        contexts = {
            identifier: (payload, comments, attachments, provenance_reasons, error)
            for (
                identifier,
                payload,
                comments,
                attachments,
                provenance_reasons,
                error,
            ) in results
        }

        def hydrate(item: RelatedIssue) -> RelatedIssue:
            context = contexts.get(item.identifier)
            if context is None:
                return item
            payload, comments, attachments, provenance_reasons, error = context
            if error is not None or payload is None:
                return item.model_copy(
                    update={
                        "hydration_error": error
                        or _canonical_related_hydration_error(item.identifier),
                    }
                )
            return hydrate_related_issue_context(
                item,
                payload,
                self.config.base_url,
                self.config.requirements,
                comments=comments,
                attachments=attachments,
                provenance_incomplete_reasons=provenance_reasons,
            )

        if issue.parent is not None:
            issue.parent = hydrate(issue.parent)
        issue.children = [hydrate(item) for item in issue.children]
        issue.linked_issues = [hydrate(item) for item in issue.linked_issues]
        issue.dependencies = [hydrate(item) for item in issue.dependencies]

    async def add_comment(self, key: str, body: str) -> None:
        response = await self._client.post(f"/rest/api/2/issue/{key}/comment", json={"body": body})
        response.raise_for_status()

    async def transition_issue(self, key: str, target_status: str) -> bool:
        response = await self._client.get(f"/rest/api/2/issue/{key}/transitions")
        response.raise_for_status()
        transitions = response.json().get("transitions", [])
        wanted = target_status.lower()
        chosen: str | None = None
        for transition in transitions:
            name = str(transition.get("name", "")).lower()
            to_name = str((transition.get("to") or {}).get("name", "")).lower()
            if wanted in {name, to_name}:
                chosen = str(transition["id"])
                break
        if not chosen:
            return False
        post = await self._client.post(f"/rest/api/2/issue/{key}/transitions", json={"transition": {"id": chosen}})
        post.raise_for_status()
        return True

    def _auth(self) -> tuple[dict[str, str], httpx.Auth | None]:
        token = resolve_configured_secret(
            env_name=self.config.auth.token_env,
            config_file=self.config.auth.token_config_file,
            config_key=self.config.auth.token_config_key,
            environ=self.environ,
        )
        if not token:
            raise JiraAuthError(f"Jira token is not configured via {self.config.auth.token_env} or token_config_file")

        if self.config.auth.mode == "basic":
            email_env = self.config.auth.email_env
            email = resolve_configured_secret(
                env_name=email_env,
                config_file=self.config.auth.token_config_file,
                config_key=self.config.auth.email_config_key,
                environ=self.environ,
            )
            if not email:
                raise JiraAuthError(f"Jira email is not configured via {email_env} or email_config_key")
            return {}, httpx.BasicAuth(email, token)
        return {"Authorization": f"Bearer {token}"}, None


def normalize_issue(
    payload: Mapping[str, Any],
    base_url: str,
    *,
    requirements_config: JiraRequirementsConfig | None = None,
    comments: list[IssueComment] | None = None,
    requirement_classifier: RequirementClassifier | None = None,
    provenance_incomplete_reasons: list[str] | None = None,
) -> Issue:
    config = requirements_config or JiraRequirementsConfig()
    classifier = requirement_classifier or MarkerRequirementClassifier()
    raw_fields = payload.get("fields")
    fields = raw_fields if isinstance(raw_fields, Mapping) else {}
    status = fields.get("status") or {}
    priority = fields.get("priority") or {}
    issue_type = fields.get("issuetype") or {}
    assignee = fields.get("assignee") or {}
    reporter = fields.get("reporter") or {}
    creator = fields.get("creator") or {}

    key = str(payload.get("key") or "")
    provenance_reasons = list(
        dict.fromkeys(
            (provenance_incomplete_reasons or [])
            + _declared_changelog_incomplete_reasons(payload, key)
            + _missing_requested_field_reasons(
                fields,
                key,
                ROOT_REQUIREMENT_PRESENCE_FIELDS,
            )
        )
    )
    issue_url = _issue_url(base_url, key)
    labels = [str(label).lower() for label in fields.get("labels") or []]
    if comments is None:
        comments_payload = ((fields.get("comment") or {}).get("comments") or [])
        comments = [
            normalize_comment(
                comment,
                issue_identifier=key,
                issue_url=issue_url,
                authority=config.comment_authority,
                authority_by_author=config.comment_authority_by_author,
            )
            for comment in comments_payload
        ]
        comments = [
            comment
            for comment in comments
            if not is_symphony_status_comment(comment.body, config.symphony_comment_patterns)
        ]
    comments = sorted(comments, key=_comment_sort_key)

    creator_name = display_name(creator) or "unknown"
    updated_at = parse_jira_datetime(fields.get("updated"))
    relation_kwargs = {
        "authority": config.relation_authority,
        "author": creator_name,
        # Jira's global updated timestamp changes for status/assignee edits and
        # must not invalidate requirement approval.
        "timestamp": None,
        "url": issue_url,
    }
    parent = (
        normalize_related_issue(
            fields["parent"],
            key,
            relation="parent",
            direction="parent",
            source_id=f"parent:{(fields['parent'] or {}).get('key') or (fields['parent'] or {}).get('id')}",
            **relation_kwargs,
        )
        if fields.get("parent")
        else None
    )
    children = [
        normalize_related_issue(
            child,
            key,
            relation="child",
            direction="child",
            source_id=f"child:{child.get('key') or child.get('id')}",
            **relation_kwargs,
        )
        for child in fields.get("subtasks") or []
    ]
    linked_issues = normalize_related_links(
        fields.get("issuelinks") or [],
        issue_identifier=key,
        authority=config.relation_authority,
        author=creator_name,
        # Workflow edits update the root issue timestamp but say nothing about
        # when an individual link became requirement-bearing evidence.
        timestamp=None,
        url=issue_url,
    )
    dependencies = [related for related in linked_issues if related.is_dependency]
    components = normalize_named_values(fields.get("components") or [], "component")
    versions = normalize_named_values(fields.get("versions") or [], "affects_version")
    versions.extend(normalize_named_values(fields.get("fixVersions") or [], "fix_version"))
    custom_field_ids = list(dict.fromkeys(config.custom_fields + config.acceptance_criteria_fields))
    custom_fields = {field_id: fields.get(field_id) for field_id in custom_field_ids if field_id in fields}
    attachments = normalize_attachments(
        fields.get("attachment") or [],
        issue_identifier=key,
        issue_url=issue_url,
        authority=config.attachment_authority,
    )

    issue = Issue(
        id=str(payload.get("id") or key),
        identifier=key,
        title=str(fields.get("summary") or ""),
        description=text_from_jira_value(fields.get("description")),
        status=str(status.get("name") or ""),
        priority=priority.get("name"),
        issue_type=issue_type.get("name"),
        assignee=display_name(assignee),
        reporter=display_name(reporter),
        creator=display_name(creator),
        labels=labels,
        url=issue_url,
        created_at=parse_jira_datetime(fields.get("created")),
        updated_at=updated_at,
        comments=comments,
        blocked_by=normalize_blockers(fields.get("issuelinks") or []),
        custom_fields=custom_fields,
        attachments=attachments,
        parent=parent,
        children=_deduplicate_related_issues(children),
        linked_issues=_deduplicate_related_issues(linked_issues),
        dependencies=_deduplicate_related_issues(dependencies),
        components=components,
        versions=versions,
        provenance_incomplete_reasons=provenance_reasons,
        raw=dict(payload),
    )
    issue.requirements_snapshot = build_requirements_snapshot(
        issue,
        payload,
        config,
        classifier,
    )
    return issue


def _attachment_analysis_artifact(
    attachment: IssueAttachment,
) -> RequirementArtifact | None:
    summary = attachment.analysis.summary.strip()
    if attachment.analysis.status != "complete" or not summary:
        return None
    return RequirementArtifact(
        artifact_id=attachment.source.source_id,
        source_type="attachment",
        text=summary,
        value={
            "attachment_id": attachment.id,
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "analysis": {
                "status": attachment.analysis.status,
                "modality": attachment.analysis.modality,
                "analyzer": attachment.analysis.analyzer,
            },
        },
        source=attachment.source,
        kind="supporting_evidence",
    )


def _refresh_related_attachment_artifacts(related: RelatedIssue) -> RelatedIssue:
    requirements = [
        artifact
        for artifact in related.requirements
        if artifact.source_type != "attachment"
    ]
    requirements.extend(
        artifact
        for attachment in related.attachments
        if (artifact := _attachment_analysis_artifact(attachment)) is not None
    )
    return related.model_copy(update={"requirements": requirements})


def build_requirements_snapshot(
    issue: Issue,
    payload: Mapping[str, Any],
    config: JiraRequirementsConfig,
    classifier: RequirementClassifier | None = None,
) -> RequirementsSnapshot:
    classifier = classifier or MarkerRequirementClassifier()
    fields = payload.get("fields") or {}
    names = payload.get("names") or {}
    fallback_author = issue.creator or "unknown"
    # If changelog provenance is absent, creation is less precise but stable.
    # Using issue.updated_at would make unrelated workflow edits material.
    fallback_timestamp = issue.created_at
    description_author, description_timestamp = latest_field_provenance(
        payload,
        field_ids=("description",),
        fallback_author=fallback_author,
        fallback_timestamp=fallback_timestamp,
    )
    description_source = RequirementSource(
        issue_identifier=issue.identifier,
        source_type="description",
        source_id="description",
        field_id="description",
        field_name="Description",
        url=issue.url,
        author=description_author,
        timestamp=description_timestamp,
        authority=config.field_authority.get("description", config.description_authority),
    )
    description = (
        RequirementArtifact(
            artifact_id="description",
            source_type="description",
            text=issue.description,
            value=fields.get("description"),
            source=description_source,
        )
        if issue.description
        else None
    )

    acceptance_fields = set(config.acceptance_criteria_fields)
    custom_artifacts: list[RequirementArtifact] = []
    incomplete_reasons: list[str] = list(issue.provenance_incomplete_reasons)
    for field_id in dict.fromkeys(config.custom_fields + config.acceptance_criteria_fields):
        if field_id not in fields:
            incomplete_reasons.append(f"Configured Jira field {field_id} was not returned.")
            continue
        value = fields.get(field_id)
        if value is None:
            continue
        field_name = str(names.get(field_id) or field_id)
        author, timestamp = latest_field_provenance(
            payload,
            field_ids=(field_id, field_name),
            fallback_author=fallback_author,
            fallback_timestamp=fallback_timestamp,
        )
        source = RequirementSource(
            issue_identifier=issue.identifier,
            source_type="custom_field",
            source_id=f"field:{field_id}",
            field_id=field_id,
            field_name=field_name,
            url=issue.url,
            author=author,
            timestamp=timestamp,
            authority=config.field_authority.get(
                field_id,
                "product" if field_id in acceptance_fields else "context",
            ),
        )
        custom_artifacts.append(
            RequirementArtifact(
                artifact_id=f"field:{field_id}",
                source_type="custom_field",
                text=text_from_jira_value(value) or "",
                value=value,
                source=source,
                kind="acceptance_criterion" if field_id in acceptance_fields else "requirement",
            )
        )

    comment_artifacts = [
        RequirementArtifact(
            artifact_id=f"comment:{comment.id or _anonymous_comment_id(comment)}",
            source_type="comment",
            text=comment.body or "",
            value=comment.body,
            source=comment.source
            or RequirementSource(
                issue_identifier=issue.identifier,
                source_type="comment",
                source_id=f"comment:{comment.id or _anonymous_comment_id(comment)}",
                url=issue.url,
                author=comment.author or "unknown",
                timestamp=comment.updated or comment.created,
                authority=_resolve_comment_authority(
                    {"displayName": comment.author},
                    config.comment_authority,
                    config.comment_authority_by_author,
                ),
            ),
        )
        for comment in issue.comments
        if comment.body
    ]
    attachment_artifacts = [
        artifact
        for attachment in issue.attachments
        if (artifact := _attachment_analysis_artifact(attachment)) is not None
    ]

    snapshot_parent = (
        _refresh_related_attachment_artifacts(issue.parent)
        if issue.parent is not None
        else None
    )
    snapshot_children = [
        _refresh_related_attachment_artifacts(related) for related in issue.children
    ]
    snapshot_linked_issues = [
        _refresh_related_attachment_artifacts(related) for related in issue.linked_issues
    ]
    snapshot_dependencies = [
        _refresh_related_attachment_artifacts(related) for related in issue.dependencies
    ]
    related_artifacts_by_source: dict[tuple[str, str, str], RequirementArtifact] = {}
    related_issues = (
        ([snapshot_parent] if snapshot_parent is not None else [])
        + snapshot_children
        + snapshot_linked_issues
        + snapshot_dependencies
    )
    for related in related_issues:
        if related.hydration_error:
            incomplete_reasons.append(related.hydration_error)
        incomplete_reasons.extend(related.provenance_incomplete_reasons)
        for artifact in related.requirements:
            key = (
                artifact.source.issue_identifier,
                artifact.source.source_id,
                artifact.artifact_id,
            )
            related_artifacts_by_source[key] = artifact

    decisions, decision_incomplete = _classify_decisions(
        issue.identifier,
        [artifact for artifact in [description] if artifact is not None]
        + custom_artifacts
        + comment_artifacts
        + attachment_artifacts
        + list(related_artifacts_by_source.values()),
        classifier,
        config.authority_rank,
    )
    incomplete_reasons.extend(decision_incomplete)
    ranked_authorities = {authority.strip().casefold() for authority in config.authority_rank}
    for attachment in issue.attachments:
        if attachment.analysis.status == "complete" and not attachment.analysis.summary.strip():
            incomplete_reasons.append(
                f"Attachment {attachment.id} ({attachment.filename}) analysis summary is blank."
            )
        elif attachment.analysis.status == "error" or (
            config.require_attachment_analysis and attachment.analysis.status != "complete"
        ):
            incomplete_reasons.append(
                f"Attachment {attachment.id} ({attachment.filename}) analysis is {attachment.analysis.status}."
            )
    seen_related_attachments: set[tuple[str, str]] = set()
    for related in related_issues:
        for attachment in related.attachments:
            attachment_key = (related.identifier, attachment.id)
            if attachment_key in seen_related_attachments:
                continue
            seen_related_attachments.add(attachment_key)
            if attachment.analysis.status == "complete" and not attachment.analysis.summary.strip():
                incomplete_reasons.append(
                    f"Attachment {attachment.id} ({attachment.filename}) on related Jira issue "
                    f"{related.identifier} analysis summary is blank."
                )
            elif attachment.analysis.status == "error" or (
                config.require_attachment_analysis and attachment.analysis.status != "complete"
            ):
                incomplete_reasons.append(
                    f"Attachment {attachment.id} ({attachment.filename}) on related Jira issue "
                    f"{related.identifier} analysis is {attachment.analysis.status}."
                )

    for decision in decisions:
        for source in decision.sources:
            author = source.author.strip().casefold()
            if not author or author == "unknown":
                incomplete_reasons.append(f"Decision {decision.id} has no known source author.")
            if source.timestamp is None:
                incomplete_reasons.append(f"Decision {decision.id} has no known source timestamp.")
            authority = source.authority.strip().casefold()
            if not authority or authority == "unknown":
                incomplete_reasons.append(
                    f"Decision {decision.id} has no known source authority."
                )
            elif authority not in ranked_authorities:
                incomplete_reasons.append(
                    f"Decision {decision.id} has unranked source authority "
                    f"{source.authority!r}."
                )
        if decision.classification == "unresolved_contradiction":
            incomplete_reasons.append(f"Decision {decision.id} is an unresolved contradiction.")

    snapshot = RequirementsSnapshot(
        issue_id=issue.id,
        issue_identifier=issue.identifier,
        issue_url=issue.url,
        description=description,
        custom_fields=custom_artifacts,
        comments=comment_artifacts,
        attachments=issue.attachments,
        parent=snapshot_parent,
        children=snapshot_children,
        linked_issues=snapshot_linked_issues,
        dependencies=snapshot_dependencies,
        components=issue.components,
        versions=issue.versions,
        current_requirements=[
            item for item in decisions if item.classification == "current"
        ],
        superseded_requirements=[
            item for item in decisions if item.classification == "superseded"
        ],
        inferred_behavior=[
            item for item in decisions if item.classification == "inferred"
        ],
        unresolved_contradictions=[
            item
            for item in decisions
            if item.classification == "unresolved_contradiction"
        ],
        incomplete_reasons=list(dict.fromkeys(incomplete_reasons)),
    )
    return snapshot.with_content_hash()


_MAX_DECISION_UNITS_PER_ARTIFACT = 64
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)(.+?)\s*$")
_CLAUSE_BOUNDARY = re.compile(
    r"(?<=[.!?;])\s+(?=(?:\[[^\]]+\]\s*)?[A-Z0-9])"
)
_DECISION_MARKER = re.compile(
    r"\[(?:classification\s*:\s*[^\]]+|supersedes\s*:[^\]]+|"
    r"inferred|assumption|superseded|obsolete|contradiction|"
    r"unresolved[_ -]?contradiction)\]",
    re.IGNORECASE,
)
_ORDER_TOKEN = re.compile(r"\b(before|after)\b", re.IGNORECASE)


def _decision_text_units(text: str) -> list[str]:
    """Split explicit bullets/paragraph clauses without pretending to understand prose."""

    segments: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        bullet = _BULLET_PREFIX.match(line)
        if bullet:
            if current:
                segments.append(" ".join(current).strip())
            current = [bullet.group(1).strip()]
            continue
        stripped = line.strip()
        if not stripped:
            if current:
                segments.append(" ".join(current).strip())
                current = []
            continue
        current.append(stripped)
    if current:
        segments.append(" ".join(current).strip())

    units: list[str] = []
    seen: set[str] = set()
    for segment in segments or [text.strip()]:
        for candidate in _CLAUSE_BOUNDARY.split(segment):
            unit = candidate.strip()
            normalized = _normalized_decision_text(unit)
            if unit and normalized and normalized not in seen:
                units.append(unit)
                seen.add(normalized)
    return units


def _normalized_decision_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _expand_decision_artifact(
    artifact: RequirementArtifact,
) -> tuple[list[RequirementArtifact], str | None]:
    units = _decision_text_units(artifact.text)
    if len(units) <= 1:
        return [artifact], None
    if len(units) > _MAX_DECISION_UNITS_PER_ARTIFACT:
        return [artifact], (
            f"Requirement artifact {artifact.source.issue_identifier}:{artifact.artifact_id} "
            f"contains {len(units)} decision units; the maximum is "
            f"{_MAX_DECISION_UNITS_PER_ARTIFACT}."
        )

    expanded: list[RequirementArtifact] = []
    for unit in units:
        digest = hashlib.sha256(
            _normalized_decision_text(unit).encode("utf-8")
        ).hexdigest()[:16]
        suffix = f"#unit:{digest}"
        expanded.append(
            artifact.model_copy(
                update={
                    "artifact_id": f"{artifact.artifact_id}{suffix}",
                    "text": unit,
                    "value": unit,
                    "source": artifact.source.model_copy(
                        update={
                            "source_id": f"{artifact.source.source_id}{suffix}",
                            "location": f"decision-unit:{digest}",
                        }
                    ),
                }
            )
        )
    return expanded, None


def _source_identity(source: RequirementSource) -> tuple[str, str, str]:
    return (source.issue_identifier, source.source_type, source.source_id)


def _merge_sources(*groups: list[RequirementSource]) -> list[RequirementSource]:
    merged: dict[tuple[str, str, str], RequirementSource] = {}
    for group in groups:
        for source in group:
            merged[_source_identity(source)] = source
    return [merged[key] for key in sorted(merged)]


def _mark_unresolved(
    decision: RequirementDecision,
    *source_groups: list[RequirementSource],
) -> None:
    decision.classification = "unresolved_contradiction"
    decision.sources = _merge_sources(decision.sources, *source_groups)


def _authority_value(
    decision: RequirementDecision,
    authority_rank: Mapping[str, int],
) -> tuple[str, int | None]:
    authority = decision.sources[0].authority if decision.sources else "unknown"
    return authority, authority_rank.get(authority.strip().casefold())


def _supersession_cycle_nodes(
    edges: list[tuple[RequirementDecision, RequirementDecision]],
) -> set[str]:
    adjacency: dict[str, list[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source.id, []).append(target.id)
    state: dict[str, int] = {}
    stack: list[str] = []
    positions: dict[str, int] = {}
    cycle_nodes: set[str] = set()

    def visit(node: str) -> None:
        state[node] = 1
        positions[node] = len(stack)
        stack.append(node)
        for target in adjacency.get(node, []):
            if state.get(target, 0) == 0:
                visit(target)
            elif state.get(target) == 1:
                cycle_nodes.update(stack[positions[target] :])
        stack.pop()
        positions.pop(node, None)
        state[node] = 2

    for node in sorted(adjacency):
        if state.get(node, 0) == 0:
            visit(node)
    return cycle_nodes


def _conflict_text(text: str) -> str:
    without_markers = _DECISION_MARKER.sub(" ", text)
    return re.sub(r"[^a-z0-9]+", " ", without_markers.casefold()).strip()


def _polarity_signature(text: str) -> tuple[str, bool] | None:
    normalized = _conflict_text(text)
    if not normalized:
        return None
    normalized = normalized.replace("doesn t", "does not").replace("don t", "do not")
    normalized = normalized.replace("isn t", "is not").replace("aren t", "are not")
    normalized = normalized.replace("can t", "cannot")
    negative = False
    replacements = (
        (r"\b(?:does|do|did) not\b", ""),
        (r"\b(is|are|was|were|will|must|should|can) not\b", r"\1"),
        (r"\bcannot\b", "can"),
    )
    for pattern, replacement in replacements:
        revised, count = re.subn(pattern, replacement, normalized)
        if count:
            negative = True
            normalized = revised
    words = normalized.split()
    lemma = {
        "sees": "see",
        "shows": "show",
        "displays": "display",
        "includes": "include",
        "uses": "use",
        "allows": "allow",
        "requires": "require",
        "has": "have",
    }
    signature = " ".join(lemma.get(word, word) for word in words)
    return (signature, negative) if signature else None


def _order_signature(text: str) -> tuple[str, str] | None:
    clean = _DECISION_MARKER.sub(" ", text)
    matches = [match.casefold() for match in _ORDER_TOKEN.findall(clean)]
    if not matches or len(set(matches)) != 1:
        return None
    relation = matches[0]
    signature = _conflict_text(_ORDER_TOKEN.sub(" order ", clean))
    return (signature, relation) if signature else None


def _reconcile_clear_conflicts(
    decisions: list[RequirementDecision],
) -> list[RequirementDecision]:
    current = [decision for decision in decisions if decision.classification == "current"]
    groups: dict[tuple[str, str, str], dict[str, list[RequirementDecision]]] = {}
    for decision in current:
        conflict_layer = (
            "requirement"
            if decision.kind in {"requirement", "supporting_evidence"}
            else decision.kind
        )
        polarity = _polarity_signature(decision.text)
        if polarity is not None:
            signature, negative = polarity
            groups.setdefault((conflict_layer, "polarity", signature), {}).setdefault(
                "negative" if negative else "positive", []
            ).append(decision)
        order = _order_signature(decision.text)
        if order is not None:
            signature, relation = order
            groups.setdefault((conflict_layer, "order", signature), {}).setdefault(
                relation, []
            ).append(decision)

    conflict_edges: set[tuple[str, str]] = set()
    for (_, conflict_kind, _), values in groups.items():
        opposing = (
            (values.get("positive", []), values.get("negative", []))
            if conflict_kind == "polarity"
            else (values.get("before", []), values.get("after", []))
        )
        for left in opposing[0]:
            for right in opposing[1]:
                conflict_edges.add(tuple(sorted((left.id, right.id))))

    if not conflict_edges:
        return decisions
    by_id = {decision.id: decision for decision in decisions}
    adjacency: dict[str, set[str]] = {}
    for left, right in conflict_edges:
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)

    conflicted: set[str] = set()
    replacements: list[RequirementDecision] = []
    for start in sorted(adjacency):
        if start in conflicted:
            continue
        stack = [start]
        component: set[str] = set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency.get(node, set()) - component)
        conflicted.update(component)
        members = [by_id[decision_id] for decision_id in sorted(component)]
        digest = hashlib.sha256("\n".join(sorted(component)).encode("utf-8")).hexdigest()[:16]
        replacements.append(
            RequirementDecision(
                id=f"jira:conflict:{digest}",
                text="Unresolved conflict: "
                + " | ".join(f"{member.id}: {member.text}" for member in members),
                kind=(
                    "requirement"
                    if any(member.kind == "requirement" for member in members)
                    else members[0].kind
                ),
                classification="unresolved_contradiction",
                sources=_merge_sources(*(member.sources for member in members)),
            )
        )
    return [decision for decision in decisions if decision.id not in conflicted] + replacements


def _classify_decisions(
    issue_identifier: str,
    artifacts: list[RequirementArtifact],
    classifier: RequirementClassifier,
    authority_rank: Mapping[str, int],
) -> tuple[list[RequirementDecision], list[str]]:
    decisions: list[RequirementDecision] = []
    by_alias: dict[str, list[RequirementDecision]] = {}
    pending_references: dict[str, list[str]] = {}
    incomplete: list[str] = []
    expanded_artifacts: list[tuple[RequirementArtifact, bool]] = []
    for original in artifacts:
        units, expansion_error = _expand_decision_artifact(original)
        if expansion_error:
            incomplete.append(expansion_error)
        expanded_artifacts.extend((unit, bool(expansion_error)) for unit in units)

    for artifact, force_unresolved in expanded_artifacts:
        source_issue_identifier = artifact.source.issue_identifier or issue_identifier
        decision_id = f"jira:{source_issue_identifier}:{artifact.artifact_id}"
        decision = RequirementDecision(
            id=decision_id,
            text=artifact.text,
            kind=artifact.kind,
            classification=(
                "unresolved_contradiction"
                if force_unresolved
                else classifier.classify(artifact)
            ),
            sources=[artifact.source],
        )
        decisions.append(decision)
        base_artifact_id = artifact.artifact_id.split("#unit:", 1)[0]
        base_source_id = artifact.source.source_id.split("#unit:", 1)[0]
        aliases = {
            decision_id.casefold(),
            artifact.artifact_id.casefold(),
            artifact.source.source_id.casefold(),
            base_artifact_id.casefold(),
            base_source_id.casefold(),
            f"{source_issue_identifier}:{artifact.artifact_id}".casefold(),
            f"{source_issue_identifier}:{artifact.source.source_id}".casefold(),
            f"{source_issue_identifier}:{base_artifact_id}".casefold(),
            f"{source_issue_identifier}:{base_source_id}".casefold(),
        }
        for alias in aliases:
            by_alias.setdefault(alias, []).append(decision)
        pending_references[decision_id] = classifier.supersedes(artifact)

    ranked_authorities = {
        authority.strip().casefold(): rank
        for authority, rank in authority_rank.items()
    }
    candidate_edges: list[tuple[RequirementDecision, RequirementDecision]] = []
    for decision in decisions:
        source_authority, source_rank = _authority_value(
            decision,
            ranked_authorities,
        )
        references = pending_references[decision.id]
        if references and decision.classification != "current":
            incomplete.append(
                f"Decision {decision.id} cannot supersede another requirement while "
                f"classified as {decision.classification}."
            )
            _mark_unresolved(decision)
            continue
        resolved_for_decision: list[tuple[RequirementDecision, RequirementDecision]] = []
        invalid_reference = False
        for raw_reference in references:
            candidates = by_alias.get(raw_reference.casefold(), [])
            if len(candidates) > 1 and decision.sources:
                source_issue_identifier = decision.sources[0].issue_identifier
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.sources
                    and candidate.sources[0].issue_identifier == source_issue_identifier
                ]
            if not candidates:
                incomplete.append(
                    f"Decision {decision.id} references unknown superseded requirement {raw_reference}."
                )
                _mark_unresolved(decision)
                invalid_reference = True
                continue
            if len(candidates) > 1:
                incomplete.append(
                    f"Decision {decision.id} has ambiguous superseded requirement {raw_reference}; "
                    "use the exact decision-unit ID."
                )
                _mark_unresolved(
                    decision,
                    *[candidate.sources for candidate in candidates],
                )
                invalid_reference = True
                continue
            target = candidates[0]
            if target.id == decision.id:
                incomplete.append(f"Decision {decision.id} cannot supersede itself.")
                _mark_unresolved(decision)
                invalid_reference = True
                continue
            target_authority, target_rank = _authority_value(
                target,
                ranked_authorities,
            )
            if source_rank is None or target_rank is None or source_rank < target_rank:
                reason = (
                    "one or both authorities are unranked"
                    if source_rank is None or target_rank is None
                    else "the overriding authority ranks below the target"
                )
                incomplete.append(
                    f"Decision {decision.id} with authority {source_authority} cannot "
                    f"supersede {target.id} with authority {target_authority}: {reason}."
                )
                _mark_unresolved(decision, target.sources)
                invalid_reference = True
                continue
            resolved_for_decision.append((decision, target))
        if not invalid_reference:
            candidate_edges.extend(resolved_for_decision)

    cycle_nodes = _supersession_cycle_nodes(candidate_edges)
    if cycle_nodes:
        cycle_decisions = [
            decision for decision in decisions if decision.id in cycle_nodes
        ]
        cycle_sources = _merge_sources(
            *(decision.sources for decision in cycle_decisions)
        )
        formatted = ", ".join(sorted(cycle_nodes))
        incomplete.append(f"Supersession cycle detected among decisions: {formatted}.")
        for decision in cycle_decisions:
            _mark_unresolved(decision, cycle_sources)

    for source, target in candidate_edges:
        if source.id in cycle_nodes or target.id in cycle_nodes:
            if source.id not in cycle_nodes:
                incomplete.append(
                    f"Decision {source.id} targets a supersession cycle at {target.id}."
                )
                _mark_unresolved(source, target.sources)
            continue
        if target.id in source.supersedes:
            continue
        source.supersedes.append(target.id)
        target.superseded_by.append(source.id)
        target.classification = "superseded"

    return _reconcile_clear_conflicts(decisions), incomplete


def normalize_comment(
    payload: Mapping[str, Any],
    *,
    issue_identifier: str = "",
    issue_url: str = "",
    authority: str = "product",
    authority_by_author: Mapping[str, str] | None = None,
) -> IssueComment:
    comment_id = str(payload.get("id")) if payload.get("id") is not None else None
    author_payload = payload.get("updateAuthor") or payload.get("author") or {}
    author = display_name(author_payload)
    resolved_authority = _resolve_comment_authority(
        author_payload,
        authority,
        authority_by_author,
    )
    created = parse_jira_datetime(payload.get("created"))
    updated = parse_jira_datetime(payload.get("updated"))
    comment_url = issue_url
    if issue_url and comment_id:
        comment_url = (
            f"{issue_url}?focusedCommentId={comment_id}"
            "&page=com.atlassian.jira.plugin.system.issuetabpanels:comment-tabpanel"
            f"#comment-{comment_id}"
        )
    return IssueComment(
        id=comment_id,
        author=author,
        body=text_from_jira_value(payload.get("body")),
        created=created,
        updated=updated,
        source=RequirementSource(
            issue_identifier=issue_identifier,
            source_type="comment",
            source_id=f"comment:{comment_id or _mapping_digest(payload)}",
            url=comment_url or None,
            author=author or "unknown",
            timestamp=updated or created,
            authority=resolved_authority,
        ),
    )


def normalize_attachments(
    attachments: list[Mapping[str, Any]],
    *,
    issue_identifier: str,
    issue_url: str,
    authority: str,
) -> list[IssueAttachment]:
    normalized: list[IssueAttachment] = []
    for payload in attachments:
        attachment_id = str(payload.get("id") or _mapping_digest(payload))
        author = display_name(payload.get("author") or {}) or "unknown"
        created = parse_jira_datetime(payload.get("created"))
        normalized.append(
            IssueAttachment(
                id=attachment_id,
                filename=str(payload.get("filename") or attachment_id),
                mime_type=payload.get("mimeType"),
                size=_optional_int(payload.get("size")),
                content_url=payload.get("content"),
                thumbnail_url=payload.get("thumbnail"),
                author=author,
                created_at=created,
                source=RequirementSource(
                    issue_identifier=issue_identifier,
                    source_type="attachment",
                    source_id=f"attachment:{attachment_id}",
                    url=payload.get("content") or issue_url,
                    author=author,
                    timestamp=created,
                    authority=authority,
                ),
                analysis=AttachmentAnalysis(
                    status="not_configured",
                    modality="metadata",
                    summary="Attachment has not been analyzed.",
                ),
            )
        )
    return sorted(normalized, key=lambda item: item.id)


def normalize_related_links(
    links: list[Mapping[str, Any]],
    *,
    issue_identifier: str,
    authority: str,
    author: str,
    timestamp: datetime | None,
    url: str,
) -> list[RelatedIssue]:
    related: list[RelatedIssue] = []
    for link in links:
        link_type = link.get("type") or {}
        link_id = str(link.get("id") or _mapping_digest(link))
        inward_issue = link.get("inwardIssue")
        if inward_issue:
            relation = str(link_type.get("inward") or link_type.get("name") or "linked to")
            related.append(
                normalize_related_issue(
                    inward_issue,
                    issue_identifier,
                    relation=relation,
                    direction="inward",
                    source_id=f"link:{link_id}:inward",
                    authority=authority,
                    author=author,
                    timestamp=timestamp,
                    url=url,
                )
            )
        outward_issue = link.get("outwardIssue")
        if outward_issue:
            relation = str(link_type.get("outward") or link_type.get("name") or "linked to")
            related.append(
                normalize_related_issue(
                    outward_issue,
                    issue_identifier,
                    relation=relation,
                    direction="outward",
                    source_id=f"link:{link_id}:outward",
                    authority=authority,
                    author=author,
                    timestamp=timestamp,
                    url=url,
                )
            )
    return related


def normalize_related_issue(
    payload: Mapping[str, Any],
    issue_identifier: str,
    *,
    relation: str,
    direction: str,
    source_id: str,
    authority: str,
    author: str = "unknown",
    timestamp: datetime | None = None,
    url: str | None = None,
) -> RelatedIssue:
    fields = payload.get("fields") or {}
    relation_lower = relation.lower()
    is_dependency = any(
        marker in relation_lower
        for marker in ("block", "depend", "required by", "requires", "precedes")
    )
    identifier = str(payload.get("key") or payload.get("id") or "")
    related_url = _related_issue_url(url, identifier)
    return RelatedIssue(
        id=str(payload.get("id")) if payload.get("id") is not None else None,
        identifier=identifier,
        title=fields.get("summary"),
        status=(fields.get("status") or {}).get("name"),
        issue_type=(fields.get("issuetype") or {}).get("name"),
        relation=relation,
        direction=direction,  # type: ignore[arg-type]
        is_dependency=is_dependency,
        url=related_url,
        source=RequirementSource(
            issue_identifier=issue_identifier,
            source_type="relation",
            source_id=source_id,
            url=related_url,
            author=author,
            timestamp=timestamp,
            authority=authority,
        ),
    )


def hydrate_related_issue_context(
    related: RelatedIssue,
    payload: Mapping[str, Any],
    base_url: str,
    config: JiraRequirementsConfig,
    *,
    comments: list[IssueComment] | None = None,
    attachments: list[IssueAttachment] | None = None,
    provenance_incomplete_reasons: list[str] | None = None,
) -> RelatedIssue:
    raw_fields = payload.get("fields")
    fields = raw_fields if isinstance(raw_fields, Mapping) else {}
    names = payload.get("names") or {}
    creator = display_name(fields.get("creator") or {}) or "unknown"
    created = parse_jira_datetime(fields.get("created"))
    url = _issue_url(base_url, related.identifier)
    provenance_reasons = list(
        dict.fromkeys(
            (provenance_incomplete_reasons or [])
            + _declared_changelog_incomplete_reasons(payload, related.identifier)
            + _missing_requested_field_reasons(
                fields,
                related.identifier,
                RELATED_REQUIREMENT_PRESENCE_FIELDS,
            )
        )
    )
    if comments is None:
        comments = [
            normalize_comment(
                comment,
                issue_identifier=related.identifier,
                issue_url=url,
                authority=config.comment_authority,
                authority_by_author=config.comment_authority_by_author,
            )
            for comment in ((fields.get("comment") or {}).get("comments") or [])
        ]
        comments = [
            comment
            for comment in comments
            if not is_symphony_status_comment(comment.body, config.symphony_comment_patterns)
        ]
    comments = sorted(comments, key=_comment_sort_key)
    if attachments is None:
        attachments = normalize_attachments(
            fields.get("attachment") or [],
            issue_identifier=related.identifier,
            issue_url=url,
            authority=config.attachment_authority,
        )
    artifacts: list[RequirementArtifact] = []
    description = text_from_jira_value(fields.get("description"))
    if description:
        author, timestamp = latest_field_provenance(
            payload,
            field_ids=("description",),
            fallback_author=creator,
            fallback_timestamp=created,
        )
        artifacts.append(
            RequirementArtifact(
                artifact_id="description",
                source_type="description",
                text=description,
                value=fields.get("description"),
                source=RequirementSource(
                    issue_identifier=related.identifier,
                    source_type="description",
                    source_id="description",
                    field_id="description",
                    field_name="Description",
                    url=url,
                    author=author,
                    timestamp=timestamp,
                    authority=config.field_authority.get(
                        "description",
                        config.description_authority,
                    ),
                ),
            )
        )

    acceptance_fields = set(config.acceptance_criteria_fields)
    custom_fields: dict[str, Any] = {}
    for field_id in dict.fromkeys(config.custom_fields + config.acceptance_criteria_fields):
        if field_id not in fields:
            provenance_reasons.append(
                f"Configured Jira field {field_id} was not returned for related issue "
                f"{related.identifier}."
            )
            continue
        if fields.get(field_id) is None:
            continue
        value = fields[field_id]
        custom_fields[field_id] = value
        field_name = str(names.get(field_id) or field_id)
        author, timestamp = latest_field_provenance(
            payload,
            field_ids=(field_id, field_name),
            fallback_author=creator,
            fallback_timestamp=created,
        )
        artifacts.append(
            RequirementArtifact(
                artifact_id=f"field:{field_id}",
                source_type="custom_field",
                text=text_from_jira_value(value) or "",
                value=value,
                source=RequirementSource(
                    issue_identifier=related.identifier,
                    source_type="custom_field",
                    source_id=f"field:{field_id}",
                    field_id=field_id,
                    field_name=field_name,
                    url=url,
                    author=author,
                    timestamp=timestamp,
                    authority=config.field_authority.get(
                        field_id,
                        "product" if field_id in acceptance_fields else "context",
                    ),
                ),
                kind=(
                    "acceptance_criterion"
                    if field_id in acceptance_fields
                    else "requirement"
                ),
            )
        )

    artifacts.extend(
        RequirementArtifact(
            artifact_id=f"comment:{comment.id or _anonymous_comment_id(comment)}",
            source_type="comment",
            text=comment.body or "",
            value=comment.body,
            source=comment.source
            or RequirementSource(
                issue_identifier=related.identifier,
                source_type="comment",
                source_id=f"comment:{comment.id or _anonymous_comment_id(comment)}",
                url=url,
                author=comment.author or "unknown",
                timestamp=comment.updated or comment.created,
                authority=_resolve_comment_authority(
                    {"displayName": comment.author},
                    config.comment_authority,
                    config.comment_authority_by_author,
                ),
            ),
        )
        for comment in comments
        if comment.body
    )
    artifacts.extend(
        artifact
        for attachment in attachments
        if (artifact := _attachment_analysis_artifact(attachment)) is not None
    )



    status = fields.get("status") or {}
    issue_type = fields.get("issuetype") or {}
    return related.model_copy(
        update={
            "title": fields.get("summary") or related.title,
            "status": status.get("name") or related.status,
            "issue_type": issue_type.get("name") or related.issue_type,
            "url": url,
            "description": description,
            "custom_fields": custom_fields,
            "comments": comments,
            "attachments": attachments,
            "requirements": artifacts,
            "hydration_error": None,
            "provenance_incomplete_reasons": list(dict.fromkeys(provenance_reasons)),
            "source": related.source.model_copy(update={"url": url}),
        }
    )


def normalize_named_values(
    values: list[Mapping[str, Any]],
    kind: str,
) -> list[JiraNamedValue]:
    normalized = [
        JiraNamedValue(
            id=str(value.get("id")) if value.get("id") is not None else None,
            name=str(value.get("name") or value.get("value") or ""),
            kind=kind,  # type: ignore[arg-type]
            description=value.get("description"),
            archived=value.get("archived"),
            released=value.get("released"),
            release_date=value.get("releaseDate"),
        )
        for value in values
    ]
    return sorted(normalized, key=lambda item: (item.name, item.id or ""))


def normalize_blockers(links: list[Mapping[str, Any]]) -> list[IssueBlocker]:
    blockers: list[IssueBlocker] = []
    for link in links:
        link_type = link.get("type") or {}
        inward = str(link_type.get("inward") or "").lower()
        outward = str(link_type.get("outward") or "").lower()
        issue_payload = None
        if "blocked by" in inward and link.get("inwardIssue"):
            issue_payload = link.get("inwardIssue")
        elif "blocks" in outward and link.get("outwardIssue"):
            issue_payload = link.get("outwardIssue")
        if issue_payload:
            fields = issue_payload.get("fields") or {}
            blockers.append(
                IssueBlocker(
                    id=str(issue_payload.get("id")) if issue_payload.get("id") is not None else None,
                    identifier=issue_payload.get("key"),
                    status=(fields.get("status") or {}).get("name"),
                )
            )
    return blockers


def _declared_changelog_incomplete_reasons(
    payload: Mapping[str, Any],
    issue_identifier: str,
) -> list[str]:
    changelog = payload.get("changelog")
    if not isinstance(changelog, Mapping):
        return [
            f"Jira changelog for {issue_identifier} was not returned as an object; "
            "field provenance is incomplete."
        ]
    declared_total = _nonnegative_int(changelog.get("total"))
    if declared_total is None:
        return [
            f"Jira changelog for {issue_identifier} did not provide a valid "
            "non-negative total; field provenance is incomplete."
        ]
    raw_histories = changelog.get("histories")
    if not isinstance(raw_histories, list):
        return [
            f"Jira changelog for {issue_identifier} did not provide a histories list; "
            "field provenance is incomplete."
        ]
    histories = [
        history
        for history in raw_histories
        if isinstance(history, Mapping)
    ]
    if len(histories) != len(raw_histories):
        return [
            f"Jira changelog for {issue_identifier} contained a malformed history; "
            "field provenance is incomplete."
        ]
    start_at = _strict_nonnegative_int(changelog.get("startAt", 0))
    if start_at is None:
        return [
            f"Jira changelog for {issue_identifier} provided an invalid startAt; "
            "field provenance is incomplete."
        ]
    if start_at == 0 and len(histories) == declared_total:
        return []
    return [
        f"Jira changelog for {issue_identifier} declared {declared_total} histories "
        f"but only {len(histories)} were available; field provenance is incomplete."
    ]



def _history_target_signature(item: Mapping[str, Any]) -> str:
    target = {
        key: item.get(key)
        for key in ("to", "toString")
        if key in item
    }
    if not target:
        return "<target-unavailable>"
    return json.dumps(target, sort_keys=True, separators=(",", ":"), default=str)


def latest_field_provenance(
    payload: Mapping[str, Any],
    *,
    field_ids: tuple[str, ...],
    fallback_author: str,
    fallback_timestamp: datetime | None,
) -> tuple[str, datetime | None]:
    wanted = {field_id.lower() for field_id in field_ids}
    provenance_candidates: list[tuple[datetime, str | None, str]] = []
    changelog = payload.get("changelog")
    histories = (
        changelog.get("histories") or []
        if isinstance(changelog, Mapping)
        else []
    )
    for history in histories:
        if not isinstance(history, Mapping):
            return "unknown", None
        items = history.get("items")
        if not isinstance(items, list):
            return "unknown", None
        matching_targets: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                return "unknown", None
            candidates = {
                str(item.get("fieldId") or "").lower(),
                str(item.get("field") or "").lower(),
            }
            if wanted & candidates:
                matching_targets.add(_history_target_signature(item))
        if not matching_targets:
            continue
        created = parse_jira_datetime(history.get("created"))
        if len(matching_targets) != 1:
            return "unknown", None
        if created is None:
            return "unknown", None
        author = display_name(history.get("author") or {})
        provenance_candidates.append((created, author, next(iter(matching_targets))))
    if not provenance_candidates:
        return fallback_author, fallback_timestamp
    latest_timestamp = max(candidate[0] for candidate in provenance_candidates)
    latest_candidates = [
        candidate
        for candidate in provenance_candidates
        if candidate[0] == latest_timestamp
    ]
    authors = {(candidate[1] or "").strip().casefold() for candidate in latest_candidates}
    targets = {candidate[2] for candidate in latest_candidates}
    if len(authors) != 1 or len(targets) != 1:
        return "unknown", None
    display_authors = sorted(
        {candidate[1] for candidate in latest_candidates if candidate[1]},
        key=lambda author: (author.casefold(), author),
    )
    return (display_authors[0] if display_authors else "unknown"), latest_timestamp


def is_symphony_status_comment(
    body: str | None,
    patterns: list[str] | None = None,
) -> bool:
    if not body:
        return False
    configured = patterns if patterns is not None else JiraRequirementsConfig().symphony_comment_patterns
    return any(re.search(pattern, body, flags=re.IGNORECASE) is not None for pattern in configured)


def _canonical_related_hydration_error(
    issue_identifier: str,
    error: Exception | None = None,
) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        detail = f"HTTP {error.response.status_code}"
    else:
        detail = "request failed"
    return (
        f"Related Jira issue {issue_identifier} could not be hydrated ({detail}); "
        "requirement context is incomplete."
    )


def _resolve_comment_authority(
    author: Mapping[str, Any],
    fallback: str,
    authority_by_author: Mapping[str, str] | None,
) -> str:
    if not authority_by_author:
        return fallback
    normalized = {
        str(identity).strip().casefold(): authority
        for identity, authority in authority_by_author.items()
    }
    for identity in (
        author.get("displayName"),
        author.get("emailAddress"),
        author.get("name"),
    ):
        if identity is not None:
            matched = normalized.get(str(identity).strip().casefold())
            if matched is not None:
                return matched
    return fallback


def display_name(user: Mapping[str, Any]) -> str | None:
    return user.get("displayName") or user.get("emailAddress") or user.get("name")


def parse_jira_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    text = text.replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", text):
        text = f"{text[:-5]}{text[-5:-2]}:{text[-2:]}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def text_from_jira_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [text_from_jira_value(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        value_type = value.get("type")
        if value_type == "text" and "text" in value:
            return str(value["text"])
        if value_type == "hardBreak":
            return "\n"
        if "content" in value:
            content = value.get("content") or []
            parts = [text_from_jira_value(item) for item in content]
            separator = "" if value_type in {"paragraph", "heading"} else "\n"
            return separator.join(part for part in parts if part)
        if "value" in value:
            return text_from_jira_value(value["value"])
        if "displayName" in value:
            return str(value["displayName"])
        if "name" in value:
            return str(value["name"])
        return json.dumps(value, sort_keys=True)
    return str(value)


def _missing_requested_field_reasons(
    fields: Mapping[str, Any],
    issue_identifier: str,
    requested_fields: tuple[str, ...],
) -> list[str]:
    return [
        f"Jira issue {issue_identifier} did not return requested field {field_id}; "
        "requirement context is incomplete."
        for field_id in requested_fields
        if field_id not in fields
    ]


def _same_origin_attachment_url(base_url: str, content_url: str) -> str:
    base = urlsplit(base_url)
    resolved_url = urljoin(f"{base_url.rstrip('/')}/", content_url)
    resolved = urlsplit(resolved_url)
    base_origin = _http_origin(base)
    resolved_origin = _http_origin(resolved)
    if base_origin is None or resolved_origin != base_origin:
        raise JiraError(
            "Jira attachment content URL must use the exact configured Jira origin"
        )
    return resolved_url


def _http_origin(url: Any) -> tuple[str, str, int] | None:
    scheme = url.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
    ):
        return None
    try:
        port = url.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, url.hostname.casefold(), port


def _oversized_attachment(
    attachment: IssueAttachment,
    limit: int,
    *,
    declared_size: int | None = None,
) -> IssueAttachment:
    if declared_size is None:
        summary = f"Downloaded attachment exceeded the configured {limit}-byte limit."
    else:
        summary = (
            f"Attachment response declared {declared_size} bytes; the configured limit "
            f"is {limit} bytes."
        )
    return attachment.model_copy(
        update={
            "analysis": AttachmentAnalysis(
                status="skipped",
                modality="metadata",
                summary=summary,
            )
        }
    )


def _canonical_child_discovery_error(
    issue_identifier: str,
    detail: str,
) -> str:
    return (
        f"Jira child discovery for {issue_identifier} is incomplete ({detail}); "
        "child requirement context is incomplete."
    )


def _jira_jql_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _issue_url(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/browse/{key}" if base_url else ""


def _related_issue_url(root_issue_url: str | None, identifier: str) -> str | None:
    if not root_issue_url:
        return None
    browse_marker = "/browse/"
    if browse_marker in root_issue_url:
        base_url = root_issue_url.split(browse_marker, 1)[0]
        return _issue_url(base_url, identifier)
    return root_issue_url


def _comment_sort_key(comment: IssueComment) -> tuple[datetime, str]:
    timestamp = comment.created or comment.updated or datetime.min.replace(tzinfo=timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp, comment.id or ""


def _anonymous_comment_id(comment: IssueComment) -> str:
    value = "|".join(
        (
            comment.author or "",
            comment.created.isoformat() if comment.created else "",
            comment.body or "",
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _mapping_digest(payload: Mapping[str, Any]) -> str:
    value = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    parsed = _optional_int(value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def _strict_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _pagination_start_error(
    payload: Mapping[str, Any],
    requested_offset: int,
) -> str | None:
    if "startAt" not in payload:
        return None
    returned_offset = _strict_nonnegative_int(payload.get("startAt"))
    if returned_offset is None:
        return "returned an invalid startAt"
    if returned_offset != requested_offset:
        return f"returned startAt {returned_offset}"
    return None


def _deduplicate_related_issues(issues: list[RelatedIssue]) -> list[RelatedIssue]:
    deduplicated: dict[tuple[str, str, str], RelatedIssue] = {}
    for issue in issues:
        deduplicated[(issue.identifier, issue.relation, issue.direction)] = issue
    return sorted(
        deduplicated.values(),
        key=lambda item: (item.identifier, item.relation, item.direction),
    )
