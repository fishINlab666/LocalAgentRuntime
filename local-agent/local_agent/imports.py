"""Validated upload storage for local document imports."""

from dataclasses import dataclass, field
import ctypes
import errno
import hashlib
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import threading
import time
from types import MappingProxyType
import unicodedata
import uuid

from .import_parsers import DocumentParseError, ImportLimits
from .import_worker import parse_document_in_worker
from .state_maintenance import StateBusy


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
    "max_chunks": 256,
    "max_index_bytes": 16 * 1024,
    "max_extracted_total_bytes": 3 * 1024 * 1024,
}
_CHUNK_BYTES = 12 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PARSER_ERRORS = frozenset({
    "IMPORT_FORMAT_UNSUPPORTED", "TEXT_INVALID_UTF8", "TEXT_INVALID_CHARACTER",
    "PDF_TEXT_NOT_FOUND", "PDF_ENCRYPTED", "DOCUMENT_CORRUPT",
    "DOCUMENT_LIMIT_EXCEEDED", "DOCUMENT_PARSER_UNAVAILABLE",
})


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json_object(raw):
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


def _identity(details):
    return details.st_dev, details.st_ino


def _file_version(details):
    return (details.st_dev, details.st_ino, details.st_size,
            details.st_mtime_ns, details.st_ctime_ns)


def _read_fd(descriptor, maximum):
    before = os.fstat(descriptor)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > maximum):
        raise ValueError("unsafe import file")
    pieces = []
    offset = 0
    while offset <= maximum:
        piece = os.pread(descriptor, min(maximum + 1 - offset, 65536), offset)
        if not piece:
            break
        pieces.append(piece)
        offset += len(piece)
    raw = b"".join(pieces)
    if len(raw) != before.st_size or len(raw) > maximum or _file_version(before) != _file_version(os.fstat(descriptor)):
        raise ValueError("import file changed")
    return raw, before


def _read_at(directory_fd, name, maximum):
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    try:
        raw, before = _read_fd(descriptor, maximum)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _file_version(named) != _file_version(before):
            raise ValueError("import file changed")
        return raw
    finally:
        os.close(descriptor)


def _directory_at(parent_fd, name, expected=None):
    descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    details = os.fstat(descriptor)
    if (stat.S_IMODE(details.st_mode) != 0o700
            or expected is not None and _identity(details) != tuple(expected)):
        os.close(descriptor)
        raise ValueError("import directory changed")
    return descriptor


def _write_at(directory_fd, name, raw):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                         | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("short write")
            view = view[count:]
        os.fsync(descriptor)
        if _identity(os.fstat(descriptor)) != _identity(os.stat(
                name, dir_fd=directory_fd, follow_symlinks=False)):
            raise OSError("output replaced")
    finally:
        os.close(descriptor)


def _remove_tree_at(parent_fd, name, expected):
    descriptor = _directory_at(parent_fd, name, expected)
    try:
        for child in os.listdir(descriptor):
            details = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                _remove_tree_at(descriptor, child, _identity(details))
            else:
                os.unlink(child, dir_fd=descriptor)
        os.fsync(descriptor)
        if _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != tuple(expected):
            raise OSError("cleanup directory replaced")
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(descriptor)


def _rename_exclusive(source_fd, name, destination_fd):
    # POSIX rename can replace an empty destination directory; use no-replace
    # kernel operations so a competing publication can never be overwritten.
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        operation = library.renameatx_np
        flags = 4  # RENAME_EXCL
    else:
        operation = getattr(library, "renameat2", None)
        flags = 1  # RENAME_NOREPLACE
    if operation is None:
        raise OSError(errno.ENOTSUP, "exclusive directory rename unavailable")
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    encoded = name.encode("ascii")
    if operation(source_fd, encoded, destination_fd, encoded, flags) != 0:
        raise OSError(ctypes.get_errno(), "import publication failed")


def _location(value, kind):
    keys = {
        "text_lines": {"kind", "start", "end"}, "pdf_page": {"kind", "page"},
        "docx_paragraph": {"kind", "paragraph"},
        "docx_table_row": {"kind", "table", "row"},
    }
    allowed = {"txt": {"text_lines"}, "md": {"text_lines"},
               "pdf": {"pdf_page"}, "docx": {"docx_paragraph", "docx_table_row"}}
    if (type(value) is not dict or value.get("kind") not in allowed[kind]
            or set(value) != keys[value["kind"]]
            or any(type(v) is not int or v < 1 for k, v in value.items() if k != "kind")
            or value["kind"] == "text_lines" and value["start"] != value["end"]):
        raise ValueError("invalid source location")
    return dict(value)


def _pieces(text):
    if len(text.encode("utf-8")) <= _CHUNK_BYTES:
        yield text
        return
    paragraphs = re.findall(r".+?(?:\n[ \t]*\n|\Z)", text, re.DOTALL)
    for paragraph in paragraphs:
        if len(paragraph.encode("utf-8")) <= _CHUNK_BYTES:
            yield paragraph
            continue
        for line in paragraph.splitlines(keepends=True):
            raw = line.encode("utf-8")
            while raw:
                piece = raw[:_CHUNK_BYTES].decode("utf-8", errors="ignore")
                yield piece
                raw = raw[len(piece.encode("utf-8")):]


