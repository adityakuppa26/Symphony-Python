from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from symphony_jira.development_verification import (
    DevelopmentVerificationRequest,
    execute_request,
    fixed_test_command,
    read_development_verification_request,
    validate_verification_report,
    verification_request_hash,
)


def test_request_is_strict_and_binds_exact_plan_repositories() -> None:
    request = DevelopmentVerificationRequest.model_validate(
        {
            "schema_version": "1.0",
            "targets": [
                {
                    "repository": "cpm",
                    "test_args": ["Test/unit/test_example.py::test_behavior"],
                },
                {
                    "repository": "foyr2",
                    "test_args": ["tests/test_example.py::test_behavior"],
                },
            ],
        }
    )

    request.validate_repositories(("cpm", "foyr2"))
    with pytest.raises(ValueError, match="cover exactly"):
        request.validate_repositories(("cpm",))
    with pytest.raises(ValidationError):
        DevelopmentVerificationRequest.model_validate(
            {
                "schema_version": "1.0",
                "targets": [
                    {"repository": "cpm", "test_args": ["tests"]},
                    {"repository": "cpm", "test_args": ["other"]},
                ],
            }
        )


def test_fixed_commands_do_not_accept_a_model_authored_executable() -> None:
    request = DevelopmentVerificationRequest.model_validate(
        {
            "schema_version": "1.0",
            "targets": [
                {
                    "repository": "cpm",
                    "test_args": ["Test/unit/test_example.py", "-k", "focused"],
                },
                {
                    "repository": "pi",
                    "test_args": ["src/test/unit/test_example.py"],
                },
            ],
        }
    )

    self_cpm, self_pi = request.targets
    with pytest.raises(ValueError, match="schema stack"):
        fixed_test_command(self_cpm)
    assert fixed_test_command(self_pi) == [
        "hatch",
        "run",
        "dev:pytest",
        "src/test/unit/test_example.py",
    ]


def test_executor_passes_literal_arguments_to_the_trusted_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        capture = root / "argv.json"
        entrypoint = root / "fake-test.py"
        entrypoint.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        entrypoint.chmod(0o700)
        request = DevelopmentVerificationRequest.model_validate(
            {
                "schema_version": "1.0",
                "targets": [
                    {
                        "repository": "pi",
                        "test_args": [
                            "Test/unit/test_example.py::test_behavior",
                            "-k",
                            "value with spaces;$(not-a-shell)",
                        ],
                    }
                ],
            }
        )

        status = execute_request(
            request,
            workspace_path=workspace,
            entrypoint=entrypoint,
        )

        assert status == 0
        assert json.loads(capture.read_text(encoding="utf-8")) == [
            "pi",
            str(workspace),
            "--",
            "hatch",
            "run",
            "dev:pytest",
            "Test/unit/test_example.py::test_behavior",
            "-k",
            "value with spaces;$(not-a-shell)",
        ]


def test_request_reader_hashes_the_exact_validated_artifact() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        request_path = workspace / ".symphony" / "request.json"
        request_path.parent.mkdir(mode=0o700)
        request_path.write_text(
            '{"schema_version":"1.0","targets":['
            '{"repository":"foyr2","test_args":["tests/test_x.py"]}]}',
            encoding="utf-8",
        )

        request, digest = read_development_verification_request(
            workspace,
            ".symphony/request.json",
        )

        assert request.targets[0].repository == "foyr2"
        assert len(digest) == 64


def test_python_and_frontend_suites_can_cover_one_changed_repository() -> None:
    request = DevelopmentVerificationRequest.model_validate({
        "schema_version": "1.0", "targets": [
            {"repository": "foyr2", "suite": "python", "test_args": ["tests/views/api/test_home.py"]},
            {"repository": "foyr2", "suite": "karma", "test_args": ["cpm/home/homeTest.js"]},
        ],
    })
    request.validate_repositories(("foyr2",))
    with pytest.raises(ValueError, match="standalone stack"):
        fixed_test_command(request.targets[1])
    with pytest.raises(ValueError):
        DevelopmentVerificationRequest.model_validate({"schema_version": "1.0", "targets": [
            {"repository": "cpm", "suite": "karma", "test_args": ["../outside.js"]},
        ]})


