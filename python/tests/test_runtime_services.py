import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('runtime_services', Path(__file__).resolve().parents[1]/'scripts/runtime-services.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def container(identifier, running=True):
    return {'id': identifier, 'running': running}


def snapshot(before=None, workspace='/work/A', scope=('foyr','ibis','db')):
    return {'before': before or {}, 'workspace': workspace, 'scope': scope,
            'dependencies': {'foyr':['ibis'], 'ibis':['db'], 'db':[]}}


class RuntimeServicesTests(unittest.TestCase):
    def test_only_services_started_for_test_are_acquired(self):
        state={'containers':{},'dependencies':{}}
        before={'db':container('user-db'), 'ibis':container('stopped-ibis',False)}
        after={'db':container('user-db'), 'ibis':container('stopped-ibis'), 'foyr':container('test-foyr')}
        runtime.register(state,snapshot(before),after)
        self.assertNotIn('db',state['containers'])
        self.assertFalse(state['containers']['ibis']['remove'])
        self.assertTrue(state['containers']['foyr']['remove'])
        self.assertEqual(runtime.releasable(state,after,'/work/A'),['foyr','ibis'])

    def test_replacement_created_by_symphony_is_owned(self):
        state={'containers':{},'dependencies':{}}
        runtime.register(state,snapshot({'foyr':container('user-old')}),{'foyr':container('new')})
        self.assertEqual(state['containers']['foyr']['id'], 'new')
        self.assertTrue(state['containers']['foyr']['remove'])

    def test_failed_startup_container_is_removed(self):
        state={'containers':{},'dependencies':{}}
        after={'foyr':container('failed',False)}
        runtime.register(state,snapshot(),after)
        self.assertEqual(runtime.releasable(state,after,'/work/A'),['foyr'])
        self.assertTrue(state['containers']['foyr']['remove'])

    def test_shared_ownership_survives_first_handoff_and_recreation(self):
        state={'containers':{},'dependencies':{}}
        first={'foyr':container('first'), 'ibis':container('ibis'), 'db':container('db')}
        runtime.register(state,snapshot(),first)
        second={**first,'foyr':container('second')}
        runtime.register(state,snapshot(first,workspace='/work/B'),second)
        self.assertEqual(runtime.releasable(state,second,'/work/A'),[])
        self.assertEqual(runtime.releasable(state,second,'/work/B'),['foyr','ibis','db'])

    def test_user_replacement_is_never_stopped_by_old_ownership(self):
        state={'containers':{},'dependencies':{}}
        runtime.register(state,snapshot(),{'foyr':container('test')})
        self.assertEqual(runtime.releasable(state,{'foyr':container('user')},'/work/A'),[])
        self.assertEqual(state['containers'],{})

    def test_dependency_used_by_unowned_service_is_retained(self):
        state={'containers':{},'dependencies':{}}
        active={'foyr':container('test'), 'ibis':container('user-ibis'), 'db':container('test-db')}
        runtime.register(state,snapshot({'ibis':active['ibis']}),active)
        self.assertEqual(runtime.releasable(state,active,'/work/A'),['foyr'])
        # Once the user service stops, the orphaned test dependency can be released.
        active.pop('foyr'); active['ibis']['running']=False
        self.assertEqual(runtime.releasable(state,active,'/work/A'),['db'])

    def test_scope_includes_transitive_dependencies_only(self):
        scope, _ = runtime.scope_for({'services':{'foyr':{'depends_on':['ibis']},'ibis':{'depends_on':{'db':{}}},'db':{},'other':{}}},['foyr'])
        self.assertEqual(scope,['db','foyr','ibis'])

    def test_interrupted_setup_recovers_only_its_labelled_containers(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); workspace=str(root/'work'); state=root/'services.json'
            pending=snapshot(workspace=workspace,scope=('foyr','other'))
            pending['token']='setup-interrupted'
            runtime.save(root/'setup-interrupted.json',pending)
            current={'foyr':{**container('test'),'setup':'setup-interrupted'},'other':container('user')}
            with patch.object(runtime.sys,'argv',['helper','down','--workspace',workspace,'--state',str(state)]), \
                 patch.object(runtime,'containers',side_effect=[current,{}]), \
                 patch.object(runtime,'podman',return_value='') as call:
                runtime.main()
                self.assertEqual(call.call_count,2)
                self.assertEqual(call.call_args_list[0].args[1], ['stop', '--time', '30', 'test'])
                self.assertEqual(call.call_args_list[1].args[1], ['rm', 'test'])
                self.assertEqual(call.call_args.args[1][-1],'test')
                self.assertFalse((root/'setup-interrupted.json').exists())

    def test_cleanup_failure_retains_ownership_for_retry(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); workspace=str(root/'work'); path=root/'services.json'
            state={'containers':{},'dependencies':{}}
            current={'foyr':container('test')}
            runtime.register(state,snapshot(workspace=workspace),current)
            runtime.save(path,state)
            args=['helper','down','--workspace',workspace,'--state',str(path)]
            with patch.object(runtime.sys,'argv',args), patch.object(runtime,'containers',side_effect=[current,{}]), \
                 patch.object(runtime,'podman',side_effect=RuntimeError('temporary failure')):
                with self.assertRaises(RuntimeError): runtime.main()
            self.assertIn('foyr',runtime.read(path)['containers'])
            with patch.object(runtime.sys,'argv',args), patch.object(runtime,'containers',side_effect=[current,{}]), \
                 patch.object(runtime,'podman',return_value=''):
                runtime.main()
            self.assertEqual(runtime.read(path)['containers'],{})

    def test_test_processes_are_cleaned_without_stopping_unowned_container(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); workspace=str(root/'work'); path=root/'services.json'
            runtime.save(path,{'containers':{},'dependencies':{},'execs':[{'workspace':workspace,'container':'user-container','token':'owned-test-token'}]})
            with patch.object(runtime.sys,'argv',['helper','down','--workspace',workspace,'--state',str(path)]), \
                 patch.object(runtime,'containers',side_effect=[{'cpm':container('user-container')},{}]), \
                 patch.object(runtime,'podman',return_value='') as call:
                runtime.main()
                command=call.call_args.args[1]
                self.assertEqual(command[:2],['exec','user-container'])
                self.assertEqual(command[-1],'owned-test-token')
                self.assertEqual(call.call_count,1)
            self.assertEqual(runtime.read(path)['execs'],[])

    def test_all_temporary_jobs_are_found_even_with_inherited_service_labels(self):
        import json
        from unittest.mock import patch
        items=[{'Id':name,'Name':name,'State':{'Running':True},'Config':{'Labels':{'com.docker.compose.service':'foyr'}}} for name in ('job-a','job-b')]
        with patch.object(runtime,'podman',side_effect=['job-a job-b',json.dumps(items)]):
            found=runtime.containers(None,[],by_service=False)
        self.assertEqual(set(found),{'job-a','job-b'})

    def test_begin_does_not_relabel_running_user_services(self):
        import io, json, tempfile
        from contextlib import redirect_stdout
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            args=['helper','begin','--workspace',str(root/'work'),'--state',str(root/'state.json'),'--services','foyr']
            config={'services':{'foyr':{'depends_on':['db']},'db':{}}}
            with patch.object(runtime.sys,'argv',args), patch.object(runtime.sys,'stdin',io.StringIO(json.dumps(config))), \
                 patch.object(runtime,'containers',return_value={'foyr':container('user-app'),'db':container('user-db')}), \
                 redirect_stdout(io.StringIO()) as output:
                runtime.main()
            overlay=Path(output.getvalue().strip()+'.override')
            self.assertEqual(json.loads(overlay.read_text())['services'],{})


def test_only_new_labelled_networks_are_owned():
    state = {}
    pending = {'workspace': '/work/A', 'token': 'owned', 'network_names': ['new', 'existing', 'foreign'],
               'before_networks': {'existing': {'id': 'old'}}}
    current = {'new': {'id': 'n', 'labels': {'symphony.runtime.setup': 'owned'}},
               'existing': {'id': 'old', 'labels': {}}, 'foreign': {'id': 'f', 'labels': {}}}
    runtime.register_networks(state, pending, current)
    assert state['networks'] == {'new': {'id': 'n', 'owners': ['/work/A']}}


def test_unused_owned_network_is_removed_without_force(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    state = {'networks': {'compost_TPM': {'id': 'network-id', 'owners': ['/work/A']}}}
    monkeypatch.setattr(runtime, 'networks', lambda _: {'compost_TPM': {'id': 'network-id'}})
    calls = Mock(return_value='')
    monkeypatch.setattr(runtime, 'podman', calls)
    monkeypatch.setattr(runtime.subprocess, 'run', Mock())
    assert runtime.cleanup_networks(SimpleNamespace(), state, '/work/A') == []
    assert calls.call_args_list[1].args[1] == ['network', 'rm', 'network-id']
    assert state['networks'] == {}


def test_network_used_by_stopped_container_is_preserved(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    state = {'networks': {'compost_TPM': {'id': 'network-id', 'owners': ['/work/A']}}}
    monkeypatch.setattr(runtime, 'networks', lambda _: {'compost_TPM': {'id': 'network-id'}})
    calls = Mock(return_value='stopped-user-container\n')
    monkeypatch.setattr(runtime, 'podman', calls)
    assert runtime.cleanup_networks(SimpleNamespace(), state, '/work/A') == []
    assert calls.call_count == 1
    assert 'compost_TPM' in state['networks']


def test_replaced_network_is_not_removed(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    state = {'networks': {'compost_TPM': {'id': 'old', 'owners': ['/work/A']}}}
    monkeypatch.setattr(runtime, 'networks', lambda _: {'compost_TPM': {'id': 'user-new'}})
    calls = Mock()
    monkeypatch.setattr(runtime, 'podman', calls)
    assert runtime.cleanup_networks(SimpleNamespace(), state, '/work/A') == []
    calls.assert_not_called()
    assert not state['networks']


def test_oracle_database_shuts_down_before_stop_and_remove(tmp_path, monkeypatch):
    from unittest.mock import Mock
    state_path = tmp_path / 'state.json'
    workspace = str(tmp_path / 'work')
    state = {'containers': {'oracledb19': {'id': 'db-id', 'owners': [workspace], 'remove': True}}, 'dependencies': {}}
    runtime.save(state_path, state)
    monkeypatch.setattr(runtime.sys, 'argv', ['helper', 'down', '--workspace', workspace, '--state', str(state_path)])
    monkeypatch.setattr(runtime, 'containers', Mock(side_effect=[{'oracledb19': container('db-id')}, {}]))
    calls = Mock(return_value='')
    monkeypatch.setattr(runtime, 'podman', calls)
    runtime.main()
    commands = [call.args[1] for call in calls.call_args_list]
    assert commands[0][:5] == ['exec', '-i', '--user', 'oracle', 'db-id']
    assert 'SHUTDOWN IMMEDIATE;' in calls.call_args_list[0].kwargs['input']
    assert commands[1:] == [['stop', '--time', '30', 'db-id'], ['rm', 'db-id']]
    assert not runtime.read(state_path)['containers']


def test_oracle_shutdown_failure_never_force_stops_database(tmp_path, monkeypatch):
    import pytest
    from unittest.mock import Mock
    state_path = tmp_path / 'state.json'
    workspace = str(tmp_path / 'work')
    runtime.save(state_path, {'containers': {'oracledb19': {'id': 'db-id', 'owners': [workspace], 'remove': True}}, 'dependencies': {}})
    monkeypatch.setattr(runtime.sys, 'argv', ['helper', 'down', '--workspace', workspace, '--state', str(state_path)])
    monkeypatch.setattr(runtime, 'containers', Mock(side_effect=[{'oracledb19': container('db-id')}, {}]))
    calls = Mock(side_effect=RuntimeError('shutdown failed'))
    monkeypatch.setattr(runtime, 'podman', calls)
    with pytest.raises(RuntimeError, match='shutdown failed'):
        runtime.main()
    assert calls.call_count == 1
    assert calls.call_args.args[1][0] == 'exec'
    assert 'oracledb19' in runtime.read(state_path)['containers']
