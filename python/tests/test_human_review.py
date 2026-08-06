from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from symphony_jira.config import CodexConfig
from symphony_jira.human_review import (
    HumanReviewContextError,
    build_human_review_implementation_prompt,
    build_human_review_triage_prompt,
    capture_workspace_diff,
    classify_human_review_triage,
    hash_verification_evidence,
    issue_from_frozen_snapshot,
    read_frozen_text_artifact,
    read_only_codex_config,
    write_frozen_text_artifact,
)
from symphony_jira.models import (
    RequirementArtifact,
    RequirementSource,
    RequirementsSnapshot,
    RunRecord,
)
from symphony_jira.plan_spec import PlanSpec


class HumanReviewTests(unittest.TestCase):
    def test_classify_human_review_triage_accepts_plain_json(self) -> None:
        self.assertEqual(
            classify_human_review_triage(
                '{"decision":"code_changes","reason":"Rename the helper."}'
            ),
            ("code_changes", "Rename the helper."),
        )

    def test_classify_human_review_triage_accepts_fenced_json(self) -> None:
        message = """Triage result:
```json
{"status":"replanning-required","message":"The API contract changes."}
```
"""

        self.assertEqual(
            classify_human_review_triage(message),
            ("plan_changes_required", "The API contract changes."),
        )

    def test_classify_human_review_triage_can_replan_only_automation(self) -> None:
        self.assertEqual(
            classify_human_review_triage(
                '{"decision":"automation_plan_changes_required",'
                '"reason":"Use the existing page object instead."}'
            ),
            (
                "automation_plan_changes_required",
                "Use the existing page object instead.",
            ),
        )

    def test_classify_human_review_triage_rejects_invalid_output(self) -> None:
        with self.subTest("not JSON"):
            self.assertEqual(
                classify_human_review_triage("code changes are needed"),
                ("invalid", "Triage output was not a JSON object."),
            )
        with self.subTest("unknown decision"):
            decision, reason = classify_human_review_triage(
                '{"decision":"ship_it"}'
            )
            self.assertEqual(decision, "invalid")
            self.assertIn("ship_it", reason)

    def test_read_only_codex_config_replaces_existing_sandbox(self) -> None:
        config = CodexConfig(
            args=["exec", "--json", "--sandbox", "workspace-write"],
            output_last_message_file="custom-final.md",
            output_human_review_triage_file="custom-triage.md",
        )

        result = read_only_codex_config(config)

        self.assertEqual(
            result.args,
            ["exec", "--json", "--sandbox", "read-only"],
        )
        self.assertEqual(result.output_last_message_file, "custom-triage.md")
        self.assertEqual(
            config.args,
            ["exec", "--json", "--sandbox", "workspace-write"],
        )
        self.assertEqual(config.output_last_message_file, "custom-final.md")

    def test_read_only_codex_config_removes_overrides_and_bypass_aliases(
        self,
    ) -> None:
        config = CodexConfig(
            args=[
                "exec",
                "--json",
                "-s",
                "workspace-write",
                "--sandbox=danger-full-access",
                "--sandbox",
                "workspace-write",
                "--sandbox",
                "danger-full-access",
                "--full-auto",
                "--dangerously-bypass-approvals-and-sandbox",
                "--yolo",
                "--model",
                "gpt-5",
            ]
        )

        result = read_only_codex_config(config)

        self.assertEqual(result.args.count("--sandbox"), 1)
        sandbox_index = result.args.index("--sandbox")
        self.assertEqual(result.args[sandbox_index + 1], "read-only")
        self.assertFalse(
            any(argument.startswith("--sandbox=") for argument in result.args)
        )
        for forbidden in (
            "-s",
            "workspace-write",
            "danger-full-access",
            "--full-auto",
            "--dangerously-bypass-approvals-and-sandbox",
            "--yolo",
        ):
            self.assertNotIn(forbidden, result.args)
        self.assertEqual(result.args[:2], ["exec", "--json"])
        self.assertIn("--model", result.args)
        self.assertIn("gpt-5", result.args)

    def test_capture_workspace_diff_is_deterministic_and_detects_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            alpha_sha = _create_repository(workspace / "alpha", "alpha\n")
            zeta_sha = _create_repository(workspace / "zeta", "zeta\n")
            reverse_plan = _plan_spec(
                [("zeta", zeta_sha), ("alpha", alpha_sha)]
            )
            sorted_plan = _plan_spec(
                [("alpha", alpha_sha), ("zeta", zeta_sha)]
            )

            first = capture_workspace_diff(workspace, reverse_plan)
            repeated = capture_workspace_diff(workspace, reverse_plan)
            reordered = capture_workspace_diff(workspace, sorted_plan)

            self.assertEqual(first, repeated)
            self.assertEqual(first, reordered)
            self.assertLess(
                first.content.index("## Repository alpha"),
                first.content.index("## Repository zeta"),
            )

            (workspace / "zeta" / "tracked.txt").write_text(
                "changed zeta\n", encoding="utf-8"
            )
            untracked = workspace / "alpha" / "new.txt"
            untracked.write_text("one\n", encoding="utf-8")
            changed = capture_workspace_diff(workspace, reverse_plan)

            self.assertNotEqual(first.content_hash, changed.content_hash)
            self.assertIn("+changed zeta", changed.content)
            self.assertIn("new.txt\tfile\t4\tsha256:", changed.content)
            self.assertEqual(
                changed,
                capture_workspace_diff(workspace, reverse_plan),
            )

            untracked.write_text("two\n", encoding="utf-8")
            changed_again = capture_workspace_diff(workspace, reverse_plan)
            self.assertNotEqual(changed.content_hash, changed_again.content_hash)

    def test_capture_workspace_diff_rejects_repository_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()

            plan_spec = _plan_spec([("repo", "a" * 40)])
            # Exercise capture's own defense in depth even though normal PlanSpec
            # parsing rejects this traversal before review.
            plan_spec.baseline_repository_shas[0].repository = "../outside"
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "resolves outside the workspace",
            ):
                capture_workspace_diff(
                    workspace,
                    plan_spec,
                )

    def test_capture_workspace_diff_rejects_repository_symlink_escape(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (workspace / "linked-repo").symlink_to(
                outside,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                HumanReviewContextError,
                "resolves outside the workspace",
            ):
                capture_workspace_diff(
                    workspace,
                    _plan_spec([("linked-repo", "a" * 40)]),
                )

    def test_read_frozen_text_artifact_is_bounded_and_does_not_follow_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            artifacts = workspace / ".symphony"
            artifacts.mkdir(parents=True)
            plan = artifacts / "codex-plan.md"
            plan.write_text("approved plan", encoding="utf-8")

            self.assertEqual(
                read_frozen_text_artifact(
                    workspace,
                    ".symphony/codex-plan.md",
                    label="PlanSpec",
                    required=True,
                ),
                "approved plan",
            )
            self.assertIsNone(
                read_frozen_text_artifact(
                    workspace,
                    ".symphony/missing.md",
                    label="optional review",
                )
            )
            with self.assertRaisesRegex(HumanReviewContextError, "is missing"):
                read_frozen_text_artifact(
                    workspace,
                    ".symphony/missing.md",
                    label="required review",
                    required=True,
                )
            with self.assertRaisesRegex(HumanReviewContextError, "byte"):
                read_frozen_text_artifact(
                    workspace,
                    ".symphony/codex-plan.md",
                    label="PlanSpec",
                    max_bytes=3,
                )
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "safe workspace-relative path",
            ):
                read_frozen_text_artifact(
                    workspace,
                    "../outside.md",
                    label="PlanSpec",
                )

            outside = root / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            symlink = artifacts / "symlink.md"
            symlink.symlink_to(outside)
            with self.assertRaisesRegex(HumanReviewContextError, "safely read"):
                read_frozen_text_artifact(
                    workspace,
                    ".symphony/symlink.md",
                    label="review",
                )

            hardlink = artifacts / "hardlink.md"
            hardlink.hardlink_to(outside)
            with self.assertRaisesRegex(HumanReviewContextError, "hard-linked"):
                read_frozen_text_artifact(
                    workspace,
                    ".symphony/hardlink.md",
                    label="review",
                )

    def test_write_frozen_text_artifact_replaces_leaf_link_without_following_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            artifacts = workspace / ".symphony"
            artifacts.mkdir(parents=True)
            outside = root / "outside.md"
            outside.write_text("do not change", encoding="utf-8")
            artifact = artifacts / "codex-automation-final.md"
            artifact.symlink_to(outside)

            write_frozen_text_artifact(
                workspace,
                ".symphony/codex-automation-final.md",
                "safe result",
                label="automation result",
            )

            self.assertEqual(outside.read_text(encoding="utf-8"), "do not change")
            self.assertFalse(artifact.is_symlink())
            self.assertEqual(artifact.read_text(encoding="utf-8"), "safe result")

    def test_write_frozen_text_artifact_rejects_linked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (workspace / ".symphony").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(HumanReviewContextError, "safely write"):
                write_frozen_text_artifact(
                    workspace,
                    ".symphony/codex-automation-final.md",
                    "unsafe result",
                    label="automation result",
                )
            self.assertFalse((outside / "codex-automation-final.md").exists())

    def test_runtime_manifest_evidence_binds_hook_log_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            runtime_dir = workspace / ".symphony" / "runtime"
            hook_dir = workspace / ".symphony" / "hooks"
            runtime_dir.mkdir(parents=True)
            hook_dir.mkdir(parents=True)
            hook_path = hook_dir / "verify.log"
            hook_content = b"hook output\x00\xff\n"
            hook_path.write_bytes(hook_content)
            manifest_path = runtime_dir / "verification.json"
            manifest = {
                "schema_version": "1.0",
                "issue_identifier": "T-1",
                "plan_spec_hash": "a" * 64,
                "affected_repositories": ["repo"],
                "hook": {
                    "output_path": str(hook_path),
                    "output_sha256": hashlib.sha256(hook_content).hexdigest(),
                },
                "runtime": {"checks": []},
            }
            manifest_content = json.dumps(manifest, sort_keys=True) + "\n"
            manifest_path.write_text(manifest_content, encoding="utf-8")

            self.assertEqual(
                hash_verification_evidence(workspace, manifest_path),
                hashlib.sha256(manifest_content.encode("utf-8")).hexdigest(),
            )

            hook_path.write_bytes(b"changed hook output\n")
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "verification hook log changed after its manifest was written",
            ):
                hash_verification_evidence(workspace, manifest_path)

            manifest["hook"] = {
                "output_path": None,
                "output_sha256": "b" * 64,
            }
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "log hash without an output path",
            ):
                hash_verification_evidence(workspace, manifest_path)

    def test_issue_from_frozen_snapshot_rehydrates_exact_context(self) -> None:
        snapshot = _snapshot()
        source_run = _source_run(snapshot)

        issue = issue_from_frozen_snapshot(source_run, snapshot)

        self.assertEqual(issue.id, snapshot.issue_id)
        self.assertEqual(issue.identifier, snapshot.issue_identifier)
        self.assertEqual(issue.description, "Retain approved behavior.")
        self.assertEqual(issue.status, "Completed - Addressing Human Review")
        self.assertEqual(issue.requirements_snapshot, snapshot)
        self.assertEqual(len(issue.comments), 1)
        self.assertEqual(issue.comments[0].id, "comment-7")
        self.assertEqual(issue.comments[0].author, "reviewer@example.test")
        self.assertEqual(issue.comments[0].body, "Use the established helper.")
        self.assertEqual(issue.comments[0].source, snapshot.comments[0].source)

    def test_issue_from_frozen_snapshot_validates_identity_and_hash(self) -> None:
        snapshot = _snapshot()
        source_run = _source_run(snapshot)

        with self.subTest("issue key"):
            mismatched_key = snapshot.model_copy(
                update={"issue_identifier": "OTHER-9"}
            )
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "different Jira issue",
            ):
                issue_from_frozen_snapshot(source_run, mismatched_key)

        with self.subTest("issue id"):
            mismatched_id = snapshot.model_copy(update={"issue_id": "99999"})
            mismatched_id = mismatched_id.with_content_hash()
            run_with_matching_hash = source_run.model_copy(
                update={"issue_fingerprint": mismatched_id.content_hash}
            )
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "different Jira issue",
            ):
                issue_from_frozen_snapshot(run_with_matching_hash, mismatched_id)

        with self.subTest("snapshot hash"):
            bad_hash_run = source_run.model_copy(
                update={"issue_fingerprint": "0" * 64}
            )
            with self.assertRaisesRegex(
                HumanReviewContextError,
                "hash does not match",
            ):
                issue_from_frozen_snapshot(bad_hash_run, snapshot)

    def test_human_review_prompts_include_frozen_and_review_context(self) -> None:
        snapshot = _snapshot()
        issue = issue_from_frozen_snapshot(_source_run(snapshot), snapshot)
        action = {
            "id": "review-action-1",
            "source_run_id": "source-run-1",
            "result_run_id": "result-run-2",
            "reviewer_identity": "Ada Reviewer",
            "source_url": "https://github.example.test/org/repo/pull/42",
            "comments": "Please reuse parse_widget and add a regression test.",
            "requirements_snapshot_hash": snapshot.calculate_content_hash(),
            "plan_spec_hash": "b" * 64,
            "plan_spec": '{"decision":"ready_for_approval"}',
            "automation_plan_hash": "c" * 64,
            "automation_plan": '{"decision":"update_required"}',
            "automation_result": "Added the focused browser regression.",
            "approval": {
                "approved_by": "Grace Approver",
                "approval_id": "approval-3",
            },
            "source_final_message": "Implemented the approved plan.",
            "source_review": '{"decision":"approve"}',
            "source_review_history": "Iteration 1: approved",
            "workspace_diff": "diff --git a/widget.py b/widget.py",
        }

        triage_prompt = build_human_review_triage_prompt(
            issue=issue,
            action=action,
            triage_instructions="  Compare only against frozen context.  ",
        )
        implementation_prompt = build_human_review_implementation_prompt(
            issue=issue,
            action=action,
            original_prompt="Original issue prompt.",
        )

        for expected in (
            "Review action ID: review-action-1",
            "Source completed run: source-run-1",
            "Reserved result run: result-run-2",
            "Reviewer: Ada Reviewer",
            "Review source / PR: https://github.example.test/org/repo/pull/42",
            "Please reuse parse_widget and add a regression test.",
            "Compare only against frozen context.",
            snapshot.calculate_content_hash(),
            '"issue_identifier": "T-7"',
            "b" * 64,
            '{"decision":"ready_for_approval"}',
            "c" * 64,
            '{"decision":"update_required"}',
            "Added the focused browser regression.",
            '"approved_by": "Grace Approver"',
            "Implemented the approved plan.",
            '{"decision":"approve"}',
            "Iteration 1: approved",
            "diff --git a/widget.py b/widget.py",
        ):
            self.assertIn(expected, triage_prompt)

        for expected in (
            "Original issue prompt.",
            "source-run-1",
            "review-action-1",
            "Ada Reviewer",
            "https://github.example.test/org/repo/pull/42",
            "Please reuse parse_widget and add a regression test.",
            "b" * 64,
            '{"decision":"ready_for_approval"}',
            "c" * 64,
            '{"decision":"update_required"}',
            "Added the focused browser regression.",
            "Do not edit the configured automation checkout",
            "automation_plan_changes_required",
            '"decision":"plan_changes_required"',
        ):
            self.assertIn(expected, implementation_prompt)