def test_executor_keeps_pi_preparation_separate_from_foyr_stack(tmp_path):
    runtime = tmp_path/'runtime.sh'
    prepared = tmp_path/'prepared'
    runtime.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" > "{prepared}"\n')
    runtime.chmod(0o700)
    legacy = tmp_path/'test.sh'
    legacy.write_text('#!/bin/sh\nexit 1\n'); legacy.chmod(0o700)
    foyr = tmp_path/'foyr.py'
    foyr.write_text('raise SystemExit(0)\n')
    request = DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[
        {'repository':'pi','test_args':['tests/test_x.py']},
        {'repository':'foyr2','test_args':['tests/test_y.py']},
    ]})
    assert execute_request(request, workspace_path=tmp_path, entrypoint=legacy,
        runtime_entrypoint=runtime, foyr_entrypoint=foyr, result_path='.symphony/result.json') == 1
    assert prepared.read_text().strip() == f'up {tmp_path} pi'
    report=json.loads((tmp_path/'.symphony/result.json').read_text())
    assert [r['status'] for r in report['results']] == ['failed','passed']


def test_skipped_coverage_is_not_reported_as_passing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        request = DevelopmentVerificationRequest.model_validate({"schema_version": "1.0", "targets": [],
            "skipped": [{"repository": "foyr2", "reason": "No applicable frontend fixture exists."}]})
        request.validate_repositories(("foyr2",))
        execute_request(request, workspace_path=root, entrypoint=root / "must-not-run",
                        result_path=".symphony/result.json")
        report = json.loads((root / ".symphony/result.json").read_text())
        assert report["status"] == "not_run"
        assert report["results"] == []


@pytest.mark.parametrize('setup_code', [1, 2, 125])
def test_runtime_start_failure_is_an_environment_result(setup_code) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = root / "runtime.sh"
        runtime.write_text(f"#!/bin/sh\nexit {setup_code}\n")
        runtime.chmod(0o700)
        request = DevelopmentVerificationRequest.model_validate({"schema_version": "1.0", "targets": [
            {"repository": "pi", "test_args": ["tests/test_x.py"]},
        ]})
        assert execute_request(request, workspace_path=root, entrypoint=root / "must-not-run",
            runtime_entrypoint=runtime, result_path=".symphony/result.json") == 125
        report = json.loads((root / ".symphony/result.json").read_text())
        assert report["status"] == "environment_error"
        assert report["results"][0]["setup_returncode"] == setup_code


def test_code_drift_rejects_test_execution_and_records_stale_result() -> None:
    from symphony_jira.human_review import capture_workspace_diff
    from symphony_jira.plan_spec import PlanSpec
    from test_orchestrator import ensure_test_git_repository, valid_plan_spec_message

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sha = ensure_test_git_repository(root, repository="foyr2")
        message = valid_plan_spec_message('requirements_snapshot_hash "' + 'a' * 64 + '"',
            "Implement scoped behavior", baseline_sha=sha, repository="foyr2")
        plan = PlanSpec.model_validate_json(message)
        (root / ".symphony").mkdir()
        (root / ".symphony/codex-plan.md").write_text(message)
        before = capture_workspace_diff(root, plan)
        request = DevelopmentVerificationRequest.model_validate({"schema_version": "1.0",
            "targets": [{"repository": "foyr2", "test_args": ["tests/test_x.py"]}],
            "workspace_diff_hash": before.content_hash, "snapshot_repositories": ["foyr2"]})
        (root / "foyr2/changed.py").write_text("changed after selection\n")
        assert execute_request(request, workspace_path=root, entrypoint=root / "must-not-run",
            result_path=".symphony/result.json") == 125
        assert json.loads((root / ".symphony/result.json").read_text())["status"] == "stale"


