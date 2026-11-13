import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from symphony_jira.dashboard import create_app, enrich_run, render_dashboard_html, build_state
from symphony_jira.models import Issue
from symphony_jira.orchestrator import SingleIssueOrchestrator
from symphony_jira.presentation import QUESTION_CONTRACT, question_view, render_question, render_document
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow
from test_dashboard import write_workflow, dashboard_requirements_snapshot, dashboard_plan_payload
from test_orchestrator import FakeJira, codex_result


OPTIONS = [
    {'label': 'Reuse the existing helper', 'description': 'Keep the current formatting behavior.'},
    {'label': 'Extend the existing helper', 'description': 'Add the missing case in one place.'},
    {'label': 'Leave behavior unchanged', 'description': 'Defer this behavior change.'},
]


@pytest.mark.parametrize('phase', ['planning', 'implementation', 'review', 'human_review_triage'])
def test_question_options_survive_dispatch_restart_and_submission(tmp_path, phase):
    workflow = load_workflow(write_workflow(tmp_path), environ={'TEST_JIRA_TOKEN':'token'})
    store = Store(tmp_path/'db.sqlite3')
    issue = Issue(id='1', identifier='T-1', title='Format project dates', status='To Do', url='u')
    run = store.create_run(issue, tmp_path/'workspace', branch_name=None)
    payload = {'decision':'needs_human','question':'How should this date be formatted?','options':OPTIONS}
    class Runner:
        async def run(self, prompt, workspace_path, config, **kwargs):
            assert QUESTION_CONTRACT in prompt
            return codex_result(workspace_path, 'completed', final_message=json.dumps(payload))
    orchestrator = SingleIssueOrchestrator(workflow, FakeJira(issue), store, codex_runner=Runner())
    asyncio.run(orchestrator._run_codex_pass(prompt='Inspect this phase.', workspace_path=tmp_path/'workspace',
        config=workflow.config.codex, run_id=run.id, event_offset=0, event_prefix=phase))
    run = store.update_run(run.id, status='blocked', blocked_phase=phase, error=payload['question'])
    restarted = Store(tmp_path/'db.sqlite3')
    view = enrich_run(run, restarted, workflow)
    assert view['human_question']['options'] == OPTIONS
    page = render_dashboard_html(build_state(workflow, restarted))
    assert page.count('type="radio" name="choice"') == 4
    assert 'Reuse the existing helper' in page
    chosen = OPTIONS[0]['label'] + ': ' + OPTIONS[0]['description']
    response = TestClient(create_app(workflow, restarted)).post(f'/api/v1/runs/{run.id}/human-input',
        json={'action':'decision','choice':chosen})
    assert response.status_code == 200, response.text
    assert restarted.list_human_inputs(run_id=run.id)[0]['response'] == chosen
    assert restarted.list_plan_approvals(run.id) == []


def test_malformed_choices_use_three_safe_clarification_replies():
    view = question_view({'question':'Which behavior?', 'options':['Only one']})
    assert len(view['options']) == 3
    assert not view['suggested']
    assert render_question(view).count('type="radio"') == 4


def test_documents_and_choices_escape_untrusted_content():
    attack = '<script>alert("unsafe")</script>'
    document = render_document(json.dumps({'summary':attack, 'acceptance_criteria':[{'description':'Must work'}]}))
    assert '<script>' not in document
    assert '&lt;script&gt;' in document
    assert '<h4>Acceptance criteria</h4>' in document
    assert 'View original text' in document
    assert '<script>' not in render_question({'question':attack,'options':[attack,'B','C']})
    prose = render_document('# Result\n\n**Passed**\n- first\n- second\n```python\nprint("<unsafe>")\n```')
    assert '<h3>Result</h3>' in prose and '<ul>' in prose and '<strong>Passed</strong>' in prose
    assert '&lt;unsafe&gt;' in prose


