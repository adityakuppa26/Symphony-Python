from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from symphony_jira.codex_runner import CodexRunner
from symphony_jira.config import CodexConfig
from symphony_jira.models import Issue, utc_now, issue_description_fingerprint
from symphony_jira.orchestrator import PollingOrchestrator, RunningIssue
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow
from test_orchestrator import FakeJira, write_fake_codex, write_workflow


@pytest.fixture
def local_root():
    with tempfile.TemporaryDirectory(prefix="symphony-reliability-") as directory:
        yield Path(directory)


def scheduler(root):
    workflow = load_workflow(write_workflow(root, write_fake_codex(root)),
                             environ={"TEST_JIRA_TOKEN": "token"})
    issue = Issue(id="1", identifier="T-1", title="Implement", description="Original scope",
                  status="To Do", labels=["codex-ready"], url="https://example.test/T-1")
    return PollingOrchestrator(workflow, FakeJira(issue), Store(root / "state.sqlite3")), issue


def failed_run(polling, issue, *, attempt=1, error="Temporary connection failure", age=0):
    run = polling.store.create_run(issue, polling.config.workspace.root / issue.identifier,
                                  attempt=attempt, branch_name=None)
    return polling.store.update_run(run.id, status="failed", error=error,
                                    finished_at=utc_now() - timedelta(seconds=age))


def test_exhausted_retry_budget_survives_polling_and_restart(local_root):
    polling, issue = scheduler(local_root)
    polling.config.agent.max_retries = 1
    failed_run(polling, issue, attempt=2)
    for _ in range(2):
        polling = PollingOrchestrator(polling.workflow, polling.jira, Store(polling.store.db_path))
        with patch.object(polling, "_start_issue") as start:
            polling.dispatch_fresh_issues([issue])
            assert not start.called
            assert not polling.retry_queue


def test_restart_restores_attempt_and_remaining_backoff(local_root):
    polling, issue = scheduler(local_root)
    polling.config.agent.max_retries = 5
    failed_run(polling, issue, attempt=4, age=3)
    with patch.object(polling, "_start_issue") as start:
        polling.dispatch_fresh_issues([issue])
        entry = polling.retry_queue[issue.id]
        assert entry.attempt == 5
        assert 3 < entry.seconds_until_due() <= 5
        assert not start.called
        entry.due_at = 0
        asyncio.run(polling.dispatch_due_retries())
        start.assert_called_once()
        assert start.call_args.args[1] == 5


def test_changed_requirements_start_a_new_retry_budget(local_root):
    polling, issue = scheduler(local_root)
    failed_run(polling, issue, attempt=20)
    changed = issue.model_copy(update={"description": "Revised scope"})
    with patch.object(polling, "_start_issue") as start:
        polling.dispatch_fresh_issues([changed])
        start.assert_called_once_with(changed, attempt=1)


def test_failed_human_action_is_not_replayed_as_fresh_work(local_root):
    polling, issue = scheduler(local_root)
    failed_run(polling, issue)
    with patch.object(polling.store, "get_human_resume_handoff", return_value={"id": "owned"}), \
            patch.object(polling, "_start_issue") as start:
        polling.dispatch_fresh_issues([issue])
        assert not start.called
        assert not polling.retry_queue


@pytest.mark.parametrize('error', ['Temporary connection failure', 'workflow configuration is invalid'])
def test_failure_before_run_creation_retains_budget_and_redacts_secrets(local_root, error):
    async def run():
        polling, issue = scheduler(local_root)
        polling.secret_values = ['PRIVATE_TEST_TOKEN']
        polling.config.agent.max_retries = 0
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        running = RunningIssue(issue_id=issue.id, identifier=issue.identifier,
            attempt=1, task=task, started_at=utc_now(),
            requirements_fingerprint=issue_description_fingerprint(issue))
        await polling._schedule_retry_or_release(running, error + ' PRIVATE_TEST_TOKEN')
        restarted = PollingOrchestrator(polling.workflow, polling.jira, Store(polling.store.db_path))
        with patch.object(restarted, '_start_issue') as start:
            restarted.dispatch_fresh_issues([issue])
            assert not start.called
            assert not restarted.retry_queue
        assert 'PRIVATE_TEST_TOKEN' not in restarted.store.get_dispatch_failure(issue.identifier)['error']
    asyncio.run(run())


def test_existing_active_run_is_not_dispatched_twice(local_root):
    polling, issue = scheduler(local_root)
    polling.store.create_run(issue, local_root / 'workspace', branch_name=None, status='running')
    with patch.object(polling, '_start_issue') as start:
        polling.dispatch_fresh_issues([issue])
        assert not start.called


