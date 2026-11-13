import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from symphony_jira.development_verification import DevelopmentVerificationTarget, fixed_test_command

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), SCRIPTS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('repository,profile', [('foyr2', 'projects-page')])
def test_smoke_profiles_use_fixed_commands(repository, profile):
    target = DevelopmentVerificationTarget(repository=repository, suite='smoke', test_args=[profile])
    assert fixed_test_command(target) == ['symphony-local-smoke', profile]


@pytest.mark.parametrize('repository,args', [
    ('cpm', ['projects-query']), ('cpm', ['projects-page']), ('pi', ['projects-query']),
    ('foyr2', ['projects-page', '--url=https://example.com']), ('cpm', ['python', '-c', 'code']),
])
def test_smoke_rejects_arbitrary_commands_and_profile_arguments(repository, args):
    with pytest.raises(ValueError):
        DevelopmentVerificationTarget(repository=repository, suite='smoke', test_args=args)


def runtime_fixture(tmp_path, monkeypatch):
    smoke = load_script('local-smoke')
    for repo in ('cpm', 'foyr2'):
        (tmp_path / repo).mkdir()
    items = {
        'ibis': {'Name': 'ibis', 'Id': 'ibis-id', 'Mounts': [{'Destination': '/ibis', 'Source': str(tmp_path / 'cpm')}]},
        'foyr': {'Name': 'foyr', 'Id': 'foyr-id', 'Mounts': [{'Destination': '/src', 'Source': str(tmp_path / 'foyr2')}]},
        'static': {'Name': 'static', 'Id': 'static-id', 'Mounts': [{'Destination': '/foyr_src', 'Source': str(tmp_path / 'foyr2')}]},
    }
    monkeypatch.setattr(smoke, 'inspect_container', lambda name: items[name])
    return smoke, items


def test_smoke_refuses_mismatched_checkouts(tmp_path, monkeypatch):
    smoke, items = runtime_fixture(tmp_path, monkeypatch)
    items['static']['Mounts'][0]['Source'] = '/different/checkout'
    with pytest.raises(smoke.Unavailable, match='requested checkout'):
        smoke.check_runtime(tmp_path, 'foyr2', 'projects-page')


def test_query_smoke_uses_exact_container_and_does_not_prepare_services(tmp_path, monkeypatch):
    smoke, _ = runtime_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('SYMPHONY_SMOKE_USER_ID', '347')
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    assert smoke.run_smoke(tmp_path, 'cpm', 'projects-query') == 0
    command = run.call_args.args[0]
    assert command[:5] == ['/usr/bin/podman', 'exec', '-i', 'ibis-id', 'timeout']
    assert command[-1] == '347'
    assert 'SET TRANSACTION READ ONLY' in run.call_args.kwargs['input']
    assert run.call_count == 1


def test_smoke_missing_operator_setup_is_unavailable(tmp_path, monkeypatch):
    smoke, _ = runtime_fixture(tmp_path, monkeypatch)
    monkeypatch.delenv('SYMPHONY_SMOKE_USER_ID', raising=False)
    monkeypatch.delenv('SYMPHONY_SMOKE_STORAGE_STATE', raising=False)
    for repository, profile in [('cpm', 'projects-query'), ('foyr2', 'projects-page')]:
        assert smoke.main(['--workspace', str(tmp_path), '--repository', repository, profile]) == 125