def test_collection_only_requests_cannot_claim_verification() -> None:
    with pytest.raises(ValueError, match="execute tests"):
        DevelopmentVerificationRequest.model_validate({"schema_version": "1.0", "targets": [
            {"repository": "foyr2", "test_args": ["tests/test_x.py", "--collect-only"]},
        ]})


def test_browser_target_uses_standalone_runner_without_backend_runtime(tmp_path):
    runner=tmp_path/'foyr.py'; capture=tmp_path/'args.json'
    runner.write_text('import json,sys,pathlib\n'+f'pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n')
    request=DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[
        {'repository':'foyr2','suite':'frontend','test_args':['foyr/client_src/karma_test/cpm/home/homeTest.js']}]})
    assert execute_request(request,workspace_path=tmp_path,entrypoint=tmp_path/'must-not-run',
        runtime_entrypoint=tmp_path/'must-not-prepare',foyr_entrypoint=runner)==0
    args=json.loads(capture.read_text())
    assert args[:3]==['frontend','--source',str(tmp_path/'foyr2')]
    assert args[-1]=='foyr/client_src/karma_test/cpm/home/homeTest.js'



@pytest.mark.parametrize('repository,args', [
    ('cpm',['api/tests/test_x.py']), ('cpm',['Test/functional/test_x.py']),
    ('cpm',['Test/unit']), ('foyr2',['foyr/client_src/test_x.py']),
    ('foyr2',['tests/views/api']), ('foyr2',['-k','home']), ('foyr2',['tests/test_home.py','--setup-only']),
    ('cpm',['Test/unit/../functional/test_x.py']),
])
def test_python_requests_are_focused_and_routed_to_supported_families(repository,args):
    with pytest.raises(ValidationError):
        DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[{'repository':repository,'test_args':args}]})


def test_prepare_with_test_uses_one_entrypoint_call(tmp_path):
    entrypoint=tmp_path/'test.sh'; capture=tmp_path/'args'
    entrypoint.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{capture}"\n')
    entrypoint.chmod(0o700)
    request=DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[{'repository':'pi','test_args':['Test/unit/test_x.py']}]})
    assert execute_request(request, workspace_path=tmp_path, entrypoint=entrypoint,
                           runtime_entrypoint=tmp_path/'unused', prepare_with_test=True) == 0
    assert '--test-only' not in capture.read_text()
    assert 'Test/unit/test_x.py' in capture.read_text()


def passing_report():
    request = DevelopmentVerificationRequest.model_validate({
        'schema_version': '1.0',
        'workspace_diff_hash': 'a' * 64, 'snapshot_repositories': ['foyr2'],
        'targets': [{'repository': 'foyr2', 'test_args': ['tests/test_home.py']}],
    })
    return request, {
        'status': 'passed', 'workspace_diff_hash': request.workspace_diff_hash,
        'request_hash': verification_request_hash(request),
        'finished_at': '2026-11-10T12:00:00+00:00', 'skipped': [],
        'results': [{**request.targets[0].model_dump(), 'status': 'passed', 'returncode': 0}],
    }


@pytest.mark.parametrize('mutation', [
    lambda report: report.update(results=[]),
    lambda report: report.update(request_hash='b' * 64),
    lambda report: report.update(workspace_diff_hash='b' * 64),
    lambda report: report['results'][0].update(test_args=['tests/test_different.py']),
    lambda report: report['results'][0].update(returncode=1),
    lambda report: report['results'][0].update(returncode=True),
    lambda report: report['results'][0].update(status='failed'),
    lambda report: report['results'][0].update(setup_returncode=1),
    lambda report: report['results'].append(report['results'][0].copy()),
    lambda report: report.update(skipped=[{'repository': 'cpm', 'reason': 'Not run'}]),
    lambda report: report.update(status='partial'),
])
def test_false_or_unbound_verification_pass_is_rejected(mutation):
    request, report = passing_report()
    mutation(report)
    with pytest.raises(ValueError):
        validate_verification_report(report, request, command_succeeded=True)


