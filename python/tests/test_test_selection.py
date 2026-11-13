from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from symphony_jira.orchestrator import SingleIssueOrchestrator, PollingOrchestrator
from symphony_jira.workspace import HookResult, WorkspaceManager
from symphony_jira.human_review import read_frozen_text_artifact, write_frozen_text_artifact
from symphony_jira.development_verification import DevelopmentVerificationRequest, verification_request_hash
from symphony_jira.models import utc_now
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
                        'request_hash': verification_request_hash(request), 'finished_at': utc_now().isoformat(),
                        'skipped': [], 'results': [{**request.targets[0].model_dump(),
                                                   'status': 'failed', 'returncode': 1}]}), label='host result')
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


def selection_request(**changes):
    return json.dumps({
        'schema_version': '1.0',
        'targets': [{'repository': 'foyr2', 'test_args': ['tests/test_projects.py']}],
        **changes,
    })


def run_selection_attempts(root, messages, *, changed_code=False, status='completed', repository='foyr2', suites=None):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, patch

    orchestrator, _, _, _ = create_verification_case(
        root, verification_status='passed', required=False, workspace_subdir=repository)
    orchestrator.config.codex.development_test_suites = suites or ['python']
    orchestrator.store = Mock()
    plan = SimpleNamespace(
        affected_surface=SimpleNamespace(repositories=[repository]),
        baseline_repository_shas=[],
    )
    snapshot = SimpleNamespace(changed_repositories=(repository,), content_hash='a' * 64, content='actual diff')
    snapshots = [snapshot] + [
        SimpleNamespace(**{**vars(snapshot), 'content_hash': 'b' * 64}) if changed_code else snapshot
        for _ in messages
    ]
    passes = AsyncMock(side_effect=[
        (SimpleNamespace(status=status, final_message=message, error='runner failed'), 10 + index)
        for index, message in enumerate(messages)
    ])
    with patch('symphony_jira.orchestrator.capture_workspace_diff', side_effect=snapshots), \
            patch('symphony_jira.orchestrator.include_changed_test_files', side_effect=lambda request, **kwargs: request), \
            patch.object(orchestrator, '_run_codex_pass', passes):
        result = asyncio.run(orchestrator._select_development_tests(
            issue=SimpleNamespace(identifier='T-1'), run_id='selection-run', workspace_path=root,
            plan=plan, plan_message='approved plan', implementation_message='implemented', event_offset=9))
    return result, passes, orchestrator


def test_selection_repairs_multiple_validation_errors(tmp_path):
    invalid = selection_request(skipped=[{'repository': 'foyr2', 'reason': str(i)} for i in range(4)])
    duplicate = selection_request(targets=[
        {'repository': 'foyr2', 'test_args': ['tests/test_projects.py']},
        {'repository': 'foyr2', 'test_args': ['tests/test_home.py']},
    ])
    (request, offset, error), passes, orchestrator = run_selection_attempts(
        tmp_path, [invalid, duplicate, selection_request()])
    assert error is None
    assert offset == 12
    assert request.workspace_diff_hash == 'a' * 64
    assert request.snapshot_repositories == ['foyr2']
    assert passes.await_count == 3
    assert 'at most 3 items' in passes.call_args_list[1].kwargs['prompt']
    assert invalid in passes.call_args_list[1].kwargs['prompt']
    assert 'repositories must be unique' in passes.call_args_list[2].kwargs['prompt']
    assert duplicate in passes.call_args_list[2].kwargs['prompt']
    assert [call.kwargs['event_offset'] for call in passes.call_args_list] == [9, 10, 11]
    assert orchestrator.store.add_log.call_count == 2
    saved = DevelopmentVerificationRequest.model_validate_json(
        (tmp_path / orchestrator.config.codex.output_development_verification_request_file).read_text())
    assert saved == request


def test_selection_validation_retries_are_bounded(tmp_path):
    (request, offset, error), passes, orchestrator = run_selection_attempts(tmp_path, ['not JSON'] * 3)
    assert request is None
    assert offset == 12
    assert 'Invalid JSON' in error
    assert passes.await_count == 3
    assert not (tmp_path / orchestrator.config.codex.output_development_verification_request_file).exists()


def test_selection_retries_disabled_suite_and_missing_repository(tmp_path):
    disabled = selection_request(targets=[
        {'repository': 'foyr2', 'suite': 'karma', 'test_args': ['projectsTest.js']},
    ])
    missing = selection_request(targets=[])
    (request, _, error), passes, _ = run_selection_attempts(tmp_path, [disabled, missing, selection_request()])
    assert request is not None
    assert error is None
    assert 'suite disabled' in passes.call_args_list[1].kwargs['prompt']
    assert 'cover exactly' in passes.call_args_list[2].kwargs['prompt']


def test_selection_does_not_retry_after_code_changes(tmp_path):
    (request, _, error), passes, _ = run_selection_attempts(tmp_path, ['not JSON'], changed_code=True)
    assert request is None
    assert 'Code changed during read-only test selection' in error
    assert passes.await_count == 1


def test_selection_does_not_retry_runner_failure(tmp_path):
    (request, _, error), passes, _ = run_selection_attempts(tmp_path, [''], status='failed')
    assert request is None
    assert error == 'runner failed'
    assert passes.await_count == 1


def test_cpm_selection_preserves_coverage_gap_without_unrelated_tests(tmp_path):
    message = selection_request(targets=[], skipped=[{'repository': 'cpm', 'reason': 'No regression fixture for changed behavior'}])
    (request, _, error), passes, orchestrator = run_selection_attempts(
        tmp_path, [message], repository='cpm', suites=['python', 'functional'])
    assert error is None
    assert request.targets == []
    assert request.skipped[0].reason == 'No regression fixture for changed behavior'
    assert request.workspace_diff_hash == 'a' * 64
    saved = DevelopmentVerificationRequest.model_validate_json(
        (tmp_path / orchestrator.config.codex.output_development_verification_request_file).read_text())
    assert saved == request
    assert 'suite=functional' in passes.call_args.kwargs['prompt']
