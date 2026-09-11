from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from starlette.testclient import TestClient

from symphony_jira.dashboard import build_state, create_app, render_dashboard_html
from symphony_jira.human_input_context import build_human_input_context
from symphony_jira.models import utc_now
from symphony_jira.orchestrator import PollingOrchestrator, SingleIssueOrchestrator
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow
from test_orchestrator import (
    FakeJira, approve_development_run, codex_result, completed_review_issue,
    dispatch_pending_human_resume, ensure_test_git_repository, hydrated_test_issue,
    valid_plan_spec_message, write_fake_codex, write_workflow,
)


OVERRIDE = 'Keep existing Reset Filters behavior and extrapolate it to the new column.'
FORMATTER = 'Reuse the existing date formatter.'
HISTORY_MARKER = 'Chronological input history (oldest first; nothing omitted):\n'


def context_entries(prompt):
    return json.loads(prompt.rsplit(HISTORY_MARKER, 1)[1])


class ContextRunner:
    def __init__(self):
        self.calls = []
        self.refinements = 0
        self.reviews = 0

    async def run(self, prompt, workspace_path, config, **kwargs):
        if prompt.startswith('You are preparing an implementation plan/spec'):
            phase = 'planning'
            message = '{"decision":"needs_human","question":"Should Reset Filters behavior change?"}'
        elif prompt.startswith('You are revising the implementation plan/spec'):
            phase = 'refinement'
            self.refinements += 1
            message = '{"decision":"ready_for_approval"}' if self.refinements == 1 else self.plan(prompt, workspace_path)
        elif prompt.startswith('You are repairing a model-generated PlanSpec'):
            phase = 'repair'
            message = self.plan(prompt, workspace_path)
        elif prompt.startswith('You are triaging pasted human code-review feedback'):
            phase = 'triage'
            message = '{"decision":"code_changes","reason":"In scope; keep the human reset override."}'
        elif prompt.startswith('You are reviewing a completed implementation'):
            phase = 'review'
            self.reviews += 1
            decision = 'changes_required' if self.reviews == 1 else 'approve'
            message = json.dumps({'decision': decision, 'findings': [], 'residual_risk': 'low'})
        else:
            phase = 'correction' if 'The previous implementation was reviewed' in prompt else 'implementation'
            message = 'Implemented the approved behavior and preserved the human override.'
        self.calls.append((phase, prompt))
        return codex_result(workspace_path, 'completed', final_message=message,
                            final_path=config.output_last_message_file)

    def plan(self, prompt, workspace):
        return valid_plan_spec_message(prompt, OVERRIDE, baseline_sha=ensure_test_git_repository(Path(workspace)))


