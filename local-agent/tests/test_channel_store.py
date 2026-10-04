from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from local_agent.session_store import SessionStore, StoreError, _V1_SCHEMA
from test_session_store import insert_run, insert_session


class ChannelMigrationTests(unittest.TestCase):
    def test_v3_migration_preserves_session_and_private_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'state'
            state.mkdir()
            with closing(sqlite3.connect(state / 'sessions.sqlite3')) as connection:
                connection.executescript(_V1_SCHEMA)
                SessionStore._migrate_v2(connection)
                SessionStore._migrate_v3(connection)
                insert_session(connection, 'old', state.parent)
                connection.commit()
            store = SessionStore.open(state, clock=lambda: 1234)
            self.addCleanup(store.close)
            self.assertEqual(store.user_version(), 4)
            self.assertEqual(store.connection().execute('SELECT id FROM sessions').fetchone()[0], 'old')
            backups = list(state.glob('sessions.v3.*.sqlite3.backup'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            with closing(sqlite3.connect(backups[0])) as backup:
                self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 3)
                self.assertEqual(backup.execute('SELECT id FROM sessions').fetchone()[0], 'old')

    def test_failed_v4_upgrade_rolls_back_earlier_steps(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            with closing(sqlite3.connect(state / 'sessions.sqlite3')) as connection:
                connection.execute('CREATE TABLE legacy (id TEXT)')
            with patch.object(SessionStore, '_migrate_v4', side_effect=ValueError('injected'), create=True):
                with self.assertRaisesRegex(StoreError, 'STATE_MIGRATION_FAILED'):
                    SessionStore.open(state)
            with closing(sqlite3.connect(state / 'sessions.sqlite3')) as connection:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('legacy',)])


