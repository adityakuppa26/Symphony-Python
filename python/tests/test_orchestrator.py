from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from symphony_jira.codex_runner import CodexRunResult
from symphony_jira.config import RuntimeRepositoryConfig
from symphony_jira.dashboard import (
    prepare_human_review_context,
    prepare_verification_bypass_context,
)
from symphony_jira.models import (
    AttachmentAnalysis,
    Issue,
    IssueAttachment,
    RequirementArtifact,
    RequirementDecision,
    RequirementSource,
    RequirementsSnapshot,
)
from symphony_jira.plan_spec import parse_plan_spec
from symphony_jira.requirements_artifacts import write_requirements_snapshot_artifacts
from symphony_jira.runtime import RuntimeVerificationResult
from symphony_jira.orchestrator import (
    PollingOrchestrator,
    SingleIssueOrchestrator,
    apply_default_epic_strategy,
    build_automation_implementation_prompt,
    build_automation_planning_prompt,
    capture_automation_mutation_guard,
    classify_review_decision,
    finish_comment,
    parse_human_request,
    parse_automation_replan_request,
    planning_requirements_snapshot_prompt,
    retry_backoff_seconds,
    restore_automation_mutation_guard,
    select_dispatchable_issues,
    inspect_automation_repository,
    validate_plan_artifact,
    validate_plan_repository_baselines,
)
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow


def hydrated_test_issue(issue: Issue) -> Issue:
    if issue.requirements_snapshot is not None:
        return issue
    text = issue.description or issue.title or "Test requirement"
    source = RequirementSource(
        issue_identifier=issue.identifier,
        source_type="description",
        source_id="description",
        author="test-product-owner",
        authority="product",
    )
    description = RequirementArtifact(
        artifact_id=f"{issue.identifier}:description",
        source_type="description",
        text=text,
        source=source,
    )
    snapshot = RequirementsSnapshot(
        issue_id=issue.id,
        issue_identifier=issue.identifier,
        issue_url=issue.url,
        current_requirements=[
            RequirementDecision(
                id=f"{issue.identifier}-R",
                text=text,
                classification="current",
                sources=[source],
            ),
            RequirementDecision(
                id=f"{issue.identifier}-AC",
                text=f"{text} is observable.",
                kind="acceptance_criterion",
                classification="current",
                sources=[source],
            ),
        ],
        description=description,
    )
    return issue.model_copy(
        update={"description": text, "requirements_snapshot": snapshot}
    )


class FakeJira:
    def __init__(
        self,
        issue: Issue,
        issues: list[Issue] | None = None,
        *,
        hydrate_requirements: bool = True,
    ) -> None:
        self.issue = issue
        self.issues = issues or [issue]
        self.comments: list[str] = []
        self.hydrate_requirements = hydrate_requirements
        self.transitions: list[str] = []

    async def search_issues(self, jql: str, limit: int) -> list[Issue]:
        issues = self.issues[:limit]
        if not self.hydrate_requirements:
            return issues
        return [hydrated_test_issue(issue) for issue in issues]

    async def get_issue(self, key: str, include_comments: bool = True) -> Issue:
        for issue in self.issues:
            if issue.identifier == key:
                return hydrated_test_issue(issue) if self.hydrate_requirements else issue
        return hydrated_test_issue(self.issue) if self.hydrate_requirements else self.issue

    async def add_comment(self, key: str, body: str) -> None:
        self.comments.append(body)

    async def transition_issue(self, key: str, target_status: str) -> bool:
        self.transitions.append(target_status)
        return True


class OrderedHandoffJira(FakeJira):
    def __init__(
        self,
        issue: Issue,
        events: list[str],
        *,
        transition_succeeds: bool = True,
    ) -> None:
        super().__init__(issue)
        self.events = events
        self.transition_succeeds = transition_succeeds

    async def transition_issue(self, key: str, target_status: str) -> bool:
        self.events.append("transition")
        self.transitions.append(target_status)
        return self.transition_succeeds


class TerminalReconciliationJira(FakeJira):
    def __init__(self, issue: Issue) -> None:
        super().__init__(issue)
        self.reconciliation_fetches = 0

    async def get_issue(
        self,
        key: str,
        include_comments: bool = True,
    ) -> Issue:
        if not include_comments:
            self.reconciliation_fetches += 1
        return await super().get_issue(key, include_comments=include_comments)


def gated_automation_workflow(root: Path, *, review_iterations: int = 3):
    workflow = load_workflow(
        write_workflow(
            root,
            write_fake_codex(root),
            codex_extra=f"""
  plan_before_implementation: true
  require_plan_approval: true
  review_after_run: true
  max_review_iterations: {review_iterations}
""",
        ),
        environ={"TEST_JIRA_TOKEN": "token"},
    )
    workflow.config.automation.enabled = True
    workflow.config.automation.require_plan_approval = True
    workflow.config.automation.review_after_run = True
    workflow.config.automation.max_review_iterations = review_iterations
    return workflow


def gated_automation_issue() -> Issue:
    return Issue(
        id="10001",
        identifier="T-1",
        title="Implement and automate the focused behavior",
        description="Implement the focused behavior and add regression coverage",
        status="To Do",
        labels=["codex-ready"],
        url="https://jira.example.test/browse/T-1",
    )


def approve_development_run(store: Store, run):
    assert run.plan_spec_hash is not None
    assert run.issue_fingerprint is not None
    return store.add_approved_human_input(
        run.issue_identifier,
        run_id=run.id,
        approver_identity="dev-approver@example.test",
        plan_spec_hash=run.plan_spec_hash,
        requirements_snapshot_hash=run.issue_fingerprint,
        question=run.error,
    )


def approve_automation_run(store: Store, run):
    assert run.automation_plan_hash is not None
    assert run.issue_fingerprint is not None
    assert run.plan_spec_hash is not None
    assert run.automation_development_diff_hash is not None
    assert run.automation_repository_diff_hash is not None
    return store.add_approved_automation_human_input(
        run.issue_identifier,
        run_id=run.id,
        approver_identity="automation-approver@example.test",
        automation_plan_hash=run.automation_plan_hash,
        requirements_snapshot_hash=run.issue_fingerprint,
        development_plan_spec_hash=run.plan_spec_hash,
        development_plan_approval_id=run.plan_approval_id,
        development_workspace_diff_hash=(
            run.automation_development_diff_hash
        ),
        automation_repository_diff_hash=(
            run.automation_repository_diff_hash
        ),
        question=run.error,
    )


async def dispatch_pending_human_resume(polling: PollingOrchestrator) -> None:
    await polling.poll_once()
    pending = [item.task for item in polling.running.values()]
    if pending:
        await asyncio.gather(*pending)
    await polling.reap_finished()


async def run_to_automation_approval(
    root: Path,
    runner,
    *,
    review_iterations: int = 3,
):
    workflow = gated_automation_workflow(
        root,
        review_iterations=review_iterations,
    )
    issue = gated_automation_issue()
    jira = FakeJira(issue)
    store = Store(root / "db.sqlite3")
    planned = await SingleIssueOrchestrator(
        workflow,
        jira,
        store,
        codex_runner=runner,
    ).run_once("T-1")
    assert planned.run is not None
    approve_development_run(store, planned.run)
    polling = PollingOrchestrator(
        workflow,
        jira,
        store,
        codex_runner=runner,
    )
    await dispatch_pending_human_resume(polling)
    automation_planned = store.latest_run_for_issue("T-1")
    assert automation_planned is not None
    return (
        workflow,
        jira,
        store,
        polling,
        planned.run,
        automation_planned,
    )


class OrchestratorTests(unittest.TestCase):
    def test_automation_prompts_treat_missing_environment_as_non_blocking(self) -> None:
        issue = hydrated_test_issue(
            Issue(
                id="10001",
                identifier="T-1",
                title="Source-only automation",
                description="Add deterministic automation coverage",
                status="To Do",
                url="https://jira.example.test/browse/T-1",
            )
        )
        planning_prompt = build_automation_planning_prompt(
            issue=issue,
            planning_instructions="Plan focused coverage.",
            requirements_snapshot_hash="a" * 64,
            development_plan_message="{}",
            development_plan_spec_hash="b" * 64,
            development_diff="diff --git a/a b/a",
            development_diff_hash="c" * 64,
            development_final_message="Development completed.",
            automation_repository="automation",
            automation_repository_baseline_sha="d" * 40,
        )
        implementation_prompt = build_automation_implementation_prompt(
            issue=issue,
            implementation_instructions="Apply the focused plan.",
            development_plan_spec_hash="b" * 64,
            development_diff_hash="c" * 64,
            automation_plan_message="{}",
            automation_plan_hash="e" * 64,
            automation_repository="automation",
        )

        self.assertIn(
            "Missing runtime access, credentials, a focused suite selector, or live fixture",
            planning_prompt,
        )
        self.assertIn(
            "must never produce `needs_human`",
            planning_prompt,
        )
        self.assertIn(
            "Do not derive authoritative expected values",
            planning_prompt,
        )
        self.assertIn(
            "environment or literal fixture values",
            implementation_prompt,
        )
        self.assertIn(
            '"decision":"automation_plan_changes_required"',
            implementation_prompt,
        )
        self.assertEqual(
            parse_automation_replan_request(
                '{"decision":"automation_plan_changes_required",'
                '"reason":"fixture values are not checked in"}'
            ),
            "fixture values are not checked in",
        )
        environment_replan = parse_automation_replan_request(
            '{"decision":"needs_human","question":"Which configured automation '
            "environment should supply the authoritative fixture data, and what are "
            "the literal Project Created Date values for ABC, ABCD, DEF, and DEFG "
            "under 8265815gc_1? No runnable environment or focused-testng.xml is "
            'locally available, so these expectations cannot be derived safely."}'
        )
        self.assertIn("unavailable automation infrastructure", environment_replan or "")
        product_question_replan = parse_automation_replan_request(
            '{"decision":"needs_human","question":"Which user-visible state is expected?"}'
        )
        self.assertIn("automation planning must first resolve", product_question_replan or "")

    def test_default_codex_runner_excludes_configured_jira_credential_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = write_workflow(root, write_fake_codex(root))
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            workflow.config.tracker.auth.token_env = "CUSTOM_JIRA_SECRET"
            workflow.config.tracker.auth.email_env = "CUSTOM_JIRA_USER"
            workflow.config.runtime.enabled = True
            store = Store(root / ".symphony" / "symphony.sqlite3")

            with (
                patch("symphony_jira.orchestrator.CodexRunner") as runner_class,
                patch(
                    "symphony_jira.orchestrator.RuntimeManager"
                ) as runtime_class,
            ):
                orchestrator = SingleIssueOrchestrator(workflow, object(), store)

            runner_class.assert_called_once_with(
                excluded_environment_names={"CUSTOM_JIRA_SECRET", "CUSTOM_JIRA_USER"}
            )
            self.assertIs(orchestrator.codex_runner, runner_class.return_value)
            runtime_class.assert_called_once_with(
                workflow.config.runtime,
                excluded_environment_names={
                    "CUSTOM_JIRA_SECRET",
                    "CUSTOM_JIRA_USER",
                },
            )
            self.assertIs(orchestrator.runtime_manager, runtime_class.return_value)

    def test_single_issue_execution_uses_fake_jira_and_fake_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = write_workflow(root, fake_codex)
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                priority="High",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            jira = FakeJira(issue)
            store = Store(root / ".symphony" / "symphony.sqlite3")

            result = asyncio.run(SingleIssueOrchestrator(workflow, jira, store).run_once("T-1"))

            self.assertIsNotNone(result.run)
            assert result.run is not None
            self.assertEqual(result.run.status, "completed")
            self.assertEqual(result.run.final_message, "orchestrator final")
            self.assertEqual(result.run.verification_status, "passed")
            self.assertEqual(len(jira.comments), 2)
            self.assertIn("Codex run started for T-1", jira.comments[0])
            self.assertIn("Codex run completed for T-1", jira.comments[1])
            self.assertEqual(len(store.list_codex_events(result.run.id)), 1)
            self.assertTrue((root / "workspaces" / "T-1").is_dir())

    def test_automation_plan_and_update_run_after_development_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  review_after_run: true
  max_review_iterations: 1
