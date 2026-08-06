from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

from symphony_jira.config import JiraRequirementsConfig, TrackerConfig
from symphony_jira.jira import (
    BASE_ISSUE_FIELDS,
    RELATED_ISSUE_FIELDS,
    JiraClient,
    build_requirements_snapshot,
    hydrate_related_issue_context,
    normalize_issue,
)
from symphony_jira.models import (
    AttachmentAnalysis,
    IssueAttachment,
    RelatedIssue,
    RequirementArtifact,
    RequirementDecision,
    RequirementSource,
    RequirementsSnapshot,
    diff_requirements_snapshots,
    issue_requirements_fingerprint,
    requirements_planning_authority_equivalent,
)


def sample_issue_payload() -> dict:
    return {
        "id": "10001",
        "key": "T-1",
        "names": {},
        "fields": {
            "summary": "Fix thing",
            "description": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Description"}]}],
            },
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "issuetype": {"name": "Bug"},
            "assignee": {"displayName": "Ada"},
            "reporter": {"displayName": "Grace"},
            "creator": {"displayName": "Grace"},
            "labels": ["Codex-Ready", "Backend"],
            "created": "2025-01-02T03:04:05.000+0000",
            "updated": "2025-01-03T03:04:05.000+0000",
            "comment": {
                "comments": [
                    {
                        "id": "200",
                        "author": {"displayName": "Lin"},
                        "body": "Looks reproducible",
                        "created": "2025-01-04T03:04:05.000+0000",
                    }
                ]
            },
            "attachment": [],
            "parent": None,
            "subtasks": [],
            "issuelinks": [],
            "components": [],
            "versions": [],
            "fixVersions": [],
        },
        "changelog": {"startAt": 0, "total": 0, "histories": []},
    }


class FakeVisionAnalyzer:
    async def analyze(self, attachment: IssueAttachment, content: bytes) -> AttachmentAnalysis:
        return AttachmentAnalysis(
            status="complete",
            modality="vision",
            summary=f"Mockup shows role-specific columns ({len(content)} bytes).",
            analyzer="test-vision/v1",
            generated_at=datetime(2025, 7, 1, tzinfo=timezone.utc),
        )


def complete_test_attachment(
    issue_identifier: str,
    attachment_id: str,
    summary: str,
    *,
    generated_at: datetime | None = None,
) -> IssueAttachment:
    created_at = datetime(2025, 6, 25, 8, 0, tzinfo=timezone.utc)
    return IssueAttachment(
        id=attachment_id,
        filename=f"{attachment_id}.png",
        mime_type="image/png",
        author="Designer",
        created_at=created_at,
        source=RequirementSource(
            issue_identifier=issue_identifier,
            source_type="attachment",
            source_id=f"attachment:{attachment_id}",
            url=f"https://jira.example.test/secure/attachment/{attachment_id}",
            author="Designer",
            timestamp=created_at,
            authority="supporting_evidence",
        ),
        analysis=AttachmentAnalysis(
            status="complete",
            modality="vision",
            summary=summary,
            analyzer="test-vision/v1",
            generated_at=generated_at,
        ),
    )