def _snapshot() -> RequirementsSnapshot:
    timestamp = datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)
    description_source = RequirementSource(
        issue_identifier="T-7",
        source_type="description",
        source_id="description",
        author="product-owner@example.test",
        timestamp=timestamp,
        authority="product",
    )
    comment_source = RequirementSource(
        issue_identifier="T-7",
        source_type="comment",
        source_id="comment-7",
        author="reviewer@example.test",
        timestamp=timestamp,
        authority="product",
    )
    snapshot = RequirementsSnapshot(
        issue_id="10007",
        issue_identifier="T-7",
        issue_url="https://jira.example.test/browse/T-7",
        captured_at=timestamp,
        description=RequirementArtifact(
            artifact_id="T-7:description",
            source_type="description",
            text="Retain approved behavior.",
            source=description_source,
        ),
        comments=[
            RequirementArtifact(
                artifact_id="T-7:comment:comment-7",
                source_type="comment",
                text="Use the established helper.",
                source=comment_source,
            )
        ],
    )
    return snapshot.with_content_hash()


def _source_run(snapshot: RequirementsSnapshot) -> RunRecord:
    return RunRecord(
        id="source-run-1",
        issue_id=snapshot.issue_id,
        issue_identifier=snapshot.issue_identifier,
        issue_fingerprint=snapshot.calculate_content_hash(),
        workspace_path="/tmp/T-7",
        status="completed",
        attempt=1,
        started_at=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 14, 12, 15, tzinfo=timezone.utc),
    )


