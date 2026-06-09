from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .codex_runner import CodexRunner
from .config import WorkflowConfig
from .logging import redact_text
from .models import Issue, RunRecord, issue_description_fingerprint, utc_now
from .store import Store
from .workflow import WorkflowDefinition, render_prompt
from .workspace import HookResult, WorkspaceError, WorkspaceInfo, WorkspaceManager


class JiraLike(Protocol):
    async def search_issues(self, jql: str, limit: int) -> list[Issue]: ...

    async def get_issue(self, key: str, include_comments: bool = True) -> Issue: ...

    async def add_comment(self, key: str, body: str) -> None: ...

    async def transition_issue(self, key: str, target_status: str) -> bool: ...


@dataclass
class OnceResult:
    issue: Issue
    prompt: str
    run: RunRecord | None
    workspace: WorkspaceInfo | None
    dry_run: bool = False


@dataclass
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int
    due_at: float
    error: str | None = None

    def seconds_until_due(self) -> float:
        return max(0.0, self.due_at - time.monotonic())


@dataclass
class RunningIssue:
    issue_id: str
    identifier: str
    attempt: int
    task: asyncio.Task[OnceResult]
    started_at: datetime


class OrchestratorError(Exception):
    """Raised when an issue cannot be dispatched."""


class SingleIssueOrchestrator:
    def __init__(
        self,
        workflow: WorkflowDefinition,
        jira: JiraLike,
        store: Store,
        *,
        workspace_manager: WorkspaceManager | None = None,
        codex_runner: CodexRunner | None = None,
        secret_values: list[str | None] | None = None,
    ) -> None:
        self.workflow = workflow
        self.config = workflow.config
        self.jira = jira
        self.store = store
        self.workspace_manager = workspace_manager or WorkspaceManager(self.config.workspace, self.config.hooks)
        self.codex_runner = codex_runner or CodexRunner()
        self.secret_values = secret_values or []

    async def run_once(
        self,
        issue_key: str,
        *,
        force: bool = False,
        dry_run: bool = False,
        attempt: int = 1,
        human_input: dict[str, Any] | None = None,
        previous_run: RunRecord | None = None,
    ) -> OnceResult:
        issue = await self.jira.get_issue(issue_key, include_comments=True)
        if not force:
            assert_issue_eligible(issue, self.config)

        prompt = render_prompt(self.workflow, issue)
        generation_prompt = prompt
        plan_approved_by_human = (
            human_input is not None
            and previous_run is not None
            and previous_run.blocked_phase == "planning_approval"
        )
        if plan_approved_by_human:
            generation_prompt = build_approved_plan_implementation_prompt(
                issue=issue,
                original_prompt=prompt,
                previous_run=previous_run,
                human_input=human_input or {},
                plan_message=read_plan_message_for_run(previous_run, self.config.codex.output_plan_file),
            )
        elif human_input is not None:
            generation_prompt = build_human_resume_prompt(
                issue=issue,
                original_prompt=prompt,
                previous_run=previous_run,
                human_input=human_input,
            )
        if dry_run:
            return OnceResult(issue=issue, prompt=generation_prompt, run=None, workspace=None, dry_run=True)

        workspace_path = self.workspace_manager.workspace_path_for(issue.identifier)
        branch_name = self.workspace_manager.branch_name_for(issue.identifier)
        run = self.store.create_run(issue, workspace_path, branch_name=branch_name, attempt=attempt, status="queued")
        self.store.update_run(run.id, status="running")

        workspace: WorkspaceInfo | None = None
        status = "failed"
        final_message: str | None = None
        error: str | None = None
        blocked_phase: str | None = None
        verification_status: str | None = None
        verification_output_path: str | None = None
        review_message: str | None = None
        review_history: list[str] = []

        try:
            if self.config.tracker.comment_on_start:
                await self._post_start_comment(issue, run.id, workspace_path, branch_name)

            workspace = await self.workspace_manager.prepare(issue.identifier, hook_context=self.hook_context(issue))

            if self.config.hooks.before_run:
                before = await self.workspace_manager.run_hook(
                    "before_run",
                    self.config.hooks.before_run,
                    workspace.path,
                    hook_context=self.hook_context(issue, workspace),
                )
                if not before.succeeded:
                    raise WorkspaceError(f"before_run hook failed; see {before.log_path}")

            total_event_offset = 0
            plan_message: str | None = None
            run_implementation = True
            if self.config.codex.plan_before_implementation and not plan_approved_by_human:
                plan_prompt = build_planning_prompt(
                    issue=issue,
                    implementation_prompt=generation_prompt,
                    planning_instructions=self.config.codex.planning_prompt,
                )
                plan_config = self.config.codex.model_copy(
                    update={"output_last_message_file": self.config.codex.output_plan_file}
                )
                plan_result = await self.codex_runner.run(
                    plan_prompt,
                    workspace.path,
                    plan_config,
                    timeout_seconds=self.config.agent.timeout_seconds,
                    event_callback=lambda seq, event_type, raw, offset=total_event_offset: self.store.add_codex_event(
                        run.id, offset + seq, f"plan.{event_type}", raw
                    ),
                    log_callback=lambda level, message: self.store.add_log(run.id, level, self.redact(message) or ""),
                )
                total_event_offset += max(len(plan_result.events), 1)
                status = plan_result.status
                plan_message = plan_result.final_message
                error = self.redact(plan_result.error)
                if status != "completed":
                    final_message = plan_message
                    blocked_phase = "planning"
                    run_implementation = False
                else:
                    if self.config.codex.require_plan_approval:
                        status = "blocked"
                        blocked_phase = "planning_approval"
                        final_message = plan_message
                        error = "Plan/spec is ready. Confirm the plan in the dashboard or provide adjustments before implementation."
                        run_implementation = False
                    else:
                        generation_prompt = build_implementation_prompt_with_plan(
                            implementation_prompt=generation_prompt,
                            plan_message=plan_message,
                        )

            generation_pass = 1
            while run_implementation:
                codex_result = await self.codex_runner.run(
                    generation_prompt,
                    workspace.path,
                    self.config.codex,
                    timeout_seconds=self.config.agent.timeout_seconds,
                    event_callback=lambda seq, event_type, raw, offset=total_event_offset: self.store.add_codex_event(
                        run.id, offset + seq, event_type, raw
                    ),
                    log_callback=lambda level, message: self.store.add_log(run.id, level, self.redact(message) or ""),
                )
                total_event_offset += max(len(codex_result.events), 1)
                status = codex_result.status
                final_message = codex_result.final_message
                error = self.redact(codex_result.error)

                if status != "completed":
                    blocked_phase = "implementation"
                    break

                if self.config.hooks.verify:
                    verify = await self.workspace_manager.run_hook(
                        "verify",
                        self.config.hooks.verify,
                        workspace.path,
                        hook_context=self.hook_context(issue, workspace),
                    )
                    verification_status = "passed" if verify.succeeded else "failed"
                    verification_output_path = str(verify.log_path)
                else:
                    verification_status = "not_configured"

                if not self.config.codex.review_after_run:
                    break

                if generation_pass > self.config.codex.max_review_iterations:
                    break

                review_prompt = build_review_prompt(
                    issue=issue,
                    workspace_path=workspace.path,
                    implementation_prompt=prompt,
                    implementation_message=final_message,
                    review_instructions=self.config.codex.review_prompt,
                )
                review_config = self.config.codex.model_copy(
                    update={"output_last_message_file": self.config.codex.output_review_file}
                )
                review_result = await self.codex_runner.run(
                    review_prompt,
                    workspace.path,
                    review_config,
                    timeout_seconds=self.config.agent.timeout_seconds,
                    event_callback=lambda seq, event_type, raw, offset=total_event_offset: self.store.add_codex_event(
                        run.id, offset + seq, f"review.{event_type}", raw
                    ),
                    log_callback=lambda level, message: self.store.add_log(run.id, level, self.redact(message) or ""),
                )
                total_event_offset += max(len(review_result.events), 1)
                review_message = review_result.final_message or review_result.error or ""
                review_history.append(f"## Review pass {generation_pass}\n\n{review_message}".strip())
                write_review_files(workspace.path, self.config.codex, review_message, review_history)

                if review_result.status != "completed":
                    status = review_result.status
                    error = self.redact(review_result.error or "Codex review pass failed")
                    blocked_phase = "review"
                    break

                decision = classify_review_decision(review_message)
                if decision == "changes_required":
                    generation_pass += 1
                    generation_prompt = build_regeneration_prompt(
                        issue=issue,
                        original_prompt=prompt,
                        review_message=review_message,
                    )
                    continue

                if review_message:
                    final_message = append_review_to_final(final_message, review_message)
                break

        except asyncio.CancelledError:
            status = "cancelled"
            error = "Run cancelled by orchestrator"
            blocked_phase = "orchestration"
            raise
        except Exception as exc:
            status = "failed"
            error = self.redact(str(exc))
            blocked_phase = "setup"
        finally:
            if workspace and self.config.hooks.after_run:
                after = await self._run_after_run_best_effort(issue, workspace)
                if after and not after.succeeded:
                    self.store.add_log(run.id, "warning", "after_run hook failed", str(after.log_path))

            updated = self.store.update_run(
                run.id,
                status=status,
                finished_at=utc_now(),
                final_message=final_message,
                error=error,
                blocked_phase=blocked_phase if status in {"blocked", "failed", "cancelled"} else None,
                verification_status=verification_status,
                verification_output_path=verification_output_path,
            )

            if self.config.tracker.comment_on_finish:
                await self._post_finish_comment(issue, updated)

            if status == "completed" and self.config.tracker.handoff_status:
                await self._transition_best_effort(issue, updated)

        stored_run = self.store.get_run(run.id)
        assert stored_run is not None
        return OnceResult(issue=issue, prompt=prompt, run=stored_run, workspace=workspace, dry_run=False)

    async def _run_after_run_best_effort(self, issue: Issue, workspace: WorkspaceInfo) -> HookResult | None:
        try:
            return await self.workspace_manager.run_hook(
                "after_run",
                self.config.hooks.after_run or "",
                workspace.path,
                hook_context=self.hook_context(issue, workspace),
            )
        except Exception as exc:
            self.store.add_log(None, "warning", f"after_run hook failed to execute: {exc}")
            return None

    def redact(self, text: str | None) -> str | None:
        return redact_text(text, self.secret_values)

    def hook_context(self, issue: Issue, workspace: WorkspaceInfo | None = None) -> dict[str, Any]:
        context: dict[str, Any] = {
            "issue": issue,
            "config": self.config,
            "workflow": self.workflow,
        }
        if workspace:
            context.update(
                {
                    "workspace": workspace,
                    "workspace_path": str(workspace.path),
                    "branch_name": workspace.branch_name,
                }
            )
        return context

    async def _post_start_comment(
        self,
        issue: Issue,
        run_id: str,
        workspace_path: Path,
        branch_name: str | None,
    ) -> None:
        body = start_comment(issue, workspace_path, branch_name)
        try:
            await self.jira.add_comment(issue.identifier, body)
            self.store.add_jira_action(issue.identifier, run_id=run_id, action="comment_start", body=body, status="completed")
        except Exception as exc:
            self.store.add_jira_action(
                issue.identifier,
                run_id=run_id,
                action="comment_start",
                body=body,
                status="failed",
                error=self.redact(str(exc)),
            )
            raise

    async def _post_finish_comment(self, issue: Issue, run: RunRecord) -> None:
        body = finish_comment(issue, run)
        try:
            await self.jira.add_comment(issue.identifier, body)
            self.store.add_jira_action(issue.identifier, run_id=run.id, action="comment_finish", body=body, status="completed")
        except Exception as exc:
            self.store.add_jira_action(
                issue.identifier,
                run_id=run.id,
                action="comment_finish",
                body=body,
                status="failed",
                error=self.redact(str(exc)),
            )
            self.store.add_log(run.id, "error", self.redact(f"Failed to post Jira finish comment: {exc}") or "")

    async def _transition_best_effort(self, issue: Issue, run: RunRecord) -> None:
        target = self.config.tracker.handoff_status
        if not target:
            return
        try:
            transitioned = await self.jira.transition_issue(issue.identifier, target)
            status = "completed" if transitioned else "skipped"
            error = None if transitioned else f"No Jira transition found for target status {target}"
            self.store.add_jira_action(
                issue.identifier,
                run_id=run.id,
                action="transition",
                body=target,
                status=status,
                error=error,
            )
        except Exception as exc:
            self.store.add_jira_action(
                issue.identifier,
                run_id=run.id,
                action="transition",
                body=target,
                status="failed",
                error=self.redact(str(exc)),
            )
            self.store.add_log(run.id, "warning", self.redact(f"Jira transition failed: {exc}") or "")


