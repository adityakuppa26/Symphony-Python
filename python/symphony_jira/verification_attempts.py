"""Verification-only work, dispatched by the normal polling worker."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path

from .development_verification import DevelopmentVerificationRequest, read_development_verification_request, validate_verification_report
from .human_review import capture_workspace_diff, issue_from_frozen_snapshot, read_frozen_text_artifact, write_frozen_text_artifact
from .plan_spec import PlanSpec
from .verification_insights import coverage_summary, failure_summary
from .workspace import WorkspaceInfo


async def run_verification_attempt(single, attempt: dict) -> None:
    store, config = single.store, single.config
    source = store.get_run(attempt['run_id'])
    report_path = None
    try:
        if source is None or source.status != 'completed':
            raise ValueError('The source implementation is no longer completed')
        latest = store.latest_run_for_issue(source.issue_identifier)
        if latest is None or latest.id != source.id:
            raise ValueError('A newer implementation exists; verify that run')
        workspace = Path(source.workspace_path)
        text = read_frozen_text_artifact(workspace, config.codex.output_plan_file, label='verification plan', required=True)
        plan = PlanSpec.model_validate_json(text)
        if plan.content_hash() != source.plan_spec_hash:
            raise ValueError('The saved plan no longer matches the completed implementation')
        snapshot = store.get_requirements_snapshot(source.issue_identifier, source.issue_fingerprint)
        if snapshot is None:
            raise ValueError('The frozen requirements snapshot is unavailable')
        issue = issue_from_frozen_snapshot(source, snapshot)
        if plan.requirements_snapshot_hash != source.issue_fingerprint:
            raise ValueError('Plan requirements do not match the source run')
        before = capture_workspace_diff(workspace, plan, managed_repositories=tuple(str(p) for p in config.workspace.managed_repositories))
        events = store.list_codex_events(source.id)
        offset = max((event.sequence for event in events), default=0)
        request, _, error = await single._select_development_tests(
            issue=issue, run_id=source.id, workspace_path=workspace, plan=plan, plan_message=text,
            implementation_message=source.final_message, event_offset=offset)
        if error or request is None:
            raise ValueError(error or 'No test selection was produced')
        if request.workspace_diff_hash != before.content_hash:
            raise ValueError('Code changed while verification was being selected')
        if attempt['mode'] == 'retry':
            previous = next((item for item in store.list_verification_attempts(source.id)
                             if item['id'] != attempt['id'] and item.get('report_path')), None)
            prior_path = previous['report_path'] if previous else source.verification_output_path
            if not prior_path:
                raise ValueError('There is no previous verification to retry')
            path = Path(prior_path)
            relative = str(path.relative_to(workspace)) if path.is_absolute() else str(path)
            prior = json.loads(read_frozen_text_artifact(workspace, relative, label='previous verification', required=True))
            request = retry_request(request, prior)
        prefix = f".symphony/verification/{attempt['id']}"
        request_path = prefix + '-request.json'
        write_frozen_text_artifact(workspace, request_path, request.model_dump_json(indent=2), label='verification attempt request')
        _, digest = read_development_verification_request(workspace, request_path)
        write_frozen_text_artifact(workspace, config.codex.output_development_verification_result_file, '{}', label='pending verification')
        info = WorkspaceInfo(source.issue_identifier, workspace, False, source.branch_name)
        result = await single._run_verification_hook('verify', config.hooks.verify, workspace,
            hook_context=single.hook_context(issue, info, affected_repositories=tuple(plan.affected_surface.repositories),
                                            verification_request_path=request_path, verification_request_hash=digest))
        value = json.loads(read_frozen_text_artifact(workspace, config.codex.output_development_verification_result_file,
                                                   label='verification result', required=True))
        report = validate_verification_report(value, request, command_succeeded=result.succeeded)
        report['coverage'] = coverage_summary(plan, request, report['results'])
        # A log excerpt is run-level evidence; do not attribute it to a specific suite.
        report['diagnostic'] = failure_summary({'status': report['status']}, single.redact(result.output) or '')
        report['log_path'] = prefix + '.log'
        write_frozen_text_artifact(workspace, report['log_path'], single.redact(result.output) or '', label='verification attempt log')
        report['request_path'] = request_path
        report_path = prefix + '.json'
        write_frozen_text_artifact(workspace, report_path, json.dumps(report, indent=2), label='verification attempt report')
        store.finish_verification(attempt['id'], report['status'], report_path=report_path)
        if report.get('recordings'):
            store.add_log(source.id, 'verification_recording', json.dumps({'report_path': report_path, 'label': 'Verification attempt'}))
    except asyncio.CancelledError:
        store.finish_verification(attempt['id'], 'interrupted', error='Worker stopped. Queue a new verification attempt.')
        raise
    except Exception as exc:
        store.finish_verification(attempt['id'], 'environment_error', report_path=report_path,
                                  error=single.redact(str(exc)))


def retry_request(request: DevelopmentVerificationRequest, prior: dict) -> DevelopmentVerificationRequest:
    if prior.get('workspace_diff_hash') != request.workspace_diff_hash:
        raise ValueError('Code changed since the previous result. Use Verify current code.')
    failed = {(r['repository'], r['suite']) for r in prior.get('results', [])
              if r['status'] in {'failed', 'environment_error'}}
    if not failed:
        raise ValueError('No failed or environment-blocked suites to retry. Use Verify current code for coverage gaps.')
    targets = [t for t in request.targets if (t.repository, t.suite) in failed]
    if {(t.repository, t.suite) for t in targets} != failed:
        raise ValueError('Test selection changed; use Verify current code.')
    for target in targets:
        old = next(r for r in prior['results'] if (r['repository'], r['suite']) == (target.repository, target.suite))
        if old['test_args'] != target.test_args:
            raise ValueError('Test selectors changed; use Verify current code.')
    omitted = {t.repository for t in request.targets if (t.repository, t.suite) not in failed}
    skips = {s.repository: s.model_dump() for s in request.skipped}
    for repository in omitted:
        reason = 'Previously passing suites were not rerun; full current-code verification is still required.'
        if repository in skips:
            skips[repository]['reason'] += ' ' + reason
        else:
            skips[repository] = {'repository': repository, 'reason': reason}
    return DevelopmentVerificationRequest.model_validate({**request.model_dump(), 'targets': [t.model_dump() for t in targets], 'skipped': list(skips.values())})