@pytest.mark.parametrize('report', [None, [], {'status': 'passed'}])
def test_malformed_verification_evidence_is_rejected(report):
    request, _ = passing_report()
    with pytest.raises(ValueError):
        validate_verification_report(report, request, command_succeeded=True)


def test_valid_report_cannot_hide_a_failed_host_command():
    request, report = passing_report()
    with pytest.raises(ValueError, match='command exit'):
        validate_verification_report(report, request, command_succeeded=False)


@pytest.mark.parametrize('code,expected', [(0, 'passed'), (1, 'failed'), (125, 'environment_error')])
def test_executor_reports_validate_against_the_selected_commands(code, expected):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        entrypoint = root / 'test.sh'
        entrypoint.write_text(f'#!/bin/sh\nexit {code}\n')
        entrypoint.chmod(0o700)
        request = DevelopmentVerificationRequest.model_validate({
            'schema_version': '1.0', 'targets': [
                {'repository': 'pi', 'test_args': ['tests/test_home.py']},
            ],
        })
        result = execute_request(request, workspace_path=root, entrypoint=entrypoint,
                                 result_path='.symphony/results.json')
        report = validate_verification_report(json.loads((root / '.symphony/results.json').read_text()),
                                              request, command_succeeded=result == 0)
        assert report['status'] == expected


@pytest.mark.parametrize('suite,args', [
    ('functional', ['Test/functional/lib/test_x.py::test_case']),
    ('ibis', ['Test/flask/v3/test_home.py::TestHomeProjects']),
])
def test_cpm_schema_suites_are_scoped(suite, args):
    DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
        {'repository': 'cpm', 'suite': suite, 'test_args': args}]})
    with pytest.raises(ValueError):
        DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
            {'repository': 'foyr2', 'suite': suite, 'test_args': args}]})


@pytest.mark.parametrize('selector', ['Test/functional', 'Test/functional/lib', 'Test/api/test_x.py', '/Test/functional/test_x.py', 'Test/functional/../unit/test_x.py'])
def test_functional_selection_cannot_run_whole_suite_or_escape(selector):
    with pytest.raises(ValueError):
        DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
            {'repository': 'cpm', 'suite': 'functional', 'test_args': [selector]}]})


def test_schema_runner_bypasses_compost_preserves_filters_and_reports_failures(tmp_path):
    calls = tmp_path / 'calls.jsonl'
    runner = tmp_path / 'verify.py'
    runner.write_text('import json,sys,pathlib\n'
        f'with pathlib.Path({str(calls)!r}).open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
        'raise SystemExit(1 if sys.argv[1] == "unit" else 0)\n')
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
        {'repository': 'cpm', 'test_args': ['Test/unit/test_x.py', '-k', 'a or b']},
        {'repository': 'cpm', 'suite': 'functional', 'test_args': ['Test/functional/test_x.py']},
        {'repository': 'cpm', 'suite': 'ibis', 'test_args': ['Test/flask/v3/test_home.py', '-k', 'metadata']},
    ]})
    status = execute_request(request, workspace_path=tmp_path,
        entrypoint=tmp_path/'must-not-run', runtime_entrypoint=tmp_path/'must-not-prepare',
        cpm_entrypoint=runner, result_path='.symphony/result.json')
    assert status == 1
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [command[0] for command in commands] == ['unit', 'functional', 'ibis']
    assert all(command[1:3] == ['--source', str(tmp_path/'cpm')] for command in commands)
    assert commands[0][-2:] == ['--pytest-arg=-k', '--pytest-arg=a or b']
    report = validate_verification_report(json.loads((tmp_path/'.symphony/result.json').read_text()),
        request, command_succeeded=False)
    assert [item['status'] for item in report['results']] == ['failed', 'passed', 'passed']