def _chunks(document):
    result = []
    parts = []
    spans = []
    size = 0
    characters = 0

    def flush():
        nonlocal parts, spans, size, characters
        if not parts:
            return
        content = "".join(parts)
        mappings = {}
        offset = 0
        cursor = 0
        for number, line in enumerate(content.splitlines(keepends=True), 1):
            end = offset + len(line)
            while cursor < len(spans) and spans[cursor][1] <= offset:
                cursor += 1
            locations = []
            for start, stop, location in spans[cursor:]:
                if start >= end:
                    break
                if stop > offset and location not in locations:
                    locations.append(location)
            mappings[str(number)] = locations
            offset = end
        result.append((content.encode("utf-8"), mappings))
        parts, spans, size, characters = [], [], 0, 0

    for unit in document.units:
        text = unit.text.replace("\r\n", "\n").replace("\r", "\n")
        location = _location(unit.location, document.stats["format"])
        if document.stats["format"] in {"pdf", "docx"} and not text.endswith("\n"):
            text += "\n"
        for piece in _pieces(text):
            encoded_size = len(piece.encode("utf-8"))
            if size + encoded_size > _CHUNK_BYTES:
                flush()
            if piece:
                parts.append(piece)
                spans.append((characters, characters + len(piece), location))
                size += encoded_size
                characters += len(piece)
    flush()
    return result


def _parser_record(kind):
    engine = {"txt": "python", "md": "python", "pdf": "pypdf", "docx": "python-docx"}[kind]
    version = (".".join(map(str, sys.version_info[:3])) if engine == "python"
               else package_version(engine))
    return {"implementation": "local_agent.import_parsers", "contract_version": 1,
            "engine": engine, "version": version}


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
    locations: Path
    workspace_device: int
    workspace_inode: int
    artifact_device: int
    artifact_inode: int


@dataclass(frozen=True)
class PublishedImport:
    import_id: str
    job_id: str
    status: str
    root: Path
    workspace: Path
    artifacts: Path
    manifest: Path
    locations: Path
    chunk_paths: tuple[Path, ...]
    manifest_hashes: MappingProxyType
    identities: MappingProxyType
    file_records: tuple[MappingProxyType, ...]
    manifest_sha256: str
    request_fingerprint: str


@dataclass(frozen=True)
class ImportJob:
    import_id: str
    job_id: str
    _store: "ImportStore" = field(repr=False, compare=False)

    def run(self) -> PublishedImport:
        return self._store._run_finalize(self.import_id, self.job_id)


