"""Durable SQLite ownership, schema, and connection lifecycle."""

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterator
import uuid


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


@dataclass(frozen=True)
class StoredMessage:
    id: str
    session_id: str
    run_id: str
    session_seq: int
    run_seq: int
    role: str
    source_kind: str
    payload: dict
    validation_state: str
    created_at: float


def _json_text(value) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise StoreError("JOURNAL_PAYLOAD_INVALID") from None


def _json_object(value) -> dict:
    if not isinstance(value, dict):
        raise StoreError("JOURNAL_PAYLOAD_INVALID")
    return json.loads(_json_text(value))


class RunJournal:
    """Ordered, short-transaction persistence for one run."""

    def __init__(
        self,
        store: "SessionStore",
        session_id: str,
        run_id: str,
        *,
        clock=time.time,
        id_factory=None,
    ):
        self._store = store
        self.session_id = session_id
        self.run_id = run_id
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def _new_id(self) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value:
            raise StoreError("JOURNAL_STORE_ERROR")
        return value

    def close_thread_connection(self) -> None:
        self._store.close_thread_connection()

    @contextmanager
    def _write(self):
        try:
            with self._store.transaction() as connection:
                yield connection
        except StoreError:
            raise
        except (sqlite3.DatabaseError, OSError) as error:
            raise StoreError("JOURNAL_STORE_ERROR") from error

    def _unfinished_run(self, connection):
        row = connection.execute(
            """SELECT state, phase, finished_at
               FROM runs WHERE session_id=? AND id=?""",
            (self.session_id, self.run_id),
        ).fetchone()
        if row is None or row[2] is not None or row[0] in {
            "completed",
            "failed",
            "validation_failed",
            "cancelled",
            "interrupted",
        }:
            raise StoreError("JOURNAL_ORDER_ERROR")
        return row

    def _append_message(
        self,
        connection,
        *,
        role: str,
        source_kind: str,
        payload: dict,
        validation: str,
    ) -> str:
        self._unfinished_run(connection)
        message_id = self._new_id()
        session_seq = connection.execute(
            "SELECT COALESCE(MAX(session_seq), 0) + 1 FROM messages WHERE session_id=?",
            (self.session_id,),
        ).fetchone()[0]
        run_seq = connection.execute(
            "SELECT COALESCE(MAX(run_seq), 0) + 1 FROM messages WHERE run_id=?",
            (self.run_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO messages (
                   id, session_id, run_id, session_seq, run_seq, role,
                   source_kind, payload_json, validation_state, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                self.session_id,
                self.run_id,
                session_seq,
                run_seq,
                role,
                source_kind,
                _json_text(payload),
                validation,
                self._clock(),
            ),
        )
        return message_id

    def record_model_request(self, request: dict, manifest: dict) -> None:
        request = _json_object(request)
        manifest = _json_object(manifest)
        request_json = _json_text(request)
        with self._write() as connection:
            self._unfinished_run(connection)
            pending = connection.execute(
                """SELECT 1 FROM tool_calls
                   WHERE run_id=? AND result_message_id IS NULL LIMIT 1""",
                (self.run_id,),
            ).fetchone()
            if pending is not None:
                raise StoreError("JOURNAL_ORDER_ERROR")
            request_seq = connection.execute(
                """SELECT COALESCE(MAX(request_seq), 0) + 1
                   FROM context_manifests WHERE run_id=?""",
                (self.run_id,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO context_manifests (
                       run_id, request_seq, session_id, payload_json,
                       input_sha256, input_bytes, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.run_id,
                    request_seq,
                    self.session_id,
                    _json_text({"request": request, "manifest": manifest}),
                    hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
                    len(request_json.encode("utf-8")),
                    self._clock(),
                ),
            )
            connection.execute(
                "UPDATE runs SET state='running', phase='model_in_flight' WHERE id=?",
                (self.run_id,),
            )

    @staticmethod
    def _valid_tool_calls(message: dict) -> tuple[tuple[str, str], ...]:
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            raise StoreError("JOURNAL_PAYLOAD_INVALID")
        indexed = []
        seen = set()
        for call in calls:
            function = call.get("function") if isinstance(call, dict) else None
            call_id = call.get("id") if isinstance(call, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in seen
                or not isinstance(name, str)
                or not name
                or not isinstance(arguments, str)
            ):
                raise StoreError("JOURNAL_PAYLOAD_INVALID")
            seen.add(call_id)
            indexed.append((call_id, name))
        return tuple(indexed)

    def record_model_reply(
        self, message: dict, usage: dict | None, validation: str
    ) -> str:
        message = _json_object(message)
        message.pop("reasoning_content", None)
        usage = None if usage is None else _json_object(usage)
        if not isinstance(validation, str) or not validation:
            raise StoreError("JOURNAL_PAYLOAD_INVALID")
        calls = (
            self._valid_tool_calls(message)
            if validation == "tool_calls_valid"
            else ()
        )
        with self._write() as connection:
            run = self._unfinished_run(connection)
            if run[1] != "model_in_flight":
                raise StoreError("JOURNAL_ORDER_ERROR")
            message_id = self._append_message(
                connection,
                role="assistant",
                source_kind="model",
                payload=message,
                validation=validation,
            )
            for call_id, name in calls:
                existing = connection.execute(
                    "SELECT 1 FROM tool_calls WHERE run_id=? AND call_id=?",
                    (self.run_id, call_id),
                ).fetchone()
                if existing is not None:
                    raise StoreError("JOURNAL_ORDER_ERROR")
                connection.execute(
                    """INSERT INTO tool_calls (
                           run_id, call_id, session_id, assistant_message_id,
                           name, stage
                       ) VALUES (?, ?, ?, ?, ?, 'requested')""",
                    (self.run_id, call_id, self.session_id, message_id, name),
                )
            if usage is not None:
                current = connection.execute(
                    "SELECT provider_json FROM runs WHERE id=?", (self.run_id,)
                ).fetchone()[0]
                try:
                    provider = json.loads(current) if current else {}
                except (TypeError, ValueError):
                    provider = {}
                if not isinstance(provider, dict):
                    provider = {}
                history = provider.get("usage")
                if not isinstance(history, list):
                    history = []
                history.append(usage)
                provider["usage"] = history
                connection.execute(
                    "UPDATE runs SET provider_json=? WHERE id=?",
                    (_json_text(provider), self.run_id),
                )
            phase = "tool_requested" if calls else "model_replied"
            connection.execute(
                "UPDATE runs SET state='running', phase=? WHERE id=?",
                (phase, self.run_id),
            )
            return message_id

    def _tool_call(self, connection, call_id: str):
        row = connection.execute(
            """SELECT stage, result_message_id, publication_state,
                      publication_intent_json
               FROM tool_calls WHERE run_id=? AND session_id=? AND call_id=?""",
            (self.run_id, self.session_id, call_id),
        ).fetchone()
        if row is None:
            raise StoreError("JOURNAL_ORDER_ERROR")
        return row

    def record_tool_started(self, call_id: str) -> None:
        with self._write() as connection:
            self._unfinished_run(connection)
            call = self._tool_call(connection, call_id)
            if call[0] != "requested" or call[1] is not None:
                raise StoreError("JOURNAL_ORDER_ERROR")
            connection.execute(
                "UPDATE tool_calls SET stage='started' WHERE run_id=? AND call_id=?",
                (self.run_id, call_id),
            )
            connection.execute(
                "UPDATE runs SET state='running', phase='tool_started' WHERE id=?",
                (self.run_id,),
            )

    def record_tool_result(self, call_id: str, result: dict) -> str:
        result = _json_object(result)
        succeeded = result.get("ok") is True
        error = result.get("error") if not succeeded else None
        outcome_unknown = (
            isinstance(error, dict) and error.get("code") == "WRITE_OUTCOME_UNKNOWN"
        )
        with self._write() as connection:
            self._unfinished_run(connection)
            call = self._tool_call(connection, call_id)
            receipt_only = (
                call[0] == "succeeded"
                and call[1] is None
                and call[2] == "confirmed"
                and succeeded
            )
            skipped_result = (
                call[0] == "skipped" and call[1] is None and not succeeded
            )
            if call[1] is not None or (
                call[0] not in {"requested", "started"}
                and not receipt_only
                and not skipped_result
            ):
                raise StoreError("JOURNAL_ORDER_ERROR")
            message_id = self._append_message(
                connection,
                role="tool",
                source_kind="tool_runtime",
                payload={"tool_call_id": call_id, "result": result},
                validation="valid",
            )
            stage = (
                "unknown" if outcome_unknown else (
                    "skipped" if skipped_result else (
                        "succeeded" if succeeded else "failed"
                    )
                )
            )
            connection.execute(
                """UPDATE tool_calls SET stage=?, result_message_id=?,
                       publication_state=CASE WHEN ? THEN 'unknown' ELSE publication_state END,
                       recovery_state=CASE WHEN ? THEN 'outcome_unknown' ELSE recovery_state END
                   WHERE run_id=? AND call_id=?""",
                (stage, message_id, outcome_unknown, outcome_unknown,
                 self.run_id, call_id),
            )
            connection.execute(
                "UPDATE runs SET state='running', phase='tool_result_committed' WHERE id=?",
                (self.run_id,),
            )
            return message_id

    def record_approval_required(self, call_id: str, approval: dict) -> None:
        approval = _json_object(approval)
        approval_id = approval.get("id") or approval.get("approval_id")
        preview = approval.get("preview")
        argument_hash = approval.get("argument_hash")
        generation = approval.get("process_generation")
        if (
            not isinstance(approval_id, str)
            or not approval_id
            or not isinstance(preview, dict)
            or not isinstance(argument_hash, str)
            or not argument_hash
            or not isinstance(generation, str)
            or not generation
        ):
            raise StoreError("JOURNAL_PAYLOAD_INVALID")
        with self._write() as connection:
            self._unfinished_run(connection)
            call = self._tool_call(connection, call_id)
            if call[0] != "requested" or call[1] is not None:
                raise StoreError("JOURNAL_ORDER_ERROR")
            connection.execute(
                """INSERT INTO approvals (
                       id, session_id, run_id, call_id, preview_json,
                       argument_hash, decision, process_generation, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    approval_id,
                    self.session_id,
                    self.run_id,
                    call_id,
                    _json_text(preview),
                    argument_hash,
                    generation,
                    self._clock(),
                ),
            )
            connection.execute(
                "UPDATE runs SET state='waiting_approval', phase='waiting_approval' WHERE id=?",
                (self.run_id,),
            )

    def record_approval_decision(self, approval_id: str, decision: str) -> None:
        normalized = {"allow": "allowed", "deny": "denied"}.get(decision, decision)
        if normalized not in {"allowed", "denied"}:
            raise StoreError("JOURNAL_PAYLOAD_INVALID")
        with self._write() as connection:
            self._unfinished_run(connection)
            row = connection.execute(
                """SELECT call_id, decision FROM approvals
                   WHERE id=? AND session_id=? AND run_id=?""",
                (approval_id, self.session_id, self.run_id),
            ).fetchone()
            if row is None or row[1] != "pending":
                raise StoreError("JOURNAL_ORDER_ERROR")
            connection.execute(
                """UPDATE approvals SET decision=?, decided_at=? WHERE id=?""",
                (normalized, self._clock(), approval_id),
            )
            if normalized == "denied":
                connection.execute(
                    """UPDATE tool_calls SET stage='skipped'
                       WHERE run_id=? AND call_id=?""",
                    (self.run_id, row[0]),
                )
            connection.execute(
                "UPDATE runs SET state='running', phase=? WHERE id=?",
                (
                    "approval_allowed" if normalized == "allowed" else "approval_denied",
                    self.run_id,
                ),
            )

    @staticmethod
    def _publication_payload(payload: dict) -> tuple[str, int, str]:
        path = payload.get("path")
        size = payload.get("bytes")
        digest = payload.get("sha256")
        candidate = Path(path) if isinstance(path, str) else None
        if (
            candidate is None
            or candidate.is_absolute()
            or not path
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise StoreError("JOURNAL_PAYLOAD_INVALID")
        try:
            int(digest, 16)
        except ValueError:
            raise StoreError("JOURNAL_PAYLOAD_INVALID") from None
        return path, size, digest

    def record_publication_intent(self, call_id: str, intent: dict) -> None:
        intent = _json_object(intent)
        self._publication_payload(intent)
        with self._write() as connection:
            self._unfinished_run(connection)
            call = self._tool_call(connection, call_id)
            allowed = connection.execute(
                """SELECT 1 FROM approvals
                   WHERE run_id=? AND call_id=? AND decision='allowed' LIMIT 1""",
                (self.run_id, call_id),
            ).fetchone()
            if (
                call[0] not in {"requested", "started"}
                or call[1] is not None
                or call[3] is not None
                or allowed is None
            ):
                raise StoreError("JOURNAL_ORDER_ERROR")
            connection.execute(
                """UPDATE tool_calls
                   SET stage='started', publication_intent_json=?,
                       publication_state='intent_recorded'
                   WHERE run_id=? AND call_id=?""",
                (_json_text(intent), self.run_id, call_id),
            )
            connection.execute(
                """UPDATE runs SET state='running',
                       phase='publication_intent_committed' WHERE id=?""",
                (self.run_id,),
            )

    def record_publication_receipt(self, call_id: str, receipt: dict) -> None:
        receipt = _json_object(receipt)
        path, size, digest = self._publication_payload(receipt)
        with self._write() as connection:
            self._unfinished_run(connection)
            call = self._tool_call(connection, call_id)
            if (
                call[0] != "started"
                or call[1] is not None
                or call[2] != "intent_recorded"
                or call[3] is None
            ):
                raise StoreError("JOURNAL_ORDER_ERROR")
            intent = json.loads(call[3])
            if self._publication_payload(intent) != (path, size, digest):
                raise StoreError("JOURNAL_ORDER_ERROR")
            connection.execute(
                """INSERT INTO artifacts (
                       id, session_id, run_id, call_id, path, bytes, sha256,
                       receipt_json, recovery_state, confirmed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)""",
                (
                    self._new_id(),
                    self.session_id,
                    self.run_id,
                    call_id,
                    path,
                    size,
                    digest,
                    _json_text(receipt),
                    self._clock(),
                ),
            )
            connection.execute(
                """UPDATE tool_calls SET stage='succeeded',
                       publication_state='confirmed', recovery_state='confirmed'
                   WHERE run_id=? AND call_id=?""",
                (self.run_id, call_id),
            )
            connection.execute(
                """UPDATE runs SET state='running',
                       phase='publication_receipt_committed' WHERE id=?""",
                (self.run_id,),
            )

    def finish_run(self, result: dict) -> None:
        result = _json_object(result)
        state = result.get("state")
        if state not in {
            "completed", "failed", "validation_failed", "cancelled",
            "unable", "timed_out", "max_steps",
        }:
            raise StoreError("JOURNAL_PAYLOAD_INVALID")
        stop_reason = result.get("stop_reason")
        if stop_reason is not None and not isinstance(stop_reason, str):
            raise StoreError("JOURNAL_PAYLOAD_INVALID")
        with self._write() as connection:
            self._unfinished_run(connection)
            connection.execute(
                """UPDATE runs SET state=?, phase='finished', stop_reason=?,
                       result_json=?, finished_at=? WHERE id=?""",
                (
                    state,
                    stop_reason,
                    _json_text(result),
                    self._clock(),
                    self.run_id,
                ),
            )


class SessionStore:
    def __init__(self, state_dir: Path, lock_fd: int, clock):
        self.state_dir = state_dir
        self.database_path = state_dir / "sessions.sqlite3"
        self._lock_fd = lock_fd
        self._clock = clock
        self._local = threading.local()
        self._lifecycle_lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._recovered = False
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

    def load_run_messages(
        self, session_id: str, run_id: str
    ) -> tuple[StoredMessage, ...]:
        try:
            connection = self.connection()
            owned = connection.execute(
                "SELECT 1 FROM runs WHERE session_id=? AND id=?",
                (session_id, run_id),
            ).fetchone()
            if owned is None:
                raise StoreError("NOT_FOUND")
            rows = connection.execute(
                """SELECT id, session_id, run_id, session_seq, run_seq, role,
                          source_kind, payload_json, validation_state, created_at
                   FROM messages WHERE session_id=? AND run_id=?
                   ORDER BY run_seq""",
                (session_id, run_id),
            ).fetchall()
            messages = []
            for row in rows:
                payload = json.loads(row[7])
                if not isinstance(payload, dict):
                    raise ValueError("message payload is not an object")
                messages.append(
                    StoredMessage(
                        id=row[0],
                        session_id=row[1],
                        run_id=row[2],
                        session_seq=row[3],
                        run_seq=row[4],
                        role=row[5],
                        source_kind=row[6],
                        payload=payload,
                        validation_state=row[8],
                        created_at=row[9],
                    )
                )
            return tuple(messages)
        except StoreError:
            raise
        except (sqlite3.DatabaseError, OSError, TypeError, ValueError) as error:
            raise StoreError("SESSION_STORE_ERROR") from error

    def recover_interrupted(self, process_generation: str) -> int:
        if not isinstance(process_generation, str) or not process_generation:
            raise StoreError("PROCESS_GENERATION_INVALID")
        with self._recovery_lock:
            if self._recovered:
                return 0
            try:
                with self.transaction() as connection:
                    runs = connection.execute(
                        """SELECT id, session_id, phase FROM runs
                           WHERE finished_at IS NULL
                             AND state NOT IN (
                               'completed','failed','validation_failed',
                               'cancelled','interrupted'
                             )
                           ORDER BY started_at, id"""
                    ).fetchall()
                    now = self._clock()
                    for run_id, session_id, phase in runs:
                        unknown = connection.execute(
                            """SELECT t.call_id FROM tool_calls t
                               LEFT JOIN artifacts a
                                 ON a.run_id=t.run_id AND a.call_id=t.call_id
                               WHERE t.run_id=?
                                 AND t.publication_intent_json IS NOT NULL
                                 AND t.result_message_id IS NULL
                                 AND a.id IS NULL""",
                            (run_id,),
                        ).fetchall()
                        unknown_ids = {row[0] for row in unknown}
                        if unknown_ids:
                            placeholders = ",".join("?" for _ in unknown_ids)
                            connection.execute(
                                f"""UPDATE tool_calls
                                    SET stage='unknown', publication_state='unknown',
                                        recovery_state='WRITE_OUTCOME_UNKNOWN'
                                    WHERE run_id=? AND call_id IN ({placeholders})""",
                                (run_id, *sorted(unknown_ids)),
                            )
                        connection.execute(
                            """UPDATE tool_calls
                               SET stage='interrupted', recovery_state='interrupted'
                               WHERE run_id=? AND stage IN ('requested','started')
                                 AND publication_intent_json IS NULL""",
                            (run_id,),
                        )
                        connection.execute(
                            """UPDATE approvals
                               SET decision='expired', invalidated_at=?
                               WHERE run_id=? AND decision IN ('pending','allowed')
                                 AND process_generation<>?""",
                            (now, run_id, process_generation),
                        )
                        reason = (
                            "WRITE_OUTCOME_UNKNOWN"
                            if unknown_ids
                            else "PROCESS_INTERRUPTED"
                        )
                        session_seq = connection.execute(
                            """SELECT COALESCE(MAX(session_seq), 0) + 1
                               FROM messages WHERE session_id=?""",
                            (session_id,),
                        ).fetchone()[0]
                        run_seq = connection.execute(
                            """SELECT COALESCE(MAX(run_seq), 0) + 1
                               FROM messages WHERE run_id=?""",
                            (run_id,),
                        ).fetchone()[0]
                        connection.execute(
                            """INSERT INTO messages (
                                   id, session_id, run_id, session_seq, run_seq,
                                   role, source_kind, payload_json,
                                   validation_state, created_at
                               ) VALUES (?, ?, ?, ?, ?, 'control', 'recovery',
                                         ?, 'valid', ?)""",
                            (
                                uuid.uuid4().hex,
                                session_id,
                                run_id,
                                session_seq,
                                run_seq,
                                _json_text(
                                    {
                                        "kind": "process_interrupted",
                                        "phase": phase,
                                        "stop_reason": reason,
                                    }
                                ),
                                now,
                            ),
                        )
                        connection.execute(
                            """UPDATE runs SET state='interrupted', stop_reason=?,
                                   finished_at=? WHERE id=?""",
                            (reason, now, run_id),
                        )
                self._recovered = True
                return len(runs)
            except StoreError:
                raise
            except (sqlite3.DatabaseError, OSError) as error:
                raise StoreError("SESSION_STORE_ERROR") from error

    def inspect_unknown_publication(
        self, session_id: str, target_path: str
    ) -> str | None:
        try:
            connection = self.connection()
            session = connection.execute(
                """SELECT workspace_path, workspace_device, workspace_inode
                   FROM sessions WHERE id=?""",
                (session_id,),
            ).fetchone()
            if session is None:
                raise StoreError("NOT_FOUND")
            rows = connection.execute(
                """SELECT t.publication_intent_json
                   FROM tool_calls t
                   JOIN runs r ON r.id=t.run_id AND r.session_id=t.session_id
                   JOIN sessions s ON s.id=r.session_id
                   WHERE s.workspace_path=? AND s.workspace_device=?
                     AND s.workspace_inode=?
                     AND t.recovery_state='WRITE_OUTCOME_UNKNOWN'""",
                session,
            ).fetchall()
            expected_hash = None
            for row in rows:
                intent = json.loads(row[0])
                if isinstance(intent, dict) and intent.get("path") == target_path:
                    expected_hash = intent.get("sha256")
                    break
            if expected_hash is None:
                return None
            workspace = Path(session[0]).resolve(strict=True)
            actual_identity = os.stat(workspace)
            if (actual_identity.st_dev, actual_identity.st_ino) != session[1:]:
                raise StoreError("WORKSPACE_CHANGED")
            target = (workspace / target_path).resolve()
            if not target.is_relative_to(workspace):
                raise StoreError("JOURNAL_PAYLOAD_INVALID")
            if not target.exists():
                return "missing"
            if not target.is_file():
                return "present_different_hash"
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            return (
                "present_same_hash"
                if digest == expected_hash
                else "present_different_hash"
            )
        except StoreError:
            raise
        except (sqlite3.DatabaseError, OSError, TypeError, ValueError) as error:
            raise StoreError("SESSION_STORE_ERROR") from error

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
