from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

import pytest

from symphony_jira.recordings import (
    MAX_MANIFEST_BYTES, MAX_RECORDING_BYTES, Recording, RecordingError,
    WEBM_SIGNATURE, create_recording_capture, load_recording_manifest,
    open_verified_recording,
)
from symphony_jira.development_verification import (
    DevelopmentVerificationRequest, execute_request, validate_verification_report,
)

SPEC = 'playwright/integrated/live_home.spec.js'
VIDEO = WEBM_SIGNATURE + b'verified video fixture'


def recording(workspace, *, capture=None, content=VIDEO, outcome='passed'):
    capture = capture or create_recording_capture(workspace)
    video_id = uuid.uuid4().hex
    path = capture/(video_id + '.webm')
    path.write_bytes(content)
    metadata = {'id': video_id, 'path': path.relative_to(workspace).as_posix(),
                'sha256': hashlib.sha256(content).hexdigest(), 'size_bytes': len(content),
                'title': 'Home page displays project dates', 'spec': SPEC, 'outcome': outcome,
                'recorded_at': '2026-11-16T12:34:56.123Z'}
    return capture, path, metadata


def manifest(capture, rows):
    (capture/'manifest.json').write_text(json.dumps({'schema_version': '1.0', 'recordings': rows}))


def test_capture_is_private_and_never_reuses_an_existing_name(tmp_path, monkeypatch):
    identifier = uuid.uuid4()
    monkeypatch.setattr('symphony_jira.recordings.uuid.uuid4', lambda: identifier)
    capture = create_recording_capture(tmp_path)
    assert capture == tmp_path/'.symphony/recordings'/identifier.hex
    assert capture.stat().st_mode & 0o777 == 0o700
    with pytest.raises(RecordingError):
        create_recording_capture(tmp_path)


@pytest.mark.parametrize('field,value', [
    ('id', 'a' * 31), ('sha256', 'z' * 64), ('size_bytes', 0), ('size_bytes', True),
    ('size_bytes', MAX_RECORDING_BYTES + 1), ('title', 'x' * 501),
    ('path', '/tmp/other.webm'), ('path', '.symphony/recordings/../other.webm'),
    ('spec', 'playwright/integrated/../other.spec.js'), ('outcome', 'approved'),
    ('recorded_at', '2026-11-16T12:34:56'), ('recorded_at', 'not a date'),
])
def test_metadata_rejects_unsafe_or_unbounded_values(tmp_path, field, value):
    _, _, metadata = recording(tmp_path)
    metadata[field] = value
    with pytest.raises(ValueError):
        Recording.model_validate(metadata)


def test_manifest_loads_only_selected_verified_recordings(tmp_path):
    capture, _, passed = recording(tmp_path)
    _, _, failed = recording(tmp_path, capture=capture, outcome='failed')
    manifest(capture, [passed, failed])
    assert load_recording_manifest(tmp_path, capture, [SPEC]) == [passed, failed]


@pytest.mark.parametrize('case', ['other_capture', 'unselected_spec', 'duplicate', 'wrong_id', 'no_version', 'too_many'])
def test_manifest_cannot_relabel_or_import_other_evidence(tmp_path, case):
    capture, _, metadata = recording(tmp_path)
    rows = [metadata]
    if case == 'other_capture':
        capture = create_recording_capture(tmp_path)
    elif case == 'unselected_spec':
        metadata['spec'] = 'playwright/integrated/unselected.spec.js'
    elif case == 'duplicate':
        rows.append(metadata.copy())
    elif case == 'wrong_id':
        metadata['id'] = uuid.uuid4().hex
    elif case == 'too_many':
        rows = [metadata.copy() for _ in range(21)]
    manifest(capture, rows)
    if case == 'no_version':
        (capture/'manifest.json').write_text(json.dumps({'recordings': rows}))
    with pytest.raises(RecordingError):
        load_recording_manifest(tmp_path, capture, [SPEC])


