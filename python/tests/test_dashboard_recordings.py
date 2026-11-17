import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from symphony_jira.dashboard import build_state, create_app, render_dashboard_html
from symphony_jira.human_review import write_frozen_text_artifact
from symphony_jira.models import Issue
from symphony_jira.store import Store
from symphony_jira.workflow import load_workflow
from test_dashboard import write_workflow


def request(app, path, *, method='GET', headers=None):
    """Direct ASGI requests avoid the sandbox's network/test-client portal issues."""
    async def perform():
        sent = []
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                 'http_version': '1.1', 'method': method, 'scheme': 'http',
                 'path': path, 'raw_path': path.encode(), 'query_string': b'',
                 'root_path': '', 'server': ('testserver', 80), 'client': ('127.0.0.1', 1234),
                 'headers': [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()]}

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            sent.append(message)

        await asyncio.wait_for(app(scope, receive, send), timeout=10)
        start = next(item for item in sent if item['type'] == 'http.response.start')
        body = b''.join(item.get('body', b'') for item in sent if item['type'] == 'http.response.body')
        return SimpleNamespace(status_code=start['status'], content=body,
                               text=body.decode('utf-8', errors='replace'),
                               headers={key.decode(): value.decode() for key, value in start['headers']})
    return asyncio.run(perform())


@pytest.fixture
def recorded_run(tmp_path):
    workflow = load_workflow(write_workflow(tmp_path), environ={'TEST_JIRA_TOKEN': 'token'})
    store = Store(tmp_path/'db.sqlite3')
    workspace = tmp_path/'workspace'; workspace.mkdir()
    issue = Issue(id='1', identifier='T-1', title='Home page', status='To Do', url='u')
    run = store.create_run(issue, workspace, branch_name=None)
    recording_id, capture_id = 'a' * 32, 'b' * 32
    content = b'\x1aE\xdf\xa3' + bytes(range(256)) * 4
    relative = f'.symphony/recordings/{capture_id}/{recording_id}.webm'
    video = workspace/relative
    video.parent.mkdir(parents=True); video.write_bytes(content)
    metadata = {'id': recording_id, 'path': relative, 'sha256': hashlib.sha256(content).hexdigest(),
                'size_bytes': len(content), 'title': 'Home <script>alert(1)</script>',
                'spec': 'playwright/integrated/live_home.spec.js', 'outcome': 'passed',
                'recorded_at': '2026-11-17T00:00:00Z'}
    report = {'status': 'partial', 'workspace_diff_hash': 'd' * 64, 'request_hash': 'e' * 64,
              'results': [], 'skipped': [{'repository': 'foyr2', 'reason': 'Other flow not covered'}],
              'recordings': [metadata]}
    report_path = '.symphony/verification/results.json'

    def save():
        write_frozen_text_artifact(workspace, report_path, json.dumps(report), label='test verification')

    save()
    run = store.update_run(run.id, status='completed', verification_status='partial',
                           verification_output_path=str(workspace/report_path))
    return SimpleNamespace(app=create_app(workflow, store), store=store, workflow=workflow,
                           run=run, workspace=workspace, metadata=metadata, report=report,
                           save=save, video=video, content=content,
                           video_url=f'/api/v1/runs/{run.id}/recordings/{recording_id}')


def test_recording_gallery_is_historical_escaped_and_linked_from_results_and_row(recorded_run):
    saved = recorded_run
    page = request(saved.app, f'/runs/{saved.run.id}/recordings')
    assert page.status_code == 200
    assert '<video controls preload="metadata"' in page.text
    assert saved.video_url in page.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page.text
    assert '<script>alert(1)</script>' not in page.text
    assert 'Original run verification: <strong>partial</strong>' in page.text
    assert 'Passed' in page.text and '2026-11-17' in page.text
    assert 'd' * 64 in page.text
    assert 'does not establish coverage of other behavior' in page.text
    assert 'current workspace changes are not checked during playback' in page.text
    results = request(saved.app, f'/runs/{saved.run.id}/verification')
    assert results.status_code == 200 and 'Watch verification video' in results.text
    dashboard = render_dashboard_html(build_state(saved.workflow, saved.store))
    assert f'/runs/{saved.run.id}/recordings' in dashboard
    assert 'Watch verification video' in dashboard


