import importlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_agent.agents'),
                             'Agent configuration is not implemented')
        self.agents = importlib.import_module('local_agent.agents')

    def test_builtin_configs_preserve_modes_and_budget_single_source(self):
        file_agent = self.agents.builtin_agent('file')
        directory = self.agents.builtin_agent('directory')
        self.assertEqual(file_agent.id, 'file-qa')
        self.assertEqual(directory.id, 'directory-qa')
        changed = directory.to_dict()
        changed['id'] = 'small-project'
        changed['budgets']['max_files'] = 2
        changed['budgets']['max_steps'] = 3
        changed['budgets']['max_file_bytes'] = 5
        agent = self.agents.AgentDefinition.from_dict(changed)
        with tempfile.TemporaryDirectory() as tmp:
            engine = self.agents.build_file_engine(agent, Path(tmp), None)
            prompt = engine.policy.initial_messages('查找项目', None)[0]['content']
            self.assertIn('最多成功读取2个不同文件', prompt)
            self.assertNotIn('最多成功读取4个不同文件', prompt)
            self.assertEqual(engine.policy.tool.max_files, 2)
            (Path(tmp) / 'big.md').write_text('longer than allowed')
            listed = engine.policy.tool.execute('list_files', {'path': '.'})
            engine.policy.tool.record('list_files', {'path': '.'}, listed)
            read = engine.policy.tool.execute('read_file', {'path': 'big.md'})
            self.assertFalse(read['ok'])
            self.assertEqual(read['error']['code'], 'FILE_TOO_LARGE')
            self.assertEqual(agent.run_config().max_steps, 3)

    def test_snapshot_is_immutable_and_revision_changes_with_behavior(self):
        agent = self.agents.builtin_agent('directory')
        snapshot = agent.to_dict()
        snapshot['tools'].clear()
        self.assertIn('read_file', agent.tools)
        new = agent.to_dict()
        new['instructions'] = '只报告明确发现的风险。'
        other = self.agents.AgentDefinition.from_dict(new)
        self.assertNotEqual(agent.revision, other.revision)
        self.assertEqual(agent.revision,
                         self.agents.AgentDefinition.from_dict(agent.to_dict()).revision)

    def test_invalid_config_and_write_denial_fail_before_execution(self):
        for field, value in [('max_files', True), ('max_steps', 10000),
                             ('max_input_bytes', 999999), ('tool_timeout', 0)]:
            with self.subTest(field=field):
                raw = self.agents.builtin_agent('file').to_dict()
                raw['budgets'][field] = value
                with self.assertRaises(self.agents.AgentError):
                    self.agents.AgentDefinition.from_dict(raw)
        raw = self.agents.builtin_agent('file').to_dict()
        raw['model']['api_key'] = 'not-a-real-secret'
        with self.assertRaises(self.agents.AgentError):
            self.agents.AgentDefinition.from_dict(raw)
        raw = self.agents.builtin_agent('file').to_dict()
        raw['approval'] = 'deny_writes'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.agents.AgentError):
                self.agents.build_file_engine(
                    self.agents.AgentDefinition.from_dict(raw), Path(tmp), 'note.md', 'report.md')

    def test_catalog_restart_versions_disable_and_invalid_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = self.agents.AgentCatalog(Path(tmp))
            raw = self.agents.builtin_agent('directory').to_dict()
            raw.update(id='my-assistant', name='自定义助手')
            first = catalog.save(raw)
            raw['instructions'] = '按优先级排序。'
            second = catalog.save(raw)
            loaded = self.agents.AgentCatalog(Path(tmp))
            self.assertEqual(loaded.get(first.id).revision, second.revision)
            self.assertEqual(loaded.get(first.id, first.revision).revision, first.revision)
            loaded.set_enabled(first.id, False)
            self.assertFalse(loaded.is_enabled(first.id))
            self.assertEqual(loaded.get(first.id, first.revision).id, first.id)
            with self.assertRaises(self.agents.AgentError):
                loaded.get('../outside')

    def test_runtime_uses_configured_prompt_budget_and_actual_tool_result(self):
        from local_agent.runtime import Runtime
        from local_agent.provider import ModelReply
        from local_agent.trace import Trace
        raw = self.agents.builtin_agent('file').to_dict()
        raw.update(id='custom-reader', instructions='这是一份自定义阅读约定。')
        raw['budgets']['max_steps'] = 2
        agent = self.agents.AgentDefinition.from_dict(raw)
        requests = []

        class Model:
            metadata = {'provider': 'test', 'simulated': True}
            def complete(self, messages, tools, timeout):
                requests.append(messages)
                if len(requests) == 1:
                    return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [
                        {'id': 'read-1', 'type': 'function', 'function': {'name': 'read_file',
                         'arguments': json.dumps({'path': 'note.md', 'intent': '读取原文'})}}]})
                return ModelReply({'role': 'assistant', 'content': json.dumps({
                    'status': 'answered', 'answer': '项目为青禾。',
                    'citations': [{'path': 'note.md', 'start_line': 1, 'end_line': 1}]})})

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'workspace'
            workspace.mkdir()
            (workspace / 'note.md').write_text('项目为青禾。\n')
            runtime = Runtime(Model(), self.agents.build_file_engine(agent, workspace, 'note.md'),
                              Trace(Path(tmp) / 'runs', workspace))
            result = runtime.run('项目是什么？', 'note.md')
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(runtime.config.max_steps, 2)
        self.assertIn(raw['instructions'], requests[0][0]['content'])
        self.assertNotIn('项目为青禾', json.dumps(requests[0], ensure_ascii=False))
        returned = next(m for m in requests[1] if m['role'] == 'tool')
        self.assertEqual(returned['tool_call_id'], 'read-1')
        self.assertIn('项目为青禾', returned['content'])