class ImportStore:
    def __init__(self, store, *, limits=None, clock=time.time, parser_limits=None, fault=None):
        self.store = store
        self.clock = clock
        self.parser_limits = parser_limits if parser_limits is not None else ImportLimits()
        if not isinstance(self.parser_limits, ImportLimits) or fault is not None and not callable(fault):
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        self._fault = fault or (lambda _point: None)
        with store._lifecycle_lock:
            if not hasattr(store, "_import_coordination"):
                store._import_coordination = ({}, threading.Lock())
            self._controls, self._controls_lock = store._import_coordination
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
        lock, _cancelled = self._control(import_id)
        with lock, self.store.maintenance_gate.start("import-finalize-start", import_id):
            try:
                row, files = self._record(import_id)
                if self._formal_exists(import_id):
                    if row["status"] not in {"finalizing", "ready"}:
                        raise ImportStoreError("IMPORT_INTEGRITY_ERROR")
                    if row['status'] == 'ready':
                        self.open(import_id)
                    self._load_publication(row, files)
                elif row["status"] == "uploading":
                    if not files or any(item["upload_state"] != "stored" for item in files):
                        raise ImportStoreError("IMPORT_UPLOAD_INCOMPLETE")
                    self._verify_current_import_directories(import_id)
                    job_id = uuid.uuid4().hex
                    with self.store.transaction() as connection:
                        changed = connection.execute(
                            """UPDATE imports SET status='finalizing', job_id=?, updated_at=?
                               WHERE id=? AND status='uploading'
                               AND NOT EXISTS (SELECT 1 FROM import_files
                                   WHERE import_id=? AND upload_state<>'stored')""",
                            (job_id, float(self.clock()), import_id, import_id),
                        )
                        if changed.rowcount != 1:
                            raise ImportStoreError("IMPORT_STATE_CONFLICT")
                    row["status"], row["job_id"] = "finalizing", job_id
                elif row["status"] != "finalizing":
                    raise ImportStoreError(row["error_code"] or "IMPORT_STATE_CONFLICT")
                if not self._is_managed_id(row["job_id"]):
                    raise ImportStoreError("IMPORT_INTEGRITY_ERROR")
                return ImportJob(import_id, row["job_id"], self)
            except ImportStoreError:
                raise
            except (OSError, ValueError, TypeError, sqlite3.Error):
                raise ImportStoreError("IMPORT_INTEGRITY_ERROR") from None

    def _control(self, import_id):
        if not self._is_managed_id(import_id):
            raise ImportStoreError("IMPORT_REQUEST_INVALID")
        with self._controls_lock:
            return self._controls.setdefault(import_id, (threading.RLock(), threading.Event()))

    def _record(self, import_id):
        cursor = self.store.connection().execute("SELECT * FROM imports WHERE id=?", (import_id,))
        values = cursor.fetchone()
        if values is None:
            raise ImportStoreError("IMPORT_NOT_FOUND")
        row = dict(zip((column[0] for column in cursor.description), values))
        cursor = self.store.connection().execute(
            "SELECT * FROM import_files WHERE import_id=? ORDER BY rowid", (import_id,)
        )
        names = [column[0] for column in cursor.description]
        files = [dict(zip(names, values)) for values in cursor.fetchall()]
        request = _json_object(row["request_json"].encode("utf-8"))
        expected = self._validate_request(request)
        if (hashlib.sha256(_canonical(request)).hexdigest() != row["request_fingerprint"]
                or len(files) != len(expected) or request["agent_id"] != row["agent_id"]):
            raise ValueError("request changed")
        for item, (logical_path, extension, declared_bytes) in zip(files, expected):
            if (not self._is_managed_id(item["source_id"])
                    or not self._is_managed_id(item["slot_id"])
                    or (item["logical_path"], item["extension"], item["declared_bytes"])
                    != (logical_path, extension, declared_bytes)
                    or item["upload_state"] == "stored" and (
                        type(item["sha256"]) is not str or not _HASH.fullmatch(item["sha256"]))):
                raise ValueError("invalid stored source")
        return row, files

    def _formal_exists(self, import_id):
        descriptor = self._open_managed_directory("imports")
        try:
            try:
                os.stat(import_id, dir_fd=descriptor, follow_symlinks=False)
                return True
            except FileNotFoundError:
                return False
        finally:
            os.close(descriptor)

    def _batch_fd(self, import_id, *, formal=False):
        components = ("imports",) if formal else ("imports", ".staging")
        parent = self._open_managed_directory(*components)
        try:
            expected = self._managed_directory_identities.get(components + (import_id,))
            return _directory_at(parent, import_id, expected)
        finally:
            os.close(parent)

    def _adopt_staging(self, row, files):
        if row["status"] != "finalizing":
            raise ValueError("only finalizing staging can resume")
        descriptor = self._batch_fd(row["id"])
        try:
            prefix = ("imports", ".staging", row["id"])
            originals = _directory_at(descriptor, "originals",
                                     self._managed_directory_identities.get(prefix + ("originals",)))
            try:
                if set(os.listdir(originals)) != {item["source_id"] + ".bin" for item in files}:
                    raise ValueError("unexpected originals")
                for item in files:
                    raw = _read_at(originals, item["source_id"] + ".bin", item["declared_bytes"])
                    if (len(raw) != item["declared_bytes"] or item["upload_state"] != "stored"
                            or hashlib.sha256(raw).hexdigest() != item["sha256"]):
                        raise ValueError("original changed")
                self._managed_directory_identities[prefix] = _identity(os.fstat(descriptor))
                self._managed_directory_identities[prefix + ("originals",)] = _identity(os.fstat(originals))
            finally:
                os.close(originals)
        finally:
            os.close(descriptor)

    @staticmethod
    def _import_fingerprint(row, files):
        return hashlib.sha256(_canonical({
            "import_id": row["id"], "request_fingerprint": row["request_fingerprint"],
            "sources": [[item["source_id"], item["sha256"]] for item in files],
        })).hexdigest()

    def _clear_generated(self, import_id, batch_fd):
        prefix = ("imports", ".staging", import_id)
        if not set(os.listdir(batch_fd)).issubset(
                {"originals", "workspace", "artifacts", "manifest.json", "locations.json"}):
            raise ValueError("unexpected staged file")
        for name in ("workspace", "artifacts", "manifest.json", "locations.json"):
            try:
                details = os.stat(name, dir_fd=batch_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if name in {"workspace", "artifacts"}:
                expected = self._managed_directory_identities.get(prefix + (name,), _identity(details))
                _remove_tree_at(batch_fd, name, expected)
                self._managed_directory_identities.pop(prefix + (name,), None)
            else:
                if not stat.S_ISREG(details.st_mode):
                    raise ValueError("unexpected staged metadata")
                os.unlink(name, dir_fd=batch_fd)
        os.fsync(batch_fd)

    def _parse_staged(self, row, files, cancelled):
        import_id = row["id"]
        self._adopt_staging(row, files)
        batch_fd = originals_fd = workspace_fd = artifacts_fd = documents_fd = None
        try:
            batch_fd = self._batch_fd(import_id)
            originals_fd = self._open_originals_directory(import_id)
            self._clear_generated(import_id, batch_fd)
            parsed = []
            total_text = 0
            chunk_count = 0
            for item in files:
                if cancelled.is_set():
                    raise ImportStoreError("IMPORT_CANCELLED")
                name = item["source_id"] + ".bin"
                source_fd = os.open(name, _FILE_FLAGS, dir_fd=originals_fd)
                try:
                    raw, fixed = _read_fd(source_fd, item["declared_bytes"])
                    named = os.stat(name, dir_fd=originals_fd, follow_symlinks=False)
                    if (hashlib.sha256(raw).hexdigest() != item["sha256"]
                            or _file_version(named) != _file_version(fixed)):
                        raise ImportStoreError("IMPORT_INTEGRITY_ERROR")
                    self._verify_current_import_directories(import_id, originals_fd)
                    document = parse_document_in_worker(
                        self.staging / import_id / "originals" / name,
                        item["logical_path"], self.parser_limits, cancelled=cancelled.is_set,
                        source_fd=source_fd, source_identity=_identity(fixed),
                    )
                    self._verify_current_import_directories(import_id, originals_fd)
                    after, current = _read_fd(source_fd, item["declared_bytes"])
                    named = os.stat(name, dir_fd=originals_fd, follow_symlinks=False)
                    if (after != raw or _file_version(current) != _file_version(fixed)
                            or _file_version(named) != _file_version(fixed)):
                        raise ImportStoreError("IMPORT_INTEGRITY_ERROR")
                finally:
                    os.close(source_fd)
                if (document.logical_path != item["logical_path"]
                        or document.stats["format"] != item["extension"][1:]
                        or document.stats["source_bytes"] != len(raw)):
                    raise ImportStoreError("DOCUMENT_CORRUPT")
                chunks = _chunks(document)
                normalized_bytes = sum(len(content) for content, _mapping in chunks)
                total_text += max(document.stats["extracted_bytes"], normalized_bytes)
                chunk_count += len(chunks)
                if (total_text > min(self.limits["max_extracted_total_bytes"], 3 * 1024 * 1024)
                        or chunk_count > min(self.limits["max_chunks"], 256)):
                    raise ImportStoreError("DOCUMENT_LIMIT_EXCEEDED")
                parsed.append((item, document, chunks, raw))

            os.mkdir("workspace", 0o700, dir_fd=batch_fd)
            workspace_fd = _directory_at(batch_fd, "workspace")
            os.mkdir("artifacts", 0o700, dir_fd=batch_fd)
            artifacts_fd = _directory_at(batch_fd, "artifacts")
            os.mkdir("documents", 0o700, dir_fd=workspace_fd)
            documents_fd = _directory_at(workspace_fd, "documents")
            identities = {"workspace": list(_identity(os.fstat(workspace_fd))),
                          "artifacts": list(_identity(os.fstat(artifacts_fd)))}
            prefix = ("imports", ".staging", import_id)
            for name, identity in identities.items():
                self._managed_directory_identities[prefix + (name,)] = tuple(identity)
            hashes = {}
            locations = {}
            sources = []
            catalog = ["# Imported documents\n"]
            for item, document, chunks, raw in parsed:
                source_id = item["source_id"]
                os.mkdir(source_id, 0o700, dir_fd=documents_fd)
                source_fd = _directory_at(documents_fd, source_id)
                records = []
                try:
                    for number, (content, mapping) in enumerate(chunks, 1):
                        name = f"chunk-{number:04d}.md"
                        path = f"documents/{source_id}/{name}"
                        _write_at(source_fd, name, content)
                        hashes["workspace/" + path] = hashlib.sha256(content).hexdigest()
                        locations[path] = mapping
                        records.append({"path": path, "bytes": len(content), "line_count": len(mapping),
                                        "location_start": mapping["1"][0],
                                        "location_end": mapping[str(len(mapping))][-1]})
                    os.fsync(source_fd)
                finally:
                    os.close(source_fd)
                hashes[f"originals/{source_id}.bin"] = item["sha256"]
                newlines = None
                if item["extension"] in {".txt", ".md"}:
                    newlines = {"crlf": raw.count(b"\r\n"), "cr": raw.count(b"\r") - raw.count(b"\r\n"),
                                "lf": raw.count(b"\n") - raw.count(b"\r\n")}
                source = {"source_id": source_id, "logical_path": item["logical_path"],
                          "format": item["extension"][1:], "original_sha256": item["sha256"],
                          "parser": _parser_record(item["extension"][1:]), "stats": document.stats,
                          "warnings": list(document.warnings), "original_newlines": newlines,
                          "normalized_bytes": sum(len(content) for content, _ in chunks), "chunks": records}
                sources.append(source)
                catalog.append(_canonical({
                    "logical_path": item["logical_path"], "format": source["format"],
                    "chunks": [1, len(records)] if records else [],
                    "location_start": records[0]["location_start"] if records else None,
                    "location_end": records[-1]["location_end"] if records else None,
                }).decode("utf-8") + "\n")
            index = "".join(catalog).encode("utf-8")
            if len(index) > min(self.limits["max_index_bytes"], 16 * 1024):
                raise ImportStoreError("DOCUMENT_LIMIT_EXCEEDED")
            _write_at(workspace_fd, "index.md", index)
            location_bytes = _canonical(locations)
            _write_at(batch_fd, "locations.json", location_bytes)
            hashes["workspace/index.md"] = hashlib.sha256(index).hexdigest()
            hashes["locations.json"] = hashlib.sha256(location_bytes).hexdigest()
            manifest = {"version": 1, "status": "parsed", "import_id": import_id, "job_id": row["job_id"],
                        "request_fingerprint": row["request_fingerprint"],
                        "import_fingerprint": self._import_fingerprint(row, files),
                        "sources": sources, "hashes": hashes, "identities": identities}
            manifest_bytes = _canonical(manifest)
            _write_at(batch_fd, "manifest.json", manifest_bytes)
            for descriptor in (documents_fd, workspace_fd, artifacts_fd, originals_fd, batch_fd):
                os.fsync(descriptor)
            self._verify_current_import_directories(import_id, originals_fd)
            staging_fd = self._open_managed_directory("imports", ".staging")
            try:
                os.fsync(staging_fd)
            finally:
                os.close(staging_fd)
            root_fd = self._open_managed_directory("imports")
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            with self.store.transaction() as connection:
                changed = connection.execute(
                    """UPDATE imports SET manifest_sha256=?, workspace_device=?, workspace_inode=?,
                       artifact_device=?, artifact_inode=?, updated_at=?
                       WHERE id=? AND status='finalizing' AND job_id=?""",
                    (hashlib.sha256(manifest_bytes).hexdigest(), *identities["workspace"],
                     *identities["artifacts"], float(self.clock()), import_id, row["job_id"]),
                )
                if changed.rowcount != 1:
                    raise ImportStoreError("IMPORT_STATE_CONFLICT")
        finally:
            for descriptor in (documents_fd, artifacts_fd, workspace_fd, originals_fd, batch_fd):
                if descriptor is not None:
                    os.close(descriptor)

    def _validate_manifest(self, manifest, row, files):
        if (type(manifest) is not dict or set(manifest) != {
                "version", "status", "import_id", "job_id", "request_fingerprint",
                "import_fingerprint", "sources", "hashes", "identities"}
                or type(manifest["version"]) is not int or manifest["version"] != 1
                or manifest["status"] != "parsed" or manifest["import_id"] != row["id"]
                or manifest["job_id"] != row["job_id"]
                or manifest["request_fingerprint"] != row["request_fingerprint"]
                or manifest["import_fingerprint"] != self._import_fingerprint(row, files)):
            raise ValueError("invalid manifest")
        expected_identities = {"workspace": [row["workspace_device"], row["workspace_inode"]],
                               "artifacts": [row["artifact_device"], row["artifact_inode"]]}
        if (manifest["identities"] != expected_identities or any(
                type(value) is not int or value < 0 for pair in expected_identities.values() for value in pair)):
            raise ValueError("invalid identities")
        if type(manifest["sources"]) is not list or len(manifest["sources"]) != len(files):
            raise ValueError("invalid sources")
        expected_files = {"manifest.json", "locations.json", "workspace/index.md"}
        expected_dirs = {"originals", "workspace", "workspace/documents", "artifacts"}
        chunks = []
        total = 0
        for source, item in zip(manifest["sources"], files):
            if (type(source) is not dict or set(source) != {
                    "source_id", "logical_path", "format", "original_sha256", "parser", "stats",
                    "warnings", "original_newlines", "normalized_bytes", "chunks"}
                    or source["source_id"] != item["source_id"]
                    or source["logical_path"] != item["logical_path"]
                    or source["format"] != item["extension"][1:]
                    or source["original_sha256"] != item["sha256"]):
                raise ValueError("invalid source record")
            kind = source["format"]
            parser = source["parser"]
            engine = {"txt": "python", "md": "python", "pdf": "pypdf", "docx": "python-docx"}[kind]
            if (type(parser) is not dict or set(parser) != {"implementation", "contract_version", "engine", "version"}
                    or parser["implementation"] != "local_agent.import_parsers"
                    or type(parser["contract_version"]) is not int or parser["contract_version"] != 1
                    or parser["engine"] != engine or type(parser["version"]) is not str
                    or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", parser["version"]) is None):
                raise ValueError("invalid parser record")
            stats = source["stats"]
            keys = {"format", "source_bytes", "extracted_bytes", "unit_count"}
            if kind == "pdf":
                keys.update({"page_count", "pdf_content_bytes"})
            if kind == "docx":
                keys.update({"docx_entries", "docx_uncompressed_bytes"})
            if (type(stats) is not dict or set(stats) != keys or stats["format"] != kind
                    or any(type(stats[key]) is not int or stats[key] < 0 for key in keys - {"format"})
                    or stats["source_bytes"] != item["declared_bytes"]
                    or stats["extracted_bytes"] > self.parser_limits.max_extracted_bytes):
                raise ValueError("invalid parser statistics")
            warnings = source["warnings"]
            if type(warnings) is not list or kind != "pdf" and warnings:
                raise ValueError("invalid parser warnings")
            for warning in warnings:
                if (type(warning) is not dict or set(warning) != {"code", "page"}
                        or warning["code"] != "PDF_PAGE_TEXT_NOT_FOUND" or type(warning["page"]) is not int
                        or not 1 <= warning["page"] <= stats["page_count"]):
                    raise ValueError("invalid PDF warning")
            newlines = source["original_newlines"]
            if kind in {"txt", "md"}:
                if (type(newlines) is not dict or set(newlines) != {"crlf", "cr", "lf"}
                        or any(type(value) is not int or value < 0 for value in newlines.values())):
                    raise ValueError("invalid newline record")
            elif newlines is not None:
                raise ValueError("invalid newline record")
            if type(source["chunks"]) is not list:
                raise ValueError("invalid chunks")
            normalized_bytes = 0
            for number, chunk in enumerate(source["chunks"], 1):
                path = f"documents/{item['source_id']}/chunk-{number:04d}.md"
                if (type(chunk) is not dict or set(chunk) != {
                        "path", "bytes", "line_count", "location_start", "location_end"}
                        or chunk["path"] != path or type(chunk["bytes"]) is not int
                        or not 1 <= chunk["bytes"] <= _CHUNK_BYTES
                        or type(chunk["line_count"]) is not int or chunk["line_count"] < 1):
                    raise ValueError("invalid chunk")
                _location(chunk["location_start"], kind)
                _location(chunk["location_end"], kind)
                normalized_bytes += chunk["bytes"]
                expected_files.add("workspace/" + path)
                chunks.append((chunk, kind))
            if type(source["normalized_bytes"]) is not int or source["normalized_bytes"] != normalized_bytes:
                raise ValueError("invalid normalized size")
            total += max(normalized_bytes, stats["extracted_bytes"])
            expected_files.add(f"originals/{item['source_id']}.bin")
            expected_dirs.add(f"workspace/documents/{item['source_id']}")
        if total > 3 * 1024 * 1024 or len(chunks) > 256:
            raise ValueError("published limits exceeded")
        hashes = manifest["hashes"]
        if (type(hashes) is not dict or set(hashes) != expected_files - {"manifest.json"}
                or any(type(value) is not str or not _HASH.fullmatch(value) for value in hashes.values())):
            raise ValueError("invalid file manifest")
        for item in files:
            if hashes[f"originals/{item['source_id']}.bin"] != item["sha256"]:
                raise ValueError("original digest changed")
        return expected_files, expected_dirs, chunks

    def _load_publication(self, row, files, *, formal=True):
        try:
            if (row["status"] not in {"finalizing", "ready"} or not self._is_managed_id(row["job_id"])
                    or type(row["manifest_sha256"]) is not str or not _HASH.fullmatch(row["manifest_sha256"])):
                raise ValueError("publication not anchored")
            batch_fd = self._batch_fd(row["id"], formal=formal)
            try:
                raw_manifest = _read_at(batch_fd, "manifest.json", 8 * 1024 * 1024)
                if hashlib.sha256(raw_manifest).hexdigest() != row["manifest_sha256"]:
                    raise ValueError("manifest changed")
                manifest = _json_object(raw_manifest)
                expected_files, expected_dirs, chunks = self._validate_manifest(manifest, row, files)
                remaining_files, remaining_dirs = set(expected_files), set(expected_dirs)
                contents = {}
                def visit(descriptor, prefix=""):
                    for name in os.listdir(descriptor):
                        relative = prefix + name
                        details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        if relative in remaining_dirs and stat.S_ISDIR(details.st_mode):
                            remaining_dirs.remove(relative)
                            child = _directory_at(descriptor, name, manifest["identities"].get(relative))
                            try:
                                visit(child, relative + "/")
                                if _identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) != _identity(os.fstat(child)):
                                    raise ValueError("directory changed")
                            finally:
                                os.close(child)
                        elif relative in remaining_files and stat.S_ISREG(details.st_mode):
                            remaining_files.remove(relative)
                            maximum = (self.parser_limits.max_file_bytes if relative.startswith("originals/")
                                       else _CHUNK_BYTES if relative.startswith("workspace/documents/")
                                       else 16 * 1024 if relative == "workspace/index.md"
                                       else 8 * 1024 * 1024 if relative == "manifest.json"
                                       else 3 * 1024 * 1024 * 256)
                            raw = _read_at(descriptor, name, maximum)
                            expected_hash = row["manifest_sha256"] if relative == "manifest.json" else manifest["hashes"][relative]
                            if hashlib.sha256(raw).hexdigest() != expected_hash:
                                raise ValueError("file digest changed")
                            if not relative.startswith("originals/"):
                                contents[relative] = raw
                        else:
                            raise ValueError("unexpected published entry")
                visit(batch_fd)
                if remaining_files or remaining_dirs:
                    raise ValueError("publication incomplete")
                locations = _json_object(contents["locations.json"])
                if type(locations) is not dict or set(locations) != {chunk["path"] for chunk, _ in chunks}:
                    raise ValueError("invalid locations")
                for chunk, kind in chunks:
                    raw = contents["workspace/" + chunk["path"]]
                    lines = raw.decode("utf-8", errors="strict").splitlines()
                    mapping = locations[chunk["path"]]
                    if (len(raw) != chunk["bytes"] or len(lines) != chunk["line_count"]
                            or type(mapping) is not dict or set(mapping) != {str(n) for n in range(1, len(lines) + 1)}):
                        raise ValueError("invalid mapped lines")
                    for values in mapping.values():
                        if type(values) is not list or not values:
                            raise ValueError("unmapped line")
                        for value in values:
                            _location(value, kind)
                    if mapping["1"][0] != chunk["location_start"] or mapping[str(len(lines))][-1] != chunk["location_end"]:
                        raise ValueError("invalid mapped range")
                components = ("imports",) if formal else ("imports", ".staging")
                parent = self._open_managed_directory(*components)
                try:
                    if _identity(os.stat(row["id"], dir_fd=parent, follow_symlinks=False)) != _identity(os.fstat(batch_fd)):
                        raise ValueError("batch directory replaced")
                    if formal:
                        os.fsync(parent)
                        staging = self._open_managed_directory("imports", ".staging")
                        try:
                            os.fsync(staging)
                        finally:
                            os.close(staging)
                finally:
                    os.close(parent)
            finally:
                os.close(batch_fd)
            root = self.root / row["id"]
            records = tuple(MappingProxyType({
                "source_id": item["source_id"], "logical_path": item["logical_path"],
                "extension": item["extension"], "declared_bytes": item["declared_bytes"],
                "sha256": item["sha256"], "parser_json": _canonical(source).decode("utf-8"),
            }) for item, source in zip(files, manifest["sources"]))
            return PublishedImport(
                row["id"], row["job_id"], "published_unlinked", root, root / "workspace", root / "artifacts",
                root / "manifest.json", root / "locations.json",
                tuple(root / "workspace" / chunk["path"] for chunk, _ in chunks),
                MappingProxyType(dict(manifest["hashes"])),
                MappingProxyType({key: tuple(value) for key, value in manifest["identities"].items()}),
                records, row["manifest_sha256"], row["request_fingerprint"],
            )
        except ImportStoreError:
            raise
        except (OSError, ValueError, TypeError, KeyError, UnicodeError, sqlite3.Error):
            raise ImportStoreError("IMPORT_INTEGRITY_ERROR") from None

    def _set_failure(self, import_id, code):
        with self.store.transaction() as connection:
            connection.execute(
                """UPDATE imports SET status=?, error_code=?, updated_at=?
                   WHERE id=? AND status IN ('uploading','finalizing')""",
                ("cancelled" if code == "IMPORT_CANCELLED" else "failed", code,
                 float(self.clock()), import_id),
            )

    def _run_finalize(self, import_id, job_id):
        lock, cancelled = self._control(import_id)
        try:
            with lock, self.store.maintenance_gate.start("import-finalize", import_id):
                try:
                    row, files = self._record(import_id)
                    if row["status"] not in {"finalizing", "ready"} or row["job_id"] != job_id:
                        raise ImportStoreError(row["error_code"] or "IMPORT_STATE_CONFLICT")
                    if self._formal_exists(import_id):
                        if row['status'] == 'ready':
                            self.open(import_id)
                        return self._load_publication(row, files)
                    if row['status'] == 'ready':
                        self.open(import_id)
                    if cancelled.is_set():
                        raise ImportStoreError("IMPORT_CANCELLED")
                    self._adopt_staging(row, files)
                    staged_valid = False
                    if row["manifest_sha256"] is not None:
                        try:
                            self._load_publication(row, files, formal=False)
                            staged_valid = True
                        except ImportStoreError:
                            pass
                    if not staged_valid:
                        self._parse_staged(row, files, cancelled)
                    self._fault("after_parse_fsync")
                    row, files = self._record(import_id)
                    self._load_publication(row, files, formal=False)
                    if cancelled.is_set():
                        raise ImportStoreError("IMPORT_CANCELLED")
                    staging_fd = root_fd = None
                    try:
                        staging_fd = self._open_managed_directory("imports", ".staging")
                        root_fd = self._open_managed_directory("imports")
                        self._verify_current_import_directories(import_id)
                        _rename_exclusive(staging_fd, import_id, root_fd)
                        self._fault("after_publish_rename")
                        os.fsync(root_fd)
                        os.fsync(staging_fd)
                        self._fault("after_parent_fsync")
                    finally:
                        for descriptor in (root_fd, staging_fd):
                            if descriptor is not None:
                                os.close(descriptor)
                    published = self._load_publication(row, files)
                    self._forget_import_directory_identities(import_id)
                    return published
                except DocumentParseError as error:
                    code = ("IMPORT_CANCELLED" if error.code == "DOCUMENT_PARSER_CANCELLED"
                            else "DOCUMENT_LIMIT_EXCEEDED" if error.code == "DOCUMENT_PARSER_TIMEOUT"
                            else error.code if error.code in _PARSER_ERRORS else "DOCUMENT_CORRUPT")
                    self._fail_unpublished(import_id, code)
                    raise ImportStoreError(code) from None
                except ImportStoreError as error:
                    self._fail_unpublished(import_id, error.code)
                    raise
                except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                    self._fail_unpublished(import_id, "IMPORT_INTEGRITY_ERROR")
                    raise ImportStoreError("IMPORT_INTEGRITY_ERROR") from None
                except Exception:
                    self._fail_unpublished(import_id, "IMPORT_STORAGE_FAILED")
                    raise ImportStoreError("IMPORT_STORAGE_FAILED") from None
        finally:
            self.store.close_thread_connection()

    def _fail_unpublished(self, import_id, code):
        try:
            if self._formal_exists(import_id):
                return
            self._set_failure(import_id, code)
            self._remove_staging_batch(import_id)
        except Exception:
            # A replaced directory or unavailable DB must not mask the stable
            # primary error or cause cleanup through an untrusted pathname.
            pass

    def recover_interrupted(self):
        """Reconcile imports only; Session linking and Run recovery are separate."""
        try:
            return self._recover_interrupted()
        except (ImportStoreError, StateBusy):
            raise
        except Exception:
            raise ImportStoreError("IMPORT_STORAGE_FAILED") from None

    def _recover_interrupted(self):
        published = []
        with self.store.maintenance_gate.maintenance():
            rows = self.store.connection().execute("SELECT id, status FROM imports").fetchall()
            known = {row[0] for row in rows}
            for components in (("imports",), ("imports", ".staging")):
                parent = self._open_managed_directory(*components)
                try:
                    for name in os.listdir(parent):
                        if not self._is_managed_id(name) or name in known:
                            continue
                        details = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if stat.S_ISDIR(details.st_mode):
                            _remove_tree_at(parent, name, _identity(details))
                        else:
                            os.unlink(name, dir_fd=parent)
                    os.fsync(parent)
                finally:
                    os.close(parent)
            for import_id, status in rows:
                if status not in {"uploading", "finalizing"}:
                    continue
                lock, _cancelled = self._control(import_id)
                with lock:
                    try:
                        row, files = self._record(import_id)
                        if self._formal_exists(import_id):
                            published.append(self._load_publication(row, files))
                        elif status == "finalizing":
                            self._adopt_staging(row, files)
                        else:
                            self._set_failure(import_id, "IMPORT_UPLOAD_INCOMPLETE")
                            parent = self._open_managed_directory("imports", ".staging")
                            try:
                                try:
                                    details = os.stat(import_id, dir_fd=parent, follow_symlinks=False)
                                except FileNotFoundError:
                                    continue
                                _remove_tree_at(parent, import_id, _identity(details))
                            finally:
                                os.close(parent)
                    except (ImportStoreError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                        self._set_failure(import_id, "IMPORT_INTEGRITY_ERROR")
        return tuple(published)

    def open(self, import_id) -> ImportedWorkspace:
        lock, _ = self._control(import_id)
        with lock:
            status = self._known_import_status(import_id)
            if status == "unavailable":
                raise ImportStoreError("IMPORT_UNAVAILABLE")
            if status != "ready":
                raise ImportStoreError("IMPORT_NOT_READY")
            try:
                row, files = self._record(import_id)
                published = self._load_publication(row, files)
                if any(item['parser_json'] != record['parser_json']
                       for item, record in zip(files, published.file_records)):
                    raise ValueError('parser record changed')
                return ImportedWorkspace(
                    import_id, published.root, published.root / 'originals',
                    published.workspace, published.artifacts, published.manifest,
                    published.locations, *published.identities['workspace'],
                    *published.identities['artifacts'])
            except (ImportStoreError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                with self.store.transaction() as connection:
                    connection.execute(
                        "UPDATE imports SET status='unavailable', error_code='IMPORT_INTEGRITY_ERROR', "
                        "updated_at=? WHERE id=? AND status='ready'",
                        (float(self.clock()), import_id))
                raise ImportStoreError("IMPORT_INTEGRITY_ERROR") from None

    def _known_import_status(self, import_id) -> str:
        if not self._is_managed_id(import_id):
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
            self._clear_generated(import_id, batch_fd)
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
        for key in tuple(self._managed_directory_identities):
            if key[:len(prefix)] == prefix:
                self._managed_directory_identities.pop(key, None)

    def cancel(self, import_id) -> ImportSnapshot:
        lock, cancelled = self._control(import_id)
        status = self._known_import_status(import_id)
        if status == "finalizing":
            cancelled.set()
        with lock:
            if status == "uploading":
                return self._cancel_uploading(import_id)
            with self.store.maintenance_gate.start("import-upload", "cancel:" + import_id):
                try:
                    row, files = self._record(import_id)
                    if self._formal_exists(import_id):
                        raise ImportStoreError("IMPORT_STATE_CONFLICT")
                    if row["status"] == "cancelled" and status == "finalizing":
                        return self.snapshot(import_id)
                    if row["status"] != "finalizing":
                        raise ImportStoreError("IMPORT_STATE_CONFLICT")
                    self._adopt_staging(row, files)
                    self._remove_staging_batch(import_id)
                    self._set_failure(import_id, "IMPORT_CANCELLED")
                    return self.snapshot(import_id)
                except ImportStoreError:
                    raise
                except (OSError, ValueError, TypeError, sqlite3.Error):
                    raise ImportStoreError("IMPORT_STORAGE_FAILED") from None

    def _cancel_uploading(self, import_id) -> ImportSnapshot:
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
