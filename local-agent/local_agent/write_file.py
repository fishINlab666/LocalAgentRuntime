"""One approved, bounded UTF-8 file creation in a pinned workspace."""

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
from uuid import uuid4

from .files import WorkspaceChanged, _open_directory, _path_parts, _workspace_identity
from .tool_runtime import ToolSpec


_MAX_BYTES = 32768
_CLEANUP_WARNING = 'Temporary output cleanup could not be completed.'
_ERROR_CODES = frozenset({
    'INVALID_ARGUMENT', 'PATH_DENIED', 'UNSUPPORTED_FILE', 'FILE_TOO_LARGE',
    'FILE_EXISTS', 'DIRECTORY_NOT_FOUND', 'OS_PERMISSION_DENIED', 'WORKSPACE_CHANGED',
    'DISK_FULL', 'WRITE_ERROR', 'WRITE_OUTCOME_UNKNOWN', 'OUTPUT_LIMIT',
})
_MESSAGES = {
    'INVALID_ARGUMENT': 'Expected exactly the string fields path and content.',
    'PATH_DENIED': 'Only the output path specified for this task may be created.',
    'UNSUPPORTED_FILE': 'Only UTF-8 text in .md and .txt files is supported.',
    'FILE_TOO_LARGE': 'The output exceeds the 32 KiB limit.',
    'FILE_EXISTS': 'The output target already exists and will not be overwritten.',
    'DIRECTORY_NOT_FOUND': 'The output parent directory does not exist.',
    'OS_PERMISSION_DENIED': 'The operating system denied access to the output directory.',
    'WORKSPACE_CHANGED': 'The workspace or output directory changed; start a new task.',
    'DISK_FULL': 'There is not enough storage space to create the output.',
    'WRITE_ERROR': 'The output could not be created.',
    'WRITE_OUTCOME_UNKNOWN': 'Publication could not be confirmed; inspect the target before retrying.',
    'OUTPUT_LIMIT': 'This task has already created its one permitted output.',
}


def _error(code: str) -> dict:
    error = {'code': code, 'message': _MESSAGES[code]}
    if code in {'FILE_EXISTS', 'DIRECTORY_NOT_FOUND'}:
        error['owner'] = 'user'
    return {'ok': False, 'error': error}


def _os_error(error: OSError, *, publishing: bool = False) -> dict:
    if isinstance(error, WorkspaceChanged):
        code = 'WORKSPACE_CHANGED'
    elif error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        code = 'OS_PERMISSION_DENIED'
    elif error.errno in {errno.ENOSPC, errno.EDQUOT}:
        code = 'DISK_FULL'
    elif error.errno == errno.EEXIST:
        code = 'FILE_EXISTS'
    elif error.errno == errno.ENOENT:
        code = 'DIRECTORY_NOT_FOUND'
    elif error.errno in {errno.ELOOP, errno.ENOTDIR}:
        code = 'PATH_DENIED'
    elif publishing:
        code = 'WRITE_OUTCOME_UNKNOWN'
    else:
        code = 'WRITE_ERROR'
    return _error(code)


@dataclass(frozen=True)
class WriteCandidate:
    path: str
    content: bytes