class PollingOrchestrator:
    def __init__(
        self,
        workflow: WorkflowDefinition,
        jira: JiraLike,
        store: Store,
        *,
            workspace_manager: WorkspaceManager | None = None,
        codex_runner: CodexRunner | None = None,
        secret_values: list[str | None] | None = None,
        search_limit: int = 50,
    ) -> None:
        self.workflow = workflow
        self.config = workflow.config
        self.jira = jira
        self.store = store
        self.workspace_manager = workspace_manager
        self.codex_runner = codex_runner
        self.secret_values = secret_values or []
        self.search_limit = search_limit
        self.claimed: set[str] = set()
        self.running: dict[str, RunningIssue] = {}
        self.retry_queue: dict[str, RetryEntry] = {}
        self.completed: dict[str, str] = {}
        self.blocked: set[str] = set()
        self.last_poll_error: str | None = None
        self.last_poll_at: datetime | None = None
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.polling.interval_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def poll_once(self) -> None:
        self.last_poll_at = utc_now()
        await self.reap_finished()
        try:
            issues = await self.jira.search_issues(self.config.tracker.jql, limit=self.search_limit)
            self.last_poll_error = None
        except Exception as exc:
            self.last_poll_error = redact_text(str(exc), self.secret_values)
            self.store.add_log(None, "error", redact_text(f"Jira search failed: {exc}", self.secret_values) or "")
            return

        await self.reconcile_running()
        await self.dispatch_human_resumes()
        await self.dispatch_due_retries()
        self.dispatch_fresh_issues(issues)

    async def reconcile_running(self) -> None:
        for issue_id, running in list(self.running.items()):
            if running.task.done():
                continue
            try:
                current = await self.jira.get_issue(running.identifier, include_comments=False)
            except Exception as exc:
                self.store.add_log(
                    None,
                    "warning",
                    redact_text(f"Could not reconcile {running.identifier}: {exc}", self.secret_values) or "",
                )
                continue

            if current.status in self.config.tracker.terminal_statuses:
                running.task.cancel()
                self.store.add_log(None, "info", f"Cancelled {running.identifier}: terminal status {current.status}")
            elif current.status not in self.config.tracker.active_statuses:
                running.task.cancel()
                self.store.add_log(None, "info", f"Cancelled {running.identifier}: inactive status {current.status}")

    async def reap_finished(self) -> None:
        for issue_id, running in list(self.running.items()):
            if not running.task.done():
                continue
            self.running.pop(issue_id, None)
            try:
                result = running.task.result()
            except asyncio.CancelledError:
                self.claimed.discard(issue_id)
                continue
            except Exception as exc:
                await self._schedule_retry_or_release(running, str(exc))
                continue

            run = result.run
            status = run.status if run else "failed"
            if status == "completed":
                self.completed[issue_id] = issue_description_fingerprint(result.issue)
                self.claimed.discard(issue_id)
                self.retry_queue.pop(issue_id, None)
            elif status == "blocked":
                self.blocked.add(issue_id)
                self.claimed.discard(issue_id)
                self.retry_queue.pop(issue_id, None)
            elif status == "cancelled":
                self.claimed.discard(issue_id)
                self.retry_queue.pop(issue_id, None)
            elif run and is_retryable_error(run.error):
                await self._schedule_retry_or_release(running, run.error)
            else:
                self.claimed.discard(issue_id)
                self.retry_queue.pop(issue_id, None)

    async def dispatch_due_retries(self) -> None:
        now = time.monotonic()
        due_entries = [entry for entry in self.retry_queue.values() if entry.due_at <= now]
        for entry in due_entries:
            if self.available_slots() <= 0:
                return
            self.retry_queue.pop(entry.issue_id, None)
            try:
                issue = await self.jira.get_issue(entry.identifier, include_comments=False)
                assert_issue_eligible(issue, self.config)
            except Exception as exc:
                self.claimed.discard(entry.issue_id)
                self.store.add_log(
                    None,
                    "warning",
                    redact_text(f"Retry skipped for {entry.identifier}: {exc}", self.secret_values) or "",
                )
                continue
            self._start_issue(issue, entry.attempt)

    async def dispatch_human_resumes(self) -> None:
        for human_input in self.store.list_unconsumed_human_inputs():
            if self.available_slots() <= 0:
                return
            previous_run = self.store.get_run(str(human_input["run_id"]))
            if not previous_run or previous_run.status != "blocked":
                self.store.mark_human_input_consumed(str(human_input["id"]))
                continue
            if previous_run.issue_id in self.claimed:
                continue
            try:
                issue = await self.jira.get_issue(previous_run.issue_identifier, include_comments=True)
                assert_issue_eligible(issue, self.config)
            except Exception as exc:
                self.store.add_log(
                    previous_run.id,
                    "warning",
                    redact_text(
                        f"Human clarification resume skipped for {previous_run.issue_identifier}: {exc}",
                        self.secret_values,
                    )
                    or "",
                )
                continue
            self.store.mark_human_input_consumed(str(human_input["id"]))
            self._start_issue(
                issue,
                attempt=previous_run.attempt + 1,
                human_input=human_input,
                previous_run=previous_run,
            )

    def dispatch_fresh_issues(self, issues: list[Issue]) -> None:
        if self.available_slots() <= 0:
            return
        for issue in sort_issues_for_dispatch(issues):
            if self.available_slots() <= 0:
                return
            if issue.id in self.claimed:
                continue
            if self.blocked_waiting_for_human(issue):
                continue
            if self.already_completed(issue):
                continue
            try:
                assert_issue_eligible(issue, self.config)
            except OrchestratorError:
                continue
            self._start_issue(issue, attempt=1)

    def _start_issue(
        self,
        issue: Issue,
        attempt: int,
        *,
        human_input: dict[str, Any] | None = None,
        previous_run: RunRecord | None = None,
    ) -> None:
        self.claimed.add(issue.id)
        self.blocked.discard(issue.id)
        self.completed.pop(issue.id, None)
        single = SingleIssueOrchestrator(
            self.workflow,
            self.jira,
            self.store,
            workspace_manager=self.workspace_manager,
            codex_runner=self.codex_runner,
            secret_values=self.secret_values,
        )
        task = asyncio.create_task(
            single.run_once(issue.identifier, attempt=attempt, human_input=human_input, previous_run=previous_run)
        )
        self.running[issue.id] = RunningIssue(
            issue_id=issue.id,
            identifier=issue.identifier,
            attempt=attempt,
            task=task,
            started_at=utc_now(),
        )

    async def _schedule_retry_or_release(self, running: RunningIssue, error: str | None) -> None:
        if running.attempt <= self.config.agent.max_retries:
            next_attempt = running.attempt + 1
            delay = retry_backoff_seconds(running.attempt, self.config)
            self.retry_queue[running.issue_id] = RetryEntry(
                issue_id=running.issue_id,
                identifier=running.identifier,
                attempt=next_attempt,
                due_at=time.monotonic() + delay,
                error=error,
            )
            self.claimed.add(running.issue_id)
            self.store.add_log(
                None,
                "warning",
                redact_text(f"Retry scheduled for {running.identifier} in {delay}s: {error}", self.secret_values) or "",
            )
            return
        self.claimed.discard(running.issue_id)

    def available_slots(self) -> int:
        return max(0, self.config.agent.max_concurrent_agents - len(self.running))

    def snapshot(self) -> dict[str, Any]:
        return {
            "workflow_path": str(self.workflow.path),
            "jql": self.config.tracker.jql,
            "poll_interval_seconds": self.config.polling.interval_seconds,
            "max_concurrent_agents": self.config.agent.max_concurrent_agents,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "last_poll_error": self.last_poll_error,
            "running": [
                {
                    "issue_id": item.issue_id,
                    "identifier": item.identifier,
                    "attempt": item.attempt,
                    "started_at": item.started_at.isoformat(),
                }
                for item in self.running.values()
            ],
            "retry_queue": [
                {
                    "issue_id": item.issue_id,
                    "identifier": item.identifier,
                    "attempt": item.attempt,
                    "seconds_until_due": item.seconds_until_due(),
                    "error": item.error,
                }
                for item in self.retry_queue.values()
            ],
            "blocked_issue_ids": sorted(self.blocked),
            "completed_issue_ids": sorted(self.completed),
        }

    def already_completed(self, issue: Issue) -> bool:
        fingerprint = issue_description_fingerprint(issue)
        if self.completed.get(issue.id) == fingerprint:
            return True
        previous = self.store.latest_completed_run_for_issue_fingerprint(issue.identifier, fingerprint)
        if previous:
            self.completed[issue.id] = fingerprint
            return True
        return False

    def blocked_waiting_for_human(self, issue: Issue) -> bool:
        latest_run = self.store.latest_run_for_issue(issue.identifier)
        if not latest_run or latest_run.status != "blocked":
            return False
        return self.store.latest_unconsumed_human_input_for_issue(issue.identifier) is None


