"""Per-run discovery authority, recorded only after a tool result is accepted."""

from pathlib import Path

from .files import ListFiles, ReadFile, _path_parts, _valid_arguments


READ_FILE_SCHEMA = {"type": "function", "function": {
    "name": "read_file", "description": "读取本任务允许的工作区相对路径的 UTF-8 .md 或 .txt 文件。",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"], "additionalProperties": False}}}

LIST_FILES_SCHEMA = {"type": "function", "function": {
    "name": "list_files", "description": "列出已发现目录的单层子目录及 .md/.txt 文件；根目录用 .。只返回路径和文件大小。",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"], "additionalProperties": False}}}

_MESSAGES = {
    "INVALID_ARGUMENT": "Expected exactly one string field named path.",
    "PATH_DENIED": "The requested path is not permitted.",
    "PATH_NOT_DISCOVERED": "The requested path has not been discovered by listing its parent directory.",
    "FILE_COUNT_LIMIT": "The run has reached its distinct file read limit.",
    "TOOL_NOT_FOUND": "The requested tool is not available.",
}


def _error(code: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": _MESSAGES[code]}}


class DirectoryTools:
    def __init__(self, workspace: Path, max_files: int = 4):
        if type(max_files) is not int or max_files < 1:
            raise ValueError("max_files must be a positive integer")
        self._lister = ListFiles(workspace)
        self.workspace = self._lister.workspace
        self.workspace_identity = self._lister.workspace_identity
        self.schemas = [LIST_FILES_SCHEMA, READ_FILE_SCHEMA]
        self.max_files = max_files
        self._directories = {"."}
        self._files: set[str] = set()
        self._listed: set[str] = set()
        self._read: set[str] = set()
        self._ever_read: set[str] = set()
        self._attempted = False
        self._had_error = False

    def execute(self, name: str, arguments: dict) -> dict:
        if name not in {"list_files", "read_file"}:
            return _error("TOOL_NOT_FOUND")
        if not _valid_arguments(arguments):
            return _error("INVALID_ARGUMENT")
        path = arguments["path"]
        if _path_parts(path, allow_root=name == "list_files") is None:
            return _error("PATH_DENIED")
        discovered = self._directories if name == "list_files" else self._files
        if path not in discovered:
            return _error("PATH_NOT_DISCOVERED")
        if name == "list_files":
            return self._lister.execute(arguments)
        if path not in self._ever_read and len(self._ever_read) >= self.max_files:
            return _error("FILE_COUNT_LIMIT")
        try:
            reader = ReadFile(self.workspace, self._files)
        except (OSError, RuntimeError):
            return _error("PATH_DENIED")
        # A new allowlist must not re-authorize a replacement of the pinned root.
        if (reader.workspace != self.workspace
                or reader.workspace_identity != self.workspace_identity):
            return _error("PATH_DENIED")
        return reader.execute(arguments)

    def record(self, name: str, arguments: dict | None, result: dict) -> None:
        self._attempted = True
        path = (arguments.get("path") if isinstance(arguments, dict)
                and isinstance(arguments.get("path"), str) else None)
        if (not isinstance(result, dict) or result.get("ok") is not True
                or not _valid_arguments(arguments) or result.get("path") != path):
            self._record_error(name, path)
            return
        if name == "read_file" and path in self._files:
            self._read.add(path)
            self._ever_read.add(path)
            return
        if (name != "list_files" or path not in self._directories
                or result.get("complete") is not True or not isinstance(result.get("entries"), list)):
            self._record_error(name, path)
            return
        directories, files = set(), set()
        parent_parts = [] if path == "." else path.split("/")
        for entry in result["entries"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                self._record_error(name, path)
                return
            child = entry["path"]
            parts = _path_parts(child)
            if parts is None or parts[:-1] != parent_parts:
                self._record_error(name, path)
                return
            if entry.get("type") == "directory" and set(entry) == {"path", "type"}:
                directories.add(child)
            elif (entry.get("type") == "file" and set(entry) == {"path", "type", "bytes"}
                  and type(entry["bytes"]) is int and entry["bytes"] >= 0
                  and Path(child).suffix in {".md", ".txt"}):
                files.add(child)
            else:
                self._record_error(name, path)
                return
        self._listed.add(path)
        self._directories.update(directories)
        self._files.update(files)

    def _record_error(self, name: str, path: str | None) -> None:
        self._had_error = True
        if name == "read_file":
            # Re-reading a now-unavailable file invalidates its current evidence,
            # while the cumulative distinct-file budget remains consumed.
            self._read.discard(path)
        elif name == "list_files":
            self._listed.discard(path)

    def coverage(self) -> dict:
        unlisted = self._directories - self._listed
        unread = self._files - self._read
        return {"attempted": self._attempted, "had_error": self._had_error,
                "listed_directories": sorted(self._listed),
                "unlisted_directories": sorted(unlisted),
                "discovered_files": sorted(self._files), "read_files": sorted(self._read),
                "unread_files": sorted(unread),
                "complete": "." in self._listed and not unlisted and not unread}