""",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug with regression coverage",
                description="Please fix and cover the behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            store = Store(root / "db.sqlite3")
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                binding_store=store,
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    store,
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            assert result.workspace is not None
            self.assertEqual(result.run.status, "completed", result.run.error)
            self.assertEqual(
                runner.phases,
                [
                    "development_plan",
                    "development_implementation",
                    "automation_planning",
                    "automation_implementation",
                    "review",
                ],
            )
            self.assertTrue(
                (result.workspace.path / "automation" / "generated-test.java").is_file()
            )
            self.assertTrue(
                (result.workspace.path / workflow.config.automation.output_plan_file).is_file()
            )
            self.assertTrue(
                (result.workspace.path / workflow.config.automation.output_result_file).is_file()
            )
            self.assertIn("Validated automation plan", runner.review_prompt)
            self.assertIn("Automation:", result.run.final_message or "")
            self.assertIsNotNone(runner.binding_at_implementation_start)
            assert runner.binding_at_implementation_start is not None
            self.assertIsNotNone(
                runner.binding_at_implementation_start.automation_plan_hash
            )
            self.assertIsNotNone(
                runner.binding_at_implementation_start.automation_development_diff_hash
            )
            self.assertIsNotNone(
                runner.binding_at_implementation_start.automation_repository_diff_hash
            )
            self.assertIsNone(
                runner.binding_at_implementation_start.automation_result_hash
            )

    def test_gated_automation_flow_requires_exact_approval_between_reviews(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runner = AutomationWorkflowCodexRunner(
                    "update_required",
                    review_decisions=["approve", "approve"],
                    named_review_phases=True,
                )
                (
                    _workflow,
                    _jira,
                    store,
                    polling,
                    development_planned,
                    automation_planned,
                ) = await run_to_automation_approval(root, runner)

                self.assertEqual(development_planned.status, "blocked")
                self.assertEqual(
                    development_planned.blocked_phase,
                    "planning_approval",
                )
                self.assertEqual(automation_planned.status, "blocked")
                self.assertEqual(
                    automation_planned.blocked_phase,
                    "automation_planning_approval",
                )
                self.assertEqual(
                    runner.phases,
                    [
                        "development_plan",
                        "development_implementation",
                        "development_review",
                        "automation_planning",
                    ],
                )
                self.assertEqual(
                    runner.phases.count("automation_implementation"),
                    0,
                )
                self.assertIsNotNone(automation_planned.plan_approval_id)
                self.assertIsNotNone(automation_planned.automation_plan_hash)
                self.assertIsNone(
                    automation_planned.automation_plan_approval_id
                )
                approved_plan_hash = automation_planned.automation_plan_hash

                _input, automation_approval = approve_automation_run(
                    store,
                    automation_planned,
                )
                await dispatch_pending_human_resume(polling)

                completed = store.latest_run_for_issue("T-1")
                assert completed is not None
                self.assertEqual(completed.status, "completed", completed.error)
                self.assertNotEqual(completed.id, automation_planned.id)
                self.assertEqual(
                    completed.attempt,
                    automation_planned.attempt + 1,
                )
                self.assertEqual(
                    completed.automation_plan_hash,
                    approved_plan_hash,
                )
                self.assertEqual(
                    completed.automation_plan_approval_id,
                    automation_approval["id"],
                )
                self.assertEqual(
                    automation_approval["run_id"],
                    automation_planned.id,
                )
                self.assertEqual(
                    runner.phases,
                    [
                        "development_plan",
                        "development_implementation",
                        "development_review",
                        "automation_planning",
                        "automation_implementation",
                        "automation_review",
                    ],
                )
                self.assertEqual(runner.phases.count("development_plan"), 1)
                self.assertEqual(
                    runner.phases.count("development_implementation"),
                    1,
                )
                self.assertEqual(runner.phases.count("automation_planning"), 1)

        asyncio.run(run())

    def test_gated_noop_automation_plan_still_requires_approval(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runner = AutomationWorkflowCodexRunner(
                    "no_update_required",
                    review_decisions=["approve", "approve"],
                    named_review_phases=True,
                )
                (
                    _workflow,
                    _jira,
                    store,
                    polling,
                    _development_planned,
                    automation_planned,
                ) = await run_to_automation_approval(root, runner)

                self.assertEqual(automation_planned.status, "blocked")
                self.assertEqual(
                    automation_planned.blocked_phase,
                    "automation_planning_approval",
                )
                self.assertEqual(runner.phases.count("automation_planning"), 1)
                self.assertEqual(
                    runner.phases.count("automation_implementation"),
                    0,
                )

                approve_automation_run(store, automation_planned)
                await dispatch_pending_human_resume(polling)

                completed = store.latest_run_for_issue("T-1")
                assert completed is not None
                self.assertEqual(completed.status, "completed", completed.error)
                self.assertEqual(runner.phases.count("development_plan"), 1)
                self.assertEqual(
                    runner.phases.count("development_implementation"),
                    1,
                )
                self.assertEqual(runner.phases.count("automation_planning"), 1)
                self.assertEqual(
                    runner.phases.count("automation_implementation"),
                    0,
                )
                self.assertEqual(runner.phases.count("automation_review"), 1)

        asyncio.run(run())

    def test_automation_review_changes_reuse_the_exact_approved_plan(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runner = AutomationWorkflowCodexRunner(
                    "update_required",
                    review_decisions=["approve", "changes_required", "approve"],
                    named_review_phases=True,
                )
                (
                    _workflow,
                    _jira,
                    store,
                    polling,
                    _development_planned,
                    automation_planned,
                ) = await run_to_automation_approval(root, runner)
                _input, automation_approval = approve_automation_run(
                    store,
                    automation_planned,
                )

                await dispatch_pending_human_resume(polling)

                completed = store.latest_run_for_issue("T-1")
                assert completed is not None
                self.assertEqual(completed.status, "completed", completed.error)
                self.assertEqual(
                    completed.automation_plan_approval_id,
                    automation_approval["id"],
                )
                persisted = store.get_automation_plan_approval(
                    str(automation_approval["id"])
                )
                assert persisted is not None
                self.assertIsNone(persisted["invalidated_at"])
                self.assertEqual(runner.phases.count("development_plan"), 1)
                self.assertEqual(
                    runner.phases.count("development_implementation"),
                    1,
                )
                self.assertEqual(runner.phases.count("development_review"), 1)
                self.assertEqual(runner.phases.count("automation_planning"), 1)
                self.assertEqual(
                    runner.phases.count("automation_implementation"),
                    2,
                )
                self.assertEqual(runner.phases.count("automation_review"), 2)

        asyncio.run(run())

    def test_automation_review_replan_invalidates_approval_and_regates(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runner = AutomationWorkflowCodexRunner(
                    "update_required",
                    automation_decisions=["update_required", "update_required"],
                    review_decisions=[
                        "approve",
                        "automation_plan_changes_required",
                    ],
                    named_review_phases=True,
                )
                (
                    _workflow,
                    _jira,
                    store,
                    polling,
                    _development_planned,
                    automation_planned,
                ) = await run_to_automation_approval(root, runner)
                development_approval_id = automation_planned.plan_approval_id
                _input, old_automation_approval = approve_automation_run(
                    store,
                    automation_planned,
                )

                await dispatch_pending_human_resume(polling)

                replanned = store.latest_run_for_issue("T-1")
                assert replanned is not None
                self.assertEqual(replanned.status, "blocked", replanned.error)
                self.assertEqual(
                    replanned.blocked_phase,
                    "automation_planning_approval",
                )
                self.assertIsNone(replanned.automation_plan_approval_id)
                self.assertEqual(replanned.plan_approval_id, development_approval_id)
                old_persisted = store.get_automation_plan_approval(
                    str(old_automation_approval["id"])
                )
                assert old_persisted is not None
                self.assertIsNotNone(old_persisted["invalidated_at"])
                assert development_approval_id is not None
                development_approval = store.get_plan_approval(
                    development_approval_id
                )
                assert development_approval is not None
                self.assertIsNone(development_approval["invalidated_at"])
                self.assertEqual(runner.phases.count("development_plan"), 1)
                self.assertEqual(
                    runner.phases.count("development_implementation"),
                    1,
                )
                self.assertEqual(runner.phases.count("development_review"), 1)
                self.assertEqual(runner.phases.count("automation_planning"), 2)
                self.assertEqual(
                    runner.phases.count("automation_implementation"),
                    1,
                )
                self.assertEqual(runner.phases.count("automation_review"), 1)

        asyncio.run(run())

    def test_automation_no_update_plan_skips_automation_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Internal-only fix",
                description="Change internal behavior already covered by automation",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner("no_update_required")

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "completed", result.run.error)
            self.assertEqual(
                runner.phases,
                [
                    "development_plan",
                    "development_implementation",
                    "automation_planning",
                ],
            )
            self.assertIn(
                "No automation update was required",
                result.run.final_message or "",
            )

    def test_automation_update_requires_a_nonempty_completion_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Require an automation completion report",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                empty_automation_result=True,
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            assert result.workspace is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "automation_implementation")
            self.assertIn(
                "without a non-empty completion result",
                result.run.error or "",
            )
            self.assertIsNone(result.run.automation_result_hash)
            self.assertIn(
                "treated the attempt as incomplete",
                (
                    result.workspace.path
                    / workflow.config.automation.output_result_file
                ).read_text(encoding="utf-8"),
            )

    def test_automation_planning_clarification_resume_skips_development(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Clarify automation coverage",
                description="Implement and automate the behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                first_automation_plan_needs_human=True,
            )
            orchestrator = SingleIssueOrchestrator(
                workflow,
                FakeJira(issue),
                Store(root / "db.sqlite3"),
                codex_runner=runner,
            )

            blocked = asyncio.run(orchestrator.run_once("T-1"))
            assert blocked.run is not None
            self.assertEqual(blocked.run.status, "blocked")
            self.assertEqual(blocked.run.blocked_phase, "automation_planning")

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    force=True,
                    attempt=2,
                    previous_run=blocked.run,
                    human_input={
                        "question": blocked.run.error,
                        "response": "Add the focused regression scenario.",
                    },
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed", resumed.run.error)
            self.assertEqual(runner.phases.count("development_implementation"), 1)
            self.assertEqual(runner.phases.count("automation_planning"), 2)
            self.assertEqual(runner.phases[-1], "automation_implementation")
            self.assertIn(
                "Add the focused regression scenario.",
                runner.automation_plan_prompts[-1],
            )

    def test_automation_planning_environment_question_replans_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Plan automation without a runtime",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                first_automation_plan_needs_environment=True,
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "completed", result.run.error)
            self.assertEqual(result.run.attempt, 1)
            self.assertEqual(runner.phases.count("development_implementation"), 1)
            self.assertEqual(runner.phases.count("automation_planning"), 2)
            self.assertEqual(runner.phases.count("automation_implementation"), 1)
            self.assertIn(
                "Prior automation planning feedback",
                runner.automation_plan_prompts[-1],
            )
            self.assertIn(
                "untrusted diagnostic feedback for source-only replanning",
                runner.automation_plan_prompts[-1],
            )

    def test_automation_implementation_can_request_source_only_replanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Replan automation without a runtime",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                automation_implementation_replan=True,
            )
            orchestrator = SingleIssueOrchestrator(
                workflow,
                FakeJira(issue),
                Store(root / "db.sqlite3"),
                codex_runner=runner,
            )

            result = asyncio.run(orchestrator.run_once("T-1"))

            assert result.run is not None
            self.assertEqual(result.run.status, "completed", result.run.error)
            self.assertEqual(result.run.attempt, 1)
            self.assertEqual(runner.phases.count("development_implementation"), 1)
            self.assertEqual(runner.phases.count("automation_planning"), 2)
            self.assertEqual(runner.phases.count("automation_implementation"), 2)
            self.assertIn(
                "literal Project Created Date values",
                runner.automation_plan_prompts[-1],
            )
            self.assertIn(
                "untrusted diagnostic feedback for source-only replanning",
                runner.automation_plan_prompts[-1],
            )
            self.assertIsNotNone(result.run.automation_plan_hash)
            self.assertIsNotNone(result.run.automation_result_hash)

    def test_automation_source_only_replanning_has_a_retry_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Bound source-only replanning",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                automation_implementation_replan=True,
                persistent_automation_implementation_replan=True,
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "failed")
            self.assertEqual(result.run.blocked_phase, "automation_planning")
            self.assertIn("remained unimplementable after 1 attempt", result.run.error or "")
            self.assertIn("No live environment", result.run.error or "")
            self.assertEqual(runner.phases.count("development_implementation"), 1)
            self.assertEqual(runner.phases.count("automation_planning"), 2)
            self.assertEqual(runner.phases.count("automation_implementation"), 2)

    def test_polling_does_not_restart_exhausted_automation_replanning(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(
                    write_workflow(
                        root,
                        write_fake_codex(root),
                        codex_extra="  plan_before_implementation: true",
                    ),
                    environ={"TEST_JIRA_TOKEN": "token"},
                )
                workflow.config.automation.enabled = True
                workflow.config.agent.max_retries = 3
                issue = Issue(
                    id="10001",
                    identifier="T-1",
                    title="Do not restart bounded automation replanning",
                    description="Implement and automate the focused behavior",
                    status="To Do",
                    labels=["codex-ready"],
                    url="https://jira.example.test/browse/T-1",
                )
                store = Store(root / "db.sqlite3")
                runner = AutomationWorkflowCodexRunner(
                    "update_required",
                    automation_implementation_replan=True,
                    persistent_automation_implementation_replan=True,
                )
                polling = PollingOrchestrator(
                    workflow,
                    FakeJira(issue),
                    store,
                    codex_runner=runner,
                )

                await polling.poll_once()
                await asyncio.gather(
                    *(item.task for item in polling.running.values())
                )
                await polling.reap_finished()

                latest = store.latest_run_for_issue("T-1")
                assert latest is not None
                self.assertEqual(latest.status, "failed")
                self.assertEqual(latest.attempt, 1)
                self.assertEqual(polling.retry_queue, {})
                self.assertEqual(len(store.list_runs()), 1)
                self.assertEqual(
                    runner.phases.count("development_implementation"),
                    1,
                )

        asyncio.run(run())

    def test_automation_implementation_blocks_unplanned_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Reject automation scope drift",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                unplanned_automation_file=True,
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(
                result.run.blocked_phase,
                "automation_implementation",
            )
            self.assertIn("unplanned: unexpected-test.java", result.run.error or "")
            self.assertEqual(
                runner.phases,
                [
                    "development_plan",
                    "development_implementation",
                    "automation_planning",
                    "automation_implementation",
                ],
            )
            automation_repository = Path(result.run.workspace_path) / "automation"
            self.assertFalse(
                (automation_repository / "generated-test.java").exists()
            )
            self.assertFalse(
                (automation_repository / "unexpected-test.java").exists()
            )

            runner.unplanned_automation_file = False
            resumed = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once(
                    "T-1",
                    force=True,
                    attempt=2,
                    previous_run=result.run,
                    human_input={"response": "Retry the exact approved scope."},
                )
            )
            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed", resumed.run.error)
            self.assertFalse(
                (automation_repository / "unexpected-test.java").exists()
            )
            self.assertTrue(
                (automation_repository / "generated-test.java").is_file()
            )

    def test_automation_planning_restores_and_blocks_ignored_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Reject hidden automation planning changes",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                ignored_automation_mutation_phase="planning",
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            assert result.workspace is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "automation_planning")
            self.assertIn("Git-ignored files", result.run.error or "")
            automation_repository = result.workspace.path / "automation"
            self.assertEqual(
                (automation_repository / "preexisting-output.tmp").read_text(
                    encoding="utf-8"
                ),
                "baseline ignored output\n",
            )
            self.assertFalse(
                (automation_repository / "new-output.tmp").exists()
            )

    def test_automation_implementation_restores_and_blocks_ignored_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Reject hidden automation implementation changes",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                ignored_automation_mutation_phase="implementation",
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            assert result.workspace is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(
                result.run.blocked_phase,
                "automation_implementation",
            )
            self.assertIn("Git-ignored files", result.run.error or "")
            automation_repository = result.workspace.path / "automation"
            self.assertEqual(
                (automation_repository / "preexisting-output.tmp").read_text(
                    encoding="utf-8"
                ),
                "baseline ignored output\n",
            )
            self.assertFalse(
                (automation_repository / "new-output.tmp").exists()
            )
            self.assertFalse(
                (automation_repository / "generated-test.java").exists()
            )

    def test_automation_repository_rejects_local_git_hiding_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            automation_repository = workspace / "automation"
            ensure_test_git_repository(workspace, repository="automation")
            subprocess.run(
                ["git", "-C", str(automation_repository), "checkout", "-q", "-B", "T-1"],
                check=True,
            )
            tracked_path = automation_repository / "tracked-test.java"
            tracked_path.write_text("final class TrackedTest {}\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(automation_repository), "add", "tracked-test.java"],
                check=True,
            )
            commit_test_git_repository(automation_repository, "add tracked test")

            with self.subTest("skip-worktree index flag"):
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(automation_repository),
                        "update-index",
                        "--skip-worktree",
                        "tracked-test.java",
                    ],
                    check=True,
                )
                with self.assertRaisesRegex(Exception, "skip-worktree"):
                    inspect_automation_repository(
                        workspace,
                        "automation",
                        expected_branch_name="T-1",
                        require_clean=False,
                    )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(automation_repository),
                        "update-index",
                        "--no-skip-worktree",
                        "tracked-test.java",
                    ],
                    check=True,
                )

            with self.subTest("local exclude metadata"):
                (automation_repository / ".git" / "info" / "exclude").write_text(
                    "hidden-output.tmp\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(Exception, "info/exclude"):
                    inspect_automation_repository(
                        workspace,
                        "automation",
                        expected_branch_name="T-1",
                        require_clean=False,
                    )
                (automation_repository / ".git" / "info" / "exclude").write_text(
                    "# no local excludes\n",
                    encoding="utf-8",
                )

            with self.subTest("local config excludes file"):
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(automation_repository),
                        "config",
                        "--local",
                        "core.excludesFile",
                        str(workspace / "outside-excludes"),
                    ],
                    check=True,
                )
                with self.assertRaisesRegex(Exception, "core.excludesfile"):
                    inspect_automation_repository(
                        workspace,
                        "automation",
                        expected_branch_name="T-1",
                        require_clean=False,
                    )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(automation_repository),
                        "config",
                        "--local",
                        "--unset",
                        "core.excludesFile",
                    ],
                    check=True,
                )

            with self.subTest("file mode tracking disabled"):
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(automation_repository),
                        "config",
                        "--local",
                        "core.filemode",
                        "false",
                    ],
                    check=True,
                )
                with self.assertRaisesRegex(Exception, "core.filemode"):
                    inspect_automation_repository(
                        workspace,
                        "automation",
                        expected_branch_name="T-1",
                        require_clean=False,
                    )

    def test_automation_mutation_guard_preserves_retained_file_newly_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            automation_repository = workspace / "automation"
            ensure_test_git_repository(workspace, repository="automation")
            ignore_file = automation_repository / ".gitignore"
            ignore_file.write_text("*.baseline.tmp\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(automation_repository), "add", ".gitignore"],
                check=True,
            )
            commit_test_git_repository(automation_repository, "add ignore rules")
            retained_file = automation_repository / "retained-test.java"
            retained_file.write_text(
                "final class RetainedTest {}\n",
                encoding="utf-8",
            )
            guard = capture_automation_mutation_guard(workspace, "automation")

            ignore_file.write_text(
                "*.baseline.tmp\nretained-test.java\nnew-output.tmp\n",
                encoding="utf-8",
            )
            retained_file.write_text("unsafe replacement\n", encoding="utf-8")
            new_ignored_file = automation_repository / "new-output.tmp"
            new_ignored_file.write_text("new hidden output\n", encoding="utf-8")

            error = restore_automation_mutation_guard(
                workspace,
                "automation",
                guard,
            )

            self.assertIsNotNone(error)
            self.assertIn("pre-existing dirty file became ignored", error or "")
            self.assertEqual(
                retained_file.read_text(encoding="utf-8"),
                "final class RetainedTest {}\n",
            )
            self.assertFalse(new_ignored_file.exists())

    def test_automation_mutation_guard_preserves_staged_addition_newly_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            automation_repository = workspace / "automation"
            ensure_test_git_repository(workspace, repository="automation")
            ignore_file = automation_repository / ".gitignore"
            ignore_file.write_text("*.baseline.tmp\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(automation_repository), "add", ".gitignore"],
                check=True,
            )
            commit_test_git_repository(automation_repository, "add ignore rules")
            retained_file = automation_repository / "retained-test.java"
            retained_file.write_text(
                "final class RetainedTest {}\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(automation_repository),
                    "add",
                    "retained-test.java",
                ],
                check=True,
            )
            guard = capture_automation_mutation_guard(workspace, "automation")

            subprocess.run(
                [
                    "git",
                    "-C",
                    str(automation_repository),
                    "reset",
                    "-q",
                    "HEAD",
                    "--",
                    "retained-test.java",
                ],
                check=True,
            )
            ignore_file.write_text(
                "*.baseline.tmp\nretained-test.java\n",
                encoding="utf-8",
            )
            retained_file.write_text("unsafe replacement\n", encoding="utf-8")

            error = restore_automation_mutation_guard(
                workspace,
                "automation",
                guard,
            )

            self.assertIsNotNone(error)
            self.assertIn("pre-existing dirty file became ignored", error or "")
            self.assertEqual(
                retained_file.read_text(encoding="utf-8"),
                "final class RetainedTest {}\n",
            )

    def test_automation_mutation_guard_clears_new_index_hiding_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            automation_repository = workspace / "automation"
            ensure_test_git_repository(workspace, repository="automation")
            tracked_file = automation_repository / "tracked-test.java"
            tracked_file.write_text("final class TrackedTest {}\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(automation_repository), "add", "tracked-test.java"],
                check=True,
            )
            commit_test_git_repository(automation_repository, "add tracked test")
            guard = capture_automation_mutation_guard(workspace, "automation")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(automation_repository),
                    "update-index",
                    "--skip-worktree",
                    "tracked-test.java",
                ],
                check=True,
            )

            error = restore_automation_mutation_guard(
                workspace,
                "automation",
                guard,
            )
            index_state = subprocess.run(
                ["git", "-C", str(automation_repository), "ls-files", "-v"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

            self.assertIsNotNone(error)
            self.assertIn("index hiding flags changed", error or "")
            self.assertEqual(index_state, "H tracked-test.java\n")

    def test_automation_mutation_guard_refuses_a_redirected_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            automation_repository = workspace / "automation"
            ensure_test_git_repository(workspace, repository="automation")
            guard = capture_automation_mutation_guard(workspace, "automation")
            original_repository = workspace / "automation-original"
            automation_repository.rename(original_repository)
            outside_directory = workspace / "outside"
            outside_directory.mkdir()
            sentinel = outside_directory / "sentinel.txt"
            sentinel.write_text("untouched\n", encoding="utf-8")
            automation_repository.symlink_to(
                outside_directory,
                target_is_directory=True,
            )

            error = restore_automation_mutation_guard(
                workspace,
                "automation",
                guard,
            )

            self.assertIsNotNone(error)
            self.assertIn("refused to follow", error or "")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")

    def test_automation_plan_rejects_a_git_ignored_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Reject an ignored planned automation source",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                ignore_planned_automation_path=True,
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "automation_planning")
            self.assertIn("ignored by Git", result.run.error or "")
            self.assertNotIn("automation_implementation", runner.phases)

    def test_automation_resume_rejects_development_drift_before_replanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Reject development drift on automation retry",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                unplanned_automation_file=True,
            )
            orchestrator = SingleIssueOrchestrator(
                workflow,
                FakeJira(issue),
                Store(root / "db.sqlite3"),
                codex_runner=runner,
            )

            blocked = asyncio.run(orchestrator.run_once("T-1"))
            assert blocked.run is not None
            assert blocked.workspace is not None
            (blocked.workspace.path / "repo" / "development.py").write_text(
                "IMPLEMENTED = True\nUNAUTHORIZED_DRIFT = True\n",
                encoding="utf-8",
            )
            planning_count = runner.phases.count("automation_planning")

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    force=True,
                    attempt=2,
                    previous_run=blocked.run,
                    human_input={"response": "Retry automation."},
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "planning")
            self.assertIn("Development workspace changed", resumed.run.error or "")
            self.assertIn("not accepted as automation output", resumed.run.error or "")
            self.assertIsNone(resumed.run.automation_plan_hash)
            self.assertIsNone(resumed.run.automation_development_diff_hash)
            self.assertIsNone(resumed.run.automation_repository_diff_hash)
            self.assertIsNone(resumed.run.automation_result_hash)
            self.assertEqual(
                runner.phases.count("automation_planning"),
                planning_count,
            )

            runner.unplanned_automation_file = False
            (blocked.workspace.path / "repo" / "development.py").unlink()
            recovered = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    force=True,
                    attempt=3,
                    previous_run=resumed.run,
                    human_input={
                        "response": (
                            "Replan the development work and remove any change that "
                            "is not justified by the Jira requirements."
                        )
                    },
                )
            )

            assert recovered.run is not None
            self.assertEqual(recovered.run.status, "completed", recovered.run.error)
            self.assertEqual(runner.phases.count("development_plan"), 2)
            self.assertEqual(runner.phases.count("development_implementation"), 2)
            self.assertNotIn(
                "UNAUTHORIZED_DRIFT",
                (blocked.workspace.path / "repo" / "development.py").read_text(
                    encoding="utf-8"
                ),
            )

    def test_automation_development_mutation_transitions_to_replanning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="  plan_before_implementation: true",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Recover from automation development mutation",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                change_development_during_automation=True,
            )
            orchestrator = SingleIssueOrchestrator(
                workflow,
                FakeJira(issue),
                Store(root / "db.sqlite3"),
                codex_runner=runner,
            )

            blocked = asyncio.run(orchestrator.run_once("T-1"))

            assert blocked.run is not None
            assert blocked.workspace is not None
            self.assertEqual(blocked.run.status, "blocked")
            self.assertEqual(blocked.run.blocked_phase, "planning")
            self.assertIn(
                "Automation implementation changed a development repository",
                blocked.run.error or "",
            )
            self.assertIn(
                "not accepted as automation output",
                blocked.run.error or "",
            )
            self.assertIsNone(blocked.run.automation_plan_hash)
            self.assertIsNone(blocked.run.automation_development_diff_hash)
            self.assertIsNone(blocked.run.automation_repository_diff_hash)
            self.assertIsNone(blocked.run.automation_result_hash)
            self.assertFalse(
                (blocked.workspace.path / "automation" / "generated-test.java").exists()
            )
            self.assertIn(
                "AUTOMATION_SCOPE_VIOLATION",
                (blocked.workspace.path / "repo" / "development.py").read_text(
                    encoding="utf-8"
                ),
            )

            runner.change_development_during_automation = False
            (blocked.workspace.path / "repo" / "development.py").unlink()
            recovered = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    force=True,
                    attempt=2,
                    previous_run=blocked.run,
                    human_input={
                        "response": (
                            "Replan from Jira and remove the unauthorized automation-phase "
                            "development edit."
                        )
                    },
                )
            )

            assert recovered.run is not None
            self.assertEqual(recovered.run.status, "completed", recovered.run.error)
            self.assertEqual(runner.phases.count("development_plan"), 2)
            self.assertEqual(runner.phases.count("development_implementation"), 2)
            self.assertNotIn(
                "AUTOMATION_SCOPE_VIOLATION",
                (blocked.workspace.path / "repo" / "development.py").read_text(
                    encoding="utf-8"
                ),
            )

    def test_review_development_change_replans_automation_before_second_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  review_after_run: true
  max_review_iterations: 2
""",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Refresh automation after review",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            events: list[str] = []
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                review_decisions=["changes_required", "approve"],
                change_development_on_regeneration=True,
                events=events,
            )
            orchestrator = SingleIssueOrchestrator(
                workflow,
                FakeJira(issue),
                Store(root / "db.sqlite3"),
                codex_runner=runner,
            )
            run_hook = orchestrator.workspace_manager.run_hook

            async def record_run_hook(name, *args, **kwargs):
                if name == "verify":
                    events.append("verify")
                return await run_hook(name, *args, **kwargs)

            with patch.object(
                orchestrator.workspace_manager,
                "run_hook",
                side_effect=record_run_hook,
            ):
                result = asyncio.run(orchestrator.run_once("T-1"))

            assert result.run is not None
            assert result.workspace is not None
            self.assertEqual(result.run.status, "completed", result.run.error)
            self.assertEqual(
                runner.phases,
                [
                    "development_plan",
                    "development_implementation",
                    "automation_planning",
                    "automation_implementation",
                    "review",
                    "regeneration",
                    "automation_planning",
                    "automation_implementation",
                    "review",
                ],
            )
            self.assertEqual(
                events,
                [
                    "development_plan",
                    "development_implementation",
                    "automation_planning",
                    "automation_implementation",
                    "verify",
                    "review",
                    "regeneration",
                    "automation_planning",
                    "automation_implementation",
                    "verify",
                    "review",
                ],
            )
            self.assertEqual(len(runner.automation_plan_diff_hashes), 2)
            self.assertNotEqual(
                runner.automation_plan_diff_hashes[0],
                runner.automation_plan_diff_hashes[1],
            )
            retained_plan = json.loads(
                (
                    result.workspace.path
                    / workflow.config.automation.output_plan_file
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                retained_plan["development_workspace_diff_hash"],
                runner.automation_plan_diff_hashes[-1],
            )
            self.assertIsNotNone(result.run.automation_plan_hash)
            self.assertEqual(result.run.verification_status, "passed")

    def test_review_replan_can_reconcile_retained_update_to_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  review_after_run: true
  max_review_iterations: 2
""",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Remove obsolete derived automation",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                automation_decisions=["update_required", "no_update_required"],
                review_decisions=["changes_required", "approve"],
                change_development_on_regeneration=True,
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            assert result.workspace is not None
            self.assertEqual(result.run.status, "completed", result.run.error)
            self.assertEqual(
                runner.phases,
                [
                    "development_plan",
                    "development_implementation",
                    "automation_planning",
                    "automation_implementation",
                    "review",
                    "regeneration",
                    "automation_planning",
                    "review",
                ],
            )
            self.assertFalse(
                (result.workspace.path / "automation" / "generated-test.java").exists()
            )
            self.assertFalse(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(result.workspace.path / "automation"),
                        "status",
                        "--porcelain=v1",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
            self.assertIn(
                "baseline checkout, without those changes",
                " ".join(runner.automation_plan_prompts[-1].split()),
            )
            self.assertIn(
                "No automation update was required",
                result.run.final_message or "",
            )

    def test_development_replanning_invalidates_derived_automation_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  review_after_run: true
  max_review_iterations: 1
""",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Return safely to development planning",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                review_decisions=["plan_changes_required"],
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            assert result.workspace is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "planning")
            self.assertIsNone(result.run.automation_plan_hash)
            self.assertIsNone(result.run.automation_repository_diff_hash)
            self.assertFalse(
                (result.workspace.path / "automation" / "generated-test.java").exists()
            )
            self.assertFalse(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(result.workspace.path / "automation"),
                        "status",
                        "--porcelain=v1",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
            )

    def test_automation_enabled_development_planning_question_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root, write_fake_codex(root)),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Clarify development scope",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = PlanningQuestionThenPlanCodexRunner()

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "planning")
            self.assertIn("Where should the column go?", result.run.error or "")
            self.assertEqual(runner.prompts_seen, ["plan_question"])

    def test_post_automation_environment_resume_reuses_bound_automation_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_runtime_workflow(
                    root,
                    write_fake_codex(root),
                    required=True,
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Resume verification after automation",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner("update_required")
            runtime_manager = FakeRuntimeManager("environment_blocked")
            orchestrator = SingleIssueOrchestrator(
                workflow,
                FakeJira(issue),
                Store(root / "db.sqlite3"),
                codex_runner=runner,
                runtime_manager=runtime_manager,
            )

            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            assert first.workspace is not None
            self.assertEqual(first.run.status, "blocked")
            self.assertEqual(
                first.run.blocked_phase,
                "verification_environment",
            )
            self.assertIsNotNone(first.run.automation_plan_hash)
            first_automation_hash = first.run.automation_plan_hash
            self.assertTrue(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(first.workspace.path / "automation"),
                        "status",
                        "--porcelain=v1",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
            )

            runtime_manager.status = "passed"
            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    previous_run=first.run,
                    human_input={
                        "response": "The operator fixed the local runtime environment."
                    },
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed", resumed.run.error)
            self.assertEqual(
                resumed.run.automation_plan_hash,
                first_automation_hash,
            )
            self.assertEqual(len(runtime_manager.calls), 2)
            self.assertEqual(
                runner.phases,
                [
                    "development_plan",
                    "development_implementation",
                    "automation_planning",
                    "automation_implementation",
                ],
            )
            self.assertEqual(len(runner.automation_plan_prompts), 1)

    def test_post_automation_resume_requires_automation_to_remain_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_runtime_workflow(
                    root,
                    write_fake_codex(root),
                    required=True,
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Keep the automation continuation bound",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner("update_required")
            runtime_manager = FakeRuntimeManager("environment_blocked")
            store = Store(root / "db.sqlite3")
            orchestrator = SingleIssueOrchestrator(
                workflow,
                FakeJira(issue),
                store,
                codex_runner=runner,
                runtime_manager=runtime_manager,
            )

            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            expected_bindings = (
                first.run.automation_plan_hash,
                first.run.automation_development_diff_hash,
                first.run.automation_repository_diff_hash,
                first.run.automation_result_hash,
            )
            phase_count = len(runner.phases)
            workflow.config.automation.enabled = False

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    force=True,
                    attempt=2,
                    previous_run=first.run,
                    human_input={"response": "The runtime environment is ready."},
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "automation_planning")
            self.assertIn("Re-enable", resumed.run.error or "")
            self.assertEqual(
                (
                    resumed.run.automation_plan_hash,
                    resumed.run.automation_development_diff_hash,
                    resumed.run.automation_repository_diff_hash,
                    resumed.run.automation_result_hash,
                ),
                expected_bindings,
            )
            self.assertEqual(len(runner.phases), phase_count)

    def test_post_automation_resume_rejects_a_missing_noop_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_runtime_workflow(
                    root,
                    write_fake_codex(root),
                    required=True,
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Retain no-op result across verification",
                description="Implement behavior already covered by automation",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner("no_update_required")
            runtime_manager = FakeRuntimeManager("environment_blocked")
            orchestrator = SingleIssueOrchestrator(
                workflow,
                FakeJira(issue),
                Store(root / "db.sqlite3"),
                codex_runner=runner,
                runtime_manager=runtime_manager,
            )

            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            assert first.workspace is not None
            self.assertEqual(first.run.blocked_phase, "verification_environment")
            self.assertIsNotNone(first.run.automation_result_hash)
            (
                first.workspace.path
                / workflow.config.automation.output_result_file
            ).unlink()
            runtime_manager.status = "passed"

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    force=True,
                    attempt=2,
                    previous_run=first.run,
                    human_input={"response": "The runtime environment is ready."},
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "automation_planning")
            self.assertIn("result artifact is missing", resumed.run.error or "")

    def test_post_automation_review_resume_skips_writable_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  review_after_run: true
  max_review_iterations: 2
""",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            workflow.config.automation.enabled = True
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Resume the retained review",
                description="Implement and automate the focused behavior",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AutomationWorkflowCodexRunner(
                "update_required",
                review_decisions=["needs_human", "approve"],
            )
            orchestrator = SingleIssueOrchestrator(
                workflow,
                FakeJira(issue),
                Store(root / "db.sqlite3"),
                codex_runner=runner,
            )

            blocked = asyncio.run(orchestrator.run_once("T-1"))
            assert blocked.run is not None
            self.assertEqual(blocked.run.status, "blocked")
            self.assertEqual(blocked.run.blocked_phase, "review")

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    previous_run=blocked.run,
                    human_input={
                        "response": "The retained evidence answers the review question."
                    },
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed", resumed.run.error)
            self.assertEqual(runner.phases.count("development_implementation"), 1)
            self.assertEqual(runner.phases.count("automation_planning"), 1)
            self.assertEqual(runner.phases.count("automation_implementation"), 1)
            self.assertEqual(runner.phases.count("review"), 2)

    def test_failed_verify_hook_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = write_workflow(
                root,
                fake_codex,
                codex_extra="  review_after_run: true",
            )
            workflow_path.write_text(
                workflow_path.read_text(encoding="utf-8")
                .replace("echo verify ok", "exit 7")
                .replace(
                    '  active_statuses: ["To Do"]',
                    '  active_statuses: ["To Do"]\n  handoff_status: Done',
                ),
                encoding="utf-8",
            )
            workflow = load_workflow(
                workflow_path, environ={"TEST_JIRA_TOKEN": "token"}
            )
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            jira = FakeJira(issue)
            store = Store(root / ".symphony" / "symphony.sqlite3")
            runner = MessageCodexRunner(
                ["Implementation complete.", '{"decision":"approve"}']
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow, jira, store, codex_runner=runner
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "completed")
            self.assertIsNone(result.run.blocked_phase)
            self.assertEqual(result.run.verification_status, "failed")
            self.assertIsNone(result.run.error)
            self.assertEqual(len(jira.comments), 2)
            self.assertIn("Codex run completed for T-1", jira.comments[-1])
            self.assertIn("- `verify`: failed", jira.comments[-1])
            self.assertEqual(runner.messages, [])
            review_path = root / "workspaces" / "T-1" / ".symphony" / "codex-review.md"
            self.assertTrue(review_path.is_file())
            self.assertIn('"decision":"approve"', review_path.read_text(encoding="utf-8"))
            logs = store.list_logs(run_id=result.run.id)
            self.assertTrue(
                any(
                    log["level"] == "warning"
                    and "verification is advisory" in log["message"]
                    and log["path"] == result.run.verification_output_path
                    for log in logs
                )
            )
            self.assertEqual(jira.transitions, ["Done"])

    def test_required_verify_hook_blocks_before_runtime_or_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = write_workflow(root, write_fake_codex(root))
            workflow_path.write_text(
                workflow_path.read_text(encoding="utf-8")
                .replace("  verify: |\n", "  verify_required: true\n  verify: |\n")
                .replace("    echo verify ok", "    exit 7"),
                encoding="utf-8",
            )
            workflow = load_workflow(
                workflow_path,
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=MessageCodexRunner(["Implementation complete."]),
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "verification")
            self.assertEqual(result.run.verification_status, "failed")
            self.assertIn("Required verification hook failed", result.run.error or "")

    def test_required_runtime_verification_passes_exact_plan_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, runtime_manager, _ = run_runtime_case(
                root,
                runtime_status="passed",
                required=True,
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "completed")
            self.assertEqual(result.run.verification_status, "passed")
            self.assertEqual(len(runtime_manager.calls), 1)
            self.assertEqual(runtime_manager.calls[0]["repositories"], ("repo",))
            self.assertEqual(
                runtime_manager.calls[0]["source_repositories"],
                ("repo",),
            )
            manifest_path = Path(result.run.verification_output_path or "")
            self.assertIn(result.run.id, manifest_path.name)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["affected_repositories"], ["repo"])
            self.assertEqual(manifest["runtime"]["status"], "passed")
            self.assertEqual(manifest["hook"]["status"], "passed")
            hook_log_path = Path(manifest["hook"]["output_path"])
            self.assertEqual(
                manifest["hook"]["output_sha256"],
                hashlib.sha256(hook_log_path.read_bytes()).hexdigest(),
            )
            runtime_check = manifest["runtime"]["checks"][0]
            runtime_log_path = Path(runtime_check["log_path"])
            self.assertEqual(
                runtime_check["log_sha256"],
                hashlib.sha256(runtime_log_path.read_bytes()).hexdigest(),
            )

    def test_runtime_translates_plan_repository_path_to_config_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, runtime_manager, _ = run_runtime_case(
                root,
                runtime_status="passed",
                required=True,
                shutdown_after_handoff=True,
                repository_key="backend",
                workspace_subdir="services/api",
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "completed", result.run.error)
            self.assertEqual(
                runtime_manager.calls[0]["repositories"],
                ("backend",),
            )
            self.assertEqual(
                runtime_manager.calls[0]["source_repositories"],
                ("backend",),
            )
            self.assertEqual(
                runtime_manager.shutdown_calls[0]["repositories"],
                ("backend",),
            )
            manifest = json.loads(
                Path(result.run.verification_output_path or "").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["affected_repositories"],
                ["services/api"],
            )
            self.assertEqual(
                manifest["runtime"]["checks"][0]["repository"],
                "backend",
            )
            self.assertEqual(
                manifest["runtime"]["checks"][0]["workspace_subdir"],
                "services/api",
            )

    def test_required_runtime_test_failure_blocks_before_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, _, jira = run_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "verification")
            self.assertEqual(result.run.verification_status, "test_failed")
            self.assertIn("repo (test_failed)", result.run.error or "")
            self.assertEqual(
                len(result.run.verification_workspace_diff_hash or ""),
                64,
            )
            evidence_path = Path(result.run.verification_output_path or "")
            self.assertEqual(
                result.run.verification_evidence_sha256,
                hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(jira.transitions, [])

    def test_required_runtime_environment_block_blocks_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, _, _ = run_runtime_case(
                root,
                runtime_status="environment_blocked",
                required=True,
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(
                result.run.blocked_phase,
                "verification_environment",
            )
            self.assertEqual(
                result.run.verification_status,
                "environment_blocked",
            )

    def test_environment_resume_preserves_plan_and_reruns_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, runner, _ = create_runtime_case(
                root,
                runtime_status="environment_blocked",
                required=True,
            )
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            self.assertEqual(
                first.run.blocked_phase,
                "verification_environment",
            )
            first_plan_hash = first.run.plan_spec_hash
            first_manifest_path = first.run.verification_output_path

            runtime_manager.status = "passed"
            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input={
                        "response": "The operator fixed the local runtime environment."
                    },
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed")
            self.assertEqual(resumed.run.plan_spec_hash, first_plan_hash)
            self.assertEqual(len(runtime_manager.calls), 2)
            self.assertNotEqual(
                resumed.run.verification_output_path,
                first_manifest_path,
            )
            self.assertTrue(Path(first_manifest_path or "").is_file())
            self.assertEqual(
                runner.prompts_seen,
                ["plan", "implementation"],
            )
            logs = orchestrator.store.list_logs(run_id=resumed.run.id)
            self.assertTrue(
                any(
                    "Skipped Codex implementation for an environment-only resume"
                    in log["message"]
                    for log in logs
                )
            )

    def test_human_verification_bypass_hands_off_without_rerunning_codex_or_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, runner, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
                shutdown_after_handoff=True,
            )
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            self.assertEqual(first.run.status, "blocked")
            self.assertEqual(first.run.blocked_phase, "verification")
            first_manifest_path = first.run.verification_output_path

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=verification_bypass_input(
                        orchestrator,
                        first.run,
                    ),
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed", resumed.run.error)
            self.assertIsNone(resumed.run.blocked_phase)
            self.assertEqual(resumed.run.verification_status, "test_failed")
            self.assertEqual(
                resumed.run.verification_output_path,
                first_manifest_path,
            )
            self.assertEqual(runner.prompts_seen, ["plan", "implementation"])
            self.assertEqual(len(runtime_manager.calls), 1)
            self.assertEqual(jira.transitions, ["Done"])
            self.assertEqual(len(runtime_manager.shutdown_calls), 1)
            self.assertEqual(
                runtime_manager.shutdown_calls[0]["repositories"],
                ("repo",),
            )
            self.assertIn(
                "Verification override:",
                resumed.run.final_message or "",
            )
            self.assertIn(
                "operator@example.test",
                resumed.run.final_message or "",
            )
            logs = orchestrator.store.list_logs(run_id=resumed.run.id)
            self.assertTrue(
                any(
                    log["level"] == "warning"
                    and "explicitly overridden by operator@example.test"
                    in log["message"]
                    and log["path"] == first_manifest_path
                    for log in logs
                )
            )

    def test_generic_human_response_with_old_bypass_prefix_does_not_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, runner, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            runtime_manager.status = "passed"

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input={
                        "action": "response",
                        "run_id": first.run.id,
                        "response": (
                            "Verification bypass approved for handoff by: "
                            "operator@example.test"
                        ),
                    },
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed", resumed.run.error)
            self.assertEqual(resumed.run.verification_status, "passed")
            self.assertEqual(
                runner.prompts_seen,
                ["plan", "implementation", "implementation"],
            )
            self.assertEqual(len(runtime_manager.calls), 2)
            self.assertEqual(jira.transitions, ["Done"])
            self.assertNotIn(
                "Verification override:",
                resumed.run.final_message or "",
            )

    def test_verification_bypass_blocks_when_workspace_diff_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, runner, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            self.assertIsNotNone(
                first.run.verification_workspace_diff_hash,
                first.run.error,
            )
            bypass_input = verification_bypass_input(orchestrator, first.run)
            repository_path = Path(first.run.workspace_path) / "repo"
            (repository_path / "unverified-drift.txt").write_text(
                "changed after approval\n",
                encoding="utf-8",
            )

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=bypass_input,
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "verification")
            self.assertIn(
                "workspace diff changed after approval",
                resumed.run.error or "",
            )
            self.assertEqual(runner.prompts_seen, ["plan", "implementation"])
            self.assertEqual(len(runtime_manager.calls), 1)
            self.assertEqual(jira.transitions, [])

    def test_verification_bypass_binding_includes_managed_sibling_repository(self) -> None:
        class SiblingRepositoryRunner(PlanThenImplementCodexRunner):
            async def run(self, prompt, workspace_path, config, **kwargs):
                if not self.prompts_seen:
                    sibling = Path(workspace_path) / "sibling"
                    sibling.mkdir(parents=True, exist_ok=True)
                    subprocess.run(
                        ["git", "init", "-q", str(sibling)],
                        check=True,
                    )
                    commit_test_git_repository(sibling, "sibling baseline")
                return await super().run(
                    prompt,
                    workspace_path,
                    config,
                    **kwargs,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, _, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )
            runner = SiblingRepositoryRunner()
            orchestrator.codex_runner = runner
            orchestrator.config.runtime.repositories["sibling"] = (
                RuntimeRepositoryConfig(
                    workspace_subdir=Path("sibling"),
                    source_env="SIBLING_SRC",
                    service="sibling",
                    mount_target="/sibling",
                    verification_profile="tests",
                )
            )
            sibling_path = root / "workspaces" / "T-1" / "sibling"

            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            self.assertIsNotNone(
                first.run.verification_workspace_diff_hash,
                first.run.error,
            )
            bypass_input = verification_bypass_input(orchestrator, first.run)
            (sibling_path / "unverified-drift.txt").write_text(
                "changed outside the planned repository\n",
                encoding="utf-8",
            )

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=bypass_input,
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "verification")
            self.assertIn(
                "workspace diff changed after approval",
                resumed.run.error or "",
            )
            self.assertEqual(runner.prompts_seen, ["plan", "implementation"])
            self.assertEqual(len(runtime_manager.calls), 1)
            self.assertEqual(jira.transitions, [])

    def test_verification_bypass_blocks_when_evidence_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, runner, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            bypass_input = verification_bypass_input(orchestrator, first.run)
            evidence_path = Path(first.run.verification_output_path or "")
            evidence_path.write_text(
                evidence_path.read_text(encoding="utf-8") + "\nchanged\n",
                encoding="utf-8",
            )

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=bypass_input,
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "verification")
            self.assertIn(
                "evidence changed after approval",
                resumed.run.error or "",
            )
            self.assertEqual(runner.prompts_seen, ["plan", "implementation"])
            self.assertEqual(len(runtime_manager.calls), 1)
            self.assertEqual(jira.transitions, [])

    def test_verification_bypass_blocks_when_runtime_log_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, runner, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            bypass_input = verification_bypass_input(orchestrator, first.run)
            evidence_path = Path(first.run.verification_output_path or "")
            manifest = json.loads(evidence_path.read_text(encoding="utf-8"))
            runtime_log_path = Path(
                manifest["runtime"]["checks"][0]["log_path"]
            )
            runtime_log_path.write_bytes(
                runtime_log_path.read_bytes() + b"changed after approval\n"
            )

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=bypass_input,
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "verification")
            self.assertIn(
                "runtime verification log changed after its manifest was written",
                resumed.run.error or "",
            )
            self.assertEqual(runner.prompts_seen, ["plan", "implementation"])
            self.assertEqual(len(runtime_manager.calls), 1)
            self.assertEqual(jira.transitions, [])

    def test_verification_bypass_still_runs_configured_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, _, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )
            runner = VerificationBypassReviewRunner(["approve"])
            orchestrator.codex_runner = runner
            orchestrator.config.codex.review_after_run = True
            orchestrator.config.codex.max_review_iterations = 2
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=verification_bypass_input(
                        orchestrator,
                        first.run,
                    ),
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed", resumed.run.error)
            self.assertEqual(
                runner.prompts_seen,
                ["plan", "implementation", "review"],
            )
            self.assertEqual(len(runtime_manager.calls), 1)
            self.assertEqual(jira.transitions, ["Done"])
            self.assertIn("Review:", resumed.run.final_message or "")
            self.assertIn("Verification override:", resumed.run.final_message or "")

    def test_verification_bypass_blocks_when_review_changes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, _, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )
            runner = VerificationBypassReviewRunner(
                ["approve"],
                mutate_review=True,
            )
            orchestrator.codex_runner = runner
            orchestrator.config.codex.review_after_run = True
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=verification_bypass_input(
                        orchestrator,
                        first.run,
                    ),
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "verification")
            self.assertIn(
                "completion after review",
                resumed.run.error or "",
            )
            self.assertEqual(
                runner.prompts_seen,
                ["plan", "implementation", "review"],
            )
            self.assertEqual(len(runtime_manager.calls), 1)
            self.assertEqual(jira.transitions, [])

    def test_verification_bypass_blocks_when_after_run_changes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, runner, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            bypass_input = verification_bypass_input(orchestrator, first.run)
            orchestrator.config.hooks.after_run = (
                "printf 'after-run\\n' >> repo/after-run-drift.txt"
            )

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=bypass_input,
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "verification")
            self.assertIn(
                "after after_run before Jira handoff",
                resumed.run.error or "",
            )
            self.assertEqual(runner.prompts_seen, ["plan", "implementation"])
            self.assertEqual(len(runtime_manager.calls), 1)
            self.assertEqual(jira.transitions, [])

    def test_review_changes_consume_bypass_and_require_normal_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, _, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
            )
            runner = VerificationBypassReviewRunner(
                ["changes_required", "approve"]
            )
            orchestrator.codex_runner = runner
            orchestrator.config.codex.review_after_run = True
            orchestrator.config.codex.max_review_iterations = 2
            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            bypass_input = verification_bypass_input(orchestrator, first.run)
            runtime_manager.status = "passed"

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=bypass_input,
                    previous_run=first.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed", resumed.run.error)
            self.assertEqual(resumed.run.verification_status, "passed")
            self.assertEqual(
                runner.prompts_seen,
                [
                    "plan",
                    "implementation",
                    "review",
                    "regeneration",
                    "review",
                ],
            )
            self.assertEqual(len(runtime_manager.calls), 2)
            self.assertEqual(jira.transitions, ["Done"])
            self.assertIn(
                "override was consumed when review required code changes",
                resumed.run.final_message or "",
            )

    def test_advisory_runtime_failure_is_persisted_but_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, _, _ = run_runtime_case(
                root,
                runtime_status="test_failed",
                required=False,
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "completed")
            self.assertEqual(result.run.verification_status, "test_failed")
            self.assertIsNone(result.run.error)
            logs = Store(root / "db.sqlite3").list_logs(run_id=result.run.id)
            self.assertTrue(
                any("runtime verification is advisory" in log["message"] for log in logs)
            )

    def test_runtime_shutdown_runs_after_persisted_successful_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, _, original_jira = create_runtime_case(
                root,
                runtime_status="passed",
                required=True,
                shutdown_after_handoff=True,
            )
            events: list[str] = []
            jira = OrderedHandoffJira(original_jira.issue, events)
            orchestrator.jira = jira
            runtime_manager.events = events
            runtime_manager.on_shutdown = lambda: (
                orchestrator.store.latest_run_for_issue("T-1").status
            )

            result = asyncio.run(orchestrator.run_once("T-1"))

            assert result.run is not None
            self.assertEqual(result.run.status, "completed")
            self.assertEqual(events, ["verify", "transition", "shutdown"])
            self.assertEqual(runtime_manager.persisted_status_at_shutdown, "completed")
            self.assertEqual(len(runtime_manager.shutdown_calls), 1)
            self.assertEqual(
                runtime_manager.shutdown_calls[0]["repositories"],
                ("repo",),
            )
            self.assertEqual(
                runtime_manager.shutdown_calls[0]["source_repositories"],
                ("repo",),
            )
            self.assertEqual(jira.transitions, ["Done"])

    def test_runtime_shutdown_runs_when_no_jira_transition_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, _, jira = create_runtime_case(
                root,
                runtime_status="passed",
                required=True,
                shutdown_after_handoff=True,
            )
            orchestrator.config.tracker.handoff_status = None

            result = asyncio.run(orchestrator.run_once("T-1"))

            assert result.run is not None
            self.assertEqual(result.run.status, "completed")
            self.assertEqual(jira.transitions, [])
            self.assertEqual(len(runtime_manager.shutdown_calls), 1)

    def test_completed_review_shuts_down_without_a_second_jira_transition(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_completed_review_workflow(root)
                runtime_workflow = load_workflow(
                    write_runtime_workflow(
                        root,
                        write_fake_codex(root),
                        required=True,
                        shutdown_after_handoff=True,
                    ),
                    environ={"TEST_JIRA_TOKEN": "token"},
                )
                workflow.config.runtime = runtime_workflow.config.runtime
                workflow.config.tracker.handoff_status = "Done"
                issue = completed_review_issue()
                jira = FakeJira(issue)
                store = Store(root / "db.sqlite3")
                action, _, result_run, _ = create_completed_review_action(
                    root,
                    workflow,
                    issue,
                    store,
                )
                runtime_manager = FakeRuntimeManager("passed")
                polling = PollingOrchestrator(
                    workflow,
                    jira,
                    store,
                    codex_runner=CompletedReviewCodexRunner("code_changes"),
                    runtime_manager=runtime_manager,
                )

                await polling.poll_once()
                await asyncio.gather(
                    *(running.task for running in polling.running.values())
                )
                await polling.reap_finished()

                completed = store.get_run(result_run.id)
                assert completed is not None
                self.assertEqual(completed.status, "completed")
                self.assertEqual(jira.transitions, [])
                self.assertEqual(len(runtime_manager.shutdown_calls), 1)
                self.assertEqual(
                    runtime_manager.shutdown_calls[0]["repositories"],
                    ("repo",),
                )
                linked_action = store.human_review_action_for_result_run(
                    completed.id
                )
                assert linked_action is not None
                self.assertEqual(linked_action["id"], action["id"])
                self.assertEqual(linked_action["status"], "completed")

        asyncio.run(run())

    def test_runtime_shutdown_failure_warns_without_changing_completed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, _, jira = create_runtime_case(
                root,
                runtime_status="passed",
                required=True,
                shutdown_after_handoff=True,
                shutdown_status="environment_blocked",
            )

            result = asyncio.run(orchestrator.run_once("T-1"))

            assert result.run is not None
            self.assertEqual(result.run.status, "completed")
            self.assertIsNone(result.run.error)
            self.assertEqual(jira.transitions, ["Done"])
            self.assertEqual(len(runtime_manager.shutdown_calls), 1)
            logs = orchestrator.store.list_logs(run_id=result.run.id)
            warnings = [
                log
                for log in logs
                if log["level"] == "warning"
                and "Runtime shutdown was blocked" in log["message"]
            ]
            self.assertEqual(len(warnings), 1)
            self.assertTrue(warnings[0]["path"].endswith("fake-shutdown.log"))

    def test_runtime_shutdown_does_not_run_for_blocked_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, _, jira = create_runtime_case(
                root,
                runtime_status="test_failed",
                required=True,
                shutdown_after_handoff=True,
            )

            result = asyncio.run(orchestrator.run_once("T-1"))

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "verification")
            self.assertEqual(runtime_manager.shutdown_calls, [])
            self.assertEqual(jira.transitions, [])

    def test_runtime_services_are_retained_when_jira_handoff_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator, runtime_manager, _, original_jira = create_runtime_case(
                root,
                runtime_status="passed",
                required=True,
                shutdown_after_handoff=True,
            )
            events: list[str] = []
            jira = OrderedHandoffJira(
                original_jira.issue,
                events,
                transition_succeeds=False,
            )
            orchestrator.jira = jira
            runtime_manager.events = events

            result = asyncio.run(orchestrator.run_once("T-1"))

            assert result.run is not None
            self.assertEqual(result.run.status, "completed")
            self.assertEqual(events, ["verify", "transition"])
            self.assertEqual(runtime_manager.shutdown_calls, [])
            logs = orchestrator.store.list_logs(run_id=result.run.id)
            self.assertTrue(
                any(
                    log["level"] == "warning"
                    and "services were retained" in log["message"]
                    for log in logs
                )
            )

    def test_dry_run_renders_prompt_without_workspace_or_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = write_workflow(root, fake_codex)
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            jira = FakeJira(issue)
            store = Store(root / ".symphony" / "symphony.sqlite3")

            result = asyncio.run(SingleIssueOrchestrator(workflow, jira, store).run_once("T-1", dry_run=True))

            self.assertTrue(result.dry_run)
            self.assertIn("T-1", result.prompt)
            self.assertEqual(jira.comments, [])
            self.assertFalse((root / "workspaces" / "T-1").exists())
            self.assertEqual(store.list_runs(), [])

    def test_dispatch_selection_respects_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root, write_fake_codex(root)), environ={"TEST_JIRA_TOKEN": "token"})
            issues = [
                Issue(id="1", identifier="T-1", title="one", status="To Do", labels=["codex-ready"], url="u"),
                Issue(id="2", identifier="T-2", title="two", status="To Do", labels=["codex-ready"], url="u"),
            ]

            selected = select_dispatchable_issues(issues, claimed_issue_ids=set(), config=workflow.config)

            self.assertEqual([issue.identifier for issue in selected], ["T-1"])

    def test_retry_backoff_caps_exponential_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root, write_fake_codex(root)), environ={"TEST_JIRA_TOKEN": "token"})
            workflow.config.agent.max_retry_backoff_seconds = 4

            self.assertEqual(retry_backoff_seconds(1, workflow.config), 1)
            self.assertEqual(retry_backoff_seconds(3, workflow.config), 4)
            self.assertEqual(retry_backoff_seconds(10, workflow.config), 4)

    def test_structured_review_json_decision_is_supported(self) -> None:
        self.assertEqual(classify_review_decision('{"decision":"approve","findings":[]}'), "approve")
        self.assertEqual(
            classify_review_decision('```json\n{"decision":"changes_required","findings":["fix it"]}\n```'),
            "changes_required",
        )
        self.assertEqual(classify_review_decision('{"decision":"plan_changes_required"}'), "plan_changes_required")
        self.assertEqual(
            classify_review_decision(
                '{"decision":"automation_plan_changes_required"}'
            ),
            "automation_plan_changes_required",
        )
        self.assertEqual(classify_review_decision(None), "invalid")
        self.assertEqual(classify_review_decision("APPROVE: no findings"), "approve")
        self.assertEqual(classify_review_decision("Approved plan must change"), "invalid")
        self.assertEqual(classify_review_decision("approved? no"), "invalid")
        self.assertEqual(classify_review_decision("Looks fine to me."), "invalid")

    def test_structured_human_request_json_is_supported(self) -> None:
        self.assertEqual(
            parse_human_request('{"decision":"needs_human","question":"Apply to CPM only?"}'),
            "Apply to CPM only?",
        )
        self.assertEqual(
            parse_human_request('```json\n{"decision":"needs_human","question":"Which repo?"}\n```'),
            "Which repo?",
        )
        self.assertIsNone(
            parse_human_request('{"decision":"ready_for_approval","questions":["Where should the column go?"]}'),
        )
        self.assertEqual(
            parse_human_request('{"questions":["Where should the column go?"]}'),
            "Where should the column go?",
        )
        self.assertEqual(
            parse_human_request('{"decision":"needs_human","questions":["Which schema?"]}'),
            "Which schema?",
        )
        self.assertEqual(
            parse_human_request(
                '{"decision":"ready_for_approval","assumptions":[{"assumption":"Place the column last","needs_human":true}]}'
            ),
            "Place the column last",
        )
        self.assertIsNone(parse_human_request('{"decision":"approve","question":"Nope"}'))
        self.assertIsNone(
            parse_human_request(
                '{"decision":"plan_changes_required","questions":["Which schema?"]}'
            )
        )

    def test_polling_orchestrator_respects_concurrency(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(write_workflow(root, write_fake_codex(root)), environ={"TEST_JIRA_TOKEN": "token"})
                issues = [
                    Issue(id="1", identifier="T-1", title="one", status="To Do", labels=["codex-ready"], url="u"),
                    Issue(id="2", identifier="T-2", title="two", status="To Do", labels=["codex-ready"], url="u"),
                ]
                runner = ReleasableCodexRunner()
                polling = PollingOrchestrator(workflow, FakeJira(issues[0], issues), Store(root / "db.sqlite3"), codex_runner=runner)

                await polling.poll_once()
                await asyncio.sleep(0.05)

                self.assertEqual(len(polling.running), 1)
                self.assertEqual(runner.started, ["T-1"])
                runner.release()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                await polling.poll_once()
                await asyncio.sleep(0.05)

                self.assertEqual(len(polling.running), 1)
                self.assertEqual(runner.started, ["T-1", "T-2"])
                runner.release()
                await asyncio.gather(*(item.task for item in polling.running.values()))

        asyncio.run(run())

    def test_polling_orchestrator_retries_transient_failures(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(write_workflow(root, write_fake_codex(root)), environ={"TEST_JIRA_TOKEN": "token"})
                workflow.config.agent.max_retries = 1
                issue = Issue(id="1", identifier="T-1", title="one", status="To Do", labels=["codex-ready"], url="u")
                store = Store(root / "db.sqlite3")
                polling = PollingOrchestrator(
                    workflow,
                    FakeJira(issue),
                    store,
                    codex_runner=StatusCodexRunner(["failed", "completed"]),
                )

                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()
                self.assertEqual(list(polling.retry_queue.values())[0].attempt, 2)

                for entry in polling.retry_queue.values():
                    entry.due_at = 0
                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                runs = store.list_runs()
                self.assertEqual([run.attempt for run in sorted(runs, key=lambda run: run.attempt)], [1, 2])
                self.assertEqual(runs[0].status, "completed")

        asyncio.run(run())

    def test_polling_restart_skips_only_same_description_fingerprint(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(write_workflow(root, write_fake_codex(root)), environ={"TEST_JIRA_TOKEN": "token"})
                issue = Issue(
                    id="1",
                    identifier="T-1",
                    title="one",
                    description="original description",
                    status="To Do",
                    labels=["codex-ready"],
                    url="u",
                )
                store = Store(root / "db.sqlite3")

                first_runner = StatusCodexRunner(["completed"])
                first = PollingOrchestrator(workflow, FakeJira(issue), store, codex_runner=first_runner)
                await first.poll_once()
                await asyncio.gather(*(item.task for item in first.running.values()))
                await first.reap_finished()

                same_description_runner = StatusCodexRunner(["completed"])
                same_description = PollingOrchestrator(
                    workflow,
                    FakeJira(issue),
                    store,
                    codex_runner=same_description_runner,
                )
                await same_description.poll_once()
                self.assertEqual(same_description.running, {})
                self.assertEqual(same_description_runner.statuses, ["completed"])

                changed_issue = issue.model_copy(update={"description": "changed description"})
                changed_description_runner = StatusCodexRunner(["completed"])
                changed_description = PollingOrchestrator(
                    workflow,
                    FakeJira(changed_issue),
                    store,
                    codex_runner=changed_description_runner,
                )
                await changed_description.poll_once()
                await asyncio.gather(*(item.task for item in changed_description.running.values()))
                await changed_description.reap_finished()
                self.assertEqual(changed_description_runner.statuses, [])

        asyncio.run(run())

    def test_blocked_issue_waits_for_human_input_then_resumes(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(write_workflow(root, write_fake_codex(root)), environ={"TEST_JIRA_TOKEN": "token"})
                issue = Issue(id="1", identifier="T-1", title="one", status="To Do", labels=["codex-ready"], url="u")
                store = Store(root / "db.sqlite3")
                runner = PromptStatusCodexRunner(["blocked", "completed"])
                polling = PollingOrchestrator(workflow, FakeJira(issue), store, codex_runner=runner)

                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                blocked_run = store.latest_run_for_issue("T-1")
                self.assertIsNotNone(blocked_run)
                assert blocked_run is not None
                self.assertEqual(blocked_run.status, "blocked")
                self.assertEqual(blocked_run.blocked_phase, "implementation")

                await polling.poll_once()
                self.assertEqual(polling.running, {})
                self.assertEqual(runner.statuses, ["completed"])

                store.add_human_input(
                    "T-1",
                    run_id=blocked_run.id,
                    question=blocked_run.error,
                    response="Use the CPM report flow only.",
                )

                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                latest = store.latest_run_for_issue("T-1")
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertEqual(latest.status, "completed")
                self.assertIn("Human clarification:", runner.prompts[-1])
                self.assertIn("Use the CPM report flow only.", runner.prompts[-1])
                self.assertIn("Continue implementation from the existing workspace.", runner.prompts[-1])
                self.assertIn("Leave an updated final report", runner.prompts[-1])
                self.assertIsNotNone(store.list_human_inputs(run_id=blocked_run.id)[0]["consumed_at"])

        asyncio.run(run())

    def test_human_resume_fetch_failure_requeues_input_without_fresh_dispatch(self) -> None:
        class FailFirstResumeFetchJira(FakeJira):
            def __init__(self, issue: Issue) -> None:
                super().__init__(issue)
                self.fail_resume_fetch = True

            async def get_issue(self, key: str, include_comments: bool = True) -> Issue:
                if include_comments and self.fail_resume_fetch:
                    self.fail_resume_fetch = False
                    raise RuntimeError("temporary Jira failure")
                return await super().get_issue(key, include_comments=include_comments)

        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(
                    write_workflow(root, write_fake_codex(root)),
                    environ={"TEST_JIRA_TOKEN": "token"},
                )
                issue = Issue(
                    id="1",
                    identifier="T-1",
                    title="one",
                    status="To Do",
                    labels=["codex-ready"],
                    url="u",
                )
                store = Store(root / "db.sqlite3")
                blocked = store.create_run(hydrated_test_issue(issue), root / "workspaces" / "T-1", branch_name=None)
                blocked = store.update_run(blocked.id, status="blocked", blocked_phase="implementation")
                pending = store.add_human_input(
                    "T-1",
                    run_id=blocked.id,
                    response="Use the CPM report flow only.",
                )
                runner = StatusCodexRunner(["completed"])
                polling = PollingOrchestrator(
                    workflow,
                    FailFirstResumeFetchJira(issue),
                    store,
                    codex_runner=runner,
                )

                await polling.poll_once()

                deferred = store.list_human_inputs(run_id=blocked.id)[0]
                self.assertIsNone(deferred["claimed_at"])
                self.assertIsNone(deferred["consumed_at"])
                self.assertEqual(pending["id"], deferred["id"])
                self.assertEqual(polling.running, {})
                self.assertEqual(runner.statuses, ["completed"])

                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                consumed = store.list_human_inputs(run_id=blocked.id)[0]
                self.assertIsNotNone(consumed["consumed_at"])
                self.assertEqual(store.latest_run_for_issue("T-1").attempt, 2)
                self.assertEqual(runner.statuses, [])

        asyncio.run(run())

    def test_polling_recovers_abandoned_stale_human_input_claim(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(
                    write_workflow(root, write_fake_codex(root)),
                    environ={"TEST_JIRA_TOKEN": "token"},
                )
                issue = Issue(
                    id="1",
                    identifier="T-1",
                    title="one",
                    status="To Do",
                    labels=["codex-ready"],
                    url="u",
                )
                store = Store(root / "db.sqlite3")
                blocked = store.create_run(hydrated_test_issue(issue), root / "workspaces" / "T-1", branch_name=None)
                blocked = store.update_run(
                    blocked.id,
                    status="blocked",
                    blocked_phase="implementation",
                )
                pending = store.add_human_input(
                    "T-1",
                    run_id=blocked.id,
                    response="Use the CPM report flow only.",
                )
                abandoned = store.claim_human_input(
                    pending["id"],
                    now=datetime(2000, 1, 1, tzinfo=timezone.utc),
                )
                self.assertIsNotNone(abandoned)
                runner = StatusCodexRunner(["completed"])
                polling = PollingOrchestrator(
                    workflow,
                    FakeJira(issue),
                    store,
                    codex_runner=runner,
                )

                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                recovered = store.list_human_inputs(run_id=blocked.id)[0]
                self.assertIsNotNone(recovered["consumed_at"])
                self.assertIsNone(recovered["claimed_at"])
                self.assertIsNone(recovered["claim_token"])
                latest = store.latest_run_for_issue("T-1")
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertEqual(latest.attempt, 2)
                self.assertEqual(latest.status, "completed")
                self.assertEqual(runner.statuses, [])

        asyncio.run(run())

    def test_human_resume_is_discarded_if_run_becomes_stale_during_jira_fetch(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(
                    write_workflow(root, write_fake_codex(root)),
                    environ={"TEST_JIRA_TOKEN": "token"},
                )
                issue = Issue(
                    id="1",
                    identifier="T-1",
                    title="one",
                    status="To Do",
                    labels=["codex-ready"],
                    url="u",
                )
                store = Store(root / "db.sqlite3")
                blocked = store.create_run(hydrated_test_issue(issue), root / "workspaces" / "T-1", branch_name=None)
                blocked = store.update_run(blocked.id, status="blocked", blocked_phase="implementation")
                store.add_human_input(
                    "T-1",
                    run_id=blocked.id,
                    response="Resume the old attempt.",
                )

                class SupersedingJira(FakeJira):
                    def __init__(self) -> None:
                        super().__init__(issue)
                        self.superseded = False

                    async def get_issue(self, key: str, include_comments: bool = True) -> Issue:
                        if include_comments and not self.superseded:
                            self.superseded = True
                            newer = store.create_run(
                                hydrated_test_issue(issue),
                                root / "workspaces" / "T-1",
                                branch_name=None,
                            )
                            store.update_run(newer.id, status="completed")
                        return await super().get_issue(key, include_comments=include_comments)

                runner = StatusCodexRunner(["completed"])
                polling = PollingOrchestrator(
                    workflow,
                    SupersedingJira(),
                    store,
                    codex_runner=runner,
                )

                await polling.poll_once()

                self.assertEqual(polling.running, {})
                self.assertEqual(runner.statuses, ["completed"])
                stale_input = store.list_human_inputs(run_id=blocked.id)[0]
                self.assertIsNotNone(stale_input["consumed_at"])
                self.assertIsNone(stale_input["claimed_at"])
                latest = store.latest_run_for_issue("T-1")
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertEqual(latest.status, "completed")

        asyncio.run(run())

    def test_concurrent_polls_dispatch_one_human_resume(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(
                    write_workflow(root, write_fake_codex(root)),
                    environ={"TEST_JIRA_TOKEN": "token"},
                )
                issue = Issue(
                    id="1",
                    identifier="T-1",
                    title="one",
                    status="To Do",
                    labels=["codex-ready"],
                    url="u",
                )
                store = Store(root / "db.sqlite3")
                blocked = store.create_run(hydrated_test_issue(issue), root / "workspaces" / "T-1", branch_name=None)
                blocked = store.update_run(blocked.id, status="blocked", blocked_phase="implementation")
                store.add_human_input(
                    "T-1",
                    run_id=blocked.id,
                    response="Use the CPM report flow only.",
                )
                runner = ReleasableCodexRunner()
                polling = PollingOrchestrator(
                    workflow,
                    FakeJira(issue),
                    store,
                    codex_runner=runner,
                )

                await asyncio.gather(polling.poll_once(), polling.poll_once())
                await asyncio.sleep(0.05)

                self.assertEqual(runner.started, ["T-1"])
                self.assertEqual(len(polling.running), 1)
                submitted = store.list_human_inputs(run_id=blocked.id)[0]
                self.assertIsNotNone(submitted["consumed_at"])
                runner.release()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

        asyncio.run(run())

    def test_reserved_human_resume_recovers_after_restart_without_a_second_run(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_workflow(
                    write_workflow(root, write_fake_codex(root)),
                    environ={"TEST_JIRA_TOKEN": "token"},
                )
                issue = Issue(
                    id="1",
                    identifier="T-1",
                    title="one",
                    description="Implement the exact requirement.",
                    status="To Do",
                    labels=["codex-ready"],
                    url="u",
                )
                hydrated = hydrated_test_issue(issue)
                db_path = root / "db.sqlite3"
                original_store = Store(db_path)
                workspace_path = root / "workspaces" / "T-1"
                blocked = original_store.create_run(
                    hydrated, workspace_path, branch_name=None
                )
                blocked = original_store.update_run(
                    blocked.id,
                    status="blocked",
                    blocked_phase="implementation",
                )
                pending = original_store.add_human_input(
                    "T-1",
                    run_id=blocked.id,
                    response="recover this exact response",
                )
                claimed = original_store.claim_human_input(pending["id"])
                assert claimed is not None
                reserved, reservation_status = original_store.reserve_human_resume(
                    hydrated,
                    workspace_path,
                    input_id=pending["id"],
                    claim_token=claimed["claim_token"],
                    expected_predecessor_run_id=blocked.id,
                    branch_name=None,
                    attempt=2,
                )
                self.assertEqual(reservation_status, "reserved")
                assert reserved is not None

                class CountingJira(FakeJira):
                    def __init__(self) -> None:
                        super().__init__(issue)
                        self.full_fetches = 0

                    async def get_issue(
                        self, key: str, include_comments: bool = True
                    ) -> Issue:
                        if include_comments:
                            self.full_fetches += 1
                        return await super().get_issue(
                            key, include_comments=include_comments
                        )

                restarted_store = Store(db_path)
                jira = CountingJira()

                class FetchAwareRunner(PromptStatusCodexRunner):
                    def __init__(self) -> None:
                        super().__init__(["completed"])
                        self.fetches_before_run: int | None = None

                    async def run(self, *args, **kwargs):
                        self.fetches_before_run = jira.full_fetches
                        return await super().run(*args, **kwargs)

                runner = FetchAwareRunner()
                polling = PollingOrchestrator(
                    workflow,
                    jira,
                    restarted_store,
                    codex_runner=runner,
                )

                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                runs = restarted_store.list_runs_for_issue("T-1")
                self.assertEqual(len(runs), 2)
                latest = restarted_store.latest_run_for_issue("T-1")
                assert latest is not None
                self.assertEqual(latest.id, reserved.id)
                self.assertEqual(latest.attempt, 2)
                self.assertEqual(latest.status, "completed")
                self.assertGreaterEqual(runner.fetches_before_run or 0, 1)
                self.assertEqual(len(runner.prompts), 1)
                self.assertIn("recover this exact response", runner.prompts[0])
                persisted_input = restarted_store.list_human_inputs(
                    run_id=blocked.id
                )[0]
                self.assertIsNotNone(persisted_input["consumed_at"])
                self.assertEqual(
                    restarted_store.list_recoverable_human_resume_run_ids(), []
                )
                handoff = restarted_store.get_human_resume_handoff(reserved.id)
                assert handoff is not None
                self.assertIsNotNone(handoff["started_at"])

        asyncio.run(run())

    def test_completed_review_registration_survives_terminal_jira_reconciliation(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_completed_review_workflow(root)
                issue = completed_review_issue()
                store = Store(root / "db.sqlite3")
                action, _, result_run, _ = create_completed_review_action(
                    root,
                    workflow,
                    issue,
                    store,
                )
                jira = TerminalReconciliationJira(issue)
                runner = CompletedReviewCodexRunner(
                    "plan_changes_required",
                    gate_triage=True,
                )
                polling = PollingOrchestrator(
                    workflow,
                    jira,
                    store,
                    codex_runner=runner,
                )

                await polling.poll_once()
                await asyncio.wait_for(runner.triage_started.wait(), timeout=1)

                self.assertEqual(set(polling.running), {issue.id})
                running = polling.running[issue.id]
                self.assertTrue(running.completed_review)
                self.assertFalse(running.human_resume)
                self.assertFalse(running.task.done())
                self.assertEqual(running.identifier, issue.identifier)
                self.assertEqual(running.attempt, result_run.attempt)

                await polling.reconcile_running()

                self.assertEqual(jira.reconciliation_fetches, 0)
                self.assertFalse(running.task.cancelled())
                self.assertFalse(running.task.done())

                runner.release_triage()
                await running.task
                await polling.reap_finished()

                persisted_action = store.get_human_review_action(action["id"])
                assert persisted_action is not None
                self.assertEqual(persisted_action["status"], "blocked")

        asyncio.run(run())

    def test_completed_review_code_changes_force_review_and_complete_linked_action(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_completed_review_workflow(root)
                self.assertFalse(workflow.config.codex.review_after_run)
                issue = completed_review_issue()
                store = Store(root / "db.sqlite3")
                action, source_run, result_run, _ = create_completed_review_action(
                    root,
                    workflow,
                    issue,
                    store,
                )
                jira = FakeJira(issue)
                runner = CompletedReviewCodexRunner("code_changes")
                polling = PollingOrchestrator(
                    workflow,
                    jira,
                    store,
                    codex_runner=runner,
                )

                await polling.poll_once()
                await asyncio.gather(
                    *(running.task for running in polling.running.values())
                )
                await polling.reap_finished()

                completed = store.get_run(result_run.id)
                assert completed is not None
                self.assertEqual(completed.status, "completed")
                self.assertEqual(completed.workspace_path, source_run.workspace_path)
                self.assertEqual(completed.verification_status, "passed")
                self.assertIn("Addressed the pasted code review.", completed.final_message or "")
                self.assertIn('"decision":"approve"', completed.final_message or "")
                self.assertEqual(
                    runner.phases,
                    ["triage", "implementation", "review"],
                )
                self.assertEqual(
                    runner.args_by_phase["triage"][-2:],
                    ["--sandbox", "read-only"],
                )
                self.assertTrue(
                    (
                        Path(completed.workspace_path)
                        / "repo"
                        / "review_regression_test.py"
                    ).is_file()
                )

                linked_action = store.human_review_action_for_result_run(
                    completed.id
                )
                assert linked_action is not None
                self.assertEqual(linked_action["id"], action["id"])
                self.assertEqual(linked_action["source_run_id"], source_run.id)
                self.assertEqual(linked_action["status"], "completed")
                self.assertEqual(linked_action["triage_decision"], "code_changes")
                self.assertIn(
                    "Fits the approved PlanSpec",
                    linked_action["triage_output"],
                )
                self.assertIsNotNone(linked_action["finished_at"])
                self.assertIsNone(linked_action["claim_token"])
                self.assertEqual(
                    store.list_recoverable_human_review_action_ids(),
                    [],
                )

                review_history = (
                    Path(completed.workspace_path)
                    / workflow.config.codex.output_review_history_file
                ).read_text(encoding="utf-8")
                self.assertIn("Original automated review", review_history)
                self.assertIn(f"Human review {action['id']}", review_history)
                self.assertIn("reviewer@example.test", review_history)
                self.assertEqual(jira.comments, [])
                self.assertEqual(jira.transitions, [])

        asyncio.run(run())

    def test_completed_review_plan_change_blocks_before_implementation_and_invalidates_approval(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = load_completed_review_workflow(root)
                issue = completed_review_issue()
                store = Store(root / "db.sqlite3")
                action, source_run, result_run, approval = (
                    create_completed_review_action(
                        root,
                        workflow,
                        issue,
                        store,
                    )
                )
                runner = CompletedReviewCodexRunner("plan_changes_required")
                polling = PollingOrchestrator(
                    workflow,
                    FakeJira(issue),
                    store,
                    codex_runner=runner,
                )

                await polling.poll_once()
                await asyncio.gather(
                    *(running.task for running in polling.running.values())
                )
                await polling.reap_finished()

                blocked = store.get_run(result_run.id)
                assert blocked is not None
                self.assertEqual(blocked.status, "blocked")
                self.assertEqual(blocked.blocked_phase, "planning")
                self.assertIn(
                    "requires replanning and a new approval",
                    blocked.error or "",
                )
                self.assertEqual(blocked.workspace_path, source_run.workspace_path)
                self.assertEqual(runner.phases, ["triage"])
                self.assertFalse(
                    (
                        Path(blocked.workspace_path)
                        / "repo"
                        / "review_regression_test.py"
                    ).exists()
                )

                invalidated = store.get_plan_approval(str(approval["id"]))
                assert invalidated is not None
                self.assertIsNotNone(invalidated["invalidated_at"])
                self.assertIn(
                    "changes an acceptance criterion",
                    invalidated["invalidation_reason"],
                )
                linked_action = store.human_review_action_for_result_run(
                    blocked.id
                )
                assert linked_action is not None
                self.assertEqual(linked_action["id"], action["id"])
                self.assertEqual(linked_action["status"], "blocked")
                self.assertEqual(
                    linked_action["triage_decision"],
                    "plan_changes_required",
                )
                self.assertEqual(
                    store.get_run(source_run.id).status,
                    "completed",
                )

        asyncio.run(run())

    def test_plan_pass_runs_before_implementation_and_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = write_workflow(
                root,
                fake_codex,
                codex_extra="""
  plan_before_implementation: true
  planning_prompt: |
    Write a plan only. Do not edit files.
""",
            )
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                priority="High",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            jira = FakeJira(issue)
            store = Store(root / ".symphony" / "symphony.sqlite3")
            runner = PlanThenImplementCodexRunner()

            result = asyncio.run(SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once("T-1"))

            self.assertIsNotNone(result.run)
            assert result.run is not None
            self.assertEqual(result.run.status, "completed")
            self.assertEqual(runner.prompts_seen, ["plan", "implementation"])
            assert result.workspace is not None
            self.assertEqual(
                json.loads(
                    (result.workspace.path / ".symphony" / "codex-plan.md").read_text(encoding="utf-8")
                )["simplest_implementation"],
                "Edit one file and run verify.",
            )
            self.assertIn("Codex planning/spec pass:", runner.implementation_prompt)
            self.assertIn('"schema_version": "1.0"', runner.implementation_prompt)
            self.assertIn("Reserve needs_human for a genuine conflict", runner.plan_prompt)
            self.assertIn(
                "Do not manufacture requirements or acceptance criteria",
                runner.plan_prompt,
            )
            self.assertIn(
                "Attachments, attachment metadata/analysis, generic custom fields",
                runner.plan_prompt,
            )

    def test_invalid_model_plan_is_automatically_repaired_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  planning_prompt: |
    Write a plan only.
""",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            store = Store(root / ".symphony" / "symphony.sqlite3")
            runner = StructuralPlanRepairCodexRunner()

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    store,
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "completed", result.run.error)
            self.assertEqual(
                runner.prompts_seen,
                ["plan", "plan_repair", "implementation"],
            )
            self.assertIn("invalid PlanSpec", runner.repair_prompt)
            self.assertIn(
                "Product authority is limited to the root Description",
                runner.repair_prompt,
            )
            self.assertIn(
                "Attachments, attachment metadata/analysis, generic custom",
                runner.repair_prompt,
            )
            self.assertIn(
                "Reserve\nneeds_human for a genuine Jira conflict",
                runner.repair_prompt,
            )
            logs = store.list_logs(run_id=result.run.id)
            self.assertTrue(
                any("automatic repair attempt 1/2" in log["message"] for log in logs)
            )

    def test_plan_repair_is_bounded_when_model_keeps_returning_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  planning_prompt: |
    Write a plan only.