def test_readable_plan_retains_integrity_checks_and_raw_endpoint(tmp_path):
    workflow = load_workflow(write_workflow(tmp_path), environ={'TEST_JIRA_TOKEN':'token'})
    store=Store(tmp_path/'db.sqlite3')
    snapshot=dashboard_requirements_snapshot()
    issue=Issue(id='1',identifier='T-1',title='Plan',status='To Do',url='u',requirements_snapshot=snapshot)
    run=store.create_run(issue,tmp_path/'workspaces/T-1',branch_name=None)
    content=json.dumps(dashboard_plan_payload(snapshot.content_hash))
    run=store.update_run(run.id,status='blocked',blocked_phase='planning_approval',final_message=content)
    plan_path=Path(run.workspace_path)/'.symphony/codex-plan.md'
    plan_path.parent.mkdir(parents=True);plan_path.write_text(content)
    client=TestClient(create_app(workflow,store))
    page=client.get(f'/runs/{run.id}/plan')
    assert page.status_code==200, page.text
    assert 'text/html' in page.headers['content-type']
    assert 'Acceptance criteria' in page.text and 'Original JSON' in page.text
    assert client.get(f'/api/v1/runs/{run.id}/plan').status_code==200
    requirements=client.get(f'/runs/{run.id}/requirements')
    assert requirements.status_code==200
    plan_path.write_text('{}')
    assert client.get(f'/runs/{run.id}/plan').status_code==409


def test_verification_reader_cannot_read_outside_workspace(tmp_path):
    workflow=load_workflow(write_workflow(tmp_path),environ={'TEST_JIRA_TOKEN':'token'})
    store=Store(tmp_path/'db.sqlite3')
    issue=Issue(id='1',identifier='T-1',title='Check',status='To Do',url='u')
    workspace=tmp_path/'workspace';workspace.mkdir()
    run=store.create_run(issue,workspace,branch_name=None)
    output=workspace/'results.json';output.write_text(json.dumps({'status':'passed','results':[{'test':'test_changed_behavior','status':'passed'}]}))
    store.update_run(run.id,verification_output_path=str(output))
    client=TestClient(create_app(workflow,store))
    page=client.get(f'/runs/{run.id}/verification')
    assert page.status_code==200 and 'test_changed_behavior' in page.text
    outside=tmp_path/'private.txt';outside.write_text('not a public artifact')
    store.update_run(run.id,verification_output_path=str(outside))
    page=client.get(f'/runs/{run.id}/verification')
    assert page.status_code==409 and 'not a public artifact' not in page.text
    output.unlink();output.symlink_to(outside)
    store.update_run(run.id,verification_output_path=str(output))
    assert client.get(f'/runs/{run.id}/verification').status_code==409


def test_retained_question_options_render_without_new_log_records(tmp_path):
    workflow=load_workflow(write_workflow(tmp_path),environ={'TEST_JIRA_TOKEN':'token'})
    store=Store(tmp_path/'db.sqlite3')
    issue=Issue(id='1',identifier='T-1',title='Question',status='To Do',url='u')
    run=store.create_run(issue,tmp_path/'workspace',branch_name=None)
    payload={'decision':'needs_human','question':'Which helper?','options':OPTIONS}
    run=store.update_run(run.id,status='blocked',blocked_phase='planning',error='Which helper?',final_message=json.dumps(payload))
    assert enrich_run(run,store,workflow)['human_question']['options']==OPTIONS
    run=store.update_run(run.id,error='A different blocker')
    assert not enrich_run(run,store,workflow)['human_question']['suggested']


def test_review_uses_retained_triage_choices_when_run_has_only_legacy_fallback():
    from symphony_jira.review_page import review_progress
    action={'triage_decision':'needs_human','triage_output':json.dumps({'question':'Which helper?','options':OPTIONS})}
    run={'id':'run','status':'blocked','blocked_phase':'review','error':'Which helper?',
         'human_question':question_view({},'Which helper?')}
    view=review_progress(action,run)
    assert 'Reuse the existing helper' in view['question_html']
    assert 'Explain the options' not in view['question_html']
