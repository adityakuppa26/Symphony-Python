import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from symphony_jira.run_impact import capture_impact_snapshot, compare_snapshots, save_handoff_impact, impact_history
from symphony_jira.store import Store
from symphony_jira.models import Issue
from symphony_jira.config import CodexConfig
from symphony_jira.conversation import answer_question
from symphony_jira.dashboard import render_run_details
from symphony_jira.run_impact import compact_impact


def repository(root):
    checkout = root / 'app'
    checkout.mkdir()
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    (checkout / 'file.py').write_text('original\n')
    subprocess.run(['git', '-C', str(checkout), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-qm', 'initial'], check=True)
    return checkout


def config():
    return SimpleNamespace(workspace=SimpleNamespace(managed_repositories=['app']))


def new_run(store, root, attempt=1):
    issue = Issue(id='1', identifier='T-1', title='Title', status='Open', url='u')
    return store.create_run(issue, root, branch_name=None, attempt=attempt)


def handoff(store, run):
    return store.update_run(run.id, status='completed', final_message='Corrected the date filter.',
                            verification_status='partial')


def test_run_impact_separates_this_run_from_changes_since_handoff(tmp_path):
    repo = repository(tmp_path)
    store = Store(tmp_path / 'state.db')
    first = new_run(store, tmp_path)
    store.save_impact_start(first.id, capture_impact_snapshot(tmp_path, ['app']))
    (repo / 'file.py').write_text('first implementation\n')
    first = handoff(store, first)
    first_summary = save_handoff_impact(first, store, config())
    assert first_summary['this_run']['modified'] == ['app/file.py']
    assert 'First recorded handoff' in first_summary['comparison_summary']
    # Operator edits before the second run must not be attributed to that run.
    (repo / 'external.py').write_text('external change\n')
    second = new_run(store, tmp_path, 2)
    store.save_impact_start(second.id, capture_impact_snapshot(tmp_path, ['app']))
    (repo / 'file.py').write_text('corrected implementation\n')
    second = handoff(store, second)
    summary = save_handoff_impact(second, store, config())
    assert summary['previous_run_id'] == first.id
    assert summary['this_run']['added'] == []
    assert summary['this_run']['modified'] == ['app/file.py']
    assert summary['since_previous_handoff']['added'] == ['app/external.py']
    assert json.loads(Path(summary['artifact_path']).read_text()) == summary
    # Later edits never rewrite the handoff.
    (repo / 'file.py').write_text('later edit\n')
    assert save_handoff_impact(second, store, config()) == summary
    assert len(impact_history(store, second)) == 2
    assert len(impact_history(store, first)) == 1


def test_snapshot_tracks_deletions_modes_and_untracked_files(tmp_path):
    repo = repository(tmp_path)
    start = capture_impact_snapshot(tmp_path, ['app'])
    (repo / 'file.py').unlink()
    (repo / 'new.py').write_text('new\n')
    end = capture_impact_snapshot(tmp_path, ['app'])
    delta = compare_snapshots(start, end)
    assert delta['deleted'] == ['app/file.py']
    assert delta['added'] == ['app/new.py']
    (repo / 'new.py').chmod(0o755)
    delta = compare_snapshots(end, capture_impact_snapshot(tmp_path, ['app']))
    assert delta['modified'] == ['app/new.py']


def test_unchanged_files_are_not_new_work_just_because_they_were_committed(tmp_path):
    repo = repository(tmp_path)
    (repo / 'file.py').write_text('already present\n')
    start = capture_impact_snapshot(tmp_path, ['app'])
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=t@example.com',
                    'commit', '-qm', 'record existing content'], check=True)
    delta = compare_snapshots(start, capture_impact_snapshot(tmp_path, ['app']))
    assert delta['modified'] == []
    assert delta['head_changes']


def test_missing_snapshots_are_explicit_not_claimed_as_no_changes(tmp_path):
    repository(tmp_path)
    store = Store(tmp_path / 'db')
    old = handoff(store, new_run(store, tmp_path))
    current = new_run(store, tmp_path, 2)
    store.save_impact_start(current.id, capture_impact_snapshot(tmp_path, ['app']))
    summary = save_handoff_impact(handoff(store, current), store, config())
    assert summary['previous_run_id'] == old.id
    assert summary['since_previous_handoff']['gaps']
    assert 'incomplete' in summary['comparison_summary']
    missing = capture_impact_snapshot(tmp_path, ['missing'])
    assert missing['gaps']
    assert compare_snapshots(missing, missing)['gaps']


def test_summary_ui_is_bounded_escaped_and_links_to_artifact(tmp_path):
    repository(tmp_path)
    store = Store(tmp_path / 'db')
    run = new_run(store, tmp_path)
    store.save_impact_start(run.id, capture_impact_snapshot(tmp_path, ['app']))
    run = store.update_run(run.id, status='completed', final_message='<script>bad()</script>' + ' detail' * 1000)
    summary = save_handoff_impact(run, store, config())
    page = render_run_details({'id': run.id, 'status': 'completed', 'workspace_path': str(tmp_path),
                               'handoff_impact': compact_impact(summary)}, '')
    assert 'This run’s impact' in page
    assert '<script>bad()' not in page
    assert '&lt;script&gt;' in page
    assert ' detail' * 200 not in page
    assert summary['artifact_path'] in page


def test_questions_receive_frozen_comparisons_without_tools(tmp_path):
    class Runner:
        async def run(self, prompt, workspace, config, **kwargs):
            self.prompt = prompt
            return SimpleNamespace(status='completed', final_message='Run 2 corrected the date filter.')
    runner = Runner()
    run = SimpleNamespace(issue_identifier='T-1', status='completed', blocked_phase=None,
                          error=None, final_message='Done', human_input_context='')
    answer = asyncio.run(answer_question(run=run, question='What changed in run 2?', plan='', config=CodexConfig(),
        runner=runner, impact_history=[{'attempt': 2, 'this_run_summary': '1 modified file.', 'reported_behavior': 'Corrected the date filter'}]))
    assert 'date filter' in answer
    assert '1 modified file.' in runner.prompt
    assert 'Do not use tools' in runner.prompt


def test_start_snapshot_is_preserved_on_resume(tmp_path):
    store = Store(tmp_path / 'db')
    run = new_run(store, tmp_path)
    store.save_impact_start(run.id, {'repositories': {}, 'gaps': ['original']})
    store.save_impact_start(run.id, {'repositories': {}, 'gaps': ['replacement']})
    assert store.get_run_impact(run.id)['start_snapshot']['gaps'] == ['original']
