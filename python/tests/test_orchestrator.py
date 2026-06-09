from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from symphony_jira.codex_runner import CodexRunResult
from symphony_jira.models import Issue
from symphony_jira.orchestrator import (
    PollingOrchestrator,
    SingleIssueOrchestrator,
    classify_review_decision,
    finish_comment,
    retry_backoff_seconds,
    select_dispatchable_issues,
)
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow


class FakeJira:
    def __init__(self, issue: Issue, issues: list[Issue] | None = None) -> None:
        self.issue = issue
        self.issues = issues or [issue]
        self.comments: list[str] = []
        self.transitions: list[str] = []

    async def search_issues(self, jql: str, limit: int) -> list[Issue]:
        return self.issues[:limit]

    async def get_issue(self, key: str, include_comments: bool = True) -> Issue:
        for issue in self.issues:
            if issue.identifier == key:
                return issue
        return self.issue

    async def add_comment(self, key: str, body: str) -> None:
        self.comments.append(body)

    async def transition_issue(self, key: str, target_status: str) -> bool:
        self.transitions.append(target_status)
        return True


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
                self.assertIsNotNone(store.list_human_inputs(run_id=blocked_run.id)[0]["consumed_at"])

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
                (result.workspace.path / ".symphony" / "codex-plan.md").read_text(encoding="utf-8"),
                "Plan: edit one file and run verify.",
            )
            self.assertIn("Codex planning/spec pass:", runner.implementation_prompt)
            self.assertIn("Plan: edit one file and run verify.", runner.implementation_prompt)

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

                store.add_human_input("T-1", run_id=first.run.id, response="Approved. Keep the change small.")
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
                self.assertIn("Approved. Keep the change small.", runner.implementation_prompt)

        asyncio.run(run())

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
            self.assertEqual(result.run.status, "completed")
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


class PromptStatusCodexRunner(StatusCodexRunner):
    def __init__(self, statuses: list[str]) -> None:
        super().__init__(statuses)
        self.prompts: list[str] = []

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        self.prompts.append(prompt)
        return await super().run(prompt, workspace_path, config, timeout_seconds=timeout_seconds)


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


class PlanThenImplementCodexRunner:
    def __init__(self) -> None:
        self.prompts_seen: list[str] = []
        self.implementation_prompt = ""

    async def run(self, prompt, workspace_path, config, *, timeout_seconds, event_callback=None, log_callback=None):
        if "planning pass only" in prompt.lower() or "write the plan/spec now" in prompt.lower():
            self.prompts_seen.append("plan")
            return codex_result(
                workspace_path,
                "completed",
                final_message="Plan: edit one file and run verify.",
                final_path=config.output_last_message_file,
            )
        self.prompts_seen.append("implementation")
        self.implementation_prompt = prompt
        return codex_result(workspace_path, "completed", final_message="implemented")


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