def _plan_spec(repositories: list[tuple[str, str]]) -> PlanSpec:
    repository_names = [repository for repository, _ in repositories]
    primary_repository = repository_names[0]
    return PlanSpec.model_validate(
        {
            "schema_version": "1.0",
            "decision": "ready_for_approval",
            "issue_key": "T-7",
            "requirements_snapshot_hash": "f" * 64,
            "baseline_repository_shas": [
                {"repository": repository, "sha": sha}
                for repository, sha in repositories
            ],
            "requirements": [
                {
                    "id": "R-1",
                    "statement": "Retain approved behavior.",
                    "jira_sources": [
                        {
                            "issue_key": "T-7",
                            "source_type": "description",
                            "source_id": "description",
                        }
                    ],
                    "acceptance_criteria": [
                        {
                            "id": "AC-1",
                            "statement": "The behavior remains observable.",
                            "jira_sources": [
                                {
                                    "issue_key": "T-7",
                                    "source_type": "description",
                                    "source_id": "description",
                                }
                            ],
                        }
                    ],
                }
            ],
            "role_state_matrix": [
                {
                    "canonical_role": "other",
                    "role": "Reviewer",
                    "state": "completed run",
                    "expected_behavior": "Code-only feedback is applied.",
                    "requirement_ids": ["R-1"],
                    "acceptance_criterion_ids": ["AC-1"],
                }
            ],
            "affected_surface": {
                "repositories": repository_names,
                "files": [
                    {
                        "repository": primary_repository,
                        "target": "tracked.txt",
                        "change": "Apply the approved code-only adjustment.",
                    }
                ],
                "apis": [],
                "schemas": [],
                "migrations": [],
                "translations": [],
            },
            "existing_precedents": [],
            "simplest_implementation": "Apply the focused code change.",
            "assumptions": [],
            "non_goals": ["Changing product behavior."],
            "prohibited_scope": ["Unapproved requirements."],
            "test_cases": [
                {
                    "id": "TEST-1",
                    "acceptance_criterion_id": "AC-1",
                    "level": "unit",
                    "description": "Exercise the retained behavior.",
                    "expected_result": "AC-1 remains satisfied.",
                }
            ],
            "rollout": "Use the normal release path.",
            "rollback": "Revert the focused commit.",
            "compatibility": "No compatibility change.",
            "risks": [],
            "open_questions": [],
            "epic_strategy": None,
        }
    )


def _create_repository(path: Path, initial_content: str) -> str:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        capture_output=True,
        timeout=5,
    )
    (path / "tracked.txt").write_text(initial_content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "tracked.txt"],
        check=True,
        capture_output=True,
        timeout=5,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Symphony Test",
            "-c",
            "user.email=symphony@example.test",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
        timeout=5,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


if __name__ == "__main__":
    unittest.main()