def test_missing_schema_controller_is_environment_error_without_compost_fallback(tmp_path):
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
        {'repository': 'cpm', 'suite': 'functional', 'test_args': ['Test/functional/test_x.py']}]})
    assert execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
        cpm_entrypoint=tmp_path/'missing.py') == 125


def test_cpm_default_cannot_fall_back_to_compost_when_stack_is_missing(tmp_path, monkeypatch):
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
        {'repository': 'cpm', 'test_args': ['Test/unit/test_x.py']}]})
    monkeypatch.setattr(Path, 'is_file', lambda self: False)
    assert execute_request(request, workspace_path=tmp_path,
        entrypoint=tmp_path/'must-not-run', runtime_entrypoint=tmp_path/'must-not-prepare') == 125


def test_fixed_sanity_profile_is_not_a_symphony_verification_suite():
    with pytest.raises(ValueError):
        DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
            {'repository': 'cpm', 'suite': 'sanity', 'test_args': ['sanity']}]})


def test_changed_tests_are_mandatory_unfiltered_and_unrelated_tests_are_not_added(tmp_path):
    import subprocess
    from symphony_jira.development_verification import include_changed_test_files

    checkout = tmp_path / 'cpm'
    checkout.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(checkout), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    for name in ['Test/unit/test_existing.py', 'Test/unit/test_unrelated.py']:
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('def test_old(): pass\n')
    git('add', '.')
    git('commit', '-qm', 'baseline')
    baseline = git('rev-parse', 'HEAD')
    (checkout/'Test/unit/test_existing.py').write_text('def test_old(): pass\ndef test_added(): pass\n')
    staged = checkout/'Test/unit/test_new.py'
    staged.write_text('def test_new(): pass\n')
    git('add', '.')
    functional = checkout/'Test/functional/test_new.py'
    functional.parent.mkdir(parents=True)
    functional.write_text('def test_new(): pass\n')
    api = checkout/'Test/api/test_new.py'
    api.parent.mkdir(parents=True)
    api.write_text('def test_new_api(): pass\n')
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
        {'repository': 'cpm', 'test_args': ['Test/unit/test_existing.py::test_old', '-k', 'old', '-x', '--maxfail=1']}]})
    result = include_changed_test_files(request, workspace_path=tmp_path,
        repositories=('cpm',), baseline_shas={'cpm': baseline}, enabled_suites=['python', 'functional'])
    assert [(target.suite, target.test_args) for target in result.targets] == [
        ('python', ['Test/unit/test_existing.py', 'Test/unit/test_new.py']),
        ('functional', ['Test/functional/test_new.py']),
    ]
    assert 'Test/api/test_new.py' in result.skipped[0].reason
    assert 'sanity' not in result.model_dump_json()


def test_changed_tests_add_target_when_selector_reports_no_coverage(tmp_path):
    import subprocess
    from symphony_jira.development_verification import include_changed_test_files
    checkout = tmp_path/'cpm'
    checkout.mkdir()
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-qm', 'baseline'], check=True)
    test = checkout/'Test/unit/test_added.py'
    test.parent.mkdir(parents=True)
    test.write_text('def test_added(): pass\n')
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [],
        'skipped': [{'repository': 'cpm', 'reason': 'Other behavior has no existing coverage'}]})
    result = include_changed_test_files(request, workspace_path=tmp_path,
        repositories=('cpm',), baseline_shas={}, enabled_suites=['python', 'functional'])
    assert result.targets[0].test_args == ['Test/unit/test_added.py']
    assert result.skipped == request.skipped


@pytest.mark.parametrize('selector', [
    'foyr/client_src/karma_test/cpm/home/homeTest.js',
    'foyr/src/ts/widget/__tests__/widget.spec.ts',
    'foyr/src/ts/widget/__tests__/widget.spec.tsx',
])
def test_foyr_frontend_selectors_are_supported(selector):
    DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[
        {'repository':'foyr2','suite':'frontend','test_args':[selector]}]})


