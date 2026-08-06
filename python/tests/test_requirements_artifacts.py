from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import symphony_jira.requirements_artifacts as requirements_artifacts
from symphony_jira.config import WorkflowConfig
from symphony_jira.models import (
    Issue,
    RequirementArtifact,
    RequirementDecision,
    RequirementSource,
    RequirementsSnapshot,
)
from symphony_jira.orchestrator import SingleIssueOrchestrator, build_review_prompt
from symphony_jira.requirements_artifacts import (
    RequirementsArtifactError,
    read_requirements_snapshot_artifact,
    write_requirements_snapshot_artifacts,
)


class SnapshotStore:
    def __init__(self) -> None:
        self.saved: list[RequirementsSnapshot] = []
        self.invalidated: list[tuple[str, str]] = []

    def save_requirements_snapshot(self, snapshot: RequirementsSnapshot) -> dict:
        self.saved.append(snapshot)
        return {"content_hash": snapshot.calculate_content_hash()}

    def invalidate_active_plan_approvals_for_issue(
        self,
        issue_identifier: str,
        reason: str,
    ) -> int:
        self.invalidated.append((issue_identifier, reason))
        return 1


class CurrentIssueJira:
    def __init__(self, issue: Issue) -> None:
        self.issue = issue

    async def get_issue(self, key: str, include_comments: bool = True) -> Issue:
        return self.issue


def requirement_issue(text: str) -> Issue:
    timestamp = datetime(2025, 6, 25, 10, 0, tzinfo=timezone.utc)
    source = RequirementSource(
        issue_identifier="T-1",
        source_type="description",
        source_id="description",
        field_id="description",
        author="Product Owner",
        timestamp=timestamp,
        authority="product",
        url="https://jira.example.test/browse/T-1",
    )
    artifact = RequirementArtifact(
        artifact_id="description",
        source_type="description",
        text=text,
        value=text,
        source=source,
    )
    decision = RequirementDecision(
        id="jira:T-1:description",
        text=text,
        classification="current",
        sources=[source],
    )
    snapshot = RequirementsSnapshot(
        issue_id="10001",
        issue_identifier="T-1",
        issue_url="https://jira.example.test/browse/T-1",
        description=artifact,
        current_requirements=[decision],
    ).with_content_hash()
    return Issue(
        id="10001",
        identifier="T-1",
        title="Role behavior",
        description=text,
        status="To Do",
        url="https://jira.example.test/browse/T-1",
        requirements_snapshot=snapshot,
    )