""",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = AlwaysInvalidPlanCodexRunner()

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / ".symphony" / "symphony.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "planning")
            self.assertEqual(runner.calls, 3)
            self.assertIn(
                "Automatic PlanSpec repair remained invalid after 2 attempt(s)",
                result.run.error or "",
            )

    def test_planning_evidence_excludes_attachments_and_generic_jira_context(self) -> None:
        def source(source_type: str, source_id: str) -> RequirementSource:
            return RequirementSource(
                issue_identifier="T-1",
                source_type=source_type,
                source_id=source_id,
                author="product-owner",
                authority="product",
            )

        description_source = source("description", "description")
        acceptance_source = source("custom_field", "field:customfield_15812")
        generic_source = source("custom_field", "field:customfield_99999")
        comment_source = source("comment", "comment:100")
        attachment_source = source("attachment", "attachment:200")
        description = RequirementArtifact(
            artifact_id="description",
            source_type="description",
            text="DESCRIPTION-EVIDENCE",
            source=description_source,
        )
        acceptance = RequirementArtifact(
            artifact_id="field:customfield_15812",
            source_type="custom_field",
            text="ACCEPTANCE-EVIDENCE",
            source=acceptance_source,
            kind="acceptance_criterion",
            planning_eligible=True,
        )
        generic = RequirementArtifact(
            artifact_id="field:customfield_99999",
            source_type="custom_field",
            text="GENERIC-CUSTOM-FIELD-CONTEXT",
            source=generic_source,
            planning_eligible=False,
        )
        comment = RequirementArtifact(
            artifact_id="comment:100",
            source_type="comment",
            text="ROOT-COMMENT-EVIDENCE",
            source=comment_source,
        )
        attachment = IssueAttachment(
            id="200",
            filename="scope.png",
            source=attachment_source,
            analysis=AttachmentAnalysis(
                status="complete",
                modality="vision",
                summary="ATTACHMENT-MUST-NOT-BECOME-SCOPE",
            ),
        )
        snapshot = RequirementsSnapshot(
            issue_id="10001",
            issue_identifier="T-1",
            issue_url="https://jira.example.test/browse/T-1",
            description=description,
            custom_fields=[acceptance, generic],
            comments=[comment],
            attachments=[attachment],
            current_requirements=[
                RequirementDecision(
                    id="T-1-REQ-01",
                    text=description.text,
                    kind="requirement",
                    classification="current",
                    sources=[description_source],
                ),
                RequirementDecision(
                    id="T-1-AC-01",
                    text=acceptance.text,
                    kind="acceptance_criterion",
                    classification="current",
                    sources=[acceptance_source],
                ),
                RequirementDecision(
                    id="T-1-REQ-02",
                    text=comment.text,
                    kind="requirement",
                    classification="current",
                    sources=[comment_source],
                ),
                RequirementDecision(
                    id="T-1-REQ-X1",
                    text=generic.text,
                    kind="requirement",
                    classification="current",
                    sources=[generic_source],
                ),
                RequirementDecision(
                    id="T-1-REQ-X2",
                    text=attachment.analysis.summary,
                    kind="requirement",
                    classification="current",
                    sources=[attachment_source],
                ),
            ],
        )
        issue = Issue(
            id="10001",
            identifier="T-1",
            title="Planning evidence boundary",
            description=description.text,
            status="To Do",
            url="https://jira.example.test/browse/T-1",
            requirements_snapshot=snapshot,
        )

        evidence = planning_requirements_snapshot_prompt(issue)

        self.assertIn("DESCRIPTION-EVIDENCE", evidence)
        self.assertIn("ACCEPTANCE-EVIDENCE", evidence)
        self.assertIn("ROOT-COMMENT-EVIDENCE", evidence)
        self.assertNotIn("GENERIC-CUSTOM-FIELD-CONTEXT", evidence)
        self.assertNotIn("ATTACHMENT-MUST-NOT-BECOME-SCOPE", evidence)
        self.assertNotIn("scope.png", evidence)

    def test_blocked_planning_pass_records_blocked_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = write_workflow(
                root,
                write_fake_codex(root),
                codex_extra="""
  plan_before_implementation: true
  planning_prompt: |
    Write a plan only.
