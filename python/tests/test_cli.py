from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from symphony_jira.cli import main
from symphony_jira.models import Issue


class CliTests(unittest.TestCase):
    def test_runtime_verify_uses_all_configured_sources_without_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            runtime_config = SimpleNamespace(
                repositories={"cpm": object(), "foyr2": object(), "pi": object()}
            )
            workflow = runtime_workflow(runtime_config)
            result = runtime_result("passed", workspace, profile="cpm_pytest")
            manager = SimpleNamespace(
                verify=AsyncMock(return_value=result),
                start_preview=AsyncMock(),
                stop_preview=AsyncMock(),
                shutdown=AsyncMock(),
            )
            stdout = io.StringIO()

            with (
                patch("symphony_jira.cli.load_workflow", return_value=workflow),
                patch("symphony_jira.cli.RuntimeManager", return_value=manager) as manager_class,
                patch("symphony_jira.cli.validate_preflight") as preflight,
                redirect_stdout(stdout),
            ):
                code = main(
                    [
                        "runtime",
                        "WORKFLOW.md",
                        "verify",
                        "--workspace",
                        str(workspace),
                        "--repository",
                        "cpm",
                        "--target-arg",
                        "Test/unit",
                        "--target-arg",
                        "quick",
                    ]
                )

            self.assertEqual(code, 0)
            preflight.assert_not_called()
            manager_class.assert_called_once_with(
                runtime_config,
                environ=os.environ,
                excluded_environment_names={
                    "CUSTOM_JIRA_TOKEN",
                    "CUSTOM_JIRA_EMAIL",
                },
            )
            manager.verify.assert_awaited_once_with(
                workspace.resolve(),
                "cpm",
                target_args=["Test/unit", "quick"],
                source_repositories=["cpm", "foyr2", "pi"],
            )
            manager.start_preview.assert_not_awaited()
            manager.stop_preview.assert_not_awaited()
            manager.shutdown.assert_not_awaited()
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "argv": ["fake-compose", "run"],
                    "log_path": str(workspace / ".symphony" / "runtime.log"),
                    "message": "runtime result",
                    "profile": "cpm_pytest",
                    "repository": "cpm",
                    "returncode": 0,
                    "status": "passed",
                },
            )

    def test_runtime_start_uses_explicit_source_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            runtime_config = SimpleNamespace(repositories={"cpm": object()})
            workflow = runtime_workflow(runtime_config)
            manager = SimpleNamespace(
                verify=AsyncMock(),
                start_preview=AsyncMock(
                    return_value=runtime_result("started", workspace)
                ),
                stop_preview=AsyncMock(),
                shutdown=AsyncMock(),
            )

            with (
                patch("symphony_jira.cli.load_workflow", return_value=workflow),
                patch("symphony_jira.cli.RuntimeManager", return_value=manager),
                patch("symphony_jira.cli.validate_preflight") as preflight,
                redirect_stdout(io.StringIO()),
            ):
                code = main(
                    [
                        "runtime",
                        "WORKFLOW.md",
                        "start",
                        "--workspace",
                        str(workspace),
                        "--repository",
                        "cpm",
                        "--source-repository",
                        "cpm",
                        "--source-repository",
                        "foyr2",
                    ]
                )

            self.assertEqual(code, 0)
            preflight.assert_not_called()
            manager.start_preview.assert_awaited_once_with(
                workspace.resolve(),
                "cpm",
                source_repositories=["cpm", "foyr2"],
            )
            manager.verify.assert_not_awaited()
            manager.stop_preview.assert_not_awaited()
            manager.shutdown.assert_not_awaited()

    def test_runtime_stop_returns_failure_for_environment_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            runtime_config = SimpleNamespace(repositories={"cpm": object()})
            workflow = runtime_workflow(runtime_config)
            manager = SimpleNamespace(
                verify=AsyncMock(),
                start_preview=AsyncMock(),
                stop_preview=AsyncMock(
                    return_value=runtime_result(
                        "environment_blocked", workspace, returncode=125
                    )
                ),
                shutdown=AsyncMock(),
            )
            stdout = io.StringIO()

            with (
                patch("symphony_jira.cli.load_workflow", return_value=workflow),
                patch("symphony_jira.cli.RuntimeManager", return_value=manager),
                patch("symphony_jira.cli.validate_preflight") as preflight,
                redirect_stdout(stdout),
            ):
                code = main(
                    [
                        "runtime",
                        "WORKFLOW.md",
                        "stop",
                        "--workspace",
                        str(workspace),
                        "--repository",
                        "cpm",
                    ]
                )

            self.assertEqual(code, 1)
            preflight.assert_not_called()
            manager.stop_preview.assert_awaited_once_with(
                workspace.resolve(),
                "cpm",
                source_repositories=["cpm"],
            )
            self.assertEqual(
                json.loads(stdout.getvalue())["status"], "environment_blocked"
            )
            manager.shutdown.assert_not_awaited()

    def test_runtime_shutdown_stops_selected_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            runtime_config = SimpleNamespace(
                repositories={"cpm": object(), "foyr2": object(), "pi": object()}
            )
            workflow = runtime_workflow(runtime_config)
            result = runtime_shutdown_result("stopped", workspace)
            manager = SimpleNamespace(
                verify=AsyncMock(),
                start_preview=AsyncMock(),
                stop_preview=AsyncMock(),
                shutdown=AsyncMock(return_value=result),
            )
            stdout = io.StringIO()

            with (
                patch("symphony_jira.cli.load_workflow", return_value=workflow),
                patch("symphony_jira.cli.RuntimeManager", return_value=manager),
                patch("symphony_jira.cli.validate_preflight") as preflight,
                redirect_stdout(stdout),
            ):
                code = main(
                    [
                        "runtime",
                        "WORKFLOW.md",
                        "shutdown",
                        "--workspace",
                        str(workspace),
                        "--repository",
                        "cpm",
                        "--repository",
                        "foyr2",
                        "--source-repository",
                        "cpm",
                        "--source-repository",
                        "foyr2",
                    ]
                )

            self.assertEqual(code, 0)
            preflight.assert_not_called()
            manager.shutdown.assert_awaited_once_with(
                workspace.resolve(),
                ["cpm", "foyr2"],
                source_repositories=["cpm", "foyr2"],
            )
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "argv": ["fake-compose", "stop", "cpm", "foyr"],
                    "log_path": str(workspace / ".symphony" / "shutdown.log"),
                    "message": "runtime shutdown result",
                    "repositories": ["cpm", "foyr2"],
                    "returncode": 0,
                    "services": ["cpm", "foyr"],
                    "status": "stopped",
                },
            )

    def test_runtime_shutdown_returns_failure_for_environment_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            runtime_config = SimpleNamespace(repositories={"cpm": object()})
            workflow = runtime_workflow(runtime_config)
            result = runtime_shutdown_result(
                "environment_blocked", workspace, repositories=("cpm",), services=("cpm",)
            )
            manager = SimpleNamespace(shutdown=AsyncMock(return_value=result))

            with (
                patch("symphony_jira.cli.load_workflow", return_value=workflow),
                patch("symphony_jira.cli.RuntimeManager", return_value=manager),
                redirect_stdout(io.StringIO()),
            ):
                code = main(
                    [
                        "runtime",
                        "WORKFLOW.md",
                        "shutdown",
                        "--workspace",
                        str(workspace),
                        "--repository",
                        "cpm",
                    ]
                )

            self.assertEqual(code, 1)

    def test_runtime_stop_rejects_multiple_repositories(self) -> None:
        stderr = io.StringIO()

        with (
            patch("symphony_jira.cli.load_workflow") as load_workflow,
            patch("symphony_jira.cli.RuntimeManager") as manager_class,
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "runtime",
                    "WORKFLOW.md",
                    "stop",
                    "--workspace",
                    "/tmp/workspace",
                    "--repository",
                    "cpm",
                    "--repository",
                    "foyr2",
                ]
            )

        self.assertEqual(code, 1)
        self.assertIn("require exactly one --repository", stderr.getvalue())
        load_workflow.assert_not_called()
        manager_class.assert_not_called()

    def test_runtime_rejects_target_arguments_for_preview_actions(self) -> None:
        stderr = io.StringIO()

        with (
            patch("symphony_jira.cli.load_workflow") as load_workflow,
            patch("symphony_jira.cli.RuntimeManager") as manager_class,
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "runtime",
                    "WORKFLOW.md",
                    "start",
                    "--workspace",
                    "/tmp/workspace",
                    "--repository",
                    "cpm",
                    "--target-arg",
                    "Test/unit",
                ]
            )

        self.assertEqual(code, 1)
        self.assertIn("--target-arg is valid only", stderr.getvalue())
        load_workflow.assert_not_called()
        manager_class.assert_not_called()

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


