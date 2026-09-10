"""Session lifecycle and atomic, idempotent run submission."""

from dataclasses import asdict, dataclass, field
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Callable, Literal
import uuid

from .session_store import RunJournal, SessionStore, StoreError


class SessionError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _FrozenDict(dict):
    def _immutable(self, *_args, **_kwargs):
        raise TypeError("immutable mapping")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _freeze(value):
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class SessionScope:
    mode: Literal["file", "directory"]
    target_path: str | None


@dataclass(frozen=True)
class RunSubmission:
    client_request_id: str
    question: str
    task_type: Literal["files", "conversation"]
    scope: SessionScope
    output_path: str | None
    parent_run_id: str | None
    execution_options: dict

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_options", _freeze(self.execution_options))

    def fingerprint(self) -> str:
        canonical = _canonical_json(_submission_payload(self))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    session_id: str
    created: bool
    submission: RunSubmission
    journal: RunJournal = field(compare=False, repr=False)


@dataclass(frozen=True)
class SessionRecord:
    id: str
    title: str
    workspace_path: str
    workspace_device: int
    workspace_inode: int
    scope: SessionScope
    status: Literal["active", "archived"]
    revision: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class RunRecord:
    id: str
    session_id: str
    client_request_id: str
    submission: RunSubmission
    state: str
    phase: str
    stop_reason: str | None
    started_at: float
    finished_at: float | None


@dataclass(frozen=True)
class MessageRecord:
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


@dataclass(frozen=True)
class Page:
    items: tuple[SessionRecord, ...]
    next_cursor: str | None


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _submission_payload(submission: RunSubmission) -> dict:
    return {
        "question": submission.question,
        "task_type": submission.task_type,
        "scope": asdict(submission.scope),
        "output_path": submission.output_path,
        "parent_run_id": submission.parent_run_id,
        "execution_options": submission.execution_options,
    }


def _scope_json(scope: SessionScope) -> str:
    return _canonical_json(asdict(scope))


def _relative_file_path(path: object) -> bool:
    if not isinstance(path, str) or not path or "\\" in path:
        return False
    if any((ord(char) < 32) or (127 <= ord(char) <= 159) for char in path):
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    candidate = Path(path)
    parts = path.split("/")
    return (
        not candidate.is_absolute()
        and all(part and not part.startswith(".") for part in parts)
        and candidate.suffix in {".md", ".txt"}
    )


def _validate_scope(scope: object) -> SessionScope:
    if not isinstance(scope, SessionScope):
        raise SessionError("SESSION_SCOPE_INVALID")
    if scope.mode == "file" and _relative_file_path(scope.target_path):
        return scope
    if scope.mode == "directory" and scope.target_path is None:
        return scope
    raise SessionError("SESSION_SCOPE_INVALID")


def _validate_title(title: object) -> str:
    if not isinstance(title, str):
        raise SessionError("SESSION_TITLE_INVALID")
    title = title.strip()
    if not 1 <= len(title) <= 120:
        raise SessionError("SESSION_TITLE_INVALID")
    return title


def _validate_client_request_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SessionError("CLIENT_REQUEST_ID_INVALID")
    return value


def _json_object_copy(value: object) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SessionError("EXECUTION_OPTIONS_INVALID")
    try:
        encoded = _canonical_json(value)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise SessionError("EXECUTION_OPTIONS_INVALID") from None
    if not isinstance(decoded, dict):
        raise SessionError("EXECUTION_OPTIONS_INVALID")
    return decoded


