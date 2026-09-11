from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from starlette.testclient import TestClient

from symphony_jira.dashboard import create_app, current_plan_spec_hash, build_state, render_dashboard_html
from symphony_jira.models import issue_description_fingerprint
from symphony_jira.orchestrator import PollingOrchestrator, SingleIssueOrchestrator, validate_bound_plan_approval
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow
from test_orchestrator import (
    FakeJira, approve_development_run, codex_result, commit_test_git_repository,
    dispatch_pending_human_resume, ensure_test_git_repository, hydrated_test_issue,
    completed_review_issue, valid_plan_spec_message, write_fake_codex, write_workflow,
)


class ReplanningRunner:
    def __init__(self):
        self.phases = []
        self.planning_prompts = []
        self.implementation_count = 0
        self.mutate_replan = False

    async def run(self, prompt, workspace_path, config, **kwargs):
        workspace = Path(workspace_path)
        if prompt.startswith('You are triaging pasted human code-review feedback'):
            self.phases.append('triage')
            return codex_result(workspace, 'completed', final_message=json.dumps({
                'decision': 'plan_changes_required', 'reason': 'Human feedback changes the approved scope.',
            }), final_path=config.output_last_message_file)
        if 'planning pass only' in prompt.lower():
            self.phases.append('plan')
            self.planning_prompts.append(prompt)
            if self.mutate_replan and 'Retained implementation for replanning:' in prompt:
                (workspace / 'repo' / 'new_test.py').write_text('unauthorized planning edit\n')
            plan = valid_plan_spec_message(
                prompt, 'Reconcile retained edits.' if self.implementation_count else 'Implement scoped behavior.',
                baseline_sha=ensure_test_git_repository(workspace),
            )
            return codex_result(workspace, 'completed', final_message=plan,
                                final_path=config.output_last_message_file)
        if prompt.startswith('You are reviewing a completed implementation'):
            self.phases.append('review')
            decision = 'plan_changes_required' if self.implementation_count == 1 else 'approve'
            return codex_result(workspace, 'completed', final_message=json.dumps({
                'decision': decision, 'findings': ['Revise the affected surfaces in the plan.'],
            }), final_path=config.output_last_message_file)
        self.phases.append('implementation')
        self.implementation_count += 1
        (workspace / 'repo' / 'module.py').write_text('retained implementation\n')
        (workspace / 'repo' / 'new_test.py').write_text('retained test\n')
        return codex_result(workspace, 'completed', final_message='Implemented scoped behavior.',
                            final_path=config.output_last_message_file)


class ReplanningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        path = write_workflow(self.root, write_fake_codex(self.root), codex_extra='''
  plan_before_implementation: true
  require_plan_approval: true
  review_after_run: true
  max_review_iterations: 3
''')
        self.workflow = load_workflow(path, environ={'TEST_JIRA_TOKEN': 'token'})
        self.workflow.config.workspace.managed_repositories = [Path('repo'), Path('sibling')]
        self.issue = hydrated_test_issue(completed_review_issue()).model_copy(update={'status': 'To Do'})
        self.jira = FakeJira(self.issue)
        self.store = Store(self.root / 'db.sqlite3')
        self.workspace = self.workflow.config.workspace.root / 'T-1'
        self.repository = self.workspace / 'repo'
        ensure_test_git_repository(self.workspace)
        ensure_test_git_repository(self.workspace, repository='sibling')
        (self.repository / 'module.py').write_text('baseline\n')
        subprocess.run(['git', '-C', str(self.repository), 'add', 'module.py'], check=True)
        self.head = commit_test_git_repository(self.repository, 'tracked baseline')
        self.runner = ReplanningRunner()

    def polling(self, store=None):
        return PollingOrchestrator(self.workflow, self.jira, store or self.store,
                                   codex_runner=self.runner)

    async def implementation_sent_to_replanning(self):
        initial = await SingleIssueOrchestrator(
            self.workflow, self.jira, self.store, codex_runner=self.runner,
        ).run_once('T-1')
        self.assertEqual(initial.run.blocked_phase, 'planning_approval', initial.run.error)
        self.assertIsNone(initial.run.planning_baseline)
        _, approval = approve_development_run(self.store, initial.run)
        await dispatch_pending_human_resume(self.polling())
        source = self.store.latest_run_for_issue('T-1')
        self.assertEqual(source.blocked_phase, 'planning', source.error)
        self.assertEqual(self.runner.phases, ['plan', 'implementation', 'review'])
        self.assertIsNotNone(self.store.get_plan_approval(approval['id'])['invalidated_at'])
        return source

    async def replan(self, source):
        self.store.add_human_input('T-1', run_id=source.id, response='Revise the plan using the code review.')
        await dispatch_pending_human_resume(self.polling())
        return self.store.latest_run_for_issue('T-1')

    async def test_initial_dirty_worktree_still_blocks(self):
        (self.repository / 'module.py').write_text('unapproved dirt\n')
        result = await SingleIssueOrchestrator(
            self.workflow, self.jira, self.store, codex_runner=self.runner,
        ).run_once('T-1')
        self.assertEqual(result.run.blocked_phase, 'planning')
        self.assertIn('not clean', result.run.error)
        self.assertIsNone(result.run.planning_baseline)
        self.assertEqual(self.runner.phases, ['plan'])

    async def test_replan_preserves_changes_approves_exact_diff_and_resumes_after_restart(self):
        source = await self.implementation_sent_to_replanning()
        subprocess.run(['git', '-C', str(self.repository), 'add', 'new_test.py'], check=True)
        status_before = subprocess.check_output(['git', '-C', str(self.repository), 'status', '--porcelain'])
        planned = await self.replan(source)
        self.assertEqual(planned.blocked_phase, 'planning_approval', planned.error)
        baseline = planned.planning_baseline
        self.assertEqual(baseline.source_run_id, source.id)
        self.assertEqual(baseline.repositories, ['repo', 'sibling'])
        self.assertEqual((self.repository / 'module.py').read_text(), 'retained implementation\n')
        self.assertEqual((self.repository / 'new_test.py').read_text(), 'retained test\n')
        self.assertEqual(subprocess.check_output(
            ['git', '-C', str(self.repository), 'status', '--porcelain'],
        ), status_before)
        self.assertIn('new_test.py', baseline.workspace_diff)
        self.assertIn('Revise the affected surfaces', self.runner.planning_prompts[-1])
        self.assertIn('Reconcile', planned.final_message)
        html = render_dashboard_html(build_state(self.workflow, self.store))
        self.assertIn('Review retained implementation diff', html)
        self.assertIn(baseline.workspace_diff_hash, html)
        response = TestClient(create_app(self.workflow, self.store)).post(
            f'/api/v1/runs/{planned.id}/human-input',
            json={'action': 'approve', 'approver_identity': 'reviewer@example.test'},
        )
        self.assertEqual(response.status_code, 200, response.text)
        approval = self.store.latest_plan_approval_for_run(planned.id)
        self.assertEqual(approval['workspace_diff_hash'], baseline.workspace_diff_hash)
        restarted = Store(self.root / 'db.sqlite3')
        self.assertEqual(restarted.get_run(planned.id).planning_baseline, baseline)
        await dispatch_pending_human_resume(self.polling(restarted))
        result = restarted.latest_run_for_issue('T-1')
        self.assertEqual(result.status, 'completed', result.error)
        self.assertEqual(self.runner.phases, ['plan', 'implementation', 'review', 'plan', 'implementation', 'review'])
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.repository), 'rev-parse', 'HEAD'], text=True).strip(), self.head)

    async def test_failed_clean_tree_replan_can_recover_approved_execution_history(self):
        source = await self.implementation_sent_to_replanning()
        failed = self.store.create_run(
            self.issue, self.workspace, branch_name=None,
            plan_spec_hash=source.plan_spec_hash, plan_approval_id=source.plan_approval_id,
        )
        failed = self.store.update_run(failed.id, status='blocked', blocked_phase='planning',
                                       error='Replan from a clean worktree.')
        (self.workspace / self.workflow.config.codex.output_plan_file).write_text('{"invalid":true}')
        planned = await self.replan(failed)
        self.assertEqual(planned.blocked_phase, 'planning_approval', planned.error)
        self.assertEqual(planned.planning_baseline.source_run_id, source.id)

    async def test_drift_in_tracked_untracked_or_managed_sibling_blocks_dashboard_approval(self):
        source = await self.implementation_sent_to_replanning()
        planned = await self.replan(source)
        files = [self.repository / 'module.py', self.repository / 'new_test.py',
                 self.workspace / 'sibling' / 'unexpected.py']
        for path in files:
            with self.subTest(path=path.name):
                previous = path.read_bytes() if path.exists() else None
                path.write_text('modified after planning\n')
                response = TestClient(create_app(self.workflow, self.store)).post(
                    f'/api/v1/runs/{planned.id}/human-input',
                    json={'action': 'approve', 'approver_identity': 'reviewer@example.test'},
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn('Retained implementation changed', response.text)
                self.assertEqual(self.store.list_plan_approvals(planned.id), [])
                if previous is None:
                    path.unlink()
                else:
                    path.write_bytes(previous)
        self.assertEqual(current_plan_spec_hash(planned, self.workflow, self.store), planned.plan_spec_hash)

    async def test_planning_cannot_modify_retained_implementation(self):
        source = await self.implementation_sent_to_replanning()
        self.runner.mutate_replan = True
        result = await self.replan(source)
        self.assertEqual(result.blocked_phase, 'planning')
        self.assertIn('Retained implementation changed', result.error)
        self.assertEqual(self.runner.implementation_count, 1)
        self.assertIsNone(result.planning_baseline)

    async def test_drift_after_approval_invalidates_it(self):
        source = await self.implementation_sent_to_replanning()
        planned = await self.replan(source)
        human_input, approval = approve_development_run(self.store, planned)
        human_input.update(approval_id=approval['id'], approved_at=approval['approved_at'],
                           plan_spec_hash=approval['plan_spec_hash'],
                           requirements_snapshot_hash=approval['requirements_snapshot_hash'])
        (self.repository / 'new_test.py').write_text('changed after approval\n')
        error = validate_bound_plan_approval(
            issue=self.issue, previous_run=planned, human_input=human_input,
            output_plan_file=self.workflow.config.codex.output_plan_file,
            requirements_snapshot_hash=issue_description_fingerprint(self.issue), store=self.store,
        )
        self.assertIn('Retained implementation changed', error)
        self.assertIsNotNone(self.store.get_plan_approval(approval['id'])['invalidated_at'])

    async def test_before_run_mutation_does_not_reuse_approved_diff(self):
        source = await self.implementation_sent_to_replanning()
        planned = await self.replan(source)
        approve_development_run(self.store, planned)
        self.workflow.config.hooks.before_run = "printf 'changed by hook\\n' >> repo/new_test.py"
        await dispatch_pending_human_resume(self.polling())
        result = self.store.latest_run_for_issue('T-1')
        self.assertEqual(result.blocked_phase, 'planning')
        self.assertIn('Retained implementation changed', result.error)
        self.assertEqual(self.runner.implementation_count, 1)

    async def test_human_scope_feedback_after_handoff_is_retained_in_replanning(self):
        source = await self.implementation_sent_to_replanning()
        planned = await self.replan(source)
        approve_development_run(self.store, planned)
        await dispatch_pending_human_resume(self.polling())
        completed = self.store.latest_run_for_issue('T-1')
        self.assertEqual(completed.status, 'completed', completed.error)
        client = TestClient(create_app(self.workflow, self.store))
        response = client.post(f'/api/v1/runs/{completed.id}/human-review', json={
            'reviewer_identity': 'reviewer@example.test',
            'source_url': 'https://example.test/reviews/1',
            'comments': 'Please revise the plan to cover the saved-filter compatibility requirement.',
        })
        self.assertEqual(response.status_code, 200, response.text)
        await dispatch_pending_human_resume(self.polling())
        blocked = self.store.latest_run_for_issue('T-1')
        self.assertEqual(blocked.blocked_phase, 'planning', blocked.error)
        revised = await self.replan(blocked)
        self.assertEqual(revised.blocked_phase, 'planning_approval', revised.error)
        self.assertEqual(revised.planning_baseline.source_run_id, blocked.id)
        self.assertIn('saved-filter compatibility requirement', self.runner.planning_prompts[-1])
        self.assertEqual((self.repository / 'module.py').read_text(), 'retained implementation\n')