def assert_issue_eligible(issue: Issue, config: WorkflowConfig) -> None:
    if config.tracker.active_statuses and issue.status not in config.tracker.active_statuses:
        raise OrchestratorError(f"Issue {issue.identifier} status is not active: {issue.status}")
    required = config.tracker.required_label_set
    if required:
        labels = {label.lower() for label in issue.labels}
        missing = sorted(required - labels)
        if missing:
            raise OrchestratorError(f"Issue {issue.identifier} is missing required labels: {', '.join(missing)}")
    unresolved = [
        blocker.identifier or blocker.id or "unknown"
        for blocker in issue.blocked_by
        if (blocker.status or "") not in config.tracker.terminal_statuses
    ]
    if unresolved:
        raise OrchestratorError(f"Issue {issue.identifier} is blocked by unresolved issues: {', '.join(unresolved)}")


def select_dispatchable_issues(
    issues: list[Issue],
    claimed_issue_ids: set[str],
    config: WorkflowConfig,
) -> list[Issue]:
    selected: list[Issue] = []
    for issue in sort_issues_for_dispatch(issues):
        if issue.id in claimed_issue_ids:
            continue
        try:
            assert_issue_eligible(issue, config)
        except OrchestratorError:
            continue
        selected.append(issue)
        if len(selected) >= config.agent.max_concurrent_agents:
            break
    return selected


