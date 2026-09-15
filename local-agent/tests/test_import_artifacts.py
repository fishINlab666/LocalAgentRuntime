import copy
import hashlib
from io import BytesIO
import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest

from local_agent.agents import AgentError, build_file_engine, builtin_agent
from local_agent.approvals import ApprovalBroker
from local_agent.imports import ImportStore
from local_agent.provider import ModelReply
from local_agent.session_store import SessionStore
from local_agent.sessions import RunSubmission, SessionError, SessionScope, SessionService
from local_agent.trace import Trace


def tool_call(call_id, name, arguments):
    return {
        'role': 'assistant',
        'content': None,
        'tool_calls': [{
            'id': call_id,
            'type': 'function',
            'function': {
                'name': name,
                'arguments': json.dumps(
                    {**arguments, 'intent': '核对导入资料并生成指定文件'},
                    ensure_ascii=False,
                ),
            },
        }],
    }


def tool_calls(items):
    return {
        'role': 'assistant',
        'content': None,
        'tool_calls': [
            tool_call(call_id, name, arguments)['tool_calls'][0]
            for call_id, name, arguments in items
        ],
    }


def final_answer(path):
    return {
        'role': 'assistant',
        'content': json.dumps({
            'status': 'answered',
            'answer': '已依据导入资料生成报告。',
            'citations': [{
                'path': path,
                'start_line': 1,
                'end_line': 1,
                'quote': '代号：orange-731',
            }],
        }, ensure_ascii=False),
    }


def unable_answer():
    return {
        'role': 'assistant',
        'content': json.dumps({
            'status': 'unable',
            'answer': '未创建文件。',
            'citations': [],
        }, ensure_ascii=False),
    }


class RecordingProvider:
    metadata = {'provider': 'artifact-test', 'model': 'none', 'simulated': True}

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages, tools, timeout):
        self.requests.append({
            'messages': copy.deepcopy(messages),
            'tools': copy.deepcopy(tools),
        })
        return ModelReply(next(self.replies))


class ImportedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SessionStore.open(self.root / 'state')
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)
        self.published, self.session = self.publish_import()
        self.chunk = self.published.chunk_paths[0].relative_to(
            self.published.workspace
        ).as_posix()

    def publish_import(self, *, logical_path='notes.txt', display_name='导入资料'):
        raw = '代号：orange-731\n'.encode()
        request = {
            'kind': 'file',
            'name': display_name,
            'agent_id': 'directory-qa',
            'files': [{'logical_path': logical_path, 'bytes': len(raw)}],
            'ignored': [],
        }
        imports = ImportStore(self.store)
        batch = imports.begin(request)
        imports.add_file(
            batch.id, batch.files[0].slot_id, BytesIO(raw), len(raw)
        )
        published = imports.start_finalize(batch.id).run()
        return published, self.service.attach_import(published, request)

    def prepare(self, output_path, *, request_id='artifact-request'):
        try:
            return self.service.submit(self.session.id, RunSubmission(
                request_id,
                '读取代号并生成报告，引用原文。',
                'files',
                self.session.scope,
                output_path,
                None,
                {'max_steps': 6},
            ))
        except SessionError as error:
            self.fail(
                '导入会话应接受用户预先声明的唯一输出目标，'
                f'实际返回 {error.code}'
            )

    def run_import(self, replies, *, output_path='report.md', decision='allow',
                   request_id='artifact-request'):
        prepared = self.prepare(output_path, request_id=request_id)
        provider = RecordingProvider(replies)
        approvals = ApprovalBroker(prepared.run_id, journal=prepared.journal)
        self.addCleanup(approvals.close)
        approval_events = []

        def resolve(event, data):
            if event == 'approval.required':
                approval_events.append(copy.deepcopy(data))
                approvals.decide(data['id'], decision)

        approvals.publish = resolve
        result = self.service.execute(
            prepared,
            provider,
            Trace(
                self.root / 'runs',
                self.published.workspace,
                run_id=prepared.run_id,
            ),
            approvals=approvals,
        )
        return result, prepared, provider, approval_events

    def success_replies(self, *, content='# 核对报告\n\n代号：orange-731\n'):
        return [
            tool_call('read', 'read_file', {'path': self.chunk}),
            tool_call('write', 'write_file', {
                'path': 'report.md',
                'content': content,
            }),
            final_answer(self.chunk),
        ]

    def create_approved_artifact(self):
        result, prepared, _, events = self.run_import(self.success_replies())
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(len(events), 1)
        view = self.service.run_view(self.session.id, prepared.run_id)
        self.assertEqual(len(view['artifacts']), 1)
        return prepared, view['artifacts'][0]

    def test_undeclared_output_never_registers_write_file(self):
        prepared = self.prepare(None, request_id='no-output')
        provider = RecordingProvider([
            tool_call('read', 'read_file', {'path': self.chunk}),
            final_answer(self.chunk),
        ])

        result = self.service.execute(
            prepared,
            provider,
            Trace(
                self.root / 'runs',
                self.published.workspace,
                run_id=prepared.run_id,
            ),
        )

        self.assertEqual(result['state'], 'completed', result)
        names = [tool['function']['name'] for tool in provider.requests[0]['tools']]
        self.assertNotIn('write_file', names)
        self.assertEqual(list(self.published.artifacts.iterdir()), [])
        self.assertEqual(
            self.store.connection().execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],
            0,
        )

    def test_import_writer_requires_explicit_resolved_write_authority(self):
        resolved = self.service.resolver.resolve(self.session)
        incomplete = (
            {},
            {'write_root': resolved.write_root},
            {'write_root': resolved.write_root,
             'write_identity': resolved.write_identity},
        )
        for authority in incomplete:
            with self.subTest(authority=authority):
                with self.assertRaises(AgentError) as caught:
                    build_file_engine(
                        builtin_agent('directory'), resolved.read_root, None, 'report.md',
                        source_mapper=resolved.source_mapper, **authority)
                self.assertEqual(str(caught.exception), 'IMPORT_INTEGRITY_ERROR')

    def test_approved_write_uses_only_artifact_root_and_persists_real_receipt(self):
        content = '# 核对报告\n\n代号：orange-731\n'
        result, prepared, _, events = self.run_import(
            self.success_replies(content=content)
        )

        artifact_path = self.published.artifacts / 'report.md'
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(len(events), 1)
        self.assertTrue(artifact_path.is_file())
        self.assertFalse((self.published.workspace / 'report.md').exists())
        self.assertEqual(artifact_path.read_text(), content)
        self.assertEqual(len(result['artifacts']), 1)
        self.assertEqual(
            result['artifacts'][0]['sha256'],
            hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        )
        stored = self.service.run_view(self.session.id, prepared.run_id)['artifacts']
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['path'], 'report.md')
        self.assertEqual(stored[0]['sha256'], result['artifacts'][0]['sha256'])

    def test_model_cannot_rename_the_declared_output(self):
        replies = [
            tool_call('read', 'read_file', {'path': self.chunk}),
            tool_call('renamed', 'write_file', {
                'path': 'model-chosen.md',
                'content': '不能写入这里\n',
            }),
            unable_answer(),
        ]

        result, _, _, events = self.run_import(
            replies, request_id='model-rename'
        )

        self.assertEqual(result['state'], 'unable', result)
        self.assertEqual(result['stop_reason'], 'PATH_DENIED')
        self.assertEqual(events, [])
        self.assertEqual(list(self.published.artifacts.iterdir()), [])
        self.assertEqual(
            self.store.connection().execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],
            0,
        )

    def test_rejected_write_has_no_file_or_artifact_receipt(self):
        result, _, _, events = self.run_import(
            self.success_replies(), decision='deny', request_id='rejected'
        )

        self.assertEqual(result['state'], 'unable', result)
        self.assertEqual(result['stop_reason'], 'USER_REJECTED')
        self.assertEqual(len(events), 1)
        self.assertEqual(list(self.published.artifacts.iterdir()), [])
        self.assertEqual(
            self.store.connection().execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],
            0,
        )

    def test_one_run_can_publish_once_and_cannot_overwrite_it(self):
        first = '# 第一版\n\n代号：orange-731\n'
        second = '# 被拒绝的覆盖版\n\n代号：orange-731\n'
        replies = [
            tool_call('read', 'read_file', {'path': self.chunk}),
            tool_calls([
                ('write-first', 'write_file', {
                    'path': 'report.md', 'content': first,
                }),
                ('write-again', 'write_file', {
                    'path': 'report.md', 'content': second,
                }),
            ]),
            final_answer(self.chunk),
        ]

        result, prepared, _, events = self.run_import(
            replies, request_id='write-once'
        )

        artifact_path = self.published.artifacts / 'report.md'
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(len(events), 1)
        self.assertEqual(artifact_path.read_text(), first)
        self.assertEqual(len(result['artifacts']), 1)
        self.assertEqual(
            len(self.service.run_view(self.session.id, prepared.run_id)['artifacts']),
            1,
        )

    def test_artifact_writer_refuses_a_symbolic_link_target(self):
        parameters = inspect.signature(build_file_engine).parameters
        self.assertIn('write_root', parameters)
        self.assertIn('write_identity', parameters)
        resolved = self.service.resolver.resolve(self.session)
        engine = build_file_engine(
            builtin_agent('directory'),
            resolved.read_root,
            None,
            'report.md',
            write_root=resolved.write_root,
            read_identity=resolved.read_identity,
            write_identity=resolved.write_identity,
            source_mapper=resolved.source_mapper,
        )
        writer = engine.registry.get('write_file')
        self.assertIsNotNone(writer)
        outside = self.root / 'outside.md'
        outside.write_text('不可改动\n')
        (self.published.artifacts / 'report.md').symlink_to(outside)
        arguments = {'path': 'report.md', 'content': '恶意替换\n'}

        rejected = writer.validate(arguments)

        self.assertIs(rejected['ok'], False)
        self.assertEqual(rejected['error']['code'], 'FILE_EXISTS')
        self.assertEqual(outside.read_text(), '不可改动\n')
        self.assertTrue((self.published.artifacts / 'report.md').is_symlink())

    def test_open_artifact_checks_full_ownership_and_current_digest(self):
        prepared, artifact = self.create_approved_artifact()
        self.assertTrue(
            callable(getattr(self.service, 'open_artifact', None)),
            'SessionService.open_artifact is missing',
        )

        opened = self.service.open_artifact(
            self.session.id,
            prepared.run_id,
            artifact['id'],
            self.session.agent_id,
        )
        self.assertEqual(opened.download_name, 'report.md')
        self.assertEqual(opened.length, artifact['bytes'])
        self.assertEqual(opened.content, (
            self.published.artifacts / 'report.md'
        ).read_bytes())

        other_workspace = self.root / 'other-workspace'
        other_workspace.mkdir()
        other_session = self.service.create(
            other_workspace,
            '其他会话',
            SessionScope('directory', None),
        )
        denied = (
            (other_session.id, prepared.run_id, artifact['id'], self.session.agent_id),
            (self.session.id, '0' * 32, artifact['id'], self.session.agent_id),
            (self.session.id, prepared.run_id, '0' * 32, self.session.agent_id),
            (self.session.id, prepared.run_id, artifact['id'], 'file-qa'),
        )
        for arguments in denied:
            with self.subTest(arguments=arguments), self.assertRaises(SessionError) as caught:
                self.service.open_artifact(*arguments)
            self.assertEqual(caught.exception.code, 'NOT_FOUND')

        self.store.connection().execute(
            "UPDATE artifacts SET recovery_state='unknown' WHERE id=?",
            (artifact['id'],),
        )
        with self.assertRaises(SessionError) as caught:
            self.service.open_artifact(
                self.session.id,
                prepared.run_id,
                artifact['id'],
                self.session.agent_id,
            )
        self.assertEqual(caught.exception.code, 'IMPORT_INTEGRITY_ERROR')
        self.store.connection().execute(
            "UPDATE artifacts SET recovery_state='confirmed' WHERE id=?",
            (artifact['id'],),
        )

        path = self.published.artifacts / artifact['path']
        original = path.read_bytes()
        path.write_bytes(b'x' * len(original))
        with self.assertRaises(SessionError) as caught:
            self.service.open_artifact(
                self.session.id,
                prepared.run_id,
                artifact['id'],
                self.session.agent_id,
            )
        self.assertEqual(caught.exception.code, 'IMPORT_INTEGRITY_ERROR')


if __name__ == '__main__':
    unittest.main()