@pytest.mark.parametrize('selector', ['foyr/src', 'foyr/src/**/*.spec.ts', '../test.js',
    '/foyr/src/test.spec.ts', 'foyr/client_src/karma_test/../test.js', '--help'])
def test_foyr_frontend_requires_explicit_safe_files(selector):
    with pytest.raises(ValueError):
        DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[
            {'repository':'foyr2','suite':'frontend','test_args':[selector]}]})


def test_new_foyr_python_and_frontend_tests_are_all_mandatory(tmp_path):
    import subprocess
    from symphony_jira.development_verification import include_changed_test_files
    checkout=tmp_path/'foyr2';checkout.mkdir()
    subprocess.run(['git','init','-q',str(checkout)],check=True)
    subprocess.run(['git','-C',str(checkout),'-c','user.name=Test','-c','user.email=test@example.invalid',
        'commit','--allow-empty','-qm','baseline'],check=True)
    paths=['tests/test_added.py','foyr/client_src/karma_test/featureTest.js',
        'foyr/src/ts/component/__tests__/component.spec.ts','foyr/src/ts/component/__tests__/component.spec.tsx']
    for name in paths:
        p=checkout/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('test source')
    request=DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[],
        'skipped':[{'repository':'foyr2','reason':'No other relevant existing tests'}]})
    selected=include_changed_test_files(request,workspace_path=tmp_path,repositories=('foyr2',),
        baseline_shas={},enabled_suites=['python','frontend'])
    assert {t.suite: t.test_args for t in selected.targets} == {'python':[paths[0]],'frontend':paths[1:]}


def test_foyr_python_uses_private_stack_and_literal_filters(tmp_path):
    capture=tmp_path/'args.json'; runner=tmp_path/'foyr.py'
    runner.write_text('import json,sys,pathlib\n'+f'pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n')
    request=DevelopmentVerificationRequest.model_validate({'schema_version':'1.0','targets':[
        {'repository':'foyr2','test_args':['tests/test_feature.py','-k','case or other']}]})
    assert execute_request(request,workspace_path=tmp_path,entrypoint=tmp_path/'must-not-run',
        runtime_entrypoint=tmp_path/'must-not-prepare',foyr_entrypoint=runner)==0
    args=json.loads(capture.read_text())
    assert args[:3]==['python','--source',str(tmp_path/'foyr2')]
    assert args[-2:]==['--pytest-arg=-k','--pytest-arg=case or other']


@pytest.mark.parametrize('selector', [
    'Test/flask', 'Test/flask/v3', 'Test/api/test_home.py',
    'Test/flask/../functional/test_home.py', '/Test/flask/v3/test_home.py',
    'Test/flask/**/test_home.py', 'Test/flask/v3/test_home.py;touch /tmp/unsafe',
])
def test_ibis_rejects_unbounded_or_unsafe_selectors(selector):
    with pytest.raises(ValueError):
        DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
            {'repository': 'cpm', 'suite': 'ibis', 'test_args': [selector]}]})


@pytest.mark.parametrize('enabled', [True, False])
def test_changed_ibis_tests_are_selected_in_full_or_reported_uncovered(tmp_path, enabled):
    import subprocess
    from symphony_jira.development_verification import include_changed_test_files
    checkout = tmp_path/'cpm'
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-qm', 'baseline'], check=True)
    path = checkout/'Test/flask/v3/test_home.py'
    path.parent.mkdir(parents=True); path.write_text('def test_added(): pass\n')
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': []})
    result = include_changed_test_files(request, workspace_path=tmp_path, repositories=('cpm',),
                                       baseline_shas={}, enabled_suites=['ibis'] if enabled else ['python'])
    if enabled:
        assert [(t.suite, t.test_args) for t in result.targets] == [('ibis', ['Test/flask/v3/test_home.py'])]
        assert not result.skipped
    else:
        assert not result.targets
        assert 'Test/flask/v3/test_home.py' in result.skipped[0].reason


