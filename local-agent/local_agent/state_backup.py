"""Atomic SQLite and managed-import backup bundles."""

from dataclasses import dataclass
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import tempfile
import time
import uuid

from .imports import ImportStore, ImportStoreError
from .session_store import RunJournal, SessionStore, StoreError
from .state_maintenance import StateBusy


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 32 * 1024


@dataclass(frozen=True)
class BackupBundle:
    root: Path
    database: Path
    sidecar: Path
    manifest: Path
    counts: dict


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _object(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    result = json.loads(raw, object_pairs_hook=unique)
    if _canonical(result) != raw:
        raise ValueError("noncanonical JSON")
    return result


def _identity(details) -> tuple[int, int]:
    return details.st_dev, details.st_ino


def _version(details) -> tuple:
    return (
        details.st_dev, details.st_ino, details.st_mode, details.st_nlink,
        details.st_size, details.st_mtime_ns, details.st_ctime_ns,
    )


def _safe_component(value: str) -> bool:
    return (
        isinstance(value, str) and value not in {"", ".", ".."}
        and "/" not in value and "\\" not in value
        and not any(ord(character) < 32 or 127 <= ord(character) <= 159
                    for character in value)
    )


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute() and all(_safe_component(part) for part in path.parts)
        and path.as_posix() == value
    )


def _open_directory(path: Path, *, private: bool = True) -> int:
    path = Path(path)
    before = os.lstat(path)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise OSError(errno.ENOTDIR, "unsafe directory")
    descriptor = os.open(path, _DIR_FLAGS)
    after = os.fstat(descriptor)
    if (_identity(before) != _identity(after)
            or private and stat.S_IMODE(after.st_mode) != 0o700):
        os.close(descriptor)
        raise OSError(errno.ESTALE, "directory changed")
    return descriptor


def _open_directory_at(parent_fd: int, name: str, *, private: bool = True) -> int:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise OSError(errno.ENOTDIR, "unsafe directory")
    descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    after = os.fstat(descriptor)
    if (_identity(before) != _identity(after)
            or private and stat.S_IMODE(after.st_mode) != 0o700):
        os.close(descriptor)
        raise OSError(errno.ESTALE, "directory changed")
    return descriptor


def _pinned_regular_at(parent_fd: int, name: str,
                       identity: tuple[int, int] | None = None) -> tuple[int, int]:
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
            or stat.S_IMODE(named.st_mode) != 0o600
            or identity is not None and _identity(named) != identity):
        raise OSError(errno.ESTALE, "backup file changed")
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if _version(opened) != _version(named):
            raise OSError(errno.ESTALE, "backup file changed")
        return _identity(opened)
    finally:
        os.close(descriptor)


def _read_regular_at(parent_fd: int, name: str, *, maximum: int | None = None) -> bytes:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or maximum is not None and before.st_size > maximum):
            raise OSError(errno.EPERM, "unsafe backup file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if maximum is not None and total > maximum:
                raise OSError(errno.EFBIG, "backup file too large")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if total != before.st_size or _version(before) != _version(after) \
                or _version(after) != _version(named):
            raise OSError(errno.ESTALE, "backup file changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_regular_at(parent_fd: int, name: str, raw: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view[:64 * 1024])
            if written <= 0:
                raise OSError(errno.EIO, "backup write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        current = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(current) != _identity(named):
            raise OSError(errno.ESTALE, "backup output changed")
    finally:
        os.close(descriptor)


def _copy_regular_at(source_fd: int, source_name: str, destination_fd: int,
                     destination_name: str, *, maximum: int | None = None,
                     expected: tuple[int, str] | None = None) -> dict:
    source = os.open(source_name, _FILE_FLAGS, dir_fd=source_fd)
    destination = None
    try:
        before = os.fstat(source)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or maximum is not None and before.st_size > maximum):
            raise OSError(errno.EPERM, "unsafe backup source")
        destination = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=destination_fd,
        )
        os.fchmod(destination, 0o600)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if maximum is not None and total > maximum:
                raise OSError(errno.EFBIG, "backup source too large")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination, view)
                if written <= 0:
                    raise OSError(errno.EIO, "backup write made no progress")
                view = view[written:]
        os.fsync(destination)
        after = os.fstat(source)
        named = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
        destination_details = os.fstat(destination)
        destination_named = os.stat(
            destination_name, dir_fd=destination_fd, follow_symlinks=False
        )
        sha256 = digest.hexdigest()
        if (total != before.st_size or _version(before) != _version(after)
                or _version(after) != _version(named)
                or _version(destination_details) != _version(destination_named)
                or expected is not None and expected != (total, sha256)):
            raise OSError(errno.ESTALE, "backup source changed")
        return {"bytes": total, "sha256": sha256}
    finally:
        if destination is not None:
            os.close(destination)
        os.close(source)