class ChannelStoreTests(unittest.TestCase):
    def setUp(self):
        try:
            module = importlib.import_module('local_agent.channels.store')
        except ModuleNotFoundError:
            self.fail('ChannelStore is not implemented')
        self.ChannelError = module.ChannelError
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = 100.0
        self.state = Path(self.temporary.name) / 'state'
        self.store = SessionStore.open(self.state, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.channels = module.ChannelStore(self.store)
        insert_session(self.store.connection(), 'session-a', self.state.parent / 'workspace')
        insert_session(self.store.connection(), 'session-b', self.state.parent / 'workspace')
        self.config = dict(app_id='app', tenant_key='tenant', open_id='user',
                           chat_id='chat', session_id='session-a', agent_id='legacy', enabled=True)
        self.binding = self.channels.configure(self.config)
        self.envelope = dict(event_id='event-1', message_id='message-1',
                             event_type='im.message.receive_v1', app_id='app',
                             tenant_key='tenant', open_id='user', chat_id='chat',
                             message_type='text', text='检查文件')
        self.request = dict(question='检查文件', task_type='files', output_path=None)

    def receive(self, **changes):
        return self.channels.receive(self.binding, dict(self.envelope, **changes), self.request)

    def link_run(self, event, run_id='run-a', session_id='session-a'):
        insert_run(self.store.connection(), run_id, session_id)
        self.channels.update_event(event['id'], 'dispatched', run_id=run_id)

    def test_binding_same_configuration_is_idempotent(self):
        self.assertEqual(self.binding['id'], 'feishu')
        self.assertEqual(self.binding['revision'], 1)
        self.now += 1
        self.assertEqual(self.channels.configure(self.config), self.binding)

    def test_receive_freezes_request_and_ack_atomically(self):
        event, created = self.receive()
        self.assertTrue(created)
        self.assertEqual(event['state'], 'received')
        self.assertEqual(event['client_request_id'], 'feishu:' + event['id'])
        self.assertEqual(len(event['id']), 64)
        self.request['question'] = 'changed'
        self.assertEqual(self.channels.event(event['id'])['request']['question'], '检查文件')
        self.assertEqual(event['binding']['revision'], 1)
        row, = self.channels.list_outbox(event['id'])
        self.assertEqual((row['kind'], row['state']), ('received', 'pending'))
        self.assertEqual(row['body'], '已收到，等待启动；尚未开始执行。')
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)

    def test_ack_failure_rolls_back_event(self):
        self.store.connection().execute("""CREATE TRIGGER reject_ack BEFORE INSERT ON channel_outbox
            BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.receive()
        self.assertEqual(self.channels.events(), [])

    def test_message_replays_and_aliases_are_deduplicated(self):
        first, _ = self.receive()
        again, created = self.receive(event_id='alias')
        self.assertFalse(created)
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(again['event_ids'], ['event-1', 'alias'])
        for event_id in ('event-1', 'alias'):
            with self.assertRaisesRegex(self.ChannelError, 'EVENT_CONFLICT'):
                self.receive(event_id=event_id, message_id='other')
        self.assertEqual(len(self.channels.list_outbox()), 1)

    def test_same_text_different_message_is_a_new_request(self):
        first, _ = self.receive()
        second, created = self.receive(event_id='event-2', message_id='message-2')
        self.assertTrue(created)
        self.assertNotEqual(first['client_request_id'], second['client_request_id'])

    def test_reused_message_with_changed_body_or_subject_is_rejected(self):
        self.receive()
        for changes in ({'text': 'other'}, {'open_id': 'other'}):
            with self.subTest(changes=changes):
                with self.assertRaises(self.ChannelError):
                    self.receive(**changes)
        self.assertEqual(len(self.channels.events()), 1)

    def test_concurrent_replays_create_one_event_and_ack(self):
        def deliver(index):
            try:
                return self.receive(event_id=f'event-{index}')[1]
            finally:
                self.store.close_thread_connection()
        with ThreadPoolExecutor(max_workers=4) as pool:
            created = list(pool.map(deliver, range(8)))
        self.assertEqual(sum(created), 1)
        self.assertEqual(len(self.channels.events()), 1)
        self.assertEqual(len(self.channels.list_outbox()), 1)
        self.assertEqual(len(self.channels.events()[0]['event_ids']), 8)

    def test_reconfigure_invalidates_waiting_events_and_suppresses_old_delivery(self):
        event, _ = self.receive()
        row, = self.channels.list_outbox()
        self.channels.claim(row['id'])
        self.channels.finish_delivery(row['id'], 'unknown')
        binding = self.channels.configure(dict(self.config, session_id='session-b'))
        self.assertEqual(binding['revision'], 2)
        self.assertEqual(self.channels.event(event['id'])['state'], 'invalidated')
        self.assertEqual(self.channels.list_outbox()[0]['state'], 'suppressed')
        with self.assertRaisesRegex(self.ChannelError, 'BINDING_INACTIVE'):
            self.receive()
        with self.assertRaises(self.ChannelError):
            self.channels.retry_unknown(row['id'])

    def test_disable_rejects_new_events(self):
        self.binding = self.channels.configure(dict(self.config, enabled=False))
        with self.assertRaisesRegex(self.ChannelError, 'BINDING_INACTIVE'):
            self.receive()

    def test_run_mapping_enforces_session_and_binding_revision(self):
        event, _ = self.receive()
        self.link_run(event)
        found = self.channels.find_run('feishu', 1, 'run-a')
        self.assertEqual(found['id'], event['id'])
        self.assertIsNone(self.channels.find_run('feishu', 2, 'run-a'))
        self.assertIsNone(self.channels.find_run('other', 1, 'run-a'))
        insert_run(self.store.connection(), 'run-b', 'session-b')
        with self.assertRaises(self.ChannelError):
            self.channels.update_event(event['id'], 'dispatched', run_id='run-b')
        self.assertEqual(self.channels.event(event['id'])['run_id'], 'run-a')

    def test_event_and_run_lifecycle_notifications_are_unique(self):
        event, _ = self.receive()
        self.link_run(event)
        first = self.channels.enqueue(event['id'], 'terminal', 'done', run_id='run-a')
        again = self.channels.enqueue(event['id'], 'terminal', 'changed', run_id='run-a')
        other, _ = self.receive(event_id='event-2', message_id='message-2')
        duplicate = self.channels.enqueue(other['id'], 'terminal', 'done', run_id='run-a')
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(first['id'], duplicate['id'])
        self.assertEqual(again['body'], 'done')
        self.assertEqual(first['uuid'], again['uuid'])

    def test_delivery_retries_are_bounded_and_do_not_create_runs(self):
        self.receive()
        row, = self.channels.due_outbox(self.now)
        for attempt in range(1, 4):
            self.assertTrue(self.channels.claim(row['id']))
            self.assertFalse(self.channels.claim(row['id']))
            self.channels.finish_delivery(row['id'], 'retry_wait', retry_after=10)
            row, = self.channels.list_outbox()
            self.assertEqual(row['attempts'], attempt)
            self.assertEqual(row['state'], 'retry_wait' if attempt < 3 else 'failed')
            self.assertEqual(self.channels.due_outbox(self.now), [])
            self.now += 10
        self.assertEqual(self.channels.due_outbox(self.now), [])
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)

    def test_unknown_requires_explicit_retry_and_preserves_uuid(self):
        self.receive()
        row, = self.channels.list_outbox()
        self.channels.claim(row['id'])
        self.assertEqual(self.channels.recover_delivery(), 1)
        self.assertEqual(self.channels.due_outbox(self.now), [])
        unknown, = self.channels.list_outbox()
        self.assertEqual(unknown['state'], 'unknown')
        self.channels.retry_unknown(row['id'])
        self.assertTrue(self.channels.claim(row['id']))
        self.channels.finish_delivery(row['id'], 'accepted', message_id='sent-1')
        accepted, = self.channels.list_outbox()
        self.assertEqual(accepted['uuid'], row['uuid'])
        self.assertEqual(accepted['message_id'], 'sent-1')
        with self.assertRaises(self.ChannelError):
            self.channels.retry_unknown(row['id'])

    def test_accepted_requires_platform_id_and_non_sending_cannot_finish(self):
        self.receive()
        row, = self.channels.list_outbox()
        with self.assertRaises(self.ChannelError):
            self.channels.finish_delivery(row['id'], 'accepted', message_id='sent')
        self.channels.claim(row['id'])
        with self.assertRaises(self.ChannelError):
            self.channels.finish_delivery(row['id'], 'accepted')
        self.assertEqual(self.channels.list_outbox()[0]['state'], 'sending')

    def test_suppression_and_event_filters(self):
        event, _ = self.receive()
        self.channels.enqueue(event['id'], 'running', 'running')
        self.channels.enqueue(event['id'], 'terminal', 'done')
        self.channels.suppress(event['id'], kinds=['received', 'running'])
        self.assertEqual([row['kind'] for row in self.channels.due_outbox(self.now)], ['terminal'])
        self.channels.update_event(event['id'], 'rejected', error_code='BUSY')
        self.assertEqual(self.channels.events(states=['received']), [])
        self.assertEqual(self.channels.events(states=['rejected'])[0]['error_code'], 'BUSY')

    def test_terminal_projection_preserves_run_mapping(self):
        event, _ = self.receive()
        self.link_run(event)
        saved = self.channels.update_event(event['id'], 'terminal')
        self.assertEqual(saved['state'], 'terminal')
        self.assertEqual(saved['run_id'], 'run-a')
        self.assertEqual(self.channels.find_run('feishu', 1, 'run-a')['id'], event['id'])

    def test_handled_command_needs_no_run(self):
        event, _ = self.receive()
        saved = self.channels.update_event(event['id'], 'handled')
        self.assertEqual(saved['state'], 'handled')
        self.assertIsNone(saved['run_id'])
        self.assertEqual(self.store.connection().execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)

    def test_backup_restores_channel_mapping_and_fixed_delivery(self):
        event, _ = self.receive()
        self.link_run(event)
        backup = self.store.backup(self.state.parent / 'backup.sqlite3')
        restored_state = self.state.parent / 'restored'
        restored_state.mkdir()
        shutil.copy2(backup, restored_state / 'sessions.sqlite3')
        restored = SessionStore.open(restored_state)
        self.addCleanup(restored.close)
        channels = type(self.channels)(restored)
        self.assertEqual(channels.event(event['id'])['run_id'], 'run-a')
        self.assertEqual(channels.list_outbox(), self.channels.list_outbox())
        self.assertEqual(channels.binding(), self.channels.binding())
        self.assertEqual(restored.connection().execute('PRAGMA foreign_key_check').fetchall(), [])
