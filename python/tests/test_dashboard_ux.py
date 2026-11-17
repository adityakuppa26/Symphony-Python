from fastapi.testclient import TestClient
from symphony_jira.dashboard import create_app
from symphony_jira.models import Issue
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow
from test_dashboard import write_workflow


def test_case_page_preserves_history_without_historical_actions(tmp_path):
    workflow = load_workflow(write_workflow(tmp_path), environ={'TEST_JIRA_TOKEN': 'token'})
    workflow.config.hooks.verify = 'echo test'
    workflow.config.hooks.use_development_verification_request = True
    store = Store(tmp_path / 'state.db')
    issue = Issue(id='1', identifier='T-1', title='Title', status='Open', url='u')
    old = store.create_run(issue, tmp_path / 'case', branch_name=None)
    store.update_run(old.id, status='completed', final_message='Earlier change.')
    current = store.create_run(issue, tmp_path / 'case', branch_name=None, attempt=2)
    store.update_run(current.id, status='completed', final_message='<script>bad()</script> Latest change.')
    client = TestClient(create_app(workflow, store))
    page = client.get(f'/runs/{current.id}')
    assert page.status_code == 200
    assert 'data-case-page="true"' in page.text
    assert 'Earlier attempts (1)' in page.text
    assert f'/api/v1/runs/{current.id}/verify' in page.text
    assert f'/api/v1/runs/{old.id}/verify' not in page.text
    assert '<script>bad()</script>' not in page.text
    assert f'/api/v1/runs/{current.id}/question' in page.text
    assert 'data-copy-path=' in page.text
    assert client.get('/runs/missing').status_code == 404
    old_page = client.get(f'/runs/{old.id}')
    assert old_page.status_code == 200
    assert 'data-verification-form' not in old_page.text.split('<script>')[0]
