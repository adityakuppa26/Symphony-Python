import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from symphony_jira.config import WorkflowConfig
from symphony_jira.dashboard import build_state, enrich_run, render_dashboard_html
from symphony_jira.handlers.development import DevelopmentHandler
from symphony_jira.orchestrator import PollingOrchestrator, SingleIssueOrchestrator
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow
from test_orchestrator import (
    CompletedReviewCodexRunner, FakeJira, PlanThenImplementCodexRunner,
    approve_development_run, codex_result, completed_review_issue,
    create_completed_review_action, create_verification_case,
    dispatch_pending_human_resume, load_completed_review_workflow,
)


def independent(workflow, kind):
    data = workflow.config.model_dump()
    data['kind'] = kind
    data['codex'].update(plan_before_implementation=True, require_plan_approval=True,
                         review_after_run=True, max_review_iterations=3)
    data['workspace']['managed_repositories'] = ['repo']
    workflow.config = WorkflowConfig.model_validate(data)
    return workflow


class IndependentRunner(PlanThenImplementCodexRunner):
    def __init__(self):
        super().__init__()
        self.args = []
        self.review_prompts = []

    async def run(self, prompt, workspace_path, config, **kwargs):
        self.args.append(list(config.args))
        if prompt.startswith('You are reviewing a completed implementation'):
            self.prompts_seen.append('review')
            self.review_prompts.append(prompt)
            decision = 'changes_required' if len(self.review_prompts) == 1 else 'approve'
            return codex_result(workspace_path, 'completed', final_message=(
                '{"decision":"' + decision + '","findings":[]}'
            ), final_path=config.output_last_message_file)
        return await super().run(prompt, workspace_path, config, **kwargs)


class IndependentWorkflowTests(unittest.TestCase):
    def test_development_config_requires_approval_and_review_with_advisory_checks(self):
        config = load_workflow(Path(__file__).resolve().parents[1] / 'WORKFLOW.md').config
        self.assertEqual(config.kind, 'development')
        self.assertEqual(config.workspace.managed_repositories, [Path('foyr2'), Path('cpm'), Path('pi')])
        self.assertIn('--skip-git-repo-check', config.codex.args)
        self.assertFalse(config.hooks.verify_required)
        self.assertIsNone(config.hooks.verify)
        self.assertTrue(config.codex.require_plan_approval)
        self.assertTrue(config.codex.review_after_run)

    def test_approval_review_loop_and_handoff_with_advisory_verification(self):
        async def run(kind, verification):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                original, manager, _, jira = create_verification_case(
                    root, verification_status=verification, required=True)
                workflow = independent(original.workflow, kind)
                if verification == 'not_configured':
                    workflow.config.hooks.verify = None
                elif verification == 'invalid_request':
                    workflow.config.hooks.use_development_verification_request = True
                runner = IndependentRunner()
                single = SingleIssueOrchestrator(workflow, jira, original.store,
                    codex_runner=runner, workspace_manager=manager)
                self.assertIsInstance(single.handler,
                    DevelopmentHandler)
                planned = await single.run_once('T-1')
                self.assertEqual(planned.run.blocked_phase, 'planning_approval', planned.run.error)
                self.assertEqual(runner.prompts_seen, ['plan'])
                self.assertEqual(runner.args[0][-2:], ['--sandbox', 'read-only'])
                approve_development_run(original.store, planned.run)
                polling = PollingOrchestrator(workflow, jira, original.store,
                    codex_runner=runner, workspace_manager=manager)
                if verification == 'unavailable':
                    async def unavailable(*args, **kwargs):
                        raise OSError('test runner is unavailable')
                    with patch.object(manager, 'run_hook', side_effect=unavailable):
                        await dispatch_pending_human_resume(polling)
                else:
                    await dispatch_pending_human_resume(polling)
                result = original.store.latest_run_for_issue('T-1')
                self.assertEqual(result.status, 'completed', result.error)
                expected = 'failed' if verification in {'invalid_request', 'unavailable'} else verification
                self.assertEqual(result.verification_status, expected)
                self.assertEqual(runner.prompts_seen,
                    ['plan', 'implementation', 'review', 'implementation', 'review'])
                self.assertEqual(jira.transitions, ['Done'])
                self.assertIn('nearby tracked precedents', runner.plan_prompt)
                self.assertIn('minimal scope', runner.review_prompts[-1])
                self.assertIn('Running tests is optional', runner.implementation_prompt)
                self.assertTrue(enrich_run(result, original.store, workflow)['verification_advisory'])
                html = render_dashboard_html(build_state(workflow, original.store))
                self.assertIn('Advisory', html)
        for kind in ('development',):
            for verification in ('passed', 'failed', 'not_configured', 'invalid_request', 'unavailable'):
                with self.subTest(kind=kind, verification=verification):
                    asyncio.run(run(kind, verification))

    def test_planning_rejects_repositories_owned_by_another_workflow(self):
        async def run(kind):
            with tempfile.TemporaryDirectory() as tmp:
                original, manager, _, jira = create_verification_case(
                    Path(tmp), verification_status='passed', required=False)
                workflow = independent(original.workflow, kind)
                runner = PlanThenImplementCodexRunner(repository='other')
                result = await SingleIssueOrchestrator(
                    workflow, jira, original.store, codex_runner=runner,
                    workspace_manager=manager).run_once('T-1')
                self.assertEqual(result.run.status, 'blocked')
                self.assertEqual(result.run.blocked_phase, 'planning')
                self.assertIn('outside this workflow', result.run.error)
                self.assertNotIn('implementation', runner.prompts_seen)
        for kind in ('development',):
            with self.subTest(kind=kind):
                asyncio.run(run(kind))

    def test_later_human_review_reuses_context_after_restart(self):
        async def run(kind):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                workflow = independent(load_completed_review_workflow(root), kind)
                issue = completed_review_issue()
                store = Store(root / 'db.sqlite3')
                action, source, result_run, _ = create_completed_review_action(root, workflow, issue, store)
                runner = CompletedReviewCodexRunner('code_changes')
                captured = []
                original_run = runner.run
                async def capture(prompt, *args, **kwargs):
                    captured.append(prompt)
                    return await original_run(prompt, *args, **kwargs)
                runner.run = capture
                restarted = Store(root / 'db.sqlite3')
                polling = PollingOrchestrator(workflow, FakeJira(issue), restarted, codex_runner=runner)
                await polling.poll_once()
                await asyncio.gather(*(item.task for item in polling.running.values()))
                await polling.reap_finished()
                result = restarted.get_run(result_run.id)
                self.assertEqual(result.status, 'completed', result.error)
                self.assertEqual(result.workspace_path, source.workspace_path)
                self.assertEqual(result.plan_spec_hash, source.plan_spec_hash)
                self.assertEqual(result.plan_approval_id, source.plan_approval_id)
                self.assertEqual(runner.phases, ['triage', 'implementation', 'review'])
                self.assertIn('Original implementation completed.', captured[0])
                self.assertIn('Original automated review', captured[0])
                self.assertIn(action['comments'], captured[1])
                self.assertEqual(restarted.get_human_review_action(action['id'])['status'], 'completed')
                history = (Path(result.workspace_path) / workflow.config.codex.output_review_history_file).read_text()
                self.assertIn('Original automated review', history)
                self.assertIn(f"Human review {action['id']}", history)
        for kind in ('development',):
            with self.subTest(kind=kind):
                asyncio.run(run(kind))
