"""In-process web tasks; the existing Runtime remains the only execution engine."""

import copy
from pathlib import Path
import threading
import time

from .files import ReadFile
from .discovery import DirectoryTools
from .provider import ProviderError
from .runtime import Runtime
from .trace import Trace


class WebError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


class LiveTrace(Trace):
    def __init__(self, directory, workspace, publish):
        super().__init__(directory, workspace)
        self.publish = publish

    def emit(self, event, data):
        super().emit(event, data)
        # Publish only progress metadata, never model messages or file contents.
        detail = {key: data[key] for key in ('step', 'name', 'path', 'code', 'stop_reason') if key in data}
        if event == 'tool.completed':
            result = data['result']
            detail.update(ok=result.get('ok', False), code=result.get('error', {}).get('code'))
        self.publish({'event': event, 'detail': detail})


class WebRuns:
    def __init__(self, workspace: Path, directory: Path, provider_factory):
        root = DirectoryTools(workspace)
        self.workspace, self.workspace_identity = root.workspace, root.workspace_identity
        self.directory = Path(directory).resolve()
        if self.directory == self.workspace or self.directory.is_relative_to(self.workspace):
            raise ValueError('Logs must be outside the readable workspace')
        self.provider_factory = provider_factory
        self.lock = threading.RLock()
        self.jobs = {}
        self.closed = False

    def config(self):
        with self.lock:
            result = {'workspace': str(self.workspace), 'latest_run_id': next(reversed(self.jobs), None),
                      'ready': False, 'provider': None, 'error': None,
                      'example_file': 'demo-note.md' if (self.workspace / 'demo-note.md').is_file() else None}
        try:
            result.update(ready=True, provider=self.provider_factory().metadata)
        except ProviderError as error:
            result['error'] = error.code
        return result

    @staticmethod
    def validate(data):
        if not isinstance(data, dict):
            raise WebError(400, 'INVALID_TASK')
        if set(data) == {'mode', 'question'} and data['mode'] == 'directory':
            path = None
        elif set(data) == {'file', 'question'}:
            path = data['file']
            if (not isinstance(path, str) or not 1 <= len(path) <= 1024 or '\\' in path
                    or any(ord(char) < 32 for char in path)
                    or any(not part or part.startswith('.') for part in path.split('/'))):
                raise WebError(400, 'PATH_DENIED')
            if Path(path).suffix not in {'.md', '.txt'}:
                raise WebError(400, 'UNSUPPORTED_FILE')
        else:
            raise WebError(400, 'INVALID_TASK')
        question = data['question']
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise WebError(400, 'INVALID_QUESTION')
        try:
            ((path or '') + question).encode('utf-8')
        except UnicodeEncodeError:
            raise WebError(400, 'INVALID_TASK') from None
        return path, question.strip()

    def start(self, data):
        path, question = self.validate(data)
        with self.lock:
            if self.closed:
                raise WebError(503, 'SERVER_STOPPING')
            if any(job['result'] is None for job in self.jobs.values()):
                raise WebError(409, 'RUN_ACTIVE')
            try:
                reader = DirectoryTools(self.workspace) if path is None else ReadFile(self.workspace, {path})
            except (OSError, RuntimeError, ValueError):
                raise WebError(400, 'WORKSPACE_CHANGED') from None
            if (reader.workspace != self.workspace
                    or reader.workspace_identity != self.workspace_identity):
                raise WebError(400, 'WORKSPACE_CHANGED')
            try:
                provider = self.provider_factory()
            except ProviderError as error:
                raise WebError(503, error.code) from None
            trace = LiveTrace(self.directory, self.workspace,
                              lambda event: self._publish(trace.run_id, event))
            job = {'id': trace.run_id, 'file': path, 'mode': 'directory' if path is None else 'file',
                   'question': question, 'state': 'running',
                   'started': time.monotonic(), 'events': [], 'result': None,
                   'cancel': threading.Event(), 'cancelling': False, 'revision': 0}
            self.jobs[job['id']] = job
            while len(self.jobs) > 20:
                del self.jobs[next(iter(self.jobs))]
            runtime = Runtime(provider, reader, trace)
            threading.Thread(target=self._execute, args=(runtime, job), daemon=True).start()
            return self.snapshot(job['id'])

    def _publish(self, run_id, event):
        with self.lock:
            job = self.jobs[run_id]
            event['elapsed'] = round(time.monotonic() - job['started'], 2)
            job['events'].append(event)
            job['revision'] += 1

    def _execute(self, runtime, job):
        try:
            result = runtime.run(job['question'], job['file'], job['cancel'])
        except Exception:
            result = {'state': 'failed', 'stop_reason': 'INTERNAL_ERROR', 'answer': None}
        with self.lock:
            job.update(result=result, state=result['state'], cancelling=False)
            job['revision'] += 1

    def snapshot(self, run_id):
        with self.lock:
            if run_id not in self.jobs:
                raise WebError(404, 'RUN_NOT_FOUND')
            return copy.deepcopy({key: value for key, value in self.jobs[run_id].items()
                                  if key not in {'cancel', 'started'}})

    def cancel(self, run_id):
        with self.lock:
            self.snapshot(run_id)
            job = self.jobs[run_id]
            if job['result'] is None:
                job['cancel'].set()
                job['cancelling'] = True
                job['revision'] += 1
            return self.snapshot(run_id)

    def close(self):
        with self.lock:
            self.closed = True
            for job in self.jobs.values():
                job['cancel'].set()
