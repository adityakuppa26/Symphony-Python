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

    def test_preexisting_running_service_is_preserved_when_recreated(self):
        state={'containers':{},'dependencies':{}}
        runtime.register(state,snapshot({'foyr':container('user-old')}),{'foyr':container('new')})
        self.assertEqual(state['containers'],{})

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
                self.assertEqual(call.call_count,1)
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