def test_reconciliation_runs_during_search_outage(local_root):
    async def run():
        polling, issue = scheduler(local_root)
        polling.jira.issue = issue.model_copy(update={"status": "Done"})
        polling.jira.issues = [polling.jira.issue]
        polling.jira.search_issues = AsyncMock(side_effect=RuntimeError("Search unavailable"))
        worker = asyncio.create_task(asyncio.sleep(60))
        polling.running[issue.id] = RunningIssue(issue_id=issue.id, identifier=issue.identifier,
                                                attempt=1, task=worker, started_at=utc_now())
        try:
            await polling.poll_once()
            await asyncio.sleep(0)
            assert worker.cancelled()
            assert polling.last_poll_error == "Search unavailable"
        finally:
            await polling.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("cancel_loop", [False, True])
def test_shutdown_waits_for_worker_cleanup(local_root, cancel_loop):
    async def run():
        polling, issue = scheduler(local_root)
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def work():
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        worker = asyncio.create_task(work())
        await started.wait()
        polling.running[issue.id] = RunningIssue(issue_id=issue.id, identifier=issue.identifier,
                                                attempt=1, task=worker, started_at=utc_now())
        polling.claimed.add(issue.id)
        polling.poll_once = AsyncMock()
        loop = asyncio.create_task(polling.run_forever())
        await asyncio.sleep(0)
        if cancel_loop:
            loop.cancel()
        else:
            polling.stop()
        await asyncio.gather(loop, return_exceptions=True)
        assert cleaned.is_set()
        assert worker.cancelled()
        assert not polling.running
        assert not polling.claimed
    asyncio.run(run())


def run_child(root, script, *, stall=1, timeout=5):
    config = CodexConfig(command=sys.executable, args=["-c", script], stall_timeout_seconds=stall)
    return asyncio.run(CodexRunner().run("prompt", root, config, timeout_seconds=timeout))


def test_silent_child_is_stopped_before_overall_deadline(local_root):
    result = run_child(local_root, "import sys,time; sys.stdin.read(); time.sleep(60)")
    assert result.status == "failed"
    assert result.error == "Codex stalled: no output for 1 seconds"
    assert result.returncode is not None


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_activity_extends_stall_deadline_without_extending_overall_budget(local_root, stream):
    script = (
        "import sys,time,json\nsys.stdin.read()\n"
        f"for _ in range(8):\n print('active', file=sys.{stream}, flush=True)\n time.sleep(0.2)\n"
        "print(json.dumps({'type':'turn.completed','final_message':'Done'}), flush=True)\n"
    )
    result = run_child(local_root, script)
    assert result.status == "completed", result.error
    assert result.final_message == "Done"
    if stream == "stderr":
        assert result.stderr_path.read_text().count("active") == 8


def test_stall_watchdog_can_be_disabled(local_root):
    result = run_child(local_root,
        "import sys,time; sys.stdin.read(); time.sleep(1.1); print('done')", stall=0)
    assert result.status == "completed"


def test_stall_preserves_partial_stderr_for_diagnosis(local_root):
    result = run_child(local_root,
        "import sys,time; sys.stdin.read(); print('Last tool diagnostic',file=sys.stderr,flush=True); time.sleep(60)")
    assert result.status == 'failed'
    assert 'stalled' in result.error
    assert result.stderr_path.read_text() == 'Last tool diagnostic\n'


def test_shutdown_finalizes_the_actual_run_record(local_root):
    async def run():
        polling, _ = scheduler(local_root)
        started = asyncio.Event()

        class WaitingRunner:
            async def run(self, *args, **kwargs):
                started.set()
                await asyncio.Event().wait()

        polling.codex_runner = WaitingRunner()
        loop = asyncio.create_task(polling.run_forever())
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            polling.stop()
            await asyncio.wait_for(loop, timeout=10)
            latest = polling.store.latest_run_for_issue('T-1')
            assert latest.status == 'cancelled'
            assert latest.finished_at is not None
            assert not polling.running
        finally:
            if not loop.done():
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)
    asyncio.run(run())


def test_overall_timeout_still_applies_to_active_child(local_root):
    result = run_child(local_root,
        "import sys,time\nsys.stdin.read()\nwhile True:\n print('active',flush=True)\n time.sleep(0.05)",
        timeout=1)
    assert result.status == "failed"
    assert result.error == "Codex timed out after 1 seconds"


@pytest.mark.parametrize("changes", [
    {"stall_timeout_seconds": -1}, {"max_review_no_progress_passes": 0},
    {"max_review_no_progress_passes": 11},
])
def test_invalid_execution_limits_are_rejected(changes):
    with pytest.raises(ValueError):
        CodexConfig(**changes)
