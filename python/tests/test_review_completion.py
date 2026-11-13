"""Executable acceptance scenarios for handoff evidence and scope boundaries."""
import asyncio
import json
from pathlib import Path
import tempfile

import pytest

from symphony_jira.human_review import validate_feedback_items, validate_comment_resolutions
from symphony_jira.orchestrator import PollingOrchestrator
from symphony_jira.review_page import review_progress
from symphony_jira.store import Store
from test_orchestrator import (
    CompletedReviewCodexRunner, FakeJira, completed_review_issue, create_completed_review_action,
    codex_result, dispatch_pending_human_resume, ensure_test_git_repository, load_completed_review_workflow,
)


ITEMS = [
    {'id': 'R1', 'comment': 'Rename the local variable', 'scope': 'in_scope', 'proposed_action': 'Use the established local name'},
    {'id': 'R2', 'comment': 'Add regression coverage', 'scope': 'in_scope', 'proposed_action': 'Extend the existing test module'},
]
RESOLUTIONS = [
    {'id': 'R1', 'status': 'already_satisfied', 'evidence': 'repo/README.md:1 — inspected existing naming'},
    {'id': 'R2', 'status': 'addressed', 'evidence': 'repo/review_regression_test.py:1 — focused regression added; execution not claimed'},
]


def triage(items=ITEMS):
    return json.dumps({'decision': 'code_changes', 'reason': 'In approved scope', 'feedback_items': items})


def approval(resolutions=RESOLUTIONS):
    return json.dumps({'decision': 'approve', 'findings': [], 'comment_resolutions': resolutions})


class CoverageRunner(CompletedReviewCodexRunner):
    def __init__(self, *, triages=None, reviews=None, outside=False, mutate_review=False):
        super().__init__('code_changes')
        self.triages = triages or [triage()]
        self.reviews = reviews or [approval()]
        self.outside = outside
        self.mutate_review = mutate_review

    async def run(self, prompt, workspace_path, config, **kwargs):
        result = await super().run(prompt, workspace_path, config, **kwargs)
        phase = self.phases[-1]
        if phase in {'triage', 'review'}:
            outputs = self.triages if phase == 'triage' else self.reviews
            result.final_message = outputs.pop(0) if len(outputs) > 1 else outputs[0]
        if phase == 'implementation' and self.outside:
            (workspace_path / 'other/unauthorized.txt').write_text('unapproved code\n')
        if phase == 'review' and self.mutate_review and self.phases.count('review') == 2:
            (workspace_path / 'repo/unapproved-review-edit.txt').write_text('read-only repair must not edit\n')
        return result


def evaluate_scenario(runner, *, second_repository=False):
    async def run(root):
        workflow = load_completed_review_workflow(root)
        workflow.config.codex.require_review_comment_coverage = True
        workflow.config.codex.max_review_iterations = 3
        if second_repository:
            workflow.config.workspace.managed_repositories = [Path('repo'), Path('other')]
            ensure_test_git_repository(root / 'workspaces/T-1', repository='other')
            (root / 'workspaces/T-1/other/user-notes.txt').write_text('Retain this pre-existing work\n')
        store = Store(root / 'state.sqlite3')
        issue = completed_review_issue()
        action, _, result, _ = create_completed_review_action(root, workflow, issue, store)
        polling = PollingOrchestrator(workflow, FakeJira(issue), store, codex_runner=runner)
        await dispatch_pending_human_resume(polling)
        persisted = Store(store.db_path)
        final = persisted.get_run(result.id)
        recorded = persisted.get_human_review_action(action['id'])
        if second_repository:
            assert (root / 'workspaces/T-1/other/user-notes.txt').read_text() == 'Retain this pre-existing work\n'
            if runner.outside:
                assert (root / 'workspaces/T-1/other/unauthorized.txt').exists()
        return final, recorded
    with tempfile.TemporaryDirectory(prefix='symphony-completion-test-') as folder:
        return asyncio.run(run(Path(folder)))


def test_complete_resolution_evidence_survives_restart_and_reaches_progress_page():
    runner = CoverageRunner()
    run, action = evaluate_scenario(runner)
    assert run.status == 'completed', run.error
    assert runner.phases == ['triage', 'implementation', 'review']
    assert action['resolution']['items'] == RESOLUTIONS
    assert len(action['resolution']['workspace_diff_hash']) == 64
    progress = review_progress(action, run.model_dump())
    assert progress['comment_coverage'] == 'recorded'
    assert [item['resolution'] for item in progress['items']] == ['already_satisfied', 'addressed']


def test_missing_evidence_is_repaired_read_only_without_reimplementing():
    runner = CoverageRunner(reviews=['{"decision":"approve"}', approval()])
    run, action = evaluate_scenario(runner)
    assert run.status == 'completed', run.error
    assert runner.phases == ['triage', 'implementation', 'review', 'review']
    assert runner.args_by_phase['review'][-2:] == ['--sandbox', 'read-only']
    assert action['resolution']['items'] == RESOLUTIONS