def sort_issues_for_dispatch(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=lambda issue: (priority_rank(issue.priority), updated_timestamp(issue)))


def priority_rank(priority: str | None) -> int:
    ranks = {
        "highest": 0,
        "blocker": 0,
        "critical": 0,
        "high": 1,
        "major": 1,
        "medium": 2,
        "normal": 2,
        "low": 3,
        "minor": 3,
        "lowest": 4,
        "trivial": 4,
    }
    return ranks.get((priority or "").lower(), 5)


def updated_timestamp(issue: Issue) -> float:
    updated = issue.updated_at
    if updated is None:
        return float("inf")
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return updated.timestamp()


def retry_backoff_seconds(attempt: int, config: WorkflowConfig) -> int:
    if attempt <= 0:
        return 0
    return min(2 ** (attempt - 1), config.agent.max_retry_backoff_seconds)


def is_retryable_error(error: str | None) -> bool:
    if not error:
        return True
    lowered = error.lower()
    non_retryable = [
        "prompt rendering failed",
        "missing required labels",
        "status is not active",
        "workflow",
        "codex command not found",
        "workspace.source_repo",
        "source_repo",
        "jira token",
        "jira email",
    ]
    return not any(marker in lowered for marker in non_retryable)


def build_review_prompt(
    *,
    issue: Issue,
    workspace_path: Path,
    implementation_prompt: str,
    implementation_message: str | None,
    review_instructions: str,
) -> str:
    return f"""You are reviewing a completed Codex implementation for Jira issue {issue.identifier}.

Issue title: {issue.title}
Workspace: {workspace_path}

Review instructions:
{review_instructions}

Decision contract:
- Prefer JSON: {{"decision":"approve","findings":[],"residual_risk":"low"}}.
- Use decision `approve` if no further code changes are needed.
- Use decision `changes_required` if another implementation pass is needed.
- If you cannot emit JSON, start with `APPROVE` or `CHANGES_REQUIRED`, then explain concisely.

Implementation final message:
{implementation_message or "No implementation final message was produced."}

Original implementation prompt:
{implementation_prompt}

Review the current git diff in the workspace and produce the review decision."""


