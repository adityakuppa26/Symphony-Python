from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from symphony_jira.config import (
    CodexConfig,
    JiraRequirementsConfig,
    WorkspaceConfig,
    WorkflowConfig,
    validate_preflight,
)
from symphony_jira.workflow import load_workflow


class ConfigTests(unittest.TestCase):


    def test_managed_repositories_are_safe_and_deduplicated(self) -> None:
        config = WorkspaceConfig(
            managed_repositories=[Path("cpm"), Path("cpm"), Path("foyr2")]
        )

        self.assertEqual(
            config.managed_repositories,
            [Path("cpm"), Path("foyr2")],
        )
        for unsafe in (Path("../cpm"), Path("/tmp/cpm"), Path(".")):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "safe relative paths|name"):
                    WorkspaceConfig(managed_repositories=[unsafe])

    def test_jira_attachments_are_disabled_by_default(self) -> None:
        requirements = JiraRequirementsConfig()

        self.assertFalse(requirements.download_attachments)
        self.assertFalse(requirements.require_attachment_analysis)

    def test_default_planning_prompt_uses_existing_behavior_for_incidental_edges(self) -> None:
        prompt = CodexConfig().planning_prompt

        self.assertIn("incidental edge cases", prompt)
        self.assertIn("established repository behavior", prompt)
        self.assertIn("no applicable precedent exists", prompt)


    def test_codex_environment_exclusions_have_safe_defaults_and_are_configurable(self) -> None:
        self.assertEqual(
            CodexConfig().environment_exclude,
            ["JIRA_*", "*_JIRA_TOKEN", "*_JIRA_EMAIL"],
        )
        self.assertEqual(
            CodexConfig(environment_exclude=["PRIVATE_*", "EXACT_NAME"]).environment_exclude,
            ["PRIVATE_*", "EXACT_NAME"],
        )

    def test_codex_environment_exclusions_reject_blank_patterns(self) -> None:
        with self.assertRaises(ValueError):
            CodexConfig(environment_exclude=["JIRA_*", " "])

    def test_validate_accepts_fake_codex_and_jira_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
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
Prompt
""",
                encoding="utf-8",
            )
            env = {"TEST_JIRA_TOKEN": "token"}
            workflow = load_workflow(workflow_path, environ=env)

            issues = validate_preflight(workflow_path, workflow.config, environ=env)

        self.assertEqual(issues, [])

    def test_validate_catches_missing_jira_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                f"""---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
  jql: "project = T"
workspace:
  strategy: hook_only
codex:
  command: "{fake_codex}"
---
Prompt
""",
                encoding="utf-8",
            )
            workflow = load_workflow(workflow_path, environ={})

            issues = validate_preflight(workflow_path, workflow.config, environ={})

        self.assertIn("jira_token_missing", {issue.code for issue in issues})

    def test_validate_accepts_jira_token_from_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            token_file = root / "config.toml"
            token_file.write_text('JIRA_PERSONAL_TOKEN = "token-from-file"\n', encoding="utf-8")
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                f"""---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
    token_config_file: "{token_file}"
    token_config_key: JIRA_PERSONAL_TOKEN
  jql: "project = T"
workspace:
  strategy: hook_only
codex:
  command: "{fake_codex}"
---
Prompt
""",
                encoding="utf-8",
            )
            workflow = load_workflow(workflow_path, environ={})

            issues = validate_preflight(workflow_path, workflow.config, environ={})

        self.assertEqual(issues, [])

    def test_validate_catches_missing_git_worktree_source_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                f"""---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
  jql: "project = T"
workspace:
  strategy: git_worktree
  source_repo: "./missing"
codex:
  command: "{fake_codex}"
---
Prompt
""",
                encoding="utf-8",
            )
            env = {"TEST_JIRA_TOKEN": "token"}
            workflow = load_workflow(workflow_path, environ=env)

            issues = validate_preflight(workflow_path, workflow.config, environ=env)

        self.assertIn("source_repo_missing", {issue.code for issue in issues})


def write_fake_codex(root: Path) -> Path:
    path = root / "fake_codex.py"
    path.write_text(
        """#!/usr/bin/env python3
import sys
if "--version" in sys.argv:
    print("fake codex 0.0")
    sys.exit(0)
print("{}")
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


if __name__ == "__main__":
    unittest.main()