def test_manifest_is_bounded_and_cannot_be_a_symlink(tmp_path):
    capture, _, metadata = recording(tmp_path)
    manifest(capture, [metadata])
    saved = capture/'manifest.json'; elsewhere = tmp_path/'saved.json'
    saved.rename(elsewhere); saved.symlink_to(elsewhere)
    with pytest.raises(RecordingError):
        load_recording_manifest(tmp_path, capture, [SPEC])
    saved.unlink(); saved.write_bytes(b' ' * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(RecordingError):
        load_recording_manifest(tmp_path, capture, [SPEC])


@pytest.mark.parametrize('component', ['.symphony', 'recordings', 'capture', 'video'])
def test_recording_symlinks_are_rejected_at_every_component(tmp_path, component):
    capture, path, metadata = recording(tmp_path)
    target = {'.symphony': tmp_path/'.symphony', 'recordings': tmp_path/'.symphony/recordings',
              'capture': capture, 'video': path}[component]
    moved = tmp_path/'moved'
    target.rename(moved); target.symlink_to(moved, target_is_directory=component != 'video')
    with pytest.raises(RecordingError):
        open_verified_recording(tmp_path, metadata)


def test_capture_allocation_rejects_symlinked_parent(tmp_path):
    outside = tmp_path/'outside'; outside.mkdir()
    (tmp_path/'.symphony').symlink_to(outside, target_is_directory=True)
    with pytest.raises(RecordingError):
        create_recording_capture(tmp_path)
    assert not list(outside.iterdir())


@pytest.mark.parametrize('change', ['same_size_tamper', 'size', 'signature', 'oversized', 'fifo'])
def test_recording_requires_regular_bounded_webm_bytes_matching_metadata(tmp_path, change):
    _, path, metadata = recording(tmp_path)
    if change == 'same_size_tamper':
        path.write_bytes(WEBM_SIGNATURE + b'x' * (len(VIDEO) - len(WEBM_SIGNATURE)))
    elif change == 'size':
        metadata['size_bytes'] += 1
    elif change == 'signature':
        content = b'<html>not a video</html>'
        path.write_bytes(content)
        metadata.update(size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    elif change == 'oversized':
        with path.open('wb') as source:
            source.truncate(MAX_RECORDING_BYTES + 1)
    elif change == 'fifo':
        path.unlink(); os.mkfifo(path)
    with pytest.raises(RecordingError):
        open_verified_recording(tmp_path, metadata)


def test_verified_snapshot_survives_source_mutation_and_path_replacement(tmp_path):
    _, path, metadata = recording(tmp_path)
    with open_verified_recording(tmp_path, metadata) as snapshot:
        path.write_bytes(b'tampered after verification')
        assert snapshot.read() == VIDEO
        path.unlink(); path.symlink_to(tmp_path/'missing')
        snapshot.seek(0)
        assert snapshot.read() == VIDEO


def test_in_place_mutation_during_hashing_is_rejected(tmp_path, monkeypatch):
    import symphony_jira.recordings as module
    _, path, metadata = recording(tmp_path)
    original = hashlib.sha256
    class MutatingDigest:
        def __init__(self):
            self.digest = original()
        def update(self, content):
            self.digest.update(content)
            # The read bytes still match the saved SHA; inode metadata must catch this write.
            path.write_bytes(VIDEO)
            os.utime(path, ns=(1, 1))
        def hexdigest(self):
            return self.digest.hexdigest()
    monkeypatch.setattr(module.hashlib, 'sha256', MutatingDigest)
    with pytest.raises(RecordingError):
        open_verified_recording(tmp_path, metadata)


def controller_script(tmp_path, *, code=0, mode='valid'):
    path = tmp_path/'recording-controller.py'
    path.write_text('''import argparse,hashlib,json,pathlib,uuid
parser=argparse.ArgumentParser()
parser.add_argument('--workspace',type=pathlib.Path)
parser.add_argument('--timeout')
parser.add_argument('--recording-output',type=pathlib.Path)
parser.add_argument('specs',nargs='+')
args=parser.parse_args()
''' + f'code={code!r}\nmode={mode!r}\ncontent={VIDEO!r}\n' + '''
if mode != 'missing':
 rows=[]
 for spec in args.specs:
  video_id=uuid.uuid4().hex
  video=args.recording_output/(video_id+'.webm')
  video.write_bytes(content)
  record={'id':video_id,'path':video.relative_to(args.workspace).as_posix(),
          'sha256':hashlib.sha256(content).hexdigest(),'size_bytes':len(content),
          'title':'Home page','spec':spec,
          'outcome':'passed' if code==0 else 'failed','recorded_at':'2026-11-16T12:34:56Z'}
  if mode=='tampered': video.write_bytes(b'x'*len(content))
  rows.append(record)
 if mode=='empty': rows=[]
 if mode=='partial': rows=rows[:1]
 (args.recording_output/'manifest.json').write_text(json.dumps({'schema_version':'1.0','recordings':rows}))
raise SystemExit(code)
''')
    return path


def browser_request():
    return DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
        {'repository': 'foyr2', 'suite': 'playwright', 'test_args': [SPEC]}]})


@pytest.mark.parametrize('code,status', [(0, 'passed'), (1, 'failed'), (125, 'environment_error')])
def test_host_propagates_recordings_for_success_and_failure(tmp_path, code, status):
    request = browser_request()
    result = execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
                             playwright_entrypoint=controller_script(tmp_path, code=code),
                             result_path='.symphony/result.json')
    assert result == code
    report = validate_verification_report(json.loads((tmp_path/'.symphony/result.json').read_text()),
                                         request, command_succeeded=code == 0)
    assert report['status'] == status
    assert len(report['recordings']) == 1
    with open_verified_recording(tmp_path, report['recordings'][0]) as source:
        assert source.read() == VIDEO