def build_regeneration_prompt(*, issue: Issue, original_prompt: str, review_message: str) -> str:
    return f"""{original_prompt}

The previous implementation was reviewed and needs another pass.

Review feedback:
{review_message}

Update the workspace to address the review feedback. Keep changes scoped to Jira issue {issue.identifier}.
After making changes, leave a concise final report with files changed, verification, and residual risk."""


def build_planning_prompt(*, issue: Issue, implementation_prompt: str, planning_instructions: str) -> str:
    return f"""You are preparing an implementation plan/spec for Jira issue {issue.identifier}.

Planning instructions:
{planning_instructions.strip()}

Important constraints:
- This is a planning pass only.
- Inspect the repository as needed.
- Do not edit files.
- If the requirements are unclear enough that implementation would be risky, clearly state the clarification needed.

Implementation prompt that will be used after planning:
{implementation_prompt}

Write the plan/spec now."""


def build_implementation_prompt_with_plan(*, implementation_prompt: str, plan_message: str | None) -> str:
    return f"""{implementation_prompt}

Codex planning/spec pass:
{plan_message or "No plan was produced."}

Use the plan/spec above as implementation guidance. If the plan identifies open questions that make implementation unsafe, stop and ask for clarification. Otherwise implement the scoped change, run verification, and leave the final report."""