class JiraModelTests(unittest.TestCase):
    def test_normalize_issue_payload(self) -> None:
        issue = normalize_issue(sample_issue_payload(), "https://jira.example.test")

        self.assertEqual(issue.identifier, "T-1")
        self.assertEqual(issue.title, "Fix thing")
        self.assertEqual(issue.description, "Description")
        self.assertEqual(issue.labels, ["codex-ready", "backend"])
        self.assertEqual(issue.comments[0].author, "Lin")
        self.assertEqual(issue.url, "https://jira.example.test/browse/T-1")
        self.assertIsNotNone(issue.requirements_snapshot)
        assert issue.requirements_snapshot is not None
        self.assertEqual(issue.requirements_snapshot.schema_version, "jira-requirements/v4")
        self.assertEqual(len(issue.requirements_snapshot.content_hash), 64)

    def test_stored_v1_snapshot_remains_readable(self) -> None:
        snapshot = RequirementsSnapshot.model_validate(
            {
                "schema_version": "jira-requirements/v1",
                "issue_id": "10001",
                "issue_identifier": "T-1",
                "issue_url": "https://jira.example.test/browse/T-1",
            }
        )

        self.assertEqual(snapshot.schema_version, "jira-requirements/v1")
        self.assertEqual(snapshot.issue_identifier, "T-1")

    def test_pre_v4_snapshots_keep_historical_context_hash_semantics(self) -> None:
        original = RequirementsSnapshot.model_validate(
            {
                "schema_version": "jira-requirements/v3",
                "issue_id": "10001",
                "issue_identifier": "T-1",
                "issue_url": "https://jira.example.test/browse/T-1",
                "components": [{"id": "1", "name": "Reports", "kind": "component"}],
            }
        )
        changed = original.model_copy(
            update={
                "components": [
                    original.components[0].model_copy(update={"name": "Projects"})
                ]
            }
        )

        self.assertNotEqual(
            original.calculate_content_hash(),
            changed.calculate_content_hash(),
        )
        changed_url = original.model_copy(
            update={"issue_url": "https://moved-jira.example.test/browse/T-1"}
        )
        self.assertNotEqual(
            original.calculate_content_hash(),
            changed_url.calculate_content_hash(),
        )
        historical_content = original.canonical_content()
        self.assertNotIn("context_warnings", historical_content)

    def test_jira_client_search_get_and_comment_with_fake_server(self) -> None:
        posted_comments: list[dict] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/search":
                return httpx.Response(200, json={"issues": [sample_issue_payload()]})
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=sample_issue_payload())
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                if request.method == "GET":
                    return httpx.Response(
                        200,
                        json={
                            "startAt": 0,
                            "maxResults": 100,
                            "total": 1,
                            "comments": sample_issue_payload()["fields"]["comment"]["comments"],
                        },
                    )
                posted_comments.append(json.loads(request.content.decode()))
                return httpx.Response(201, json={"id": "comment-1"})
            return httpx.Response(404, json={"error": "not found"})

        async def run() -> None:
            config = TrackerConfig(base_url="https://jira.example.test", jql="project = T")
            client = JiraClient(config, environ={"JIRA_TOKEN": "token"}, transport=httpx.MockTransport(handler))
            try:
                found = await client.search_issues("project = T", limit=10)
                issue = await client.get_issue("T-1")
                await client.add_comment("T-1", "done")
            finally:
                await client.close()
            self.assertEqual(found[0].identifier, "T-1")
            self.assertIsNotNone(found[0].requirements_snapshot)
            self.assertEqual(issue.identifier, "T-1")
            self.assertEqual(posted_comments, [{"body": "done"}])

        asyncio.run(run())

    def test_search_hydration_is_chunked_and_preserves_order(self) -> None:
        payloads: dict[str, dict] = {}
        for index in range(1, 6):
            payload = sample_issue_payload()
            payload["id"] = str(10_000 + index)
            payload["key"] = f"T-{index}"
            payload["fields"]["summary"] = f"Issue {index}"
            payloads[payload["key"]] = payload

        active_issue_requests = 0
        max_active_issue_requests = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active_issue_requests, max_active_issue_requests
            if request.url.path == "/rest/api/2/search":
                return httpx.Response(200, json={"issues": list(payloads.values())})

            issue_match = re.fullmatch(r"/rest/api/2/issue/(T-[1-5])", request.url.path)
            if issue_match:
                active_issue_requests += 1
                max_active_issue_requests = max(
                    max_active_issue_requests,
                    active_issue_requests,
                )
                try:
                    await asyncio.sleep(0.01)
                    return httpx.Response(200, json=payloads[issue_match.group(1)])
                finally:
                    active_issue_requests -= 1

            if re.fullmatch(r"/rest/api/2/issue/T-[1-5]/comment", request.url.path):
                return httpx.Response(
                    200,
                    json={
                        "startAt": 0,
                        "maxResults": 100,
                        "total": 0,
                        "comments": [],
                    },
                )
            return httpx.Response(404)

        async def run() -> None:
            config = TrackerConfig(
                base_url="https://jira.example.test",
                jql="project = T",
                requirements={"related_issue_hydration_max_concurrency": 2},
            )
            client = JiraClient(
                config,
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                found = await client.search_issues("project = T", limit=10)
            finally:
                await client.close()

            self.assertEqual([issue.identifier for issue in found], list(payloads))
            self.assertEqual(max_active_issue_requests, 2)

        asyncio.run(run())

    def test_comments_are_explicitly_paginated_and_symphony_status_is_filtered(self) -> None:
        requested_offsets: list[int] = []
        product_comments = [
            {
                "id": "1",
                "author": {"displayName": "Product Owner"},
                "body": "GC users see the column.",
                "created": "2025-06-10T10:00:00.000+0000",
            },
            {
                "id": "2",
                "author": {"displayName": "Symphony"},
                "body": "Codex run completed for T-1.\n\nStatus: completed",
                "created": "2025-06-11T10:00:00.000+0000",
            },
            {
                "id": "3",
                "author": {"displayName": "Product Owner"},
                "body": "GC acting as Sub follows Sub behavior.",
                "created": "2025-06-25T10:00:00.000+0000",
            },
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=sample_issue_payload())
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                start_at = int(request.url.params["startAt"])
                requested_offsets.append(start_at)
                return httpx.Response(
                    200,
                    json={
                        "startAt": start_at,
                        "maxResults": 2,
                        "total": 3,
                        "comments": product_comments[start_at : start_at + 2],
                    },
                )
            return httpx.Response(404)

        async def run() -> None:
            config = TrackerConfig(
                base_url="https://jira.example.test",
                jql="project = T",
                requirements={"comment_page_size": 2},
            )
            client = JiraClient(config, environ={"JIRA_TOKEN": "token"}, transport=httpx.MockTransport(handler))
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            self.assertEqual(requested_offsets, [0, 2])
            self.assertEqual([comment.id for comment in issue.comments], ["1", "3"])
            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            self.assertEqual([item.artifact_id for item in snapshot.comments], ["comment:1", "comment:3"])
            decision = next(item for item in snapshot.current_requirements if item.id.endswith("comment:3"))
            self.assertEqual(decision.sources[0].author, "Product Owner")
            self.assertEqual(decision.sources[0].authority, "product")
            self.assertEqual(
                decision.sources[0].timestamp,
                datetime(2025, 6, 25, 10, 0, tzinfo=timezone.utc),
            )

        asyncio.run(run())

    def test_declared_comment_total_truncation_blocks_snapshot(self) -> None:
        comment = {
            "id": "301",
            "author": {"displayName": "Product Owner"},
            "body": "GC users see the Cost column.",
            "created": "2025-06-10T10:00:00.000+0000",
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=sample_issue_payload())
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(
                    200,
                    json={
                        "startAt": 0,
                        "total": 2,
                        "isLast": True,
                        "comments": [comment],
                    },
                )
            return httpx.Response(404)

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(base_url="https://jira.example.test", jql="project = T"),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            expected = (
                "Jira comments for T-1 declared 2 comments but only 1 were available; "
                "comment requirements are incomplete."
            )
            self.assertEqual(issue.provenance_incomplete_reasons, [expected])
            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            self.assertFalse(snapshot.complete)
            self.assertIn(expected, snapshot.incomplete_reasons)
            self.assertEqual([item.id for item in issue.comments], ["301"])

        asyncio.run(run())

    def test_comment_pages_reject_invalid_or_jumped_start_offsets_before_items(self) -> None:
        comment = {
            "id": "offset-comment",
            "author": {"displayName": "Product Owner"},
            "body": "This item must not be accepted from a mismatched page.",
            "created": "2025-06-25T10:00:00.000+0000",
        }
        cases = {
            "negative": (
                -1,
                "returned an invalid startAt for requested offset 0",
            ),
            "jump": (
                4,
                "returned startAt 4 for requested offset 0",
            ),
            "string": (
                "0",
                "returned an invalid startAt for requested offset 0",
            ),
        }

        async def run_case(returned_start: object, expected_detail: str) -> None:
            async def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "startAt": returned_start,
                        "total": 1,
                        "comments": [comment],
                    },
                )

            client = JiraClient(
                TrackerConfig(base_url="https://jira.example.test", jql="project = T"),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                comments, reasons = await client._get_all_comments("T-1")
            finally:
                await client.close()

            self.assertEqual(comments, [])
            self.assertEqual(
                reasons,
                [
                    f"Jira comments for T-1 {expected_detail}; "
                    "comment requirements are incomplete."
                ],
            )

        for label, (returned_start, expected_detail) in cases.items():
            with self.subTest(label=label):
                asyncio.run(run_case(returned_start, expected_detail))

    def test_comment_authority_can_match_display_name_or_email_case_insensitively(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "201",
                "author": {
                    "displayName": "Product Owner",
                    "emailAddress": "OWNER@EXAMPLE.TEST",
                },
                "body": "GC users see the role column.",
                "created": "2025-06-25T10:00:00.000+0000",
            },
            {
                "id": "202",
                "author": {"displayName": "eNgInEeR"},
                "body": "Implementation note only.",
                "created": "2025-06-25T11:00:00.000+0000",
            },
            {
                "id": "203",
                "author": {"displayName": "Unmapped Reviewer"},
                "body": "Review note.",
                "created": "2025-06-25T12:00:00.000+0000",
            },
        ]
        issue = normalize_issue(
            payload,
            "https://jira.example.test",
            requirements_config=JiraRequirementsConfig(
                comment_authority="product",
                comment_authority_by_author={
                    "owner@example.test": "product_owner",
                    "Engineer": "engineering_context",
                },
            ),
        )

        self.assertEqual(
            [comment.source.authority for comment in issue.comments if comment.source],
            ["product_owner", "engineering_context", "product"],
        )
        snapshot = issue.requirements_snapshot
        assert snapshot is not None
        authorities = {
            decision.id: decision.sources[0].authority
            for decision in snapshot.current_requirements
        }
        self.assertEqual(authorities["jira:T-1:comment:201"], "product_owner")
        self.assertEqual(authorities["jira:T-1:comment:202"], "engineering_context")
        self.assertEqual(authorities["jira:T-1:comment:203"], "product")

    def test_edited_comment_uses_last_editor_for_current_body_provenance(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "204",
                "author": {
                    "displayName": "Engineer",
                    "emailAddress": "engineer@example.test",
                },
                "updateAuthor": {
                    "displayName": "Product Owner",
                    "emailAddress": "owner@example.test",
                },
                "body": "Use the Product Owner's revised role behavior.",
                "created": "2025-06-10T10:00:00.000+0000",
                "updated": "2025-06-25T12:30:00.000+0000",
            }
        ]
        issue = normalize_issue(
            payload,
            "https://jira.example.test",
            requirements_config=JiraRequirementsConfig(
                comment_authority="engineering_context",
                comment_authority_by_author={
                    "engineer@example.test": "engineering_context",
                    "owner@example.test": "product",
                },
            ),
        )

        comment = issue.comments[0]
        self.assertEqual(comment.author, "Product Owner")
        assert comment.source is not None
        self.assertEqual(comment.source.author, "Product Owner")
        self.assertEqual(comment.source.authority, "product")
        self.assertEqual(
            comment.source.timestamp,
            datetime(2025, 6, 25, 12, 30, tzinfo=timezone.utc),
        )
        snapshot = issue.requirements_snapshot
        assert snapshot is not None
        decision = next(
            item for item in snapshot.current_requirements if item.id == "jira:T-1:comment:204"
        )
        self.assertEqual(decision.sources[0], comment.source)

    def test_declared_partial_root_changelog_is_explicitly_paginated(self) -> None:
        payload = sample_issue_payload()
        payload["changelog"] = {
            "startAt": 0,
            "maxResults": 1,
            "total": 2,
            "histories": [
                {
                    "id": "h1",
                    "author": {"displayName": "Initial Product Owner"},
                    "created": "2025-06-10T09:00:00.000+0000",
                    "items": [{"fieldId": "description", "field": "Description"}],
                }
            ],
        }
        changelog_offsets: list[int] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=payload)
            if request.url.path == "/rest/api/2/issue/T-1/changelog":
                changelog_offsets.append(int(request.url.params["startAt"]))
                return httpx.Response(
                    200,
                    json={
                        "startAt": 1,
                        "total": 2,
                        "values": [
                            {
                                "id": "h2",
                                "author": {"displayName": "Final Product Owner"},
                                "created": "2025-06-25T10:00:00.000+0000",
                                "items": [
                                    {
                                        "fieldId": "description",
                                        "field": "Description",
                                    }
                                ],
                            }
                        ],
                    },
                )
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(200, json={"startAt": 0, "total": 0, "comments": []})
            return httpx.Response(404)

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(base_url="https://jira.example.test", jql="project = T"),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            self.assertEqual(changelog_offsets, [1])
            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            assert snapshot.description is not None
            self.assertEqual(snapshot.description.source.author, "Final Product Owner")
            self.assertEqual(
                snapshot.description.source.timestamp,
                datetime(2025, 6, 25, 10, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(issue.provenance_incomplete_reasons, [])
            self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)

        asyncio.run(run())

    def test_changelog_pages_reject_negative_or_jumped_start_offsets_before_histories(self) -> None:
        cases = {
            "negative": (
                -1,
                "returned an invalid startAt for requested offset 1",
            ),
            "jump": (
                9,
                "returned startAt 9 for requested offset 1",
            ),
        }

        async def run_case(returned_start: int, expected_detail: str) -> None:
            payload = sample_issue_payload()
            payload["changelog"] = {
                "startAt": 0,
                "total": 2,
                "histories": [
                    {
                        "id": "h1",
                        "author": {"displayName": "Initial Product Owner"},
                        "created": "2025-06-10T09:00:00.000+0000",
                        "items": [
                            {
                                "fieldId": "description",
                                "field": "Description",
                                "toString": "Initial",
                            }
                        ],
                    }
                ],
            }
            rejected_history = {
                "id": "h2",
                "author": {"displayName": "Later Product Owner"},
                "created": "2025-06-25T10:00:00.000+0000",
                "items": [
                    {
                        "fieldId": "description",
                        "field": "Description",
                        "toString": "Rejected",
                    }
                ],
            }

            async def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "startAt": returned_start,
                        "total": 2,
                        "values": [rejected_history],
                    },
                )

            client = JiraClient(
                TrackerConfig(base_url="https://jira.example.test", jql="project = T"),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                completed, reasons = await client._complete_changelog("T-1", payload)
            finally:
                await client.close()

            histories = completed["changelog"]["histories"]
            self.assertEqual([history["id"] for history in histories], ["h1"])
            self.assertTrue(
                any(expected_detail in reason for reason in reasons),
                reasons,
            )

        for label, (returned_start, expected_detail) in cases.items():
            with self.subTest(label=label):
                asyncio.run(run_case(returned_start, expected_detail))

    def test_unavailable_declared_changelog_page_is_context_warning(self) -> None:
        payload = sample_issue_payload()
        payload["changelog"] = {
            "startAt": 0,
            "maxResults": 1,
            "total": 2,
            "histories": [
                {
                    "id": "h1",
                    "author": {"displayName": "Product Owner"},
                    "created": "2025-06-10T09:00:00.000+0000",
                    "items": [{"fieldId": "description", "field": "Description"}],
                }
            ],
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=payload)
            if request.url.path == "/rest/api/2/issue/T-1/changelog":
                return httpx.Response(503, json={"error": "unavailable"})
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(200, json={"startAt": 0, "total": 0, "comments": []})
            return httpx.Response(404)

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(base_url="https://jira.example.test", jql="project = T"),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            expected = (
                "Jira changelog for T-1 declared 2 histories but only 1 were available; "
                "field provenance is incomplete."
            )
            self.assertEqual(issue.provenance_incomplete_reasons, [])
            self.assertIn(expected, issue.context_warnings)
            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
            self.assertIn(expected, snapshot.context_warnings)
            assert snapshot.description is not None
            self.assertEqual(snapshot.description.source.author, "Product Owner")

        asyncio.run(run())

    def test_snapshot_includes_context_and_explicit_decision_categories(self) -> None:
        payload = sample_issue_payload()
        payload["names"] = {"customfield_100": "Acceptance Criteria"}
        payload["changelog"] = {
            "histories": [
                {
                    "author": {"displayName": "Product Lead"},
                    "created": "2025-06-25T09:30:00.000+0000",
                    "items": [{"fieldId": "customfield_100", "field": "Acceptance Criteria"}],
                }
            ]
        }
        payload["fields"].update(
            {
                "customfield_100": "AC-1: GC acting as Sub uses Sub behavior.",
                "parent": {
                    "id": "900",
                    "key": "T-EPIC",
                    "fields": {
                        "summary": "Role-aware report",
                        "status": {"name": "Development"},
                        "issuetype": {"name": "Epic"},
                    },
                },
                "subtasks": [
                    {
                        "id": "901",
                        "key": "T-CHILD",
                        "fields": {
                            "summary": "Phase 2",
                            "status": {"name": "Open"},
                            "issuetype": {"name": "Story"},
                        },
                    }
                ],
                "issuelinks": [
                    {
                        "id": "77",
                        "type": {"inward": "is blocked by", "outward": "blocks"},
                        "inwardIssue": {
                            "id": "902",
                            "key": "T-BLOCKER",
                            "fields": {
                                "summary": "API dependency",
                                "status": {"name": "Open"},
                                "issuetype": {"name": "Task"},
                            },
                        },
                    },
                    {
                        "id": "78",
                        "type": {"inward": "relates to", "outward": "relates to"},
                        "outwardIssue": {
                            "id": "903",
                            "key": "T-PHASE2",
                            "fields": {
                                "summary": "Phase 2 design",
                                "status": {"name": "Open"},
                                "issuetype": {"name": "Story"},
                            },
                        },
                    },
                ],
                "components": [{"id": "10", "name": "Reports"}],
                "versions": [{"id": "11", "name": "2025.2", "released": False}],
                "fixVersions": [{"id": "12", "name": "2025.3", "released": False}],
                "comment": {
                    "comments": [
                        {
                            "id": "210",
                            "author": {"displayName": "Product Lead"},
                            "body": "[supersedes: description] Use the revised role matrix.",
                            "created": "2025-06-25T10:00:00.000+0000",
                        },
                        {
                            "id": "211",
                            "author": {"displayName": "Engineer"},
                            "body": "[inferred] Existing Sub column placement is reusable.",
                            "created": "2025-06-25T11:00:00.000+0000",
                        },
                        {
                            "id": "212",
                            "author": {"displayName": "Product Lead"},
                            "body": "[contradiction] Two mockups disagree about column order.",
                            "created": "2025-06-25T12:00:00.000+0000",
                        },
                    ]
                },
            }
        )
        requirements = JiraRequirementsConfig(
            acceptance_criteria_fields=["customfield_100"],
            field_authority={"customfield_100": "product_owner"},
        )
        issue = normalize_issue(payload, "https://jira.example.test", requirements_config=requirements)
        snapshot = issue.requirements_snapshot
        assert snapshot is not None

        self.assertEqual(snapshot.parent.identifier, "T-EPIC")  # type: ignore[union-attr]
        self.assertEqual(
            snapshot.parent.source.url,  # type: ignore[union-attr]
            "https://jira.example.test/browse/T-EPIC",
        )
        self.assertEqual([item.identifier for item in snapshot.children], ["T-CHILD"])
        self.assertEqual(
            [item.identifier for item in snapshot.linked_issues],
            ["T-BLOCKER", "T-PHASE2"],
        )
        self.assertEqual([item.identifier for item in snapshot.dependencies], ["T-BLOCKER"])
        self.assertEqual([item.name for item in snapshot.components], ["Reports"])
        self.assertEqual(
            [(item.kind, item.name) for item in snapshot.versions],
            [("affects_version", "2025.2"), ("fix_version", "2025.3")],
        )
        acceptance = snapshot.custom_fields[0]
        self.assertEqual(acceptance.kind, "acceptance_criterion")
        self.assertEqual(acceptance.source.author, "Product Lead")
        self.assertEqual(acceptance.source.authority, "product_owner")
        self.assertTrue(
            any(item.id == "jira:T-1:description" for item in snapshot.superseded_requirements)
        )
        revised = next(item for item in snapshot.current_requirements if item.id.endswith("comment:210"))
        self.assertEqual(revised.supersedes, ["jira:T-1:description"])
        self.assertEqual(len(snapshot.inferred_behavior), 1)
        self.assertEqual(len(snapshot.unresolved_contradictions), 1)
        self.assertIn("T-BLOCKER", [item.identifier for item in issue.blocked_by])

    def test_normal_jira_prose_is_classified_conservatively_with_provenance(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"] = {
            "comments": [
                {
                    "id": "201",
                    "author": {"displayName": "Product Lead"},
                    "body": "Sub users keep the existing column placement.",
                    "created": "2025-06-25T09:00:00.000+0000",
                },
                {
                    "id": "202",
                    "author": {"displayName": "Engineer"},
                    "body": "This design replaces the previous requirement. Use the revised matrix.",
                    "created": "2025-06-25T10:00:00.000+0000",
                },
                {
                    "id": "203",
                    "author": {"displayName": "Product Lead"},
                    "body": "This requirement supersedes the prior decision. Keep Sub placement.",
                    "created": "2025-06-25T11:00:00.000+0000",
                },
                {
                    "id": "204",
                    "author": {"displayName": "Product Lead"},
                    "body": "This decision overrides previous design. GC-as-Sub follows Sub behavior.",
                    "created": "2025-06-25T12:00:00.000+0000",
                },
                {
                    "id": "205",
                    "author": {"displayName": "Engineer"},
                    "body": "Inferred behavior: reuse the existing Sub column component.",
                    "created": "2025-06-25T13:00:00.000+0000",
                },
                {
                    "id": "206",
                    "author": {"displayName": "Product Lead"},
                    "body": "There is an unresolved contradiction between the two role mockups.",
                    "created": "2025-06-25T14:00:00.000+0000",
                },
                {
                    "id": "207",
                    "author": {"displayName": "Product Lead"},
                    "body": "This requirement has been superseded.",
                    "created": "2025-06-25T15:00:00.000+0000",
                },
                {
                    "id": "208",
                    "author": {"displayName": "Engineer"},
                    "body": "This design does not supersede the previous design; it documents it.",
                    "created": "2025-06-25T16:00:00.000+0000",
                },
                {
                    "id": "209",
                    "author": {"displayName": "Engineer"},
                    "body": "There is no unresolved contradiction and no inferred behavior is asserted.",
                    "created": "2025-06-25T17:00:00.000+0000",
                },
            ]
        }
        issue = normalize_issue(payload, "https://jira.example.test")
        snapshot = issue.requirements_snapshot
        assert snapshot is not None
        decisions = {
            decision.id: decision
            for decision in (
                snapshot.current_requirements
                + snapshot.superseded_requirements
                + snapshot.inferred_behavior
                + snapshot.unresolved_contradictions
            )
        }

        self.assertEqual(decisions["jira:T-1:comment:201"].classification, "current")
        for comment_id in ("202", "203", "204"):
            comment_units = [
                decision
                for decision in decisions.values()
                if decision.sources[0].source_id.startswith(
                    f"comment:{comment_id}#unit:"
                )
            ]
            ambiguous = [
                decision
                for decision in comment_units
                if decision.classification == "unresolved_contradiction"
            ]
            self.assertEqual(len(ambiguous), 1)
            self.assertEqual(ambiguous[0].supersedes, [])
        comment_202 = next(
            decision
            for decision in decisions.values()
            if decision.sources[0].source_id.startswith("comment:202#unit:")
        )
        self.assertEqual(comment_202.sources[0].author, "Engineer")
        self.assertEqual(decisions["jira:T-1:comment:205"].classification, "inferred")
        self.assertEqual(
            decisions["jira:T-1:comment:206"].classification,
            "unresolved_contradiction",
        )
        self.assertEqual(decisions["jira:T-1:comment:207"].classification, "superseded")
        self.assertEqual(decisions["jira:T-1:comment:208"].classification, "current")
        self.assertEqual(decisions["jira:T-1:comment:208"].supersedes, [])
        self.assertEqual(decisions["jira:T-1:comment:209"].classification, "current")
        replacement_source = next(
            decision.sources[0]
            for decision in decisions.values()
            if decision.classification == "unresolved_contradiction"
            and decision.sources[0].source_id.startswith("comment:204#unit:")
        )
        self.assertEqual(replacement_source.author, "Product Lead")
        self.assertEqual(replacement_source.issue_identifier, "T-1")
        self.assertEqual(
            replacement_source.timestamp,
            datetime(2025, 6, 25, 12, 0, tzinfo=timezone.utc),
        )

    def test_mixed_comment_creates_stable_source_specific_decision_units(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "300",
                "author": {"displayName": "Product Owner"},
                "body": (
                    "- [classification: current] GC sees the Cost column.\n"
                    "- [superseded] The old export uses a legacy label.\n"
                    "- [inferred] The existing renderer may be reusable."
                ),
                "created": "2025-06-25T10:00:00.000+0000",
            }
        ]

        first = normalize_issue(payload, "https://jira.example.test").requirements_snapshot
        second = normalize_issue(payload, "https://jira.example.test").requirements_snapshot
        assert first is not None and second is not None
        first_decisions = (
            first.current_requirements
            + first.superseded_requirements
            + first.inferred_behavior
            + first.unresolved_contradictions
        )
        units = [
            decision
            for decision in first_decisions
            if decision.sources
            and decision.sources[0].source_id.startswith("comment:300#unit:")
        ]

        self.assertEqual(
            {decision.classification for decision in units},
            {"current", "superseded", "inferred"},
        )
        self.assertEqual(len(units), 3)
        self.assertTrue(all("#unit:" in decision.id for decision in units))
        self.assertTrue(
            all(
                decision.sources[0].location.startswith("decision-unit:")
                for decision in units
                if decision.sources[0].location
            )
        )
        self.assertEqual(first.comments[0].source.source_id, "comment:300")
        self.assertEqual(
            sorted(decision.id for decision in units),
            sorted(
                decision.id
                for decision in second.current_requirements
                + second.superseded_requirements
                + second.inferred_behavior
                if decision.sources[0].source_id.startswith("comment:300#unit:")
            ),
        )
        self.assertEqual(first.content_hash, second.content_hash)

    def test_lexical_polarity_and_order_reversals_do_not_manufacture_conflicts(self) -> None:
        cases = {
            "polarity": (
                "GC sees the Cost column.",
                "GC does not see the Cost column.",
            ),
            "order": (
                "Place the Cost column before Amount.",
                "Place the Cost column after Amount.",
            ),
        }
        for label, (earlier, later) in cases.items():
            with self.subTest(label=label):
                payload = sample_issue_payload()
                payload["fields"]["comment"]["comments"] = [
                    {
                        "id": "301",
                        "author": {"displayName": "Product Owner"},
                        "body": earlier,
                        "created": "2025-06-10T10:00:00.000+0000",
                    },
                    {
                        "id": "302",
                        "author": {"displayName": "Product Owner"},
                        "body": later,
                        "created": "2025-06-25T10:00:00.000+0000",
                    },
                ]
                snapshot = normalize_issue(
                    payload,
                    "https://jira.example.test",
                ).requirements_snapshot
                assert snapshot is not None

                self.assertEqual(snapshot.unresolved_contradictions, [])
                self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
                current_sources = {
                    source.source_id
                    for decision in snapshot.current_requirements
                    for source in decision.sources
                }
                self.assertTrue({"comment:301", "comment:302"} <= current_sources)

    def test_opposing_current_units_require_explicit_contradiction_marker(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "303",
                "author": {"displayName": "Product Owner"},
                "body": (
                    "- [classification: current] GC sees the Cost column.\n"
                    "- [classification: current] GC does not see the Cost column."
                ),
                "created": "2025-06-25T10:00:00.000+0000",
            }
        ]
        snapshot = normalize_issue(
            payload,
            "https://jira.example.test",
        ).requirements_snapshot
        assert snapshot is not None

        self.assertEqual(snapshot.unresolved_contradictions, [])
        units = [
            decision
            for decision in snapshot.current_requirements
            if decision.sources[0].source_id.startswith("comment:303#unit:")
        ]
        self.assertEqual(len(units), 2)
        self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)

    def test_exact_supersession_resolves_otherwise_clear_conflict(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "301",
                "author": {"displayName": "Product Owner"},
                "body": "GC sees the Cost column.",
                "created": "2025-06-10T10:00:00.000+0000",
            },
            {
                "id": "302",
                "author": {"displayName": "Product Owner"},
                "body": (
                    "[supersedes: jira:T-1:comment:301] "
                    "GC does not see the Cost column."
                ),
                "created": "2025-06-25T10:00:00.000+0000",
            },
        ]
        snapshot = normalize_issue(
            payload,
            "https://jira.example.test",
        ).requirements_snapshot
        assert snapshot is not None

        replacement = next(
            decision
            for decision in snapshot.current_requirements
            if decision.id == "jira:T-1:comment:302"
        )
        target = next(
            decision
            for decision in snapshot.superseded_requirements
            if decision.id == "jira:T-1:comment:301"
        )
        self.assertEqual(replacement.supersedes, [target.id])
        self.assertEqual(target.superseded_by, [replacement.id])
        self.assertFalse(
            any(
                decision.id.startswith("jira:conflict:")
                for decision in snapshot.unresolved_contradictions
            )
        )

    def test_lower_or_unranked_authority_cannot_supersede_target(self) -> None:
        cases = {
            "lower": ("engineering_context", "product_owner", "ranks below"),
        }
        for label, (source_authority, target_authority, expected_reason) in cases.items():
            with self.subTest(label=label):
                payload = sample_issue_payload()
                payload["fields"]["comment"]["comments"] = [
                    {
                        "id": "350",
                        "author": {"displayName": "Reviewer"},
                        "body": "[supersedes: description] Use a different behavior.",
                        "created": "2025-06-25T10:00:00.000+0000",
                    }
                ]
                snapshot = normalize_issue(
                    payload,
                    "https://jira.example.test",
                    requirements_config=JiraRequirementsConfig(
                        description_authority=target_authority,
                        comment_authority=source_authority,
                    ),
                ).requirements_snapshot
                assert snapshot is not None

                self.assertTrue(
                    any(
                        decision.id == "jira:T-1:description"
                        for decision in snapshot.current_requirements
                    )
                )
                override = next(
                    decision
                    for decision in snapshot.unresolved_contradictions
                    if decision.id == "jira:T-1:comment:350"
                )
                self.assertEqual(
                    {source.source_id for source in override.sources},
                    {"description", "comment:350"},
                )
                self.assertEqual(override.supersedes, [])
                self.assertTrue(
                    any(expected_reason in reason for reason in snapshot.incomplete_reasons)
                )

    def test_configured_custom_authority_rank_can_authorize_supersession(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "351",
                "author": {"displayName": "Legal Approver"},
                "body": "[supersedes: description] Use the approved legal behavior.",
                "created": "2025-06-25T10:00:00.000+0000",
            }
        ]
        snapshot = normalize_issue(
            payload,
            "https://jira.example.test",
            requirements_config=JiraRequirementsConfig(
                description_authority="product",
                comment_authority="legal_custom",
                authority_rank={"product": 30, "legal_custom": 50},
            ),
        ).requirements_snapshot
        assert snapshot is not None

        replacement = next(
            decision
            for decision in snapshot.current_requirements
            if decision.id == "jira:T-1:comment:351"
        )
        self.assertEqual(replacement.supersedes, ["jira:T-1:description"])
        self.assertTrue(
            any(
                decision.id == "jira:T-1:description"
                for decision in snapshot.superseded_requirements
            )
        )

    def test_supersession_cycle_is_unresolved_and_cites_cycle_sources(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "401",
                "author": {"displayName": "Product Owner"},
                "body": "[supersedes: jira:T-1:comment:402] Choose behavior A.",
                "created": "2025-06-25T10:00:00.000+0000",
            },
            {
                "id": "402",
                "author": {"displayName": "Product Owner"},
                "body": "[supersedes: jira:T-1:comment:401] Choose behavior B.",
                "created": "2025-06-25T11:00:00.000+0000",
            },
        ]
        snapshot = normalize_issue(
            payload,
            "https://jira.example.test",
        ).requirements_snapshot
        assert snapshot is not None

        cycle = {
            decision.id: decision
            for decision in snapshot.unresolved_contradictions
            if decision.id in {"jira:T-1:comment:401", "jira:T-1:comment:402"}
        }
        self.assertEqual(set(cycle), {"jira:T-1:comment:401", "jira:T-1:comment:402"})
        for decision in cycle.values():
            self.assertEqual(
                {source.source_id for source in decision.sources},
                {"comment:401", "comment:402"},
            )
            self.assertEqual(decision.supersedes, [])
        self.assertTrue(
            any(
                "Supersession cycle detected" in reason
                for reason in snapshot.incomplete_reasons
            )
        )

    def test_source_level_supersession_of_multi_unit_artifact_is_ambiguous(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "410",
                "author": {"displayName": "Product Owner"},
                "body": "- Keep behavior A.\n- Keep behavior B.",
                "created": "2025-06-10T10:00:00.000+0000",
            },
            {
                "id": "411",
                "author": {"displayName": "Product Owner"},
                "body": "[supersedes: comment:410] Replace one behavior.",
                "created": "2025-06-25T10:00:00.000+0000",
            },
        ]
        snapshot = normalize_issue(
            payload,
            "https://jira.example.test",
        ).requirements_snapshot
        assert snapshot is not None

        ambiguous = next(
            decision
            for decision in snapshot.unresolved_contradictions
            if decision.id == "jira:T-1:comment:411"
        )
        self.assertEqual(ambiguous.supersedes, [])
        self.assertTrue(
            any(
                "ambiguous superseded requirement comment:410" in reason
                and "exact decision-unit ID" in reason
                for reason in snapshot.incomplete_reasons
            )
        )

    def test_previous_prose_reference_is_unresolved_without_stable_id(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "201",
                "author": {"displayName": "Product Lead"},
                "body": "This decision replaces the previous requirement.",
                "created": "2025-06-25T10:00:00.000+0000",
            }
        ]
        snapshot = normalize_issue(payload, "https://jira.example.test").requirements_snapshot
        assert snapshot is not None
        decision = next(
            item
            for item in snapshot.unresolved_contradictions
            if item.id.endswith("comment:201")
        )
        self.assertEqual(decision.supersedes, [])
        self.assertTrue(
            any(decision.id in reason for reason in snapshot.incomplete_reasons)
        )
        self.assertTrue(any(item.id == "jira:T-1:description" for item in snapshot.current_requirements))

    def test_related_issue_content_is_context_not_conflict_input(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["comment"]["comments"] = [
            {
                "id": "420",
                "author": {"displayName": "Product Owner"},
                "body": "GC sees the Cost column.",
                "created": "2025-06-10T10:00:00.000+0000",
            }
        ]
        issue = normalize_issue(payload, "https://jira.example.test")
        related = RelatedIssue(
            id="904",
            identifier="T-PHASE2",
            title="Phase 2",
            relation="relates to",
            direction="outward",
            source=RequirementSource(
                issue_identifier="T-1",
                source_type="relation",
                source_id="relation:78:T-PHASE2",
                author="Product Owner",
                timestamp=datetime(2025, 6, 25, 9, 0, tzinfo=timezone.utc),
                authority="context",
            ),
        )
        related_payload = {
            "id": "904",
            "key": "T-PHASE2",
            "names": {},
            "fields": {
                "summary": "Phase 2",
                "description": "GC does not see the Cost column.",
                "status": {"name": "Open"},
                "issuetype": {"name": "Story"},
                "reporter": {"displayName": "Product Owner"},
                "created": "2025-06-25T09:00:00.000+0000",
                "attachment": [],
            },
        }
        issue.linked_issues = [
            hydrate_related_issue_context(
                related,
                related_payload,
                "https://jira.example.test",
                JiraRequirementsConfig(),
                comments=[],
                attachments=[],
            )
        ]

        snapshot = build_requirements_snapshot(
            issue,
            payload,
            JiraRequirementsConfig(),
        )
        self.assertEqual(snapshot.unresolved_contradictions, [])
        self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
        self.assertTrue(
            any(
                decision.id == "jira:T-1:comment:420"
                for decision in snapshot.current_requirements
            )
        )
        self.assertFalse(
            any(
                source.issue_identifier == "T-PHASE2"
                for decision in snapshot.current_requirements
                for source in decision.sources
            )
        )
        self.assertEqual(
            snapshot.linked_issues[0].requirements[0].text,
            "GC does not see the Cost column.",
        )

    def test_linked_issue_requirement_context_is_hydrated_once_without_recursion(self) -> None:
        payload = sample_issue_payload()
        payload["names"] = {"customfield_100": "Acceptance Criteria"}
        payload["fields"]["customfield_100"] = "AC-ROOT: Phase 2 evidence remains in scope."
        payload["fields"]["issuelinks"] = [
            {
                "id": "78",
                "type": {"inward": "relates to", "outward": "relates to"},
                "outwardIssue": {
                    "id": "903",
                    "key": "T-PHASE2",
                    "fields": {
                        "summary": "Phase 2 design",
                        "status": {"name": "Open"},
                        "issuetype": {"name": "Story"},
                    },
                },
            }
        ]
        phase_two = {
            "id": "903",
            "key": "T-PHASE2",
            "names": {"customfield_100": "Acceptance Criteria"},
            "changelog": {
                "startAt": 0,
                "maxResults": 1,
                "total": 2,
                "histories": [
                    {
                        "id": "p1",
                        "author": {"displayName": "Phase Designer"},
                        "created": "2025-06-24T09:00:00.000+0000",
                        "items": [
                            {"fieldId": "description", "field": "Description"}
                        ],
                    }
                ],
            },
            "fields": {
                "summary": "Phase 2 design",
                "description": "Add the role-specific column placement.",
                "status": {"name": "Open"},
                "issuetype": {"name": "Story"},
                "reporter": {"displayName": "Product Lead"},
                "creator": {"displayName": "Product Lead"},
                "created": "2025-06-25T09:00:00.000+0000",
                "customfield_100": "AC-P2: GC-as-Sub follows Sub placement.",
                "attachment": [
                    {
                        "id": "550",
                        "filename": "phase-two-matrix.png",
                        "mimeType": "image/png",
                        "size": 8,
                        "content": "https://jira.example.test/secure/attachment/550/phase-two-matrix.png",
                        "author": {"displayName": "Designer"},
                        "created": "2025-06-25T09:15:00.000+0000",
                    }
                ],
                # This relation must not be traversed during one-level hydration.
                "issuelinks": [{"outwardIssue": {"key": "T-PHASE3"}}],
            },
        }
        related_fetches: list[str] = []
        related_changelog_offsets: list[int] = []
        related_comment_offsets: list[int] = []
        related_comments = [
            {
                "id": "310",
                "author": {"displayName": "Product Lead"},
                "body": "Sub users keep the existing column placement.",
                "created": "2025-06-25T10:00:00.000+0000",
            },
            {
                "id": "311",
                "author": {"displayName": "Symphony"},
                "body": "Codex run completed for T-2.\n\nStatus: completed",
                "created": "2025-06-25T10:30:00.000+0000",
            },
            {
                "id": "312",
                "author": {"displayName": "Product Lead"},
                "body": "[supersedes: comment:310] GC-as-Sub uses Sub placement.",
                "created": "2025-06-25T11:00:00.000+0000",
            },
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=payload)
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(200, json={"startAt": 0, "total": 0, "comments": []})
            if request.url.path == "/rest/api/2/issue/T-PHASE2":
                related_fetches.append(request.url.path)
                return httpx.Response(200, json=phase_two)
            if request.url.path == "/rest/api/2/issue/T-PHASE2/changelog":
                related_changelog_offsets.append(int(request.url.params["startAt"]))
                return httpx.Response(
                    200,
                    json={
                        "startAt": 1,
                        "total": 2,
                        "values": [
                            {
                                "id": "p2",
                                "author": {"displayName": "Phase Product Approver"},
                                "created": "2025-06-25T09:30:00.000+0000",
                                "items": [
                                    {
                                        "fieldId": "customfield_100",
                                        "field": "Acceptance Criteria",
                                    }
                                ],
                            }
                        ],
                    },
                )
            if request.url.path == "/rest/api/2/issue/T-PHASE2/comment":
                start_at = int(request.url.params["startAt"])
                related_comment_offsets.append(start_at)
                return httpx.Response(
                    200,
                    json={
                        "startAt": start_at,
                        "total": len(related_comments),
                        "comments": related_comments[start_at : start_at + 2],
                    },
                )
            if request.url.path == "/secure/attachment/550/phase-two-matrix.png":
                return httpx.Response(200, content=b"mock-png")
            return httpx.Response(404)

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(
                    base_url="https://jira.example.test",
                    jql="project = T",
                    requirements={
                        "acceptance_criteria_fields": ["customfield_100"],
                        "comment_page_size": 2,
                    },
                ),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
                attachment_analyzer=FakeVisionAnalyzer(),
            )
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            self.assertEqual(related_fetches, ["/rest/api/2/issue/T-PHASE2"])
            self.assertEqual(related_changelog_offsets, [1])
            self.assertEqual(related_comment_offsets, [0, 2])
            linked = issue.linked_issues[0]
            self.assertEqual(linked.url, "https://jira.example.test/browse/T-PHASE2")
            self.assertEqual(linked.source.url, linked.url)
            self.assertIsNone(linked.source.timestamp)
            self.assertEqual(
                linked.description,
                "Add the role-specific column placement.",
            )
            self.assertEqual([comment.id for comment in linked.comments], ["310", "312"])
            self.assertEqual(
                [artifact.artifact_id for artifact in linked.requirements],
                [
                    "description",
                    "field:customfield_100",
                    "comment:310",
                    "comment:312",
                ],
            )
            self.assertEqual(linked.requirements[1].kind, "acceptance_criterion")
            self.assertEqual(
                linked.requirements[1].source.author,
                "Phase Product Approver",
            )
            self.assertEqual(linked.provenance_incomplete_reasons, [])
            self.assertEqual(len(linked.attachments), 1)
            self.assertEqual(linked.attachments[0].analysis.status, "not_configured")
            self.assertEqual(
                linked.attachments[0].source.issue_identifier,
                "T-PHASE2",
            )
            assert issue.requirements_snapshot is not None
            self.assertEqual(
                issue.requirements_snapshot.linked_issues[0].requirements,
                linked.requirements,
            )
            self.assertEqual(
                issue.requirements_snapshot.linked_issues[0].attachments,
                linked.attachments,
            )
            related_decision_ids = [
                decision.id
                for decision in issue.requirements_snapshot.current_requirements
                if decision.sources[0].issue_identifier == "T-PHASE2"
            ]
            self.assertEqual(related_decision_ids, [])
            self.assertFalse(
                any(
                    decision.sources[0].issue_identifier == "T-PHASE2"
                    for decision in issue.requirements_snapshot.superseded_requirements
                )
            )
            self.assertTrue(
                issue.requirements_snapshot.complete,
                issue.requirements_snapshot.incomplete_reasons,
            )

            incomplete_issue = issue.model_copy(deep=True)
            incomplete_issue.linked_issues[0].attachments[0].analysis = AttachmentAnalysis(
                status="skipped",
                modality="metadata",
                summary="Analysis intentionally skipped for regression coverage.",
            )
            incomplete_snapshot = build_requirements_snapshot(
                incomplete_issue,
                payload,
                client.config.requirements,
            )
            self.assertTrue(
                incomplete_snapshot.complete,
                incomplete_snapshot.incomplete_reasons,
            )
            self.assertEqual(incomplete_snapshot.incomplete_reasons, [])

        asyncio.run(run())

    def test_related_missing_configured_field_warns_but_does_not_block(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["customfield_200"] = None
        payload["fields"]["issuelinks"] = [
            {
                "id": "missing",
                "type": {"outward": "relates to"},
                "outwardIssue": {
                    "id": "902",
                    "key": "T-MISSING",
                    "fields": {
                        "summary": "Missing configured field",
                        "status": {"name": "Open"},
                        "issuetype": {"name": "Story"},
                    },
                },
            },
            {
                "id": "null",
                "type": {"outward": "relates to"},
                "outwardIssue": {
                    "id": "903",
                    "key": "T-NULL",
                    "fields": {
                        "summary": "Null configured field",
                        "status": {"name": "Open"},
                        "issuetype": {"name": "Story"},
                    },
                },
            },
        ]

        def related_payload(key: str, *, include_null: bool) -> dict:
            fields = {
                "summary": f"Related {key}",
                "description": "Related requirement context.",
                "status": {"name": "Open"},
                "issuetype": {"name": "Story"},
                "reporter": {"displayName": "Product Owner"},
                "creator": {"displayName": "Product Owner"},
                "created": "2025-06-25T09:00:00.000+0000",
                "attachment": [],
            }
            if include_null:
                fields["customfield_200"] = None
            return {
                "id": "902" if key == "T-MISSING" else "903",
                "key": key,
                "names": {"customfield_200": "Design decision"},
                "fields": fields,
                "changelog": {"startAt": 0, "total": 0, "histories": []},
            }

        related_payloads = {
            "T-MISSING": related_payload("T-MISSING", include_null=False),
            "T-NULL": related_payload("T-NULL", include_null=True),
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=payload)
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(200, json={"startAt": 0, "total": 0, "comments": []})
            match = re.fullmatch(r"/rest/api/2/issue/(T-MISSING|T-NULL)", request.url.path)
            if match:
                return httpx.Response(200, json=related_payloads[match.group(1)])
            if re.fullmatch(
                r"/rest/api/2/issue/(T-MISSING|T-NULL)/comment",
                request.url.path,
            ):
                return httpx.Response(200, json={"startAt": 0, "total": 0, "comments": []})
            return httpx.Response(404)

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(
                    base_url="https://jira.example.test",
                    jql="project = T",
                    requirements={"custom_fields": ["customfield_200"]},
                ),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            linked = {item.identifier: item for item in issue.linked_issues}
            expected = (
                "Configured Jira field customfield_200 was not returned for related issue "
                "T-MISSING."
            )
            self.assertEqual(linked["T-MISSING"].provenance_incomplete_reasons, [expected])
            self.assertEqual(linked["T-NULL"].provenance_incomplete_reasons, [])
            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
            self.assertNotIn(expected, snapshot.incomplete_reasons)
            self.assertIn(expected, snapshot.context_warnings)

        asyncio.run(run())

    def test_related_comment_fetch_failure_warns_without_losing_context(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["issuelinks"] = [
            {
                "id": "failed-comments",
                "type": {"outward": "relates to"},
                "outwardIssue": {
                    "id": "904",
                    "key": "T-COMMENTS",
                    "fields": {
                        "summary": "Related comments",
                        "status": {"name": "Open"},
                        "issuetype": {"name": "Story"},
                    },
                },
            }
        ]
        related_payload = {
            "id": "904",
            "key": "T-COMMENTS",
            "names": {},
            "fields": {
                "summary": "Related comments",
                "description": "Keep this related requirement context.",
                "status": {"name": "Open"},
                "issuetype": {"name": "Story"},
                "reporter": {"displayName": "Product Owner"},
                "creator": {"displayName": "Product Owner"},
                "created": "2025-06-25T09:00:00.000+0000",
                "attachment": [],
            },
            "changelog": {"startAt": 0, "total": 0, "histories": []},
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=payload)
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(200, json={"startAt": 0, "total": 0, "comments": []})
            if request.url.path == "/rest/api/2/issue/T-COMMENTS":
                return httpx.Response(200, json=related_payload)
            if request.url.path == "/rest/api/2/issue/T-COMMENTS/comment":
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(404)

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(base_url="https://jira.example.test", jql="project = T"),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            linked = issue.linked_issues[0]
            expected = (
                "Jira comments for T-COMMENTS could not be completely fetched; "
                "comment requirements are incomplete."
            )
            self.assertIsNone(linked.hydration_error)
            self.assertEqual(linked.description, "Keep this related requirement context.")
            self.assertEqual(linked.provenance_incomplete_reasons, [expected])
            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
            self.assertNotIn(expected, snapshot.incomplete_reasons)
            self.assertIn(expected, snapshot.context_warnings)

        asyncio.run(run())

    def test_related_hydration_is_bounded_and_one_failure_remains_canonical(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["issuelinks"] = [
            {
                "id": str(index),
                "type": {"outward": "relates to"},
                "outwardIssue": {
                    "id": str(900 + index),
                    "key": key,
                    "fields": {
                        "summary": f"Related {key}",
                        "status": {"name": "Open"},
                        "issuetype": {"name": "Story"},
                    },
                },
            }
            for index, key in enumerate(("T-2", "T-3", "T-4"), start=1)
        ]
        active_requests = 0
        max_active_requests = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active_requests, max_active_requests
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=payload)
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(200, json={"startAt": 0, "total": 0, "comments": []})
            match = re.fullmatch(r"/rest/api/2/issue/(T-[234])", request.url.path)
            if match:
                key = match.group(1)
                active_requests += 1
                max_active_requests = max(max_active_requests, active_requests)
                await asyncio.sleep(0.01)
                active_requests -= 1
                if key == "T-3":
                    return httpx.Response(403, json={"error": "forbidden"})
                return httpx.Response(
                    200,
                    json={
                        "id": key.split("-")[1],
                        "key": key,
                        "fields": {
                            "summary": f"Hydrated {key}",
                            "description": f"Requirements from {key}.",
                            "status": {"name": "Open"},
                            "issuetype": {"name": "Story"},
                            "reporter": {"displayName": "Product Lead"},
                            "created": "2025-06-25T09:00:00.000+0000",
                            "attachment": [],
                        },
                    },
                )
            if re.fullmatch(r"/rest/api/2/issue/T-[24]/comment", request.url.path):
                return httpx.Response(200, json={"startAt": 0, "total": 0, "comments": []})
            return httpx.Response(404)

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(
                    base_url="https://jira.example.test",
                    jql="project = T",
                    requirements={"related_issue_hydration_max_concurrency": 2},
                ),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            self.assertEqual(max_active_requests, 2)
            related = {item.identifier: item for item in issue.linked_issues}
            self.assertEqual(related["T-2"].description, "Requirements from T-2.")
            self.assertEqual(related["T-4"].description, "Requirements from T-4.")
            self.assertIsNone(related["T-2"].hydration_error)
            self.assertEqual(
                related["T-3"].hydration_error,
                "Related Jira issue T-3 could not be hydrated (HTTP 403); "
                "requirement context is incomplete.",
            )
            self.assertEqual(related["T-3"].title, "Related T-3")
            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
            self.assertIn(
                related["T-3"].hydration_error,
                snapshot.context_warnings,
            )
            self.assertFalse(
                any(
                    decision.id == "jira:T-2:description"
                    for decision in snapshot.current_requirements
                )
            )
            self.assertFalse(
                any(
                    decision.id == "jira:T-4:description"
                    for decision in snapshot.current_requirements
                )
            )

        asyncio.run(run())

    def test_related_hydration_limit_must_be_positive(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "related_issue_hydration_max_concurrency must be positive",
        ):
            JiraRequirementsConfig(related_issue_hydration_max_concurrency=0)

        with self.assertRaisesRegex(
            ValueError,
            "related_issue_hydration_max_concurrency must be at most 32",
        ):
            JiraRequirementsConfig(related_issue_hydration_max_concurrency=33)

        with self.assertRaisesRegex(
            ValueError,
            "attachment_download_max_concurrency must be positive",
        ):
            JiraRequirementsConfig(attachment_download_max_concurrency=0)

        with self.assertRaisesRegex(
            ValueError,
            "attachment_download_max_concurrency must be at most 32",
        ):
            JiraRequirementsConfig(attachment_download_max_concurrency=33)

    def test_only_root_planning_evidence_fields_are_completeness_checked(self) -> None:
        evidence_fields = {"description", "comment"}
        for field_id in BASE_ISSUE_FIELDS:
            with self.subTest(field_id=field_id, state="missing"):
                payload = sample_issue_payload()
                payload["fields"].pop(field_id)
                snapshot = normalize_issue(
                    payload,
                    "https://jira.example.test",
                ).requirements_snapshot
                assert snapshot is not None
                if field_id in evidence_fields:
                    expected = (
                        f"Jira issue T-1 did not return requested field {field_id}; "
                        "requirement context is incomplete."
                    )
                    self.assertIn(expected, snapshot.incomplete_reasons)
                    self.assertFalse(snapshot.complete)
                else:
                    warning = (
                        f"Jira issue T-1 did not return contextual field {field_id}; "
                        "planning evidence is unaffected."
                    )
                    self.assertNotIn(warning, snapshot.incomplete_reasons)
                    self.assertIn(warning, snapshot.context_warnings)

            with self.subTest(field_id=field_id, state="present-null"):
                payload = sample_issue_payload()
                payload["fields"][field_id] = None
                snapshot = normalize_issue(
                    payload,
                    "https://jira.example.test",
                ).requirements_snapshot
                assert snapshot is not None
                self.assertFalse(
                    any(
                        f"requested field {field_id}" in reason
                        for reason in snapshot.incomplete_reasons
                    )
                )

    def test_paginated_comments_do_not_depend_on_redundant_embedded_comment_field(self) -> None:
        payload = sample_issue_payload()
        payload["fields"].pop("comment")

        snapshot = normalize_issue(
            payload,
            "https://jira.example.test",
            comments=[],
        ).requirements_snapshot

        assert snapshot is not None
        self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
        self.assertFalse(
            any("requested field comment" in reason for reason in snapshot.incomplete_reasons)
        )
        self.assertFalse(
            any("contextual field comment" in warning for warning in snapshot.context_warnings)
        )

    def test_missing_parent_is_allowed_for_explicit_non_subtask_issue(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["issuetype"] = {"name": "Epic", "subtask": False}
        payload["fields"].pop("parent")

        snapshot = normalize_issue(
            payload,
            "https://jira.example.test",
        ).requirements_snapshot

        assert snapshot is not None
        self.assertFalse(
            any(
                "requested field parent" in reason
                for reason in snapshot.incomplete_reasons
            )
        )
        self.assertTrue(snapshot.complete)

    def test_missing_parent_never_blocks_planning_evidence(self) -> None:
        cases = {
            "subtask": {"name": "Sub-task", "subtask": True},
            "missing-subtask-marker": {"name": "Epic"},
        }
        expected = (
            "Jira issue T-1 did not return contextual field parent; "
            "planning evidence is unaffected."
        )

        for label, issue_type in cases.items():
            with self.subTest(label=label):
                payload = sample_issue_payload()
                payload["fields"]["issuetype"] = issue_type
                payload["fields"].pop("parent")

                snapshot = normalize_issue(
                    payload,
                    "https://jira.example.test",
                ).requirements_snapshot

                assert snapshot is not None
                self.assertNotIn(expected, snapshot.incomplete_reasons)
                self.assertIn(expected, snapshot.context_warnings)
                self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)

    def test_missing_epic_issue_type_warns_when_child_discovery_is_skipped(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["summary"] = "Epic: role-aware reporting"
        payload["fields"]["issuetype"] = {"name": "Epic"}
        payload["fields"].pop("issuetype")
        child_searches: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=payload)
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(
                    200,
                    json={"startAt": 0, "total": 0, "comments": []},
                )
            if request.url.path == "/rest/api/2/search":
                child_searches.append(str(request.url.params.get("jql") or ""))
                return httpx.Response(
                    200,
                    json={"startAt": 0, "total": 0, "issues": []},
                )
            return httpx.Response(404)

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(base_url="https://jira.example.test", jql="project = T"),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            self.assertIsNone(issue.issue_type)
            self.assertEqual(child_searches, [])
            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            expected = (
                "Jira issue T-1 did not return contextual field issuetype; "
                "planning evidence is unaffected."
            )
            self.assertIn(expected, snapshot.context_warnings)
            self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)

        asyncio.run(run())

    def test_requested_related_field_presence_distinguishes_missing_from_null(self) -> None:
        related = RelatedIssue(
            id="20002",
            identifier="T-2",
            title="Related issue",
            relation="relates to",
            direction="outward",
            source=RequirementSource(
                issue_identifier="T-1",
                source_type="relation",
                source_id="link:T-2",
                author="Product Owner",
                timestamp=datetime(2025, 6, 25, tzinfo=timezone.utc),
                authority="context",
            ),
        )
        base_fields = {
            "summary": "Related issue",
            "description": None,
            "status": {"name": "Open"},
            "issuetype": {"name": "Story"},
            "creator": {"displayName": "Product Owner"},
            "created": "2025-06-25T09:00:00.000+0000",
            "attachment": [],
        }

        for field_id in RELATED_ISSUE_FIELDS:
            with self.subTest(field_id=field_id, state="missing"):
                fields = dict(base_fields)
                fields.pop(field_id)
                hydrated = hydrate_related_issue_context(
                    related,
                    {
                        "id": "20002",
                        "key": "T-2",
                        "fields": fields,
                        "changelog": {"startAt": 0, "total": 0, "histories": []},
                    },
                    "https://jira.example.test",
                    JiraRequirementsConfig(),
                    comments=[],
                    attachments=[],
                )
                expected = (
                    f"Jira issue T-2 did not return contextual field {field_id}; "
                    "planning evidence is unaffected."
                )
                self.assertIn(expected, hydrated.provenance_incomplete_reasons)

            with self.subTest(field_id=field_id, state="present-null"):
                fields = dict(base_fields)
                fields[field_id] = None
                hydrated = hydrate_related_issue_context(
                    related,
                    {
                        "id": "20002",
                        "key": "T-2",
                        "fields": fields,
                        "changelog": {"startAt": 0, "total": 0, "histories": []},
                    },
                    "https://jira.example.test",
                    JiraRequirementsConfig(),
                    comments=[],
                    attachments=[],
                )
                self.assertFalse(
                    any(
                        f"requested field {field_id}" in reason
                        for reason in hydrated.provenance_incomplete_reasons
                    )
                )

    def test_missing_or_invalid_changelog_metadata_is_context_only(self) -> None:
        cases = {
            "absent": None,
            "not-an-object": [],
            "missing-total": {"startAt": 0, "histories": []},
            "invalid-total": {"startAt": 0, "total": -1, "histories": []},
        }
        for label, changelog in cases.items():
            with self.subTest(label=label):
                payload = sample_issue_payload()
                if changelog is None:
                    payload.pop("changelog")
                else:
                    payload["changelog"] = changelog
                snapshot = normalize_issue(
                    payload,
                    "https://jira.example.test",
                ).requirements_snapshot
                assert snapshot is not None
                self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
                self.assertTrue(
                    any("field provenance is incomplete" in reason for reason in snapshot.context_warnings)
                )

    def test_initial_provenance_uses_creator_not_reporter(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["creator"] = {"displayName": "Original Creator"}
        payload["fields"]["reporter"] = {"displayName": "Current Reporter"}

        snapshot = normalize_issue(payload, "https://jira.example.test").requirements_snapshot
        assert snapshot is not None and snapshot.description is not None
        self.assertEqual(snapshot.description.source.author, "Original Creator")

    def test_matching_changelog_edit_preserves_missing_provenance_as_warning(self) -> None:
        cases = {
            "missing-author": {
                "created": "2025-06-25T09:00:00.000+0000",
            },
            "invalid-timestamp": {
                "author": {"displayName": "Product Owner"},
                "created": "not-a-date",
            },
        }
        for label, provenance in cases.items():
            with self.subTest(label=label):
                payload = sample_issue_payload()
                payload["changelog"] = {
                    "startAt": 0,
                    "total": 1,
                    "histories": [
                        {
                            "id": label,
                            **provenance,
                            "items": [{"fieldId": "description", "field": "Description"}],
                        }
                    ],
                }
                snapshot = normalize_issue(
                    payload,
                    "https://jira.example.test",
                ).requirements_snapshot
                assert snapshot is not None and snapshot.description is not None
                self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
                source = snapshot.description.source
                if label == "missing-author":
                    self.assertEqual(source.author, "unknown")
                    self.assertIn(
                        "Decision jira:T-1:description has no known source author.",
                        snapshot.context_warnings,
                    )
                else:
                    self.assertIsNone(source.timestamp)
                    self.assertIn(
                        "Decision jira:T-1:description has no known source timestamp.",
                        snapshot.context_warnings,
                    )

    def test_equal_timestamp_conflicting_provenance_is_order_independent_warning(self) -> None:
        timestamp = "2025-06-25T09:00:00.000+0000"
        cases = {
            "authors": [
                {
                    "id": "history-1",
                    "author": {"displayName": "Alice"},
                    "created": timestamp,
                    "items": [
                        {
                            "fieldId": "description",
                            "field": "Description",
                            "toString": "Final behavior",
                        }
                    ],
                },
                {
                    "id": "history-2",
                    "author": {"displayName": "Bob"},
                    "created": timestamp,
                    "items": [
                        {
                            "fieldId": "description",
                            "field": "Description",
                            "toString": "Final behavior",
                        }
                    ],
                },
            ],
            "values": [
                {
                    "id": "history-a",
                    "author": {"displayName": "Product Owner"},
                    "created": timestamp,
                    "items": [
                        {
                            "fieldId": "description",
                            "field": "Description",
                            "toString": "Behavior A",
                        }
                    ],
                },
                {
                    "id": "history-b",
                    "author": {"displayName": "Product Owner"},
                    "created": timestamp,
                    "items": [
                        {
                            "fieldId": "description",
                            "field": "Description",
                            "toString": "Behavior B",
                        }
                    ],
                },
            ],
        }

        for label, histories in cases.items():
            with self.subTest(label=label):
                snapshots: list[RequirementsSnapshot] = []
                for ordered in (histories, list(reversed(histories))):
                    payload = sample_issue_payload()
                    payload["fields"]["description"] = "Final behavior"
                    payload["changelog"] = {
                        "startAt": 0,
                        "total": 2,
                        "histories": ordered,
                    }
                    snapshot = normalize_issue(
                        payload,
                        "https://jira.example.test",
                    ).requirements_snapshot
                    assert snapshot is not None and snapshot.description is not None
                    snapshots.append(snapshot)
                    self.assertEqual(snapshot.description.source.author, "unknown")
                    self.assertIsNone(snapshot.description.source.timestamp)
                    self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
                    self.assertIn(
                        "Decision jira:T-1:description has no known source author.",
                        snapshot.context_warnings,
                    )
                    self.assertIn(
                        "Decision jira:T-1:description has no known source timestamp.",
                        snapshot.context_warnings,
                    )

                self.assertEqual(snapshots[0].content_hash, snapshots[1].content_hash)

    def test_all_configured_authorities_must_be_nonblank_and_ranked(self) -> None:
        cases = (
            {"description_authority": ""},
            {"comment_authority": "unranked"},
            {"attachment_authority": "unranked"},
            {"relation_authority": "unranked"},
            {"field_authority": {"customfield_1": "unranked"}},
            {"comment_authority_by_author": {"Owner": "unranked"}},
            {"comment_authority_by_author": {" ": "product"}},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                JiraRequirementsConfig(**kwargs)

    def test_authority_rank_rejects_bool_and_conflicting_normalized_keys(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "authority_rank values must be integers",
        ):
            JiraRequirementsConfig(authority_rank={"product": True})

        with self.assertRaisesRegex(
            ValueError,
            "authority_rank contains conflicting normalized key 'legal'",
        ):
            JiraRequirementsConfig(
                authority_rank={"Legal": 40, " legal ": 50},
            )

        config = JiraRequirementsConfig(
            authority_rank={"Legal": 50, " legal ": 50},
        )
        self.assertEqual(config.authority_rank["legal"], 50)

    def test_snapshot_rejects_unknown_decision_source_authority(self) -> None:
        payload = sample_issue_payload()
        issue = normalize_issue(payload, "https://jira.example.test")
        assert issue.comments[0].source is not None
        issue.comments[0].source.authority = "unknown"

        snapshot = build_requirements_snapshot(
            issue,
            payload,
            JiraRequirementsConfig(),
        )

        self.assertFalse(snapshot.complete)
        self.assertIn(
            "Decision jira:T-1:comment:200 has no known source authority.",
            snapshot.incomplete_reasons,
        )

    def test_partial_search_hydration_opt_out_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "hydrate_search_results must be true",
        ):
            JiraRequirementsConfig(hydrate_search_results=False)

    def test_issue_ingestion_keeps_attachment_metadata_without_analysis(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["attachment"] = [
            {
                "id": "500",
                "filename": "role-matrix.png",
                "mimeType": "image/png",
                "size": 8,
                "content": "https://jira.example.test/secure/attachment/500/role-matrix.png",
                "thumbnail": "https://jira.example.test/secure/thumbnail/500",
                "author": {"displayName": "Designer"},
                "created": "2025-06-25T08:00:00.000+0000",
            }
        ]
        image_content = b"mock-png"
        attachment_fetches: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=payload)
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(200, json={"startAt": 0, "total": 0, "comments": []})
            if request.url.path == "/secure/attachment/500/role-matrix.png":
                attachment_fetches.append(request.url.path)
                return httpx.Response(200, content=image_content)
            return httpx.Response(404)

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(base_url="https://jira.example.test", jql="project = T"),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
                attachment_analyzer=FakeVisionAnalyzer(),
            )
            try:
                issue = await client.get_issue("T-1")
            finally:
                await client.close()

            attachment = issue.attachments[0]
            self.assertIsNone(attachment.content_sha256)
            self.assertEqual(attachment.analysis.status, "not_configured")
            self.assertEqual(attachment.analysis.modality, "metadata")
            self.assertEqual(attachment.source.author, "Designer")
            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            self.assertTrue(snapshot.complete)

            changed_context = snapshot.model_copy(deep=True)
            changed_context.attachments[0].analysis = AttachmentAnalysis(
                status="complete",
                modality="vision",
                summary="Different attachment analysis that is not planning evidence.",
            )
            self.assertEqual(
                snapshot.calculate_content_hash(),
                changed_context.calculate_content_hash(),
            )

        asyncio.run(run())
        self.assertEqual(attachment_fetches, [])

    def test_complete_screenshot_summary_is_not_planning_evidence(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["description"] = None
        payload["fields"]["comment"]["comments"] = []
        issue = normalize_issue(payload, "https://jira.example.test")
        issue.attachments = [
            complete_test_attachment(
                "T-1",
                "role-columns",
                "\n".join(
                    (
                        "- [classification: current] GC: Cost appears immediately after Budget.",
                        "- [classification: current] Sub: Cost is not shown.",
                        "- [classification: current] GC acting as Sub: Cost is not shown.",
                    )
                ),
            )
        ]

        snapshot = build_requirements_snapshot(
            issue,
            payload,
            JiraRequirementsConfig(),
        )

        self.assertIsNone(snapshot.description)
        self.assertEqual(snapshot.comments, [])
        self.assertEqual(snapshot.current_requirements, [])
        self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)

    def test_attachment_markers_cannot_create_inference_or_contradiction(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["description"] = None
        payload["fields"]["comment"]["comments"] = []
        issue = normalize_issue(payload, "https://jira.example.test")
        issue.attachments = [
            complete_test_attachment(
                "T-1",
                "ambiguous-mockup",
                "\n".join(
                    (
                        "- [inferred] GC acting as Sub may follow the Sub placement.",
                        "- [contradiction] The left mockup places Cost before Budget while the right mockup places Cost after Budget.",
                    )
                ),
            )
        ]

        snapshot = build_requirements_snapshot(
            issue,
            payload,
            JiraRequirementsConfig(),
        )

        self.assertEqual(snapshot.inferred_behavior, [])
        self.assertEqual(snapshot.unresolved_contradictions, [])
        self.assertEqual(snapshot.current_requirements, [])
        self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)

    def test_noncomplete_or_blank_attachment_analysis_is_not_promoted(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["description"] = None
        payload["fields"]["comment"]["comments"] = []
        issue = normalize_issue(payload, "https://jira.example.test")
        base = complete_test_attachment(
            "T-1",
            "base",
            "This would otherwise become a decision.",
        )
        issue.attachments = [
            base.model_copy(
                update={
                    "id": "not-configured",
                    "analysis": AttachmentAnalysis(
                        status="not_configured",
                        modality="vision",
                        summary="No analyzer was configured.",
                    ),
                }
            ),
            base.model_copy(
                update={
                    "id": "skipped",
                    "analysis": AttachmentAnalysis(
                        status="skipped",
                        modality="metadata",
                        summary="Analysis was skipped.",
                    ),
                }
            ),
            base.model_copy(
                update={
                    "id": "error",
                    "analysis": AttachmentAnalysis(
                        status="error",
                        modality="unknown",
                        summary="Analysis failed.",
                    ),
                }
            ),
            complete_test_attachment("T-1", "blank", "   "),
        ]

        snapshot = build_requirements_snapshot(
            issue,
            payload,
            JiraRequirementsConfig(require_attachment_analysis=False),
        )

        self.assertEqual(snapshot.current_requirements, [])
        self.assertEqual(snapshot.inferred_behavior, [])
        self.assertEqual(snapshot.superseded_requirements, [])
        self.assertEqual(snapshot.unresolved_contradictions, [])
        self.assertEqual(snapshot.incomplete_reasons, [])
        self.assertTrue(snapshot.complete)

    def test_related_attachment_remains_context_with_timestamp_stable_hash(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["description"] = None
        payload["fields"]["comment"]["comments"] = []
        issue = normalize_issue(payload, "https://jira.example.test")
        related_shell = RelatedIssue(
            id="903",
            identifier="T-PHASE2",
            title="Phase 2 design",
            relation="relates to",
            direction="outward",
            source=RequirementSource(
                issue_identifier="T-1",
                source_type="relation",
                source_id="link:T-PHASE2",
                author="Product Owner",
                timestamp=datetime(2025, 6, 25, tzinfo=timezone.utc),
                authority="context",
            ),
        )
        related_payload = {
            "id": "903",
            "key": "T-PHASE2",
            "fields": {
                "summary": "Phase 2 design",
                "description": None,
                "status": {"name": "Open"},
                "issuetype": {"name": "Story"},
                "creator": {"displayName": "Product Owner"},
                "created": "2025-06-25T09:00:00.000+0000",
                "attachment": [],
            },
            "changelog": {"startAt": 0, "total": 0, "histories": []},
        }

        def hydrate_at(generated_at: datetime) -> RelatedIssue:
            return hydrate_related_issue_context(
                related_shell,
                related_payload,
                "https://jira.example.test",
                JiraRequirementsConfig(),
                comments=[],
                attachments=[
                    complete_test_attachment(
                        "T-PHASE2",
                        "shared",
                        "[classification: current] Phase 2 places Cost after Budget for GC.",
                        generated_at=generated_at,
                    )
                ],
            )

        first_related = hydrate_at(datetime(2025, 7, 1, tzinfo=timezone.utc))
        issue.linked_issues = [first_related]
        first = build_requirements_snapshot(
            issue,
            payload,
            JiraRequirementsConfig(),
        )
        second_related = hydrate_at(datetime(2026, 1, 1, tzinfo=timezone.utc))
        issue.linked_issues = [second_related]
        second = build_requirements_snapshot(
            issue,
            payload,
            JiraRequirementsConfig(),
        )

        self.assertEqual(first_related.requirements, [])
        self.assertEqual(len(first_related.attachments), 1)
        self.assertEqual(first.current_requirements, [])
        self.assertEqual(first.incomplete_reasons, [])
        self.assertEqual(first.content_hash, second.content_hash)

    def test_attachment_and_text_reversal_remain_current_without_explicit_conflict(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["description"] = (
            "The Cost column appears before the Budget column."
        )
        payload["fields"]["comment"]["comments"] = []
        issue = normalize_issue(payload, "https://jira.example.test")
        issue.attachments = [
            complete_test_attachment(
                "T-1",
                "opposite-placement",
                "[classification: current] The Cost column appears after the Budget column.",
            )
        ]

        snapshot = build_requirements_snapshot(
            issue,
            payload,
            JiraRequirementsConfig(),
        )

        self.assertEqual(snapshot.unresolved_contradictions, [])
        self.assertEqual(
            {decision.kind for decision in snapshot.current_requirements},
            {"requirement"},
        )
        self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)

    def test_off_origin_attachment_is_rejected_without_leaking_auth(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["attachment"] = [
            {
                "id": "evil",
                "filename": "mockup.png",
                "mimeType": "image/png",
                "content": "https://evil.example.test/steal",
                "author": {"displayName": "Designer"},
                "created": "2025-06-25T08:00:00.000+0000",
            }
        ]
        seen_requests: list[tuple[str, str | None]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append((request.url.host, request.headers.get("Authorization")))
            return httpx.Response(200, content=b"should-not-be-fetched")

        async def run() -> None:
            config = TrackerConfig(
                base_url="https://jira.example.test",
                jql="project = T",
                requirements={
                    "download_attachments": True,
                    "require_attachment_analysis": False,
                },
            )
            issue = normalize_issue(
                payload,
                config.base_url,
                requirements_config=config.requirements,
            )
            client = JiraClient(
                config,
                environ={"JIRA_TOKEN": "secret-token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                issue.attachments[0] = await client._analyze_attachment(issue.attachments[0])
            finally:
                await client.close()

            attachment = issue.attachments[0]
            self.assertEqual(attachment.analysis.status, "error")
            self.assertIn("exact configured Jira origin", attachment.analysis.summary)
            snapshot = build_requirements_snapshot(issue, payload, config.requirements)
            self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)

        asyncio.run(run())
        self.assertEqual(seen_requests, [])

    def test_attachment_stream_aborts_immediately_after_byte_limit(self) -> None:
        class CountingStream(httpx.AsyncByteStream):
            def __init__(self) -> None:
                self.yielded = 0

            async def __aiter__(self):
                for chunk in (b"1234", b"56", b"never-read"):
                    self.yielded += 1
                    yield chunk

            async def aclose(self) -> None:
                return None

        stream = CountingStream()
        payload = sample_issue_payload()
        payload["fields"]["attachment"] = [
            {
                "id": "large",
                "filename": "large.txt",
                "mimeType": "text/plain",
                "content": "/secure/attachment/large",
                "author": {"displayName": "Designer"},
                "created": "2025-06-25T08:00:00.000+0000",
            }
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        async def run() -> None:
            config = TrackerConfig(
                base_url="https://jira.example.test",
                jql="project = T",
                requirements={
                    "download_attachments": True,
                    "max_attachment_bytes": 5,
                },
            )
            attachment = normalize_issue(
                payload,
                config.base_url,
                requirements_config=config.requirements,
            ).attachments[0]
            client = JiraClient(
                config,
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                analyzed = await client._analyze_attachment(attachment)
            finally:
                await client.close()
            self.assertEqual(analyzed.analysis.status, "skipped")
            self.assertIsNone(analyzed.content_sha256)

        asyncio.run(run())
        self.assertEqual(stream.yielded, 2)

    def test_attachment_content_length_precheck_does_not_read_body(self) -> None:
        class UnreadStream(httpx.AsyncByteStream):
            def __init__(self) -> None:
                self.read = False

            async def __aiter__(self):
                self.read = True
                yield b"body"

            async def aclose(self) -> None:
                return None

        stream = UnreadStream()
        payload = sample_issue_payload()
        payload["fields"]["attachment"] = [
            {
                "id": "declared-large",
                "filename": "large.txt",
                "mimeType": "text/plain",
                "content": "/secure/attachment/declared-large",
                "author": {"displayName": "Designer"},
                "created": "2025-06-25T08:00:00.000+0000",
            }
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": "100"},
                stream=stream,
            )

        async def run() -> None:
            config = TrackerConfig(
                base_url="https://jira.example.test",
                requirements={"max_attachment_bytes": 5},
            )
            attachment = normalize_issue(
                payload,
                config.base_url,
                requirements_config=config.requirements,
            ).attachments[0]
            client = JiraClient(
                config,
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                analyzed = await client._analyze_attachment(attachment)
            finally:
                await client.close()
            self.assertEqual(analyzed.analysis.status, "skipped")

        asyncio.run(run())
        self.assertFalse(stream.read)

    def test_attachment_download_concurrency_is_client_global_across_lists(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["attachment"] = [
            {
                "id": str(index),
                "filename": f"{index}.txt",
                "mimeType": "text/plain",
                "content": f"/secure/attachment/{index}",
                "author": {"displayName": "Designer"},
                "created": "2025-06-25T08:00:00.000+0000",
            }
            for index in range(6)
        ]
        active = 0
        peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, content=b"text")

        async def run() -> None:
            config = TrackerConfig(
                base_url="https://jira.example.test",
                jql="project = T",
                requirements={
                    "download_attachments": True,
                    "attachment_download_max_concurrency": 2,
                },
            )
            attachments = normalize_issue(
                payload,
                config.base_url,
                requirements_config=config.requirements,
            ).attachments
            client = JiraClient(
                config,
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                first, second = await asyncio.gather(
                    client._analyze_attachments(attachments[:3]),
                    client._analyze_attachments(attachments[3:]),
                )
            finally:
                await client.close()
            analyzed = first + second
            self.assertTrue(all(item.analysis.status == "complete" for item in analyzed))

        asyncio.run(run())
        self.assertEqual(peak, 2)

    def test_hash_stability_filtering_and_material_diff(self) -> None:
        config = JiraRequirementsConfig()
        original_payload = sample_issue_payload()
        original = normalize_issue(original_payload, "https://jira.example.test", requirements_config=config)
        original_snapshot = original.requirements_snapshot
        assert original_snapshot is not None

        recaptured = build_requirements_snapshot(original, original_payload, config)
        self.assertNotEqual(original_snapshot.captured_at, recaptured.captured_at)
        self.assertEqual(original_snapshot.content_hash, recaptured.content_hash)

        unrelated_update_payload = sample_issue_payload()
        unrelated_update_payload["fields"]["updated"] = "2025-02-03T03:04:05.000+0000"
        unrelated_update = normalize_issue(
            unrelated_update_payload,
            "https://jira.example.test",
            requirements_config=config,
        )
        assert unrelated_update.requirements_snapshot is not None
        self.assertEqual(
            original_snapshot.content_hash,
            unrelated_update.requirements_snapshot.content_hash,
        )

        status_only_payload = sample_issue_payload()
        status_only_payload["fields"]["comment"]["comments"].append(
            {
                "id": "999",
                "author": {"displayName": "Symphony"},
                "body": "Codex run started for T-1.\n\nWorkspace: /tmp/work",
                "created": "2025-01-05T03:04:05.000+0000",
            }
        )
        status_only = normalize_issue(
            status_only_payload,
            "https://jira.example.test",
            requirements_config=config,
        )
        assert status_only.requirements_snapshot is not None
        self.assertEqual(original_snapshot.content_hash, status_only.requirements_snapshot.content_hash)

        changed_payload = sample_issue_payload()
        changed_payload["fields"]["comment"]["comments"][0]["body"] = "Revised product requirement"
        changed = normalize_issue(changed_payload, "https://jira.example.test", requirements_config=config)
        changed_snapshot = changed.requirements_snapshot
        assert changed_snapshot is not None
        diff = diff_requirements_snapshots(original_snapshot, changed_snapshot)
        self.assertTrue(diff.material)
        self.assertIn("comments", diff.changed_sections)
        self.assertNotEqual(issue_requirements_fingerprint(original), issue_requirements_fingerprint(changed))

    def test_v4_hash_excludes_context_and_generic_custom_fields(self) -> None:
        config = JiraRequirementsConfig(
            custom_fields=["customfield_200"],
            acceptance_criteria_fields=["customfield_100"],
        )
        payload = sample_issue_payload()
        payload["names"] = {
            "customfield_100": "Acceptance Criteria",
            "customfield_200": "Engineering context",
        }
        payload["fields"]["customfield_100"] = "AC-1: Show the Created Date."
        payload["fields"]["customfield_200"] = "Use the legacy helper."
        original = normalize_issue(
            payload,
            "https://jira.example.test",
            requirements_config=config,
        ).requirements_snapshot
        assert original is not None

        artifacts = {artifact.source.field_id: artifact for artifact in original.custom_fields}
        self.assertTrue(artifacts["customfield_100"].planning_eligible)
        self.assertFalse(artifacts["customfield_200"].planning_eligible)
        canonical = original.canonical_content()
        for context_key in (
            "attachments",
            "parent",
            "children",
            "linked_issues",
            "dependencies",
            "components",
            "versions",
            "context_warnings",
        ):
            self.assertNotIn(context_key, canonical)
        self.assertEqual(
            [artifact["source"]["field_id"] for artifact in canonical["custom_fields"]],
            ["customfield_100"],
        )
        self.assertFalse(
            any(
                decision.sources[0].field_id == "customfield_200"
                for decision in original.current_requirements
            )
        )

        context_changed_payload = json.loads(json.dumps(payload))
        context_changed_payload["fields"]["customfield_200"] = "Different context."
        context_changed_payload["fields"]["attachment"] = [
            {
                "id": "context-only",
                "filename": "mockup.png",
                "mimeType": "image/png",
            }
        ]
        context_changed_payload["fields"]["components"] = [
            {"id": "10", "name": "Reporting"}
        ]
        context_changed_payload["fields"]["versions"] = [
            {"id": "11", "name": "2026.3"}
        ]
        context_changed_payload["fields"]["issuelinks"] = [
            {
                "id": "77",
                "type": {"outward": "relates to"},
                "outwardIssue": {
                    "id": "900",
                    "key": "T-2",
                    "fields": {"summary": "Context issue"},
                },
            }
        ]
        context_changed = normalize_issue(
            context_changed_payload,
            "https://jira.example.test",
            requirements_config=config,
        ).requirements_snapshot
        assert context_changed is not None
        self.assertEqual(original.content_hash, context_changed.content_hash)
        self.assertFalse(diff_requirements_snapshots(original, context_changed).material)

        warning_changed = original.model_copy(
            update={"context_warnings": ["Context changed."]}
        )
        self.assertEqual(
            original.calculate_content_hash(),
            warning_changed.calculate_content_hash(),
        )

        acceptance_changed_payload = json.loads(json.dumps(payload))
        acceptance_changed_payload["fields"]["customfield_100"] = (
            "AC-1: Hide the Created Date."
        )
        acceptance_changed = normalize_issue(
            acceptance_changed_payload,
            "https://jira.example.test",
            requirements_config=config,
        ).requirements_snapshot
        assert acceptance_changed is not None
        self.assertNotEqual(original.content_hash, acceptance_changed.content_hash)

        fallback_context_payload = json.loads(json.dumps(context_changed_payload))
        fallback_context_payload["fields"]["customfield_200"] = (
            payload["fields"]["customfield_200"]
        )
        fallback_original = normalize_issue(
            payload,
            "https://jira.example.test",
            requirements_config=config,
        ).model_copy(update={"requirements_snapshot": None})
        fallback_context = normalize_issue(
            fallback_context_payload,
            "https://jira.example.test",
            requirements_config=config,
        ).model_copy(update={"requirements_snapshot": None})
        self.assertEqual(
            issue_requirements_fingerprint(fallback_original),
            issue_requirements_fingerprint(fallback_context),
        )

    def test_v4_hash_excludes_derived_source_presentation_metadata(self) -> None:
        source = RequirementSource(
            issue_identifier="T-1",
            source_type="custom_field",
            source_id="field:customfield_100#unit:stable",
            field_id="customfield_100",
            field_name="Acceptance Criteria",
            url="https://jira.example.test/browse/T-1",
            location="decision-unit:1",
            author="Product Owner",
            authority="product",
        )
        snapshot = RequirementsSnapshot(
            issue_id="10001",
            issue_identifier="T-1",
            issue_url="https://jira.example.test/browse/T-1",
            custom_fields=[
                RequirementArtifact(
                    artifact_id="field:customfield_100",
                    source_type="custom_field",
                    text="Show the Created Date.",
                    source=source,
                    kind="acceptance_criterion",
                )
            ],
            current_requirements=[
                RequirementDecision(
                    id="decision:created-date",
                    text="Show the Created Date.",
                    kind="acceptance_criterion",
                    classification="current",
                    sources=[source],
                )
            ],
        )

        presentation_changed = snapshot.model_copy(deep=True)
        presentation_changed.issue_url = (
            "https://moved-jira.example.test/browse/T-1"
        )
        presentation_sources = [
            presentation_changed.custom_fields[0].source,
            *presentation_changed.current_requirements[0].sources,
        ]
        for changed_source in presentation_sources:
            changed_source.field_name = "Renamed Acceptance Criteria"
            changed_source.url = "https://moved-jira.example.test/browse/T-1"
            changed_source.location = "rendered-unit:99"

        self.assertEqual(
            snapshot.calculate_content_hash(),
            presentation_changed.calculate_content_hash(),
        )
        canonical = snapshot.canonical_content()
        self.assertEqual(canonical["issue_url"], "")
        canonical_json = json.dumps(canonical, sort_keys=True)
        for excluded_key in ("field_name", "url", "location"):
            self.assertNotIn(f'"{excluded_key}"', canonical_json)

        identity_changed = snapshot.model_copy(deep=True)
        identity_changed.current_requirements[0].sources[0].source_id = (
            "field:customfield_100#unit:different"
        )
        self.assertNotEqual(
            snapshot.calculate_content_hash(),
            identity_changed.calculate_content_hash(),
        )

        text_changed = snapshot.model_copy(deep=True)
        text_changed.custom_fields[0].text = "Hide the Created Date."
        self.assertNotEqual(
            snapshot.calculate_content_hash(),
            text_changed.calculate_content_hash(),
        )

        classification_changed = snapshot.model_copy(deep=True)
        classification_changed.current_requirements[0].classification = "superseded"
        self.assertNotEqual(
            snapshot.calculate_content_hash(),
            classification_changed.calculate_content_hash(),
        )

    def test_only_missing_root_acceptance_custom_field_blocks(self) -> None:
        payload = sample_issue_payload()
        config = JiraRequirementsConfig(
            custom_fields=["customfield_context"],
            acceptance_criteria_fields=["customfield_acceptance"],
        )

        missing_both = normalize_issue(
            payload,
            "https://jira.example.test",
            requirements_config=config,
        ).requirements_snapshot
        assert missing_both is not None
        self.assertIn(
            "Configured Jira field customfield_acceptance was not returned.",
            missing_both.incomplete_reasons,
        )
        self.assertIn(
            "Configured Jira field customfield_context was not returned.",
            missing_both.context_warnings,
        )

        generic_missing_payload = sample_issue_payload()
        generic_missing_payload["fields"]["customfield_acceptance"] = "AC-1: Works."
        generic_missing = normalize_issue(
            generic_missing_payload,
            "https://jira.example.test",
            requirements_config=config,
        ).requirements_snapshot
        assert generic_missing is not None
        self.assertTrue(generic_missing.complete, generic_missing.incomplete_reasons)

    def test_link_relation_hash_ignores_root_global_updated_timestamp(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["issuelinks"] = [
            {
                "id": "78",
                "type": {"inward": "relates to", "outward": "relates to"},
                "outwardIssue": {
                    "id": "903",
                    "key": "T-PHASE2",
                    "fields": {
                        "summary": "Phase 2 design",
                        "status": {"name": "Open"},
                        "issuetype": {"name": "Story"},
                    },
                },
            }
        ]
        original = normalize_issue(payload, "https://jira.example.test")
        original_snapshot = original.requirements_snapshot
        assert original_snapshot is not None
        self.assertIsNone(original_snapshot.linked_issues[0].source.timestamp)

        updated_payload = sample_issue_payload()
        updated_payload["fields"]["issuelinks"] = payload["fields"]["issuelinks"]
        updated_payload["fields"]["updated"] = "2026-07-13T12:00:00.000+0000"
        updated = normalize_issue(updated_payload, "https://jira.example.test")
        updated_snapshot = updated.requirements_snapshot
        assert updated_snapshot is not None
        self.assertIsNone(updated_snapshot.linked_issues[0].source.timestamp)
        self.assertEqual(original_snapshot.content_hash, updated_snapshot.content_hash)

    def test_default_epic_child_discovery_failure_is_context_only(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["issuetype"] = {"name": "Epic"}
        child_jql: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=payload)
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                return httpx.Response(
                    200,
                    json={"startAt": 0, "total": 0, "comments": []},
                )
            if request.url.path == "/rest/api/2/search":
                child_jql.append(request.url.params["jql"])
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(404)

        async def run() -> None:
            config = TrackerConfig(
                base_url="https://jira.example.test",
                jql="project = T",
            )
            client = JiraClient(
                config,
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                issue = await client.get_issue("T-1")
                non_epic = normalize_issue(
                    sample_issue_payload(),
                    config.base_url,
                    requirements_config=config.requirements,
                )
                self.assertFalse(client._child_discovery_enabled(non_epic))
            finally:
                await client.close()

            snapshot = issue.requirements_snapshot
            assert snapshot is not None
            self.assertTrue(snapshot.complete, snapshot.incomplete_reasons)
            self.assertTrue(
                any(
                    "Jira child discovery for T-1 is incomplete" in reason
                    for reason in snapshot.context_warnings
                )
            )

        asyncio.run(run())
        self.assertEqual(child_jql, ['parent = "T-1"'])

    def test_explicit_child_query_retains_non_epic_behavior_and_can_be_disabled(self) -> None:
        async def run() -> None:
            root = normalize_issue(sample_issue_payload(), "https://jira.example.test")
            explicit = JiraClient(
                TrackerConfig(
                    base_url="https://jira.example.test",
                    requirements={"child_issue_jql": "parent = {issue_key}"},
                ),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            )
            disabled = JiraClient(
                TrackerConfig(
                    base_url="https://jira.example.test",
                    requirements={"discover_epic_children": False},
                ),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            )
            try:
                self.assertTrue(explicit._child_discovery_enabled(root))
                epic = root.model_copy(update={"issue_type": "Epic"})
                self.assertFalse(disabled._child_discovery_enabled(epic))
            finally:
                await explicit.close()
                await disabled.close()

        asyncio.run(run())

    def test_child_search_detects_deduplication_truncation_and_invalid_offsets(self) -> None:
        child = {
            "id": "20001",
            "key": "T-2",
            "fields": {
                "summary": "Child",
                "status": {"name": "Open"},
                "issuetype": {"name": "Story"},
            },
        }
        cases = {
            "duplicate": {
                "startAt": 0,
                "total": 2,
                "issues": [child, child],
            },
            "early-is-last": {
                "startAt": 0,
                "total": 2,
                "isLast": True,
                "issues": [child],
            },
            "negative-offset": {
                "startAt": -1,
                "total": 2,
                "issues": [child],
            },
            "jumped-offset": {
                "startAt": 7,
                "total": 2,
                "issues": [child],
            },
        }

        async def run_case(label: str, response_payload: dict) -> None:
            async def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=response_payload)

            client = JiraClient(
                TrackerConfig(
                    base_url="https://jira.example.test",
                    requirements={"child_issue_jql": "parent = {issue_key}"},
                ),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                root = normalize_issue(sample_issue_payload(), "https://jira.example.test")
                children, reasons = await client._get_configured_children(root)
            finally:
                await client.close()
            invalid_offset = label in {"negative-offset", "jumped-offset"}
            expected_children = [] if invalid_offset else ["T-2"]
            self.assertEqual([item.identifier for item in children], expected_children)
            self.assertTrue(reasons, label)
            if invalid_offset:
                self.assertTrue(
                    any("startAt" in reason for reason in reasons),
                    reasons,
                )

        for label, response_payload in cases.items():
            with self.subTest(label=label):
                asyncio.run(run_case(label, response_payload))

    def test_child_search_page_limit_marks_results_incomplete(self) -> None:
        children = [
            {"id": str(index), "key": f"T-{index}", "fields": {}}
            for index in range(1, 101)
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"startAt": 0, "total": 101, "issues": children},
            )

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(
                    base_url="https://jira.example.test",
                    requirements={
                        "child_issue_jql": "parent = {issue_key}",
                        "child_issue_max_pages": 1,
                    },
                ),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                root = normalize_issue(sample_issue_payload(), "https://jira.example.test")
                _, reasons = await client._get_configured_children(root)
            finally:
                await client.close()
            self.assertTrue(any("1-page limit was reached" in reason for reason in reasons))

        asyncio.run(run())

    def test_configured_child_query_paginates_beyond_one_hundred(self) -> None:
        children = [
            {
                "id": str(10_000 + index),
                "key": f"T-CHILD-{index:03d}",
                "fields": {
                    "summary": f"Child {index}",
                    "status": {"name": "Open"},
                    "issuetype": {"name": "Story"},
                },
            }
            for index in range(101)
        ]
        requested_offsets: list[int] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path != "/rest/api/2/search":
                return httpx.Response(404)
            start_at = int(request.url.params["startAt"])
            max_results = int(request.url.params["maxResults"])
            requested_offsets.append(start_at)
            return httpx.Response(
                200,
                json={
                    "startAt": start_at,
                    "maxResults": max_results,
                    "total": len(children),
                    "issues": children[start_at : start_at + max_results],
                },
            )

        async def run() -> None:
            client = JiraClient(
                TrackerConfig(
                    base_url="https://jira.example.test",
                    jql="project = T",
                    requirements={"child_issue_jql": "parent = {issue_key}"},
                ),
                environ={"JIRA_TOKEN": "token"},
                transport=httpx.MockTransport(handler),
            )
            try:
                root = normalize_issue(sample_issue_payload(), "https://jira.example.test")
                related, incomplete_reasons = await client._get_configured_children(root)
            finally:
                await client.close()
            self.assertEqual(requested_offsets, [0, 100])
            self.assertEqual(incomplete_reasons, [])
            self.assertEqual(len(related), 101)
            self.assertEqual(related[0].identifier, "T-CHILD-000")
            self.assertEqual(related[-1].identifier, "T-CHILD-100")

        asyncio.run(run())

    def test_dynamic_attachment_error_text_is_not_material(self) -> None:
        payload = sample_issue_payload()
        payload["fields"]["attachment"] = [
            {
                "id": "500",
                "filename": "mockup.png",
                "mimeType": "image/png",
                "content": "https://jira.example.test/attachment/500",
                "author": {"displayName": "Designer"},
                "created": "2025-06-25T08:00:00.000+0000",
            }
        ]
        issue = normalize_issue(payload, "https://jira.example.test")
        snapshot = issue.requirements_snapshot
        assert snapshot is not None
        first = snapshot.model_copy(deep=True)
        second = snapshot.model_copy(deep=True)
        first.attachments[0].analysis = AttachmentAnalysis(
            status="error",
            summary="ConnectError: request 123 failed",
        )
        second.attachments[0].analysis = AttachmentAnalysis(
            status="error",
            summary="TimeoutError: request 999 failed",
        )
        self.assertEqual(first.calculate_content_hash(), second.calculate_content_hash())

    def test_jira_client_uses_token_config_file(self) -> None:
        seen_auth: list[str | None] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("Authorization"))
            return httpx.Response(200, json={"issues": []})

        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                token_file = Path(tmp) / "config.toml"
                token_file.write_text('JIRA_PERSONAL_TOKEN = "file-token"\n', encoding="utf-8")
                config = TrackerConfig(
                    base_url="https://jira.example.test",
                    jql="project = T",
                    auth={
                        "token_env": "MISSING_TOKEN",
                        "token_config_file": token_file,
                        "token_config_key": "JIRA_PERSONAL_TOKEN",
                    },
                )
                client = JiraClient(config, environ={}, transport=httpx.MockTransport(handler))
                try:
                    await client.search_issues("project = T", limit=10)
                finally:
                    await client.close()

        asyncio.run(run())
        self.assertEqual(seen_auth, ["Bearer file-token"])

    def test_pre_v4_projection_ignores_context_but_not_root_decisions(self) -> None:
        description_source = RequirementSource(
            issue_identifier="T-1",
            source_type="description",
            source_id="description",
            field_id="description",
            author="Product Owner",
            authority="product",
        )
        description = RequirementArtifact(
            artifact_id="T-1:description",
            source_type="description",
            text="Display the project creation date.",
            source=description_source,
        )
        decision_source = description_source.model_copy(
            update={"source_id": "description#unit:date"}
        )
        decision = RequirementDecision(
            id="T-1-R1",
            text="Display the project creation date.",
            classification="current",
            sources=[decision_source],
        )
        legacy = RequirementsSnapshot(
            schema_version="jira-requirements/v2",
            issue_id="1",
            issue_identifier="T-1",
            issue_url="https://jira.example.test/browse/T-1",
            description=description,
            current_requirements=[decision],
            components=[{"id": "10", "name": "Home", "kind": "component"}],
        )
        current = legacy.model_copy(
            deep=True,
            update={
                "schema_version": "jira-requirements/v4",
                "components": [],
                "context_warnings": ["Context lookup failed."],
            },
        )
        self.assertTrue(
            requirements_planning_authority_equivalent(legacy, current)
        )

        changed = current.model_copy(deep=True)
        changed.current_requirements[0].classification = "superseded"
        self.assertFalse(
            requirements_planning_authority_equivalent(legacy, changed)
        )


if __name__ == "__main__":
    unittest.main()