@pytest.mark.parametrize('code,mode,expected', [
    (0, 'missing', 125), (0, 'empty', 125), (0, 'tampered', 125),
    (1, 'missing', 1), (1, 'tampered', 1),
])
def test_host_never_claims_success_without_valid_recordings(tmp_path, code, mode, expected):
    request = browser_request()
    assert execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
                           playwright_entrypoint=controller_script(tmp_path, code=code, mode=mode),
                           result_path='.symphony/result.json') == expected
    report = json.loads((tmp_path/'.symphony/result.json').read_text())
    assert report['recordings'] == []
    assert report['status'] == ('failed' if expected == 1 else 'environment_error')


def test_recordings_cannot_be_attached_to_other_specs_or_unbound_reports(tmp_path):
    request = browser_request()
    execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
                    playwright_entrypoint=controller_script(tmp_path), result_path='.symphony/result.json')
    original = json.loads((tmp_path/'.symphony/result.json').read_text())
    for field in ('request_hash', 'workspace_diff_hash'):
        report = dict(original); report[field] = 'a' * 64
        with pytest.raises(ValueError, match='not bound'):
            validate_verification_report(report, request, command_succeeded=True)
    report = json.loads(json.dumps(original))
    report['recordings'][0]['spec'] = 'playwright/integrated/another.spec.js'
    with pytest.raises(ValueError, match='selected Playwright spec'):
        validate_verification_report(report, request, command_succeeded=True)
    report = json.loads(json.dumps(original)); report['recordings'].append(report['recordings'][0].copy())
    with pytest.raises(ValueError, match='unique'):
        validate_verification_report(report, request, command_succeeded=True)
    # Historical reports may predate recording support.
    legacy = dict(original); legacy.pop('recordings')
    assert validate_verification_report(legacy, request, command_succeeded=True)['recordings'] == []


def test_valid_video_is_retained_but_cannot_make_drifted_code_pass(tmp_path):
    from symphony_jira.human_review import capture_workspace_diff
    from symphony_jira.plan_spec import PlanSpec
    from test_orchestrator import ensure_test_git_repository, valid_plan_spec_message
    ensure_test_git_repository(tmp_path, repository='cpm')
    sha = ensure_test_git_repository(tmp_path, repository='foyr2')
    message = valid_plan_spec_message('requirements_snapshot_hash "' + 'a' * 64 + '"',
                                     'Implement scoped behavior', baseline_sha=sha, repository='foyr2')
    plan = PlanSpec.model_validate_json(message)
    (tmp_path/'.symphony').mkdir()
    (tmp_path/'.symphony/codex-plan.md').write_text(message)
    before = capture_workspace_diff(tmp_path, plan, managed_repositories=('cpm', 'foyr2'))
    request = browser_request().model_copy(update={'workspace_diff_hash': before.content_hash,
                                                   'snapshot_repositories': ['cpm', 'foyr2']})
    controller = controller_script(tmp_path)
    controller.write_text(controller.read_text().replace('raise SystemExit(code)',
        "(args.workspace/'cpm/changed.py').write_text('changed during verification')\nraise SystemExit(code)"))
    assert execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
                           playwright_entrypoint=controller, result_path='.symphony/result.json') == 125
    report = validate_verification_report(json.loads((tmp_path/'.symphony/result.json').read_text()),
                                         request, command_succeeded=False)
    assert report['status'] == 'stale'
    assert report['workspace_diff_hash'] == before.content_hash
    assert len(report['recordings']) == 1
    with open_verified_recording(tmp_path, report['recordings'][0]) as video:
        assert video.read() == VIDEO



def test_reader_revalidates_preconstructed_metadata_before_opening(tmp_path):
    _, _, metadata = recording(tmp_path)
    metadata['id'] = uuid.uuid4().hex
    unchecked = Recording.model_construct(**metadata)
    with pytest.raises(RecordingError):
        open_verified_recording(tmp_path, unchecked)


def test_success_requires_video_for_each_selected_spec_but_keeps_available_evidence(tmp_path):
    request = browser_request()
    request.targets[0].test_args.append('playwright/integrated/other.spec.js')
    assert execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
                           playwright_entrypoint=controller_script(tmp_path, mode='partial'),
                           result_path='.symphony/result.json') == 125
    report = validate_verification_report(json.loads((tmp_path/'.symphony/result.json').read_text()),
                                         request, command_succeeded=False)
    assert report['status'] == 'environment_error'
    assert len(report['recordings']) == 1
    assert report['recordings'][0]['spec'] == SPEC