def build_approved_plan_implementation_prompt(
    *,
    issue: Issue,
    original_prompt: str,
    previous_run: RunRecord,
    human_input: dict[str, Any],
    plan_message: str | None,
) -> str:
    response = human_input.get("response") or ""
    return f"""{original_prompt}

This run is resuming Jira issue {issue.identifier} after human plan approval.

Approved plan/spec from the previous planning pass:
{plan_message or previous_run.final_message or "No plan was found."}

Human confirmation or adjustments:
{response}

Implement according to the approved plan and human confirmation. If the confirmation asks for plan adjustments, apply those adjustments. Keep changes scoped, run verification, and leave a concise final report with files changed, verification, and residual risk."""


def read_plan_message_for_run(run: RunRecord, output_plan_file: str) -> str | None:
    path = Path(run.workspace_path) / output_plan_file
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def build_human_resume_prompt(
    *,
    issue: Issue,
    original_prompt: str,
    previous_run: RunRecord | None,
    human_input: dict[str, Any],
) -> str:
    previous_error = previous_run.error if previous_run else None
    previous_final = previous_run.final_message if previous_run else None
    previous_workspace = previous_run.workspace_path if previous_run else "current workspace"
    previous_phase = previous_run.blocked_phase if previous_run else None
    question = human_input.get("question") or previous_error or "Codex requested human clarification."
    response = human_input.get("response") or ""
    return f"""{original_prompt}

This is a resumed run for Jira issue {issue.identifier}. A previous Codex attempt was blocked and a human has provided clarification.

Previous workspace:
{previous_workspace}

Previous blocked phase:
{previous_phase or "unknown"}

Previous blocked reason or question:
{question}

Previous final message:
{previous_final or "No final message was produced before blocking."}

Human clarification:
{response}

Continue from the existing workspace. Preserve useful existing changes, revise anything that conflicts with the clarification, run the configured verification, and leave a concise final report with files changed, verification, and residual risk."""


