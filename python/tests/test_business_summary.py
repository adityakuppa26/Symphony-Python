import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from symphony_jira.business_summary import ensure_business_summary, parse_business_summary
from symphony_jira.config import CodexConfig
from symphony_jira.dashboard import enrich_run, render_run_row
from symphony_jira.models import Issue
from symphony_jira.orchestrator import SingleIssueOrchestrator
from symphony_jira.store import Store
from test_orchestrator import PlanThenImplementCodexRunner, codex_result, create_verification_case

SUMMARY = 'Project managers can filter projects by creation date. This helps them find recently created projects.'
OUTPUT = json.dumps({'sentences': ['Project managers can filter projects by creation date.', 'This helps them find recently created projects.']})


class FakeRunner:
    def __init__(self, mode='ok'):
        self.calls=[]
        self.mode=mode

    async def run(self, prompt, workspace_path, config, **kwargs):
        self.calls.append((prompt,workspace_path,config,kwargs))
        if self.mode=='error': raise RuntimeError('unavailable')
        if self.mode=='cancel': raise asyncio.CancelledError()
        if self.mode=='timeout': await asyncio.sleep(5)
        return SimpleNamespace(status='completed',final_message=OUTPUT if self.mode=='ok' else 'invalid output')


class BusinessSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_caches_by_requirements_and_uses_isolated_read_only_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.db'; store=Store(path); runner=FakeRunner()
            args=dict(issue=SimpleNamespace(identifier='T-1',title='Filter projects'), evidence='Creation-date filters.',
                      store=store,runner=runner,config=CodexConfig())
            self.assertEqual(await ensure_business_summary(requirements_hash='one',**args),SUMMARY)
            self.assertEqual(await ensure_business_summary(requirements_hash='one',**args),SUMMARY)
            self.assertEqual(len(runner.calls),1)
            self.assertEqual(Store(path).get_business_summary('T-1','one'),SUMMARY)
            call=runner.calls[0]
            self.assertFalse(call[1].exists())
            self.assertIn('read-only',call[2].args)
            self.assertIn('--skip-git-repo-check',call[2].args)
            self.assertNotIn('event_callback',call[3])
            await ensure_business_summary(requirements_hash='two',**args)
            self.assertEqual(len(runner.calls),2)

    async def test_failures_are_optional_and_cancellation_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.db')
            for mode in ('error','invalid','timeout'):
                args=dict(issue=SimpleNamespace(identifier='T-1',title='Feature'),requirements_hash=mode,
                          evidence='Requirements',store=store,runner=FakeRunner(mode),
                          config=CodexConfig(business_summary_timeout_seconds=1))
                self.assertIsNone(await ensure_business_summary(**args))
                self.assertIsNone(store.get_business_summary('T-1',mode))
            args['runner']=FakeRunner('cancel')
            with self.assertRaises(asyncio.CancelledError): await ensure_business_summary(**args)

    async def test_summary_precedes_planning_without_a_status_phase(self):
        class Runner(PlanThenImplementCodexRunner):
            def __init__(self):
                super().__init__(); self.order=[]
            async def run(self,prompt,workspace_path,config,**kwargs):
                if prompt.startswith('Write a short business overview'):
                    self.order.append('summary')
                    return codex_result(workspace_path,'completed',final_message=OUTPUT,final_path=config.output_last_message_file)
                self.order.append('planning')
                self.planning_prompt = prompt
                return await super().run(prompt,workspace_path,config,**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            original,manager,_,jira=create_verification_case(Path(tmp),verification_status='passed',required=False)
            workflow=original.workflow
            workflow.config.codex.require_plan_approval=True
            workflow.config.codex.summarize_requirements_before_planning=True
            runner=Runner()
            result=await SingleIssueOrchestrator(workflow,jira,original.store,codex_runner=runner,workspace_manager=manager).run_once('T-1')
            self.assertEqual(runner.order,['summary','planning'])
            self.assertNotIn(SUMMARY, runner.planning_prompt)
            self.assertEqual(result.run.blocked_phase,'planning_approval')
            row=enrich_run(result.run,original.store,workflow)
            self.assertEqual(row['business_summary'],SUMMARY)
            self.assertNotIn('summary',row['workflow_progress'].lower())
            html=render_run_row(row)
            issue_cell=html.split('<td data-label="Status">')[0]
            self.assertIn(SUMMARY,issue_cell)
            self.assertIn('business-summary',issue_cell)
            for event in original.store.list_codex_events(result.run.id):
                self.assertNotIn('summary',event.event_type)
            row['business_summary']='<script>alert(1)</script>'
            self.assertIn('&lt;script&gt;',render_run_row(row))

    def test_rejects_long_or_non_sentence_list_outputs(self):
        for value in ({'sentences':['one']},{'sentences':['one']*5},{'sentences':['', 'two']},{'sentences':['a'*501,'two']}):
            with self.assertRaises(ValueError): parse_business_summary(json.dumps(value))

    def test_summary_table_can_be_added_without_modifying_an_existing_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.db'; store=Store(path)
            issue=Issue(id='1',identifier='T-1',title='Feature',status='In Progress',url='https://jira.example/T-1')
            run=store.create_run(issue,Path(tmp)/'workspace',branch_name=None)
            before=store.get_run(run.id).model_dump()
            with store._connect() as connection:
                connection.execute('DROP TABLE business_summaries')
            updated=Store(path)
            updated.save_business_summary('T-1',run.issue_fingerprint,SUMMARY)
            self.assertEqual(updated.get_run(run.id).model_dump(),before)
            self.assertEqual(updated.get_business_summary('T-1',run.issue_fingerprint),SUMMARY)