def _validate_submission(submission: object) -> RunSubmission:
    if not isinstance(submission, RunSubmission):
        raise SessionError("SUBMISSION_INVALID")
    client_request_id = _validate_client_request_id(submission.client_request_id)
    if not isinstance(submission.question, str) or not submission.question.strip():
        raise SessionError("QUESTION_INVALID")
    if submission.task_type not in ("files", "conversation"):
        raise SessionError("TASK_TYPE_INVALID")
    scope = _validate_scope(submission.scope)
    if submission.output_path is not None and not _relative_file_path(
        submission.output_path
    ):
        raise SessionError("OUTPUT_PATH_INVALID")
    if submission.task_type == "conversation" and submission.output_path is not None:
        raise SessionError("OUTPUT_PATH_INVALID")
    if submission.parent_run_id is not None and (
        not isinstance(submission.parent_run_id, str)
        or not submission.parent_run_id.strip()
    ):
        raise SessionError("PARENT_RUN_ID_INVALID")
    options = _json_object_copy(submission.execution_options)
    return RunSubmission(
        client_request_id=client_request_id,
        question=submission.question,
        task_type=submission.task_type,
        scope=scope,
        output_path=submission.output_path,
        parent_run_id=submission.parent_run_id,
        execution_options=options,
    )


def _parse_json_object(payload: str) -> dict:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError, RecursionError):
        raise SessionError("SESSION_STORE_ERROR") from None
    if not isinstance(value, dict):
        raise SessionError("SESSION_STORE_ERROR")
    return value


