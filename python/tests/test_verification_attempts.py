from __future__ import annotations
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from symphony_jira.dashboard import create_app, enrich_run, render_run_row
from symphony_jira.models import Issue
from symphony_jira.store import Store
from symphony_jira.development_verification import DevelopmentVerificationRequest, DevelopmentVerificationTarget
from symphony_jira.verification_insights import coverage_summary, runtime_readiness
from symphony_jira.verification_view import phase_excerpt
from symphony_jira.orchestrator import SingleIssueOrchestrator, PollingOrchestrator
from test_dashboard import write_workflow
from symphony_jira.workflow import load_workflow
from test_test_selection import SelectionRunner, HostVerificationManager
from test_orchestrator import create_verification_case, approve_development_run, dispatch_pending_human_resume


def completed(store, path):
    issue = Issue(id='1', identifier='T-1', title='Title', status='Open', url='u')
    run = store.create_run(issue, path, branch_name=None)
    return store.update_run(run.id, status='completed', verification_status='partial')


def test_queue_is_durable_exclusive_and_preserves_original(tmp_path):
    store = Store(tmp_path / 'state.db')
    run = completed(store, tmp_path / 'T-1')
    item = store.queue_verification(run.id)
    with pytest.raises(ValueError, match='already queued'):
        store.queue_verification(run.id)
    other = Store(store.db_path)
    assert other.list_verification_attempts(run.id)[0]['id'] == item['id']
    assert other.claim_verification(item['id'])
    assert not store.claim_verification(item['id'])
    other.finish_verification(item['id'], 'passed', report_path='.symphony/verification/report.json')
    assert store.get_run(run.id).verification_status == 'partial'
    assert store.list_verification_attempts(run.id)[0]['status'] == 'passed'


def test_verify_endpoint_and_compact_phase_output(tmp_path):
    workflow = load_workflow(write_workflow(tmp_path), environ={'TEST_JIRA_TOKEN': 'token'})
    workflow.config.hooks.verify = 'echo test'
    workflow.config.hooks.use_development_verification_request = True
    store = Store(tmp_path / 'db')
    run = completed(store, tmp_path / 'T-1')
    store.update_run(run.id, final_message='Implementation done. ' + 'long evidence ' * 2000)
    client = TestClient(create_app(workflow, store))
    response = client.post(f'/api/v1/runs/{run.id}/verify', json={'mode': 'all'})
    assert response.status_code == 200
    assert client.post(f'/api/v1/runs/{run.id}/verify', json={}).status_code == 409
    data = enrich_run(store.get_run(run.id), store, workflow)
    page = render_run_row(data)
    assert 'Phase summaries' in page
    assert 'Latest attempt' in page
    assert 'long evidence ' * 100 not in page
    assert client.get(f'/runs/{run.id}/verification').status_code == 200


def test_coverage_distinguishes_passed_assertions_from_missing_behavior():
    plan = SimpleNamespace(requirements=[SimpleNamespace(id='R1', acceptance_criteria=[
        SimpleNamespace(id='AC1', statement='Loads'), SimpleNamespace(id='AC2', statement='Filters')])])
    request = DevelopmentVerificationRequest(schema_version='1.0', targets=[
        DevelopmentVerificationTarget(repository='foyr2', suite='playwright',
            test_args=['playwright/integrated/home.spec.js'], acceptance_criterion_ids=['AC1'])])
    rows = coverage_summary(plan, request, [{'status': 'passed'}])
    assert [r['status'] for r in rows] == ['selected_tests_passed', 'uncovered']
    assert rows[0]['tests']
    assert not rows[1]['tests']


def test_readiness_missing_controller_and_browser_is_not_ready(tmp_path):
    target = DevelopmentVerificationTarget(repository='foyr2', suite='playwright',
        test_args=['playwright/integrated/home.spec.js'])
    with patch('symphony_jira.verification_insights.subprocess.run', side_effect=OSError('missing')):
        report = runtime_readiness(target, tmp_path / 'missing.py')
    assert report['status'] == 'unavailable'
    assert report['runtime_fingerprint'] is None
    assert any(c['name'] == 'browser' and c['status'] == 'unavailable' for c in report['checks'])


def test_structured_phase_summary_is_bounded():
    summary = phase_excerpt(json.dumps({'decision': 'approve', 'findings': ['x' * 10000] * 20}))
    assert summary == 'approve. 20 findings'


