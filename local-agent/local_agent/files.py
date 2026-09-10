"""Bounded directory listings and allowlisted UTF-8 reads anchored to a workspace."""

import errno
import copy
import hashlib
import os
from pathlib import Path
import stat


_MESSAGES = {
    "INVALID_ARGUMENT": "Expected exactly one string field named path.",
    "PATH_DENIED": "The requested path is not permitted.",
    "FILE_NOT_FOUND": "The requested file was not found.",
    "UNSUPPORTED_FILE": "Only regular UTF-8 .md and .txt files are supported.",
    "FILE_TOO_LARGE": "The requested file exceeds the read limit.",
    "FILE_CHANGED": "The file changed while it was being read.",
    "READ_ERROR": "The file could not be read.",
    "DIRECTORY_NOT_FOUND": "The requested directory was not found.",
    "DIRECTORY_TOO_LARGE": "The requested directory exceeds the listing limit.",
    "DIRECTORY_CHANGED": "The directory changed while it was being listed.",
    "LIST_ERROR": "The directory could not be listed.",
    "OS_PERMISSION_DENIED": "The operating system denied access; check file permissions.",
    "WORKSPACE_CHANGED": "The workspace root changed; start a new task with the intended root.",
}


class WorkspaceChanged(PermissionError):
    pass


def _error(code: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": _MESSAGES[code]}}


def _metadata(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _valid_arguments(arguments: dict) -> bool:
    return (isinstance(arguments, dict) and set(arguments) == {"path"}
            and isinstance(arguments["path"], str))


def _path_parts(path: str, allow_root: bool = False) -> list[str] | None:
    if allow_root and path == ".":
        return []
    parts = path.split("/")
    if (not path or "\\" in path
            or any(not part or part.startswith(".") for part in parts)
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)):
        return None
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return parts


def _open_directory(workspace: Path, parts: list[str],
                    workspace_identity: tuple[int, int] | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd = os.open("/", flags)
    try:
        # Traverse the canonical workspace too: O_NOFOLLOW protects only one component.
        for part in workspace.parts[1:]:
            child_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        if workspace_identity is not None:
            info = os.fstat(directory_fd)
            if (info.st_dev, info.st_ino) != workspace_identity:
                raise WorkspaceChanged(errno.EACCES, "Workspace root changed.")
        for part in parts:
            child_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _workspace_identity(workspace: Path) -> tuple[int, int]:
    directory_fd = _open_directory(workspace, [])
    try:
        info = os.fstat(directory_fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(directory_fd)


class ListFiles:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.workspace_identity = _workspace_identity(self.workspace)
        self._result_proof = None

    def clear_result_proof(self) -> None:
        self._result_proof = None

    def take_result_proof(self, arguments: dict) -> dict | None:
        proof, self._result_proof = self._result_proof, None
        if proof is None or proof[0] != arguments:
            return None
        return proof[1]

    def execute(self, arguments: dict) -> dict:
        self._result_proof = None
        if not _valid_arguments(arguments):
            return _error("INVALID_ARGUMENT")
        path = arguments["path"]
        parts = _path_parts(path, allow_root=True)
        if parts is None:
            return _error("PATH_DENIED")
        try:
            directory_fd = _open_directory(self.workspace, parts, self.workspace_identity)
            try:
                before = os.fstat(directory_fd)
                entries = []
                with os.scandir(directory_fd) as directory:
                    for count, entry in enumerate(directory, 1):
                        if count > 200:
                            return _error("DIRECTORY_TOO_LARGE")
                        if _path_parts(entry.name) is None:
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            return _error("DIRECTORY_CHANGED")
                        child_path = "/".join([*parts, entry.name])
                        if stat.S_ISDIR(info.st_mode):
                            entries.append({"path": child_path, "type": "directory"})
                        elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                              and Path(entry.name).suffix in {".md", ".txt"}):
                            entries.append({"path": child_path, "type": "file", "bytes": info.st_size})
                if _metadata(before) != _metadata(os.fstat(directory_fd)):
                    return _error("DIRECTORY_CHANGED")
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if isinstance(exc, WorkspaceChanged):
                return _error("WORKSPACE_CHANGED")
            if exc.errno in {errno.EACCES, errno.EPERM}:
                return _error("OS_PERMISSION_DENIED")
            if exc.errno == errno.ENOENT:
                return _error("DIRECTORY_NOT_FOUND")
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                return _error("PATH_DENIED")
            return _error("LIST_ERROR")
        result = {"ok": True, "path": path,
                  "entries": sorted(entries, key=lambda entry: entry["path"]), "complete": True}
        self._result_proof = (copy.deepcopy(arguments), copy.deepcopy(result))
        return result


class ReadFile:
    def __init__(self, workspace: Path, allowed_paths: set[str], max_bytes: int = 32768):
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a nonnegative integer")
        self.workspace = Path(workspace).resolve()
        self.workspace_identity = _workspace_identity(self.workspace)
        self.allowed_paths = frozenset(allowed_paths)
        self.max_bytes = max_bytes
        self._result_proof = None

    def clear_result_proof(self) -> None:
        self._result_proof = None

    def take_result_proof(self, arguments: dict) -> dict | None:
        proof, self._result_proof = self._result_proof, None
        if proof is None or proof[0] != arguments:
            return None
        return proof[1]

    def _open_file(self, parts: list[str]) -> int:
        directory_fd = _open_directory(self.workspace, parts[:-1], self.workspace_identity)
        try:
            # Nonblocking prevents a FIFO from hanging before fstat can reject it.
            return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                           dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def execute(self, arguments: dict) -> dict:
        self._result_proof = None
        if not _valid_arguments(arguments):
            return _error("INVALID_ARGUMENT")
        path = arguments["path"]
        parts = _path_parts(path)
        if parts is None or path not in self.allowed_paths:
            return _error("PATH_DENIED")
        if Path(path).suffix not in {".md", ".txt"}:
            return _error("UNSUPPORTED_FILE")

        try:
            file_fd = self._open_file(parts)
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode):
                    return _error("UNSUPPORTED_FILE")
                if before.st_nlink > 1:
                    return _error("PATH_DENIED")
                if before.st_size > self.max_bytes:
                    return _error("FILE_TOO_LARGE")
                chunks = []
                remaining = self.max_bytes + 1
                while remaining:
                    chunk = os.read(file_fd, min(8192, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(file_fd)
                if _metadata(before) != _metadata(after):
                    return _error("FILE_CHANGED")
                raw = b"".join(chunks)
                if len(raw) > self.max_bytes:
                    return _error("FILE_TOO_LARGE")
                if len(raw) != after.st_size:
                    return _error("FILE_CHANGED")
            finally:
                os.close(file_fd)
        except OSError as exc:
            if isinstance(exc, WorkspaceChanged):
                return _error("WORKSPACE_CHANGED")
            if exc.errno in {errno.EACCES, errno.EPERM}:
                return _error("OS_PERMISSION_DENIED")
            if exc.errno == errno.ENOENT:
                return _error("FILE_NOT_FOUND")
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                return _error("PATH_DENIED")
            return _error("READ_ERROR")

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _error("UNSUPPORTED_FILE")
        if any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in content):
            return _error("UNSUPPORTED_FILE")
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        result = {
            "ok": True,
            "path": path,
            "content": content,
            "line_count": len(content.splitlines()),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        self._result_proof = (copy.deepcopy(arguments), copy.deepcopy(result))
        return result
