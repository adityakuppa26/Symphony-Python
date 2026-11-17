"""Frozen, run-specific handoff evidence. Never infer old state from today's checkout."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .human_review import HumanReviewContextError, read_frozen_artifact_bytes, read_frozen_text_artifact, write_frozen_text_artifact
from .verification_insights import excerpt
from .verification_view import phase_excerpt


def capture_impact_snapshot(workspace: Path, repositories) -> dict:
    snapshot = {'repositories': {}, 'gaps': []}
    deadline = time.monotonic() + 60
    for repository in sorted(str(value) for value in repositories):
        try:
            root = workspace / repository
            root.resolve().relative_to(workspace.resolve())
            def git(*args):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Impact capture exceeded its time budget')
                result = subprocess.run(['git', '-C', str(root), *args], check=True,
                    capture_output=True, timeout=min(15, remaining), env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
                return result.stdout
            head = git('rev-parse', 'HEAD').decode().strip()
            names = sorted(set(git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')) - {''})
            if len(names) > 100000:
                raise ValueError('Repository exceeds the impact file limit')
            files = {}
            for name in names:
                if time.monotonic() > deadline:
                    raise ValueError('Impact capture exceeded its time budget')
                path = Path(name)
                if path.parts[0] in {'.symphony', '.git'}:
                    continue
                if path.is_absolute() or '..' in path.parts:
                    raise ValueError('Invalid repository file path')
                source = root / path
                if source.is_symlink():
                    # Link text is the versioned content; never follow its target.
                    source.parent.resolve().relative_to(root.resolve())
                    files[name] = {'hash': hashlib.sha256(os.readlink(source).encode()).hexdigest(), 'mode': 'symlink'}
                    continue
                content = read_frozen_artifact_bytes(root, name, label='impact source', max_bytes=32_000_000)
                if content is not None:
                    files[name] = {'hash': hashlib.sha256(content).hexdigest(),
                                   'mode': 'executable' if source.stat().st_mode & 0o111 else 'file'}
            if git('rev-parse', 'HEAD').decode().strip() != head:
                raise ValueError('Repository HEAD changed during impact capture')
            snapshot['repositories'][repository] = {'head': head, 'files': files}
        except (OSError, ValueError, subprocess.SubprocessError, HumanReviewContextError) as exc:
            snapshot['gaps'].append(f'{repository}: snapshot unavailable ({type(exc).__name__}).')
    if not snapshot['repositories'] and not snapshot['gaps']:
        snapshot['gaps'].append('No managed repository snapshots were available.')
    return snapshot


def compare_snapshots(before: dict | None, after: dict) -> dict:
    delta = {'added': [], 'modified': [], 'deleted': [], 'gaps': [], 'head_changes': []}
    if before is None:
        delta['gaps'] = ['No saved comparison snapshot exists.']
        return delta
    delta['gaps'] = list(dict.fromkeys(before.get('gaps', []) + after.get('gaps', [])))
    for repository in sorted(set(before['repositories']) | set(after['repositories'])):
        old, new = before['repositories'].get(repository), after['repositories'].get(repository)
        if old is None or new is None:
            delta['gaps'].append(f'{repository}: snapshots are missing on one side.')
            continue
        if old['head'] != new['head']:
            delta['head_changes'].append({'repository': repository, 'before': old['head'], 'after': new['head']})
        for name in sorted(set(old['files']) | set(new['files'])):
            kind = ('added' if name not in old['files'] else 'deleted' if name not in new['files']
                    else 'modified' if old['files'][name] != new['files'][name] else None)
            if kind:
                delta[kind].append(f'{repository}/{name}')
    return delta


def delta_description(delta: dict) -> str:
    counts = ', '.join(f'{len(delta[key])} {key}' for key in ('added', 'modified', 'deleted'))
    if delta['gaps']:
        return counts + ' files in captured repositories; comparison is incomplete.'
    if not any(delta[key] for key in ('added', 'modified', 'deleted')):
        return 'No net file-content or executable-mode changes.'
    return counts + ' files.'


def save_handoff_impact(run, store, config, *, redact=lambda text: text) -> dict:
    existing = store.get_run_impact(run.id)
    if existing and existing['summary']:
        return existing['summary']
    end = capture_impact_snapshot(Path(run.workspace_path), config.workspace.managed_repositories)
    if existing is None:
        store.save_impact_start(run.id, {'repositories': {}, 'gaps': ['Run-start snapshot was not recorded.']})
        existing = store.get_run_impact(run.id)
    previous = next((item for item in store.list_runs_for_issue(run.issue_identifier, limit=1000)
                     if item.status == 'completed' and item.started_at < run.started_at), None)
    previous_impact = store.get_run_impact(previous.id) if previous else None
    this_run = compare_snapshots(existing['start_snapshot'], end)
    since_handoff = compare_snapshots(previous_impact['end_snapshot'] if previous_impact else None, end)
    reported = phase_excerpt(run.final_message)
    for entry in store.list_phase_summaries(run.id):
        value = json.loads(entry['message'])
        if value.get('phase') in {'implementation', 'development_implementation'}:
            reported = value['summary']
    verification = {'status': run.verification_status or 'not recorded', 'passed_suites': 0, 'unsuccessful_suites': 0, 'gaps': []}
    if run.verification_output_path:
        try:
            path = Path(run.verification_output_path)
            relative = str(path.relative_to(Path(run.workspace_path))) if path.is_absolute() else str(path)
            report = json.loads(read_frozen_text_artifact(Path(run.workspace_path), relative, label='handoff verification', required=True))
            verification['passed_suites'] = sum(item['status'] == 'passed' for item in report.get('results', []))
            verification['unsuccessful_suites'] = sum(item['status'] != 'passed' for item in report.get('results', []))
            verification['gaps'] = [excerpt(item['reason'], 300) for item in report.get('skipped', [])]
            verification['unmapped_criteria'] = sum(item.get('status') == 'uncovered' for item in report.get('coverage', []))
        except (ValueError, OSError, HumanReviewContextError, KeyError, TypeError):
            verification['gaps'].append('Saved verification details could not be read.')
    artifact = f'.symphony/impacts/{run.id}.json'
    summary = {'run_id': run.id, 'attempt': run.attempt, 'issue_identifier': run.issue_identifier,
               'previous_run_id': previous.id if previous else None,
               'previous_attempt': previous.attempt if previous else None,
               'reported_behavior': excerpt(redact(reported) or '', 600),
               'this_run': this_run, 'since_previous_handoff': since_handoff,
               'this_run_summary': delta_description(this_run),
               'comparison_summary': delta_description(since_handoff) if previous else 'First recorded handoff; no previous completed run to compare.',
               'verification': verification, 'artifact_path': str(Path(run.workspace_path) / artifact),
               'verification_path': run.verification_output_path,
               'scope_note': 'Net changes in managed Git files, including non-ignored untracked files. Behavior is reported by implementation; changes present before this run are not attributed to it.'}
    write_frozen_text_artifact(Path(run.workspace_path), artifact, json.dumps(summary, indent=2), label='handoff impact')
    store.save_impact_end(run.id, end, summary)
    return summary


def compact_impact(summary: dict) -> dict:
    return {**{key: summary[key] for key in ('run_id', 'attempt', 'previous_run_id', 'previous_attempt',
             'reported_behavior', 'this_run_summary', 'comparison_summary', 'verification', 'artifact_path', 'scope_note')},
            'changed_files': [path for key in ('added', 'modified', 'deleted') for path in summary['this_run'][key]][:12]}


def impact_history(store, run) -> list[dict]:
    rows = []
    for item in store.list_runs_for_issue(run.issue_identifier, limit=1000):
        if item.started_at > run.started_at or item.status != 'completed':
            continue
        summary = store.get_impact_summary(item.id)
        rows.append(compact_impact(summary) if summary else
                    {'run_id': item.id, 'attempt': item.attempt, 'summary': 'No frozen impact summary was recorded for this older run.'})
        if len(rows) == 10:
            break
    return rows