""",
            )
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            store = Store(root / ".symphony" / "symphony.sqlite3")

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    store,
                    codex_runner=StatusCodexRunner(["blocked"]),
                ).run_once("T-1")
            )

            self.assertIsNotNone(result.run)
            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "planning")

    def test_needs_human_json_blocks_planning_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = write_workflow(
                root,
                write_fake_codex(root),
                codex_extra="""
  plan_before_implementation: true
  planning_prompt: |
    Write a plan only.
""",
            )
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = MessageCodexRunner(['{"decision":"needs_human","question":"Which repo should change?"}'])

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / ".symphony" / "symphony.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            self.assertIsNotNone(result.run)
            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "planning")
            self.assertEqual(result.run.error, "Which repo should change?")

    def test_plan_assumption_needing_human_blocks_planning_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = write_workflow(
                root,
                write_fake_codex(root),
                codex_extra="""
  plan_before_implementation: true
  planning_prompt: |
    Write a plan only.
""",
            )
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = MessageCodexRunner(
                [
                    '{"decision":"ready_for_approval","assumptions":[{"assumption":"Place the new column after Status","needs_human":true}],"questions":[]}'
                ]
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / ".symphony" / "symphony.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            self.assertIsNotNone(result.run)
            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "planning")
            self.assertEqual(result.run.error, "Place the new column after Status")

    def test_needs_human_json_blocks_implementation_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(write_workflow(root, write_fake_codex(root)), environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = MessageCodexRunner(['{"decision":"needs_human","question":"Should I update translations?"}'])

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / ".symphony" / "symphony.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            self.assertIsNotNone(result.run)
            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "implementation")
            self.assertEqual(result.run.error, "Should I update translations?")

    def test_needs_human_json_blocks_review_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = write_workflow(
                root,
                write_fake_codex(root),
                codex_extra="""
  review_after_run: true
  max_review_iterations: 1