def classify_review_decision(review_message: str | None) -> str:
    if not review_message:
        return "approve"
    structured = parse_review_json(review_message)
    if structured:
        decision = str(structured.get("decision") or structured.get("status") or "").strip().lower()
        if decision in {"changes_required", "changes required", "request_changes", "needs_changes"}:
            return "changes_required"
        if decision in {"approve", "approved", "ok", "pass"}:
            return "approve"
    normalized = review_message.strip().lower()
    first_line = normalized.splitlines()[0] if normalized else ""
    if first_line.startswith("changes_required") or first_line.startswith("changes required"):
        return "changes_required"
    if first_line.startswith("approve") or first_line.startswith("approved"):
        return "approve"

    change_markers = [
        "changes_required",
        "changes required",
        "needs another pass",
        "must fix",
        "should fix before",
    ]
    if any(marker in normalized for marker in change_markers):
        return "changes_required"
    return "approve"


def parse_review_json(review_message: str) -> dict[str, Any] | None:
    text = review_message.strip()
    candidates = [text]
    if "```" in text:
        parts = text.split("```")
        for index, part in enumerate(parts):
            stripped = part.strip()
            if not stripped:
                continue
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            candidates.append(stripped)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def write_review_files(workspace_path: Path, config, review_message: str, review_history: list[str]) -> None:
    review_path = workspace_path / config.output_review_file
    history_path = workspace_path / config.output_review_history_file
    review_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(review_message, encoding="utf-8")
    history_path.write_text("\n\n---\n\n".join(review_history), encoding="utf-8")


