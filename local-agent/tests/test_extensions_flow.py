import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from local_agent.agent_runtime import CapabilityStore, assemble
from local_agent.agents import AgentCatalog
from local_agent.provider import ModelReply
from local_agent.sessions import SessionService, SessionScope, RunSubmission
from local_agent.session_store import SessionStore
from local_agent.trace import Trace


def call(name, arguments, call_id):
    return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': call_id, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]})


def answer(citations):
    return ModelReply({'role': 'assistant', 'content': json.dumps({
        'status': 'answered', 'answer': '项目青禾-47，完成3项。', 'citations': citations})})


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'note.md').write_text('项目：青禾-47\n')
        self.state = self.root / 'state'
        self.catalog = AgentCatalog(self.state / 'agents')
        self.library = CapabilityStore(self.state)
        self.installed = self.library.skills.install(Path(__file__).parents[1] / 'examples/skills/project-brief')
        self.agent = self.library.bind(self.catalog, 'project-brief', 'skill',
                                       self.installed['id'], self.installed['version'])

    def bind_mcp(self, mode='normal'):
        server = self.library.install_server({'id': 'fixture', 'command': sys.executable,
            'args': [str(Path(__file__).parent / 'fixtures/mcp_server.py'), mode],
            'cwd': str(self.root), 'env_names': [], 'allowed_tools': ['plain'],
            'allowed_resources': [], 'allowed_prompts': [], 'timeout_seconds': 2})
        self.agent = self.library.bind(self.catalog, self.agent.id, 'mcp', server['id'], server['version'])

    def test_persistent_mcp_result_reaches_next_model_with_run_source(self):
        self.bind_mcp()
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        service = SessionService(store)
        session = service.create(self.workspace, '联合', SessionScope('directory', None),
                                 agent_snapshot=self.agent.to_dict())
        prepared = service.submit(session.id, RunSubmission('once', '查询项目', 'files', session.scope, None, None, {}))
        requests = []
        class Model:
            metadata = {'provider': 'scripted', 'simulated': True}
            def complete(inner, messages, tools, timeout):
                requests.append(messages)
                if len(requests) == 1:
                    name = next(t['function']['name'] for t in tools if t['function']['name'].startswith('mcp_'))
                    return call(name, {'intent': '查询状态', 'arguments': {}}, 'remote-1')
                return answer([{'source_id': 'mcp:remote-1', 'start_line': 1, 'end_line': 1}])
        result = service.execute(prepared, Model(), Trace(self.root / 'runs', self.workspace, run_id=prepared.run_id))
        self.assertEqual(result['state'], 'completed', result)
        returned = next(m for m in requests[1] if m['role'] == 'tool')
        self.assertEqual(returned['tool_call_id'], 'remote-1')
        self.assertIn('青禾-47', returned['content'])
        self.assertNotIn('青禾-47', json.dumps(requests[0], ensure_ascii=False))
        citation = result['answer']['citations'][0]
        self.assertEqual(citation['source']['run_id'], prepared.run_id)
        self.assertEqual(citation['quote'], '合成项目 青禾-47 已完成 3 项。')
        store.close()
        restored = SessionStore.open(self.state)
        self.addCleanup(restored.close)
        self.assertEqual(SessionService(restored).load_run(session.id, prepared.run_id).state, 'completed')

    def test_selected_skill_cannot_be_skipped_or_used_as_fact_source(self):
        from local_agent.answers import AnswerError
        assembly = assemble(self.agent, self.workspace, None, None, self.library, run_id='s',
                            selected_skill=self.installed['id'])
        self.addCleanup(assembly.close)
        policy = assembly.engine.policy
        policy.base.snapshots['note.md'] = {'content': '项目：青禾-47'}
        with self.assertRaises(AnswerError) as caught:
            policy.validate(answer([{'path': 'note.md', 'start_line': 1, 'end_line': 1}]).message['content'])
        self.assertEqual(caught.exception.code, 'SKILL_NOT_LOADED')

    def test_http_management_and_wrong_agent_session_header(self):
        from local_agent.web import create_server
        from local_agent.demo import DemoProvider
        server = create_server(self.workspace, self.root / 'logs', state_dir=self.state,
                               port=0, provider_factory=DemoProvider)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join(2)
        self.addCleanup(close)
        def request(method, path, data=None, agent='project-brief'):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'X-Session-Token': server.token, 'Origin': server.origin,
                       'Content-Type': 'application/json', 'X-Agent-ID': agent}
            conn.request(method, path, json.dumps(data) if data is not None else None, headers)
            response = conn.getresponse()
            status, value = response.status, json.loads(response.read())
            conn.close()
            return status, value
        self.assertEqual(request('GET', '/api/agents')[0], 200)
        status, data = request('POST', '/api/sessions', {'title': 'HTTP', 'scope': {'mode': 'directory'},
                                                       'agent_id': 'project-brief'})
        self.assertEqual(status, 201, data)
        session_id = data['session']['id']
        self.assertEqual(request('GET', '/api/sessions/' + session_id, agent='file-qa')[0], 404)
        self.assertEqual(request('POST', '/api/sessions/' + session_id + '/runs',
             {'client_request_id': 'bad', 'task_type': 'files', 'question': '查'}, agent='file-qa')[0], 404)
        self.assertTrue(request('GET', '/api/capabilities')[1]['skills'])

    def test_conversation_permissions_and_budget_cannot_expand_snapshot(self):
        from unittest.mock import patch
        from local_agent.agents import builtin_agent
        raw = builtin_agent('file').to_dict()
        raw['tools'] = ['read_file']
        raw['budgets']['max_steps'] = 2
        raw['instructions'] = 'CUSTOM_METHOD'
        agent = self.catalog.save(raw)
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        service = SessionService(store)
        session = service.create(self.workspace, '权限', SessionScope('file', 'note.md'), agent_snapshot=agent.to_dict())
        prepared = service.submit(session.id, RunSubmission('bad-limit', '历史？', 'conversation',
            session.scope, None, None, {'max_steps': 999}))
        with patch('local_agent.runtime.Runtime') as runtime:
            result = service.execute(prepared, object(), Trace(self.root / 'runs', self.workspace, run_id=prepared.run_id))
            self.assertEqual(result.get('stop_reason'), 'INVALID_CONFIG')
            runtime.assert_not_called()
        prepared = service.submit(session.id, RunSubmission('valid-limit', '历史？', 'conversation',
            session.scope, None, None, {}))
        with patch('local_agent.runtime.Runtime') as runtime:
            runtime.return_value.run.return_value = {'state': 'completed'}
            service.execute(prepared, object(), Trace(self.root / 'runs', self.workspace, run_id=prepared.run_id))
            engine = runtime.call_args.args[1]
            self.assertEqual(engine.registry.schemas(), [])
            self.assertIn('CUSTOM_METHOD', engine.policy.initial_messages('历史？', None)[0]['content'])
            self.catalog.set_enabled(agent.id, False)
            with self.assertRaises(Exception) as caught:
                engine.policy.before('session_history', {})
            self.assertEqual(caught.exception.code, 'AGENT_DISABLED')
