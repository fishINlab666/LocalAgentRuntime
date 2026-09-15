import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_agent.__main__ import main
from local_agent.agents import AgentCatalog, AgentDefinition, builtin_agent
from local_agent.demo import DemoProvider

ROOT = Path(__file__).resolve().parents[1]


class AgentCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'note.md').write_text('项目：青禾-47\n')

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['local_agent', *map(str, args)]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main()
            except SystemExit as error:
                code = error.code
        raw = stdout.getvalue()
        return code, json.loads(raw) if raw else {}, stderr.getvalue()

    def ok(self, *args):
        code, data, stderr = self.invoke(*args)
        self.assertEqual(code, 0, stderr or data)
        return data

    def agent_document(self, agent_id='research', mode='file'):
        document = builtin_agent(mode).to_dict()
        document.update(id=agent_id, name='CLI 测试助手')
        document['model']['name'] = 'stored-model'
        path = self.root / (agent_id + '.json')
        path.write_text(json.dumps(document, ensure_ascii=False))
        return path, document

    def test_agent_import_list_enable_and_disable(self):
        path, _ = self.agent_document()
        imported = self.ok('agents', '--state-dir', self.state, 'import', path)
        self.assertEqual(imported['agent']['id'], 'research')
        listed = self.ok('agents', '--state-dir', self.state, 'list')
        self.assertIn('research', [a['id'] for a in listed['agents']])
        self.ok('agents', '--state-dir', self.state, 'disable', 'research')
        self.assertFalse(AgentCatalog(self.state / 'agents').is_enabled('research'))
        self.ok('agents', '--state-dir', self.state, 'enable', 'research')
        self.assertTrue(AgentCatalog(self.state / 'agents').is_enabled('research'))

    def test_sessions_record_selected_agent_and_reject_cross_agent(self):
        _, document = self.agent_document()
        catalog = AgentCatalog(self.state / 'agents')
        catalog.save(document)
        created = self.ok('sessions', '--state-dir', self.state, 'create', '--agent', 'research',
            '--workspace', self.workspace, '--title', '固定助手', '--file', 'note.md')
        self.assertEqual(created['session']['agent_id'], 'research')
        session_id = created['session_id']
        listed = self.ok('sessions', '--state-dir', self.state, 'list', '--agent', 'file-qa',
                         '--workspace', self.workspace)
        self.assertEqual(listed['sessions'], [])
        code, data, _ = self.invoke('sessions', '--state-dir', self.state, 'show', session_id,
                                   '--agent', 'file-qa')
        self.assertEqual((code, data.get('error')), (2, 'NOT_FOUND'))
        catalog.set_enabled('research', False)
        code, data, _ = self.invoke('sessions', '--state-dir', self.state, 'create', '--agent', 'research',
            '--workspace', self.workspace, '--title', '停用', '--file', 'note.md')
        self.assertEqual((code, data.get('error')), (2, 'AGENT_DISABLED'))

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'optional YAML dependency')
    def test_skill_install_bind_and_disable_use_shared_stores(self):
        from local_agent.agent_runtime import CapabilityStore
        _, document = self.agent_document()
        AgentCatalog(self.state / 'agents').save(document)
        package = self.root / 'method'
        package.mkdir()
        (package / 'SKILL.md').write_text('---\nname: cli-method\ndescription: 核对资料的方法\n---\n核对编号和引用。\n')
        installed = self.ok('capabilities', '--state-dir', self.state, 'install-skill', package)
        version = installed['skill']['version']
        self.ok('agents', '--state-dir', self.state, 'bind', 'research', 'skill', 'cli-method', version)
        self.assertEqual(AgentCatalog(self.state / 'agents').get('research').to_dict()['skills'],
                         [{'id': 'cli-method', 'version': version}])
        self.ok('capabilities', '--state-dir', self.state, 'disable', 'skill', 'cli-method')
        self.assertFalse(CapabilityStore(self.state).skills.list()[0]['enabled'])
        listing = self.ok('capabilities', '--state-dir', self.state, 'list')
        self.assertEqual(listing['skills'][0]['id'], 'cli-method')

    @unittest.skipUnless(importlib.util.find_spec('mcp'), 'optional MCP dependency')
    def test_mcp_install_probe_bind_and_disable(self):
        from local_agent.agent_runtime import CapabilityStore
        _, document = self.agent_document(mode='combined')
        AgentCatalog(self.state / 'agents').save(document)
        config = {'id': 'cli-server', 'command': sys.executable,
            'args': [str(ROOT / 'tests/fixtures/mcp_server.py')], 'cwd': str(self.workspace),
            'env_names': [], 'allowed_tools': ['status'], 'allowed_resources': [],
            'allowed_prompts': [], 'timeout_seconds': 2}
        source = self.root / 'server.json'
        source.write_text(json.dumps(config))
        installed = self.ok('capabilities', '--state-dir', self.state, 'install-mcp', source)
        version = installed['server']['version']
        probe = self.ok('capabilities', '--state-dir', self.state, 'probe', 'cli-server')
        self.assertEqual(sum(t['enabled'] for t in probe['catalog']['tools']), 1)
        self.ok('agents', '--state-dir', self.state, 'bind', 'research', 'mcp', 'cli-server', version)
        self.ok('capabilities', '--state-dir', self.state, 'disable', 'mcp', 'cli-server')
        self.assertFalse(CapabilityStore(self.state).server('cli-server')['enabled'])

    def test_persistent_run_uses_stored_model_after_catalog_revision_changes(self):
        _, document = self.agent_document()
        catalog = AgentCatalog(self.state / 'agents')
        catalog.save(document)
        created = self.ok('sessions', '--state-dir', self.state, 'create', '--agent', 'research',
            '--workspace', self.workspace, '--title', '固定模型', '--file', 'note.md')
        document['model']['name'] = 'new-current-model'
        catalog.save(document)
        selected = []
        def provider(agent):
            selected.append(agent.to_dict()['model']['name'])
            return DemoProvider()
        with patch.object(AgentDefinition, 'provider', autospec=True, side_effect=provider):
            code, result, stderr = self.invoke('run', '--state-dir', self.state,
                '--session', created['session_id'], '--agent', 'research', '--question', '核对',
                '--log-dir', self.root / 'runs')
        self.assertEqual(code, 0, stderr or result)
        self.assertEqual(selected, ['stored-model'])
        self.assertEqual(result['state'], 'completed')

    def test_one_shot_selection_rejected_before_provider_when_not_bound(self):
        _, document = self.agent_document()
        AgentCatalog(self.state / 'agents').save(document)
        with patch.object(AgentDefinition, 'provider', side_effect=AssertionError('provider must not start')):
            code, result, _ = self.invoke('run', '--state-dir', self.state, '--agent', 'research',
                '--workspace', self.workspace, '--file', 'note.md', '--question', '核对',
                '--skill', 'missing', '--log-dir', self.root / 'runs')
        self.assertEqual((code, result.get('error')), (2, 'SKILL_NOT_BOUND'))
        code, result, _ = self.invoke('run', '--state-dir', self.state, '--agent', 'research',
            '--workspace', self.workspace, '--file', 'note.md', '--question', '核对',
            '--mcp-prompt', 'server-only', '--log-dir', self.root / 'runs')
        self.assertEqual((code, result.get('error')), (2, 'SUBMISSION_INVALID'))

    def test_one_shot_closes_assembly_when_approval_setup_fails(self):
        from local_agent.agent_runtime import Assembly
        _, document = self.agent_document()
        AgentCatalog(self.state / 'agents').save(document)
        with patch.object(Assembly, 'close', autospec=True) as closed, \
                patch('local_agent.__main__.ConsoleApprovalBroker', side_effect=ValueError('unavailable')):
            code, result, _ = self.invoke('run', '--state-dir', self.state, '--agent', 'research',
                '--workspace', self.workspace, '--file', 'note.md', '--question', '核对',
                '--output-file', 'report.md', '--log-dir', self.root / 'runs')
        self.assertEqual(code, 2)
        self.assertTrue(closed.called, 'run-owned capabilities were left open')

    def test_one_shot_uses_selected_budget_and_keeps_real_file_loop(self):
        from local_agent.runtime import Runtime
        for mode, expected in [('file', 6), ('combined', 10)]:
            with self.subTest(mode=mode):
                _, document = self.agent_document(agent_id='cli-' + mode, mode=mode)
                AgentCatalog(self.state / 'agents').save(document)
                selection = ['--file', 'note.md'] if mode == 'file' else ['--discover']
                with patch.object(AgentDefinition, 'provider', return_value=DemoProvider()), \
                        patch('local_agent.__main__.Runtime', wraps=Runtime) as constructed:
                    code, result, stderr = self.invoke('run', '--state-dir', self.state,
                        '--agent', 'cli-' + mode, '--workspace', self.workspace, *selection,
                        '--question', '核对项目', '--log-dir', self.root / 'runs')
                self.assertEqual(code, 0, stderr or result)
                self.assertEqual(constructed.call_args.args[3].max_steps, expected)
                self.assertIn('青禾-47', result['answer']['answer'])

    def test_explicit_selections_are_translated_into_durable_submission(self):
        from local_agent.sessions import SessionService
        _, document = self.agent_document()
        AgentCatalog(self.state / 'agents').save(document)
        created = self.ok('sessions', '--state-dir', self.state, 'create', '--agent', 'research',
            '--workspace', self.workspace, '--title', '显式选择', '--file', 'note.md')
        with patch.object(AgentDefinition, 'provider', return_value=DemoProvider()), \
                patch.object(SessionService, 'execute', autospec=True,
                             return_value={'state': 'completed'}) as executed:
            self.ok('run', '--state-dir', self.state, '--session', created['session_id'],
                '--agent', 'research', '--question', '请用这个方法', '--skill', 'method-id',
                '--mcp-prompt', 'server:brief', '--log-dir', self.root / 'runs')
        submission = executed.call_args.args[1].submission
        self.assertEqual(submission.skill_id, 'method-id')
        self.assertEqual(dict(submission.mcp_prompt), {'server_id': 'server', 'name': 'brief'})