""",
            )
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = MessageCodexRunner(
                [
                    "Implementation complete.",
                    '{"decision":"needs_human","question":"Is this behavior acceptable?"}',
                ]
            )

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / ".symphony" / "symphony.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            self.assertIsNotNone(result.run)
            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "review")
            self.assertEqual(result.run.error, "Is this behavior acceptable?")

    def test_plan_approval_gate_waits_for_human_before_implementation(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow_path = write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  require_plan_approval: true
  planning_prompt: |
    Write a plan only.
""",
                )
                workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
                issue = Issue(
                    id="10001",
                    identifier="T-1",
                    title="Fix bug",
                    description="Please fix",
                    status="To Do",
                    labels=["codex-ready"],
                    url="https://jira.example.test/browse/T-1",
                )
                store = Store(root / ".symphony" / "symphony.sqlite3")
                runner = PlanThenImplementCodexRunner()
                jira = FakeJira(issue)

                first = await SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once("T-1")

                self.assertIsNotNone(first.run)
                assert first.run is not None
                self.assertEqual(first.run.status, "blocked")
                self.assertEqual(first.run.blocked_phase, "planning_approval")
                self.assertEqual(runner.prompts_seen, ["plan"])

                plan_text = (Path(first.run.workspace_path) / workflow.config.codex.output_plan_file).read_text(
                    encoding="utf-8"
                )
                approval = store.add_plan_approval(
                    "T-1",
                    run_id=first.run.id,
                    approver_identity="test-user",
                    plan_spec_hash=parse_plan_spec(plan_text).content_hash(),
                    requirements_snapshot_hash=first.run.issue_fingerprint or "",
                )
                store.add_human_input(
                    "T-1",
                    run_id=first.run.id,
                    response="Approved.",
                    approval_id=str(approval["id"]),
                )
                polling = PollingOrchestrator(workflow, jira, store, codex_runner=runner)
                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                latest = store.latest_run_for_issue("T-1")
                self.assertIsNotNone(latest)
                assert latest is not None
                self.assertEqual(latest.status, "completed")
                self.assertEqual(runner.prompts_seen, ["plan", "implementation"])
                self.assertIn("human plan approval", runner.implementation_prompt)
                self.assertIn("Approved.", runner.implementation_prompt)

        asyncio.run(run())

    def test_epic_forces_planspec_even_when_planning_config_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root, write_fake_codex(root)),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Role-aware report Epic",
                description="Implement the Epic.",
                status="To Do",
                issue_type="Epic",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            runner = PlanThenImplementCodexRunner()

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / ".symphony" / "symphony.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "planning_approval")
            self.assertIn("ready", result.run.error or "")
            self.assertEqual(runner.prompts_seen, ["plan"])
            plan = parse_plan_spec(
                (Path(result.run.workspace_path) / workflow.config.codex.output_plan_file).read_text(
                    encoding="utf-8"
                ),
                issue_type="Epic",
            )
            assert plan.epic_strategy is not None
            self.assertEqual(plan.epic_strategy.mode, "single_change")
            self.assertTrue(
                plan.epic_strategy.requires_explicit_single_change_approval
            )

    def test_epic_with_contextual_child_issue_still_defaults_to_safe_single_change(self) -> None:
        plan = parse_plan_spec(
            valid_plan_spec_message(
                'requirements_snapshot_hash is "' + ("a" * 64) + '"',
                "One change",
            )
        )
        issue = hydrated_test_issue(
            Issue(
                id="10001",
                identifier="T-1",
                title="Parent Epic",
                description="Implement it",
                status="To Do",
                issue_type="Epic",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
        )
        assert issue.requirements_snapshot is not None
        child = SimpleNamespace(identifier="T-2")
        snapshot = issue.requirements_snapshot.model_copy(update={"children": [child]})
        issue = issue.model_copy(update={"requirements_snapshot": snapshot})

        normalized = apply_default_epic_strategy(plan, issue)

        self.assertIsNotNone(normalized.epic_strategy)
        assert normalized.epic_strategy is not None
        self.assertEqual(normalized.epic_strategy.mode, "single_change")
        self.assertTrue(
            normalized.epic_strategy.requires_explicit_single_change_approval
        )

    def test_plan_approval_feedback_refines_plan_and_waits_for_approval_again(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow_path = write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  require_plan_approval: true
  planning_prompt: |
    Write a plan only.
""",
                )
                workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
                issue = Issue(
                    id="10001",
                    identifier="T-1",
                    title="Fix bug",
                    description="Please fix",
                    status="To Do",
                    labels=["codex-ready"],
                    url="https://jira.example.test/browse/T-1",
                )
                store = Store(root / ".symphony" / "symphony.sqlite3")
                runner = PlanFeedbackCodexRunner()
                jira = FakeJira(issue)

                first = await SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once("T-1")

                self.assertIsNotNone(first.run)
                assert first.run is not None
                self.assertEqual(first.run.status, "blocked")
                self.assertEqual(first.run.blocked_phase, "planning_approval")

                store.add_human_input("T-1", run_id=first.run.id, response="Put the new column after Amount.")
                polling = PollingOrchestrator(workflow, jira, store, codex_runner=runner)
                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                revised = store.latest_run_for_issue("T-1")
                self.assertIsNotNone(revised)
                assert revised is not None
                self.assertEqual(revised.status, "blocked")
                self.assertEqual(revised.blocked_phase, "planning_approval")
                self.assertEqual(runner.prompts_seen, ["plan", "plan_refinement"])
                self.assertIn("Human feedback to incorporate", runner.refinement_prompt)
                self.assertIn("Put the new column after Amount.", runner.refinement_prompt)
                assert first.workspace is not None
                self.assertEqual(
                    json.loads(
                        (first.workspace.path / ".symphony" / "codex-plan.md").read_text(encoding="utf-8")
                    )["simplest_implementation"],
                    "Put the column after Amount.",
                )

        asyncio.run(run())

    def test_planning_clarification_refines_plan_then_waits_for_approval(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow_path = write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  require_plan_approval: true
  planning_prompt: |
    Write a plan only.
""",
                )
                workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
                issue = Issue(
                    id="10001",
                    identifier="T-1",
                    title="Fix bug",
                    description="Please fix",
                    status="To Do",
                    labels=["codex-ready"],
                    url="https://jira.example.test/browse/T-1",
                )
                store = Store(root / ".symphony" / "symphony.sqlite3")
                runner = PlanningQuestionThenPlanCodexRunner()
                jira = FakeJira(issue)

                first = await SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once("T-1")

                self.assertIsNotNone(first.run)
                assert first.run is not None
                self.assertEqual(first.run.status, "blocked")
                self.assertEqual(first.run.blocked_phase, "planning")
                self.assertEqual(first.run.error, "Where should the column go?")

                store.add_human_input("T-1", run_id=first.run.id, response="After Amount.")
                polling = PollingOrchestrator(workflow, jira, store, codex_runner=runner)
                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()

                revised = store.latest_run_for_issue("T-1")
                self.assertIsNotNone(revised)
                assert revised is not None
                self.assertEqual(revised.status, "blocked")
                self.assertEqual(revised.blocked_phase, "planning_approval")
                self.assertEqual(runner.prompts_seen, ["plan_question", "plan_refinement"])
                assert first.workspace is not None
                self.assertEqual(
                    json.loads(
                        (first.workspace.path / ".symphony" / "codex-plan.md").read_text(encoding="utf-8")
                    )["simplest_implementation"],
                    "Put the column after Amount.",
                )

        asyncio.run(run())

    def test_missing_requirements_snapshot_blocks_initial_and_resumed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root, write_fake_codex(root)),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            jira = FakeJira(issue, hydrate_requirements=False)
            store = Store(root / "db.sqlite3")
            runner = PromptStatusCodexRunner(["completed"])

            first = asyncio.run(
                SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once("T-1")
            )
            assert first.run is not None
            self.assertEqual(first.run.status, "blocked")
            self.assertEqual(first.run.blocked_phase, "planning")
            self.assertIn("Canonical Jira requirements snapshot is missing", first.run.error or "")
            self.assertIsNone(first.workspace)
            self.assertEqual(runner.prompts, [])

            second = asyncio.run(
                SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once(
                    "T-1",
                    attempt=2,
                    previous_run=first.run,
                    human_input={"response": "Approved."},
                )
            )
            assert second.run is not None
            self.assertEqual(second.run.status, "blocked")
            self.assertIn("Canonical Jira requirements snapshot is missing", second.run.error or "")
            self.assertEqual(runner.prompts, [])

    def test_incomplete_attachment_analysis_never_blocks_planning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root, write_fake_codex(root)),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            issue = hydrated_issue(
                attachment_status="not_configured",
                incomplete_reasons=["role-matrix.png has no vision summary"],
            )
            jira = FakeJira(issue)
            store = Store(root / "db.sqlite3")
            runner = PromptStatusCodexRunner(["completed"])

            first = asyncio.run(
                SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once("T-1")
            )
            assert first.run is not None
            self.assertEqual(first.run.status, "completed")
            self.assertIsNotNone(first.workspace)
            self.assertEqual(len(runner.prompts), 1)
            self.assertNotIn("role-matrix.png", runner.prompts[0])

    def test_incomplete_snapshot_reason_blocks_even_without_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root, write_fake_codex(root)),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            issue = hydrated_issue(incomplete_reasons=["comments pagination did not complete"])
            runner = PromptStatusCodexRunner(["completed"])

            result = asyncio.run(
                SingleIssueOrchestrator(
                    workflow,
                    FakeJira(issue),
                    Store(root / "db.sqlite3"),
                    codex_runner=runner,
                ).run_once("T-1")
            )

            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "planning")
            self.assertIn("Requirements snapshot is incomplete", result.run.error or "")
            self.assertIn("comments pagination did not complete", result.run.error or "")
            self.assertEqual(runner.prompts, [])

    def test_contradiction_requires_versioned_jira_resolution_then_new_planspec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  require_plan_approval: true
