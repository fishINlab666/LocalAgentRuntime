from contextlib import closing
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from local_agent.session_store import SessionStore, StoreError, _V1_SCHEMA
from local_agent.sessions import RunSubmission, SessionError, SessionScope, SessionService


class AgentSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'note.md').write_text('项目说明\n', encoding='utf-8')
        self.state = self.root / 'state'

    def open_store(self):
        store = SessionStore.open(self.state, clock=lambda: 1234.0)
        self.addCleanup(store.close)
        return store

    def legacy_database(self, *, invalid_scope=False):
        self.state.mkdir(mode=0o700)
        with closing(sqlite3.connect(self.state / 'sessions.sqlite3')) as connection:
            connection.executescript(_V1_SCHEMA)
            for session_id, mode in [('old-file', 'file'), ('old-directory', 'directory')]:
                scope = {'mode': mode, 'target_path': 'note.md' if mode == 'file' else None}
                if invalid_scope and mode == 'directory':
                    scope['mode'] = 'unsupported'
                connection.execute(
                    '''INSERT INTO sessions (id,title,workspace_path,workspace_device,
                       workspace_inode,scope_json,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,'active',1,1)''',
                    (session_id, '旧会话', str(self.workspace), self.workspace.stat().st_dev,
                     self.workspace.stat().st_ino, json.dumps(scope)),
                )
            request = {'question': '旧问题', 'task_type': 'files',
                       'scope': {'mode': 'file', 'target_path': 'note.md'},
                       'output_path': None, 'parent_run_id': None, 'execution_options': {}}
            connection.execute(
                '''INSERT INTO runs (id,session_id,client_request_id,request_json,
                   request_fingerprint,task_type,question,scope_json,state,phase,
                   config_json,system_version,tool_version,protocol_version,started_at)
                   VALUES ('old-run','old-file','old-request',?,'kept','files','旧问题',
                   ?,'failed','done','{}','system-v1','tool-v1','protocol-v1',2)''',
                (json.dumps(request), json.dumps(request['scope'])),
            )
            connection.commit()

    def snapshot(self, *, mode='file', agent_id='custom-reader'):
        self.assertIsNotNone(importlib.util.find_spec('local_agent.agents'),
                             'Agent configuration is not implemented')
        agents = importlib.import_module('local_agent.agents')
        snapshot = agents.builtin_agent(mode).to_dict()
        snapshot.update(id=agent_id, name='自定义阅读助手')
        return snapshot

    @staticmethod
    def submission(scope):
        return RunSubmission('request-1', '检查项目', 'files', scope, None, None,
                             {'max_steps': 3})

    def test_explicit_capability_selection_survives_restart_and_changes_idempotency(self):
        store = self.open_store()
        service = SessionService(store)
        session = service.create(self.workspace, '选择能力', SessionScope('file', 'note.md'))
        submission = RunSubmission('chosen', '检查项目', 'files', session.scope, None, None, {},
                                   skill_id='project-brief',
                                   mcp_prompt={'server_id': 'local-project', 'name': 'brief'})
        prepared = service.submit(session.id, submission)
        store.close()
        reopened = self.open_store()
        restored = SessionService(reopened).load_run(session.id, prepared.run_id).submission
        self.assertEqual(restored.skill_id, 'project-brief')
        self.assertEqual(restored.mcp_prompt['name'], 'brief')
        other = RunSubmission('chosen', '检查项目', 'files', session.scope, None, None, {})
        self.assertNotEqual(restored.fingerprint(), other.fingerprint())

    def test_v1_migration_preserves_history_and_marks_unknown_run_configuration(self):
        self.legacy_database()
        store = self.open_store()
        self.assertEqual(store.user_version(), 2)
        service = SessionService(store)
        self.assertEqual(service.load('old-file').agent_id, 'file-qa')
        self.assertEqual(service.load('old-directory').agent_id, 'directory-qa')
        run = service.load_run('old-file', 'old-run')
        self.assertEqual(run.agent_id, 'file-qa')
        self.assertEqual(run.agent_revision, 'legacy')
        self.assertTrue(run.agent_snapshot['legacy'])
        self.assertEqual(run.agent_snapshot['configuration'], 'unknown')
        self.assertEqual(run.state, 'failed')
        self.assertEqual(run.submission.question, '旧问题')
        backups = list(self.state.glob('sessions.v1.*.sqlite3.backup'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(backup.execute('SELECT request_fingerprint FROM runs').fetchone()[0], 'kept')

    def test_v2_migration_rolls_back_columns_and_data_on_invalid_legacy_scope(self):
        self.legacy_database(invalid_scope=True)
        with self.assertRaisesRegex(StoreError, 'STATE_MIGRATION_FAILED'):
            SessionStore.open(self.state)
        with closing(sqlite3.connect(self.state / 'sessions.sqlite3')) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 1)
            columns = {row[1] for row in connection.execute('PRAGMA table_info(sessions)')}
            self.assertNotIn('agent_id', columns)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 2)

    def test_v0_upgrade_rolls_back_both_schema_steps_if_v2_fails(self):
        self.state.mkdir(mode=0o700)
        database = self.state / 'sessions.sqlite3'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('CREATE TABLE legacy (value TEXT)')
            connection.execute("INSERT INTO legacy VALUES ('kept')")
            connection.commit()
        with patch.object(SessionStore, '_migrate_v2', side_effect=ValueError('injected failure')):
            with self.assertRaisesRegex(StoreError, 'STATE_MIGRATION_FAILED'):
                SessionStore.open(self.state)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                             [('legacy',)])
            self.assertEqual(connection.execute('SELECT value FROM legacy').fetchone()[0], 'kept')

    def test_session_and_run_snapshot_survive_restart_and_are_not_execution_options(self):
        store = self.open_store()
        service = SessionService(store)
        scope = SessionScope('file', 'note.md')
        snapshot = self.snapshot()
        expected = json.loads(json.dumps(snapshot))
        created = service.create(self.workspace, '检查', scope, agent_snapshot=snapshot)
        expected_revision = hashlib.sha256(json.dumps(expected, ensure_ascii=False,
            allow_nan=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(created.agent_revision, expected_revision)
        snapshot['instructions'] = 'later edit'
        prepared = service.submit(created.id, self.submission(scope))
        duplicate = service.submit(created.id, self.submission(scope))
        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.run_id, prepared.run_id)
        row = store.connection().execute(
            'SELECT config_json, agent_snapshot_json FROM runs WHERE id=?',
            (prepared.run_id,),
        ).fetchone()
        self.assertEqual(json.loads(row[0]), {'max_steps': 3})
        self.assertEqual(json.loads(row[1]), expected)
        store.close()
        service = SessionService(self.open_store())
        loaded = service.load(created.id, agent_id=created.agent_id)
        run = service.load_run(created.id, prepared.run_id)
        self.assertEqual(loaded.agent_revision, expected_revision)
        self.assertEqual(json.loads(json.dumps(loaded.agent_snapshot)), expected)
        self.assertEqual(run.agent_snapshot, loaded.agent_snapshot)
        self.assertEqual(run.agent_revision, loaded.agent_revision)
        self.assertEqual(service.run_view(created.id, run.id)['agent_revision'], expected_revision)

    def test_sessions_are_filtered_and_load_checks_requested_agent(self):
        store = self.open_store()
        service = SessionService(store)
        scope = SessionScope('file', 'note.md')
        first = service.create(self.workspace, '一', scope, agent_snapshot=self.snapshot(agent_id='first'))
        second = service.create(self.workspace, '二', scope, agent_snapshot=self.snapshot(agent_id='second'))
        with self.assertRaisesRegex(SessionError, 'NOT_FOUND'):
            service.load(first.id, agent_id='second')
        self.assertEqual([item.id for item in service.list(self.workspace, agent_id='first').items], [first.id])
        info = self.workspace.stat()
        self.assertEqual([item.id for item in service.list_bound(str(self.workspace.resolve()), info.st_dev,
            info.st_ino, agent_id='second').items], [second.id])

    def test_default_creation_binds_compatible_agent_and_invalid_snapshot_is_not_saved(self):
        store = self.open_store()
        service = SessionService(store)
        self.assertEqual(service.create(self.workspace, '默认', SessionScope('directory', None)).agent_id,
                         'directory-qa')
        invalid = self.snapshot()
        invalid['model']['api_key'] = 'not-a-real-secret'
        for snapshot in [invalid, self.snapshot(mode='directory'), {'id': 'incomplete'}]:
            with self.subTest(snapshot=snapshot.get('id')):
                with self.assertRaisesRegex(SessionError, 'AGENT_CONFIG_INVALID'):
                    service.create(self.workspace, '拒绝', SessionScope('file', 'note.md'), agent_snapshot=snapshot)
        self.assertEqual(store.connection().execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