def test_video_stream_supports_verified_full_head_and_byte_ranges(recorded_run):
    saved = recorded_run
    response = request(saved.app, saved.video_url)
    assert response.status_code == 200 and response.content == saved.content
    assert response.headers['content-type'] == 'video/webm'
    assert response.headers['accept-ranges'] == 'bytes'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    head = request(saved.app, saved.video_url, method='HEAD')
    assert head.status_code == 200 and head.content == b''
    assert int(head.headers['content-length']) == len(saved.content)
    for header, expected in [('bytes=4-9', saved.content[4:10]),
                             ('bytes=-8', saved.content[-8:]),
                             ('bytes=1000-', saved.content[1000:])]:
        part = request(saved.app, saved.video_url, headers={'Range': header})
        assert part.status_code == 206 and part.content == expected
        assert int(part.headers['content-length']) == len(expected)
        assert part.headers['content-range'].endswith('/' + str(len(saved.content)))
    mismatched = request(saved.app, saved.video_url, headers={'Range': 'bytes=4-9', 'If-Range': '"different"'})
    assert mismatched.status_code == 200 and mismatched.content == saved.content


@pytest.mark.parametrize('header', ['bytes=99999-', 'bytes=8-2', 'bytes=-0', 'bytes=',
                                    'items=0-1', 'bytes=0-1,4-8'])
def test_invalid_video_range_is_416(recorded_run, header):
    saved = recorded_run
    result = request(saved.app, saved.video_url, headers={'Range': header})
    assert result.status_code == 416
    assert result.headers['content-range'] == f'bytes */{len(saved.content)}'


def test_recording_id_cannot_access_another_runs_video(recorded_run):
    saved = recorded_run
    issue = Issue(id='2', identifier='T-2', title='Other', status='To Do', url='u')
    other = saved.store.create_run(issue, saved.workspace, branch_name=None)
    result = request(saved.app, f'/api/v1/runs/{other.id}/recordings/{saved.metadata["id"]}')
    assert result.status_code == 404
    assert request(saved.app, saved.video_url[:-32] + 'f' * 32).status_code == 404
    assert request(saved.app, saved.video_url[:-32] + 'invalid').status_code == 404


@pytest.mark.parametrize('mutation', ['missing', 'hash', 'symlink', 'path'])
def test_unsafe_missing_or_tampered_recording_cannot_be_served(recorded_run, tmp_path, mutation):
    saved = recorded_run
    if mutation == 'missing':
        saved.video.unlink()
    elif mutation == 'hash':
        saved.video.write_bytes(b'changed' + saved.content[7:])
    elif mutation == 'symlink':
        outside = tmp_path/'outside.webm'; outside.write_bytes(saved.content)
        saved.video.unlink(); saved.video.symlink_to(outside)
    else:
        saved.metadata['path'] = '../outside.webm'; saved.save()
    result = request(saved.app, saved.video_url)
    assert result.status_code == 409
    assert result.content != saved.content


def test_verified_snapshot_survives_source_change_before_stream(recorded_run, monkeypatch):
    from symphony_jira import dashboard
    saved = recorded_run
    original = dashboard.open_verified_recording

    def change_after_open(workspace, metadata):
        snapshot = original(workspace, metadata)
        saved.video.write_bytes(b'changed' + saved.content[7:])
        return snapshot

    monkeypatch.setattr(dashboard, 'open_verified_recording', change_after_open)
    result = request(saved.app, saved.video_url)
    assert result.status_code == 200 and result.content == saved.content


def test_stale_and_uncovered_outcomes_are_not_shown_as_passed(recorded_run):
    saved = recorded_run
    saved.report['status'] = 'stale'; saved.metadata['outcome'] = 'skipped'; saved.save()
    result = request(saved.app, f'/runs/{saved.run.id}/recordings')
    assert result.status_code == 200
    assert 'Stale verification' in result.text and 'Not covered' in result.text
    assert 'not evidence for the current workspace' in result.text