@pytest.mark.parametrize('selector', [
    'playwright/integrated/live_home.spec.js',
    'playwright/integrated/projects/date_filters.spec.js',
])
def test_playwright_accepts_explicit_integrated_files_only_for_foyr(selector):
    request = {'schema_version': '1.0', 'targets': [
        {'repository': 'foyr2', 'suite': 'playwright', 'test_args': [selector]}]}
    DevelopmentVerificationRequest.model_validate(request)
    for repository in ('cpm', 'pi'):
        request['targets'][0]['repository'] = repository
        with pytest.raises(ValueError, match='requires foyr2'):
            DevelopmentVerificationRequest.model_validate(request)


@pytest.mark.parametrize('selector', [
    'playwright/integrated', 'playwright/local.spec.js',
    'playwright/integrated/**/*.spec.js', 'playwright/integrated/../local.spec.js',
    '/playwright/integrated/live_home.spec.js', 'playwright/integrated/./live_home.spec.js',
    'playwright//integrated/live_home.spec.js', 'playwright/integrated/live_home.spec.js:12',
    'playwright/integrated/live_home.spec.ts', 'playwright/integrated/live_home.spec.js\n',
    '--project=chromium', '--grep=home', '--list',
    'playwright/integrated/test with spaces.spec.js', 'playwright/integrated/test\\name.spec.js',
])
def test_playwright_rejects_unbounded_or_alternate_selection(selector):
    with pytest.raises(ValueError):
        DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
            {'repository': 'foyr2', 'suite': 'playwright', 'test_args': [selector]}]})


def test_playwright_code_binding_requires_both_application_repositories():
    request = {'schema_version': '1.0', 'targets': [
        {'repository': 'foyr2', 'suite': 'playwright', 'test_args': ['playwright/integrated/live_home.spec.js']}],
        'workspace_diff_hash': 'a' * 64, 'snapshot_repositories': ['foyr2']}
    with pytest.raises(ValueError, match='both cpm and foyr2'):
        DevelopmentVerificationRequest.model_validate(request)
    request['snapshot_repositories'] = ['cpm', 'foyr2']
    DevelopmentVerificationRequest.model_validate(request)


@pytest.mark.parametrize('code,status', [(0, 'passed'), (1, 'failed'), (125, 'environment_error')])
def test_playwright_uses_isolated_controller_and_preserves_result(tmp_path, code, status):
    capture = tmp_path/'args.json'
    from test_recordings import controller_script
    controller = controller_script(tmp_path, code=code)
    controller.write_text('import json,sys,pathlib\n'
        + f'pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n'
        + controller.read_text())
    selectors = ['playwright/integrated/live_home.spec.js', 'playwright/integrated/project_dates.spec.js']
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
        {'repository': 'foyr2', 'suite': 'playwright', 'test_args': selectors}]})
    result = execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
                             runtime_entrypoint=tmp_path/'must-not-start-compost',
                             playwright_entrypoint=controller, result_path='.symphony/result.json',
                             timeout_seconds=60)
    assert result == code
    args = json.loads(capture.read_text())
    assert args[:3] == ['--workspace', str(tmp_path), '--timeout']
    assert 1 <= int(args[3]) <= 50
    assert args[4] == '--recording-output'
    assert Path(args[5]).parent == tmp_path/'.symphony/recordings'
    assert args[6:] == selectors
    report = validate_verification_report(json.loads((tmp_path/'.symphony/result.json').read_text()),
                                         request, command_succeeded=code == 0)
    assert report['status'] == status
    assert report['results'][0]['suite'] == 'playwright'


