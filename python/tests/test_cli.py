from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from symphony_jira.cli import main
from symphony_jira.models import Issue


class CliTests(unittest.TestCase):
    def test_validate_command_uses_workflow_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow = write_workflow(root, fake_codex)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with patch.dict(os.environ, {"TEST_JIRA_TOKEN": "token"}, clear=False):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(["validate", str(workflow)])

            self.assertEqual(code, 0)
            self.assertIn("Workflow valid.", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_validate_command_reports_missing_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow = write_workflow(root, fake_codex)
            stderr = io.StringIO()

            with patch.dict(os.environ, {}, clear=True):
                with redirect_stderr(stderr):
                    code = main(["validate", str(workflow)])

            self.assertEqual(code, 1)
            self.assertIn("jira_token_missing", stderr.getvalue())

    def test_once_dry_run_without_issue_uses_jql_when_one_issue_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow = write_workflow(root, fake_codex, prompt="Issue {{ issue.identifier }}")
            stdout = io.StringIO()
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Fix thing",
                status="To Do",
                labels=[],
                url="https://jira.example.test/browse/T-1",
            )

            with patch.dict(os.environ, {"TEST_JIRA_TOKEN": "token"}, clear=False):
                with patch("symphony_jira.cli.JiraClient", lambda *args, **kwargs: FakeJiraClient([issue])):
                    with redirect_stdout(stdout):
                        code = main(["once", str(workflow), "--dry-run", "--force"])

            self.assertEqual(code, 0)
            self.assertIn("JQL matched 1 issue(s).", stdout.getvalue())
            self.assertIn("Rendered prompt:", stdout.getvalue())
            self.assertIn("Issue T-1", stdout.getvalue())

    def test_once_without_issue_requires_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow = write_workflow(root, fake_codex)
            stderr = io.StringIO()

            with patch.dict(os.environ, {"TEST_JIRA_TOKEN": "token"}, clear=False):
                with patch("symphony_jira.cli.JiraClient", lambda *args, **kwargs: FakeJiraClient([])):
                    with redirect_stderr(stderr):
                        code = main(["once", str(workflow)])

            self.assertEqual(code, 2)
            self.assertIn("once requires --issue unless --dry-run is set.", stderr.getvalue())


class FakeJiraClient:
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def search_issues(self, jql: str, limit: int) -> list[Issue]:
        return self.issues[:limit]

    async def get_issue(self, key: str, include_comments: bool = True) -> Issue:
        for issue in self.issues:
            if issue.identifier == key:
                return issue
        raise AssertionError(f"unexpected issue key {key}")


def write_workflow(root: Path, fake_codex: Path, prompt: str = "Prompt") -> Path:
    workflow = root / "WORKFLOW.md"
    workflow.write_text(
        f"""---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
  jql: "project = T"
workspace:
  root: "./workspaces"
  strategy: hook_only
codex:
  command: "{fake_codex}"
---
{prompt}
""",
        encoding="utf-8",
    )
    return workflow


def write_fake_codex(root: Path) -> Path:
    path = root / "fake_codex.py"
    path.write_text(
        """#!/usr/bin/env python3
import sys
if "--version" in sys.argv:
    print("fake codex 0.0")
    sys.exit(0)
sys.exit(0)
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


if __name__ == "__main__":
    unittest.main()