def test_legacy_report_without_recordings_preserves_readable_results(recorded_run):
    saved = recorded_run
    saved.report.pop('recordings'); saved.save()
    result = request(saved.app, f'/runs/{saved.run.id}/verification')
    assert result.status_code == 200 and 'Watch verification video' not in result.text
    gallery = request(saved.app, f'/runs/{saved.run.id}/recordings')
    assert gallery.status_code == 200 and 'No verification video' in gallery.text
    assert request(saved.app, saved.video_url).status_code == 404


def attach_rerun(saved):
    metadata = {**saved.metadata, 'id': 'c' * 32,
                'path': '.symphony/recordings/' + 'f' * 32 + '/' + 'c' * 32 + '.webm',
                'title': 'Recorded Home verification rerun'}
    path = saved.workspace/metadata['path']; path.parent.mkdir(parents=True); path.write_bytes(saved.content)
    report = {**saved.report, 'status': 'passed', 'workspace_diff_hash': '1' * 64,
              'request_hash': '2' * 64, 'skipped': [], 'recordings': [metadata]}
    relative = '.symphony/verification/manual-rerun.json'
    write_frozen_text_artifact(saved.workspace, relative, json.dumps(report), label='test supplemental recording')
    saved.store.add_log(saved.run.id, 'verification_recording', json.dumps({
        'report_path': relative, 'label': 'Recorded verification rerun'}))
    return metadata


def test_supplemental_rerun_has_own_binding_without_replacing_original_result(recorded_run):
    saved = recorded_run
    metadata = attach_rerun(saved)
    page = request(saved.app, f'/runs/{saved.run.id}/recordings')
    assert page.status_code == 200
    assert 'Original run verification: <strong>partial</strong>' in page.text
    assert 'Recorded verification rerun' in page.text
    assert 'Report status: <strong>passed</strong>' in page.text
    assert 'd' * 64 in page.text and '1' * 64 in page.text and '2' * 64 in page.text
    assert page.text.count('<video ') == 2
    assert '<details><summary>Details</summary>' in page.text
    assert saved.store.get_run(saved.run.id).verification_status == 'partial'
    response = request(saved.app, f'/api/v1/runs/{saved.run.id}/recordings/{metadata["id"]}', headers={'Range': 'bytes=0-3'})
    assert response.status_code == 206 and response.content == saved.content[:4]


@pytest.mark.parametrize('path', ['/tmp/report.json', '../report.json', '.symphony/verification/../../report.json',
                                  '.symphony/recordings/report.json'])
def test_supplemental_report_reference_cannot_escape_verification_directory(recorded_run, path):
    saved = recorded_run
    saved.store.add_log(saved.run.id, 'verification_recording', json.dumps({'report_path': path}))
    page = request(saved.app, f'/runs/{saved.run.id}/recordings')
    assert page.status_code == 409
    assert request(saved.app, saved.video_url).status_code == 409


def test_recording_rerun_log_is_scoped_to_its_run(recorded_run):
    saved = recorded_run
    metadata = attach_rerun(saved)
    issue = Issue(id='2', identifier='T-2', title='Other', status='To Do', url='u')
    other = saved.store.create_run(issue, saved.workspace, branch_name=None)
    saved.store.update_run(other.id, verification_output_path=saved.run.verification_output_path)
    response = request(saved.app, f'/api/v1/runs/{other.id}/recordings/{metadata["id"]}')
    assert response.status_code == 404


def test_supplemental_video_link_appears_when_original_report_has_no_video(recorded_run):
    saved = recorded_run
    saved.report.pop('recordings'); saved.save()
    attach_rerun(saved)
    page = request(saved.app, f'/runs/{saved.run.id}/verification')
    assert page.status_code == 200 and 'Watch verification video' in page.text
    dashboard = render_dashboard_html(build_state(saved.workflow, saved.store))
    assert f'/runs/{saved.run.id}/recordings' in dashboard


def test_missing_supplemental_artifact_does_not_hide_original_results(recorded_run):
    saved = recorded_run
    saved.store.add_log(saved.run.id, 'verification_recording', json.dumps({
        'report_path': '.symphony/verification/missing-rerun.json'}))
    result = request(saved.app, f'/runs/{saved.run.id}/verification')
    assert result.status_code == 200
    assert 'Watch verification video' not in result.text
    assert request(saved.app, f'/runs/{saved.run.id}/recordings').status_code == 409
