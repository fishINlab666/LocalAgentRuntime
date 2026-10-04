"""Durable channel inbox/outbox; this module never executes or sends a task.

Rows are plain dictionaries. JSON columns remain available as strings, with
decoded aliases ``binding``, ``request`` and ``event_ids`` where applicable.
Outbox ``body`` is frozen plain text; all timestamps use the SessionStore clock.
"""

import hashlib
import json
import math
import uuid

from ..session_store import SessionStore


_BINDING_FIELDS = (
    'app_id', 'tenant_key', 'open_id', 'chat_id', 'session_id', 'agent_id', 'enabled',
)
_SUBJECT_FIELDS = ('app_id', 'tenant_key', 'open_id', 'chat_id')
_ENVELOPE_FIELDS = (
    'app_id', 'tenant_key', 'open_id', 'chat_id', 'event_type',
    'message_id', 'message_type', 'text',
)
_EVENT_STATES = {
    'received', 'dispatching', 'dispatched', 'rejected',
    'invalidated', 'interrupted_before_dispatch', 'handled', 'terminal',
}
_LIFECYCLE_KINDS = {'running', 'waiting_approval', 'terminal'}


class ChannelError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ChannelError('CHANNEL_PAYLOAD_INVALID') from None


def _rows(connection, query, parameters=()) -> list[dict]:
    cursor = connection.execute(query, parameters)
    columns = [column[0] for column in cursor.description]
    result = []
    for values in cursor:
        row = dict(zip(columns, values))
        for field in ('binding', 'request', 'event_ids'):
            if field + '_json' in row:
                row[field] = json.loads(row[field + '_json'])
        if 'enabled' in row:
            row['enabled'] = bool(row['enabled'])
        result.append(row)
    return result


def _one(connection, query, parameters=()) -> dict | None:
    rows = _rows(connection, query, parameters)
    return rows[0] if rows else None


