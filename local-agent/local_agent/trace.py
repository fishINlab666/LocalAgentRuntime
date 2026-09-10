"""Append-only local events, outside the workspace readable by the model."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid


def digest(value) -> dict:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def redact(value):
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ('reasoning_content', 'api_key', 'authorization'):
                continue
            result[key] = digest(item) if key in (
                'content', 'answer', 'quote', 'arguments', 'question', 'intent', 'preview') else redact(item)
        return result
    return value


class Trace:
    def __init__(self, directory: Path, workspace: Path, debug_content: bool = False,
                 *, run_id: str | None = None):
        directory, workspace = Path(directory).resolve(), Path(workspace).resolve()
        if directory == workspace or directory.is_relative_to(workspace):
            raise ValueError('Trace directory must be outside the readable workspace')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if run_id is not None and (
                not isinstance(run_id, str) or not run_id
                or len(run_id) > 128
                or any(not (char.isascii() and (char.isalnum() or char in '-_'))
                       for char in run_id)):
            raise ValueError('Run ID must be a safe nonempty identifier')
        self.run_id = run_id or uuid.uuid4().hex
        self.path = directory / (self.run_id + '.jsonl')
        fd = self._open_log(create=True)
        try:
            info = os.fstat(fd)
            self._identity = (info.st_dev, info.st_ino)
        finally:
            os.close(fd)
        self.debug_content = debug_content
        self._seq = 0

    def _open_log(self, create: bool = False) -> int:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory_fd = os.open('/', directory_flags)
        try:
            # O_NOFOLLOW alone protects only the final component, not its parents.
            for part in self.path.parts[1:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            return os.open(self.path.name, flags, 0o600, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def emit(self, event: str, data: dict):
        self._seq += 1
        row = {'run_id': self.run_id, 'seq': self._seq, 'event': event,
               'time': datetime.now(timezone.utc).isoformat(),
               'data': data if self.debug_content else redact(data)}
        encoded = json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n'
        with os.fdopen(self._open_log(), 'w', encoding='utf-8') as output:
            info = os.fstat(output.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != self._identity):
                raise OSError('The trace file was replaced or linked')
            output.write(encoded)