@pytest.mark.parametrize('mode', ['all', 'retry', 'drift', 'cancel'])
def test_worker_verifies_without_implementation_or_review(tmp_path, mode):
    async def scenario():
        original, _, _, jira = create_verification_case(tmp_path, verification_status='passed',
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
        source = original.store.latest_run_for_issue('T-1')
        assert source.status == 'completed'
        handoff_impact = original.store.get_impact_summary(source.id)
        assert handoff_impact is not None
        phases = list(runner.phases)
        started = asyncio.Event()
        if mode == 'cancel':
            async def hang(*args, **kwargs):
                started.set()
                await asyncio.Event().wait()
            runner.run = hang
        if mode == 'drift':
            from pathlib import Path
            (Path(source.workspace_path) / 'foyr2' / 'changed.py').write_text('new code after original verification')
        original.store.queue_verification(source.id, 'retry' if mode in {'retry', 'drift'} else 'all')
        await polling.dispatch_verifications()
        if mode == 'cancel':
            await asyncio.wait_for(started.wait(), timeout=5)
            await polling.shutdown()
        else:
            await asyncio.gather(*polling.verification_tasks.values())
            await polling.dispatch_verifications()
        attempts = original.store.list_verification_attempts(source.id)
        if mode == 'cancel':
            assert attempts[0]['status'] == 'interrupted', attempts
        elif mode == 'drift':
            assert attempts[0]['status'] == 'environment_error', attempts
            assert 'Code changed' in attempts[0]['error']
        else:
            assert attempts[0]['status'] == 'failed', attempts
            assert attempts[0]['report_path']
        assert runner.phases == phases + ([] if mode == 'cancel' else ['test_selection'])
        assert original.store.latest_run_for_issue('T-1').id == source.id
        assert original.store.get_run(source.id).verification_output_path == source.verification_output_path
        assert original.store.get_impact_summary(source.id) == handoff_impact
        assert not polling.claimed
    asyncio.run(scenario())


def test_targeted_retry_retains_coverage_gap_and_rejects_changed_selectors():
    from symphony_jira.verification_attempts import retry_request
    from symphony_jira.development_verification import verification_status
    request = DevelopmentVerificationRequest(schema_version='1.0', workspace_diff_hash='a' * 64,
        snapshot_repositories=['foyr2'], targets=[
            DevelopmentVerificationTarget(repository='foyr2', suite='python', test_args=['tests/test_home.py']),
            DevelopmentVerificationTarget(repository='foyr2', suite='frontend', test_args=['foyr/client_src/karma_test/homeTest.js'])])
    prior = {'workspace_diff_hash': 'a' * 64, 'results': [
        {**request.targets[0].model_dump(), 'status': 'passed'},
        {**request.targets[1].model_dump(), 'status': 'environment_error'}]}
    retry = retry_request(request, prior)
    assert [t.suite for t in retry.targets] == ['frontend']
    assert 'not rerun' in retry.skipped[0].reason
    assert verification_status([{'returncode': 0}], skipped=bool(retry.skipped)) == 'partial'
    prior['results'][1]['test_args'] = ['foyr/client_src/karma_test/otherTest.js']
    with pytest.raises(ValueError, match='selectors changed'):
        retry_request(request, prior)


def test_assertion_summary_reads_only_safe_runner_artifacts(tmp_path):
    from symphony_jira.verification_insights import runner_failure_evidence
    result = tmp_path / 'results' / '20261117-run'
    result.mkdir(parents=True)
    junit = result / 'junit.xml'
    junit.write_text('<testsuite><testcase name="date_filter"><failure message="Expected September, got October"/></testcase></testsuite>')
    output = f'Artifacts: {result}'
    evidence = runner_failure_evidence(output, tmp_path / 'verify.py')
    assert 'Expected September' in evidence['assertions'][0]
    assert evidence['evidence_paths'] == [str(junit)]
    outside = tmp_path / 'outside.xml'
    outside.write_text(junit.read_text())
    junit.unlink()
    junit.symlink_to(outside)
    assert runner_failure_evidence(output, tmp_path / 'verify.py')['assertions'] == []


def test_verification_blocks_fresh_dispatch_and_rejects_invalid_mode(tmp_path):
    store = Store(tmp_path / 'db')
    source = completed(store, tmp_path / 'T-1')
    with pytest.raises(ValueError, match='mode'):
        store.queue_verification(source.id, [])
    store.queue_verification(source.id)
    issue = Issue(id='1', identifier='T-1', title='Title', status='Open', url='u')
    with pytest.raises(ValueError, match='active verification'):
        store.create_run(issue, tmp_path / 'T-1', branch_name=None, require_no_active_run=True)


def test_phase_history_keeps_latest_summary_after_many_logs(tmp_path):
    store = Store(tmp_path / 'db')
    run = completed(store, tmp_path)
    store.add_log(run.id, 'phase_summary', json.dumps({'phase': 'review', 'summary': 'First review', 'path': 'first'}))
    store.add_log(run.id, 'phase_summary', json.dumps({'phase': 'review', 'summary': 'Final review', 'path': 'last'}))
    for i in range(105):
        store.add_log(run.id, 'info', str(i))
    rows = store.list_phase_summaries(run.id)
    assert [json.loads(row['message'])['summary'] for row in rows] == ['First review', 'Final review']


def test_unavailable_preflight_prevents_test_execution(tmp_path):
    from symphony_jira.development_verification import execute_request
    request = DevelopmentVerificationRequest(schema_version='1.0', targets=[
        DevelopmentVerificationTarget(repository='foyr2', suite='python', test_args=['tests/test_home.py'])])
    readiness = {'repository': 'foyr2', 'suite': 'python', 'status': 'unavailable',
                 'checks': [{'name': 'image', 'status': 'unavailable', 'detail': 'Missing local image'}]}
    with patch('symphony_jira.development_verification.runtime_readiness', return_value=readiness), \
         patch('symphony_jira.development_verification.subprocess.Popen') as process:
        code = execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path / 'unused',
                               preflight=True, result_path='.symphony/result.json')
    assert code == 125
    process.assert_not_called()
    report = json.loads((tmp_path / '.symphony/result.json').read_text())
    assert report['status'] == 'environment_error'
    assert report['readiness'] == [readiness]