class WriteFile:
    def __init__(self, workspace: Path, output_path: str,
                 workspace_identity: tuple[int, int] | None = None):
        self.workspace = Path(workspace).resolve()
        self.workspace_identity = (workspace_identity if workspace_identity is not None
                                   else _workspace_identity(self.workspace))
        self.output_path = output_path
        self._receipt = None
        self.spec = ToolSpec(
            name='write_file',
            description='Create the user-specified report after approval; never overwrite a file.',
            input_schema={
                'type': 'object',
                'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                'required': ['path', 'content'], 'additionalProperties': False,
            },
            risk='medium', source='builtin', timeout_seconds=5, max_result_bytes=65536,
            error_codes=_ERROR_CODES,
            user_error_codes=frozenset({'FILE_EXISTS', 'DIRECTORY_NOT_FOUND'}),
        )

    def verify_success(self, arguments: dict, data: dict) -> dict:
        required = {'path', 'bytes', 'sha256', 'operation'}
        if (not isinstance(arguments, dict) or set(arguments) != {'path', 'content'}
                or not required <= set(data) <= required | {'cleanup_warning'}
                or data.get('path') != arguments.get('path')
                or data.get('path') != self.output_path
                or data.get('operation') != 'created'
                or type(data.get('bytes')) is not int):
            raise ValueError('invalid write receipt')
        raw = arguments['content'].encode('utf-8')
        if (data['bytes'] != len(raw)
                or data.get('sha256') != hashlib.sha256(raw).hexdigest()
                or ('cleanup_warning' in data and data['cleanup_warning'] != _CLEANUP_WARNING)):
            raise ValueError('write receipt does not match approved content')
        core = {key: data[key] for key in required}
        stored = ({key: value for key, value in self._receipt.items() if key != 'ok'}
                  if self._receipt is not None else None)
        if stored is not None and core != stored:
            raise ValueError('write receipt does not match published receipt')
        return dict(data)

    def verify_preview(self, arguments: dict, preview: dict) -> dict:
        candidate = self._prepare(arguments)
        if isinstance(candidate, dict):
            raise ValueError('invalid write preview arguments')
        expected = {
            'action_summary': f'新建 {candidate.path}，{len(candidate.content)} 字节',
            'path': candidate.path, 'bytes': len(candidate.content),
            'operation': 'created', 'content': candidate.content.decode('utf-8'),
        }
        if preview != expected:
            raise ValueError('write preview does not match arguments')
        return dict(preview)

    def _prepare(self, arguments: dict) -> WriteCandidate | dict:
        if (not isinstance(arguments, dict) or set(arguments) != {'path', 'content'}
                or not isinstance(arguments['path'], str)
                or not isinstance(arguments['content'], str)):
            return _error('INVALID_ARGUMENT')
        path, content = arguments['path'], arguments['content']
        if path != self.output_path or _path_parts(path) is None:
            return _error('PATH_DENIED')
        if Path(path).suffix not in {'.md', '.txt'}:
            return _error('UNSUPPORTED_FILE')
        try:
            raw = content.encode('utf-8')
        except UnicodeEncodeError:
            return _error('UNSUPPORTED_FILE')
        if len(raw) > _MAX_BYTES:
            return _error('FILE_TOO_LARGE')
        if any((ord(char) < 32 and char not in '\t\r\n') or ord(char) == 127 for char in content):
            return _error('UNSUPPORTED_FILE')
        if self._receipt is not None:
            return _error('OUTPUT_LIMIT')
        return WriteCandidate(path, raw)

    def _open_parent(self, parts: list[str]) -> int:
        try:
            root_fd = _open_directory(self.workspace, [], self.workspace_identity)
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                raise WorkspaceChanged(errno.ESTALE, 'Workspace root changed.') from error
            raise
        os.close(root_fd)
        return _open_directory(self.workspace, parts[:-1], self.workspace_identity)

    @staticmethod
    def _target_absent(directory_fd: int, filename: str) -> bool:
        try:
            os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False

    def validate(self, arguments: dict) -> dict | None:
        candidate = self._prepare(arguments)
        if isinstance(candidate, dict):
            return candidate
        parts = _path_parts(candidate.path)
        try:
            directory_fd = self._open_parent(parts)
            try:
                if not self._target_absent(directory_fd, parts[-1]):
                    return _error('FILE_EXISTS')
            finally:
                os.close(directory_fd)
        except OSError as error:
            return _os_error(error)
        return None

    def preview(self, arguments: dict) -> dict:
        candidate = self._prepare(arguments)
        if isinstance(candidate, dict):
            return candidate
        return {'action_summary': f'新建 {candidate.path}，{len(candidate.content)} 字节',
                'path': candidate.path, 'bytes': len(candidate.content),
                'operation': 'created', 'content': candidate.content.decode('utf-8')}

    def execute(self, arguments: dict) -> WriteCandidate | dict:
        return self._prepare(arguments)

    def commit(self, arguments: dict, candidate: WriteCandidate, *, check=None, publish=None) -> dict:
        prepared = self._prepare(arguments)
        if isinstance(prepared, dict):
            return prepared
        if not isinstance(candidate, WriteCandidate) or candidate != prepared:
            return _error('INVALID_ARGUMENT')
        parts = _path_parts(candidate.path)
        directory_fd = temporary_fd = None
        temporary_name = None
        temporary_created = False
        result = None
        publishing = False
        try:
            if check is not None:
                check()
            directory_fd = self._open_parent(parts)
            if not self._target_absent(directory_fd, parts[-1]):
                return _error('FILE_EXISTS')
            temporary_name = f'.local-agent-{uuid4().hex}.tmp'
            try:
                temporary_fd = os.open(
                    temporary_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600, dir_fd=directory_fd)
            except FileExistsError:
                return _error('WRITE_ERROR')
            temporary_created = True
            written = 0
            while written < len(candidate.content):
                if check is not None:
                    check()
                count = os.write(temporary_fd, candidate.content[written:written + 8192])
                if count <= 0:
                    raise OSError(errno.EIO, 'Output write made no progress.')
                written += count
            os.fsync(temporary_fd)
            os.lseek(temporary_fd, 0, os.SEEK_SET)
            chunks = []
            remaining = _MAX_BYTES + 1
            while remaining:
                if check is not None:
                    check()
                chunk = os.read(temporary_fd, min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            actual = b''.join(chunks)
            info = os.fstat(temporary_fd)
            if (actual != candidate.content or info.st_size != len(actual)
                    or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                raise OSError(errno.EIO, 'Output verification failed.')
            receipt = {'ok': True, 'path': candidate.path, 'bytes': len(actual),
                       'sha256': hashlib.sha256(actual).hexdigest(), 'operation': 'created'}
            current_fd = self._open_parent(parts)
            try:
                current, opened = os.fstat(current_fd), os.fstat(directory_fd)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise WorkspaceChanged(errno.ESTALE, 'Output directory changed.')
            finally:
                os.close(current_fd)
            source = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
            if ((source.st_dev, source.st_ino) != (info.st_dev, info.st_ino)
                    or not stat.S_ISREG(source.st_mode) or source.st_nlink != 1):
                raise WorkspaceChanged(errno.ESTALE, 'Temporary output changed.')
            if check is not None:
                check()

            def publish_once():
                nonlocal publishing, result
                publishing = True
                os.link(temporary_name, parts[-1], src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd, follow_symlinks=False)
                # Publication is final: cancellation and cleanup must not erase this receipt.
                self._receipt = receipt
                result = dict(receipt)
                return result

            result = publish(publish_once) if publish is not None else publish_once()
        except OSError as error:
            result = _os_error(error, publishing=publishing)
        finally:
            cleanup_failed = False
            if temporary_fd is not None:
                try:
                    os.close(temporary_fd)
                except OSError:
                    cleanup_failed = True
            if directory_fd is not None:
                try:
                    if temporary_created:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_failed = True
                try:
                    os.close(directory_fd)
                except OSError:
                    cleanup_failed = True
            if cleanup_failed and result is not None:
                result['cleanup_warning'] = _CLEANUP_WARNING
        return result