def append_review_to_final(final_message: str | None, review_message: str) -> str:
    base = final_message or "No implementation final message was produced."
    return f"{base}\n\nReview:\n{review_message}"


def start_comment(issue: Issue, workspace_path: Path, branch_name: str | None) -> str:
    lines = [
        f"Codex run started for {issue.identifier}.",
        "",
        f"Workspace: `{workspace_path}`",
    ]
    if branch_name:
        lines.append(f"Branch: `{branch_name}`")
    return "\n".join(lines)


def finish_comment(issue: Issue, run: RunRecord) -> str:
    if run.status == "blocked" and run.blocked_phase == "planning_approval":
        return "\n".join(
            [
                f"Codex plan/spec is ready for {issue.identifier}.",
                "",
                "Status: waiting for human plan approval",
                f"Workspace: `{run.workspace_path}`",
                "",
                "Plan/spec:",
                run.final_message or "No plan text was produced.",
                "",
                "Next step:",
                "- Confirm the plan in the Symphony dashboard, or provide adjustments before implementation.",
            ]
        )

    if run.status == "blocked":
        return "\n".join(
            [
                f"Codex run is blocked for {issue.identifier}.",
                "",
                f"Phase: {run.blocked_phase or 'unknown'}",
                "",
                "Reason:",
                run.error or "Codex requires operator input.",
                "",
                f"Workspace: `{run.workspace_path}`",
            ]
        )

    if run.status == "completed":
        verification = run.verification_status or "not_configured"
        summary = run.final_message or "No final Codex message was produced."
        branch = f"Branch: `{run.branch_name}`\n" if run.branch_name else ""
        return "\n".join(
            [
                f"Codex run completed for {issue.identifier}.",
                "",
                "Status: completed",
                branch.rstrip(),
                f"Workspace: `{run.workspace_path}`",
                "",
                "Verification:",
                f"- `verify`: {verification}",
                "",
                "Summary:",
                summary,
                "",
                "Notes:",
                "- Review the branch before merging.",
            ]
        ).replace("\n\n\n", "\n\n")

    return "\n".join(
        [
            f"Codex run failed for {issue.identifier}.",
            "",
            f"Status: {run.status}",
            f"Workspace: `{run.workspace_path}`",
            "",
            "Error:",
            run.error or "Unknown error",
            "",
            "Logs are available in the local Symphony SQLite store.",
        ]
    )
