"""Validated upload storage for local document imports."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import time
import unicodedata
import uuid


_SUPPORTED_EXTENSIONS = frozenset({".md", ".txt", ".pdf", ".docx"})
_REQUEST_KEYS = frozenset({"kind", "name", "agent_id", "files", "ignored"})
_FILE_KEYS = frozenset({"logical_path", "bytes"})
_DEFAULT_LIMITS = {
    "max_items": 500,
    "max_files": 50,
    "max_file_bytes": 20 * 1024 * 1024,
    "max_total_bytes": 100 * 1024 * 1024,
    "read_chunk_bytes": 64 * 1024,
    "max_name_bytes": 256,
    "max_agent_id_bytes": 256,
}


class ImportStoreError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ImportFileSlot:
    source_id: str
    slot_id: str
    logical_path: str
    extension: str
    declared_bytes: int
    upload_state: str


@dataclass(frozen=True)
class ImportBatch:
    id: str
    status: str
    files: tuple[ImportFileSlot, ...]


@dataclass(frozen=True)
class FileReceipt:
    import_id: str
    source_id: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class ImportSnapshot:
    id: str
    status: str
    files: tuple[ImportFileSlot, ...]
    job_id: str | None
    session_id: str | None
    error_code: str | None
    total_files: int
    stored_files: int


@dataclass(frozen=True)
class ImportedWorkspace:
    import_id: str
    root: Path
    originals: Path
    workspace: Path
    artifacts: Path
    manifest: Path
    locations: tuple[tuple[str, str], ...]
    workspace_device: int
    workspace_inode: int
    artifact_device: int
    artifact_inode: int


@dataclass(frozen=True)
class ImportJob:
    import_id: str
    job_id: str


class ImportStore:
    def __init__(self, store, *, limits=None, clock=time.time):
        self.store = store
        self.clock = clock
        self.limits = dict(_DEFAULT_LIMITS)
        if limits is not None:
            if not isinstance(limits, dict) or not set(limits).issubset(self.limits):
                raise ImportStoreError("IMPORT_REQUEST_INVALID")
            for key, value in limits.items():
                if type(value) is not int or value <= 0:
                    raise ImportStoreError("IMPORT_REQUEST_INVALID")
                self.limits[key] = value
        self.root = Path(store.state_dir) / "imports"
        self.staging = self.root / ".staging"
        try:
            self._ensure_private_directory(self.root)
            self._ensure_private_directory(self.staging)
            root_details = os.lstat(self.root)
            staging_details = os.lstat(self.staging)
            self._managed_directory_identities = {
                ("imports",): (root_details.st_dev, root_details.st_ino),
                ("imports", ".staging"): (
                    staging_details.st_dev, staging_details.st_ino,
                ),
            }
        except (OSError, ValueError):
            raise ImportStoreError("IMPORT_STORAGE_FAILED") from None

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            details = os.lstat(path)
            if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
                raise ValueError("unsafe directory")
        details = os.lstat(path)
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise ValueError("unsafe directory")
        os.chmod(path, 0o700, follow_symlinks=False)

    @staticmethod
    def _is_managed_id(value) -> bool:
        return (
            type(value) is str
            and len(value) == 32
            and all(character in "0123456789abcdef" for character in value)
        )

    def _open_managed_directory(self, *components: str) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.store.state_dir, flags)
        try:
            traversed = []
            for component in components:
                traversed.append(component)
                expected = self._managed_directory_identities.get(tuple(traversed))
                if len(traversed) >= 3 and expected is None:
                    raise OSError("managed directory identity unavailable")
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                details = os.fstat(next_descriptor)
                if expected is not None and (details.st_dev, details.st_ino) != expected:
                    os.close(next_descriptor)
                    raise OSError("managed directory replaced")
                os.close(descriptor)
                descriptor = next_descriptor
            details = os.fstat(descriptor)
            if not stat.S_ISDIR(details.st_mode):
                raise OSError("unsafe managed directory")
            return descriptor
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    def _create_staging_batch(self, import_id: str) -> None:
        if not self._is_managed_id(import_id):
            raise OSError("unsafe import id")
        staging_fd = self._open_managed_directory("imports", ".staging")
        batch_fd = None
        originals_fd = None
        created = False
        try:
            os.mkdir(import_id, 0o700, dir_fd=staging_fd)
            created = True
            flags = (
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            batch_fd = os.open(import_id, flags, dir_fd=staging_fd)
            os.fchmod(batch_fd, 0o700)
            os.mkdir("originals", 0o700, dir_fd=batch_fd)
            originals_fd = os.open("originals", flags, dir_fd=batch_fd)
            os.fchmod(originals_fd, 0o700)
            os.fsync(originals_fd)
            os.fsync(batch_fd)
            os.fsync(staging_fd)
            batch_details = os.fstat(batch_fd)
            originals_details = os.fstat(originals_fd)
            prefix = ("imports", ".staging", import_id)
            self._managed_directory_identities[prefix] = (
                batch_details.st_dev, batch_details.st_ino,
            )
            self._managed_directory_identities[prefix + ("originals",)] = (
                originals_details.st_dev, originals_details.st_ino,
            )
        except BaseException:
            if originals_fd is not None:
                try:
                    os.close(originals_fd)
                except OSError:
                    pass
                originals_fd = None
            if batch_fd is not None:
                try:
                    os.rmdir("originals", dir_fd=batch_fd)
                except OSError:
                    pass
            if created:
                try:
                    os.rmdir(import_id, dir_fd=staging_fd)
                except OSError:
                    pass
            raise
        finally:
            for descriptor in (originals_fd, batch_fd, staging_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    @staticmethod
    def _validate_text(value, maximum_bytes: int) -> str:
        if type(value) is not str or not value or any(
            unicodedata.category(character).startswith("C") for character in value
        ):
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        try:
            encoded = value.encode("utf-8", "strict")
        except UnicodeError:
            raise ImportStoreError("IMPORT_REQUEST_INVALID") from None
        if len(encoded) > maximum_bytes:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        return value

    @staticmethod
    def _validate_path(value) -> str:
        if type(value) is not str or not value or "\\" in value:
            raise ImportStoreError("IMPORT_PATH_INVALID")
        if value.startswith("/") or value.endswith("/"):
            raise ImportStoreError("IMPORT_PATH_INVALID")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ImportStoreError("IMPORT_PATH_INVALID")
        try:
            value.encode("utf-8", "strict")
        except UnicodeError:
            raise ImportStoreError("IMPORT_PATH_INVALID") from None
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ImportStoreError("IMPORT_PATH_INVALID")
        return value

    @staticmethod
    def _validated_bytes(value) -> int:
        if type(value) is not int or value < 0:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        return value

    def _validate_request(self, request):
        if type(request) is not dict or set(request) != _REQUEST_KEYS:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        kind = request.get("kind")
        if type(kind) is not str or kind not in {"file", "folder"}:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        self._validate_text(request.get("name"), self.limits["max_name_bytes"])
        self._validate_text(request.get("agent_id"), self.limits["max_agent_id_bytes"])
        files = request.get("files")
        ignored = request.get("ignored")
        if type(files) is not list or type(ignored) is not list:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        if len(files) + len(ignored) > self.limits["max_items"]:
            raise ImportStoreError("IMPORT_LIMIT_EXCEEDED")
        if len(files) > self.limits["max_files"]:
            raise ImportStoreError("IMPORT_LIMIT_EXCEEDED")

        validated_files = []
        seen = set()
        total_bytes = 0
        for item, supported in tuple((item, True) for item in files) + tuple(
            (item, False) for item in ignored
        ):
            if type(item) is not dict or set(item) != _FILE_KEYS:
                raise ImportStoreError("IMPORT_REQUEST_INVALID")
            logical_path = self._validate_path(item.get("logical_path"))
            declared_bytes = self._validated_bytes(item.get("bytes"))
            if logical_path in seen:
                raise ImportStoreError("IMPORT_PATH_INVALID")
            seen.add(logical_path)
            extension = PurePosixPath(logical_path).suffix.casefold()
            if supported and extension not in _SUPPORTED_EXTENSIONS:
                raise ImportStoreError("IMPORT_FORMAT_UNSUPPORTED")
            if not supported and extension in _SUPPORTED_EXTENSIONS:
                raise ImportStoreError("IMPORT_REQUEST_INVALID")
            if supported:
                if declared_bytes > self.limits["max_file_bytes"]:
                    raise ImportStoreError("IMPORT_LIMIT_EXCEEDED")
                total_bytes += declared_bytes
                if total_bytes > self.limits["max_total_bytes"]:
                    raise ImportStoreError("IMPORT_LIMIT_EXCEEDED")
                validated_files.append((logical_path, extension, declared_bytes))

        if not validated_files:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        return tuple(validated_files)

    @staticmethod
    def _canonical_request(request) -> str:
        try:
            return json.dumps(
                request, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            )
        except (TypeError, ValueError, UnicodeError):
            raise ImportStoreError("IMPORT_REQUEST_INVALID") from None

    def begin(self, request) -> ImportBatch:
        activity_id = "begin:" + uuid.uuid4().hex
        with self.store.maintenance_gate.start("import-upload", activity_id):
            validated_files = self._validate_request(request)
            request_json = self._canonical_request(request)
            fingerprint = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
            import_id = uuid.uuid4().hex
            slots = tuple(
                ImportFileSlot(
                    source_id=uuid.uuid4().hex,
                    slot_id=uuid.uuid4().hex,
                    logical_path=logical_path,
                    extension=extension,
                    declared_bytes=declared_bytes,
                    upload_state="pending",
                )
                for logical_path, extension, declared_bytes in validated_files
            )
            batch_created = False
            try:
                self._create_staging_batch(import_id)
                batch_created = True
                now = float(self.clock())
                with self.store.transaction() as connection:
                    connection.execute(
                        """INSERT INTO imports (
                               id, request_json, request_fingerprint, agent_id, status,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, ?, 'uploading', ?, ?)""",
                        (import_id, request_json, fingerprint, request["agent_id"], now, now),
                    )
                    connection.executemany(
                        """INSERT INTO import_files (
                               import_id, source_id, slot_id, logical_path, extension,
                               declared_bytes, upload_state
                           ) VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                        tuple(
                            (
                                import_id, slot.source_id, slot.slot_id,
                                slot.logical_path, slot.extension, slot.declared_bytes,
                            )
                            for slot in slots
                        ),
                    )
                    self._verify_current_import_directories(import_id)
            except ImportStoreError:
                if batch_created:
                    try:
                        self._remove_staging_batch(import_id)
                    except OSError:
                        pass
                raise
            except (OSError, sqlite3.Error, TypeError, ValueError):
                if batch_created:
                    try:
                        self._remove_staging_batch(import_id)
                    except OSError:
                        pass
                raise ImportStoreError("IMPORT_STORAGE_FAILED") from None
            return ImportBatch(import_id, "uploading", slots)

    def _lookup_upload(self, import_id, slot_id):
        if type(import_id) is not str or type(slot_id) is not str:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        with self.store.transaction() as connection:
            imported = connection.execute(
                "SELECT status FROM imports WHERE id = ?", (import_id,)
            ).fetchone()
            if imported is None:
                raise ImportStoreError("IMPORT_NOT_FOUND")
            if imported[0] != "uploading":
                raise ImportStoreError("IMPORT_STATE_CONFLICT")
            row = connection.execute(
                """SELECT source_id, declared_bytes, upload_state
                   FROM import_files WHERE import_id = ? AND slot_id = ?""",
                (import_id, slot_id),
            ).fetchone()
            if row is None:
                raise ImportStoreError("IMPORT_SLOT_NOT_FOUND")
            if row[2] == "stored":
                raise ImportStoreError("IMPORT_SLOT_ALREADY_STORED")
            return row[0], row[1]

    def _open_originals_directory(self, import_id):
        if not self._is_managed_id(import_id):
            raise ImportStoreError("IMPORT_STORAGE_FAILED")
        return self._open_managed_directory("imports", ".staging", import_id, "originals")

    def _verify_current_import_directories(self, import_id, originals_fd=None) -> None:
        current_fd = None
        try:
            current_fd = self._open_originals_directory(import_id)
            if originals_fd is not None:
                current = os.fstat(current_fd)
                held = os.fstat(originals_fd)
                if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                    raise OSError("current originals directory changed")
        finally:
            if current_fd is not None:
                os.close(current_fd)

    @staticmethod
    def _upload_file_identity(file_fd, declared_bytes):
        details = os.fstat(file_fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_size != declared_bytes
        ):
            raise OSError("unsafe upload")
        return details.st_dev, details.st_ino

    def add_file(self, import_id, slot_id, stream, content_length) -> FileReceipt:
        identity = f"{import_id}:{slot_id}"
        try:
            lease = self.store.maintenance_gate.start("import-upload", identity)
        except (TypeError, ValueError):
            raise ImportStoreError("IMPORT_REQUEST_INVALID") from None
        with lease:
            try:
                source_id, declared_bytes = self._lookup_upload(import_id, slot_id)
            except ImportStoreError:
                raise
            except (OSError, sqlite3.Error):
                raise ImportStoreError("IMPORT_STORAGE_FAILED") from None
            if type(content_length) is not int or content_length < 0:
                raise ImportStoreError("IMPORT_REQUEST_INVALID")
            if content_length != declared_bytes:
                raise ImportStoreError("IMPORT_LENGTH_MISMATCH")
            if not self._is_managed_id(source_id):
                raise ImportStoreError("IMPORT_STORAGE_FAILED")

            directory_fd = None
            file_fd = None
            final_fd = None
            temporary_name = f".{source_id}.{uuid.uuid4().hex}.tmp"
            final_name = f"{source_id}.bin"
            final_created = False
            committed = False
            digest = hashlib.sha256()
            try:
                directory_fd = self._open_originals_directory(import_id)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                file_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
                remaining = declared_bytes
                while remaining:
                    chunk = stream.read(min(self.limits["read_chunk_bytes"], remaining))
                    if not isinstance(chunk, bytes) or not chunk:
                        raise ImportStoreError("IMPORT_LENGTH_MISMATCH")
                    if len(chunk) > remaining:
                        raise ImportStoreError("IMPORT_LENGTH_MISMATCH")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(file_fd, view)
                        if written <= 0:
                            raise OSError("write failed")
                        view = view[written:]
                    remaining -= len(chunk)
                extra = stream.read(1)
                if not isinstance(extra, bytes):
                    raise ImportStoreError("IMPORT_LENGTH_MISMATCH")
                if extra:
                    raise ImportStoreError("IMPORT_LENGTH_MISMATCH")
                os.fsync(file_fd)
                original_identity = self._upload_file_identity(file_fd, declared_bytes)
                self._verify_current_import_directories(import_id, directory_fd)
                os.link(
                    temporary_name, final_name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                final_created = True
                final_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                final_flags |= getattr(os, "O_NOFOLLOW", 0)
                final_fd = os.open(final_name, final_flags, dir_fd=directory_fd)
                if self._upload_file_identity(final_fd, declared_bytes) != original_identity:
                    raise OSError("upload candidate replaced")
                os.unlink(temporary_name, dir_fd=directory_fd)
                os.fsync(directory_fd)

                checksum = digest.hexdigest()
                with self.store.transaction() as connection:
                    updated = connection.execute(
                        """UPDATE import_files SET upload_state = 'stored', sha256 = ?
                           WHERE import_id = ? AND slot_id = ? AND upload_state = 'pending'
                             AND EXISTS (
                                 SELECT 1 FROM imports
                                 WHERE id = ? AND status = 'uploading'
                             )""",
                        (checksum, import_id, slot_id, import_id),
                    )
                    if updated.rowcount != 1:
                        raise ImportStoreError("IMPORT_STATE_CONFLICT")
                    self._verify_current_import_directories(import_id, directory_fd)
                    verification_fd = None
                    try:
                        verification_fd = os.open(
                            final_name, final_flags, dir_fd=directory_fd
                        )
                        if (
                            self._upload_file_identity(
                                verification_fd, declared_bytes
                            )
                            != original_identity
                        ):
                            raise OSError("published upload replaced")
                    finally:
                        if verification_fd is not None:
                            os.close(verification_fd)
                committed = True
                return FileReceipt(import_id, source_id, declared_bytes, checksum)
            except ImportStoreError:
                raise
            except FileExistsError:
                raise ImportStoreError("IMPORT_SLOT_ALREADY_STORED") from None
            except (OSError, sqlite3.Error, AttributeError, TypeError, ValueError):
                raise ImportStoreError("IMPORT_STORAGE_FAILED") from None
            except Exception:
                raise ImportStoreError("IMPORT_STORAGE_FAILED") from None
            finally:
                if final_fd is not None:
                    try:
                        os.close(final_fd)
                    except OSError:
                        pass
                if file_fd is not None:
                    try:
                        os.close(file_fd)
                    except OSError:
                        pass
                if directory_fd is not None:
                    if not committed and final_created:
                        try:
                            os.unlink(final_name, dir_fd=directory_fd)
                        except OSError:
                            pass
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    except OSError:
                        pass
                    try:
                        os.close(directory_fd)
                    except OSError:
                        pass

    @staticmethod
    def _slot_from_row(row) -> ImportFileSlot:
        return ImportFileSlot(
            source_id=row[0], slot_id=row[1], logical_path=row[2], extension=row[3],
            declared_bytes=row[4], upload_state=row[5],
        )

    def snapshot(self, import_id) -> ImportSnapshot:
        if type(import_id) is not str:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        try:
            rows = self.store.connection().execute(
                """SELECT i.status, i.job_id, i.error_code, s.id,
                          f.source_id, f.slot_id, f.logical_path, f.extension,
                          f.declared_bytes, f.upload_state
                   FROM imports AS i
                   LEFT JOIN sessions AS s ON s.import_id = i.id
                   LEFT JOIN import_files AS f ON f.import_id = i.id
                   WHERE i.id = ?
                   ORDER BY f.rowid""",
                (import_id,),
            ).fetchall()
            if not rows:
                raise ImportStoreError("IMPORT_NOT_FOUND")
        except ImportStoreError:
            raise
        except (OSError, sqlite3.Error):
            raise ImportStoreError("IMPORT_STORAGE_FAILED") from None
        imported = rows[0]
        files = tuple(self._slot_from_row(row[4:]) for row in rows if row[4] is not None)
        return ImportSnapshot(
            id=import_id,
            status=imported[0],
            files=files,
            job_id=imported[1],
            session_id=imported[3],
            error_code=imported[2],
            total_files=len(files),
            stored_files=sum(slot.upload_state == "stored" for slot in files),
        )

    def start_finalize(self, import_id) -> ImportJob:
        self._known_import_status(import_id)
        raise ImportStoreError("IMPORT_NOT_READY")

    def open(self, import_id) -> ImportedWorkspace:
        self._known_import_status(import_id)
        raise ImportStoreError("IMPORT_NOT_READY")

    def _known_import_status(self, import_id) -> str:
        if type(import_id) is not str:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        try:
            row = self.store.connection().execute(
                "SELECT status FROM imports WHERE id = ?", (import_id,)
            ).fetchone()
        except (OSError, sqlite3.Error):
            raise ImportStoreError("IMPORT_STORAGE_FAILED") from None
        if row is None:
            raise ImportStoreError("IMPORT_NOT_FOUND")
        return row[0]

    def _remove_staging_batch(self, import_id) -> None:
        if not self._is_managed_id(import_id):
            raise OSError("unsafe import id")
        prefix = ("imports", ".staging", import_id)
        batch_expected = self._managed_directory_identities.get(prefix)
        originals_expected = self._managed_directory_identities.get(
            prefix + ("originals",)
        )
        if batch_expected is None or originals_expected is None:
            raise OSError("managed directory identity unavailable")
        staging_fd = self._open_managed_directory("imports", ".staging")
        batch_fd = None
        originals_fd = None
        try:
            flags = (
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                batch_fd = os.open(import_id, flags, dir_fd=staging_fd)
            except FileNotFoundError:
                self._forget_import_directory_identities(import_id)
                return
            batch_details = os.fstat(batch_fd)
            if (batch_details.st_dev, batch_details.st_ino) != batch_expected:
                raise OSError("managed batch directory replaced")
            originals_fd = os.open("originals", flags, dir_fd=batch_fd)
            originals_details = os.fstat(originals_fd)
            if (originals_details.st_dev, originals_details.st_ino) != originals_expected:
                raise OSError("managed originals directory replaced")
            for name in os.listdir(originals_fd):
                details = os.stat(name, dir_fd=originals_fd, follow_symlinks=False)
                if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
                    raise OSError("unexpected directory in originals")
                os.unlink(name, dir_fd=originals_fd)
            os.fsync(originals_fd)
            current_originals = os.stat(
                "originals", dir_fd=batch_fd, follow_symlinks=False
            )
            if (current_originals.st_dev, current_originals.st_ino) != originals_expected:
                raise OSError("managed originals directory replaced")
            os.rmdir("originals", dir_fd=batch_fd)
            if os.listdir(batch_fd):
                raise OSError("unexpected entry in import staging directory")
            os.fsync(batch_fd)
            current_batch = os.stat(import_id, dir_fd=staging_fd, follow_symlinks=False)
            if (current_batch.st_dev, current_batch.st_ino) != batch_expected:
                raise OSError("managed batch directory replaced")
            os.rmdir(import_id, dir_fd=staging_fd)
            os.fsync(staging_fd)
            self._forget_import_directory_identities(import_id)
        finally:
            for descriptor in (originals_fd, batch_fd, staging_fd):
                if descriptor is not None:
                    os.close(descriptor)

    def _forget_import_directory_identities(self, import_id) -> None:
        prefix = ("imports", ".staging", import_id)
        self._managed_directory_identities.pop(prefix, None)
        self._managed_directory_identities.pop(prefix + ("originals",), None)

    def cancel(self, import_id) -> ImportSnapshot:
        if type(import_id) is not str or not import_id:
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        try:
            lease = self.store.maintenance_gate.start("import-upload", "cancel:" + import_id)
        except (TypeError, ValueError):
            raise ImportStoreError("IMPORT_REQUEST_INVALID") from None
        with lease:
            try:
                with self.store.transaction() as connection:
                    row = connection.execute(
                        "SELECT status FROM imports WHERE id = ?", (import_id,)
                    ).fetchone()
                    if row is None:
                        raise ImportStoreError("IMPORT_NOT_FOUND")
                    if row[0] != "uploading":
                        raise ImportStoreError("IMPORT_STATE_CONFLICT")
                    updated = connection.execute(
                        """UPDATE imports SET status = 'cancelled', updated_at = ?
                           WHERE id = ? AND status = 'uploading'""",
                        (float(self.clock()), import_id),
                    )
                    if updated.rowcount != 1:
                        raise ImportStoreError("IMPORT_STATE_CONFLICT")
                self._remove_staging_batch(import_id)
            except ImportStoreError:
                raise
            except (OSError, sqlite3.Error, TypeError, ValueError):
                raise ImportStoreError("IMPORT_STORAGE_FAILED") from None
            return self.snapshot(import_id)
