import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class RuntimeShellTests(unittest.TestCase):
    def test_frontend_keeps_proxy_but_bypasses_corporate_mirror(self):
        prefix = (SCRIPTS / 'foyr-frontend-tests.sh').read_text().split('# Build outputs')[0]
        result = subprocess.run(
            ['bash', '-c', prefix + '\nprintf "%s\\n" "$HTTP_PROXY" "$NO_PROXY" "$no_proxy"', 'test', 'cpm/home/homeTest.js'],
            env={**os.environ, 'SYMPHONY_HTTP_PROXY': 'http://proxy.example:80', 'SYMPHONY_NO_PROXY': 'existing.internal'},
            capture_output=True, text=True, check=True,
        )
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], 'http://proxy.example:80')
        self.assertIn('existing.internal', lines[1].split(','))
        self.assertIn('artifacthub-phx.oci.oraclecorp.com', lines[1].split(','))
        self.assertEqual(lines[1], lines[2])

    def test_disposable_jobs_limit_resources_and_cleanup(self):
        source = (SCRIPTS / 'test.sh').read_text()
        function = source[source.index('run_container() ('):source.index('\ncollect_diagnostics()')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            podman = root / 'podman'
            podman.write_text('#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ["CALL_LOG"], "a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
            podman.chmod(0o755)
            logfile = root / 'calls'
            subprocess.run(['bash', '-c', function + '\nrun_container 2s --pull=never image true'],
                           env={**os.environ, 'PODMAN': str(podman), 'CALL_LOG': str(logfile), 'SYMPHONY_HOST_BROWSER': ''}, check=True)
            calls = [json.loads(line) for line in logfile.read_text().splitlines()]
            run = calls[0]
            self.assertIn('--memory=2g', run)
            self.assertIn('--memory-swap=2g', run)
            self.assertIn('--cpus=2', run)
            name = run[run.index('--name') + 1]
            self.assertEqual(calls[-1], ['rm', '-f', '--ignore', name])

    def test_karma_setup_installs_locked_packages_without_browser_lifecycle_hooks(self):
        source = (SCRIPTS / 'foyr-frontend-tests.sh').read_text()
        install = source[source.index('export PYTHONPATH='):source.index('node node_modules/grunt-cli')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'foyr').mkdir()
            (root / 'package-lock.json').write_text('{}')
            (root / 'foyr/package-lock.json').write_text('{}')
            npm = root / 'npm'
            npm.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n')
            npm.chmod(0o755)
            logfile = root / 'calls'
            subprocess.run(['bash', '-c', install], cwd=root,
                           env={**os.environ, 'PATH': str(root) + ':' + os.environ['PATH'], 'CALL_LOG': str(logfile)}, check=True)
            calls = logfile.read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertTrue(all('ci' in call and '--ignore-scripts' in call for call in calls), calls)

    def _check_compose_runtime(self, repository):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scripts').mkdir()
            (root / 'scripts/runtime-services.py').write_text((SCRIPTS/'runtime-services.py').read_text())
            compost = root / 'compost'
            compost.mkdir()
            (compost / '.env').write_text('CPM_SRC=old\nFOYR_SRC=old\nKEEP=value\nCREATE_NEW_DATABASE_WHEN_TESTING=True\n')
            original_env = (compost / '.env').read_bytes()
            original_mtime = (compost / '.env').stat().st_mtime_ns
            (compost / 'docker-compose.yml').write_text('services: {}')
            workspace = root / 'workspace'
            for repo_name in ('foyr2','cpm'):
                source=workspace/repo_name
                source.mkdir(parents=True)
                subprocess.run(['git', 'init', '-q', str(source)], check=True)
            podman = root / 'podman'
            podman.write_text('''#!/usr/bin/env python3
import json,os,sys
args=sys.argv[1:]
with open(os.environ['CALL_LOG'],'a') as f: f.write(json.dumps(args)+'\\n')
if args[0]=='compose':
 assert os.environ.get('PODMAN_COMPOSE_PROVIDER','').endswith('/podman-compose')
 assert '--project-directory' not in args and '--format' not in args
if 'config' in args:
 print(json.dumps({'services': {name: {'image':name,'volumes':[os.environ[var]+':'+mount+':rw'] if name=='foyr' else [{'target':mount,'source':os.environ[var]}]} for name,mount,var in [('foyr','/src','FOYR_SRC'),('ibis','/ibis','CPM_SRC')]}}))
elif args[0]=='ps':
 print('fake-cpm fake-foyr fake-ibis')
elif args[0]=='inspect' and '--format' not in args:
 print(json.dumps([{'Id':'fake-'+name,'Name':name,'State':{'Running':True},'Config':{'Labels':{'com.docker.compose.service':name}}} for name in ('cpm','foyr','ibis')]))
elif args[0]=='inspect':
 if args[-1]=='{{.State.Running}}': print('true')
 elif '.State' in args[-1]: print(json.dumps({'Running':True,'Health':{'Status':'healthy'}}))
 else: print(json.dumps([{'Destination':'/src','Source':os.environ['FOYR_SRC']},{'Destination':'/ibis','Source':os.environ['CPM_SRC']},{'Destination':'/TexturaWD/textura','Source':os.environ['CPM_SRC']}]))
''')
            podman.chmod(0o755)
            provider = root / 'podman-compose'
            provider.write_text('#!/bin/sh\nexit 0\n')
            provider.chmod(0o755)
            script = root / 'scripts/test.sh'
            script.write_text((SCRIPTS / 'test.sh').read_text()
                              .replace('/home/adkuppa/compost', str(compost))
                              .replace('/usr/bin/podman', str(podman))
                              .replace('/home/adkuppa/.local/state/symphony', str(root / 'locks')))
            logfile = root / 'calls'
            result = subprocess.run(['bash', str(script), repository, str(workspace), '--', 'pytest', 'Test/unit/test_home.py' if repository=='cpm' else 'tests/test_home.py'],
                                    env={**os.environ, 'CALL_LOG': str(logfile), 'PATH': str(root) + os.pathsep + os.environ['PATH'], 'PODMAN_COMPOSE_PROVIDER': '/invalid/docker-compose'}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = [json.loads(line) for line in logfile.read_text().splitlines()]
            up=next(i for i,call in enumerate(calls) if 'up' in call)
            execute=next(i for i,call in enumerate(calls) if 'exec' in call)
            self.assertLess(up,execute)
            self.assertIn('timeout',calls[execute])
            expected_env = 'CREATE_NEW_DATABASE_WHEN_TESTING=False' if repository=='cpm' else 'FOYR_CONFIG_FILE=/src/tests/testing.yml'
            self.assertIn(expected_env,calls[execute])
            self.assertIn('CREATE_NEW_DATABASE_WHEN_TESTING=True',(compost/'.env').read_text())
            self.assertEqual((compost/'.env').read_bytes(), original_env)
            self.assertEqual((compost/'.env').stat().st_mtime_ns, original_mtime)
            self.assertFalse((root/'.symphony/env-backups').exists())

    def test_legacy_foyr_route_is_rejected_before_runtime_setup(self):
        result = subprocess.run(['bash', str(SCRIPTS / 'test.sh'), 'foyr2', '/missing-checkout'],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Foyr verification moved to', result.stderr)

    def test_legacy_cpm_route_is_rejected_before_runtime_setup(self):
        result = subprocess.run(['bash', str(SCRIPTS / 'test.sh'), 'cpm', '/missing-checkout'],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CPM verification moved to', result.stderr)


def test_podman_children_cannot_inherit_runtime_lock(tmp_path):
    source = (SCRIPTS / 'test.sh').read_text()
    function = source[source.index('run_logged() {'):source.index('# Always remove')]
    child = tmp_path / 'child.py'
    child.write_text('import os\nassert not os.path.exists("/proc/self/fd/9"), "lock leaked to child"\n')
    script = 'exec 9>"$LOCK_FILE"\nflock 9\n' + function + '\nrun_logged python3 "$CHILD"\n! flock -n "$LOCK_FILE" true\n'
    result = subprocess.run(['bash', '-e', '-c', script],
        env={**os.environ, 'LOCK_FILE': str(tmp_path/'lock'), 'CHILD': str(child), 'log_file': str(tmp_path/'log')},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cleanup_trap_runs_on_test_exit_and_failed_setup(tmp_path):
    source = (SCRIPTS / 'test.sh').read_text()
    function = source[source.index('cleanup_runtime() {'):source.index('trap cleanup_runtime EXIT')]
    python = tmp_path / 'python3'
    python.write_text('#!/bin/sh\necho "$*" >> "$CALLS"\n')
    python.chmod(0o700)
    for mode, code, cleanup_expected in [('test', 0, True), ('test', 3, True), ('prepare', 0, False), ('prepare', 1, True)]:
        calls = tmp_path / f'{mode}-{code}'
        result = subprocess.run(['bash', '-c', 'record_runtime() { :; }\n' + function + '\ntrap cleanup_runtime EXIT\nexit "$TEST_CODE"'],
            env={**os.environ, 'PATH': str(tmp_path)+':'+os.environ['PATH'], 'mode': mode,
                 'TEST_CODE': str(code), 'CALLS': str(calls), 'runtime_owner': '/work/A',
                 'runtime_state': '/state.json', 'PODMAN': '/podman', 'SCRIPT_ROOT': '/scripts'}, timeout=10)
        assert result.returncode == code
        assert calls.exists() == cleanup_expected
