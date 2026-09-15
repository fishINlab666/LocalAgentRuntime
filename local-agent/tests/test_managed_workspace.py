from dataclasses import FrozenInstanceError, replace
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from local_agent.imports import ImportStore, ImportStoreError
from local_agent.session_store import SessionStore
from local_agent.sessions import RunSubmission, SessionError, SessionScope, SessionService
from local_agent.state_maintenance import StateBusy
from local_agent.trace import Trace
from test_runtime import ScriptedProvider, call_message, final_message


def publish_import(store):
    imports = ImportStore(store)
    raw = '代号：orange-731\n'.encode()
    request = {'kind': 'file', 'name': '导入资料', 'agent_id': 'directory-qa',
               'files': [{'logical_path': 'notes.txt', 'bytes': len(raw)}], 'ignored': []}
    batch = imports.begin(request)
    imports.add_file(batch.id, batch.files[0].slot_id, BytesIO(raw), len(raw))
    return imports.start_finalize(batch.id).run(), request


def imported_provider(published):
    chunk = published.chunk_paths[0].relative_to(published.workspace).as_posix()
    answer = final_message()
    value = json.loads(answer['content'])
    value['citations'][0]['path'] = chunk
    answer['content'] = json.dumps(value)
    return ScriptedProvider([
        call_message('root', path='.', name='list_files'),
        call_message('docs', path='documents', name='list_files'),
        call_message('source', path=str(Path(chunk).parent), name='list_files'),
        call_message('read', path=chunk), answer])


class LinkCrash(BaseException):
    pass


class ManagedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SessionStore.open(self.root / 'state')
        self.addCleanup(lambda: self.store.close())
        self.service = SessionService(self.store)
        self.secret = self.root / 'secret'
        self.secret.mkdir()

    def ready(self):
        published, request = publish_import(self.store)
        self.assertTrue(callable(getattr(self.service, 'attach_import', None)),
                        'SessionService.attach_import is missing')
        session = self.service.attach_import(published, request)
        return published, request, session

    def prepare(self, session, *, task='files', output=None, request_id='request'):
        return self.service.submit(session.id, RunSubmission(
            request_id, '读取代号并引用原文。', task, session.scope, output, None, {}))

    def trace(self, prepared, workspace):
        return Trace(self.root / 'runs', workspace, run_id=prepared.run_id)

    def test_attach_is_atomic_idempotent_and_freezes_agent_and_parser_records(self):
        published, request, session = self.ready()
        again = self.service.attach_import(published, request)
        self.assertEqual(again.id, session.id)
        self.assertEqual(session.import_id, published.import_id)
        self.assertEqual(session.agent_id, 'directory-qa')
        with self.assertRaises(TypeError):
            session.agent_snapshot['name'] = 'changed'
        row = self.store.connection().execute(
            'SELECT status,workspace_device,workspace_inode,artifact_device,artifact_inode '
            'FROM imports WHERE id=?', (published.import_id,)).fetchone()
        self.assertEqual(row, ('ready', *published.identities['workspace'],
                              *published.identities['artifacts']))
        parsers = self.store.connection().execute(
            'SELECT parser_json FROM import_files WHERE import_id=?',
            (published.import_id,)).fetchall()
        self.assertEqual([row[0] for row in parsers],
                         [item['parser_json'] for item in published.file_records])
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)
        self.assertEqual(json.loads(published.manifest.read_text())['status'], 'parsed')

    def test_import_session_ignores_forged_database_workspace_path(self):
        published, _, session = self.ready()
        self.store.connection().execute('UPDATE sessions SET workspace_path=? WHERE id=?',
                                       (str(self.secret), session.id))
        resolved = self.service.resolver.resolve(self.service.load(session.id))
        self.assertEqual(resolved.read_root, published.workspace)
        self.assertEqual(resolved.write_root, published.artifacts)
        self.assertEqual(resolved.read_identity, published.identities['workspace'])
        self.assertEqual(resolved.write_identity, published.identities['artifacts'])
        self.assertIsNotNone(resolved.source_mapper)
        self.assertEqual(resolved.source_mapper.fact_paths,
                         tuple(path.relative_to(published.workspace).as_posix()
                               for path in published.chunk_paths))
        with self.assertRaises(FrozenInstanceError):
            resolved.read_root = self.secret

    def test_ordinary_workspace_has_no_import_source_mapper(self):
        workspace = self.root / 'ordinary'
        workspace.mkdir()
        session = self.service.create(workspace, '普通目录', SessionScope('directory', None))
        self.assertIsNone(self.service.resolver.resolve(session).source_mapper)

    def test_other_import_id_and_noncanonical_id_cannot_resolve_session(self):
        _, _, session = self.ready()
        other, _, _ = self.ready()
        for import_id in (other.import_id, '../secret', 'A' * 32, '0' * 32):
            with self.subTest(import_id=import_id), self.assertRaises(SessionError):
                self.service.resolver.resolve(replace(session, import_id=import_id))

    def test_only_ready_import_can_open(self):
        published, _ = publish_import(self.store)
        imports = ImportStore(self.store)
        with self.assertRaises(ImportStoreError) as caught:
            imports.open(published.import_id)
        self.assertEqual(caught.exception.code, 'IMPORT_NOT_READY')

    def test_tamper_marks_unavailable_and_history_stays_readable(self):
        published, _, session = self.ready()
        published.chunk_paths[0].write_text('forged\n')
        with self.assertRaises(SessionError) as caught:
            self.prepare(session)
        self.assertEqual(caught.exception.code, 'IMPORT_INTEGRITY_ERROR')
        self.assertEqual(self.store.connection().execute(
            'SELECT status FROM imports WHERE id=?', (published.import_id,)).fetchone()[0], 'unavailable')
        self.assertEqual(self.service.load(session.id).id, session.id)
        with self.assertRaises(ImportStoreError) as caught:
            self.service.imports.open(published.import_id)
        self.assertEqual(caught.exception.code, 'IMPORT_UNAVAILABLE')
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)

    def test_directory_replacement_and_symlinks_are_integrity_failures(self):
        for mode in ('copy', 'symlink'):
            with self.subTest(mode=mode):
                published, _, session = self.ready()
                moved = published.root / 'moved'
                published.workspace.rename(moved)
                if mode == 'copy':
                    shutil.copytree(moved, published.workspace)
                else:
                    published.workspace.symlink_to(moved, target_is_directory=True)
                with self.assertRaises(SessionError) as caught:
                    self.service.resolver.resolve(session)
                self.assertEqual(caught.exception.code, 'IMPORT_INTEGRITY_ERROR')

    def test_direct_execute_rechecks_integrity_and_persists_failure_before_provider(self):
        published, _, session = self.ready()
        prepared = self.prepare(session)
        published.chunk_paths[0].write_text('forged\n')
        provider = ScriptedProvider([])
        result = self.service.execute(prepared, provider, self.trace(prepared, published.workspace))
        self.assertEqual((result['state'], result['stop_reason'], result['model_calls']),
                         ('failed', 'IMPORT_INTEGRITY_ERROR', 0))
        self.assertEqual(provider.requests, [])
        stored = self.service.load_run(session.id, prepared.run_id)
        self.assertEqual((stored.state, stored.stop_reason), ('failed', 'IMPORT_INTEGRITY_ERROR'))
        with self.store.maintenance_gate.maintenance():
            pass

    def test_import_output_resolves_to_separate_artifact_root_without_writing(self):
        published, _, session = self.ready()
        resolutions = []
        resolve = self.service.resolver.resolve

        def capture(record):
            resolved = resolve(record)
            resolutions.append(resolved)
            return resolved

        with patch.object(self.service.resolver, 'resolve', side_effect=capture):
            prepared = self.prepare(session, output='answer.md')

        self.assertEqual(prepared.submission.output_path, 'answer.md')
        self.assertEqual(len(resolutions), 1)
        resolved = resolutions[0]
        self.assertEqual(resolved.read_root, published.workspace)
        self.assertEqual(resolved.write_root, published.artifacts)
        self.assertNotEqual(resolved.read_root, resolved.write_root)
        self.assertEqual(resolved.read_identity, published.identities['workspace'])
        self.assertEqual(resolved.write_identity, published.identities['artifacts'])
        self.assertNotEqual(resolved.read_identity, resolved.write_identity)
        self.assertFalse((published.workspace / 'answer.md').exists())
        self.assertFalse((published.artifacts / 'answer.md').exists())

    def test_submit_resolves_under_short_gate_outside_run_transaction(self):
        _, _, session = self.ready()
        resolve = self.service.resolver.resolve
        def checked(record):
            self.assertFalse(self.store.connection().in_transaction)
            with self.assertRaises(StateBusy), self.store.maintenance_gate.maintenance():
                pass
            return resolve(record)
        with patch.object(self.service.resolver, 'resolve', side_effect=checked):
            self.prepare(session)
        with self.store.maintenance_gate.maintenance():
            pass

    def test_conversation_never_resolves_unavailable_import(self):
        published, _, session = self.ready()
        published.chunk_paths[0].unlink()
        with patch.object(self.service.resolver, 'resolve', side_effect=AssertionError('must not resolve')):
            prepared = self.prepare(session, task='conversation')
            message = self.store.load_run_messages(session.id, prepared.run_id)[0]
            provider = ScriptedProvider([{'role': 'assistant', 'content': json.dumps({
                'status': 'answered', 'answer': '你要求读取代号。', 'references': [{
                    'message_id': message.id, 'start': 0, 'end': 4}]})}])
            complete = provider.complete
            def checked(*args, **kwargs):
                with self.assertRaises(StateBusy), self.store.maintenance_gate.maintenance():
                    pass
                return complete(*args, **kwargs)
            with patch.object(provider, 'complete', side_effect=checked):
                result = self.service.execute(prepared, provider, self.trace(prepared, published.workspace))
        self.assertEqual(result['state'], 'completed')
        with self.store.maintenance_gate.maintenance():
            pass

    def test_execute_uses_trusted_root_and_holds_gate_through_provider(self):
        published, _, session = self.ready()
        self.store.connection().execute('UPDATE sessions SET workspace_path=? WHERE id=?',
                                       (str(self.secret), session.id))
        session = self.service.load(session.id)
        prepared = self.prepare(session)
        chunk = published.chunk_paths[0].relative_to(published.workspace).as_posix()
        answer = final_message()
        value = json.loads(answer['content'])
        value['citations'][0]['path'] = chunk
        answer['content'] = json.dumps(value)
        replies = [call_message('root', path='.', name='list_files'),
                   call_message('docs', path='documents', name='list_files'),
                   call_message('source', path=str(Path(chunk).parent), name='list_files'),
                   call_message('read', path=chunk),
                   answer]
        provider = ScriptedProvider(replies)
        complete = provider.complete
        def checked(*args, **kwargs):
            with self.assertRaises(StateBusy), self.store.maintenance_gate.maintenance():
                pass
            return complete(*args, **kwargs)
        with patch.object(provider, 'complete', side_effect=checked):
            result = self.service.execute(prepared, provider, self.trace(prepared, published.workspace))
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(result['answer']['citations'][0]['quote'], '代号：orange-731')
        with self.store.maintenance_gate.maintenance():
            pass

    def test_link_faults_recover_without_reparse_or_duplicate_session(self):
        for point, expected_count in (('before_link_transaction', 0), ('after_link_commit', 1)):
            with self.subTest(point=point):
                published, request = publish_import(self.store)
                self.assertTrue(callable(getattr(self.service, 'attach_import', None)))
                before = self.store.connection().execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
                def fail(at):
                    if at == point:
                        raise LinkCrash()
                self.service._fault = fail
                with self.assertRaises(LinkCrash):
                    self.service.attach_import(published, request)
                self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM sessions').fetchone()[0],
                                 before + expected_count)
                self.store.close()
                self.store = SessionStore.open(self.root / 'state')
                self.service = SessionService(self.store)
                with patch('local_agent.imports.parse_document_in_worker', side_effect=AssertionError('reparse')):
                    self.service.recover_imports()
                    first = self.service.attach_import(published, request)
                    second = self.service.attach_import(published, request)
                self.assertEqual(first.id, second.id)
                self.assertEqual(self.store.connection().execute(
                    'SELECT COUNT(*) FROM sessions WHERE import_id=?', (published.import_id,)).fetchone()[0], 1)

    def test_attach_rejects_changed_request_and_forged_publication(self):
        published, request, _ = self.ready()
        for candidate, body in ((published, {**request, 'name': 'changed'}),
                                (replace(published, workspace=self.secret), request)):
            with self.subTest(candidate=candidate.workspace, name=body['name']), self.assertRaises(SessionError):
                self.service.attach_import(candidate, body)

    def test_ready_complete_and_job_run_reuse_publication_and_same_session(self):
        published, request, session = self.ready()
        with patch('local_agent.imports.parse_document_in_worker', side_effect=AssertionError('reparse')):
            try:
                job = self.service.imports.start_finalize(published.import_id)
            except ImportStoreError as error:
                self.fail('ready complete must reuse the original job: ' + error.code)
            self.assertEqual(job.job_id, published.job_id)
            again = job.run()
            self.assertEqual(self.service.attach_import(again, request).id, session.id)
        with self.assertRaises(ImportStoreError):
            self.service.imports.cancel(published.import_id)
        self.assertEqual(self.service.imports.snapshot(published.import_id).status, 'ready')

    def test_workspace_swap_during_assembly_cannot_grant_a_new_root(self):
        from local_agent.agent_runtime import assemble
        published, _, session = self.ready()
        prepared = self.prepare(session)
        moved = self.root / 'original-workspace'
        def switched(agent, workspace, *args, **kwargs):
            published.workspace.rename(moved)
            published.workspace.symlink_to(self.secret, target_is_directory=True)
            try:
                return assemble(agent, workspace, *args, **kwargs)
            finally:
                published.workspace.unlink()
                moved.rename(published.workspace)
        provider = ScriptedProvider([])
        with patch('local_agent.agent_runtime.assemble', side_effect=switched):
            result = self.service.execute(prepared, provider, self.trace(prepared, published.workspace))
        self.assertEqual(result['stop_reason'], 'IMPORT_INTEGRITY_ERROR')
        self.assertEqual(provider.requests, [])

    def test_partial_link_transaction_rolls_back_parser_records_and_session(self):
        published, request = publish_import(self.store)
        self.store.connection().execute(
            "CREATE TRIGGER reject_session BEFORE INSERT ON sessions BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(SessionError):
            self.service.attach_import(published, request)
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        self.assertEqual(self.store.connection().execute(
            'SELECT parser_json FROM import_files WHERE import_id=?', (published.import_id,)).fetchone()[0], None)
        self.assertEqual(self.service.imports.snapshot(published.import_id).status, 'finalizing')

    def test_disabled_import_agent_does_not_block_startup_and_can_recover_later(self):
        from local_agent.__main__ import _open_service
        from local_agent.agents import AgentCatalog
        (self.secret / 'note.md').write_text('existing\n')
        existing = self.service.create(self.secret, 'existing', SessionScope('file', 'note.md'))
        published, _ = publish_import(self.store)
        catalog = AgentCatalog(self.store.state_dir / 'agents')
        catalog.set_enabled('directory-qa', False)
        for _ in range(2):
            self.store.close()
            self.store, self.service = _open_service(self.root / 'state')
            self.assertEqual(self.service.load(existing.id).title, 'existing')
            self.assertEqual(self.store.connection().execute(
                'SELECT status,error_code FROM imports WHERE id=?',
                (published.import_id,)).fetchone(), ('finalizing', 'AGENT_DISABLED'))
            self.assertEqual(self.store.connection().execute(
                'SELECT COUNT(*) FROM sessions WHERE import_id=?',
                (published.import_id,)).fetchone()[0], 0)
        catalog.set_enabled('directory-qa', True)
        with patch('local_agent.imports.parse_document_in_worker', side_effect=AssertionError('reparse')):
            for _ in range(2):
                self.store.close()
                self.store, self.service = _open_service(self.root / 'state')
        self.assertEqual(self.store.connection().execute(
            'SELECT status,error_code FROM imports WHERE id=?',
            (published.import_id,)).fetchone(), ('ready', None))
        self.assertEqual(self.store.connection().execute(
            'SELECT COUNT(*) FROM sessions WHERE import_id=?',
            (published.import_id,)).fetchone()[0], 1)

    def test_import_recovery_does_not_swallow_global_link_errors(self):
        publish_import(self.store)
        for code in ('SESSION_STORE_ERROR', 'STATE_BUSY'):
            with self.subTest(code=code), patch.object(
                    self.service, 'attach_import', side_effect=SessionError(code)):
                with self.assertRaises(SessionError) as caught:
                    self.service.recover_imports()
                self.assertEqual(caught.exception.code, code)