class RequirementsArtifactTests(unittest.TestCase):
    def test_writes_current_and_immutable_content_addressed_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            initial = requirement_issue("GC behavior").requirements_snapshot
            assert initial is not None

            first = write_requirements_snapshot_artifacts(workspace, initial)
            self.assertTrue(first.history_created)
            self.assertEqual(
                first.current,
                workspace / ".symphony" / "requirements-snapshot.json",
            )
            self.assertEqual(
                first.historical,
                workspace
                / ".symphony"
                / "requirements-snapshots"
                / f"{initial.content_hash}.json",
            )
            self.assertEqual(first.current.read_text(), first.historical.read_text())
            round_tripped = read_requirements_snapshot_artifact(first.current)
            self.assertEqual(round_tripped.content_hash, initial.content_hash)
            self.assertEqual(round_tripped.issue_url, "")
            assert round_tripped.description is not None
            self.assertIsNone(round_tripped.description.source.url)

            repeated = write_requirements_snapshot_artifacts(workspace, initial)
            self.assertFalse(repeated.history_created)

            changed = requirement_issue("Sub behavior").requirements_snapshot
            assert changed is not None
            second = write_requirements_snapshot_artifacts(workspace, changed)
            self.assertTrue(second.history_created)
            self.assertTrue(first.historical.exists())
            self.assertEqual(
                json.loads(second.current.read_text())["content_hash"],
                changed.content_hash,
            )
            self.assertEqual(stat.S_IMODE((workspace / ".symphony").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(first.historical.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(second.current.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(second.historical.stat().st_mode), 0o600)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow file support")
    def test_rejects_symphony_directory_symlink_without_writing_outside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            (workspace / ".symphony").symlink_to(outside, target_is_directory=True)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None

            with self.assertRaisesRegex(RequirementsArtifactError, "safely open artifact directory"):
                write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow file support")
    def test_rejects_history_directory_symlink_without_writing_outside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            symphony_dir = workspace / ".symphony"
            outside = root / "outside"
            symphony_dir.mkdir(parents=True)
            outside.mkdir()
            (symphony_dir / "requirements-snapshots").symlink_to(
                outside,
                target_is_directory=True,
            )
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None

            with self.assertRaisesRegex(RequirementsArtifactError, "safely open artifact directory"):
                write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertEqual(list(outside.iterdir()), [])

    def test_missing_nofollow_support_fails_closed_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            (workspace / ".symphony").symlink_to(outside, target_is_directory=True)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None

            with patch.object(requirements_artifacts.os, "O_NOFOLLOW", 0):
                with self.assertRaisesRegex(RequirementsArtifactError, "O_NOFOLLOW"):
                    write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_symphony_directory_owned_by_another_user(self) -> None:
        if not hasattr(os, "geteuid"):
            self.skipTest("requires POSIX ownership")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".symphony").mkdir()
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            foreign_uid = os.geteuid() + 1

            with patch.object(
                requirements_artifacts.os,
                "geteuid",
                return_value=foreign_uid,
            ):
                with self.assertRaisesRegex(
                    RequirementsArtifactError,
                    "not owned by the current user",
                ):
                    write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertEqual(list((workspace / ".symphony").iterdir()), [])

    def test_rejects_history_directory_owned_by_another_user(self) -> None:
        if not hasattr(os, "geteuid"):
            self.skipTest("requires POSIX ownership")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            history_dir = workspace / ".symphony" / "requirements-snapshots"
            history_dir.mkdir(parents=True)
            history_stat = history_dir.stat()
            history_identity = (history_stat.st_dev, history_stat.st_ino)
            real_fstat = os.fstat
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None

            def foreign_history_stat(fd: int) -> os.stat_result:
                result = real_fstat(fd)
                if (result.st_dev, result.st_ino) != history_identity:
                    return result
                values = list(result)
                values[4] = result.st_uid + 1
                return os.stat_result(values)

            with patch.object(
                requirements_artifacts.os,
                "fstat",
                side_effect=foreign_history_stat,
            ):
                with self.assertRaisesRegex(
                    RequirementsArtifactError,
                    "not owned by the current user",
                ):
                    write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertEqual(list(history_dir.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow file support")
    def test_rejects_current_target_symlink_without_overwriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            symphony_dir = workspace / ".symphony"
            (symphony_dir / "requirements-snapshots").mkdir(parents=True)
            victim = root / "victim.json"
            victim.write_text("do not change\n", encoding="utf-8")
            current = symphony_dir / "requirements-snapshot.json"
            current.symlink_to(victim)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None

            with self.assertRaisesRegex(RequirementsArtifactError, "not a regular file"):
                write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertTrue(current.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change\n")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow file support")
    def test_rejects_historical_target_symlink_without_overwriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            history_dir = workspace / ".symphony" / "requirements-snapshots"
            history_dir.mkdir(parents=True)
            victim = root / "victim.json"
            victim.write_text("do not change\n", encoding="utf-8")
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            historical = history_dir / f"{snapshot.calculate_content_hash()}.json"
            historical.symlink_to(victim)

            with self.assertRaisesRegex(RequirementsArtifactError, "safely read"):
                write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertTrue(historical.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change\n")

    def test_rejects_current_target_hardlink_without_overwriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            symphony_dir = workspace / ".symphony"
            (symphony_dir / "requirements-snapshots").mkdir(parents=True)
            victim = root / "victim.json"
            victim.write_text("do not change\n", encoding="utf-8")
            current = symphony_dir / "requirements-snapshot.json"
            os.link(victim, current)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None

            with self.assertRaisesRegex(RequirementsArtifactError, "unsafe hard links"):
                write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertEqual(current.stat().st_ino, victim.stat().st_ino)
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change\n")

    def test_rejects_historical_target_hardlink_without_overwriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            history_dir = workspace / ".symphony" / "requirements-snapshots"
            history_dir.mkdir(parents=True)
            victim = root / "victim.json"
            victim.write_text("do not change\n", encoding="utf-8")
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            historical = history_dir / f"{snapshot.calculate_content_hash()}.json"
            os.link(victim, historical)

            with self.assertRaisesRegex(RequirementsArtifactError, "unsafe hard links"):
                write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertEqual(historical.stat().st_ino, victim.stat().st_ino)
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change\n")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow file support")
    def test_atomic_current_replace_does_not_follow_raced_in_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            victim = root / "victim.json"
            victim.write_text("do not change\n", encoding="utf-8")
            current = workspace / ".symphony" / "requirements-snapshot.json"
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            inspect_target = requirements_artifacts._reject_unsafe_existing_target

            def race_in_symlink(directory_fd: int, name: str, display_path: Path) -> None:
                inspect_target(directory_fd, name, display_path)
                current.symlink_to(victim)

            with patch.object(
                requirements_artifacts,
                "_reject_unsafe_existing_target",
                side_effect=race_in_symlink,
            ):
                paths = write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertFalse(current.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change\n")
            self.assertEqual(
                read_requirements_snapshot_artifact(paths.current).content_hash,
                snapshot.content_hash,
            )

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow file support")
    def test_history_entry_substitution_is_detected_without_removing_attacker_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            victim = root / "victim.json"
            victim.write_text("do not change\n", encoding="utf-8")
            snapshot = requirement_issue("history race").requirements_snapshot
            assert snapshot is not None
            historical = (
                workspace
                / ".symphony"
                / "requirements-snapshots"
                / f"{snapshot.calculate_content_hash()}.json"
            )
            current = workspace / ".symphony" / "requirements-snapshot.json"
            real_write_all = requirements_artifacts._write_all
            calls = 0

            def substitute_history(fd: int, content: bytes) -> None:
                nonlocal calls
                calls += 1
                real_write_all(fd, content)
                if calls == 1:
                    historical.unlink()
                    historical.symlink_to(victim)

            with patch.object(
                requirements_artifacts,
                "_write_all",
                side_effect=substitute_history,
            ):
                with self.assertRaisesRegex(
                    RequirementsArtifactError,
                    "changed while being written",
                ):
                    write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertTrue(historical.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change\n")
            self.assertFalse(current.exists())

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow file support")
    def test_post_fsync_current_substitution_is_detected_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            victim = root / "victim.json"
            victim.write_text("do not change\n", encoding="utf-8")
            snapshot = requirement_issue("current race").requirements_snapshot
            assert snapshot is not None
            current = workspace / ".symphony" / "requirements-snapshot.json"
            real_fsync_directory = requirements_artifacts._fsync_directory
            substituted = False

            def substitute_after_fsync(directory_fd: int) -> None:
                nonlocal substituted
                real_fsync_directory(directory_fd)
                if current.exists() and not substituted:
                    current.unlink()
                    current.symlink_to(victim)
                    substituted = True

            with patch.object(
                requirements_artifacts,
                "_fsync_directory",
                side_effect=substitute_after_fsync,
            ):
                with self.assertRaisesRegex(
                    RequirementsArtifactError,
                    "changed while being written",
                ):
                    write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertTrue(substituted)
            self.assertTrue(current.is_symlink())
            self.assertEqual(victim.read_text(encoding="utf-8"), "do not change\n")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires no-follow file support")
    def test_read_rejects_leaf_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            paths = write_requirements_snapshot_artifacts(workspace, snapshot)
            linked = workspace / "linked-snapshot.json"
            linked.symlink_to(paths.current)

            with self.assertRaisesRegex(RequirementsArtifactError, "safely read"):
                read_requirements_snapshot_artifact(linked)

    def test_read_rejects_leaf_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            paths = write_requirements_snapshot_artifacts(workspace, snapshot)
            linked = workspace / "linked-snapshot.json"
            os.link(paths.current, linked)

            with self.assertRaisesRegex(RequirementsArtifactError, "unsafe hard links"):
                read_requirements_snapshot_artifact(linked)

    def test_read_rejects_nonowner_managed_directories(self) -> None:
        if not hasattr(os, "geteuid"):
            self.skipTest("requires POSIX ownership")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            paths = write_requirements_snapshot_artifacts(workspace, snapshot)
            real_fstat = os.fstat

            for artifact, unsafe_directory in (
                (paths.current, workspace / ".symphony"),
                (paths.historical, paths.historical.parent),
            ):
                with self.subTest(directory=unsafe_directory):
                    unsafe_stat = unsafe_directory.stat()
                    unsafe_identity = (unsafe_stat.st_dev, unsafe_stat.st_ino)

                    def foreign_directory_stat(fd: int) -> os.stat_result:
                        result = real_fstat(fd)
                        if (result.st_dev, result.st_ino) != unsafe_identity:
                            return result
                        values = list(result)
                        values[4] = result.st_uid + 1
                        return os.stat_result(values)

                    with patch.object(
                        requirements_artifacts.os,
                        "fstat",
                        side_effect=foreign_directory_stat,
                    ):
                        with self.assertRaisesRegex(
                            RequirementsArtifactError,
                            "not owned by the current user",
                        ):
                            read_requirements_snapshot_artifact(artifact)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_read_rejects_fifo_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "snapshot.json"
            os.mkfifo(fifo)

            with self.assertRaisesRegex(RequirementsArtifactError, "not a regular file"):
                read_requirements_snapshot_artifact(fifo)

    def test_read_rejects_oversized_artifact_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "snapshot.json"
            with artifact.open("wb") as handle:
                handle.truncate(
                    requirements_artifacts.MAX_REQUIREMENTS_ARTIFACT_BYTES + 1
                )

            with self.assertRaisesRegex(RequirementsArtifactError, "exceeds"):
                read_requirements_snapshot_artifact(artifact)

    def test_read_rejects_history_filename_that_does_not_match_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            paths = write_requirements_snapshot_artifacts(workspace, snapshot)
            renamed = paths.historical.with_name(f"{'0' * 64}.json")
            paths.historical.rename(renamed)

            with self.assertRaisesRegex(
                RequirementsArtifactError,
                "history filename mismatch",
            ):
                read_requirements_snapshot_artifact(renamed)

    def test_failed_private_history_creation_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            historical = (
                workspace
                / ".symphony"
                / "requirements-snapshots"
                / f"{snapshot.calculate_content_hash()}.json"
            )

            with patch.object(
                requirements_artifacts,
                "_make_file_private",
                side_effect=RequirementsArtifactError("unsafe permissions"),
            ):
                with self.assertRaisesRegex(RequirementsArtifactError, "unsafe permissions"):
                    write_requirements_snapshot_artifacts(workspace, snapshot)

            self.assertFalse(historical.exists())

    def test_failed_current_write_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            real_write_all = requirements_artifacts._write_all
            calls = 0

            def fail_second_write(fd: int, content: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated current write failure")
                real_write_all(fd, content)

            with patch.object(
                requirements_artifacts,
                "_write_all",
                side_effect=fail_second_write,
            ):
                with self.assertRaisesRegex(
                    RequirementsArtifactError,
                    "Could not write current requirements snapshot",
                ):
                    write_requirements_snapshot_artifacts(workspace, snapshot)

            symphony_dir = workspace / ".symphony"
            self.assertFalse((symphony_dir / "requirements-snapshot.json").exists())
            self.assertEqual(list(symphony_dir.glob("*.tmp")), [])
            history = list((symphony_dir / "requirements-snapshots").glob("*.json"))
            self.assertEqual(len(history), 1)

    def test_rejects_modified_immutable_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            snapshot = requirement_issue("GC behavior").requirements_snapshot
            assert snapshot is not None
            paths = write_requirements_snapshot_artifacts(workspace, snapshot)
            paths.historical.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(
                RequirementsArtifactError,
                "Immutable requirements snapshot history was modified",
            ):
                write_requirements_snapshot_artifacts(workspace, snapshot)

    def test_changed_checkpoint_versions_snapshot_and_invalidates_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            baseline = requirement_issue("GC behavior")
            current = requirement_issue("GC and Sub behavior")
            store = SnapshotStore()
            workflow = SimpleNamespace(
                config=WorkflowConfig(workspace={"root": workspace / "workspaces"})
            )
            orchestrator = SingleIssueOrchestrator(
                workflow,  # type: ignore[arg-type]
                CurrentIssueJira(current),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
            )
            orchestrator._persist_requirements_snapshot(baseline, workspace)
            assert baseline.requirements_snapshot is not None
            error = asyncio.run(
                orchestrator._requirements_change_error(
                    baseline,
                    baseline.requirements_snapshot.content_hash,
                    checkpoint="review",
                    workspace_path=workspace,
                )
            )

            self.assertIsNotNone(error)
            assert error is not None
            self.assertIn("prior PlanSpec and approval are invalid", error)
            self.assertEqual(
                [snapshot.content_hash for snapshot in store.saved],
                [
                    baseline.requirements_snapshot.content_hash,
                    current.requirements_snapshot.content_hash,  # type: ignore[union-attr]
                ],
            )
            self.assertEqual(store.invalidated, [("T-1", error)])
            current_document = json.loads(
                (workspace / ".symphony" / "requirements-snapshot.json").read_text()
            )
            self.assertEqual(
                current_document["content_hash"],
                current.requirements_snapshot.content_hash,  # type: ignore[union-attr]
            )
            history = list(
                (workspace / ".symphony" / "requirements-snapshots").glob("*.json")
            )
            self.assertEqual(len(history), 2)

    def test_review_prompt_contains_full_canonical_snapshot(self) -> None:
        issue = requirement_issue("GC acting as Sub follows Sub behavior")
        snapshot = issue.requirements_snapshot
        assert snapshot is not None

        prompt = build_review_prompt(
            issue=issue,
            workspace_path=Path("/tmp/T-1"),
            implementation_prompt="Implement it.",
            implementation_message="Done.",
            review_instructions="Review requirements.",
            requirements_snapshot_hash=snapshot.content_hash,
        )

        self.assertIn("Current canonical requirements snapshot:", prompt)
        self.assertIn('"current_requirements"', prompt)
        self.assertIn("GC acting as Sub follows Sub behavior", prompt)
        self.assertIn(snapshot.content_hash, prompt)


if __name__ == "__main__":
    unittest.main()
