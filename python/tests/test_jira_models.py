from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from symphony_jira.config import TrackerConfig
from symphony_jira.jira import JiraClient, normalize_issue


def sample_issue_payload() -> dict:
    return {
        "id": "10001",
        "key": "T-1",
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
        },
    }


class JiraModelTests(unittest.TestCase):
    def test_normalize_issue_payload(self) -> None:
        issue = normalize_issue(sample_issue_payload(), "https://jira.example.test")

        self.assertEqual(issue.identifier, "T-1")
        self.assertEqual(issue.title, "Fix thing")
        self.assertEqual(issue.description, "Description")
        self.assertEqual(issue.labels, ["codex-ready", "backend"])
        self.assertEqual(issue.comments[0].author, "Lin")
        self.assertEqual(issue.url, "https://jira.example.test/browse/T-1")

    def test_jira_client_search_get_and_comment_with_fake_server(self) -> None:
        comments: list[dict] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/2/search":
                return httpx.Response(200, json={"issues": [sample_issue_payload()]})
            if request.url.path == "/rest/api/2/issue/T-1":
                return httpx.Response(200, json=sample_issue_payload())
            if request.url.path == "/rest/api/2/issue/T-1/comment":
                comments.append(__import__("json").loads(request.content.decode()))
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
            self.assertEqual(issue.identifier, "T-1")
            self.assertEqual(comments, [{"body": "done"}])

        asyncio.run(run())

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


if __name__ == "__main__":
    unittest.main()