""",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            jira = FakeJira(hydrated_issue(unresolved_contradiction=True))
            store = Store(root / "db.sqlite3")
            runner = PlanThenImplementCodexRunner()

            first = asyncio.run(
                SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once("T-1")
            )
            assert first.run is not None
            self.assertEqual(first.run.status, "blocked")
            self.assertEqual(first.run.blocked_phase, "planning")
            self.assertIn("Unresolved Jira requirement contradictions", first.run.error or "")
            self.assertIn("Update Jira with the authoritative decision", first.run.error or "")
            self.assertEqual(runner.prompts_seen, [])

            clarification = "D-ROLE is resolved in Jira: GC acting as Sub follows Sub behavior."
            still_unresolved = asyncio.run(
                SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once(
                    "T-1",
                    attempt=2,
                    previous_run=first.run,
                    human_input={"response": clarification},
                )
            )
            assert still_unresolved.run is not None
            self.assertEqual(still_unresolved.run.status, "blocked")
            self.assertEqual(runner.prompts_seen, [])

            resolved = hydrated_issue()
            jira.issue = resolved
            jira.issues = [resolved]
            resumed = asyncio.run(
                SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once(
                    "T-1",
                    attempt=3,
                    previous_run=still_unresolved.run,
                    human_input={"response": clarification},
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "planning_approval", resumed.run.error)
            self.assertEqual(runner.prompts_seen, ["plan"])
            self.assertNotIn(clarification, runner.plan_prompt)
            self.assertIn("prior dashboard response", runner.plan_prompt)
            self.assertIn("GC acting as Sub follows Sub behavior.", runner.plan_prompt)

    def test_planning_approval_finish_comment_includes_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            issue = Issue(id="1", identifier="T-1", title="Title", status="To Do", labels=[], url="u")
            store = Store(root / "db.sqlite3")
            run = store.create_run(issue, root / "workspaces" / "T-1", branch_name=None)
            run = store.update_run(
                run.id,
                status="blocked",
                blocked_phase="planning_approval",
                final_message="Plan: change the report message.",
            )

            comment = finish_comment(issue, run)

            self.assertIn("Codex plan/spec is ready for T-1", comment)
            self.assertIn("waiting for human plan approval", comment)
            self.assertIn("Plan: change the report message.", comment)
            self.assertIn("Confirm the plan", comment)

    def test_review_loop_reruns_generation_when_review_requires_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = write_fake_codex(root)
            workflow_path = write_workflow(
                root,
                fake_codex,
                codex_extra="""
  review_after_run: true
  max_review_iterations: 1
  review_prompt: |
    Review the diff. Start with APPROVE or CHANGES_REQUIRED.
""",
            )
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                priority="High",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            jira = FakeJira(issue)
            store = Store(root / ".symphony" / "symphony.sqlite3")
            runner = ReviewLoopCodexRunner()

            result = asyncio.run(SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once("T-1"))

            self.assertIsNotNone(result.run)
            assert result.run is not None
            self.assertEqual(result.run.status, "blocked")
            self.assertEqual(result.run.blocked_phase, "review")
            self.assertIn("remain unreviewed", result.run.error or "")
            self.assertEqual(result.run.final_message, "fixed implementation")
            self.assertEqual(runner.prompts_seen, ["implementation", "review", "regeneration"])
            assert result.workspace is not None
            self.assertEqual(
                (result.workspace.path / ".symphony" / "codex-review.md").read_text(encoding="utf-8"),
                "CHANGES_REQUIRED\nFix the missing edge case.",
            )
            self.assertIn(
                "Review pass 1",
                (result.workspace.path / ".symphony" / "codex-review-history.md").read_text(encoding="utf-8"),
            )


    def test_plan_binding_survives_needs_human_and_rejects_edited_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow_path = write_workflow(
                root,
                write_fake_codex(root),
                codex_extra="""
  plan_before_implementation: true
  require_plan_approval: true
  planning_prompt: |
    Write a plan only.
""",
            )
            workflow = load_workflow(workflow_path, environ={"TEST_JIRA_TOKEN": "token"})
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            store = Store(root / "db.sqlite3")
            runner = PlanNeedsHumanThenCompleteRunner()
            jira = FakeJira(issue)
            orchestrator = SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner)

            first = asyncio.run(orchestrator.run_once("T-1"))
            assert first.run is not None
            plan_path = Path(first.run.workspace_path) / workflow.config.codex.output_plan_file
            plan_text = plan_path.read_text(encoding="utf-8")
            plan_hash = parse_plan_spec(plan_text).content_hash()
            approval = store.add_plan_approval(
                "T-1",
                run_id=first.run.id,
                approver_identity="ada@example.test",
                plan_spec_hash=plan_hash,
                requirements_snapshot_hash=first.run.issue_fingerprint or "",
            )
            store.add_human_input(
                "T-1",
                run_id=first.run.id,
                response="Approved.",
                approval_id=str(approval["id"]),
            )
            approved_input = store.list_unconsumed_human_inputs()[0]

            blocked = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=approved_input,
                    previous_run=first.run,
                )
            )
            assert blocked.run is not None
            self.assertEqual(blocked.run.status, "blocked")
            self.assertEqual(blocked.run.blocked_phase, "implementation")
            self.assertEqual(blocked.run.plan_spec_hash, plan_hash)
            self.assertEqual(blocked.run.plan_approval_id, approval["id"])

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=3,
                    human_input={"response": "Use option A."},
                    previous_run=blocked.run,
                )
            )
            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "completed")
            self.assertIn("Trusted PlanSpec continuity binding", runner.implementation_prompts[-1])
            self.assertIn(plan_text.strip(), runner.implementation_prompts[-1])

            payload = json.loads(plan_text)
            payload["simplest_implementation"] = "Tampered scope."
            plan_path.write_text(json.dumps(payload), encoding="utf-8")
            prompt_count = len(runner.implementation_prompts)
            tampered = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=4,
                    human_input={"response": "Continue."},
                    previous_run=blocked.run,
                )
            )
            assert tampered.run is not None
            self.assertEqual(tampered.run.status, "blocked")
            self.assertEqual(tampered.run.blocked_phase, "planning")
            self.assertIn("PlanSpec", tampered.run.error or "")
            self.assertEqual(len(runner.implementation_prompts), prompt_count)
            persisted_approval = store.get_plan_approval(str(approval["id"]))
            assert persisted_approval is not None
            self.assertIsNotNone(persisted_approval["invalidated_at"])

            plan_path.write_text(plan_text, encoding="utf-8")
            plan_path.unlink()
            missing_error = validate_plan_artifact(
                Path(blocked.run.workspace_path),
                workflow.config.codex.output_plan_file,
                expected_hash=plan_hash,
                issue=hydrated_test_issue(issue),
                requirements_snapshot_hash=first.run.issue_fingerprint or "",
            )
            self.assertIn("artifact is missing", missing_error or "")
            plan_path.write_text(plan_text, encoding="utf-8")
            commit_test_git_repository(Path(blocked.run.workspace_path) / "repo", "drift")
            drift_error = validate_plan_artifact(
                Path(blocked.run.workspace_path), workflow.config.codex.output_plan_file,
                expected_hash=plan_hash, issue=hydrated_test_issue(issue),
                requirements_snapshot_hash=first.run.issue_fingerprint or "",
            )
            self.assertIn("baseline drift", drift_error or "")


    def test_clean_baseline_allows_only_untracked_symphony_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            sha = ensure_test_git_repository(workspace)
            repository = workspace / "repo"

            def plan_for(baseline_sha: str):
                return parse_plan_spec(
                    valid_plan_spec_message(
                        f'requirements_snapshot_hash "{"b" * 64}"',
                        "Use the simplest implementation.",
                        baseline_sha=baseline_sha,
                    )
                )

            (repository / "source.py").write_text("untracked", encoding="utf-8")
            error = validate_plan_repository_baselines(
                plan_for(sha), workspace, require_clean=True
            )
            self.assertIn("not clean", error or "")
            (repository / "source.py").unlink()

            artifact = repository / ".symphony" / "codex-plan.md"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("generated", encoding="utf-8")
            self.assertIsNone(
                validate_plan_repository_baselines(
                    plan_for(sha), workspace, require_clean=True
                )
            )

            tracked = repository / ".symphony" / "tracked.json"
            tracked.write_text("baseline", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repository), "add", ".symphony/tracked.json"],
                check=True,
                capture_output=True,
                text=True,
            )
            sha = commit_test_git_repository(repository, "track symphony state")
            tracked.write_text("modified", encoding="utf-8")
            error = validate_plan_repository_baselines(
                plan_for(sha), workspace, require_clean=True
            )
            self.assertIn("not clean", error or "")

    def test_clean_baseline_requires_tracked_non_symphony_precedents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_test_git_repository(workspace)
            repository = workspace / "repo"
            module = repository / "module.py"
            module.write_text("precedent", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repository), "add", "module.py"],
                check=True,
                capture_output=True,
                text=True,
            )
            sha = commit_test_git_repository(repository, "add precedent")

            def plan_with_precedent(path: str):
                payload = json.loads(
                    valid_plan_spec_message(
                        f'requirements_snapshot_hash "{"b" * 64}"',
                        "Use the simplest implementation.",
                        baseline_sha=sha,
                    )
                )
                payload["existing_precedents"] = [
                    {
                        "repository": "repo",
                        "path": path,
                        "description": "Follow the existing pattern.",
                    }
                ]
                return parse_plan_spec(json.dumps(payload))

            self.assertIsNone(
                validate_plan_repository_baselines(
                    plan_with_precedent("module.py"),
                    workspace,
                    require_clean=True,
                )
            )

            (repository / ".git" / "info" / "exclude").write_text(
                "ignored.py\n", encoding="utf-8"
            )
            (repository / "ignored.py").write_text("ignored", encoding="utf-8")
            error = validate_plan_repository_baselines(
                plan_with_precedent("ignored.py"),
                workspace,
                require_clean=True,
            )
            self.assertIn("not Git-tracked", error or "")

            symphony_precedent = repository / ".symphony" / "reference.py"
            symphony_precedent.parent.mkdir(parents=True, exist_ok=True)
            symphony_precedent.write_text("generated", encoding="utf-8")
            error = validate_plan_repository_baselines(
                plan_with_precedent(".symphony/reference.py"),
                workspace,
                require_clean=True,
            )
            self.assertIn("cannot use Symphony artifact paths", error or "")

    def test_post_implementation_checkpoint_invalidates_mutated_plan_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(
                    root,
                    write_fake_codex(root),
                    codex_extra="""
  plan_before_implementation: true
  require_plan_approval: true
  planning_prompt: |
    Write a plan only.
