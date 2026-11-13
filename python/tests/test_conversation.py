import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from symphony_jira.config import CodexConfig
from symphony_jira.conversation import answer_question, decision_ledger, plan_changes
from symphony_jira.human_input_context import HISTORY_MARKER


class ConversationTests(unittest.TestCase):
    def test_ledger_preserves_question_binding_and_excludes_system_routing(self):
        history = [
            {'id': '1', 'kind': 'operator_feedback', 'text': 'sure', 'question': 'Use a timestamp?'},
            {'id': '2', 'kind': 'system_routing', 'text': 'Replan'},
            {'id': '3', 'kind': 'plan_approval', 'text': 'Approved', 'approval_invalidated_at': 'now'},
        ]
        ledger = decision_ledger(HISTORY_MARKER + json.dumps(history))
        self.assertEqual([item['id'] for item in ledger], ['3', '1'])
        self.assertEqual(ledger[0]['approval_state'], 'invalidated')
        self.assertEqual(ledger[1]['question'], 'Use a timestamp?')

    def test_question_uses_isolated_read_only_workspace_and_does_not_resume(self):
        class Runner:
            async def run(self, prompt, workspace, config, **kwargs):
                self.prompt, self.workspace, self.config = prompt, workspace, config
                assert workspace.name.startswith('symphony-question-')
                return SimpleNamespace(status='completed', final_message='The approved plan lacks the exception.')
        runner = Runner()
        run = SimpleNamespace(issue_identifier='T-1', status='blocked', blocked_phase='implementation',
                              error='Replan required', final_message=None, human_input_context='Accepted exception')
        answer = asyncio.run(answer_question(run=run, question='Why?', plan='Old plan',
                            config=CodexConfig(), runner=runner))
        self.assertIn('lacks the exception', answer)
        self.assertIn('read-only', runner.config.args)
        self.assertIn('not a resume, decision, approval', runner.prompt)
        self.assertFalse(runner.workspace.exists())
        self.assertEqual(run.status, 'blocked')

    def test_plan_delta_includes_exception_and_tests_but_ignores_baseline_noise(self):
        old = {'assumptions': [], 'test_cases': [], 'baseline_repository_shas': ['old']}
        new = {'assumptions': [{'id': 'A1', 'statement': 'Allow the narrow aggregation'}],
               'test_cases': [{'id': 'T1'}], 'baseline_repository_shas': ['new']}
        changes = plan_changes(old, new)
        self.assertEqual(len(changes), 2)
        self.assertIn('Allow the narrow aggregation', changes[0])
        self.assertIn('Test plan', changes[1])
