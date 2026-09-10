"""Durable SQLite ownership, schema, and connection lifecycle."""

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterator


_SCHEMA_VERSION = 1

_V1_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    workspace_device INTEGER NOT NULL,
    workspace_inode INTEGER NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','archived')),
    revision INTEGER NOT NULL DEFAULT 1,
    active_summary_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(active_summary_id, id) REFERENCES summaries(id, session_id)
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    task_type TEXT NOT NULL CHECK(task_type IN ('files','conversation')),
    question TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    output_path TEXT,
    parent_run_id TEXT,
    state TEXT NOT NULL,
    phase TEXT NOT NULL,
    stop_reason TEXT,
    result_json TEXT,
    trace_path TEXT,
    provider_json TEXT,
    config_json TEXT NOT NULL,
    system_version TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE(session_id, client_request_id),
    UNIQUE(id, session_id),
    FOREIGN KEY(session_id) REFERENCES sessions(id),
    FOREIGN KEY(parent_run_id, session_id) REFERENCES runs(id, session_id)
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_seq INTEGER NOT NULL,
    run_seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','control')),
    source_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    validation_state TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(session_id, session_seq),
    UNIQUE(run_id, run_seq),
    UNIQUE(id, session_id, run_id),
    FOREIGN KEY(run_id, session_id) REFERENCES runs(id, session_id)
);
CREATE TABLE tool_calls (
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    assistant_message_id TEXT NOT NULL,
    name TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN (
        'requested','started','succeeded','failed','skipped','interrupted','unknown'
    )),
    result_message_id TEXT,
    publication_intent_json TEXT,
    publication_state TEXT NOT NULL DEFAULT 'none'
        CHECK(publication_state IN ('none','intent_recorded','confirmed','unknown')),
    recovery_state TEXT NOT NULL DEFAULT 'none',
    PRIMARY KEY(run_id, call_id),
    UNIQUE(run_id, call_id, session_id),
    FOREIGN KEY(run_id, session_id) REFERENCES runs(id, session_id),
    FOREIGN KEY(assistant_message_id, session_id, run_id)
        REFERENCES messages(id, session_id, run_id),
    FOREIGN KEY(result_message_id, session_id, run_id)
        REFERENCES messages(id, session_id, run_id)
);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    argument_hash TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending'
        CHECK(decision IN ('pending','allowed','denied','expired','cancelled')),
    process_generation TEXT NOT NULL,
    created_at REAL NOT NULL,
    decided_at REAL,
    invalidated_at REAL,
    FOREIGN KEY(run_id, call_id, session_id)
        REFERENCES tool_calls(run_id, call_id, session_id)
);
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK(bytes >= 0),
    sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    recovery_state TEXT NOT NULL,
    confirmed_at REAL NOT NULL,
    UNIQUE(run_id, call_id),
    FOREIGN KEY(run_id, call_id, session_id)
        REFERENCES tool_calls(run_id, call_id, session_id)
);
CREATE TABLE summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    covered_through_seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    model_json TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(session_id, version),
    UNIQUE(id, session_id),
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE context_manifests (
    run_id TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    input_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, request_seq),
    FOREIGN KEY(run_id, session_id) REFERENCES runs(id, session_id)
);
PRAGMA user_version=1;
"""


class StoreError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SessionStore:
    def __init__(self, state_dir: Path, lock_fd: int, clock):
        self.state_dir = state_dir
        self.database_path = state_dir / "sessions.sqlite3"
        self._lock_fd = lock_fd
        self._clock = clock
        self._local = threading.local()
        self._lifecycle_lock = threading.Lock()
        self._closed = False

    @classmethod
    def open(cls, state_dir: Path, *, clock=time.time) -> "SessionStore":
        state_dir = Path(state_dir).resolve()
        lock_fd = None
        try:
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(state_dir, 0o700)
            lock_fd = os.open(
                state_dir / "state.lock",
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(lock_fd, 0o600)
        except OSError as error:
            if lock_fd is not None:
                os.close(lock_fd)
            raise StoreError("STATE_OPEN_FAILED") from error

        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(lock_fd)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise StoreError("STATE_IN_USE") from None
            raise StoreError("STATE_OPEN_FAILED") from error

        store = cls(state_dir, lock_fd, clock)
        try:
            store._initialize_database()
        except Exception:
            store._release_lock()
            raise
        return store

    def _initialize_database(self) -> None:
        try:
            existed = self.database_path.exists()
            if not existed:
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                fd = os.open(self.database_path, flags, 0o600)
                os.close(fd)
                version = 0
            else:
                os.chmod(self.database_path, 0o600)
                version = self._read_existing_version()
            if version > _SCHEMA_VERSION:
                raise StoreError("STATE_VERSION_UNSUPPORTED")
            if version == 0:
                connection = None
                try:
                    connection = self._new_connection()
                    if existed:
                        self._backup_before_migration(connection, version)
                    self._migrate_v1(connection)
                except Exception as error:
                    raise StoreError("STATE_MIGRATION_FAILED") from error
                finally:
                    if connection is not None:
                        connection.close()
        except StoreError:
            raise
        except sqlite3.DatabaseError as error:
            code = (getattr(error, "sqlite_errorcode", 0) or 0) & 0xFF
            if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise StoreError("STATE_CORRUPT") from None
            raise StoreError("STATE_OPEN_FAILED") from error
        except OSError as error:
            raise StoreError("STATE_OPEN_FAILED") from error

    def _read_existing_version(self) -> int:
        uri = self.database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    @staticmethod
    def _migrate_v1(connection: sqlite3.Connection) -> None:
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + _V1_SCHEMA + "\nCOMMIT;")
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise

    def _backup_before_migration(
        self, connection: sqlite3.Connection, version: int
    ) -> Path:
        timestamp = int(self._clock())
        base = self.state_dir / (
            f"sessions.v{version}.{timestamp}.sqlite3.backup"
        )
        destination = base
        counter = 1
        while destination.exists():
            destination = self.state_dir / (
                f"sessions.v{version}.{timestamp}.{counter}.sqlite3.backup"
            )
            counter += 1
        self._copy_database(connection, destination)
        return destination

    @staticmethod
    def _copy_database(connection, destination: Path) -> None:
        created = False
        fd = None
        target = None
        try:
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                fd = os.open(destination, flags, 0o600)
            except FileExistsError as error:
                raise StoreError("STATE_BACKUP_EXISTS") from error
            created = True
            os.fchmod(fd, 0o600)
            os.close(fd)
            fd = None
            target = sqlite3.connect(destination)
            connection.backup(target)
            target.close()
            target = None
            os.chmod(destination, 0o600)
        except BaseException:
            if target is not None:
                target.close()
            if fd is not None:
                os.close(fd)
            if created:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
            raise

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=5000")
        except Exception:
            connection.close()
            raise
        return connection

    def connection(self) -> sqlite3.Connection:
        with self._lifecycle_lock:
            if self._closed:
                raise StoreError("STATE_CLOSED")
            connection = getattr(self._local, "connection", None)
            if connection is None:
                connection = self._new_connection()
                self._local.connection = connection
            return connection

    def user_version(self) -> int:
        return self.connection().execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise

    def backup(self, destination: Path) -> Path:
        try:
            destination = Path(destination)
            is_directory = destination.exists() and destination.is_dir()
            if is_directory:
                timestamp = int(self._clock())
                base = destination / f"sessions.{timestamp}.sqlite3"
                candidate = base
                counter = 1
                while candidate.exists():
                    candidate = destination / f"sessions.{timestamp}.{counter}.sqlite3"
                    counter += 1
                destination = candidate
            resolved = destination.resolve()
            connection = self.connection()
            for row in connection.execute("SELECT workspace_path FROM sessions"):
                workspace = Path(row[0]).resolve()
                if resolved == workspace or resolved.is_relative_to(workspace):
                    raise StoreError("STATE_DIR_INSIDE_WORKSPACE")
            if resolved == self.database_path.resolve():
                raise StoreError("STATE_BACKUP_FAILED")
            if not is_directory and destination.exists():
                raise StoreError("STATE_BACKUP_EXISTS")

            destination.parent.mkdir(parents=True, exist_ok=True)
            self._copy_database(connection, destination)
        except StoreError:
            raise
        except (OSError, sqlite3.DatabaseError) as error:
            raise StoreError("STATE_BACKUP_FAILED") from error
        return destination

    def close_thread_connection(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            return
        connection.close()
        del self._local.connection

    def _release_lock(self) -> None:
        lock_fd = self._lock_fd
        if lock_fd is None:
            return
        self._lock_fd = None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
        self.close_thread_connection()
        self._release_lock()