""",
                ),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            issue = Issue(
                id="10001",
                identifier="T-1",
                title="Fix bug",
                description="Please fix",
                status="To Do",
                labels=["codex-ready"],
                url="https://jira.example.test/browse/T-1",
            )
            jira = FakeJira(issue)
            store = Store(root / "db.sqlite3")
            runner = PostPassPlanMutationRunner()
            orchestrator = SingleIssueOrchestrator(
                workflow, jira, store, codex_runner=runner
            )

            planned = asyncio.run(orchestrator.run_once("T-1"))
            assert planned.run is not None
            self.assertEqual(planned.run.status, "blocked")
            self.assertEqual(planned.run.blocked_phase, "planning_approval")
            assert planned.run.plan_spec_hash is not None
            approval = store.add_plan_approval(
                "T-1",
                run_id=planned.run.id,
                approver_identity="ada@example.test",
                plan_spec_hash=planned.run.plan_spec_hash,
                requirements_snapshot_hash=planned.run.issue_fingerprint or "",
            )
            store.add_human_input(
                "T-1",
                run_id=planned.run.id,
                response="Approved.",
                approval_id=str(approval["id"]),
            )

            resumed = asyncio.run(
                orchestrator.run_once(
                    "T-1",
                    attempt=2,
                    human_input=store.list_unconsumed_human_inputs()[0],
                    previous_run=planned.run,
                )
            )

            assert resumed.run is not None
            self.assertEqual(resumed.run.status, "blocked")
            self.assertEqual(resumed.run.blocked_phase, "planning")
            self.assertIn("artifact is missing", resumed.run.error or "")
            persisted_approval = store.get_plan_approval(str(approval["id"]))
            assert persisted_approval is not None
            self.assertIsNotNone(persisted_approval["invalidated_at"])
            self.assertEqual(runner.implementation_passes, 1)

def hydrated_issue(
    *,
    attachment_status: str | None = None,
    unresolved_contradiction: bool = False,
    incomplete_reasons: list[str] | None = None,
) -> Issue:
    description_source = RequirementSource(
        issue_identifier="T-1",
        source_type="description",
        source_id="description",
        author="product-owner",
        authority="product",
    )
    description = RequirementArtifact(
        artifact_id="T-1:description",
        source_type="description",
        text="Implement role-aware behavior.",
        source=description_source,
    )
    attachments: list[IssueAttachment] = []
    if attachment_status is not None:
        attachments.append(
            IssueAttachment(
                id="ATT-1",
                filename="role-matrix.png",
                mime_type="image/png",
                source=RequirementSource(
                    issue_identifier="T-1",
                    source_type="attachment",
                    source_id="ATT-1",
                    author="product-owner",
                    authority="supporting_evidence",
                ),
                analysis=AttachmentAnalysis(
                    status=attachment_status,
                    modality="vision",
                    summary=(
                        "Shows the GC, Sub, and GC-as-Sub role matrix."
                        if attachment_status == "complete"
                        else "Attachment analyzer is not configured."
                    ),
                ),
            )
        )
    unresolved = [
        RequirementDecision(
            id="D-ROLE",
            text="GC-as-Sub behavior conflicts between Jira sources.",
            classification="unresolved_contradiction",
            sources=[description_source],
        )
    ] if unresolved_contradiction else []
    current = [] if unresolved_contradiction else [
        RequirementDecision(
            id="R-ROLE",
            text="GC acting as Sub follows Sub behavior.",
            classification="current",
            sources=[description_source],
        ),
        RequirementDecision(
            id="AC-ROLE",
            text="The role-aware behavior is observable.",
            kind="acceptance_criterion",
            classification="current",
            sources=[description_source],
        ),
    ]
    snapshot = RequirementsSnapshot(
        issue_id="10001",
        issue_identifier="T-1",
        issue_url="https://jira.example.test/browse/T-1",
        description=description,
        attachments=attachments,
        current_requirements=current,
        unresolved_contradictions=unresolved,
        incomplete_reasons=incomplete_reasons or [],
    )
    return Issue(
        id="10001",
        identifier="T-1",
        title="Role-aware behavior",
        description=description.text,
        status="To Do",
        labels=["codex-ready"],
        url=snapshot.issue_url,
        requirements_snapshot=snapshot,
    )


def completed_review_issue() -> Issue:
    return Issue(
        id="1",
        identifier="T-1",
        title="Completed implementation",
        description="Implement the exact reviewed behavior.",
        status="Done",
        labels=["codex-ready"],
        url="https://jira.example.test/browse/T-1",
    )


def load_completed_review_workflow(root: Path):
    workflow_path = write_workflow(
        root,
        write_fake_codex(root),
        codex_extra="""
  plan_before_implementation: true
  require_plan_approval: true
  review_after_run: false
  max_review_iterations: 1
""",
    )
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            '  active_statuses: ["To Do"]',
            '  active_statuses: ["To Do"]\n  terminal_statuses: ["Done"]',
        ),
        encoding="utf-8",
    )
    return load_workflow(
        workflow_path,
        environ={"TEST_JIRA_TOKEN": "token"},
    )


def create_completed_review_action(
    root: Path,
    workflow,
    issue: Issue,
    store: Store,
):
    frozen_issue = hydrated_test_issue(issue)
    workspace_path = root / "workspaces" / issue.identifier
    baseline_sha = ensure_test_git_repository(workspace_path)
    source_run = store.create_run(
        frozen_issue,
        workspace_path,
        branch_name=None,
        attempt=3,
    )
    snapshot_hash = source_run.issue_fingerprint
    assert snapshot_hash is not None
    plan_message = valid_plan_spec_message(
        f'requirements_snapshot_hash "{snapshot_hash}"',
        "Keep behavior stable and add the scoped regression coverage.",
        baseline_sha=baseline_sha,
    )
    plan_spec = parse_plan_spec(
        plan_message,
        expected_issue_key=issue.identifier,
        expected_snapshot_hash=snapshot_hash,
        requirements_snapshot=frozen_issue.requirements_snapshot,
    )
    plan_content = plan_spec.canonical_json(indent=2)

    assert frozen_issue.requirements_snapshot is not None
    write_requirements_snapshot_artifacts(
        workspace_path,
        frozen_issue.requirements_snapshot,
    )
    plan_path = workspace_path / workflow.config.codex.output_plan_file
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(plan_content, encoding="utf-8")
    (workspace_path / workflow.config.codex.output_last_message_file).write_text(
        "Original implementation completed.",
        encoding="utf-8",
    )
    (workspace_path / workflow.config.codex.output_review_file).write_text(
        '{"decision":"approve","findings":[],"summary":"Original automated review"}',
        encoding="utf-8",
    )
    (
        workspace_path / workflow.config.codex.output_review_history_file
    ).write_text(
        "## Original automated review\n\n"
        '{"decision":"approve","findings":[]}',
        encoding="utf-8",
    )

    approval = store.add_plan_approval(
        issue.identifier,
        run_id=source_run.id,
        approver_identity="plan-owner@example.test",
        plan_spec_hash=plan_spec.content_hash(),
        requirements_snapshot_hash=snapshot_hash,
    )
    source_run = store.update_run(
        source_run.id,
        status="completed",
        finished_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        final_message="Original implementation completed.",
        verification_status="passed",
    )
    action, result_run = store.create_human_review_action(
        source_run.id,
        reviewer_identity="reviewer@example.test",
        source_url="https://github.example.test/org/repo/pull/42",
        comments="Rename the local variable and add the missing regression test.",
        **prepare_human_review_context(source_run, workflow, store),
    )
    return action, source_run, result_run, approval


class FakeRuntimeManager:
    def __init__(
        self,
        status: str,
        *,
        shutdown_status: str = "stopped",
    ) -> None:
        self.status = status
        self.shutdown_status = shutdown_status
        self.calls: list[dict[str, object]] = []
        self.shutdown_calls: list[dict[str, object]] = []
        self.events: list[str] | None = None
        self.on_shutdown = None
        self.persisted_status_at_shutdown: str | None = None

    async def verify_many(
        self,
        workspace_root,
        repositories,
        *,
        target_args_by_repository=None,
        source_repositories=(),
    ):
        repository_names = tuple(repositories)
        source_names = tuple(source_repositories)
        if self.events is not None:
            self.events.append("verify")
        self.calls.append(
            {
                "workspace_root": Path(workspace_root),
                "repositories": repository_names,
                "source_repositories": source_names,
                "target_args_by_repository": target_args_by_repository,
            }
        )
        now = datetime.now(timezone.utc)
        results = []
        for repository in repository_names:
            log_path = (
                Path(workspace_root)
                / ".symphony"
                / "runtime"
                / f"{repository}-verify-{len(self.calls)}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"fake runtime status: {self.status}\n", encoding="utf-8")
            results.append(
                RuntimeVerificationResult(
                    repository=repository,
                    profile="tests",
                    status=self.status,
                    argv=("pytest",),
                    repository_path=Path(workspace_root) / repository,
                    started_at=now,
                    finished_at=now,
                    returncode=0 if self.status == "passed" else 1,
                    output=f"fake runtime status: {self.status}",
                    log_path=log_path,
                    message=f"fake runtime {self.status}",
                )
            )
        return tuple(results)

    async def shutdown(
        self,
        workspace_root,
        repositories,
        *,
        source_repositories=(),
    ):
        repository_names = tuple(repositories)
        source_names = tuple(source_repositories)
        self.shutdown_calls.append(
            {
                "workspace_root": Path(workspace_root),
                "repositories": repository_names,
                "source_repositories": source_names,
            }
        )
        if self.events is not None:
            self.events.append("shutdown")
        if self.on_shutdown is not None:
            self.persisted_status_at_shutdown = self.on_shutdown()
        log_path = (
            Path(workspace_root)
            / ".symphony"
            / "runtime"
            / "fake-shutdown.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"fake shutdown status: {self.shutdown_status}\n",
            encoding="utf-8",
        )
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            repositories=repository_names,
            services=repository_names,
            status=self.shutdown_status,
            argv=("podman", "compose", "stop"),
            started_at=now,
            finished_at=now,
            returncode=0 if self.shutdown_status == "stopped" else 1,
            output=f"fake shutdown status: {self.shutdown_status}",
            log_path=log_path,
            message=f"fake shutdown {self.shutdown_status}",
        )


def write_runtime_workflow(
    root: Path,
    fake_codex: Path,
    *,
    required: bool,
    shutdown_after_handoff: bool = False,
    repository_key: str = "repo",
    workspace_subdir: str = "repo",
) -> Path:
    path = write_workflow(
        root,
        fake_codex,
        codex_extra="""
  planning_prompt: |
    Write a plan only. Do not edit files.
""",
    )
    runtime_yaml = f"""runtime:
  enabled: true
  required: {str(required).lower()}
  shutdown_after_handoff: {str(shutdown_after_handoff).lower()}
  command: ["podman", "compose"]
  project_directory: "./runtime/project"
  compose_file: "./runtime/project/compose.yml"
  env_file: "./runtime/project/.env"
  project_name: symphony-test
  lock_file: "./runtime/runtime.lock"
  repositories:
    {json.dumps(repository_key)}:
      workspace_subdir: {json.dumps(workspace_subdir)}
      source_env: REPO_SRC
      service: repo
      mount_target: /repo
      verification_profile: tests
  verification_profiles:
    tests:
      argv: ["pytest"]
"""
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("agent:\n", runtime_yaml + "agent:\n")
        .replace(
            '  active_statuses: ["To Do"]',
            '  active_statuses: ["To Do"]\n  handoff_status: Done',
        ),
        encoding="utf-8",
    )
    return path


def create_runtime_case(
    root: Path,
    *,
    runtime_status: str,
    required: bool,
    shutdown_after_handoff: bool = False,
    shutdown_status: str = "stopped",
    repository_key: str = "repo",
    workspace_subdir: str = "repo",
):
    workflow = load_workflow(
        write_runtime_workflow(
            root,
            write_fake_codex(root),
            required=required,
            shutdown_after_handoff=shutdown_after_handoff,
            repository_key=repository_key,
            workspace_subdir=workspace_subdir,
        ),
        environ={"TEST_JIRA_TOKEN": "token"},
    )
    issue = Issue(
        id="10001",
        identifier="T-1",
        title="Fix bug",
        description="Please fix",
        status="To Do",
        labels=["codex-ready"],
        url="https://jira.example.test/browse/T-1",
    )
    jira = FakeJira(issue)
    store = Store(root / "db.sqlite3")
    runtime_manager = FakeRuntimeManager(
        runtime_status,
        shutdown_status=shutdown_status,
    )
    runner = PlanThenImplementCodexRunner(repository=workspace_subdir)
    orchestrator = SingleIssueOrchestrator(
        workflow,
        jira,
        store,
        codex_runner=runner,
        runtime_manager=runtime_manager,
    )
    return orchestrator, runtime_manager, runner, jira


def run_runtime_case(
    root: Path,
    *,
    runtime_status: str,
    required: bool,
    shutdown_after_handoff: bool = False,
    repository_key: str = "repo",
    workspace_subdir: str = "repo",
):
    orchestrator, runtime_manager, _, jira = create_runtime_case(
        root,
        runtime_status=runtime_status,
        required=required,
        shutdown_after_handoff=shutdown_after_handoff,
        repository_key=repository_key,
        workspace_subdir=workspace_subdir,
    )
    result = asyncio.run(orchestrator.run_once("T-1"))
    return result, runtime_manager, jira


def verification_bypass_input(
    orchestrator: SingleIssueOrchestrator,
    blocked_run,
    *,
    approver_identity: str = "operator@example.test",
) -> dict[str, object]:
    context = prepare_verification_bypass_context(
        blocked_run,
        orchestrator.workflow,
        orchestrator.store,
    )
    return {
        "action": "verification_bypass",
        "run_id": blocked_run.id,
        "response": "Verification bypass approved.",
        "approver_identity": approver_identity,
        **context,
    }


def write_workflow(root: Path, fake_codex: Path, codex_extra: str = "") -> Path:
    path = root / "WORKFLOW.md"
    path.write_text(
        f"""---
tracker:
  kind: jira
  base_url: "https://jira.example.test"
  auth:
    token_env: TEST_JIRA_TOKEN
  jql: "project = T"
  required_labels: ["codex-ready"]
  active_statuses: ["To Do"]
  comment_on_start: true
  comment_on_finish: true
workspace:
  root: "./workspaces"
  strategy: hook_only
hooks:
  verify: |
    echo verify ok
agent:
  max_concurrent_agents: 1
  timeout_seconds: 30