def _copy_tree(source_fd: int, destination_fd: int, prefix: str = "") -> tuple[list, list]:
    before = os.fstat(source_fd)
    if not stat.S_ISDIR(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o700:
        raise OSError(errno.EPERM, "unsafe backup directory")
    files = []
    directories = []
    for name in sorted(os.listdir(source_fd)):
        if not _safe_component(name):
            raise OSError(errno.EINVAL, "unsafe backup name")
        relative = prefix + name
        details = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
            if stat.S_IMODE(details.st_mode) != 0o700:
                raise OSError(errno.EPERM, "unsafe backup directory")
            os.mkdir(name, 0o700, dir_fd=destination_fd)
            source_child = os.open(name, _DIR_FLAGS, dir_fd=source_fd)
            destination_child = os.open(name, _DIR_FLAGS, dir_fd=destination_fd)
            try:
                destination_identity = _identity(os.fstat(destination_child))
                directories.append(relative)
                child_files, child_directories = _copy_tree(
                    source_child, destination_child, relative + "/"
                )
                files.extend(child_files)
                directories.extend(child_directories)
                named = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                if _version(named) != _version(os.fstat(source_child)):
                    raise OSError(errno.ESTALE, "backup directory changed")
                destination_named = os.stat(
                    name, dir_fd=destination_fd, follow_symlinks=False
                )
                if _identity(destination_named) != destination_identity:
                    raise OSError(errno.ESTALE, "backup output changed")
            finally:
                os.close(destination_child)
                os.close(source_child)
        elif stat.S_ISREG(details.st_mode):
            maximum = _MAX_ARTIFACT_BYTES if relative.startswith("artifacts/") else None
            copied = _copy_regular_at(
                source_fd, name, destination_fd, name, maximum=maximum
            )
            files.append({"path": relative, **copied})
        else:
            raise OSError(errno.EPERM, "special backup entry")
    os.fsync(destination_fd)
    after = os.fstat(source_fd)
    if _version(before) != _version(after):
        raise OSError(errno.ESTALE, "backup directory changed")
    return files, directories


def _fsync_directory(path: Path, *, private: bool = False) -> None:
    descriptor = _open_directory(path, private=private)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_no_replace(source: Path, destination: Path) -> None:
    if source.parent != destination.parent:
        raise OSError(errno.EXDEV, "rename must stay in one directory")
    parent_fd = _open_directory(source.parent, private=False)
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            operation = library.renameatx_np
            flags = 4
        else:
            operation = getattr(library, "renameat2", None)
            flags = 1
        if operation is None:
            raise OSError(errno.ENOTSUP, "exclusive rename unavailable")
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                              ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        if operation(
            parent_fd, source.name.encode("utf-8"), parent_fd,
            destination.name.encode("utf-8"), flags,
        ) != 0:
            raise OSError(ctypes.get_errno(), "exclusive rename failed")
    finally:
        os.close(parent_fd)


def _remove_tree_contents(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
            child = os.open(name, _DIR_FLAGS, dir_fd=descriptor)
            try:
                child_identity = _identity(os.fstat(child))
                if _identity(details) != child_identity:
                    raise OSError(errno.ESTALE, "cleanup directory changed")
                _remove_tree_contents(child)
                named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _identity(named) != child_identity:
                    raise OSError(errno.ESTALE, "cleanup directory changed")
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _cleanup_owned(path: Path, identity: tuple[int, int] | None) -> None:
    parent_fd = child_fd = None
    try:
        if identity is None:
            return
        parent_fd = _open_directory(path.parent, private=False)
        child_fd = os.open(path.name, _DIR_FLAGS, dir_fd=parent_fd)
        if _identity(os.fstat(child_fd)) != identity:
            return
        _remove_tree_contents(child_fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(named) != identity:
            return
        os.close(child_fd)
        child_fd = None
        os.rmdir(path.name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    finally:
        if child_fd is not None:
            os.close(child_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _future_path(path: Path) -> Path:
    return path.parent.resolve(strict=True) / path.name


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _open_snapshot(database: Path) -> sqlite3.Connection:
    uri = database.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        connection.close()
        raise ValueError("database integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        connection.close()
        raise ValueError("database foreign key check failed")
    return connection


class StateBackup:
    def __init__(self, store: SessionStore, *, clock=time.time, fault=None):
        if not isinstance(store, SessionStore) or not callable(clock) \
                or fault is not None and not callable(fault):
            raise StoreError("STATE_BACKUP_FAILED")
        self.store = store
        self.clock = clock
        self.fault = fault or (lambda _point: None)
        try:
            self.imports = ImportStore(store)
        except ImportStoreError:
            raise StoreError("STATE_BACKUP_FAILED") from None

    @staticmethod
    def _assert_quiet(connection: sqlite3.Connection) -> None:
        if connection.execute(
            "SELECT 1 FROM runs WHERE finished_at IS NULL LIMIT 1"
        ).fetchone() is not None:
            raise StoreError("STATE_BUSY")
        if connection.execute(
            "SELECT 1 FROM imports WHERE status IN ('uploading','finalizing') LIMIT 1"
        ).fetchone() is not None:
            raise StoreError("STATE_BUSY")
        if connection.execute(
            """SELECT 1 FROM approvals a
               LEFT JOIN tool_calls t
                 ON t.run_id=a.run_id AND t.call_id=a.call_id
               WHERE a.decision='pending'
                  OR (a.decision='allowed' AND (
                        t.call_id IS NULL OR t.publication_state<>'confirmed'
                        OR t.recovery_state<>'confirmed'))
               LIMIT 1"""
        ).fetchone() is not None:
            raise StoreError("STATE_BUSY")

    def _select_root(self, destination: Path) -> Path:
        destination = Path(destination).expanduser()
        if os.path.lexists(destination):
            details = os.lstat(destination)
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise StoreError("STATE_BACKUP_EXISTS")
            parent = destination.resolve(strict=True)
            base = f"sessions.{int(self.clock())}.backup"
            candidate = parent / base
            counter = 1
            while os.path.lexists(candidate):
                candidate = parent / f"sessions.{int(self.clock())}.{counter}.backup"
                counter += 1
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            candidate = _future_path(destination)
        current_state = self.store.state_dir.resolve(strict=True)
        if _inside(candidate, current_state):
            raise StoreError("STATE_DIR_INSIDE_WORKSPACE")
        for (workspace_path,) in self.store.connection().execute(
            "SELECT workspace_path FROM sessions WHERE import_id IS NULL"
        ):
            try:
                workspace = Path(workspace_path).resolve(strict=True)
            except OSError:
                continue
            if _inside(candidate, workspace):
                raise StoreError("STATE_DIR_INSIDE_WORKSPACE")
        return candidate

    @staticmethod
    def _counts(connection: sqlite3.Connection) -> dict:
        return {
            name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("sessions", "runs", "imports")
        }

    @staticmethod
    def _artifact_contract(connection, import_id):
        confirmed = connection.execute(
            """SELECT a.id,a.session_id,a.run_id,a.call_id,a.path,a.bytes,a.sha256,
                      a.receipt_json,a.recovery_state,s.agent_id,t.name,
                      t.publication_intent_json,t.publication_state,t.recovery_state,
                      r.output_path
               FROM artifacts a
               JOIN sessions s ON s.id=a.session_id
               JOIN tool_calls t
                 ON t.run_id=a.run_id AND t.call_id=a.call_id
                AND t.session_id=a.session_id
               JOIN runs r ON r.id=a.run_id AND r.session_id=a.session_id
               WHERE s.import_id=?
               ORDER BY a.run_id,a.call_id""",
            (import_id,),
        ).fetchall()
        confirmed_calls = connection.execute(
            """SELECT t.run_id,t.call_id
               FROM tool_calls t
               JOIN sessions s ON s.id=t.session_id
               WHERE s.import_id=?
                 AND (t.publication_state='confirmed'
                      OR t.recovery_state='confirmed')
               ORDER BY t.run_id,t.call_id""",
            (import_id,),
        ).fetchall()
        if [(row[2], row[3]) for row in confirmed] != confirmed_calls:
            raise ValueError("confirmed artifact relation changed")
        validated = []
        for row in confirmed:
            (artifact_id, session_id, run_id, call_id, path, size, digest,
             raw_receipt, artifact_recovery, agent_id, tool_name, raw_intent,
             publication_state, tool_recovery, output_path) = row
            intent = RunJournal._publication_payload(json.loads(raw_intent))
            receipt = RunJournal._publication_payload(json.loads(raw_receipt))
            if (
                artifact_recovery != "confirmed"
                or tool_name != "write_file"
                or publication_state != "confirmed"
                or tool_recovery != "confirmed"
                or output_path != path
                or intent != (path, size, digest)
                or receipt != (path, size, digest)
            ):
                raise ValueError("confirmed artifact relation changed")
            validated.append(
                (artifact_id, session_id, run_id, call_id, path, size, digest, agent_id)
            )
        unknown = connection.execute(
            """SELECT s.id,t.run_id,t.call_id,t.publication_intent_json
               FROM tool_calls t
               JOIN sessions s ON s.id=t.session_id
               WHERE s.import_id=? AND t.recovery_state='WRITE_OUTCOME_UNKNOWN'
               ORDER BY s.id,t.run_id,t.call_id""",
            (import_id,),
        ).fetchall()
        return validated, unknown

    def _copy_import(self, snapshot, service, import_id, manifest_sha256,
                     sidecar_fd) -> dict:
        workspace = self.imports.open(import_id)
        confirmed_rows, unknown_rows = self._artifact_contract(snapshot, import_id)
        confirmed = {}
        for (artifact_id, session_id, run_id, _call_id, path, size, digest,
             agent_id) in confirmed_rows:
            if path in confirmed:
                raise ValueError("duplicate artifact path")
            opened = service.open_artifact(session_id, run_id, artifact_id, agent_id)
            if (opened.length, hashlib.sha256(opened.content).hexdigest()) != (size, digest):
                raise ValueError("artifact receipt changed")
            confirmed[path] = (size, digest)

        unknown = []
        unknown_paths = {}
        for session_id, run_id, call_id, raw_intent in unknown_rows:
            path, size, digest = RunJournal._publication_payload(json.loads(raw_intent))
            classification = self.store.inspect_unknown_publication(
                session_id, path, write_root=workspace.artifacts,
                write_identity=(workspace.artifact_device, workspace.artifact_inode),
            )
            if classification is None:
                raise ValueError("unknown publication disappeared")
            previous = unknown_paths.get(path)
            contract = (size, digest, classification)
            if previous is not None and previous != contract:
                raise ValueError("conflicting unknown publication")
            unknown_paths[path] = contract
            unknown.append({
                "session_id": session_id, "run_id": run_id, "call_id": call_id,
                "path": path, "bytes": size, "sha256": digest,
                "classification": classification,
            })

        os.mkdir(import_id, 0o700, dir_fd=sidecar_fd)
        destination_fd = os.open(import_id, _DIR_FLAGS, dir_fd=sidecar_fd)
        source_fd = self.imports._batch_fd(import_id, formal=True)
        try:
            files, directories = _copy_tree(source_fd, destination_fd)
        finally:
            os.close(source_fd)
            os.close(destination_fd)
        files.sort(key=lambda item: item["path"])
        directories.sort()
        by_path = {item["path"]: item for item in files}
        if len(by_path) != len(files):
            raise ValueError("duplicate backup path")
        internal_manifest = by_path.get("manifest.json")
        if internal_manifest is None or internal_manifest["sha256"] != manifest_sha256:
            raise ValueError("import manifest changed")

        expected_immutable = {
            "manifest.json": manifest_sha256,
            **dict(workspace.manifest_hashes),
        }
        immutable_files = {
            path: item["sha256"] for path, item in by_path.items()
            if not path.startswith("artifacts/")
        }
        if immutable_files != expected_immutable:
            raise ValueError("import content changed")
        expected_immutable_directories = set()
        for path in expected_immutable:
            parts = PurePosixPath(path).parts[:-1]
            for index in range(1, len(parts) + 1):
                expected_immutable_directories.add("/".join(parts[:index]))
        immutable_directories = {
            path for path in directories
            if path != "artifacts" and not path.startswith("artifacts/")
        }
        if immutable_directories != expected_immutable_directories:
            raise ValueError("import directory set changed")

        artifact_files = {
            path.removeprefix("artifacts/"): item
            for path, item in by_path.items() if path.startswith("artifacts/")
        }
        allowed_present = set(confirmed)
        allowed_present.update(
            path for path, (_size, _digest, classification) in unknown_paths.items()
            if classification != "missing"
        )
        if set(artifact_files) != allowed_present:
            raise ValueError("unregistered artifact entry")
        allowed_directories = {"artifacts"}
        for path in allowed_present:
            parts = PurePosixPath(path).parts[:-1]
            for index in range(1, len(parts) + 1):
                allowed_directories.add("artifacts/" + "/".join(parts[:index]))
        if {path for path in directories if path == "artifacts"
                or path.startswith("artifacts/")} != allowed_directories:
            raise ValueError("unregistered artifact directory")
        for path, expected in confirmed.items():
            item = artifact_files[path]
            if (item["bytes"], item["sha256"]) != expected:
                raise ValueError("confirmed artifact changed")
        for path, (size, digest, classification) in unknown_paths.items():
            item = artifact_files.get(path)
            copied_classification = (
                "missing" if item is None else
                "present_same_hash" if (item["bytes"], item["sha256"]) == (size, digest)
                else "present_different_hash"
            )
            if copied_classification != classification:
                raise ValueError("unknown publication changed")
        unknown.sort(
            key=lambda item: (
                item["session_id"], item["run_id"], item["call_id"], item["path"]
            )
        )
        return {
            "id": import_id,
            "manifest_sha256": manifest_sha256,
            "files": files,
            "directories": directories,
            "unknown": unknown,
        }

    def backup_bundle(self, destination: Path) -> BackupBundle:
        staging = None
        staging_identity = None
        published = False
        sidecar_fd = snapshot = None
        try:
            try:
                maintenance = self.store.maintenance_gate.maintenance()
                with maintenance:
                    connection = self.store.connection()
                    self._assert_quiet(connection)
                    final_root = self._select_root(destination)
                    if os.path.lexists(final_root):
                        raise StoreError("STATE_BACKUP_EXISTS")
                    staging = Path(tempfile.mkdtemp(
                        prefix=f".{final_root.name}.staging-", dir=final_root.parent
                    ))
                    os.chmod(staging, 0o700)
                    staging_identity = _identity(os.lstat(staging))
                    database = staging / "sessions.sqlite3"
                    sidecar = staging / "sessions.sqlite3.imports"
                    self.store.backup(database)
                    sidecar.mkdir(mode=0o700)
                    sidecar_fd = _open_directory(sidecar)
                    snapshot = _open_snapshot(database)
                    try:
                        counts = self._counts(snapshot)
                        rows = snapshot.execute(
                            "SELECT id,manifest_sha256 FROM imports WHERE status='ready' ORDER BY id"
                        ).fetchall()
                        from .sessions import SessionService
                        service = SessionService(self.store, imports=self.imports)
                        imported = [
                            self._copy_import(snapshot, service, import_id, digest, sidecar_fd)
                            for import_id, digest in rows
                        ]
                    finally:
                        snapshot.close()
                        snapshot = None
                    staging_fd = _open_directory(staging)
                    try:
                        database_raw = _read_regular_at(
                            staging_fd, "sessions.sqlite3"
                        )
                    finally:
                        os.close(staging_fd)
                    database_record = {
                        "name": "sessions.sqlite3", "bytes": len(database_raw),
                        "sha256": hashlib.sha256(database_raw).hexdigest(),
                    }
                    manifest_value = {
                        "schema_version": 1,
                        "database": database_record,
                        "imports": imported,
                    }
                    _write_regular_at(sidecar_fd, "manifest.json", _canonical(manifest_value))
                    os.fsync(sidecar_fd)
                    os.close(sidecar_fd)
                    sidecar_fd = None
                    _fsync_directory(staging, private=True)
                    self.fault("before_backup_publish")
                    _rename_no_replace(staging, final_root)
                    published = True
                    self.fault("after_backup_publish")
                    try:
                        _fsync_directory(final_root.parent, private=False)
                    except OSError:
                        raise StoreError("STATE_BACKUP_OUTCOME_UNKNOWN") from None
            except StateBusy:
                raise StoreError("STATE_BUSY") from None
            return BackupBundle(
                final_root,
                final_root / "sessions.sqlite3",
                final_root / "sessions.sqlite3.imports",
                final_root / "sessions.sqlite3.imports" / "manifest.json",
                counts,
            )
        except StoreError:
            raise
        except (ImportStoreError, OSError, sqlite3.Error, TypeError, ValueError,
                KeyError, json.JSONDecodeError):
            if published:
                raise StoreError("STATE_BACKUP_OUTCOME_UNKNOWN") from None
            raise StoreError("STATE_BACKUP_FAILED") from None
        finally:
            if snapshot is not None:
                snapshot.close()
            if sidecar_fd is not None:
                os.close(sidecar_fd)
            if not published and staging is not None:
                _cleanup_owned(staging, staging_identity)

    @staticmethod
    def _validate_manifest(value, database_name):
        if type(value) is not dict or set(value) != {"schema_version", "database", "imports"} \
                or value["schema_version"] != 1:
            raise ValueError("invalid bundle manifest")
        database = value["database"]
        if (type(database) is not dict
                or set(database) != {"name", "bytes", "sha256"}
                or database["name"] != database_name
                or type(database["bytes"]) is not int or database["bytes"] < 0
                or not isinstance(database["sha256"], str)
                or not _HASH.fullmatch(database["sha256"])):
            raise ValueError("invalid database manifest")
        imports = value["imports"]
        if not isinstance(imports, list):
            raise ValueError("invalid import manifest")
        seen = set()
        for item in imports:
            if (type(item) is not dict or set(item) != {
                    "id", "manifest_sha256", "files", "directories", "unknown"}
                    or not ImportStore._is_managed_id(item["id"])
                    or item["id"] in seen
                    or not isinstance(item["manifest_sha256"], str)
                    or not _HASH.fullmatch(item["manifest_sha256"])
                    or not isinstance(item["files"], list)
                    or not isinstance(item["directories"], list)
                    or not isinstance(item["unknown"], list)):
                raise ValueError("invalid import manifest")
            seen.add(item["id"])
            paths = []
            for record in item["files"]:
                if (type(record) is not dict or set(record) != {"path", "bytes", "sha256"}
                        or not _safe_relative(record["path"])
                        or type(record["bytes"]) is not int or record["bytes"] < 0
                        or not isinstance(record["sha256"], str)
                        or not _HASH.fullmatch(record["sha256"])):
                    raise ValueError("invalid file manifest")
                paths.append(record["path"])
            if paths != sorted(paths) or len(paths) != len(set(paths)):
                raise ValueError("invalid file order")
            directories = item["directories"]
            if (directories != sorted(directories)
                    or len(directories) != len(set(directories))
                    or any(not _safe_relative(path) for path in directories)):
                raise ValueError("invalid directory manifest")
            for unknown in item["unknown"]:
                if (type(unknown) is not dict or set(unknown) != {
                        "session_id", "run_id", "call_id", "path", "bytes",
                        "sha256", "classification"}
                        or not isinstance(unknown["session_id"], str) or not unknown["session_id"]
                        or not isinstance(unknown["run_id"], str) or not unknown["run_id"]
                        or not isinstance(unknown["call_id"], str) or not unknown["call_id"]
                        or not _safe_relative(unknown["path"])
                        or type(unknown["bytes"]) is not int or unknown["bytes"] < 0
                        or not isinstance(unknown["sha256"], str)
                        or not _HASH.fullmatch(unknown["sha256"])
                        or unknown["classification"] not in {
                            "missing", "present_same_hash", "present_different_hash"}):
                    raise ValueError("invalid unknown publication")
        if [item["id"] for item in imports] != sorted(seen):
            raise ValueError("invalid import order")
        return value

    @staticmethod
    def _bundle_input(database: Path, sidecar: Path):
        database = Path(database).expanduser()
        sidecar = Path(sidecar).expanduser()
        if (database.parent != sidecar.parent
                or not _safe_component(database.name)
                or sidecar.name != database.name + ".imports"):
            raise ValueError("bundle paths do not match")
        root = database.parent.resolve(strict=True)
        root_fd = sidecar_fd = None
        try:
            root_fd = _open_directory(root)
            sidecar_fd = _open_directory_at(root_fd, sidecar.name)
            if set(os.listdir(root_fd)) != {database.name, sidecar.name}:
                raise ValueError("bundle root contains extra entries")
            raw_manifest = _read_regular_at(
                sidecar_fd, "manifest.json", maximum=_MAX_MANIFEST_BYTES
            )
            manifest = StateBackup._validate_manifest(
                _object(raw_manifest), database.name
            )
            database_raw = _read_regular_at(root_fd, database.name)
            if (len(database_raw), hashlib.sha256(database_raw).hexdigest()) != (
                    manifest["database"]["bytes"], manifest["database"]["sha256"]):
                raise ValueError("database digest changed")
            expected_sidecar = {"manifest.json", *(item["id"] for item in manifest["imports"])}
            if set(os.listdir(sidecar_fd)) != expected_sidecar:
                raise ValueError("sidecar set changed")
            return (
                root, database.name, sidecar.name, manifest, root_fd, sidecar_fd,
                _version(os.fstat(root_fd)), _version(os.fstat(sidecar_fd)),
            )
        except BaseException:
            if sidecar_fd is not None:
                os.close(sidecar_fd)
            if root_fd is not None:
                os.close(root_fd)
            raise

    @staticmethod
    def _restore_target(destination: Path, bundle_root: Path) -> Path:
        destination = Path(destination).expanduser()
        if os.path.lexists(destination):
            raise StoreError("STATE_RESTORE_EXISTS")
        parent = destination.parent.resolve(strict=True)
        final = parent / destination.name
        if _inside(final, bundle_root) or _inside(bundle_root, final):
            raise StoreError("STATE_RESTORE_FAILED")
        return final

    @staticmethod
    def _assert_destination_outside_workspaces(
        final: Path, snapshot: sqlite3.Connection
    ) -> None:
        for (workspace_path,) in snapshot.execute(
            "SELECT workspace_path FROM sessions WHERE import_id IS NULL"
        ):
            try:
                workspace = Path(workspace_path).resolve(strict=True)
            except OSError:
                continue
            if _inside(final, workspace):
                raise StoreError("STATE_DIR_INSIDE_WORKSPACE")

    @staticmethod
    def _validate_snapshot_contract(snapshot: sqlite3.Connection, manifest) -> None:
        ready = snapshot.execute(
            "SELECT id,manifest_sha256 FROM imports WHERE status='ready' ORDER BY id"
        ).fetchall()
        expected = [(item["id"], item["manifest_sha256"]) for item in manifest["imports"]]
        if ready != expected:
            raise ValueError("database and sidecar imports differ")
        for item in manifest["imports"]:
            _confirmed, unknown_rows = StateBackup._artifact_contract(
                snapshot, item["id"]
            )
            database_unknown = []
            for session_id, run_id, call_id, raw_intent in unknown_rows:
                path, size, digest = RunJournal._publication_payload(
                    json.loads(raw_intent)
                )
                database_unknown.append({
                    "session_id": session_id,
                    "run_id": run_id,
                    "call_id": call_id,
                    "path": path,
                    "bytes": size,
                    "sha256": digest,
                })
            manifest_unknown = [
                {key: unknown[key] for key in (
                    "session_id", "run_id", "call_id", "path", "bytes", "sha256"
                )}
                for unknown in item["unknown"]
            ]
            if manifest_unknown != database_unknown:
                raise ValueError("unknown publication manifest differs")

    @staticmethod
    def _copy_validated_import(source_sidecar_fd, destination_imports_fd, item):
        import_id = item["id"]
        source_fd = destination_fd = None
        try:
            source_fd = _open_directory_at(source_sidecar_fd, import_id)
            os.mkdir(import_id, 0o700, dir_fd=destination_imports_fd)
            destination_fd = _open_directory_at(destination_imports_fd, import_id)
            files, directories = _copy_tree(source_fd, destination_fd)
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            if source_fd is not None:
                os.close(source_fd)
        files.sort(key=lambda record: record["path"])
        directories.sort()
        if files != item["files"] or directories != item["directories"]:
            raise ValueError("import sidecar changed")

    @staticmethod
    def _rewrite_import_identity(staging: Path, final: Path, item) -> tuple:
        import_root = staging / "imports" / item["id"]
        workspace = import_root / "workspace"
        artifacts = import_root / "artifacts"
        workspace_identity = _identity(os.lstat(workspace))
        artifact_identity = _identity(os.lstat(artifacts))
        manifest_path = import_root / "manifest.json"
        parent_fd = _open_directory(import_root)
        try:
            raw = _read_regular_at(parent_fd, "manifest.json", maximum=_MAX_MANIFEST_BYTES)
            manifest = _object(raw)
            if not isinstance(manifest, dict) or "identities" not in manifest:
                raise ValueError("invalid import manifest")
            manifest["identities"] = {
                "workspace": list(workspace_identity),
                "artifacts": list(artifact_identity),
            }
            updated = _canonical(manifest)
            temporary = f".manifest-{uuid.uuid4().hex}.tmp"
            _write_regular_at(parent_fd, temporary, updated)
            os.replace(temporary, "manifest.json", src_dir_fd=parent_fd,
                       dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return (
            workspace_identity, artifact_identity,
            hashlib.sha256(updated).hexdigest(),
            str(final / "imports" / item["id"] / "workspace"),
        )

    @staticmethod
    def restore_bundle(database: Path, sidecar: Path, destination: Path, *, fault=None) -> dict:
        fault = fault or (lambda _point: None)
        if not callable(fault):
            raise StoreError("STATE_RESTORE_FAILED")
        staging = None
        staging_identity = None
        published = False
        snapshot = None
        source_root_fd = source_sidecar_fd = staging_fd = None
        try:
            (bundle_root, database_name, sidecar_name, manifest, source_root_fd,
             source_sidecar_fd, source_root_version, source_sidecar_version) = (
                StateBackup._bundle_input(database, sidecar)
            )
            final = StateBackup._restore_target(destination, bundle_root)
            staging = Path(tempfile.mkdtemp(
                prefix=f".{final.name}.restore-", dir=final.parent
            ))
            os.chmod(staging, 0o700)
            staging_identity = _identity(os.lstat(staging))
            staging_fd = _open_directory(staging)
            expected_database = (
                manifest["database"]["bytes"], manifest["database"]["sha256"]
            )
            _copy_regular_at(
                source_root_fd, database_name, staging_fd, "sessions.sqlite3",
                expected=expected_database,
            )
            database_identity = _pinned_regular_at(staging_fd, "sessions.sqlite3")
            os.mkdir("imports", 0o700, dir_fd=staging_fd)
            destination_imports_fd = None
            try:
                destination_imports_fd = _open_directory_at(staging_fd, "imports")
                os.mkdir(".staging", 0o700, dir_fd=destination_imports_fd)
                for item in manifest["imports"]:
                    StateBackup._copy_validated_import(
                        source_sidecar_fd, destination_imports_fd, item
                    )
                os.fsync(destination_imports_fd)
            finally:
                if destination_imports_fd is not None:
                    os.close(destination_imports_fd)
            expected_root_entries = {database_name, sidecar_name}
            expected_sidecar_entries = {
                "manifest.json", *(item["id"] for item in manifest["imports"])
            }
            if (_version(os.fstat(source_root_fd)) != source_root_version
                    or _version(os.fstat(source_sidecar_fd)) != source_sidecar_version
                    or set(os.listdir(source_root_fd)) != expected_root_entries
                    or set(os.listdir(source_sidecar_fd)) != expected_sidecar_entries):
                raise OSError(errno.ESTALE, "backup bundle changed")
            os.close(source_sidecar_fd)
            source_sidecar_fd = None
            os.close(source_root_fd)
            source_root_fd = None
            os.fsync(staging_fd)

            snapshot = _open_snapshot(staging / "sessions.sqlite3")
            _pinned_regular_at(staging_fd, "sessions.sqlite3", database_identity)
            StateBackup._validate_snapshot_contract(snapshot, manifest)
            StateBackup._assert_destination_outside_workspaces(final, snapshot)
            snapshot.close()
            snapshot = None

            rebinding = {}
            for item in manifest["imports"]:
                rebinding[item["id"]] = StateBackup._rewrite_import_identity(
                    staging, final, item
                )
            connection = sqlite3.connect(staging / "sessions.sqlite3")
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                for import_id, (read_identity, write_identity, digest, workspace_path) \
                        in rebinding.items():
                    changed = connection.execute(
                        """UPDATE imports SET manifest_sha256=?,workspace_device=?,workspace_inode=?,
                                  artifact_device=?,artifact_inode=?
                           WHERE id=? AND status='ready'""",
                        (digest, *read_identity, *write_identity, import_id),
                    )
                    if changed.rowcount != 1:
                        raise ValueError("import row changed")
                    changed = connection.execute(
                        """UPDATE sessions SET workspace_path=?,workspace_device=?,workspace_inode=?
                           WHERE import_id=?""",
                        (workspace_path, *read_identity, import_id),
                    )
                    if changed.rowcount != 1:
                        raise ValueError("import session changed")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("restored foreign key check failed")
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
            _pinned_regular_at(staging_fd, "sessions.sqlite3", database_identity)
            database_fd = os.open(
                "sessions.sqlite3", _FILE_FLAGS, dir_fd=staging_fd
            )
            try:
                os.fsync(database_fd)
            finally:
                os.close(database_fd)

            restored_store = SessionStore.open(staging)
            try:
                from .sessions import SessionService
                restored_service = SessionService(restored_store)
                for item in manifest["imports"]:
                    restored_service.imports.open(item["id"])
                session_rows = restored_store.connection().execute(
                    "SELECT id FROM sessions WHERE import_id IS NOT NULL ORDER BY id"
                ).fetchall()
                for (session_id,) in session_rows:
                    restored_service.resolver.resolve(restored_service.load(session_id))
                artifact_rows = restored_store.connection().execute(
                    """SELECT a.session_id,a.run_id,a.id,s.agent_id
                       FROM artifacts a JOIN sessions s ON s.id=a.session_id
                       WHERE s.import_id IS NOT NULL AND a.recovery_state='confirmed'
                       ORDER BY a.id"""
                ).fetchall()
                for row in artifact_rows:
                    restored_service.open_artifact(*row)
                for item in manifest["imports"]:
                    for unknown in item["unknown"]:
                        session = restored_service.load(unknown["session_id"])
                        resolved = restored_service.resolver.resolve(session)
                        actual = restored_store.inspect_unknown_publication(
                            session.id, unknown["path"], write_root=resolved.write_root,
                            write_identity=resolved.write_identity,
                        )
                        if actual != unknown["classification"]:
                            raise ValueError("unknown publication restore changed")
            finally:
                restored_store.close()

            os.fsync(staging_fd)
            parent_fd = _open_directory(final.parent, private=False)
            try:
                named_staging = os.stat(
                    staging.name, dir_fd=parent_fd, follow_symlinks=False
                )
                if _identity(named_staging) != staging_identity:
                    raise OSError(errno.ESTALE, "restore staging changed")
            finally:
                os.close(parent_fd)
            fault("before_publish_rename")
            _rename_no_replace(staging, final)
            published = True
            fault("after_publish_rename")
            try:
                _fsync_directory(final.parent, private=False)
            except OSError:
                raise StoreError("STATE_RESTORE_OUTCOME_UNKNOWN") from None
            return {
                "state_dir": str(final),
                "database_path": str(final / "sessions.sqlite3"),
                "sidecar_path": str(final / "imports"),
                "imports": len(manifest["imports"]),
            }
        except StoreError:
            raise
        except (ImportStoreError, OSError, sqlite3.Error, TypeError, ValueError,
                KeyError, json.JSONDecodeError):
            if published:
                raise StoreError("STATE_RESTORE_OUTCOME_UNKNOWN") from None
            raise StoreError("STATE_RESTORE_FAILED") from None
        finally:
            if snapshot is not None:
                snapshot.close()
            if source_sidecar_fd is not None:
                os.close(source_sidecar_fd)
            if source_root_fd is not None:
                os.close(source_root_fd)
            if staging_fd is not None:
                os.close(staging_fd)
            if not published and staging is not None:
                _cleanup_owned(staging, staging_identity)
