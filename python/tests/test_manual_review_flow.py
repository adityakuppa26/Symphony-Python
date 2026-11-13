import concurrent.futures
import asyncio
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from symphony_jira.dashboard import create_app
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow
from symphony_jira.review_page import feedback_items
from symphony_jira.orchestrator import PollingOrchestrator
from test_orchestrator import (
    load_completed_review_workflow, completed_review_issue, create_completed_review_action,
    CompletedReviewCodexRunner, FakeJira, dispatch_pending_human_resume,
)
from test_dashboard import create_completed_dashboard_run, minimal_human_review_context, write_workflow


@pytest.fixture
def review_case():
    with tempfile.TemporaryDirectory(prefix="symphony-review-test-") as folder:
        root = Path(folder)
        workflow = load_workflow(write_workflow(root), environ={"TEST_JIRA_TOKEN": "token"})
        store = Store(root / "state.sqlite3")
        source = create_completed_dashboard_run(root, store)
        with patch("symphony_jira.dashboard.prepare_human_review_context", return_value=minimal_human_review_context()) as context:
            yield store, source, TestClient(create_app(workflow, store)), context


def submit(client, source, **changes):
    return client.post(f"/api/v1/runs/{source.id}/human-review", json={
        "reviewer_identity": "Operator", "comments": "file.py:42 — handle empty input", **changes,
    })


def test_manual_feedback_needs_no_pr_url_and_has_a_progress_page(review_case):
    store, source, client, _ = review_case
    response = submit(client, source)
    assert response.status_code == 200, response.text
    action = response.json()["human_review"]
    assert action["source_url"] == ""
    assert len(store.list_runs_for_issue(source.issue_identifier)) == 2
    page = client.get(f"/reviews/{action['id']}")
    assert page.status_code == 200
    assert 'Waiting for a worker' in page.text
    assert 'file.py:42' in page.text
    assert 'Manual feedback' in page.text
    state = client.get(f"/api/v1/reviews/{action['id']}").json()
    assert state["status"] == "queued"
    assert 'claim_token' not in state
    assert 'workspace_diff' not in state


def test_retry_after_lost_response_reuses_exact_action_even_after_restart(review_case):
    store, source, client, context = review_case
    first = submit(client, source).json()
    context.side_effect = AssertionError("A replay must not refreeze or revalidate changed workspace files")
    second = submit(client, source)
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["run"]["id"] == first["run"]["id"]
    restarted = Store(store.db_path)
    assert len(restarted.list_human_review_actions_for_issue(source.issue_identifier)) == 1
    assert len(restarted.list_runs_for_issue(source.issue_identifier)) == 2


def test_changed_submission_cannot_silently_replace_queued_feedback(review_case):
    store, source, client, _ = review_case
    first = submit(client, source).json()
    second = submit(client, source, comments="New scope")
    assert second.status_code == 409
    assert 'different review' in second.json()["detail"]
    assert store.get_human_review_action(first["human_review"]["id"])["comments"] == 'file.py:42 — handle empty input'


def test_atomic_duplicate_submissions_reserve_only_one_result_run(review_case):
    store, source, _, _ = review_case
    def create():
        return Store(store.db_path).create_human_review_action(source.id,
            reviewer_identity='Operator', source_url='', comments='Fix empty input',
            **minimal_human_review_context())
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert results[0][0]['id'] == results[1][0]['id']
    assert results[0][1].id == results[1][1].id
    assert len(store.list_runs_for_issue(source.issue_identifier)) == 2


def test_completed_review_can_receive_another_manual_round(review_case):
    store, source, client, _ = review_case
    first = submit(client, source).json()
    result = store.update_run(first['run']['id'], status='completed', final_message='R1 addressed in file.py:42')
    second = submit(client, result, comments='Second round: improve the test name')
    assert second.status_code == 200, second.text
    assert second.json()['human_review']['source_run_id'] == result.id
    assert len(store.list_human_review_actions_for_issue(source.issue_identifier)) == 2


def test_progress_exposes_triage_and_response_but_escapes_review_content(review_case):
    store, source, client, _ = review_case
    response = submit(client, source, comments='</pre><script>alert(1)</script>').json()
    action_id = response['human_review']['id']
    claimed = store.claim_human_review_action(action_id)
    assert store.start_human_review_run(action_id, response['run']['id'], claimed['claim_token']) is not None
    store.record_owned_human_review_triage(action_id, claimed['claim_token'], decision='code_changes',
        output=json.dumps({'decision': 'code_changes', 'feedback_items': [
            {'id': 'R1', 'comment': '<img src=x>', 'scope': 'in_scope', 'proposed_action': 'Fix empty input'},
        ]}))
    store.update_run(response['run']['id'], status='completed', final_message='R1 addressed: file.py:42', verification_status='passed')
    state = client.get(f'/api/v1/reviews/{action_id}').json()
    assert state['items'][0]['id'] == 'R1'
    assert state['verification'] == 'passed'
    assert 'file.py:42' in state['report']
    page = client.get(f'/reviews/{action_id}').text
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    assert '<img src=x>' not in page
    assert 'claim_token' not in page