class HumanInputContextTests(unittest.IsolatedAsyncioTestCase):
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
        self.issue = hydrated_test_issue(completed_review_issue()).model_copy(update={'status': 'To Do'})
        self.store = Store(self.root / 'db.sqlite3')
        self.jira = FakeJira(self.issue)
        self.runner = ContextRunner()

    async def poll(self):
        # New instances exercise the durable context across phase/process boundaries.
        restarted = Store(self.root / 'db.sqlite3')
        polling = PollingOrchestrator(self.workflow, self.jira, restarted, codex_runner=self.runner)
        await dispatch_pending_human_resume(polling)
        return restarted.latest_run_for_issue('T-1')

    async def test_every_phase_receives_all_prior_decisions_including_after_handoff(self):
        first = await SingleIssueOrchestrator(
            self.workflow, self.jira, self.store, codex_runner=self.runner,
        ).run_once('T-1')
        self.assertEqual(first.run.blocked_phase, 'planning')
        first_input = self.store.add_human_input('T-1', run_id=first.run.id,
            question=first.run.error, response=OVERRIDE)
        repaired = await self.poll()
        self.assertEqual(repaired.blocked_phase, 'planning_approval', repaired.error)
        second_input = self.store.add_human_input('T-1', run_id=repaired.id,
            question=repaired.error, response=FORMATTER)
        revised = await self.poll()
        self.assertEqual(revised.blocked_phase, 'planning_approval', revised.error)
        _, approval = approve_development_run(self.store, revised)
        completed = await self.poll()
        self.assertEqual(completed.status, 'completed', completed.error)
        self.assertEqual([phase for phase, _ in self.runner.calls], [
            'planning', 'refinement', 'repair', 'refinement', 'implementation',
            'review', 'correction', 'review',
        ])
        for phase, prompt in self.runner.calls[1:]:
            self.assertIn(OVERRIDE, prompt, phase)
            self.assertIn('Review must not reopen that settled point', prompt, phase)
            entries = context_entries(prompt)
            self.assertEqual(sum(entry['id'] == 'human-input:' + first_input['id'] for entry in entries), 1)
        for phase, prompt in self.runner.calls[3:]:
            entries = context_entries(prompt)
            self.assertIn(FORMATTER, prompt, phase)
            self.assertEqual(sum(entry['id'] == 'human-input:' + second_input['id'] for entry in entries), 1)
        for phase, prompt in self.runner.calls[4:]:
            self.assertTrue(any(entry.get('approval_id') == approval['id'] for entry in context_entries(prompt)))
            self.assertTrue(prompt.endswith(completed.human_input_context))
        self.assertIn('Accumulated human input', render_dashboard_html(build_state(self.workflow, self.store)))

        response = TestClient(create_app(self.workflow, self.store)).post(
            f'/api/v1/runs/{completed.id}/human-review', json={
                'reviewer_identity': 'reviewer@example.test',
                'comments': 'Rename the local variable; retain the reset decision.',
                'source_url': 'https://example.test/review/1',
            })
        self.assertEqual(response.status_code, 200, response.text)
        resumed = await self.poll()
        self.assertEqual(resumed.status, 'completed', resumed.error)
        self.assertEqual([phase for phase, _ in self.runner.calls[-3:]], ['triage', 'implementation', 'review'])
        for phase, prompt in self.runner.calls[-3:]:
            self.assertTrue(prompt.endswith(resumed.human_input_context), phase)
            self.assertIn(OVERRIDE, prompt, phase)
            self.assertIn(FORMATTER, prompt, phase)
            entries = context_entries(prompt)
            self.assertTrue(any(entry['kind'] == 'code_review_feedback'
                and 'Rename the local variable' in entry['text'] for entry in entries))

    async def test_frozen_run_context_does_not_change_when_later_input_arrives(self):
        source = self.store.create_run(self.issue, self.root / 'workspace', branch_name=None)
        source = self.store.update_run(source.id, status='blocked', blocked_phase='planning')
        entry = self.store.add_human_input('T-1', run_id=source.id, response=OVERRIDE)
        run = self.store.create_run(self.issue, self.root / 'workspace', branch_name=None)
        context = build_human_input_context(self.store, 'T-1', through=run.started_at,
            run_id=run.id, current_input=entry, previous_run=source)
        self.assertEqual(len(context_entries(context)), 1)
        self.store.update_run(run.id, human_input_context=context)
        next_run = self.store.create_run(self.issue, self.root / 'workspace', branch_name=None)
        self.store.update_run(next_run.id, status='blocked', blocked_phase='planning')
        self.store.add_human_input('T-1', run_id=next_run.id, response=FORMATTER)
        restored = Store(self.root / 'db.sqlite3').get_run(run.id)
        self.assertEqual(restored.human_input_context, context)
        single = SingleIssueOrchestrator(self.workflow, self.jira, self.store, codex_runner=self.runner)
        await single._run_codex_pass(prompt='Inspect this phase.', workspace_path=self.root / 'workspace',
            config=self.workflow.config.codex, run_id=run.id, event_offset=0)
        self.assertIn(OVERRIDE, self.runner.calls[-1][1])
        self.assertNotIn(FORMATTER, self.runner.calls[-1][1])
        newer = build_human_input_context(self.store, 'T-1', through=utc_now(), run_id='next')
        self.assertIn(OVERRIDE, newer)
        self.assertIn(FORMATTER, newer)

    async def test_history_is_issue_scoped_complete_ordered_and_excludes_private_fields(self):
        source = self.store.create_run(self.issue, self.root / 'workspace', branch_name=None)
        other = self.store.create_run(self.issue.model_copy(update={
            'id': '2', 'identifier': 'T-2', 'requirements_snapshot': None,
        }),
                                     self.root / 'other', branch_name=None)
        start = utc_now() - timedelta(days=1)
        with sqlite3.connect(self.store.db_path) as conn:
            for index in range(120):
                conn.execute('''INSERT INTO human_inputs
                    (id,issue_identifier,run_id,response,created_at,claim_token)
                    VALUES (?,?,?,?,?,?)''', (f'entry-{index:03}', 'T-1', source.id,
                    f'Operator decision {index}', (start + timedelta(seconds=index)).isoformat(), 'PRIVATE_LEASE_TOKEN'))
            conn.execute('''INSERT INTO human_inputs (id,issue_identifier,run_id,response,created_at)
                VALUES (?,?,?,?,?)''', ('other', 'T-2', other.id, 'OTHER_ISSUE_SECRET', start.isoformat()))
            conn.execute('''INSERT INTO human_inputs (id,issue_identifier,run_id,response,created_at)
                VALUES (?,?,?,?,?)''', ('future', 'T-1', source.id, 'FUTURE_INPUT',
                                       (utc_now() + timedelta(days=1)).isoformat()))
        context = build_human_input_context(self.store, 'T-1', through=utc_now(), run_id='context')
        entries = context_entries(context)
        self.assertEqual(len(entries), 120)
        self.assertEqual([entry['text'] for entry in entries], [f'Operator decision {i}' for i in range(120)])
        self.assertNotIn('OTHER_ISSUE_SECRET', context)
        self.assertNotIn('FUTURE_INPUT', context)
        self.assertNotIn('PRIVATE_LEASE_TOKEN', context)
        self.assertNotIn('claim_token', context)

    async def test_direct_resume_input_is_carried_from_the_previous_run_snapshot(self):
        source = self.store.create_run(self.issue, self.root / 'workspace', branch_name=None)
        context = build_human_input_context(self.store, 'T-1', through=source.started_at,
            run_id=source.id, current_input={'response': OVERRIDE})
        source = self.store.update_run(source.id, human_input_context=context)
        resumed = build_human_input_context(self.store, 'T-1', through=utc_now(),
            run_id='resumed', previous_run=source, current_input={'response': FORMATTER})
        self.assertEqual([entry['text'] for entry in context_entries(resumed)], [OVERRIDE, FORMATTER])