def runtime_workflow(runtime_config):
    return SimpleNamespace(
        config=SimpleNamespace(
            runtime=runtime_config,
            tracker=SimpleNamespace(
                auth=SimpleNamespace(
                    token_env="CUSTOM_JIRA_TOKEN",
                    email_env="CUSTOM_JIRA_EMAIL",
                )
            ),
        )
    )


def runtime_result(
    status: str,
    workspace: Path,
    *,
    profile: str | None = None,
    returncode: int = 0,
):
    values = {
        "status": status,
        "repository": "cpm",
        "argv": ("fake-compose", "run"),
        "returncode": returncode,
        "log_path": workspace / ".symphony" / "runtime.log",
        "message": "runtime result",
    }
    if profile is not None:
        values["profile"] = profile
    return SimpleNamespace(**values)


def runtime_shutdown_result(
    status: str,
    workspace: Path,
    *,
    repositories: tuple[str, ...] = ("cpm", "foyr2"),
    services: tuple[str, ...] = ("cpm", "foyr"),
):
    return SimpleNamespace(
        status=status,
        repositories=repositories,
        services=services,
        argv=("fake-compose", "stop", "cpm", "foyr"),
        returncode=0,
        log_path=workspace / ".symphony" / "shutdown.log",
        message="runtime shutdown result",
    )


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