def test_missing_evidence_cannot_silently_handoff_after_repair_budget():
    runner = CoverageRunner(reviews=['{"decision":"approve"}'])
    run, action = evaluate_scenario(runner)
    assert run.status == 'blocked'
    assert 'evidence remains invalid after repair' in run.error
    assert runner.phases.count('review') == 2
    assert action['resolution'] is None


@pytest.mark.parametrize('legacy_run', [False, True])
@pytest.mark.parametrize('resume_phase', ['review', 'implementation'])
def test_comment_coverage_survives_human_resumes_and_restarts(legacy_run, resume_phase):
    class ContinuationRunner(CoverageRunner):
        def __init__(self, reviews):
            super().__init__(reviews=reviews)
            self.prompts = []

        async def run(self, prompt, workspace_path, config, **kwargs):
            self.prompts.append(prompt)
            if prompt.startswith('You are reviewing a completed implementation'):
                return await super().run(prompt, workspace_path, config, **kwargs)
            self.phases.append('implementation')
            return codex_result(workspace_path, 'completed',
                final_message='Checked every retained review item.',
                final_path=config.output_last_message_file)

    async def run(root):
        workflow = load_completed_review_workflow(root)
        workflow.config.codex.require_review_comment_coverage = True
        issue = completed_review_issue().model_copy(update={'status': 'To Do'})
        store = Store(root / 'state.sqlite3')
        action, _, result, _ = create_completed_review_action(root, workflow, issue, store)
        runner = CoverageRunner(reviews=['{"decision":"approve"}'])
        await dispatch_pending_human_resume(
            PollingOrchestrator(workflow, FakeJira(issue), store, codex_runner=runner))
        blocked = store.get_run(result.id)
        assert blocked.status == 'blocked', blocked.error
        assert validate_feedback_items(blocked.review_comment_checklist) == ITEMS
        store.update_run(blocked.id, blocked_phase=resume_phase)
        if legacy_run:
            store.update_run(blocked.id, review_comment_checklist=None)
        else:
            # A configuration reload must not drop a run's existing obligation.
            workflow.config.codex.require_review_comment_coverage = False
        workflow.config.codex.review_after_run = False
        workflow.config.codex.max_review_iterations = 0

        # Retry twice, then supply valid evidence through the read-only repair.
        for attempt in range(3):
            store.add_human_input(issue.identifier, run_id=blocked.id, response='Retry the review.')
            store = Store(store.db_path)
            runner = ContinuationRunner(
                ['{"decision":"approve"}', approval()] if attempt == 2 else ['{"decision":"approve"}'])
            await dispatch_pending_human_resume(
                PollingOrchestrator(workflow, FakeJira(issue), store, codex_runner=runner))
            resumed = store.list_runs_for_issue(issue.identifier)[0]
            assert resumed.id != blocked.id
            expected_implementation = ['implementation'] if attempt == 0 and resume_phase == 'implementation' else []
            assert runner.phases == expected_implementation + ['review', 'review']
            assert validate_feedback_items(resumed.review_comment_checklist) == ITEMS
            for prompt in runner.prompts:
                assert 'Rename the local variable' in prompt
                assert 'Add regression coverage' in prompt
            assert 'Required comment IDs: ["R1", "R2"]' in runner.prompts[-1]
            if attempt < 2:
                assert resumed.status == 'blocked', resumed.error
                assert 'evidence remains invalid after repair' in resumed.error
                assert resumed.blocked_phase == 'review'
            else:
                assert resumed.status == 'completed', resumed.error
                assert 'repo/review_regression_test.py:1' in resumed.final_message
                assert runner.args_by_phase['review'][-2:] == ['--sandbox', 'read-only']
            blocked = resumed
            workflow.config.codex.require_review_comment_coverage = False

        # Continuations retain their evidence without rewriting the original failed review.
        assert store.get_human_review_action(action['id'])['resolution'] is None

    with tempfile.TemporaryDirectory(prefix='symphony-completion-test-') as folder:
        asyncio.run(run(Path(folder)))


def test_unresolved_comment_returns_to_implementation_and_review():
    unresolved = [RESOLUTIONS[0], {**RESOLUTIONS[1], 'status': 'unresolved'}]
    runner = CoverageRunner(reviews=[approval(unresolved), approval()])
    run, action = evaluate_scenario(runner)
    assert run.status == 'completed', run.error
    assert runner.phases == ['triage', 'implementation', 'review', 'implementation', 'review']
    assert all(item['status'] != 'unresolved' for item in action['resolution']['items'])


