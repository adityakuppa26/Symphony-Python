from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import unittest

import httpx

from symphony_jira.config import JiraRequirementsConfig, TrackerConfig
from symphony_jira.jira import JiraClient, build_requirements_snapshot, normalize_issue
from symphony_jira.models import RelatedIssue, RequirementSource
from symphony_jira.orchestrator import planning_requirements_snapshot_prompt
from symphony_jira.requirements_artifacts import canonical_requirements_snapshot_json
from symphony_jira.workflow import load_workflow, render_prompt
from test_jira_models import sample_issue_payload


class CommentExclusionTests(unittest.TestCase):
    def test_workflow_excludes_comments_and_does_not_render_comment_summary(self):
        workflow = load_workflow(Path(__file__).resolve().parents[1] / 'WORKFLOW.md')
        self.assertFalse(workflow.config.tracker.requirements.include_comments)
        payload = sample_issue_payload()
        payload['fields']['comment']['comments'][0]['body'] = 'COMMENT_SCOPE_SENTINEL'
        # Even an Issue retaining comments cannot reintroduce the removed summary.
        issue = normalize_issue(payload, 'https://jira.example.test')
        prompt = render_prompt(workflow, issue)
        self.assertNotIn('COMMENT_SCOPE_SENTINEL', prompt)
        self.assertNotIn('Comment summary:', prompt)
        self.assertIn('Jira comments are excluded', prompt)

    def test_exclusion_precedes_classification_hashing_and_evidence_rendering(self):
        config = JiraRequirementsConfig(
            include_comments=False, acceptance_criteria_fields=['customfield_123'],
        )
        payload = sample_issue_payload()
        payload['fields']['customfield_123'] = 'The required behavior is observable.'
        payload['fields']['comment']['comments'][0]['body'] = '[contradiction] COMMENT_SCOPE_SENTINEL'
        issue = normalize_issue(payload, 'https://jira.example.test', requirements_config=config)
        snapshot = issue.requirements_snapshot
        self.assertEqual(issue.comments, [])
        self.assertEqual(snapshot.comments, [])
        self.assertEqual(snapshot.unresolved_contradictions, [])
        self.assertEqual(snapshot.incomplete_reasons, [])
        self.assertTrue(snapshot.current_requirements)
        self.assertTrue(all(source.source_type != 'comment'
                            for decision in snapshot.current_requirements
                            for source in decision.sources))
        self.assertNotIn('COMMENT_SCOPE_SENTINEL', canonical_requirements_snapshot_json(snapshot))
        bundle = json.loads(planning_requirements_snapshot_prompt(issue))
        self.assertEqual(bundle['comments'], [])
        self.assertIn('Jira comments are excluded', bundle['authority_policy'])
        self.assertNotIn('COMMENT_SCOPE_SENTINEL', json.dumps(bundle))

        edited = deepcopy(payload)
        edited['fields']['comment']['comments'][0]['body'] = 'A completely different request.'
        edited['fields']['updated'] = '2026-09-10T10:00:00.000+0000'
        revised = normalize_issue(edited, 'https://jira.example.test', requirements_config=config)
        self.assertEqual(snapshot.content_hash, revised.requirements_snapshot.content_hash)
        edited['fields']['customfield_123'] = 'Different acceptance criteria.'
        revised = normalize_issue(edited, 'https://jira.example.test', requirements_config=config)
        self.assertNotEqual(snapshot.content_hash, revised.requirements_snapshot.content_hash)

    def test_snapshot_builder_also_removes_preloaded_related_comment_artifacts(self):
        payload = sample_issue_payload()
        payload['fields']['comment']['comments'][0]['body'] = 'COMMENT_SCOPE_SENTINEL'
        issue = normalize_issue(payload, 'https://jira.example.test')
        issue.children = [RelatedIssue(
            identifier='T-2', comments=issue.comments,
            relation='child', direction='child',
            source=RequirementSource(
                issue_identifier='T-2', source_type='relation', source_id='child:T-2',
            ),
            requirements=issue.requirements_snapshot.comments,
        )]
        snapshot = build_requirements_snapshot(
            issue, payload, JiraRequirementsConfig(include_comments=False),
        )
        self.assertEqual(snapshot.comments, [])
        self.assertEqual(snapshot.children[0].comments, [])
        self.assertEqual(snapshot.children[0].requirements, [])
        self.assertNotIn('COMMENT_SCOPE_SENTINEL', canonical_requirements_snapshot_json(snapshot))

    def test_comment_endpoints_are_not_called_and_missing_comment_fields_do_not_block(self):
        async def run():
            requests = []
            root_payload = sample_issue_payload()
            del root_payload['fields']['comment']
            root_payload['fields']['customfield_123'] = 'Required acceptance criteria.'
            root_payload['fields']['parent'] = {'id': '10002', 'key': 'T-2'}
            parent_payload = deepcopy(root_payload)
            parent_payload.update(id='10002', key='T-2')
            parent_payload['fields']['parent'] = None

            async def handler(request):
                requests.append(request)
                if request.url.path.endswith('/comment'):
                    return httpx.Response(403)
                payload = parent_payload if request.url.path.endswith('/T-2') else root_payload
                return httpx.Response(200, json=payload)

            config = TrackerConfig(base_url='https://jira.example.test', requirements={
                'include_comments': False, 'acceptance_criteria_fields': ['customfield_123'],
            })
            async with JiraClient(config, environ={'JIRA_TOKEN': 'token'},
                                  transport=httpx.MockTransport(handler)) as client:
                issue = await client.get_issue('T-1')
            self.assertFalse(any(request.url.path.endswith('/comment') for request in requests))
            self.assertNotIn('comment', requests[0].url.params['fields'].split(','))
            self.assertIsNotNone(issue.requirements_snapshot)
            self.assertEqual(issue.requirements_snapshot.incomplete_reasons, [])
            self.assertEqual(issue.requirements_snapshot.comments, [])
            self.assertEqual(issue.requirements_snapshot.parent.comments, [])
        asyncio.run(run())