codex:
  command: "{fake_codex}"
  args: ["exec", "--json"]
{codex_extra.rstrip()}
---
Issue {{{{ issue.identifier }}}}: {{{{ issue.title }}}}
""",
        encoding="utf-8",
    )
    return path


def write_fake_codex(root: Path) -> Path:
    path = root / "fake_codex.py"
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
if "--version" in sys.argv:
    print("fake codex 0.0")
    sys.exit(0)
print(json.dumps({"type": "item.completed", "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "orchestrator final"}]}}))
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)
    return path


class ReleasableCodexRunner:
    def __init__(self) -> None:
        self.started: list[str] = []
        self._event = asyncio.Event()

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        if "T-1" in prompt:
            self.started.append("T-1")
        elif "T-2" in prompt:
            self.started.append("T-2")
        await self._event.wait()
        self._event.clear()
        return codex_result(workspace_path, "completed")

    def release(self) -> None:
        self._event.set()


class StatusCodexRunner:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        status = self.statuses.pop(0)
        return codex_result(workspace_path, status)


class MessageCodexRunner:
    def __init__(self, messages: list[str]) -> None:
        self.messages = messages

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        return codex_result(
            workspace_path,
            "completed",
            final_message=self.messages.pop(0),
            final_path=config.output_last_message_file,
        )


class PromptStatusCodexRunner(StatusCodexRunner):
    def __init__(self, statuses: list[str]) -> None:
        super().__init__(statuses)
        self.prompts: list[str] = []

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        self.prompts.append(prompt)
        return await super().run(prompt, workspace_path, config, timeout_seconds=timeout_seconds)


class CompletedReviewCodexRunner:
    def __init__(
        self,
        triage_decision: str,
        *,
        gate_triage: bool = False,
    ) -> None:
        self.triage_decision = triage_decision
        self.gate_triage = gate_triage
        self.phases: list[str] = []
        self.args_by_phase: dict[str, list[str]] = {}
        self.triage_started = asyncio.Event()
        self._triage_release = asyncio.Event()

    async def run(
        self,
        prompt,
        workspace_path,
        config,
        *,
        timeout_seconds,
        event_callback=None,
        log_callback=None,
    ):
        if prompt.startswith(
            "You are triaging pasted human code-review feedback"
        ):
            phase = "triage"
            self.phases.append(phase)
            self.args_by_phase[phase] = list(config.args)
            self.triage_started.set()
            if self.gate_triage:
                await self._triage_release.wait()
            reason = (
                "Fits the approved PlanSpec as code-only feedback."
                if self.triage_decision == "code_changes"
                else "Requested feedback changes an acceptance criterion."
            )
            return codex_result(
                workspace_path,
                "completed",
                final_message=json.dumps(
                    {
                        "decision": self.triage_decision,
                        "reason": reason,
                    }
                ),
                final_path=config.output_last_message_file,
            )

        if prompt.startswith("You are reviewing a completed implementation"):
            phase = "review"
            self.phases.append(phase)
            self.args_by_phase[phase] = list(config.args)
            return codex_result(
                workspace_path,
                "completed",
                final_message='{"decision":"approve","findings":[]}',
                final_path=config.output_last_message_file,
            )

        if "Symphony is addressing a human review of completed run" in prompt:
            phase = "implementation"
            self.phases.append(phase)
            self.args_by_phase[phase] = list(config.args)
            (
                Path(workspace_path)
                / "repo"
                / "review_regression_test.py"
            ).write_text(
                "def test_review_regression():\n    assert True\n",
                encoding="utf-8",
            )
            return codex_result(
                workspace_path,
                "completed",
                final_message="Addressed the pasted code review.",
                final_path=config.output_last_message_file,
            )

        raise AssertionError(f"Unexpected completed-review prompt: {prompt[:120]}")

    def release_triage(self) -> None:
        self._triage_release.set()


class ReviewLoopCodexRunner:
    def __init__(self) -> None:
        self.prompts_seen: list[str] = []

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        if "Review the current git diff" in prompt:
            self.prompts_seen.append("review")
            return codex_result(
                workspace_path,
                "completed",
                final_message="CHANGES_REQUIRED\nFix the missing edge case.",
                final_path=config.output_last_message_file,
            )
        if "Review feedback:" in prompt:
            self.prompts_seen.append("regeneration")
            return codex_result(
                workspace_path,
                "completed",
                final_message="fixed implementation",
                final_path=config.output_last_message_file,
            )
        self.prompts_seen.append("implementation")
        return codex_result(
            workspace_path,
            "completed",
            final_message="first implementation",
            final_path=config.output_last_message_file,
        )


class PostPassPlanMutationRunner:
    def __init__(self) -> None:
        self.implementation_passes = 0

    async def run(
        self,
        prompt,
        workspace_path,
        config,
        *,
        timeout_seconds,
        event_callback=None,
        log_callback=None,
    ):
        if "planning pass only" in prompt.lower():
            return codex_result(
                workspace_path,
                "completed",
                final_message=valid_plan_spec_message(
                    prompt,
                    "Edit one file and run verify.",
                    baseline_sha=ensure_test_git_repository(Path(workspace_path)),
                ),
                final_path=config.output_last_message_file,
            )
        self.implementation_passes += 1
        (Path(workspace_path) / ".symphony" / "codex-plan.md").unlink()
        return codex_result(
            workspace_path,
            "completed",
            final_message="implemented",
            final_path=config.output_last_message_file,
        )


class PlanNeedsHumanThenCompleteRunner:
    def __init__(self) -> None:
        self.implementation_prompts: list[str] = []

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        if "planning pass only" in prompt.lower():
            return codex_result(
                workspace_path,
                "completed",
                final_message=valid_plan_spec_message(
                    prompt,
                    "Edit one file and run verify.",
                    baseline_sha=ensure_test_git_repository(Path(workspace_path)),
                ),
                final_path=config.output_last_message_file,
            )
        self.implementation_prompts.append(prompt)
        if len(self.implementation_prompts) == 1:
            return codex_result(
                workspace_path,
                "completed",
                final_message='{"decision":"needs_human","question":"Which compatible option?"}',
            )
        return codex_result(workspace_path, "completed", final_message="implemented")


class PlanThenImplementCodexRunner:
    def __init__(self, *, repository: str = "repo") -> None:
        self.prompts_seen: list[str] = []
        self.implementation_prompt = ""
        self.plan_prompt = ""
        self.repository = repository

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        if "planning pass only" in prompt.lower() or "write the plan/spec now" in prompt.lower():
            self.prompts_seen.append("plan")
            self.plan_prompt = prompt
            return codex_result(
                workspace_path,
                "completed",
                final_message=valid_plan_spec_message(
                    prompt,
                    "Edit one file and run verify.",
                    baseline_sha=ensure_test_git_repository(
                        Path(workspace_path),
                        repository=self.repository,
                    ),
                    repository=self.repository,
                ),
                final_path=config.output_last_message_file,
            )
        self.prompts_seen.append("implementation")
        self.implementation_prompt = prompt
        return codex_result(workspace_path, "completed", final_message="implemented")


class VerificationBypassReviewRunner(PlanThenImplementCodexRunner):
    def __init__(
        self,
        review_decisions: list[str],
        *,
        mutate_review: bool = False,
    ) -> None:
        super().__init__()
        self.review_decisions = list(review_decisions)
        self.mutate_review = mutate_review

    async def run(
        self,
        prompt,
        workspace_path,
        config,
        *,
        timeout_seconds,
        event_callback=None,
        log_callback=None,
    ):
        if "Review the current git diff" in prompt:
            self.prompts_seen.append("review")
            if self.mutate_review:
                (Path(workspace_path) / "repo" / "review-drift.txt").write_text(
                    "changed by review\n",
                    encoding="utf-8",
                )
            decision = self.review_decisions.pop(0)
            return codex_result(
                workspace_path,
                "completed",
                final_message=json.dumps({"decision": decision}),
                final_path=config.output_last_message_file,
            )
        if "Review feedback:" in prompt:
            self.prompts_seen.append("regeneration")
            self.implementation_prompt = prompt
            return codex_result(
                workspace_path,
                "completed",
                final_message="implemented after review",
            )
        return await super().run(
            prompt,
            workspace_path,
            config,
            timeout_seconds=timeout_seconds,
            event_callback=event_callback,
            log_callback=log_callback,
        )


class StructuralPlanRepairCodexRunner:
    def __init__(self) -> None:
        self.prompts_seen: list[str] = []
        self.repair_prompt = ""

    async def run(
        self,
        prompt,
        workspace_path,
        config,
        *,
        timeout_seconds,
        event_callback=None,
        log_callback=None,
    ):
        if "repairing a model-generated PlanSpec" in prompt:
            self.prompts_seen.append("plan_repair")
            self.repair_prompt = prompt
            return codex_result(
                workspace_path,
                "completed",
                final_message=valid_plan_spec_message(
                    prompt,
                    "Repair the structural PlanSpec output.",
                    baseline_sha=ensure_test_git_repository(Path(workspace_path)),
                ),
                final_path=config.output_last_message_file,
            )
        if "planning pass only" in prompt.lower():
            self.prompts_seen.append("plan")
            return codex_result(
                workspace_path,
                "completed",
                final_message='{"decision":"ready_for_approval"}',
                final_path=config.output_last_message_file,
            )
        self.prompts_seen.append("implementation")
        return codex_result(
            workspace_path,
            "completed",
            final_message="implemented",
        )


class AlwaysInvalidPlanCodexRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        prompt,
        workspace_path,
        config,
        *,
        timeout_seconds,
        event_callback=None,
        log_callback=None,
    ):
        self.calls += 1
        return codex_result(
            workspace_path,
            "completed",
            final_message='{"decision":"ready_for_approval"}',
            final_path=config.output_last_message_file,
        )


class PlanFeedbackCodexRunner:
    def __init__(self) -> None:
        self.prompts_seen: list[str] = []
        self.refinement_prompt = ""

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        if "human feedback to incorporate" in prompt.lower():
            self.prompts_seen.append("plan_refinement")
            self.refinement_prompt = prompt
            return codex_result(
                workspace_path,
                "completed",
                final_message=valid_plan_spec_message(
                    prompt, "Put the column after Amount.", baseline_sha=ensure_test_git_repository(Path(workspace_path))
                ),
                final_path=config.output_last_message_file,
            )
        self.prompts_seen.append("plan")
        return codex_result(
            workspace_path,
            "completed",
            final_message=valid_plan_spec_message(
                prompt, "Put the column last.", baseline_sha=ensure_test_git_repository(Path(workspace_path))
            ),
            final_path=config.output_last_message_file,
        )


class PlanningQuestionThenPlanCodexRunner:
    def __init__(self) -> None:
        self.prompts_seen: list[str] = []

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        if "human feedback to incorporate" in prompt.lower():
            self.prompts_seen.append("plan_refinement")
            return codex_result(
                workspace_path,
                "completed",
                final_message=valid_plan_spec_message(
                    prompt, "Put the column after Amount.", baseline_sha=ensure_test_git_repository(Path(workspace_path))
                ),
                final_path=config.output_last_message_file,
            )
        self.prompts_seen.append("plan_question")
        return codex_result(
            workspace_path,
            "completed",
            final_message='{"decision":"needs_human","question":"Where should the column go?"}',
            final_path=config.output_last_message_file,
        )


class AutomationWorkflowCodexRunner:
    def __init__(
        self,
        decision: str,
        *,
        first_automation_plan_needs_human: bool = False,
        first_automation_plan_needs_environment: bool = False,
        unplanned_automation_file: bool = False,
        automation_decisions: list[str] | None = None,
        empty_automation_result: bool = False,
        review_decisions: list[str] | None = None,
        change_development_on_regeneration: bool = False,
        change_development_during_automation: bool = False,
        ignored_automation_mutation_phase: str | None = None,
        ignore_planned_automation_path: bool = False,
        binding_store: Store | None = None,
        automation_implementation_replan: bool = False,
        persistent_automation_implementation_replan: bool = False,
        named_review_phases: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.decision = decision
        self.first_automation_plan_needs_human = (
            first_automation_plan_needs_human
        )
        self.first_automation_plan_needs_environment = (
            first_automation_plan_needs_environment
        )
        self.unplanned_automation_file = unplanned_automation_file
        self.automation_decisions = list(automation_decisions or [decision])
        self.empty_automation_result = empty_automation_result
        self.review_decisions = list(review_decisions or ["approve"])
        self.change_development_on_regeneration = (
            change_development_on_regeneration
        )
        self.change_development_during_automation = (
            change_development_during_automation
        )
        self.ignored_automation_mutation_phase = (
            ignored_automation_mutation_phase
        )
        self.ignore_planned_automation_path = ignore_planned_automation_path
        self.binding_store = binding_store
        self.binding_at_implementation_start = None
        self.automation_implementation_replan = (
            automation_implementation_replan
        )
        self.persistent_automation_implementation_replan = (
            persistent_automation_implementation_replan
        )
        self.named_review_phases = named_review_phases
        self.events = events
        self.phases: list[str] = []
        self.automation_plan_prompts: list[str] = []
        self.automation_plan_diff_hashes: list[str] = []
        self.review_prompt = ""

    def record_phase(self, phase: str) -> None:
        self.phases.append(phase)
        if self.events is not None:
            self.events.append(phase)

    async def run(
        self,
        prompt,
        workspace_path,
        config,
        *,
        timeout_seconds,
        event_callback=None,
        log_callback=None,
    ):
        workspace = Path(workspace_path)
        if prompt.startswith("You are planning test-automation updates"):
            self.record_phase("automation_planning")
            if self.ignored_automation_mutation_phase == "planning":
                (workspace / "automation" / "preexisting-output.tmp").write_text(
                    "mutated during planning\n",
                    encoding="utf-8",
                )
                (workspace / "automation" / "new-output.tmp").write_text(
                    "created during planning\n",
                    encoding="utf-8",
                )
            self.automation_plan_prompts.append(prompt)
            self.automation_plan_diff_hashes.append(
                prompt_json_binding(prompt, "development_workspace_diff_hash")
            )
            if self.first_automation_plan_needs_human:
                self.first_automation_plan_needs_human = False
                return codex_result(
                    workspace,
                    "completed",
                    final_message=(
                        '{"decision":"needs_human","question":'
                        '"Which regression scenario should be retained?"}'
                    ),
                    final_path=config.output_last_message_file,
                )
            if self.first_automation_plan_needs_environment:
                self.first_automation_plan_needs_environment = False
                return codex_result(
                    workspace,
                    "completed",
                    final_message=(
                        '{"decision":"needs_human","question":"Which configured '
                        "automation environment should supply authoritative fixture "
                        'data when no runnable environment is available?"}'
                    ),
                    final_path=config.output_last_message_file,
                )
            decision = (
                self.automation_decisions.pop(0)
                if len(self.automation_decisions) > 1
                else self.automation_decisions[0]
            )
            return codex_result(
                workspace,
                "completed",
                final_message=valid_automation_plan_message(
                    prompt,
                    decision=decision,
                ),
                final_path=config.output_last_message_file,
            )
        if prompt.startswith("You are implementing the validated automation plan"):
            self.record_phase("automation_implementation")
            if self.binding_store is not None:
                self.binding_at_implementation_start = (
                    self.binding_store.latest_run_for_issue("T-1")
                )
            if self.automation_implementation_replan:
                if not self.persistent_automation_implementation_replan:
                    self.automation_implementation_replan = False
                return codex_result(
                    workspace,
                    "completed",
                    final_message=json.dumps(
                        {
                            "decision": "needs_human",
                            "question": (
                                "Which configured automation environment should supply "
                                "the authoritative fixture data, and what are the literal "
                                "Project Created Date values for ABC, ABCD, DEF, and DEFG "
                                "under 8265815gc_1? No runnable environment or "
                                "focused-testng.xml is locally available, so these "
                                "expectations cannot be derived safely."
                            ),
                        }
                    ),
                    final_path=config.output_last_message_file,
                )
            automation_repository = workspace / "automation"
            (automation_repository / "generated-test.java").write_text(
                "final class GeneratedTest {}\n",
                encoding="utf-8",
            )
            if self.unplanned_automation_file:
                (automation_repository / "unexpected-test.java").write_text(
                    "final class UnexpectedTest {}\n",
                    encoding="utf-8",
                )
            if self.change_development_during_automation:
                (workspace / "repo" / "development.py").write_text(
                    "IMPLEMENTED = True\nAUTOMATION_SCOPE_VIOLATION = True\n",
                    encoding="utf-8",
                )
            if self.ignored_automation_mutation_phase == "implementation":
                (automation_repository / "preexisting-output.tmp").write_text(
                    "mutated during implementation\n",
                    encoding="utf-8",
                )
                (automation_repository / "new-output.tmp").write_text(
                    "created during implementation\n",
                    encoding="utf-8",
                )
            result = codex_result(
                workspace,
                "completed",
                final_message="Added the focused automation regression coverage.",
                final_path=config.output_last_message_file,
            )
            if self.empty_automation_result:
                result.final_message = None
                result.final_message_path.write_text("", encoding="utf-8")
            return result
        if prompt.startswith("You are reviewing a completed implementation"):
            review_phase = "review"
            if self.named_review_phases:
                review_phase = (
                    "automation_review"
                    if "automation-review" in config.output_last_message_file
                    else "development_review"
                )
            self.record_phase(review_phase)
            self.review_prompt = prompt
            decision = self.review_decisions.pop(0)
            return codex_result(
                workspace,
                "completed",
                final_message=json.dumps(
                    {"decision": decision, "findings": [], "residual_risk": "low"}
                ),
                final_path=config.output_last_message_file,
            )
        if "Review feedback:" in prompt:
            self.record_phase("regeneration")
            if self.change_development_on_regeneration:
                (workspace / "repo" / "development.py").write_text(
                    "IMPLEMENTED = True\nREVIEW_FIX = True\n",
                    encoding="utf-8",
                )
            return codex_result(
                workspace,
                "completed",
                final_message="Implemented the review correction.",
                final_path=config.output_last_message_file,
            )
        if config.output_last_message_file == ".symphony/codex-plan.md" and (
            "write the plan/spec now" in prompt.lower()
            or prompt.startswith("You are revising the implementation plan/spec")
        ):
            self.record_phase("development_plan")
            baseline_sha = ensure_test_git_repository(workspace)
            ensure_test_git_repository(workspace, repository="automation")
            subprocess.run(
                ["git", "-C", str(workspace / "automation"), "checkout", "-q", "-B", "T-1"],
                check=True,
            )
            return codex_result(
                workspace,
                "completed",
                final_message=valid_plan_spec_message(
                    prompt,
                    "Implement the focused development change.",
                    baseline_sha=baseline_sha,
                ),
                final_path=config.output_last_message_file,
            )

        self.record_phase("development_implementation")
        (workspace / "repo" / "development.py").write_text(
            "IMPLEMENTED = True\n",
            encoding="utf-8",
        )
        if (
            self.ignored_automation_mutation_phase is not None
            or self.ignore_planned_automation_path
        ):
            automation_repository = workspace / "automation"
            ignore_file = automation_repository / ".gitignore"
            if not ignore_file.exists():
                ignored_patterns = ["*-output.tmp"]
                if self.ignore_planned_automation_path:
                    ignored_patterns.append("generated-test.java")
                ignore_file.write_text(
                    "\n".join(ignored_patterns) + "\n",
                    encoding="utf-8",
                )
                subprocess.run(
                    ["git", "-C", str(automation_repository), "add", ".gitignore"],
                    check=True,
                )
                commit_test_git_repository(
                    automation_repository,
                    "configure ignored automation outputs",
                )
            if self.ignored_automation_mutation_phase is not None:
                (automation_repository / "preexisting-output.tmp").write_text(
                    "baseline ignored output\n",
                    encoding="utf-8",
                )
        return codex_result(
            workspace,
            "completed",
            final_message="Implemented the development change.",
            final_path=config.output_last_message_file,
        )


def prompt_json_binding(prompt: str, name: str) -> str:
    match = re.search(rf"^- {re.escape(name)}: (.+)$", prompt, flags=re.MULTILINE)
    if not match:
        raise AssertionError(f"automation prompt omitted binding {name}")
    value = json.loads(match.group(1))
    if not isinstance(value, str):
        raise AssertionError(f"automation prompt binding {name} is not a string")
    return value


def valid_automation_plan_message(prompt: str, *, decision: str) -> str:
    update_required = decision == "update_required"
    scenario_ids = ["AUTO-1"] if update_required else []
    return json.dumps(
        {
            "schema_version": "1.0",
            "decision": decision,
            "issue_key": prompt_json_binding(prompt, "issue_key"),
            "requirements_snapshot_hash": prompt_json_binding(
                prompt, "requirements_snapshot_hash"
            ),
            "development_plan_spec_hash": prompt_json_binding(
                prompt, "development_plan_spec_hash"
            ),
            "development_workspace_diff_hash": prompt_json_binding(
                prompt, "development_workspace_diff_hash"
            ),
            "automation_repository": prompt_json_binding(
                prompt, "automation_repository"
            ),
            "repository_baseline_sha": prompt_json_binding(
                prompt, "repository_baseline_sha"
            ),
            "rationale": (
                "Add one focused regression scenario."
                if update_required
                else "The existing automation already covers the changed behavior."
            ),
            "mapped_scenarios": (
                [
                    {
                        "id": "AUTO-1",
                        "description": "Cover the implemented behavior.",
                        "requirement_ids": ["R-1"],
                        "acceptance_criterion_ids": ["AC-1"],
                    }
                ]
                if update_required
                else []
            ),
            "affected_file_changes": (
                [
                    {
                        "path": "generated-test.java",
                        "change_type": "add",
                        "description": "Add focused regression coverage.",
                        "scenario_ids": scenario_ids,
                    }
                ]
                if update_required
                else []
            ),
            "verification": (
                [
                    {
                        "id": "VERIFY-1",
                        "command": "git diff --check",
                        "expected_result": "The automation diff is valid.",
                        "scenario_ids": scenario_ids,
                    }
                ]
                if update_required
                else []
            ),
            "risks": [],
            "assumptions": [],
            "open_questions": [],
        }
    )


def ensure_test_git_repository(
    workspace_path: Path,
    *,
    repository: str = "repo",
) -> str:
    repository_path = workspace_path / repository
    if not (repository_path / ".git").is_dir():
        repository_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", str(repository_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        commit_test_git_repository(repository_path, "baseline")
    return subprocess.run(
        ["git", "-C", str(repository_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


def commit_test_git_repository(repository_path: Path, message: str) -> str:
    subprocess.run(
        [
            "git", "-C", str(repository_path),
            "-c", "user.name=Symphony Test",
            "-c", "user.email=symphony@example.test",
            "commit", "--allow-empty", "-q", "-m", message,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return subprocess.run(
        ["git", "-C", str(repository_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()


def valid_plan_spec_message(
    prompt: str,
    simplest_implementation: str,
    *,
    baseline_sha: str | None = None,
    repository: str = "repo",
) -> str:
    match = re.search(r'requirements_snapshot_hash(?: is)? "([0-9a-f]{64})"', prompt)
    if not match:
        raise AssertionError("planning prompt did not contain the requirements snapshot hash")
    payload = {
        "schema_version": "1.0",
        "decision": "ready_for_approval",
        "issue_key": "T-1",
        "requirements_snapshot_hash": match.group(1),
        "baseline_repository_shas": [
            {"repository": repository, "sha": baseline_sha or "a" * 40}
        ],
        "requirements": [
            {
                "id": "R-1",
                "statement": "Implement the Jira requirement.",
                "jira_sources": [
                    {"issue_key": "T-1", "source_type": "description", "source_id": "description"}
                ],
                "acceptance_criteria": [
                    {
                        "id": "AC-1",
                        "statement": "The behavior is observable.",
                        "jira_sources": [
                            {"issue_key": "T-1", "source_type": "description", "source_id": "description"}
                        ],
                    }
                ],
            }
        ],
        "role_state_matrix": [
            {
                "canonical_role": canonical_role,
                "role": role,
                "state": "applicable",
                "expected_behavior": "The planned behavior is available.",
                "requirement_ids": ["R-1"],
                "acceptance_criterion_ids": ["AC-1"],
            }
            for canonical_role, role in (
                ("gc", "GC"),
                ("sub", "Sub"),
                ("gc_as_sub", "GC acting as Sub"),
            )
        ],
        "affected_surface": {
            "repositories": [repository],
            "files": [
                {"repository": repository, "target": "module.py", "change": "Implement behavior."}
            ],
            "apis": [],
            "schemas": [],
            "migrations": [],
            "translations": [],
        },
        "existing_precedents": [],
        "simplest_implementation": simplest_implementation,
        "assumptions": [],
        "non_goals": ["Unrelated refactoring."],
        "prohibited_scope": ["Do not change unrelated repositories."],
        "test_cases": [
            {
                "id": "T-AC-1",
                "acceptance_criterion_id": "AC-1",
                "level": "unit",
                "description": "Exercise the planned behavior.",
                "expected_result": "AC-1 passes.",
            }
        ],
        "rollout": "Deploy through the normal release path.",
        "rollback": "Revert the scoped change.",
        "compatibility": "No compatibility impact.",
        "risks": [],
        "open_questions": [],
        "epic_strategy": None,
    }
    return json.dumps(payload)


def codex_result(
    workspace_path: Path,
    status: str,
    final_message: str | None = None,
    final_path: str = ".symphony/codex-final.md",
) -> CodexRunResult:
    symphony = workspace_path / ".symphony"
    symphony.mkdir(parents=True, exist_ok=True)
    final_message = final_message if final_message is not None else ("polling final" if status == "completed" else None)
    final_message_path = workspace_path / final_path
    final_message_path.parent.mkdir(parents=True, exist_ok=True)
    final_message_path.write_text(final_message or "", encoding="utf-8")
    return CodexRunResult(
        status=status,
        returncode=0 if status == "completed" else 1,
        final_message=final_message,
        error=None if status == "completed" else "transient network failure",
        stderr_path=symphony / "codex-stderr.log",
        final_message_path=final_message_path,
    )


if __name__ == "__main__":
    unittest.main()
