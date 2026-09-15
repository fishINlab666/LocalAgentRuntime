from pathlib import Path
import tempfile
import unittest

from local_agent.agents import builtin_agent
from local_agent.demo import DemoProvider
from local_agent.web_runs import WebRuns, WebError


class AgentWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.runs = WebRuns(self.workspace, self.root / 'runs', DemoProvider)
        self.addCleanup(self.runs.close)

    def test_create_custom_agent_session_and_filter_ownership(self):
        self.assertTrue(hasattr(self.runs, 'save_agent'), 'assistant management not implemented')
        raw = builtin_agent('directory').to_dict()
        raw.update(id='project-a', name='项目A')
        saved = self.runs.save_agent(raw)['agent']
        created = self.runs.create_session({'title': '持续项目', 'scope': {'mode': 'directory'},
                                          'agent_id': saved['id']})['session']
        self.assertEqual(created['agent_id'], 'project-a')
        self.assertEqual(created['agent_revision'], saved['revision'])
        self.assertEqual(len(self.runs.list_sessions(agent_id='project-a')['sessions']), 1)
        self.assertEqual(self.runs.list_sessions(agent_id='file-qa')['sessions'], [])
        with self.assertRaises(WebError):
            self.runs.require_agent_session(created['id'], 'file-qa')
        self.assertEqual(self.runs.require_agent_session(created['id'], 'project-a').id, created['id'])

    def test_disabled_agent_cannot_create_session(self):
        self.assertTrue(hasattr(self.runs, 'set_agent_enabled'), 'assistant management not implemented')
        self.runs.set_agent_enabled('directory-qa', False)
        with self.assertRaises(WebError):
            self.runs.create_session({'title': '不可执行', 'scope': {'mode': 'directory'},
                                     'agent_id': 'directory-qa'})

    def test_capability_management_preserves_old_session_snapshot(self):
        self.assertTrue(hasattr(self.runs, 'capability_action'))
        old = self.runs.create_session({'title': '原配置', 'scope': {'mode': 'directory'},
                                       'agent_id': 'project-brief'})['session']
        source = Path(__file__).parents[1] / 'examples/skills/project-brief'
        installed = self.runs.capability_action('skill', None, 'install', {'path': str(source)})['skill']
        self.runs.bind_capability('project-brief', {'kind': 'skill', 'id': installed['id'],
                                                  'version': installed['version']})
        fresh = self.runs.create_session({'title': '新配置', 'scope': {'mode': 'directory'},
                                         'agent_id': 'project-brief'})['session']
        self.assertEqual(old['agent_snapshot']['skills'], [])
        self.assertEqual(fresh['agent_snapshot']['skills'][0]['id'], installed['id'])
        self.assertEqual(len(self.runs.list_capabilities()['skills']), 1)