def test_oracle_smoke_rolls_back_even_on_failure(monkeypatch):
    import sys
    smoke = load_script('projects-query-smoke')
    events = []
    config = SimpleNamespace(createNewDatabase=True)
    session = SimpleNamespace(execute=lambda sql: events.append(sql), rollback=lambda: events.append('rollback'))
    user = SimpleNamespace(organization=SimpleNamespace(businessUnitsEnabled=False, enableNotaryBypass=False))
    def query(**kwargs):
        assert events[0] == 'SET TRANSACTION READ ONLY'
        raise RuntimeError('ORA-00906')
    modules = {
        'Objects.Core': SimpleNamespace(Configuration=config),
        'cpms.translations': SimpleNamespace(installGettext=lambda _: None),
        'sqlalchemy': SimpleNamespace(text=lambda sql: sql),
        'xobjects.container': SimpleNamespace(container=SimpleNamespace(engine=SimpleNamespace(_session=session))),
        'xobjects.organization_user': SimpleNamespace(OrganizationUser=SimpleNamespace(getUser=lambda _: user)),
        'cpmapi.projects.review': SimpleNamespace(ProjectsReviewQuery=SimpleNamespace(GET=query), can_open=lambda _: True),
        'cpmapi.projects.review_columns': SimpleNamespace(columns=lambda *_: []),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    with pytest.raises(RuntimeError, match='ORA-00906'):
        smoke.main(347)
    assert events == ['SET TRANSACTION READ ONLY', 'rollback']
    assert config.createNewDatabase is False


@pytest.mark.parametrize('case,expected', [('healthy', 0), ('bundle404', 1), ('query500', 1), ('denied', 125), ('login', 125)])
def test_browser_smoke_detects_page_and_network_failures(tmp_path, case, expected):
    import os
    import subprocess
    checkout = tmp_path / 'foyr2'
    module = checkout / 'node_modules/playwright'
    module.mkdir(parents=True)
    (checkout / 'package.json').write_text('{}')
    (module / 'index.js').write_text(r'''
const scenario = process.env.SMOKE_TEST_CASE;
const hooks = {};
const response = {
    url: () => 'https://127.0.0.1/foyr/projects/apis/query',
    status: () => scenario === 'query500' ? 500 : scenario === 'denied' ? 403 : 200,
    ok: () => !['query500','denied'].includes(scenario),
    json: async () => ({rows: []}),
};
const page = {
    on: (event, fn) => { hooks[event] = fn; },
    url: () => scenario === 'login' ? 'https://127.0.0.1/foyr/logon/login' : 'https://127.0.0.1/foyr/tpm-ui',
    waitForResponse: async () => { if(scenario === 'login') throw new Error('timeout'); return response; },
    goto: async () => {
        hooks.response(response);
        if(scenario === 'bundle404') hooks.response({status:()=>404,url:()=> 'https://127.0.0.1/bundle.js?secret=hidden'});
    },
    locator: () => ({waitFor: async () => {}}),
    evaluate: async () => {},
};
exports.chromium = {
    executablePath: () => __filename,
    launch: async () => ({newContext: async () => ({newPage: async () => page}), close: async () => {}}),
};
''')
    result = subprocess.run(['node', str(SCRIPTS / 'projects-page-smoke.cjs'), str(checkout), '/fake-state'],
                            env={**os.environ, 'SMOKE_TEST_CASE': case, 'SYMPHONY_SMOKE_BROWSER': ''},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stdout + result.stderr
    assert 'secret=hidden' not in result.stdout
    if case == 'bundle404':
        assert '404 https://127.0.0.1/bundle.js' in result.stdout


@pytest.mark.parametrize('prepare_with_test', [False, True])
@pytest.mark.parametrize('code', [0, 1, 125])
def test_foyr_smoke_dispatch_survives_test_stack_migration(tmp_path, prepare_with_test, code):
    from symphony_jira.development_verification import (
        DevelopmentVerificationRequest, execute_request, validate_verification_report,
    )
    scripts = tmp_path/'scripts'
    scripts.mkdir()
    capture = tmp_path/'smoke-args.json'
    entrypoint = scripts/'test.sh'
    entrypoint.write_text((SCRIPTS/'test.sh').read_text().replace(
        '/home/adkuppa/.local/state/symphony', str(tmp_path/'locks')))
    entrypoint.chmod(0o700)
    (scripts/'local-smoke.py').write_text(
        'import json,os,sys,pathlib\n'
        'assert not os.path.exists("/proc/self/fd/9"), "runtime lock leaked to smoke child"\n'
        f'pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n'
        f'raise SystemExit({code})\n')
    request = DevelopmentVerificationRequest.model_validate({'schema_version':'1.0', 'targets':[
        {'repository':'foyr2', 'suite':'smoke', 'test_args':['projects-page']}]})
    result = execute_request(request, workspace_path=tmp_path, entrypoint=entrypoint,
        runtime_entrypoint=tmp_path/'must-not-start-runtime', prepare_with_test=prepare_with_test,
        result_path='.symphony/result.json')
    assert result == code
    assert json.loads(capture.read_text()) == ['--workspace',str(tmp_path),'--repository','foyr2','projects-page']
    report = validate_verification_report(json.loads((tmp_path/'.symphony/result.json').read_text()),
        request, command_succeeded=result == 0)
    assert report['status'] == {0:'passed',1:'failed',125:'environment_error'}[code]