class SessionService:
    PAGE_SIZE = 20

    def __init__(
        self,
        store: SessionStore,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ):
        self.store = store
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def _new_id(self) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value:
            raise SessionError("SESSION_STORE_ERROR")
        return value

    def _workspace_identity(self, workspace: Path) -> tuple[Path, int, int]:
        try:
            resolved = Path(workspace).resolve(strict=True)
            information = os.stat(resolved)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise SessionError("WORKSPACE_NOT_FOUND") from None
        if not stat.S_ISDIR(information.st_mode):
            raise SessionError("WORKSPACE_NOT_FOUND")
        state_dir = self.store.state_dir.resolve()
        if state_dir == resolved or state_dir.is_relative_to(resolved):
            raise SessionError("STATE_DIR_INSIDE_WORKSPACE")
        return resolved, information.st_dev, information.st_ino

    @staticmethod
    def _session_from_row(row) -> SessionRecord:
        try:
            scope_data = _parse_json_object(row[5])
            scope = SessionScope(
                mode=scope_data["mode"], target_path=scope_data.get("target_path")
            )
            _validate_scope(scope)
            return SessionRecord(
                id=row[0],
                title=row[1],
                workspace_path=row[2],
                workspace_device=row[3],
                workspace_inode=row[4],
                scope=scope,
                status=row[6],
                revision=row[7],
                created_at=row[8],
                updated_at=row[9],
            )
        except (KeyError, TypeError, ValueError, SessionError) as error:
            if isinstance(error, SessionError) and error.code == "SESSION_STORE_ERROR":
                raise
            raise SessionError("SESSION_STORE_ERROR") from error

    @staticmethod
    def _run_from_row(row) -> RunRecord:
        try:
            request = _parse_json_object(row[3])
            scope_data = request["scope"]
            submission = RunSubmission(
                client_request_id=row[2],
                question=request["question"],
                task_type=request["task_type"],
                scope=SessionScope(
                    mode=scope_data["mode"],
                    target_path=scope_data.get("target_path"),
                ),
                output_path=request.get("output_path"),
                parent_run_id=request.get("parent_run_id"),
                execution_options=request["execution_options"],
            )
            submission = _validate_submission(submission)
            return RunRecord(
                id=row[0],
                session_id=row[1],
                client_request_id=row[2],
                submission=submission,
                state=row[4],
                phase=row[5],
                stop_reason=row[6],
                started_at=row[7],
                finished_at=row[8],
            )
        except (KeyError, TypeError, ValueError, SessionError) as error:
            if isinstance(error, SessionError) and error.code == "SESSION_STORE_ERROR":
                raise
            raise SessionError("SESSION_STORE_ERROR") from error

    @staticmethod
    def _message_from_row(row) -> MessageRecord:
        payload = _parse_json_object(row[7])
        return MessageRecord(
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

    def create(
        self, workspace: Path, title: str, scope: SessionScope
    ) -> SessionRecord:
        title = _validate_title(title)
        scope = _validate_scope(scope)
        resolved, device, inode = self._workspace_identity(workspace)
        session_id = self._new_id()
        now = self._clock()
        try:
            with self.store.transaction() as connection:
                connection.execute(
                    """INSERT INTO sessions (
                           id, title, workspace_path, workspace_device,
                           workspace_inode, scope_json, status, revision,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)""",
                    (
                        session_id,
                        title,
                        str(resolved),
                        device,
                        inode,
                        _scope_json(scope),
                        now,
                        now,
                    ),
                )
        except (sqlite3.DatabaseError, StoreError, OSError):
            raise SessionError("SESSION_STORE_ERROR") from None
        return self.load(session_id)

    def load(self, session_id: str) -> SessionRecord:
        try:
            row = self.store.connection().execute(
                """SELECT id, title, workspace_path, workspace_device,
                          workspace_inode, scope_json, status, revision,
                          created_at, updated_at
                   FROM sessions WHERE id=?""",
                (session_id,),
            ).fetchone()
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        if row is None:
            raise SessionError("NOT_FOUND")
        return self._session_from_row(row)

    @staticmethod
    def _encode_cursor(updated_at: float, session_id: str) -> str:
        payload = _canonical_json([updated_at, session_id]).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[float, str]:
        try:
            if not isinstance(cursor, str) or not cursor:
                raise ValueError()
            padding = "=" * (-len(cursor) % 4)
            raw = base64.b64decode(
                cursor + padding, altchars=b"-_", validate=True
            )
            value = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(value, list)
                or len(value) != 2
                or type(value[0]) not in (int, float)
                or not math.isfinite(value[0])
                or not isinstance(value[1], str)
                or not value[1]
            ):
                raise ValueError()
            return float(value[0]), value[1]
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise SessionError("CURSOR_INVALID") from None

    def list(
        self,
        workspace: Path,
        *,
        archived: bool = False,
        cursor: str | None = None,
    ) -> Page:
        resolved, device, inode = self._workspace_identity(workspace)
        parameters: list = [str(resolved), device, inode]
        status = "archived" if archived else "active"
        where = "workspace_path=? AND workspace_device=? AND workspace_inode=?"
        where += " AND status=?"
        parameters.append(status)
        if cursor is not None:
            updated_at, session_id = self._decode_cursor(cursor)
            where += " AND (updated_at < ? OR (updated_at = ? AND id < ?))"
            parameters.extend((updated_at, updated_at, session_id))
        parameters.append(self.PAGE_SIZE + 1)
        try:
            rows = self.store.connection().execute(
                f"""SELECT id, title, workspace_path, workspace_device,
                           workspace_inode, scope_json, status, revision,
                           created_at, updated_at
                    FROM sessions WHERE {where}
                    ORDER BY updated_at DESC, id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        has_more = len(rows) > self.PAGE_SIZE
        rows = rows[: self.PAGE_SIZE]
        items = tuple(self._session_from_row(row) for row in rows)
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = self._encode_cursor(last.updated_at, last.id)
        return Page(items=items, next_cursor=next_cursor)

    def _change_session(
        self, session_id: str, *, title: str | None = None, status: str | None = None
    ) -> SessionRecord:
        try:
            with self.store.transaction() as connection:
                row = connection.execute(
                    "SELECT title, status FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if row is None:
                    raise SessionError("NOT_FOUND")
                new_title = row[0] if title is None else title
                new_status = row[1] if status is None else status
                if (new_title, new_status) != row:
                    connection.execute(
                        """UPDATE sessions
                           SET title=?, status=?, revision=revision+1, updated_at=?
                           WHERE id=?""",
                        (new_title, new_status, self._clock(), session_id),
                    )
        except SessionError:
            raise
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        return self.load(session_id)

    def rename(self, session_id: str, title: str) -> SessionRecord:
        return self._change_session(session_id, title=_validate_title(title))

    def archive(self, session_id: str) -> SessionRecord:
        return self._change_session(session_id, status="archived")

    def restore(self, session_id: str) -> SessionRecord:
        return self._change_session(session_id, status="active")

    def load_run(self, session_id: str, run_id: str) -> RunRecord:
        try:
            row = self.store.connection().execute(
                """SELECT id, session_id, client_request_id, request_json,
                          state, phase, stop_reason, started_at, finished_at
                   FROM runs WHERE session_id=? AND id=?""",
                (session_id, run_id),
            ).fetchone()
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        if row is None:
            raise SessionError("NOT_FOUND")
        return self._run_from_row(row)

    def load_message(self, session_id: str, message_id: str) -> MessageRecord:
        try:
            row = self.store.connection().execute(
                """SELECT id, session_id, run_id, session_seq, run_seq, role,
                          source_kind, payload_json, validation_state, created_at
                   FROM messages WHERE session_id=? AND id=?""",
                (session_id, message_id),
            ).fetchone()
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        if row is None:
            raise SessionError("NOT_FOUND")
        return self._message_from_row(row)

    def _session_for_submit(self, connection, session_id: str):
        row = connection.execute(
            """SELECT status, scope_json, workspace_path, workspace_device,
                      workspace_inode
               FROM sessions WHERE id=?""",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionError("NOT_FOUND")
        if row[0] != "active":
            raise SessionError("SESSION_ARCHIVED")
        scope_data = _parse_json_object(row[1])
        try:
            scope = _validate_scope(
                SessionScope(scope_data["mode"], scope_data.get("target_path"))
            )
        except (KeyError, SessionError) as error:
            raise SessionError("SESSION_STORE_ERROR") from error
        return scope, row[2], row[3], row[4]

    @staticmethod
    def _check_workspace_identity(
        workspace_path: str, workspace_device: int, workspace_inode: int
    ) -> None:
        try:
            resolved = Path(workspace_path).resolve(strict=True)
            information = os.stat(resolved)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise SessionError("WORKSPACE_CHANGED") from None
        if (
            not stat.S_ISDIR(information.st_mode)
            or str(resolved) != workspace_path
            or (information.st_dev, information.st_ino)
            != (workspace_device, workspace_inode)
        ):
            raise SessionError("WORKSPACE_CHANGED")

    def _submit_in_transaction(
        self, connection, session_id: str, submission: RunSubmission
    ) -> PreparedRun:
        session_scope, workspace_path, workspace_device, workspace_inode = (
            self._session_for_submit(connection, session_id)
        )
        fingerprint = submission.fingerprint()
        existing = connection.execute(
            """SELECT id, request_fingerprint, request_json, state, phase,
                      stop_reason, started_at, finished_at, client_request_id,
                      session_id
               FROM runs WHERE session_id=? AND client_request_id=?""",
            (session_id, submission.client_request_id),
        ).fetchone()
        if existing is not None:
            if existing[1] != fingerprint:
                raise SessionError("SESSION_REQUEST_CONFLICT")
            run_row = (
                existing[0],
                existing[9],
                existing[8],
                existing[2],
                existing[3],
                existing[4],
                existing[5],
                existing[6],
                existing[7],
            )
            stored = self._run_from_row(run_row)
            return PreparedRun(
                stored.id,
                session_id,
                False,
                stored.submission,
                RunJournal(
                    self.store,
                    session_id,
                    stored.id,
                    clock=self._clock,
                    id_factory=self._new_id,
                ),
            )
        if submission.scope != session_scope:
            raise SessionError("SESSION_SCOPE_MISMATCH")
        if submission.task_type == "files":
            self._check_workspace_identity(
                workspace_path, workspace_device, workspace_inode
            )
        if submission.parent_run_id is not None:
            parent = connection.execute(
                "SELECT 1 FROM runs WHERE session_id=? AND id=?",
                (session_id, submission.parent_run_id),
            ).fetchone()
            if parent is None:
                raise SessionError("NOT_FOUND")

        run_id = self._new_id()
        message_id = self._new_id()
        now = self._clock()
        request_json = _canonical_json(_submission_payload(submission))
        options_json = _canonical_json(submission.execution_options)
        connection.execute(
            """INSERT INTO runs (
                   id, session_id, client_request_id, request_json,
                   request_fingerprint, task_type, question, scope_json,
                   output_path, parent_run_id, state, phase, config_json,
                   system_version, tool_version, protocol_version, started_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'submitted',
                         ?, 'system-v1', 'tool-v1', 'protocol-v1', ?)""",
            (
                run_id,
                session_id,
                submission.client_request_id,
                request_json,
                fingerprint,
                submission.task_type,
                submission.question,
                _scope_json(submission.scope),
                submission.output_path,
                submission.parent_run_id,
                options_json,
                now,
            ),
        )
        session_seq = connection.execute(
            "SELECT COALESCE(MAX(session_seq), 0) + 1 FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO messages (
                   id, session_id, run_id, session_seq, run_seq, role,
                   source_kind, payload_json, validation_state, created_at
               ) VALUES (?, ?, ?, ?, 1, 'user', 'user', ?, 'valid', ?)""",
            (
                message_id,
                session_id,
                run_id,
                session_seq,
                _canonical_json({"content": submission.question}),
                now,
            ),
        )
        connection.execute(
            """UPDATE sessions
               SET revision=revision+1, updated_at=? WHERE id=?""",
            (now, session_id),
        )
        return PreparedRun(
            run_id,
            session_id,
            True,
            submission,
            RunJournal(
                self.store,
                session_id,
                run_id,
                clock=self._clock,
                id_factory=self._new_id,
            ),
        )

    def submit(
        self, session_id: str, submission: RunSubmission
    ) -> PreparedRun:
        submission = _validate_submission(submission)
        try:
            with self.store.transaction() as connection:
                return self._submit_in_transaction(connection, session_id, submission)
        except SessionError:
            raise
        except (sqlite3.DatabaseError, StoreError, OSError):
            raise SessionError("SESSION_STORE_ERROR") from None

    def continue_interrupted(
        self, session_id: str, run_id: str, client_request_id: str
    ) -> PreparedRun:
        client_request_id = _validate_client_request_id(client_request_id)
        try:
            with self.store.transaction() as connection:
                row = connection.execute(
                    """SELECT id, session_id, client_request_id, request_json,
                              state, phase, stop_reason, started_at, finished_at
                       FROM runs WHERE session_id=? AND id=?""",
                    (session_id, run_id),
                ).fetchone()
                if row is None:
                    raise SessionError("NOT_FOUND")
                parent = self._run_from_row(row)
                if parent.state != "interrupted":
                    raise SessionError("RUN_NOT_INTERRUPTED")
                submission = _validate_submission(
                    RunSubmission(
                        client_request_id=client_request_id,
                        question=parent.submission.question,
                        task_type=parent.submission.task_type,
                        scope=parent.submission.scope,
                        output_path=None,
                        parent_run_id=parent.id,
                        execution_options={},
                    )
                )
                return self._submit_in_transaction(connection, session_id, submission)
        except SessionError:
            raise
        except (sqlite3.DatabaseError, StoreError, OSError):
            raise SessionError("SESSION_STORE_ERROR") from None

    def _current_user_seq(self, session_id: str, run_id: str) -> int:
        try:
            row = self.store.connection().execute(
                """SELECT session_seq FROM messages
                   WHERE session_id=? AND run_id=? AND role='user'
                   ORDER BY run_seq LIMIT 1""",
                (session_id, run_id),
            ).fetchone()
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        if row is None:
            raise SessionError("SESSION_STORE_ERROR")
        return row[0]

    def _stored_result(self, prepared: PreparedRun) -> dict:
        try:
            row = self.store.connection().execute(
                """SELECT state, phase, stop_reason, result_json
                   FROM runs WHERE session_id=? AND id=?""",
                (prepared.session_id, prepared.run_id),
            ).fetchone()
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        if row is None:
            raise SessionError("NOT_FOUND")
        if row[3] is not None:
            result = _parse_json_object(row[3])
            result["idempotent_replay"] = True
            return result
        return {
            "run_id": prepared.run_id,
            "state": row[0],
            "phase": row[1],
            "stop_reason": row[2],
            "answer": None,
            "idempotent_replay": True,
        }

    def execute(self, prepared: PreparedRun, provider, trace, *, approvals=None,
                control=None, config=None) -> dict:
        """Build a fresh run-local authority set and execute one durable submission."""
        if not isinstance(prepared, PreparedRun):
            raise SessionError("SUBMISSION_INVALID")
        if not prepared.created:
            return self._stored_result(prepared)

        from .context import ContextBuilder
        from .conversation import ConversationPolicy, SessionTaskPolicy
        from .discovery import DirectoryTools
        from .file_tools import adapt_tools
        from .files import ReadFile
        from .runtime import RunConfig, Runtime
        from .session_history import SessionHistoryTool
        from .tool_runtime import ToolRegistry, ToolRuntime

        session = self.load(prepared.session_id)
        before_seq = self._current_user_seq(prepared.session_id, prepared.run_id)
        history = SessionHistoryTool(
            self.store, prepared.session_id, before_seq=before_seq
        )
        target = None

        def fail_before_provider(code):
            result = {
                "run_id": prepared.run_id,
                "state": "failed",
                "stop_reason": code,
                "model_calls": 0,
                "answer": None,
                "provider": getattr(provider, "metadata", {}),
                "trace_path": str(getattr(trace, "path", "")),
            }
            try:
                prepared.journal.finish_run(result)
            except StoreError:
                result["stop_reason"] = "SESSION_STORE_ERROR"
            return result

        try:
            if prepared.submission.task_type == "files":
                self._check_workspace_identity(
                    session.workspace_path,
                    session.workspace_device,
                    session.workspace_inode,
                )
                workspace = Path(session.workspace_path)
                if session.scope.mode == "file":
                    target = session.scope.target_path
                    engine = adapt_tools(
                        ReadFile(workspace, {target}), prepared.submission.output_path
                    )
                else:
                    engine = adapt_tools(
                        DirectoryTools(workspace), prepared.submission.output_path
                    )
                if (prepared.submission.output_path is not None
                        and self.store.inspect_unknown_publication(
                            prepared.session_id, prepared.submission.output_path
                        ) is not None):
                    return fail_before_provider("WRITE_OUTCOME_UNKNOWN")
                engine.registry.register(history)
                engine.policy = SessionTaskPolicy(engine.policy)
                policy = engine.policy
            else:
                policy = ConversationPolicy(
                    self.store, prepared.session_id, before_seq=before_seq
                )
                engine = ToolRuntime(ToolRegistry([history]), policy)
        except SessionError as error:
            if error.code in {"WORKSPACE_CHANGED", "WORKSPACE_NOT_FOUND"}:
                return fail_before_provider("WORKSPACE_UNAVAILABLE")
            raise
        except Exception:
            return fail_before_provider("WORKSPACE_UNAVAILABLE")

        context = ContextBuilder(self.store, prepared.session_id, prepared.run_id)

        class RequestBuilder:
            def build(inner_self, request, limit):
                built = context.build(request, limit)
                setter = getattr(policy, "set_visible_messages", None)
                if callable(setter):
                    setter(built.manifest["selected_message_ids"])
                return built

        if approvals is not None:
            if getattr(approvals, "run_id", prepared.run_id) != prepared.run_id:
                raise SessionError("APPROVAL_NOT_FOUND")
            if getattr(approvals, "journal", None) is None:
                approvals.journal = prepared.journal

        if config is None:
            try:
                config = RunConfig(**dict(prepared.submission.execution_options))
            except (TypeError, ValueError):
                result = {
                    "run_id": prepared.run_id,
                    "state": "failed",
                    "stop_reason": "INVALID_CONFIG",
                    "model_calls": 0,
                    "answer": None,
                    "provider": getattr(provider, "metadata", {}),
                    "trace_path": str(getattr(trace, "path", "")),
                }
                prepared.journal.finish_run(result)
                return result
        return Runtime(
            provider,
            engine,
            trace,
            config,
            approvals=approvals,
            control=control,
            journal=prepared.journal,
            request_builder=RequestBuilder(),
        ).run(prepared.submission.question, target)
