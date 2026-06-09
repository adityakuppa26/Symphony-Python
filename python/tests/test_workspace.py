from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from symphony_jira.config import HooksConfig, WorkspaceConfig
from symphony_jira.workspace import WorkspaceError, WorkspaceManager, sanitize_workspace_key


class WorkspaceTests(unittest.TestCase):
    def test_sanitize_workspace_key(self) -> None:
        self.assertEqual(sanitize_workspace_key("ICPM-73100"), "ICPM-73100")
        self.assertEqual(sanitize_workspace_key("TEAM/ABC 123"), "TEAM_ABC_123")

    def test_hook_only_creates_workspace_and_after_create_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkspaceManager(
                WorkspaceConfig(root=Path(tmp), strategy="hook_only"),
                HooksConfig(after_create="echo created"),
            )
            info = asyncio.run(manager.prepare("T-1"))

            self.assertTrue(info.created)
            self.assertTrue(info.path.is_dir())
            self.assertEqual((info.path / ".symphony" / "hooks" / "after_create.log").read_text().strip(), "created")

    def test_existing_workspace_is_reused_without_deleting_dirty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "T-1"
            workspace.mkdir()
            dirty = workspace / "dirty.txt"
            dirty.write_text("keep me", encoding="utf-8")

            manager = WorkspaceManager(WorkspaceConfig(root=root, strategy="hook_only"), HooksConfig())
            info = asyncio.run(manager.prepare("T-1"))

            self.assertFalse(info.created)
            self.assertEqual(dirty.read_text(encoding="utf-8"), "keep me")

    def test_existing_non_directory_workspace_path_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "T-1").write_text("not a dir", encoding="utf-8")
            manager = WorkspaceManager(WorkspaceConfig(root=root, strategy="hook_only"), HooksConfig())

            with self.assertRaises(WorkspaceError):
                asyncio.run(manager.prepare("T-1"))

    def test_hooks_are_rendered_with_issue_context(self) -> None:
        class DummyIssue:
            identifier = "T-99"

        with tempfile.TemporaryDirectory() as tmp:
            manager = WorkspaceManager(
                WorkspaceConfig(root=Path(tmp), strategy="hook_only"),
                HooksConfig(after_create="echo {{ issue.identifier }} > rendered.txt"),
            )

            info = asyncio.run(manager.prepare("T-99", hook_context={"issue": DummyIssue()}))

            self.assertEqual((info.path / "rendered.txt").read_text(encoding="utf-8").strip(), "T-99")


if __name__ == "__main__":
    unittest.main()
