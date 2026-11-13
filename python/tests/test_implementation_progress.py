import asyncio
import json
from pathlib import Path
import tempfile

import pytest

from symphony_jira.orchestrator import (
    SingleIssueOrchestrator, PollingOrchestrator, implementation_is_incomplete, parse_human_request,
)
from test_test_selection import SelectionRunner, HostVerificationManager
from test_orchestrator import create_verification_case, approve_development_run, dispatch_pending_human_resume


class ProgressRunner(SelectionRunner):
    def __init__(self, *, finish=True, change_each_time=False):
        super().__init__()
        self.implementation_calls = 0
        self.finish = finish
        self.change_each_time = change_each_time

    async def run(self, prompt, workspace_path, config, **kwargs):
        result = await super().run(prompt, workspace_path, config, **kwargs)
        if self.phases[-1] == 'implementation':
            self.implementation_calls += 1
            if self.change_each_time:
                (workspace_path / 'foyr2' / 'changed.py').write_text(str(self.implementation_calls))
            complete = self.finish and self.implementation_calls >= 2
            result.final_message = json.dumps({
                'implementation_status': 'complete' if complete else 'continue',
                'summary': 'Implemented the current slice',
                'remaining_work': [] if complete else ['Wire the page to the API'],
            })
        return result


def run_progress(root, runner, limit=6, review_limit=1):
    async def run():
        original, _, _, jira = create_verification_case(root, verification_status='passed',
                                                       required=False, workspace_subdir='foyr2')
        workflow = original.workflow
        workflow.config.codex.require_plan_approval = True
        workflow.config.codex.review_after_run = True
        workflow.config.codex.select_tests_after_implementation = True
        workflow.config.codex.max_implementation_continuations = limit
        workflow.config.codex.max_review_iterations = review_limit
        workflow.config.hooks.use_development_verification_request = True
        manager = HostVerificationManager(workflow.config.workspace, workflow.config.hooks)
        planned = await SingleIssueOrchestrator(workflow, jira, original.store,
            codex_runner=runner, workspace_manager=manager).run_once('T-1')
        approve_development_run(original.store, planned.run)
        polling = PollingOrchestrator(workflow, jira, original.store,
            codex_runner=runner, workspace_manager=manager)
        await dispatch_pending_human_resume(polling)
        return original.store.latest_run_for_issue('T-1'), jira
    return asyncio.run(run())


def test_incomplete_work_continues_before_tests_or_review(tmp_path):
    runner = ProgressRunner()
    result, jira = run_progress(tmp_path, runner)
    assert result.status == 'completed', result.error
    assert runner.phases == ['planning', 'implementation', 'implementation', 'test_selection', 'review']
    assert jira.transitions == ['Done']
    assert 'Continue the unfinished implementation' in runner.implementation_prompt


def test_no_progress_stops_without_review_or_handoff(tmp_path):
    runner = ProgressRunner(finish=False)
    result, jira = run_progress(tmp_path, runner)
    assert result.status == 'blocked', result.error
    assert result.blocked_phase == 'implementation'
    assert 'two passes without code progress' in result.error
    assert runner.implementation_calls == 3
    assert 'review' not in runner.phases
    assert 'test_selection' not in runner.phases
    assert not jira.transitions


def test_continuation_limit_stops_even_with_progress(tmp_path):
    runner = ProgressRunner(finish=False, change_each_time=True)
    result, jira = run_progress(tmp_path, runner, limit=1)
    assert result.status == 'blocked', result.error
    assert 'continuation limit' in result.error
    assert runner.implementation_calls == 2
    assert 'review' not in runner.phases
    assert not jira.transitions


@pytest.mark.parametrize('message', [
    'Implementation is incomplete; no files changed.',
    'Partial implementation only; P1 remains unresolved.',
    '{"implementation_status":"continue","remaining_work":["API"]}',
    '{"implementation_status":"complete","remaining_work":["API"]}',
])
def test_partial_reports_are_not_completion(message):
    assert implementation_is_incomplete(message)


def test_missing_runtime_evidence_is_not_incomplete_code():
    assert not implementation_is_incomplete('Implemented. Tests not run; release readiness is unverified.')


def test_all_planning_questions_reach_the_operator():
    result = parse_human_request(json.dumps({
        'decision': 'needs_human', 'question': 'Summary',
        'questions': ['Which permission behavior?', 'Should absent contract dates be blank?'],
    }))
    assert result == '1. Which permission behavior?\n\n2. Should absent contract dates be blank?'


class CorrectionRunner(SelectionRunner):
    def __init__(self, *, makes_progress=False):
        super().__init__()
        self.makes_progress = makes_progress
        self.implementations = 0
        self.reviews = 0
        self.correction_prompts = []

    async def run(self, prompt, workspace_path, config, **kwargs):
        result = await super().run(prompt, workspace_path, config, **kwargs)
        if self.phases[-1] == 'implementation':
            self.implementations += 1
            if self.implementations > 1:
                self.correction_prompts.append(prompt)
            if self.makes_progress:
                (workspace_path / 'foyr2/changed.py').write_text(f'correction {self.implementations}\n')
            result.final_message = f'Implementation report {self.implementations}'
        elif self.phases[-1] == 'review':
            self.reviews += 1
            result.final_message = json.dumps({
                'decision': 'approve' if self.makes_progress and self.reviews == 3 else 'changes_required',
                'findings': ['Correct the integer limit'],
            })
        return result


def test_no_op_review_corrections_stop_before_iteration_budget_is_wasted():
    with tempfile.TemporaryDirectory() as tmp:
        runner = CorrectionRunner()
        result, jira = run_progress(Path(tmp), runner, review_limit=10)
        assert result.status == 'blocked', result.error
        assert 'no code progress for 2 passes' in result.error
        assert runner.implementations == 3
        assert runner.reviews == 3
        assert not jira.transitions


def test_progressing_corrections_receive_evidence_and_are_reverified():
    with tempfile.TemporaryDirectory() as tmp:
        runner = CorrectionRunner(makes_progress=True)
        result, jira = run_progress(Path(tmp), runner, review_limit=5)
        assert result.status == 'completed', result.error
        assert runner.implementations == runner.reviews == 3
        assert runner.phases.count('test_selection') == 3
        assert 'ASSERTION_FAILURE: limit must be an integer' in runner.correction_prompts[0]
        assert 'Implementation report 1' in runner.correction_prompts[0]
        assert 'Implementation report 2' in runner.correction_prompts[1]
        assert jira.transitions == ['Done']


def test_review_budget_does_not_allow_an_unreviewable_correction():
    with tempfile.TemporaryDirectory() as tmp:
        runner = CorrectionRunner(makes_progress=True)
        result, jira = run_progress(Path(tmp), runner, review_limit=1)
        assert result.status == 'blocked'
        assert 'maximum review iterations' in result.error
        assert runner.implementations == runner.reviews == 1
        assert not jira.transitions