def test_missing_playwright_controller_never_uses_shared_smoke_fallback(tmp_path):
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
        {'repository': 'foyr2', 'suite': 'playwright', 'test_args': ['playwright/integrated/live_home.spec.js']}]})
    assert execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
                           runtime_entrypoint=tmp_path/'must-not-prepare',
                           playwright_entrypoint=tmp_path/'missing.py') == 125


@pytest.mark.parametrize('enabled', [True, False])
def test_changed_integrated_specs_are_selected_in_full_or_reported_uncovered(tmp_path, enabled):
    import subprocess
    from symphony_jira.development_verification import include_changed_test_files
    checkout = tmp_path/'foyr2'
    subprocess.run(['git', 'init', '-q', str(checkout)], check=True)
    subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-qm', 'baseline'], check=True)
    integrated = 'playwright/integrated/live_home.spec.js'
    local = 'playwright/local_only.spec.js'
    for name in (integrated, local):
        path = checkout/name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('test source\n')
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': []})
    result = include_changed_test_files(request, workspace_path=tmp_path, repositories=('foyr2',),
                                       baseline_shas={}, enabled_suites=['playwright'] if enabled else ['frontend'])
    if enabled:
        assert [(t.suite, t.test_args) for t in result.targets] == [('playwright', [integrated])]
        assert integrated not in result.skipped[0].reason
    else:
        assert not result.targets
        assert integrated in result.skipped[0].reason
    assert local in result.skipped[0].reason


@pytest.mark.parametrize('suite,selector,cleanup_timeout', [
    ('playwright', 'playwright/integrated/live_home.spec.js', 90),
    ('python', 'tests/test_home.py', 5),
])
def test_timeout_allows_playwright_service_cleanup_before_forced_kill(tmp_path, monkeypatch, suite, selector, cleanup_timeout):
    import subprocess
    from unittest.mock import Mock
    import symphony_jira.development_verification as verification
    controller = tmp_path/'controller.py'; controller.write_text('unused')
    process = Mock(pid=12345)
    process.wait.side_effect = [subprocess.TimeoutExpired('controller', 1), 0]
    monkeypatch.setattr(verification.subprocess, 'Popen', Mock(return_value=process))
    kill = Mock(); monkeypatch.setattr(verification.os, 'killpg', kill)
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0', 'targets': [
        {'repository': 'foyr2', 'suite': suite, 'test_args': [selector]}]})
    assert execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
                           foyr_entrypoint=controller, playwright_entrypoint=controller) == 125
    assert process.wait.call_args_list[1].kwargs == {'timeout': cleanup_timeout}
    kill.assert_called_once_with(process.pid, verification.signal.SIGTERM)


@pytest.mark.parametrize('when', ['before', 'during'])
def test_playwright_cpm_dependency_drift_invalidates_browser_result(tmp_path, when):
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
    changed = tmp_path/'cpm/changed.py'
    marker = tmp_path/'runner-called'
    controller = tmp_path/'browser-controller.py'
    controller.write_text('from pathlib import Path\n'
                          + f'Path({str(marker)!r}).write_text("executed")\n'
                          + (f'Path({str(changed)!r}).write_text("CPM changed during browser test")\n'
                             if when == 'during' else ''))
    request = DevelopmentVerificationRequest.model_validate({'schema_version': '1.0',
        'targets': [{'repository': 'foyr2', 'suite': 'playwright',
                     'test_args': ['playwright/integrated/live_home.spec.js']}],
        'workspace_diff_hash': before.content_hash, 'snapshot_repositories': ['cpm', 'foyr2']})
    if when == 'before':
        changed.write_text('CPM changed after selection')
    assert execute_request(request, workspace_path=tmp_path, entrypoint=tmp_path/'must-not-run',
                           playwright_entrypoint=controller, result_path='.symphony/result.json') == 125
    assert marker.exists() == (when == 'during')
    report = json.loads((tmp_path/'.symphony/result.json').read_text())
    assert report['status'] == 'stale'
