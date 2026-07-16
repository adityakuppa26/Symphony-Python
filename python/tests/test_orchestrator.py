from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from symphony_jira.codex_runner import CodexRunResult
from symphony_jira.dashboard import prepare_human_review_context
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
from symphony_jira.orchestrator import (
    PollingOrchestrator,
    SingleIssueOrchestrator,
    classify_review_decision,
    finish_comment,
    parse_human_request,
    retry_backoff_seconds,
    select_dispatchable_issues,
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


class OrchestratorTests(unittest.TestCase):
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
            self.assertEqual(result.run.blocked_phase, "planning")
            self.assertIn("Epic PlanSpec", result.run.error or "")
            self.assertEqual(runner.prompts_seen, ["plan"])

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

    def test_incomplete_attachment_analysis_blocks_before_codex_and_cannot_be_approved(self) -> None:
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
            self.assertEqual(first.run.status, "blocked")
            self.assertEqual(first.run.blocked_phase, "planning")
            self.assertIn("Required attachment analysis is incomplete", first.run.error or "")
            self.assertIn("role-matrix.png (not_configured)", first.run.error or "")
            self.assertIn("Human approval cannot waive", first.run.error or "")
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
            self.assertIn("Required attachment analysis is incomplete", second.run.error or "")
            self.assertEqual(runner.prompts, [])

    def test_refreshed_complete_attachment_analysis_allows_retry_to_proceed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = load_workflow(
                write_workflow(root, write_fake_codex(root)),
                environ={"TEST_JIRA_TOKEN": "token"},
            )
            jira = FakeJira(
                hydrated_issue(
                    attachment_status="not_configured",
                    incomplete_reasons=["role-matrix.png has no vision summary"],
                )
            )
            store = Store(root / "db.sqlite3")
            runner = PromptStatusCodexRunner(["completed"])
            first = asyncio.run(
                SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once("T-1")
            )
            assert first.run is not None
            self.assertEqual(first.run.status, "blocked")

            refreshed = hydrated_issue(attachment_status="complete")
            jira.issue = refreshed
            jira.issues = [refreshed]
            second = asyncio.run(
                SingleIssueOrchestrator(workflow, jira, store, codex_runner=runner).run_once(
                    "T-1",
                    attempt=2,
                    previous_run=first.run,
                    human_input={"response": "The analyzer is configured and Jira was refreshed."},
                )
            )

            assert second.run is not None
            self.assertEqual(second.run.status, "completed")
            self.assertEqual(len(runner.prompts), 1)
            self.assertNotIn("The analyzer is configured and Jira was refreshed.", runner.prompts[0])
            self.assertIn("prior dashboard response", runner.prompts[0])

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
    def __init__(self) -> None:
        self.prompts_seen: list[str] = []
        self.implementation_prompt = ""
        self.plan_prompt = ""

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        if "planning pass only" in prompt.lower() or "write the plan/spec now" in prompt.lower():
            self.prompts_seen.append("plan")
            self.plan_prompt = prompt
            return codex_result(
                workspace_path,
                "completed",
                final_message=valid_plan_spec_message(
                    prompt, "Edit one file and run verify.", baseline_sha=ensure_test_git_repository(Path(workspace_path))
                ),
                final_path=config.output_last_message_file,
            )
        self.prompts_seen.append("implementation")
        self.implementation_prompt = prompt
        return codex_result(workspace_path, "completed", final_message="implemented")


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


def ensure_test_git_repository(workspace_path: Path) -> str:
    repository_path = workspace_path / "repo"
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
