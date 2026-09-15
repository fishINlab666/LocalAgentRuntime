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
from .imports import ImportStore, ImportStoreError, PublishedImport
from .state_maintenance import StateBusy


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
    skill_id: str | None = None
    mcp_prompt: dict | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_options", _freeze(self.execution_options))
        if self.mcp_prompt is not None:
            object.__setattr__(self, "mcp_prompt", _freeze(self.mcp_prompt))

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
    agent_id: str = "legacy"
    agent_revision: str = "legacy"
    agent_snapshot: dict = field(default_factory=dict)
    import_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_snapshot", _freeze(self.agent_snapshot))


@dataclass(frozen=True)
class ResolvedWorkspace:
    read_root: Path
    read_identity: tuple[int, int]
    write_root: Path
    write_identity: tuple[int, int]
    source_mapper: object | None = None


class ManagedWorkspaceResolver:
    def __init__(self, imports: ImportStore):
        self.imports = imports

    def resolve(self, session: SessionRecord) -> ResolvedWorkspace:
        if session.import_id is None:
            SessionService._check_workspace_identity(
                session.workspace_path, session.workspace_device, session.workspace_inode)
            identity = (session.workspace_device, session.workspace_inode)
            return ResolvedWorkspace(Path(session.workspace_path), identity,
                                     Path(session.workspace_path), identity)
        try:
            if not self.imports._is_managed_id(session.import_id):
                raise SessionError('IMPORT_INTEGRITY_ERROR')
            bound = self.imports.store.connection().execute(
                'SELECT import_id, workspace_device, workspace_inode, agent_id FROM sessions WHERE id=?',
                (session.id,)).fetchone()
            if bound != (session.import_id, session.workspace_device, session.workspace_inode, session.agent_id):
                raise SessionError('IMPORT_INTEGRITY_ERROR')
            workspace = self.imports.open(session.import_id)
            if (session.workspace_device, session.workspace_inode) != (
                    workspace.workspace_device, workspace.workspace_inode):
                raise SessionError('IMPORT_INTEGRITY_ERROR')
            from .import_tools import ImportedDocumentError, ImportedSourceMapper
            try:
                source_mapper = ImportedSourceMapper(workspace)
            except ImportedDocumentError:
                raise SessionError('IMPORT_INTEGRITY_ERROR') from None
            return ResolvedWorkspace(workspace.workspace,
                (workspace.workspace_device, workspace.workspace_inode), workspace.artifacts,
                (workspace.artifact_device, workspace.artifact_inode), source_mapper)
        except ImportStoreError as error:
            raise SessionError(error.code) from None
        except (sqlite3.Error, StoreError):
            raise SessionError('SESSION_STORE_ERROR') from None


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
    agent_id: str = "legacy"
    agent_revision: str = "legacy"
    agent_snapshot: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_snapshot", _freeze(self.agent_snapshot))


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
    payload = {
        "question": submission.question,
        "task_type": submission.task_type,
        "scope": asdict(submission.scope),
        "output_path": submission.output_path,
        "parent_run_id": submission.parent_run_id,
        "execution_options": submission.execution_options,
    }
    # Keep fingerprints of pre-extension submissions stable.
    if submission.skill_id is not None:
        payload['skill_id'] = submission.skill_id
    if submission.mcp_prompt is not None:
        payload['mcp_prompt'] = submission.mcp_prompt
    return payload


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
    if submission.skill_id is not None and (
            not isinstance(submission.skill_id, str) or not submission.skill_id
            or len(submission.skill_id) > 128):
        raise SessionError('SUBMISSION_INVALID')
    prompt = submission.mcp_prompt
    if prompt is not None and (not isinstance(prompt, dict)
            or set(prompt) != {'server_id', 'name'}
            or any(not isinstance(v, str) or not v or len(v) > 256 for v in prompt.values())):
        raise SessionError('SUBMISSION_INVALID')
    if submission.task_type == 'conversation' and (submission.skill_id or prompt):
        raise SessionError('SUBMISSION_INVALID')
    return RunSubmission(
        client_request_id=client_request_id,
        question=submission.question,
        task_type=submission.task_type,
        scope=scope,
        output_path=submission.output_path,
        parent_run_id=submission.parent_run_id,
        execution_options=options,
        skill_id=submission.skill_id,
        mcp_prompt=prompt,
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
        imports: ImportStore | None = None,
        fault=None,
    ):
        self.store = store
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self.imports = imports if imports is not None else ImportStore(store)
        if self.imports.store is not store:
            raise SessionError('SESSION_STORE_ERROR')
        self.resolver = ManagedWorkspaceResolver(self.imports)
        self._fault = fault or (lambda _point: None)

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
                agent_id=row[10],
                agent_revision=row[11],
                agent_snapshot=_parse_json_object(row[12]),
                import_id=row[13],
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
                skill_id=request.get('skill_id'),
                mcp_prompt=request.get('mcp_prompt'),
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
                agent_id=row[9] if len(row) > 9 else "legacy",
                agent_revision=row[10] if len(row) > 9 else "legacy",
                agent_snapshot=_parse_json_object(row[11]) if len(row) > 9 else {},
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
        self, workspace: Path, title: str, scope: SessionScope,
        *, agent_snapshot: dict | None = None,
    ) -> SessionRecord:
        title = _validate_title(title)
        scope = _validate_scope(scope)
        from .agents import AgentDefinition, AgentError, builtin_agent

        try:
            agent = (builtin_agent(scope.mode) if agent_snapshot is None
                     else AgentDefinition.from_dict(agent_snapshot))
            snapshot = agent.to_dict()
            expected_mode = "file" if snapshot["strategy"] == "file" else "directory"
            if expected_mode != scope.mode:
                raise SessionError("AGENT_CONFIG_INVALID")
            snapshot_json = _canonical_json(snapshot)
            agent_revision = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        except (AgentError, TypeError, ValueError, KeyError, RecursionError):
            raise SessionError("AGENT_CONFIG_INVALID") from None
        resolved, device, inode = self._workspace_identity(workspace)
        session_id = self._new_id()
        now = self._clock()
        try:
            with self.store.transaction() as connection:
                connection.execute(
                    """INSERT INTO sessions (
                           id, title, workspace_path, workspace_device,
                           workspace_inode, scope_json, status, revision,
                           created_at, updated_at, agent_id, agent_revision,
                           agent_snapshot_json
                       ) VALUES (?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        title,
                        str(resolved),
                        device,
                        inode,
                        _scope_json(scope),
                        now,
                        now,
                        snapshot["id"],
                        agent_revision,
                        snapshot_json,
                    ),
                )
        except (sqlite3.DatabaseError, StoreError, OSError):
            raise SessionError("SESSION_STORE_ERROR") from None
        return self.load(session_id)

    def load(self, session_id: str, *, agent_id: str | None = None) -> SessionRecord:
        try:
            row = self.store.connection().execute(
                """SELECT id, title, workspace_path, workspace_device,
                          workspace_inode, scope_json, status, revision,
                          created_at, updated_at, agent_id, agent_revision,
                          agent_snapshot_json, import_id
                   FROM sessions WHERE id=?""",
                (session_id,),
            ).fetchone()
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        if row is None or (agent_id is not None and row[10] != agent_id):
            raise SessionError("NOT_FOUND")
        return self._session_from_row(row)

    def attach_import(self, published: PublishedImport, request: dict) -> SessionRecord:
        """Link a verified publication and its frozen assistant in one transaction."""
        from .agents import AgentCatalog, AgentError

        if not isinstance(published, PublishedImport):
            raise SessionError('IMPORT_REQUEST_INVALID')
        try:
            lock, _ = self.imports._control(published.import_id)
            with lock, self.store.maintenance_gate.start('import-link', published.import_id):
                row, files = self.imports._record(published.import_id)
                self.imports._validate_request(request)
                if self.imports._canonical_request(request) != row['request_json']:
                    raise SessionError('IMPORT_STATE_CONFLICT')
                if row['status'] == 'unavailable':
                    raise SessionError('IMPORT_UNAVAILABLE')
                verified = self.imports._load_publication(row, files)
                if verified != published:
                    raise SessionError('IMPORT_INTEGRITY_ERROR')
                existing = self.store.connection().execute(
                    'SELECT id FROM sessions WHERE import_id=?', (published.import_id,)).fetchone()
                if row['status'] == 'ready':
                    self.imports.open(published.import_id)
                    if existing is None:
                        raise SessionError('IMPORT_INTEGRITY_ERROR')
                    session = self.load(existing[0])
                    self.resolver.resolve(session)
                    return session
                if existing is not None or row['status'] != 'finalizing':
                    raise SessionError('IMPORT_STATE_CONFLICT')
                catalog = AgentCatalog(self.store.state_dir / 'agents')
                agent = catalog.get(request['agent_id'])
                if not catalog.is_enabled(agent.id):
                    raise SessionError('AGENT_DISABLED')
                snapshot = agent.to_dict()
                if snapshot['strategy'] not in {'directory', 'combined'} or not {
                        'list_files', 'read_file', 'search_documents'}.issubset(snapshot['tools']):
                    raise SessionError('AGENT_CONFIG_INVALID')
                title = _validate_title(request['name'])
                session_id, now = self._new_id(), self._clock()
                snapshot_json = _canonical_json(snapshot)
                revision = hashlib.sha256(snapshot_json.encode()).hexdigest()
                self._fault('before_link_transaction')
                with self.store.transaction() as connection:
                    current = connection.execute(
                        'SELECT status,manifest_sha256,request_fingerprint FROM imports WHERE id=?',
                        (published.import_id,)).fetchone()
                    if current != ('finalizing', published.manifest_sha256, published.request_fingerprint):
                        raise SessionError('IMPORT_STATE_CONFLICT')
                    for item in verified.file_records:
                        changed = connection.execute(
                            'UPDATE import_files SET parser_json=? WHERE import_id=? AND source_id=? AND sha256=?',
                            (item['parser_json'], published.import_id, item['source_id'], item['sha256']))
                        if changed.rowcount != 1:
                            raise SessionError('IMPORT_INTEGRITY_ERROR')
                    connection.execute(
                        """INSERT INTO sessions (
                            id,title,workspace_path,workspace_device,workspace_inode,scope_json,
                            status,revision,created_at,updated_at,agent_id,agent_revision,
                            agent_snapshot_json,import_id)
                            VALUES (?,?,?,?,?,?,'active',1,?,?,?,?,?,?)""",
                        (session_id, title, str(verified.workspace), *verified.identities['workspace'],
                         _scope_json(SessionScope('directory', None)), now, now, agent.id, revision,
                         snapshot_json, published.import_id))
                    connection.execute(
                        "UPDATE imports SET status='ready',workspace_device=?,workspace_inode=?,"
                        "artifact_device=?,artifact_inode=?,error_code=NULL,updated_at=? WHERE id=?",
                        (*verified.identities['workspace'], *verified.identities['artifacts'],
                         now, published.import_id))
                self._fault('after_link_commit')
                return self.load(session_id)
        except (ImportStoreError, AgentError, StateBusy) as error:
            raise SessionError(error.code) from None
        except (sqlite3.Error, StoreError, OSError):
            raise SessionError('SESSION_STORE_ERROR') from None

    def recover_imports(self) -> None:
        """Recover published batches without parsing or changing prior sessions."""
        try:
            for published in self.imports.recover_interrupted():
                row, _ = self.imports._record(published.import_id)
                try:
                    self.attach_import(published, json.loads(row['request_json']))
                except SessionError as error:
                    if error.code not in {'AGENT_DISABLED', 'AGENT_NOT_FOUND', 'AGENT_CONFIG_INVALID'}:
                        raise
                    with self.store.maintenance_gate.start('import-link', published.import_id), \
                            self.store.transaction() as connection:
                        connection.execute(
                            "UPDATE imports SET error_code=?,updated_at=? WHERE id=? AND status='finalizing'",
                            (error.code, self._clock(), published.import_id))
            ready = self.store.connection().execute("SELECT id FROM imports WHERE status='ready'").fetchall()
            for (import_id,) in ready:
                try:
                    self.imports.open(import_id)
                except ImportStoreError as error:
                    if error.code not in {'IMPORT_INTEGRITY_ERROR', 'IMPORT_UNAVAILABLE'}:
                        raise
        except (ImportStoreError, StateBusy) as error:
            raise SessionError(error.code) from None

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
        agent_id: str | None = None,
    ) -> Page:
        resolved, device, inode = self._workspace_identity(workspace)
        return self.list_bound(
            str(resolved), device, inode, archived=archived, cursor=cursor,
            agent_id=agent_id,
        )

    def list_bound(
        self,
        workspace_path: str,
        workspace_device: int,
        workspace_inode: int,
        *,
        archived: bool = False,
        cursor: str | None = None,
        agent_id: str | None = None,
        include_imports: bool = False,
    ) -> Page:
        """List sessions already bound to a startup identity without re-opening it."""
        if (
            not isinstance(workspace_path, str)
            or not workspace_path
            or type(workspace_device) is not int
            or type(workspace_inode) is not int
        ):
            raise SessionError("SESSION_STORE_ERROR")
        parameters: list = [workspace_path, workspace_device, workspace_inode]
        status = "archived" if archived else "active"
        where = "workspace_path=? AND workspace_device=? AND workspace_inode=?"
        if include_imports:
            where = '((import_id IS NULL AND ' + where + ') OR import_id IS NOT NULL)'
        where += " AND status=?"
        parameters.append(status)
        if agent_id is not None:
            where += " AND agent_id=?"
            parameters.append(agent_id)
        if cursor is not None:
            updated_at, session_id = self._decode_cursor(cursor)
            where += " AND (updated_at < ? OR (updated_at = ? AND id < ?))"
            parameters.extend((updated_at, updated_at, session_id))
        if not include_imports:
            parameters.append(self.PAGE_SIZE + 1)
        try:
            query = self.store.connection().execute(
                f"""SELECT id, title, workspace_path, workspace_device,
                           workspace_inode, scope_json, status, revision,
                           created_at, updated_at, agent_id, agent_revision,
                           agent_snapshot_json, import_id
                    FROM sessions WHERE {where}
                    ORDER BY updated_at DESC, id DESC {'' if include_imports else 'LIMIT ?'}""",
                parameters,
            )
            rows = []
            try:
                for row in query:
                    if include_imports and row[13] is not None:
                        try:
                            self.resolver.resolve(self._session_from_row(row))
                        except SessionError as error:
                            if not error.code.startswith('IMPORT_'):
                                raise
                            continue
                    rows.append(row)
                    if len(rows) > self.PAGE_SIZE:
                        break
            finally:
                query.close()
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
                          state, phase, stop_reason, started_at, finished_at,
                          agent_id, agent_revision, agent_snapshot_json
                   FROM runs WHERE session_id=? AND id=?""",
                (session_id, run_id),
            ).fetchone()
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        if row is None:
            raise SessionError("NOT_FOUND")
        return self._run_from_row(row)

    @staticmethod
    def _encode_run_cursor(session_id: str, started_at: float, run_id: str) -> str:
        payload = _canonical_json([session_id, started_at, run_id]).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_run_cursor(session_id: str, cursor: str) -> tuple[float, str]:
        try:
            if not isinstance(cursor, str) or not cursor:
                raise ValueError()
            padding = "=" * (-len(cursor) % 4)
            raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
            value = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(value, list)
                or len(value) != 3
                or value[0] != session_id
                or type(value[1]) not in (int, float)
                or not math.isfinite(value[1])
                or not isinstance(value[2], str)
                or not value[2]
            ):
                raise ValueError()
            return float(value[1]), value[2]
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise SessionError("NOT_FOUND") from None

    def list_runs(self, session_id: str, *, cursor: str | None = None) -> Page:
        self.load(session_id)
        parameters: list = [session_id]
        where = "session_id=?"
        if cursor is not None:
            started_at, run_id = self._decode_run_cursor(session_id, cursor)
            where += " AND (started_at < ? OR (started_at = ? AND id < ?))"
            parameters.extend((started_at, started_at, run_id))
        parameters.append(self.PAGE_SIZE + 1)
        try:
            rows = self.store.connection().execute(
                f"""SELECT id, session_id, client_request_id, request_json,
                           state, phase, stop_reason, started_at, finished_at,
                           agent_id, agent_revision, agent_snapshot_json
                    FROM runs WHERE {where}
                    ORDER BY started_at DESC, id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        except (sqlite3.DatabaseError, StoreError):
            raise SessionError("SESSION_STORE_ERROR") from None
        has_more = len(rows) > self.PAGE_SIZE
        items = tuple(self._run_from_row(row) for row in rows[: self.PAGE_SIZE])
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = self._encode_run_cursor(
                session_id, last.started_at, last.id
            )
        return Page(items=items, next_cursor=next_cursor)

    def run_view(self, session_id: str, run_id: str) -> dict:
        """Return one session-scoped durable run view for CLI and HTTP."""
        run = self.load_run(session_id, run_id)
        try:
            connection = self.store.connection()
            metadata = connection.execute(
                """SELECT result_json, trace_path, provider_json
                   FROM runs WHERE session_id=? AND id=?""",
                (session_id, run_id),
            ).fetchone()
            tool_rows = connection.execute(
                """SELECT call_id, name, stage, assistant_message_id,
                          result_message_id, publication_state, recovery_state
                   FROM tool_calls WHERE session_id=? AND run_id=?
                   ORDER BY rowid""",
                (session_id, run_id),
            ).fetchall()
            approval_rows = connection.execute(
                """SELECT id, call_id, preview_json, decision, created_at,
                          decided_at, invalidated_at
                   FROM approvals WHERE session_id=? AND run_id=?
                   ORDER BY created_at, id""",
                (session_id, run_id),
            ).fetchall()
            artifact_rows = connection.execute(
                """SELECT id, call_id, path, bytes, sha256, receipt_json,
                          recovery_state, confirmed_at
                   FROM artifacts WHERE session_id=? AND run_id=?
                   ORDER BY confirmed_at, id""",
                (session_id, run_id),
            ).fetchall()
            manifest_rows = connection.execute(
                """SELECT request_seq, payload_json, input_sha256, input_bytes,
                          created_at
                   FROM context_manifests WHERE session_id=? AND run_id=?
                   ORDER BY request_seq""",
                (session_id, run_id),
            ).fetchall()
            messages = self.store.load_run_messages(session_id, run_id)
        except StoreError as error:
            if error.code == "NOT_FOUND":
                raise SessionError("NOT_FOUND") from None
            raise SessionError("SESSION_STORE_ERROR") from None
        except sqlite3.DatabaseError:
            raise SessionError("SESSION_STORE_ERROR") from None

        def parsed(value):
            if value is None:
                return None
            return _parse_json_object(value)

        return {
            "id": run.id,
            "session_id": run.session_id,
            "agent_id": run.agent_id,
            "agent_revision": run.agent_revision,
            "agent_snapshot": run.agent_snapshot,
            "client_request_id": run.client_request_id,
            "task_type": run.submission.task_type,
            "question": run.submission.question,
            "output_file": run.submission.output_path,
            "parent_run_id": run.submission.parent_run_id,
            "state": run.state,
            "phase": run.phase,
            "stop_reason": run.stop_reason,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "result": parsed(metadata[0]),
            "trace_path": metadata[1],
            "provider": parsed(metadata[2]),
            "messages": [
                {
                    "id": item.id,
                    "role": item.role,
                    "source_kind": item.source_kind,
                    "payload": item.payload,
                    "validation_state": item.validation_state,
                    "session_seq": item.session_seq,
                    "run_seq": item.run_seq,
                    "created_at": item.created_at,
                }
                for item in messages
            ],
            "tool_calls": [
                {
                    "call_id": row[0], "name": row[1], "stage": row[2],
                    "assistant_message_id": row[3], "result_message_id": row[4],
                    "publication_state": row[5], "recovery_state": row[6],
                }
                for row in tool_rows
            ],
            "approvals": [
                {
                    "id": row[0], "call_id": row[1], "preview": parsed(row[2]),
                    "decision": row[3], "created_at": row[4],
                    "decided_at": row[5], "invalidated_at": row[6],
                }
                for row in approval_rows
            ],
            "artifacts": [
                {
                    "id": row[0], "call_id": row[1], "path": row[2],
                    "bytes": row[3], "sha256": row[4], "receipt": parsed(row[5]),
                    "recovery_state": row[6], "confirmed_at": row[7],
                }
                for row in artifact_rows
            ],
            "context_manifests": [
                {
                    "request_seq": row[0], "payload": parsed(row[1]),
                    "input_sha256": row[2], "input_bytes": row[3],
                    "created_at": row[4],
                }
                for row in manifest_rows
            ],
            "revision": len(messages) + len(tool_rows) + (run.finished_at is not None),
        }

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
                      workspace_inode, agent_id, agent_revision, agent_snapshot_json
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
        return scope, row[2], row[3], row[4], row[5], row[6], row[7]

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
        (session_scope, workspace_path, workspace_device, workspace_inode,
         agent_id, agent_revision, agent_snapshot_json) = (
            self._session_for_submit(connection, session_id)
        )
        fingerprint = submission.fingerprint()
        existing = connection.execute(
            """SELECT id, request_fingerprint, request_json, state, phase,
                      stop_reason, started_at, finished_at, client_request_id,
                      session_id, agent_id, agent_revision, agent_snapshot_json
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
                existing[10],
                existing[11],
                existing[12],
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
                   system_version, tool_version, protocol_version, started_at,
                   agent_id, agent_revision, agent_snapshot_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'submitted',
                         ?, 'system-v1', 'tool-v1', 'protocol-v1', ?, ?, ?, ?)""",
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
                agent_id,
                agent_revision,
                agent_snapshot_json,
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
            with self.store.maintenance_gate.start('run-submit', uuid.uuid4().hex):
                if not self._has_submission(session_id, submission.client_request_id):
                    self._resolve_submission(self.load(session_id), submission)
                with self.store.transaction() as connection:
                    return self._submit_in_transaction(connection, session_id, submission)
        except StateBusy as error:
            raise SessionError(error.code) from None
        except SessionError:
            raise
        except (sqlite3.DatabaseError, StoreError, OSError):
            raise SessionError("SESSION_STORE_ERROR") from None

    def continue_interrupted(
        self, session_id: str, run_id: str, client_request_id: str
    ) -> PreparedRun:
        client_request_id = _validate_client_request_id(client_request_id)
        try:
            with self.store.maintenance_gate.start('run-submit', uuid.uuid4().hex):
                parent = self.load_run(session_id, run_id)
                if not self._has_submission(session_id, client_request_id):
                    self._resolve_submission(self.load(session_id), parent.submission)
                return self._continue_interrupted(session_id, run_id, client_request_id)
        except StateBusy as error:
            raise SessionError(error.code) from None

    def _continue_interrupted(self, session_id, run_id, client_request_id):
        try:
            with self.store.transaction() as connection:
                row = connection.execute(
                    """SELECT id, session_id, client_request_id, request_json,
                              state, phase, stop_reason, started_at, finished_at,
                              agent_id, agent_revision, agent_snapshot_json
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
                        skill_id=parent.submission.skill_id,
                        mcp_prompt=parent.submission.mcp_prompt,
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

    def _has_submission(self, session_id, client_request_id):
        return self.store.connection().execute(
            'SELECT 1 FROM runs WHERE session_id=? AND client_request_id=?',
            (session_id, client_request_id)).fetchone() is not None

    def _resolve_submission(self, session, submission):
        if submission.task_type != 'files':
            return None
        if session.import_id is not None and submission.output_path is not None:
            raise SessionError('IMPORT_OUTPUT_UNSUPPORTED')
        return self.resolver.resolve(session)

    def import_history_bound(self, session: SessionRecord) -> bool:
        if not self.imports._is_managed_id(session.import_id):
            return False
        row = self.store.connection().execute(
            'SELECT 1 FROM sessions s JOIN imports i ON s.import_id=i.id '
            'WHERE s.id=? AND i.id=? AND s.agent_id=i.agent_id AND s.agent_id=?',
            (session.id, session.import_id, session.agent_id)).fetchone()
        return row is not None

    def trace_root(self, session: SessionRecord, *, task_type='files') -> Path:
        if task_type == 'files':
            return self.resolver.resolve(session).read_root
        if session.import_id is not None:
            if not self.import_history_bound(session):
                raise SessionError('IMPORT_INTEGRITY_ERROR')
            return self.imports.root / session.import_id / 'workspace'
        return Path(session.workspace_path)

    @staticmethod
    def _execution_failure(prepared, provider, trace, code):
        result = {'run_id': prepared.run_id, 'state': 'failed', 'stop_reason': code,
                  'model_calls': 0, 'answer': None, 'provider': getattr(provider, 'metadata', {}),
                  'trace_path': str(getattr(trace, 'path', ''))}
        try:
            prepared.journal.finish_run(result)
        except StoreError:
            result['stop_reason'] = 'SESSION_STORE_ERROR'
        return result

    def execute(self, prepared: PreparedRun, provider, trace, *, approvals=None,
                control=None, config=None) -> dict:
        if not isinstance(prepared, PreparedRun):
            raise SessionError('SUBMISSION_INVALID')
        if not prepared.created:
            return self._stored_result(prepared)
        try:
            with self.store.maintenance_gate.start('run-execute', prepared.run_id):
                return self._execute_prepared(prepared, provider, trace, approvals=approvals,
                                              control=control, config=config)
        except StateBusy as error:
            return self._execution_failure(prepared, provider, trace, error.code)

    def _execute_prepared(self, prepared: PreparedRun, provider, trace, *, approvals=None,
                control=None, config=None) -> dict:
        """Build a fresh run-local authority set and execute one durable submission."""
        if not isinstance(prepared, PreparedRun):
            raise SessionError("SUBMISSION_INVALID")
        if not prepared.created:
            return self._stored_result(prepared)

        from .agents import AgentCatalog, AgentDefinition, AgentError
        from .agent_runtime import AuthorityPolicy, CapabilityStore, ExtensionPolicy, assemble
        from .approvals import RunControl
        import threading
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
        target = None
        agent = AgentDefinition.from_dict(dict(session.agent_snapshot))
        catalog = AgentCatalog(self.store.state_dir / 'agents')
        control = control or RunControl(threading.Event())
        assembly = None

        def fail_before_provider(code):
            if assembly is not None:
                assembly.close()
            return self._execution_failure(prepared, provider, trace, code)

        try:
            limits = {key: value for key, value in agent.to_dict()['budgets'].items()
                      if key not in {'max_files', 'max_file_bytes'}}
            if config is None:
                effective = {**limits, **dict(prepared.submission.execution_options)}
                config = RunConfig(**effective)
            if any(value > limits[key] for key, value in asdict(config).items()):
                raise ValueError('Budget may only be narrowed')
            control.run_timeout = min(control.run_timeout, config.run_timeout)
        except (TypeError, ValueError, KeyError):
            return fail_before_provider('INVALID_CONFIG')

        try:
            if not catalog.is_enabled(agent.id):
                return fail_before_provider('AGENT_DISABLED')
            if prepared.submission.task_type == "files":
                resolved = self._resolve_submission(session, prepared.submission)
                workspace = resolved.read_root
                target = session.scope.target_path
                assembly = assemble(agent, workspace, target, prepared.submission.output_path,
                    CapabilityStore(self.store.state_dir), run_id=prepared.run_id, control=control,
                    agent_catalog=catalog, selected_skill=prepared.submission.skill_id,
                    selected_prompt=prepared.submission.mcp_prompt,
                    source_mapper=resolved.source_mapper)
                engine = assembly.engine
                if (engine.policy.tool.workspace != resolved.read_root
                        or engine.policy.tool.workspace_identity != resolved.read_identity):
                    return fail_before_provider('IMPORT_INTEGRITY_ERROR'
                                                if session.import_id is not None else 'WORKSPACE_UNAVAILABLE')
                if (prepared.submission.output_path is not None
                        and self.store.inspect_unknown_publication(
                            prepared.session_id, prepared.submission.output_path
                        ) is not None):
                    return fail_before_provider("WRITE_OUTCOME_UNKNOWN")
                if isinstance(engine.policy, ExtensionPolicy):
                    engine.policy.base = SessionTaskPolicy(engine.policy.base)
                else:
                    engine.policy = SessionTaskPolicy(engine.policy)
                policy = engine.policy
                if 'session_history' in agent.tools:
                    history = SessionHistoryTool(
                        self.store, prepared.session_id, before_seq=before_seq,
                        result_fields=policy.result_fields,
                    )
                    engine.registry.register(history)
            else:
                policy = ConversationPolicy(
                    self.store, prepared.session_id, before_seq=before_seq
                )
                policy.agent = agent
                tools = []
                if 'session_history' in agent.tools:
                    tools.append(SessionHistoryTool(
                        self.store, prepared.session_id, before_seq=before_seq,
                        result_fields=policy.result_fields))
                policy = AuthorityPolicy(policy, agent, catalog)
                engine = ToolRuntime(ToolRegistry(tools), policy)
        except AgentError as error:
            return fail_before_provider(error.code)
        except SessionError as error:
            if error.code in {"WORKSPACE_CHANGED", "WORKSPACE_NOT_FOUND"}:
                return fail_before_provider("WORKSPACE_UNAVAILABLE")
            return fail_before_provider(error.code)
        except Exception as error:
            return fail_before_provider(getattr(error, 'code', 'WORKSPACE_UNAVAILABLE'))

        context = ContextBuilder(self.store, prepared.session_id, prepared.run_id)

        class RequestBuilder:
            def build(inner_self, request, limit, *, summarize=None):
                built = context.build(request, limit, summarize=summarize)
                built.manifest['agent'] = {'id': agent.id, 'revision': agent.revision}
                if assembly is not None:
                    built.manifest['capabilities'] = assembly.manifest
                setter = getattr(policy, "set_visible_messages", None)
                if callable(setter):
                    setter(built.manifest["selected_message_ids"])
                return built

        if approvals is not None:
            if getattr(approvals, "run_id", prepared.run_id) != prepared.run_id:
                raise SessionError("APPROVAL_NOT_FOUND")
            if getattr(approvals, "journal", None) is None:
                approvals.journal = prepared.journal

        try:
            return Runtime(
                provider, engine, trace, config, approvals=approvals, control=control,
                journal=prepared.journal, request_builder=RequestBuilder(),
            ).run(prepared.submission.question, target)
        finally:
            if assembly is not None:
                assembly.close()

    def backup(self, destination: Path) -> dict:
        try:
            path = self.store.backup(destination)
            connection = self.store.connection()
            sessions = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            return {"backup_path": str(path), "sessions": sessions, "runs": runs}
        except StoreError:
            raise
        except sqlite3.DatabaseError:
            raise SessionError("SESSION_STORE_ERROR") from None
