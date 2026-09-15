import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from local_agent.agents import AgentCatalog, builtin_agent


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_agent.agent_runtime'),
                             'extension composition is not implemented')
        self.ext = importlib.import_module('local_agent.agent_runtime')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'note.md').write_text('项目：青禾-47\n')
        self.library = self.ext.CapabilityStore(self.root / 'state')
        self.agents = AgentCatalog(self.root / 'state' / 'agents')

    def test_tool_call_limit_is_explicit_in_the_actual_system_prompt(self):
        assembly = self.ext.assemble(
            builtin_agent('combined'), self.workspace, None, None, self.library,
            run_id='prompt-budget',
        )
        self.addCleanup(assembly.close)
        system = assembly.engine.policy.initial_messages('整理项目', None)[0]['content']
        self.assertIn('每次回复最多 4 个工具请求', system)
        self.assertIn('拆分到下一轮', system)

    def test_output_task_prompt_keeps_written_artifact_out_of_citations(self):
        assembly = self.ext.assemble(
            builtin_agent('combined'), self.workspace, None, 'brief.md', self.library,
            run_id='prompt-output-citation',
        )
        self.addCleanup(assembly.close)
        system = assembly.engine.policy.initial_messages('整理并保存项目简报', None)[0]['content']
        self.assertIn('产物不能作为来源', system)
        self.assertIn('不要在citations中引用output_file', system)

    def test_write_tool_requires_the_users_single_declared_output(self):
        readonly = self.ext.assemble(
            builtin_agent('combined'), self.workspace, None, None, self.library,
            run_id='no-output',
        )
        self.addCleanup(readonly.close)
        self.assertIsNone(readonly.engine.registry.get('write_file'))

        writable = self.ext.assemble(
            builtin_agent('combined'), self.workspace, None, 'brief.md', self.library,
            run_id='declared-output',
        )
        self.addCleanup(writable.close)
        writer = writable.engine.registry.get('write_file')
        self.assertEqual(writer.output_path, 'brief.md')
        self.assertEqual(writer.validate({'path': 'other.md', 'content': 'x'})['error']['code'],
                         'PATH_DENIED')

    def test_extension_prompt_excludes_method_content_from_fact_citations(self):
        assembly = self.ext.assemble(
            builtin_agent('combined'), self.workspace, None, None, self.library,
            run_id='prompt-method-citation',
        )
        self.addCleanup(assembly.close)
        system = assembly.engine.policy.initial_messages('整理项目简报', None)[0]['content']
        self.assertIn('Skill和MCP Prompt不能放入citations', system)
        self.assertIn('MCP Tool或Resource', system)
        self.assertIn('历史引用按下一条规则', system)

    def test_history_result_exposes_exact_source_id_to_the_model(self):
        assembly = self.ext.assemble(
            builtin_agent('combined'), self.workspace, None, None, self.library,
            run_id='history-source-id',
        )
        self.addCleanup(assembly.close)
        policy = assembly.engine.policy
        result = {'ok': True, 'data': {
            'action': 'read', 'message_id': 'message-1', 'source_run_id': 'old-run',
            'source_kind': 'user', 'start': 0, 'end': 4, 'text': '旧约定',
        }}
        request = policy.model_request([{
            'role': 'tool', 'tool_call_id': 'history-1',
            'content': json.dumps(result, ensure_ascii=False),
        }], 65536, [])
        visible = json.loads(request['messages'][0]['content'])['data']
        self.assertEqual(visible.get('source_id'), 'history:message-1:0')
        self.assertEqual(visible.get('citation_line_count'), 1)
        self.assertEqual(visible.get('citation'), {
            'source_id': 'history:message-1:0', 'start_line': 1, 'end_line': 1,
        })
        search = {'ok': True, 'data': {'action': 'search', 'hits': [{
            'message_id': 'message-2', 'source_run_id': 'old-run',
            'session_seq': 1, 'source_kind': 'user', 'start': 7, 'end': 16,
            'excerpt': '新约定\n第二行',
        }]}}
        request = policy.model_request([{
            'role': 'tool', 'tool_call_id': 'history-2',
            'content': json.dumps(search, ensure_ascii=False),
        }], 65536, [])
        hit = json.loads(request['messages'][0]['content'])['data']['hits'][0]
        self.assertEqual(hit.get('source_id'), 'history:message-2:7')
        self.assertEqual(hit.get('citation_line_count'), 2)
        self.assertEqual(hit.get('citation'), {
            'source_id': 'history:message-2:7', 'start_line': 1, 'end_line': 2,
        })
        system = policy.initial_messages('回顾旧约定', None)[0]['content']
        self.assertIn('原样复制历史结果返回的source_id', system)
        self.assertIn('citation_line_count', system)

    def test_invalid_history_citation_can_be_repaired_without_another_tool_call(self):
        from local_agent.answers import AnswerError
        assembly = self.ext.assemble(
            builtin_agent('combined'), self.workspace, None, None, self.library,
            run_id='history-citation-repair',
        )
        self.addCleanup(assembly.close)
        policy = assembly.engine.policy
        policy.sources['history:message-1:0'] = {
            'content': '第一行\n第二行',
            'source': {'type': 'history', 'message_id': 'message-1'},
        }
        with self.assertRaises(AnswerError) as caught:
            policy.validate(json.dumps({
                'status': 'answered',
                'answer': '历史记录内容。',
                'citations': [{
                    'source_id': 'history:message-1:0',
                    'start_line': 1,
                    'end_line': 3,
                }],
            }, ensure_ascii=False))
        self.assertEqual(caught.exception.code, 'INVALID_CITATION')
        self.assertTrue(caught.exception.repairable)

    def test_import_bind_new_revision_and_no_plaintext_credentials(self):
        installed = self.library.skills.install(Path(__file__).parents[1] / 'examples/skills/project-brief')
        agent = self.library.bind(self.agents, 'project-brief', 'skill', installed['id'], installed['version'])
        self.assertEqual(agent.to_dict()['skills'][0]['version'], installed['version'])
        config = {'id': 'local-project', 'command': sys.executable,
                  'args': [str(Path(__file__).parent / 'fixtures/mcp_server.py')],
                  'cwd': str(self.root), 'env_names': [], 'allowed_tools': ['plain'],
                  'allowed_resources': [], 'allowed_prompts': [], 'timeout_seconds': 2}
        server = self.library.install_server(config)
        self.assertEqual(server['version'], self.library.server(server['id'])['version'])
        bad = dict(config, env={'SECRET': 'not-a-real-key'})
        with self.assertRaises(ValueError):
            self.library.install_server(bad)
        bound = self.library.bind(self.agents, agent.id, 'mcp', server['id'], server['version'])
        self.assertEqual(bound.to_dict()['mcp'][0]['tools'], ['plain'])
        self.library.set_server_enabled(server['id'], False)
        with self.assertRaises(ValueError):
            self.library.server(server['id'], server['version'], require_enabled=True)

    def test_skill_actual_result_context_and_fact_answer(self):
        from local_agent.agents import AgentDefinition
        from local_agent.provider import ModelReply
        from local_agent.runtime import Runtime
        from local_agent.trace import Trace
        source = self.root / 'method'
        source.mkdir()
        (source / 'SKILL.md').write_text('---\nname: method\ndescription: 项目资料核对方法\n---\nMETHOD_BODY_MARKER：引用原文。\n')
        installed = self.library.skills.install(source)
        raw = builtin_agent('file').to_dict()
        raw['skills'] = [{'id': installed['id'], 'version': installed['version']}]
        agent = AgentDefinition.from_dict(raw)
        requests = []
        def call(name, args, cid):
            return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]})
        class Model:
            metadata = {'provider': 'test', 'simulated': True}
            def complete(self, messages, tools, timeout):
                requests.append(messages)
                if len(requests) == 1:
                    return call('read_skill', {'skill_id': 'method', 'relative_path': 'SKILL.md', 'intent': '学习方法'}, 'skill-1')
                if len(requests) == 2:
                    return call('read_file', {'path': 'note.md', 'intent': '核对原文'}, 'file-1')
                return ModelReply({'role': 'assistant', 'content': json.dumps({'status': 'answered',
                    'answer': '项目为青禾-47。', 'citations': [{'path': 'note.md', 'start_line': 1, 'end_line': 1}]})})
        assembly = self.ext.assemble(agent, self.workspace, 'note.md', None, self.library,
                                     run_id='test-run')
        try:
            result = Runtime(Model(), assembly.engine, Trace(self.root / 'runs', self.workspace)).run('使用方法核对项目', 'note.md')
        finally:
            assembly.close()
        self.assertEqual(result['state'], 'completed', result)
        self.assertNotIn('METHOD_BODY_MARKER', json.dumps(requests[0]))
        self.assertIn('METHOD_BODY_MARKER', json.dumps(requests[1]))
        self.assertEqual(next(m for m in requests[1] if m['role'] == 'tool')['tool_call_id'], 'skill-1')
        self.assertEqual(result['answer']['citations'][0]['quote'], '项目：青禾-47')
