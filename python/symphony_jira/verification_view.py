"""Compact verification views; full evidence remains in workspace artifacts."""
import html
import json
from pathlib import Path
from .human_review import HumanReviewContextError, capture_workspace_diff, read_frozen_text_artifact
from .plan_spec import PlanSpec
from .verification_insights import excerpt
from .presentation import PRESENTATION_CSS

esc = lambda value: html.escape(str(value or ''))


def phase_excerpt(content):
    try:
        value = json.loads(content or '')
    except (ValueError, TypeError):
        return excerpt(content or 'No outcome recorded yet.')
    if isinstance(value, dict):
        pieces = [str(value[key]) for key in ('summary', 'decision', 'status', 'reason') if value.get(key)]
        for key in ('findings', 'feedback_items', 'requirements', 'tests', 'targets', 'skipped'):
            if isinstance(value.get(key), list):
                pieces.append(f"{len(value[key])} {key.replace('_', ' ')}")
        return excerpt('. '.join(pieces) or 'Structured artifact saved; open the file for details.')
    return excerpt('Structured artifact saved; open the file for details.')


def verification_page(run, store, workflow, original_content):
    attempts = store.list_verification_attempts(run.id)
    sections = [f'<h1>{esc(run.issue_identifier)} · Verification</h1>',
                f'<p>Original run: <strong>{esc(run.verification_status or "not recorded")}</strong></p>']
    reports = []
    if original_content:
        try:
            reports.append(('Original verification', json.loads(original_content), run.verification_output_path))
        except ValueError:
            sections.append(f'<p>{esc(phase_excerpt(original_content))}</p><code>{esc(run.verification_output_path)}</code>')
    for attempt in reversed(attempts):
        path = attempt.get('report_path')
        if path:
            try:
                content = read_frozen_text_artifact(Path(run.workspace_path), path, label='verification attempt', required=True)
                reports.append((f"Verification {attempt['created_at']} ({attempt['mode']})", json.loads(content), str(Path(run.workspace_path) / path)))
            except (ValueError, OSError, HumanReviewContextError):
                sections.append('<p>Saved attempt evidence unavailable.</p>')
        else:
            sections.append(f'<p>Latest attempt {esc(attempt["created_at"])}: {esc(attempt["status"])}. {esc(excerpt(attempt.get("error") or ""))}</p>')
    current_hash = None
    try:
        plan = PlanSpec.model_validate_json(read_frozen_text_artifact(Path(run.workspace_path),
            workflow.config.codex.output_plan_file, label='verification plan', required=True))
        if plan.content_hash() == run.plan_spec_hash:
            current_hash = capture_workspace_diff(Path(run.workspace_path), plan,
                managed_repositories=tuple(str(p) for p in workflow.config.workspace.managed_repositories)).content_hash
    except (ValueError, OSError, HumanReviewContextError):
        pass
    for label, report, path in reversed(reports):
        if not isinstance(report, dict):
            continue
        binding = report.get('workspace_diff_hash')
        freshness = ('Matches current code' if binding and binding == current_hash else
                     'Stale: code has changed' if binding and current_hash else 'Current code match could not be verified')
        sections.append(f'<section><h2>{esc(label)}</h2><p><strong>{esc(report.get("status"))}</strong> · {esc(freshness)}</p>')
        results = report.get('results', [])
        sections.append(f'<p>{sum(r.get("status") == "passed" for r in results)} suites passed · '
                        f'{sum(r.get("status") != "passed" for r in results)} unsuccessful · {len(report.get("skipped", []))} coverage gaps.</p>')
        sections.append('<ul>')
        for result in results:
            diagnostic = result.get('diagnostic') or {}
            sections.append(f'<li><strong>{esc(result.get("repository"))}/{esc(result.get("suite"))}: {esc(result.get("status"))}</strong> '
                            f'{esc(excerpt(diagnostic.get("summary") or result.get("reason") or ""))}'
                            f'<br>{esc(excerpt(diagnostic.get("next_step") or ""))}</li>')
            selectors = ', '.join(result.get('test_args') or []) or result.get('test') or ''
            if selectors:
                sections.append(f'<li>Tests: <code>{esc(excerpt(selectors, 400))}</code></li>')
            if result.get('log_path'):
                sections.append(f'<li>Suite log: <code>{esc(Path(run.workspace_path) / result["log_path"])}</code></li>')
        for skipped in report.get('skipped', []):
            sections.append(f'<li>Uncovered · {esc(skipped.get("repository"))}: {esc(excerpt(skipped.get("reason")))}</li>')
        sections.append('</ul>')
        diagnostic = report.get('diagnostic') or {}
        if diagnostic.get('summary') and report.get('status') != 'passed':
            sections.append(f'<p>Run diagnostic: {esc(excerpt(diagnostic["summary"]))}</p>')
        if report.get('readiness'):
            sections.append('<h3>Runtime readiness</h3><ul>')
            for item in report['readiness']:
                problems = [check['name'] + ': ' + check['detail'] for check in item.get('checks', []) if check['status'] != 'ready']
                sections.append(f'<li>{esc(item["repository"])}/{esc(item["suite"])}: {esc(item["status"])}. {esc(excerpt("; ".join(problems)))}</li>')
            sections.append('</ul>')
        coverage = report.get('coverage', [])
        sections.append('<h3>Acceptance criteria</h3><p>Mappings describe selected assertions; passing tests do not prove untested scenarios.</p>')
        if not coverage:
            sections.append('<p>No criterion mapping was recorded. Run Verify current code to generate one.</p>')
        else:
            uncovered = sum(row['status'] == 'uncovered' for row in coverage)
            sections.append(f'<p>{len(coverage)} criteria mapped or assessed · {uncovered} uncovered.</p>')
            sections.append('<table><thead><tr><th>Criterion</th><th>Outcome</th><th>Selected tests</th></tr></thead><tbody>')
            for row in coverage[:20]:
                sections.append(f'<tr><td>{esc(row["criterion_id"])} · {esc(excerpt(row["statement"], 200))}</td>'
                                f'<td>{esc(row["status"])}</td><td>{esc(excerpt("; ".join(row["tests"]), 350))}</td></tr>')
            sections.append('</tbody></table>')
            if len(coverage) > 20:
                sections.append('<p>Showing the first 20 criteria; the full report contains every criterion.</p>')
        sections.append(f'<p>Full report: <code>{esc(path)}</code></p>')
        for key in ('request_path', 'log_path'):
            if report.get(key):
                sections.append(f'<p>{esc(key.replace("_", " "))}: <code>{esc(Path(run.workspace_path) / report[key])}</code></p>')
        sections.append('</section>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Verification</title>'
            f'<style>{PRESENTATION_CSS} section{{margin:1.5rem 0;padding:1rem;border:1px solid #ddd;border-radius:8px}} td,th{{padding:.5rem;text-align:left}} code{{overflow-wrap:anywhere}}</style><body class="document-page">'
            '<nav><a href="/">All cases</a> · '
            f'<a href="/runs/{esc(run.id)}/recordings">Recordings</a></nav>' + ''.join(sections) + '</body></html>')