def test_malformed_triage_is_repaired_before_code_changes():
    runner = CoverageRunner(triages=['{"decision":"code_changes"}', triage()])
    run, _ = evaluate_scenario(runner)
    assert run.status == 'completed', run.error
    assert runner.phases[:3] == ['triage', 'triage', 'implementation']


def test_repeatedly_malformed_triage_stops_before_implementation():
    runner = CoverageRunner(triages=['{"decision":"code_changes"}'])
    run, action = evaluate_scenario(runner)
    assert run.status == 'blocked'
    assert 'checklist remains invalid after repair' in run.error
    assert runner.phases == ['triage', 'triage']
    assert action['resolution'] is None


def test_persistent_unresolved_feedback_cannot_finish_at_review_limit():
    runner = CoverageRunner(reviews=[approval([RESOLUTIONS[0], {**RESOLUTIONS[1], 'status': 'unresolved'}])])
    run, action = evaluate_scenario(runner)
    assert run.status == 'blocked'
    assert runner.phases.count('review') == 3
    assert runner.phases.count('implementation') == 3
    assert action['resolution'] is None


def test_resolution_evidence_is_fenced_to_the_live_owner():
    with tempfile.TemporaryDirectory(prefix='symphony-completion-test-') as folder:
        root = Path(folder)
        workflow = load_completed_review_workflow(root)
        store = Store(root / 'state.sqlite3')
        action, _, result, _ = create_completed_review_action(root, workflow, completed_review_issue(), store)
        claimed = store.claim_human_review_action(action['id'])
        store.start_human_review_run(action['id'], result.id, claimed['claim_token'])
        assert not store.record_owned_human_review_resolutions(action['id'], 'stale-owner',
            resolutions=RESOLUTIONS, workspace_diff_hash='a' * 64)
        assert store.get_human_review_action(action['id'])['resolution'] is None
        assert store.record_owned_human_review_resolutions(action['id'], claimed['claim_token'],
            resolutions=RESOLUTIONS, workspace_diff_hash='a' * 64)
        store.update_owned_human_review_run(action['id'], result.id, claimed['claim_token'], status='blocked')
        assert not store.record_owned_human_review_resolutions(action['id'], claimed['claim_token'],
            resolutions=[], workspace_diff_hash='b' * 64)
        progress = review_progress(store.get_human_review_action(action['id']), store.get_run(result.id).model_dump())
        assert progress['comment_coverage'] == 'not_recorded'


@pytest.mark.parametrize('scope,phase', [('plan_change', 'planning'), ('clarification', 'review')])
def test_mixed_scope_checklist_cannot_authorize_implementation(scope, phase):
    runner = CoverageRunner(triages=[triage([ITEMS[0], {**ITEMS[1], 'scope': scope}])])
    run, _ = evaluate_scenario(runner)
    assert run.status == 'blocked', run.error
    assert run.blocked_phase == phase
    assert runner.phases == ['triage']


def test_evidence_repair_cannot_modify_workspace():
    runner = CoverageRunner(reviews=['{"decision":"approve"}', approval()], mutate_review=True)
    run, action = evaluate_scenario(runner)
    assert run.status == 'blocked'
    assert 'Read-only review changed the workspace' in run.error
    assert action['resolution'] is None


def test_agent_changes_outside_approved_repositories_stop_handoff_and_preserve_work():
    runner = CoverageRunner(outside=True)
    run, action = evaluate_scenario(runner, second_repository=True)
    assert run.status == 'blocked'
    assert 'outside the approved PlanSpec: other' in run.error
    assert runner.phases == ['triage', 'implementation']
    assert action['resolution'] is None


def test_unchanged_preexisting_work_in_other_repository_is_allowed():
    run, _ = evaluate_scenario(CoverageRunner(), second_repository=True)
    assert run.status == 'completed', run.error


@pytest.mark.parametrize('resolutions', [
    [], [RESOLUTIONS[0]], [RESOLUTIONS[0], RESOLUTIONS[0]],
    [*RESOLUTIONS, {**RESOLUTIONS[0], 'id': 'R3'}],
    [RESOLUTIONS[0], {**RESOLUTIONS[1], 'evidence': '   '}],
    [RESOLUTIONS[0], {**RESOLUTIONS[1], 'status': 'done'}],
])
def test_invalid_comment_resolution_contracts_are_rejected(resolutions):
    with pytest.raises(ValueError):
        validate_comment_resolutions(approval(resolutions), ITEMS)


@pytest.mark.parametrize('items', [[], [ITEMS[0], ITEMS[0]], [{**ITEMS[0], 'comment': ' '}],
                                    [{**ITEMS[0], 'scope': 'whatever'}]])
def test_invalid_checklists_are_rejected(items):
    with pytest.raises(ValueError):
        validate_feedback_items(triage(items))
