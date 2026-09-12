from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from symphony_jira.orchestrator import SingleIssueOrchestrator, PollingOrchestrator
from symphony_jira.workspace import HookResult, WorkspaceManager
from symphony_jira.human_review import read_frozen_text_artifact, write_frozen_text_artifact
from symphony_jira.development_verification import DevelopmentVerificationRequest
from test_orchestrator import (
    PlanThenImplementCodexRunner, create_verification_case, codex_result,
    approve_development_run, dispatch_pending_human_resume,
)


class SelectionRunner(PlanThenImplementCodexRunner):
    def __init__(self):
        super().__init__(repository='foyr2')
        self.phases = []
        self.selection_prompt = ''
        self.review_prompt = ''

    async def run(self, prompt, workspace_path, config, **kwargs):
        if prompt.startswith('Select focused development tests'):
            self.phases.append('test_selection')
            self.selection_prompt = prompt
            return codex_result(workspace_path, 'completed', final_message=json.dumps({
                'schema_version': '1.0', 'targets': [{
                    'repository': 'foyr2', 'test_args': ['tests/views/api/test_home.py'],
                    'reason': 'Covers the implemented Home API change.',
                }],
            }), final_path=config.output_last_message_file)
        if prompt.startswith('You are reviewing a completed implementation'):
            self.phases.append('review')
            self.review_prompt = prompt
            return codex_result(workspace_path, 'completed', final_message='{"decision":"approve","findings":[]}',
                                final_path=config.output_last_message_file)
        if 'planning pass only' not in prompt.lower():
            self.phases.append('implementation')
            (Path(workspace_path) / 'foyr2' / 'changed.py').write_text('actual implementation\n')
        else:
            self.phases.append('planning')
        return await super().run(prompt, workspace_path, config, **kwargs)


class HostVerificationManager(WorkspaceManager):
    async def run_hook(self, name, script, workspace_path, hook_context=None):
        assert name == 'verify'
        config = hook_context['config'].codex
        request = DevelopmentVerificationRequest.model_validate_json(read_frozen_text_artifact(
            workspace_path, config.output_development_verification_request_file, label='selected tests', required=True))
        assert request.workspace_diff_hash
        assert request.snapshot_repositories == ['foyr2']
        write_frozen_text_artifact(workspace_path, config.output_development_verification_result_file,
            json.dumps({'status': 'failed', 'workspace_diff_hash': request.workspace_diff_hash,
                        'results': [{'repository': 'foyr2', 'status': 'failed'}]}), label='host result')
        path = Path(workspace_path) / '.symphony/hooks/verify.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('ASSERTION_FAILURE: limit must be an integer')
        return HookResult(name='verify', returncode=1, log_path=path, output=path.read_text())


class TestSelectionTests(unittest.TestCase):
    def test_selects_from_actual_changes_and_gives_advisory_results_to_review(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                original, _, _, jira = create_verification_case(root, verification_status='passed',
                    required=False, workspace_subdir='foyr2')
                workflow = original.workflow
                workflow.config.codex.require_plan_approval = True
                workflow.config.codex.review_after_run = True
                workflow.config.codex.select_tests_after_implementation = True
                workflow.config.hooks.use_development_verification_request = True
                runner = SelectionRunner()
                manager = HostVerificationManager(workflow.config.workspace, workflow.config.hooks)
                planned = await SingleIssueOrchestrator(workflow, jira, original.store,
                    codex_runner=runner, workspace_manager=manager).run_once('T-1')
                approve_development_run(original.store, planned.run)
                polling = PollingOrchestrator(workflow, jira, original.store,
                    codex_runner=runner, workspace_manager=manager)
                await dispatch_pending_human_resume(polling)
                completed = original.store.latest_run_for_issue('T-1')
                self.assertEqual(completed.status, 'completed', completed.error)
                self.assertEqual(completed.verification_status, 'failed')
                self.assertEqual(runner.phases, ['planning', 'implementation', 'test_selection', 'review'])
                self.assertIn('changed.py', runner.selection_prompt)
                self.assertIn('Accumulated human input', runner.selection_prompt)
                self.assertIn('ASSERTION_FAILURE', runner.review_prompt)
                self.assertIn('Status: failed', runner.review_prompt)
                self.assertTrue(Path(completed.verification_output_path).is_file())
                self.assertIn('/verification/', completed.verification_output_path)
        asyncio.run(run())
