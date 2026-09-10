from contextlib import closing
import hashlib
import os
from pathlib import Path
import queue
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_agent.session_store import SessionStore, StoreError


def insert_session(connection, session_id, workspace):
    connection.execute(
        """INSERT INTO sessions (
               id, title, workspace_path, workspace_device, workspace_inode,
               scope_json, status, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, "Session", str(workspace), 1, 2, "{}", "active", 1.0, 1.0),
    )


def insert_run(connection, run_id, session_id):
    connection.execute(
        """INSERT INTO runs (
               id, session_id, client_request_id, request_json,
               request_fingerprint, task_type, question, scope_json, state,
               phase, config_json, system_version, tool_version,
               protocol_version, started_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            session_id,
            f"request-{run_id}",
            "{}",
            "fingerprint",
            "files",
            "Question",
            "{}",
            "running",
            "model",
            "{}",
            "system-v1",
            "tool-v1",
            "protocol-v1",
            2.0,
        ),
    )


class TrackingConnection:
    def __init__(self, connection):
        self.connection = connection
        self.backup_calls = 0

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def backup(self, destination):
        self.backup_calls += 1
        return self.connection.backup(destination)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"

    def test_open_creates_private_versioned_store(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        self.assertEqual(store.user_version(), 1)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.state / "sessions.sqlite3").stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE((self.state / "state.lock").stat().st_mode),
            0o600,
        )
        self.assertEqual(
            {
                row[0]
                for row in store.connection().execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            },
            {
                "sessions",
                "runs",
                "messages",
                "tool_calls",
                "approvals",
                "artifacts",
                "summaries",
                "context_manifests",
            },
        )

    def test_second_owner_is_rejected_without_touching_database(self):
        first = SessionStore.open(self.state)
        self.addCleanup(first.close)
        database = self.state / "sessions.sqlite3"
        before = database.stat()

        with self.assertRaisesRegex(StoreError, "STATE_IN_USE") as caught:
            SessionStore.open(self.state)

        after = database.stat()
        self.assertEqual(caught.exception.code, "STATE_IN_USE")
        self.assertEqual((after.st_size, after.st_mtime_ns),
                         (before.st_size, before.st_mtime_ns))

    def test_newer_schema_and_corrupt_database_stop_explicitly(self):
        self.state.mkdir(mode=0o700)
        path = self.state / "sessions.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(
            StoreError, "STATE_VERSION_UNSUPPORTED"
        ) as caught:
            SessionStore.open(self.state)
        self.assertEqual(caught.exception.code, "STATE_VERSION_UNSUPPORTED")

        path.write_bytes(b"not a sqlite database")
        with self.assertRaisesRegex(StoreError, "STATE_CORRUPT") as caught:
            SessionStore.open(self.state)
        self.assertEqual(caught.exception.code, "STATE_CORRUPT")

    def test_connections_enable_required_pragmas_and_foreign_keys(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        connection = store.connection()
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
        self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO summaries (
                       id, session_id, version, covered_through_seq,
                       payload_json, model_json, prompt_version, state, created_at
                   ) VALUES ('summary-1', 'missing', 1, 0, '{}', '{}', 'v1',
                             'ready', 1.0)"""
            )

    def test_transaction_uses_begin_immediate_and_commits(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        statements = []
        store.connection().set_trace_callback(statements.append)

        with store.transaction() as connection:
            insert_session(connection, "session-1", self.state.parent / "workspace")

        store.connection().set_trace_callback(None)
        self.assertIn("BEGIN IMMEDIATE", statements)
        self.assertIn("COMMIT", statements)
        self.assertEqual(
            store.connection().execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            1,
        )

    def test_transaction_rolls_back_on_exception(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)

        with self.assertRaisesRegex(RuntimeError, "stop"):
            with store.transaction() as connection:
                insert_session(
                    connection, "session-rollback", self.state.parent / "workspace"
                )
                raise RuntimeError("stop")

        self.assertFalse(store.connection().in_transaction)
        self.assertEqual(
            store.connection().execute(
                "SELECT COUNT(*) FROM sessions WHERE id='session-rollback'"
            ).fetchone()[0],
            0,
        )

    def test_worker_connection_is_distinct_and_closed_by_its_thread(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        main_connection = store.connection()
        ready = threading.Event()
        proceed = threading.Event()
        outcomes = queue.Queue()

        def use_worker_connection():
            try:
                connection = store.connection()
                outcomes.put(("ready", id(connection)))
                ready.set()
                if not proceed.wait(5):
                    raise TimeoutError("main thread did not release worker")
                outcomes.put(("result", connection.execute("SELECT 1").fetchone()[0]))
            except Exception as error:
                outcomes.put(("error", repr(error)))
            finally:
                store.close_thread_connection()

        worker = threading.Thread(target=use_worker_connection)
        worker.start()
        self.assertTrue(ready.wait(5))
        ready_outcome = outcomes.get(timeout=5)
        self.assertEqual(ready_outcome[0], "ready")
        worker_connection_id = ready_outcome[1]
        store.close_thread_connection()
        proceed.set()
        outcome = outcomes.get(timeout=5)
        worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertNotEqual(worker_connection_id, id(main_connection))
        self.assertEqual(outcome, ("result", 1))

    def test_close_is_idempotent_and_releases_owner_lock(self):
        first = SessionStore.open(self.state)
        first.close()
        first.close()
        with self.assertRaisesRegex(StoreError, "STATE_CLOSED") as caught:
            first.connection()
        self.assertEqual(caught.exception.code, "STATE_CLOSED")

        second = SessionStore.open(self.state)
        second.close()

    def test_wal_backup_uses_sqlite_backup_and_is_readable(self):
        store = SessionStore.open(self.state, clock=lambda: 1234.0)
        self.addCleanup(store.close)
        workspace = self.state.parent / "workspace"
        workspace.mkdir()
        with store.transaction() as connection:
            insert_session(connection, "session-1", workspace)
            insert_run(connection, "run-1", "session-1")
        self.assertTrue(Path(str(store.database_path) + "-wal").exists())

        source = store.connection()
        tracking = TrackingConnection(source)
        destination = self.state.parent / "backup.sqlite3"
        with patch.object(store, "connection", return_value=tracking):
            actual = store.backup(destination)

        self.assertEqual(tracking.backup_calls, 1)
        self.assertEqual(actual, destination)
        uri = destination.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as backup:
            self.assertEqual(
                backup.execute(
                    """SELECT runs.id
                       FROM runs JOIN sessions ON sessions.id = runs.session_id
                       WHERE sessions.id = 'session-1'"""
                ).fetchall(),
                [("run-1",)],
            )

    def test_backup_is_private_before_sqlite_opens_destination(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        destination = self.state.parent / "private-backup.sqlite3"
        real_connect = sqlite3.connect
        observed_modes = []

        def observe_destination(database, *args, **kwargs):
            connection = real_connect(database, *args, **kwargs)
            if Path(database) == destination:
                observed_modes.append(
                    stat.S_IMODE(destination.stat().st_mode)
                )
            return connection

        previous_umask = os.umask(0o022)
        try:
            with patch(
                "local_agent.session_store.sqlite3.connect",
                side_effect=observe_destination,
            ):
                store.backup(destination)
        finally:
            os.umask(previous_umask)

        self.assertEqual(observed_modes, [0o600])

    def test_backup_refuses_existing_file_without_changing_it(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        destination = self.state.parent / "existing.sqlite3"
        with closing(sqlite3.connect(destination)) as existing:
            existing.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
            existing.execute("INSERT INTO sentinel VALUES ('keep')")
            existing.commit()
        before = destination.read_bytes()

        with self.assertRaisesRegex(
            StoreError, "STATE_BACKUP_EXISTS"
        ) as caught:
            store.backup(destination)

        self.assertEqual(caught.exception.code, "STATE_BACKUP_EXISTS")
        self.assertEqual(destination.read_bytes(), before)
        with closing(sqlite3.connect(destination)) as existing:
            self.assertEqual(
                existing.execute("SELECT value FROM sentinel").fetchone()[0],
                "keep",
            )

    def test_directory_backup_uses_unique_name_on_clock_collision(self):
        store = SessionStore.open(self.state, clock=lambda: 1234.0)
        self.addCleanup(store.close)
        destination = self.state.parent / "backups"
        destination.mkdir()
        occupied = destination / "sessions.1234.sqlite3"
        occupied.write_bytes(b"keep")

        actual = store.backup(destination)

        self.assertEqual(actual, destination / "sessions.1234.1.sqlite3")
        self.assertEqual(occupied.read_bytes(), b"keep")
        self.assertTrue(actual.is_file())

    def test_future_version_probe_does_not_modify_database(self):
        self.state.mkdir(mode=0o700)
        path = self.state / "sessions.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA user_version=99")
        before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        before_mtime = path.stat().st_mtime_ns

        with self.assertRaisesRegex(
            StoreError, "STATE_VERSION_UNSUPPORTED"
        ):
            SessionStore.open(self.state)

        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertFalse(Path(str(path) + "-shm").exists())

    def test_database_creation_error_is_mapped_and_releases_lock(self):
        real_open = os.open

        def fail_database(path, flags, mode=0o777):
            if Path(path).name == "sessions.sqlite3":
                raise OSError("injected database create failure")
            return real_open(path, flags, mode)

        with patch("local_agent.session_store.os.open", side_effect=fail_database):
            with self.assertRaisesRegex(
                StoreError, "STATE_OPEN_FAILED"
            ) as caught:
                SessionStore.open(self.state)
        self.assertEqual(caught.exception.code, "STATE_OPEN_FAILED")

        reopened = SessionStore.open(self.state)
        reopened.close()

    def test_backup_query_error_is_mapped(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)

        class BrokenQuery:
            def execute(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("injected query failure")

        with patch.object(store, "connection", return_value=BrokenQuery()):
            with self.assertRaisesRegex(
                StoreError, "STATE_BACKUP_FAILED"
            ) as caught:
                store.backup(self.state.parent / "backup.sqlite3")
        self.assertEqual(caught.exception.code, "STATE_BACKUP_FAILED")

    def test_composite_foreign_keys_reject_cross_run_and_session_links(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        workspace = self.state.parent / "workspace"
        other_workspace = self.state.parent / "other-workspace"
        with store.transaction() as connection:
            insert_session(connection, "session-1", workspace)
            insert_session(connection, "session-2", other_workspace)
            insert_run(connection, "run-1", "session-1")
            insert_run(connection, "run-2", "session-1")
            connection.execute(
                """INSERT INTO messages (
                       id, session_id, run_id, session_seq, run_seq, role,
                       source_kind, payload_json, validation_state, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "message-run-2", "session-1", "run-2", 1, 1,
                    "assistant", "model", "{}", "valid", 3.0,
                ),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            with store.transaction() as connection:
                connection.execute(
                    """INSERT INTO tool_calls (
                           run_id, call_id, session_id, assistant_message_id,
                           name, stage
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "run-1", "call-1", "session-1", "message-run-2",
                        "read_file", "requested",
                    ),
                )

        with self.assertRaises(sqlite3.IntegrityError):
            with store.transaction() as connection:
                connection.execute(
                    """INSERT INTO context_manifests (
                           run_id, request_seq, session_id, payload_json,
                           input_sha256, input_bytes, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    ("run-1", 1, "session-2", "{}", "hash", 2, 4.0),
                )

    def test_backup_rejects_file_or_directory_inside_bound_workspace(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        workspace = self.state.parent / "workspace"
        workspace.mkdir()
        with store.transaction() as connection:
            insert_session(connection, "session-1", workspace)

        for destination in (workspace / "backup.sqlite3", workspace):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(
                    StoreError, "STATE_DIR_INSIDE_WORKSPACE"
                ) as caught:
                    store.backup(destination)
                self.assertEqual(caught.exception.code, "STATE_DIR_INSIDE_WORKSPACE")
        self.assertFalse((workspace / "backup.sqlite3").exists())

    def test_existing_v0_database_is_backed_up_before_migration(self):
        self.state.mkdir(mode=0o700)
        path = self.state / "sessions.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE legacy (value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy VALUES ('kept')")
            connection.commit()

        store = SessionStore.open(self.state, clock=lambda: 1234.0)
        self.addCleanup(store.close)
        backups = list(self.state.glob("*.sqlite3.backup"))
        self.assertEqual(len(backups), 1)
        self.assertIn("v0", backups[0].name)
        self.assertIn("1234", backups[0].name)
        with closing(sqlite3.connect(backups[0])) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(backup.execute("SELECT value FROM legacy").fetchone()[0],
                             "kept")
        self.assertEqual(store.user_version(), 1)

    def test_migration_failure_preserves_old_database_and_releases_lock(self):
        self.state.mkdir(mode=0o700)
        path = self.state / "sessions.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE sessions (legacy TEXT NOT NULL)")
            connection.execute("INSERT INTO sessions VALUES ('kept')")
            connection.commit()

        with self.assertRaisesRegex(
            StoreError, "STATE_MIGRATION_FAILED"
        ) as caught:
            SessionStore.open(self.state, clock=lambda: 1234.0)
        self.assertEqual(caught.exception.code, "STATE_MIGRATION_FAILED")
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT legacy FROM sessions").fetchone()[0],
                "kept",
            )
            connection.execute("DROP TABLE sessions")
            connection.commit()

        reopened = SessionStore.open(self.state, clock=lambda: 1235.0)
        reopened.close()

    def test_migration_backup_failure_preserves_old_database_and_releases_lock(self):
        self.state.mkdir(mode=0o700)
        path = self.state / "sessions.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE legacy (value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy VALUES ('kept')")
            connection.commit()
        real_connect = sqlite3.connect

        def fail_backup(database, *args, **kwargs):
            if str(database).endswith(".sqlite3.backup"):
                raise sqlite3.OperationalError("injected backup failure")
            return real_connect(database, *args, **kwargs)

        with patch("local_agent.session_store.sqlite3.connect", side_effect=fail_backup):
            with self.assertRaisesRegex(
                StoreError, "STATE_MIGRATION_FAILED"
            ) as caught:
                SessionStore.open(self.state, clock=lambda: 1234.0)
        self.assertEqual(caught.exception.code, "STATE_MIGRATION_FAILED")
        self.assertEqual(list(self.state.glob("*.sqlite3.backup")), [])
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(backup_value := connection.execute(
                "SELECT value FROM legacy"
            ).fetchone()[0], "kept")
        self.assertEqual(backup_value, "kept")

        reopened = SessionStore.open(self.state, clock=lambda: 1235.0)
        reopened.close()


if __name__ == "__main__":
    unittest.main()
