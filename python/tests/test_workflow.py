from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from symphony_jira.models import Issue
from symphony_jira.workflow import WorkflowError, load_workflow, render_prompt, split_front_matter


class WorkflowTests(unittest.TestCase):
    def test_parse_workflow_with_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(
                """---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  jql: "project = T"
workspace:
  root: "./workspaces"
  strategy: hook_only
unknown_top_level:
  ignored: true
---
Hello {{ issue.identifier }}
""",
                encoding="utf-8",
            )

            workflow = load_workflow(path)

        self.assertEqual(workflow.config.tracker.kind, "jira")
        self.assertEqual(workflow.config.tracker.base_url, "https://jira.example.test")
        self.assertTrue(str(workflow.config.workspace.root).endswith("workspaces"))
        self.assertEqual(workflow.prompt_template, "Hello {{ issue.identifier }}")

    def test_parse_workflow_without_front_matter(self) -> None:
        config, body = split_front_matter("Plain prompt")

        self.assertEqual(config, {})
        self.assertEqual(body, "Plain prompt")

    def test_reject_non_map_front_matter(self) -> None:
        with self.assertRaises(WorkflowError):
            split_front_matter("---\n- nope\n---\nBody")

    def test_strict_prompt_rendering_rejects_unknown_variables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(
                """---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  jql: "project = T"
workspace:
  strategy: hook_only
---
{{ missing.value }}
""",
                encoding="utf-8",
            )
            workflow = load_workflow(path)
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=[],
                url="https://jira.example.test/browse/T-1",
            )

            with self.assertRaises(WorkflowError):
                render_prompt(workflow, issue)

    def test_render_prompt_keeps_base_prompt_when_planning_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "WORKFLOW.md"
            path.write_text(
                """---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  jql: "project = T"
workspace:
  strategy: hook_only
codex:
  plan_before_implementation: true
  planning_prompt: |
    Write a short plan before editing.
---
Issue {{ issue.identifier }}
""",
                encoding="utf-8",
            )
            workflow = load_workflow(path)
            issue = Issue(
                id="1",
                identifier="T-1",
                title="Title",
                status="To Do",
                labels=[],
                url="https://jira.example.test/browse/T-1",
            )

            prompt = render_prompt(workflow, issue)

        self.assertTrue(workflow.config.codex.plan_before_implementation)
        self.assertEqual(workflow.config.codex.planning_prompt.strip(), "Write a short plan before editing.")
        self.assertEqual(prompt, "Issue T-1")


if __name__ == "__main__":
    unittest.main()
