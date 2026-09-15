import json
from pathlib import Path
import tempfile
import unittest

from local_agent.agents import AgentDefinition, build_file_engine, builtin_agent
from local_agent.agent_runtime import CapabilityStore, assemble
from local_agent.answers import AnswerError


class DynamicWritePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'note.md').write_text('项目：青禾-47\n')

    def test_write_permission_not_output_hint_controls_registration(self):
        for mode in ('file', 'directory', 'combined'):
            raw = builtin_agent(mode).to_dict()
            target = 'note.md' if mode == 'file' else None
            with self.subTest(mode=mode):
                engine = build_file_engine(AgentDefinition.from_dict(raw), self.workspace, target)
                self.assertIsNotNone(engine.registry.get('write_file'))
                raw['approval'] = 'deny_writes'
                engine = build_file_engine(AgentDefinition.from_dict(raw), self.workspace, target)
                self.assertIsNone(engine.registry.get('write_file'))
                raw['approval'] = 'ask_writes'
                raw['tools'].remove('write_file')
                engine = build_file_engine(AgentDefinition.from_dict(raw), self.workspace, target)
                self.assertIsNone(engine.registry.get('write_file'))

    def test_another_artifact_does_not_satisfy_explicit_output_requirement(self):
        for mode in ('file', 'combined'):
            with self.subTest(mode=mode):
                target = 'note.md' if mode == 'file' else None
                assembly = assemble(builtin_agent(mode), self.workspace, target, 'required.md',
                                    CapabilityStore(self.root / 'state'), run_id=mode)
                self.addCleanup(assembly.close)
                policy = assembly.engine.policy
                policy.initial_messages('整理并保存', target)
                policy.before('read_file', {'path': 'note.md'})
                policy.snapshots['note.md'] = {'ok': True, 'path': 'note.md',
                                               'content': '项目：青禾-47\n'}
                policy.artifacts.append({'path': 'extra.md', 'bytes': 1,
                                         'sha256': 'a' * 64, 'operation': 'created'})
                final = json.dumps({'status': 'answered', 'answer': '已完成。', 'citations': [
                    {'path': 'note.md', 'start_line': 1, 'end_line': 1}]})
                with self.assertRaises(AnswerError) as caught:
                    policy.validate(final)
                self.assertEqual(caught.exception.code, 'OUTPUT_NOT_CREATED')

    def test_partial_write_failure_can_end_unable_with_previous_artifacts(self):
        engine = build_file_engine(builtin_agent('file'), self.workspace, 'note.md')
        policy = engine.policy
        policy.initial_messages('保存摘要和待办', 'note.md')
        policy.before('read_file', {'path': 'note.md'})
        policy.accept('read_file', {'path': 'note.md'}, {'ok': True, 'path': 'note.md', 'content': '项目：青禾-47\n'})
        policy.accept('write_file', {'path': 'summary.md'}, {'ok': True, 'path': 'summary.md',
                      'bytes': 1, 'sha256': 'a' * 64, 'operation': 'created'})
        policy.accept('write_file', {'path': 'todo.md'}, {'ok': False, 'error': {'code': 'USER_REJECTED'}})
        final = policy.validate(json.dumps({'status': 'unable', 'answer': '摘要已生成，待办被拒绝。', 'citations': []}))
        self.assertEqual(final['status'], 'unable')
        self.assertEqual([a['path'] for a in policy.result_fields()['artifacts']], ['summary.md'])

    def test_generated_outputs_cannot_be_rediscovered_as_factual_sources(self):
        for mode in ('directory', 'combined'):
            with self.subTest(mode=mode):
                assembly = assemble(builtin_agent(mode), self.workspace, None, None,
                                    CapabilityStore(self.root / 'state'), run_id=mode)
                self.addCleanup(assembly.close)
                policy = assembly.engine.policy
                policy.initial_messages('生成摘要', None)
                registry = assembly.engine.registry
                listing = registry.get('list_files')
                initial = listing.execute({'path': '.'})
                policy.accept('list_files', {'path': '.'}, initial)
                source = registry.get('read_file').execute({'path': 'note.md'})
                policy.accept('read_file', {'path': 'note.md'}, source)
                writer = registry.get('write_file')
                path = mode + '-summary.md'
                arguments = {'path': path, 'content': '模型编写的摘要\n'}
                receipt = writer.commit(arguments, writer.execute(arguments))
                self.assertTrue(receipt['ok'])
                policy.accept('write_file', arguments, receipt)
                relisted = listing.execute({'path': '.'})
                # Filtering must preserve the adapter's execution proof as well as its data.
                listing.verify_success({'path': '.'}, {k: v for k, v in relisted.items() if k != 'ok'})
                policy.accept('list_files', {'path': '.'}, relisted)
                self.assertNotIn(path, [item['path'] for item in relisted['entries']])
                denied = registry.get('read_file').execute({'path': path})
                self.assertEqual(denied['error']['code'], 'PATH_DENIED')
                policy.accept('read_file', {'path': path}, denied)
                with self.assertRaises(AnswerError) as caught:
                    policy.validate(json.dumps({'status': 'answered', 'answer': '事实来自摘要。',
                        'citations': [{'path': path, 'start_line': 1, 'end_line': 1}]}))
                self.assertEqual(caught.exception.code, 'INVALID_CITATION')


if __name__ == '__main__':
    unittest.main()