def test_html_submission_and_replay_redirect_to_the_same_progress_page(review_case):
    _, source, client, _ = review_case
    responses = [client.post(f'/api/v1/runs/{source.id}/human-review',
        data={'reviewer_identity':'Operator', 'comments':'Fix the typo'},
        headers={'accept':'text/html'}, follow_redirects=False) for _ in range(2)]
    assert [response.status_code for response in responses] == [303, 303]
    assert responses[0].headers['location'] == responses[1].headers['location']
    assert responses[0].headers['location'].startswith('/reviews/')


@pytest.mark.parametrize('output', ['not json', '[]', '{"feedback_items":null}', '{"feedback_items":[null,42]}'])
def test_unstructured_legacy_triage_does_not_break_review_page(output):
    assert feedback_items({'triage_output': output}) == []


def test_scope_answer_resumes_the_same_review_on_done_jira_after_restart():
    async def run(root):
        workflow = load_completed_review_workflow(root)
        issue = completed_review_issue()
        store = Store(root / 'state.sqlite3')
        action, source, result, _ = create_completed_review_action(root, workflow, issue, store)
        runner = CompletedReviewCodexRunner('needs_human')
        polling = PollingOrchestrator(workflow, FakeJira(issue), store, codex_runner=runner)
        await dispatch_pending_human_resume(polling)
        blocked = store.get_run(result.id)
        assert blocked.status == 'blocked'
        assert runner.phases == ['triage']
        client = TestClient(create_app(workflow, store))
        url = f'/api/v1/reviews/{action["id"]}/clarifications'
        response = client.post(url, json={'response':'Use the existing helper; keep behavior unchanged.', 'expected_revision':0})
        assert response.status_code == 200, response.text
        assert client.post(url, json={'response':'Use the existing helper; keep behavior unchanged.', 'expected_revision':0}).status_code == 200
        assert client.post(url, json={'response':'A different answer from an old tab', 'expected_revision':0}).status_code == 409
        assert len(store.get_human_review_action(action['id'])['clarifications']) == 1
        assert store.get_human_review_action(action['id'])['comments'] == action['comments']

        class AnswerAwareRunner(CompletedReviewCodexRunner):
            async def run(self, prompt, *args, **kwargs):
                assert 'Use the existing helper; keep behavior unchanged.' in prompt
                if prompt.startswith('You are reviewing'):
                    assert 'Check every pasted human-review comment' in prompt
                return await super().run(prompt, *args, **kwargs)

        restarted = Store(store.db_path)
        runner = AnswerAwareRunner('code_changes')
        polling = PollingOrchestrator(workflow, FakeJira(issue), restarted, codex_runner=runner)
        await dispatch_pending_human_resume(polling)
        finished = restarted.get_run(result.id)
        assert finished.status == 'completed', finished.error
        assert runner.phases == ['triage', 'implementation', 'review']
        assert len(restarted.list_runs_for_issue(issue.identifier)) == 2
        assert 'Use the existing helper' in finished.human_input_context
        assert restarted.get_run(source.id).status == 'completed'
    with tempfile.TemporaryDirectory(prefix='symphony-review-test-') as folder:
        asyncio.run(run(Path(folder)))


def test_clarification_cannot_bypass_a_required_plan_change(review_case):
    store, source, client, _ = review_case
    response = submit(client, source).json()
    action = response['human_review']
    claim = store.claim_human_review_action(action['id'])
    store.start_human_review_run(action['id'], response['run']['id'], claim['claim_token'])
    store.record_owned_human_review_triage(action['id'], claim['claim_token'],
        decision='plan_changes_required', output='{"decision":"plan_changes_required"}')
    store.update_owned_human_review_run(action['id'], response['run']['id'], claim['claim_token'],
        status='blocked', blocked_phase='planning', error='A new approval is needed')
    result = client.post(f'/api/v1/reviews/{action["id"]}/clarifications', json={'response':'Just do it', 'expected_revision':0})
    assert result.status_code == 409
    assert store.get_run(response['run']['id']).status == 'blocked'