class ChannelStore:
    def __init__(self, store: SessionStore):
        self.store = store

    def _binding(self, connection) -> dict | None:
        return _one(connection, "SELECT * FROM channel_bindings WHERE id='feishu'")

    def binding(self) -> dict | None:
        return self._binding(self.store.connection())

    def configure(self, config: dict) -> dict:
        if not isinstance(config, dict):
            raise ChannelError('BINDING_INVALID')
        values = {key: config.get(key) for key in _BINDING_FIELDS}
        if (any(not isinstance(values[key], str) or not values[key]
                for key in _BINDING_FIELDS if key != 'enabled')
                or not isinstance(values['enabled'], bool)):
            raise ChannelError('BINDING_INVALID')
        with self.store.transaction() as connection:
            old = self._binding(connection)
            if old and all(old[key] == values[key] for key in _BINDING_FIELDS):
                return old
            session = connection.execute(
                'SELECT agent_id FROM sessions WHERE id=?', (values['session_id'],)
            ).fetchone()
            if not session or session[0] != values['agent_id']:
                raise ChannelError('BINDING_SESSION_MISMATCH')
            now = self.store._clock()
            revision = old['revision'] + 1 if old else 1
            connection.execute(
                """INSERT INTO channel_bindings (
                       id, app_id,tenant_key,open_id,chat_id,session_id,agent_id,
                       enabled,revision,created_at,updated_at
                   ) VALUES ('feishu',?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       app_id=excluded.app_id,tenant_key=excluded.tenant_key,
                       open_id=excluded.open_id,chat_id=excluded.chat_id,
                       session_id=excluded.session_id,agent_id=excluded.agent_id,
                       enabled=excluded.enabled,revision=excluded.revision,
                       updated_at=excluded.updated_at""",
                (*values.values(), revision, now, now),
            )
            if old:
                connection.execute(
                    """UPDATE channel_events SET state='invalidated',
                       error_code='BINDING_CHANGED',updated_at=?
                       WHERE binding_id='feishu' AND binding_revision<?
                       AND state IN ('received','dispatching')""", (now, revision),
                )
                connection.execute(
                    """UPDATE channel_outbox SET state='suppressed',
                       error_code='BINDING_CHANGED',updated_at=?
                       WHERE state IN ('pending','retry_wait','unknown','failed')
                       AND event_id IN (SELECT id FROM channel_events
                           WHERE binding_id='feishu' AND binding_revision<?)""",
                    (now, revision),
                )
            return self._binding(connection)

    def _active(self, connection, binding) -> bool:
        current = self._binding(connection)
        return bool(current and current['enabled']
                    and current['id'] == binding.get('id')
                    and current['revision'] == binding.get('revision')
                    and all(current[key] == binding.get(key) for key in _BINDING_FIELDS))

    def receive(self, binding: dict, envelope: dict, request: dict) -> tuple[dict, bool]:
        if (not isinstance(envelope, dict) or not isinstance(request, dict)
                or any(not isinstance(envelope.get(key), str) for key in _ENVELOPE_FIELDS)
                or not isinstance(envelope.get('event_id'), str)
                or not envelope['event_id'] or not envelope['message_id']):
            raise ChannelError('CHANNEL_PAYLOAD_INVALID')
        stable = {key: envelope[key] for key in _ENVELOPE_FIELDS}
        fingerprint = hashlib.sha256(_json(stable).encode('utf-8')).hexdigest()
        identity = ['feishu', *(envelope[key] for key in
                               ('app_id', 'tenant_key', 'event_type', 'message_id'))]
        event_key = hashlib.sha256(_json(identity).encode('utf-8')).hexdigest()
        with self.store.transaction() as connection:
            if not self._active(connection, binding):
                raise ChannelError('BINDING_INACTIVE')
            if any(envelope[key] != binding[key] for key in _SUBJECT_FIELDS):
                raise ChannelError('SUBJECT_MISMATCH')
            # Alias IDs live in the inbox row, not a second source of identity.
            alias = connection.execute(
                """SELECT fingerprint FROM channel_events
                   WHERE app_id=? AND tenant_key=? AND event_type=?
                   AND EXISTS (SELECT 1 FROM json_each(event_ids_json) WHERE value=?)
                   LIMIT 1""",
                tuple(envelope[key] for key in ('app_id', 'tenant_key', 'event_type', 'event_id')),
            ).fetchone()
            if alias is not None and alias[0] != fingerprint:
                raise ChannelError('EVENT_CONFLICT')
            existing = self._event(connection, event_key)
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise ChannelError('EVENT_CONFLICT')
                if envelope['event_id'] not in existing['event_ids']:
                    aliases = existing['event_ids'] + [envelope['event_id']]
                    connection.execute('UPDATE channel_events SET event_ids_json=? WHERE id=?',
                                       (_json(aliases), event_key))
                return self._event(connection, event_key), False
            now = self.store._clock()
            connection.execute(
                """INSERT INTO channel_events (
                       id,binding_id,binding_revision,session_id,agent_id,
                       app_id,tenant_key,event_type,message_id,event_id,event_ids_json,
                       fingerprint,binding_json,request_json,client_request_id,
                       state,received_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'received',?,?)""",
                (event_key, binding['id'], binding['revision'], binding['session_id'],
                 binding['agent_id'], envelope['app_id'], envelope['tenant_key'],
                 envelope['event_type'], envelope['message_id'], envelope['event_id'],
                 _json([envelope['event_id']]), fingerprint, _json(binding), _json(request),
                 'feishu:' + event_key, now, now),
            )
            event = self._event(connection, event_key)
            self._enqueue(connection, event, 'received', '已收到，等待启动；尚未开始执行。')
            return event, True

    def _event(self, connection, event_id) -> dict | None:
        return _one(connection, 'SELECT * FROM channel_events WHERE id=?', (event_id,))

    def event(self, event_id: str) -> dict | None:
        return self._event(self.store.connection(), event_id)

    def events(self, states=None) -> list[dict]:
        if states is not None and not states:
            return []
        parameters = tuple(states or ())
        where = ' WHERE state IN (' + ','.join('?' for _ in parameters) + ')' if parameters else ''
        return _rows(self.store.connection(),
                     'SELECT * FROM channel_events' + where + ' ORDER BY received_at,id', parameters)

    @staticmethod
    def _check_run(connection, event, run_id):
        if run_id is not None:
            row = connection.execute('SELECT session_id FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None or row[0] != event['session_id']:
                raise ChannelError('RUN_MISMATCH')

    def update_event(self, event_id: str, state: str, *, run_id=None, error_code=None) -> dict:
        if state not in _EVENT_STATES:
            raise ChannelError('EVENT_STATE_INVALID')
        with self.store.transaction() as connection:
            event = self._event(connection, event_id)
            if event is None:
                raise ChannelError('EVENT_NOT_FOUND')
            self._check_run(connection, event, run_id)
            if event['run_id'] and run_id and event['run_id'] != run_id:
                raise ChannelError('RUN_MISMATCH')
            now = self.store._clock()
            connection.execute(
                """UPDATE channel_events SET state=?,run_id=COALESCE(?,run_id),
                   error_code=?,updated_at=?,dispatched_at=CASE WHEN ?='dispatched'
                       THEN COALESCE(dispatched_at,?) ELSE dispatched_at END WHERE id=?""",
                (state, run_id, error_code, now, state, now, event_id),
            )
            return self._event(connection, event_id)

    def find_run(self, binding_id: str, revision: int, run_id: str) -> dict | None:
        return _one(self.store.connection(),
                    'SELECT * FROM channel_events WHERE binding_id=? AND binding_revision=? AND run_id=? LIMIT 1',
                    (binding_id, revision, run_id))

    def _enqueue(self, connection, event, kind, body, run_id=None):
        self._check_run(connection, event, run_id)
        existing = _one(connection, 'SELECT * FROM channel_outbox WHERE event_id=? AND kind=?',
                        (event['id'], kind))
        if existing is None and run_id is not None and kind in _LIFECYCLE_KINDS:
            existing = _one(connection, 'SELECT * FROM channel_outbox WHERE run_id=? AND kind=?',
                            (run_id, kind))
        if existing:
            return existing
        now = self.store._clock()
        outbox_id = uuid.uuid4().hex
        state = 'pending' if self._active(connection, event['binding']) else 'suppressed'
        connection.execute(
            """INSERT INTO channel_outbox (
                   id,event_id,run_id,kind,binding_json,body,uuid,state,
                   next_attempt_at,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (outbox_id, event['id'], run_id, kind, event['binding_json'], body,
             str(uuid.uuid4()), state, now, now, now),
        )
        return _one(connection, 'SELECT * FROM channel_outbox WHERE id=?', (outbox_id,))

    def enqueue(self, event_id: str, kind: str, body: str, *, run_id=None) -> dict:
        if not isinstance(kind, str) or not kind or not isinstance(body, str):
            raise ChannelError('CHANNEL_PAYLOAD_INVALID')
        with self.store.transaction() as connection:
            event = self._event(connection, event_id)
            if event is None:
                raise ChannelError('EVENT_NOT_FOUND')
            return self._enqueue(connection, event, kind, body, run_id)

    def list_outbox(self, event_id=None) -> list[dict]:
        where = ' WHERE event_id=?' if event_id is not None else ''
        return _rows(self.store.connection(),
                     'SELECT * FROM channel_outbox' + where + ' ORDER BY created_at,rowid',
                     (event_id,) if event_id is not None else ())

    def due_outbox(self, now: float) -> list[dict]:
        return _rows(self.store.connection(),
                     """SELECT * FROM channel_outbox WHERE state IN ('pending','retry_wait')
                        AND next_attempt_at<=? ORDER BY next_attempt_at,rowid LIMIT 32""", (now,))

    def claim(self, outbox_id: str) -> bool:
        with self.store.transaction() as connection:
            row = _one(connection, 'SELECT * FROM channel_outbox WHERE id=?', (outbox_id,))
            now = self.store._clock()
            if row is None or row['state'] not in ('pending', 'retry_wait') or row['next_attempt_at'] > now:
                return False
            if not self._active(connection, row['binding']):
                connection.execute("UPDATE channel_outbox SET state='suppressed',updated_at=? WHERE id=?",
                                   (now, outbox_id))
                return False
            connection.execute("""UPDATE channel_outbox SET state='sending',attempts=attempts+1,
                                  updated_at=? WHERE id=?""", (now, outbox_id))
            return True

    def finish_delivery(self, outbox_id: str, state: str, *, message_id=None,
                        error_code=None, retry_after=0) -> dict:
        if (state not in ('accepted', 'retry_wait', 'unknown', 'failed')
                or (state == 'accepted' and (not isinstance(message_id, str) or not message_id))
                or not isinstance(retry_after, (int, float)) or not math.isfinite(retry_after)
                or retry_after < 0):
            raise ChannelError('DELIVERY_STATE_INVALID')
        with self.store.transaction() as connection:
            row = _one(connection, 'SELECT * FROM channel_outbox WHERE id=?', (outbox_id,))
            if row is None or row['state'] != 'sending':
                raise ChannelError('DELIVERY_STATE_INVALID')
            if state == 'retry_wait' and row['attempts'] >= 3:
                state = 'failed'
            now = self.store._clock()
            connection.execute(
                """UPDATE channel_outbox SET state=?,message_id=?,error_code=?,
                   next_attempt_at=?,updated_at=? WHERE id=?""",
                (state, message_id, error_code, now + retry_after, now, outbox_id),
            )
            return _one(connection, 'SELECT * FROM channel_outbox WHERE id=?', (outbox_id,))

    def suppress(self, event_id: str, kinds=None) -> int:
        if kinds is not None and not kinds:
            return 0
        with self.store.transaction() as connection:
            parameters = (self.store._clock(), event_id, *(kinds or ()))
            where = ' AND kind IN (' + ','.join('?' for _ in kinds) + ')' if kinds else ''
            cursor = connection.execute(
                """UPDATE channel_outbox SET state='suppressed',updated_at=?
                   WHERE event_id=? AND state IN ('pending','retry_wait','unknown','failed')""" + where,
                parameters,
            )
            return cursor.rowcount

    def recover_delivery(self) -> int:
        with self.store.transaction() as connection:
            return connection.execute(
                """UPDATE channel_outbox SET state='unknown',error_code='DELIVERY_INTERRUPTED',
                   updated_at=? WHERE state='sending'""", (self.store._clock(),)
            ).rowcount

    def retry_unknown(self, outbox_id: str) -> dict:
        with self.store.transaction() as connection:
            row = _one(connection, 'SELECT * FROM channel_outbox WHERE id=?', (outbox_id,))
            if row is None or row['state'] not in ('unknown', 'failed'):
                raise ChannelError('DELIVERY_NOT_RETRYABLE')
            if not self._active(connection, row['binding']):
                raise ChannelError('BINDING_INACTIVE')
            now = self.store._clock()
            connection.execute(
                """UPDATE channel_outbox SET state='pending',error_code=NULL,
                   next_attempt_at=?,updated_at=? WHERE id=?""", (now, now, outbox_id),
            )
            return _one(connection, 'SELECT * FROM channel_outbox WHERE id=?', (outbox_id,))
