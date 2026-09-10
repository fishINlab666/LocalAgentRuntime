"""In-process web tasks; the existing Runtime remains the only execution engine."""

import copy
from pathlib import Path
import threading
import time

from .files import ReadFile
from .discovery import DirectoryTools
from .approvals import ApprovalBroker, ApprovalError, RunControl
from .file_tools import adapt_tools
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
        # The page may show intent; Trace.emit already redacted it on disk. Never expose full model messages.
        detail = {key: data[key] for key in (
            'step', 'intent', 'id', 'approval_id', 'call_id', 'name', 'path', 'code', 'stop_reason',
            'action_summary', 'source', 'risk', 'bytes', 'operation', 'decision',
            'created_at', 'expires_at', 'elapsed_seconds') if key in data}
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
        fields = set(data) - {'output_file'}
        if fields == {'mode', 'question'} and data['mode'] == 'directory':
            path = None
        elif fields == {'file', 'question'}:
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
        WebRuns.validate_output(data, path)
        return path, question.strip()

    @staticmethod
    def validate_output(data, input_path):
        if 'output_file' not in data:
            return None
        path = data['output_file']
        if (not isinstance(path, str) or not 1 <= len(path) <= 1024 or '\\' in path
                or any(ord(char) < 32 for char in path)
                or any(not part or part.startswith('.') for part in path.split('/'))
                or Path(path).suffix not in {'.md', '.txt'} or path == input_path):
            raise WebError(400, 'INVALID_OUTPUT_FILE')
        try:
            path.encode('utf-8')
        except UnicodeEncodeError:
            raise WebError(400, 'INVALID_OUTPUT_FILE') from None
        return path

    def _check_start(self):
        if self.closed:
            raise WebError(503, 'SERVER_STOPPING')
        if any(job['result'] is None for job in self.jobs.values()):
            raise WebError(409, 'RUN_ACTIVE')

    def start(self, data):
        path, question = self.validate(data)
        output_path = data.get('output_file')
        with self.lock:
            self._check_start()
        try:
            reader = DirectoryTools(self.workspace) if path is None else ReadFile(self.workspace, {path})
        except (OSError, RuntimeError, ValueError):
            raise WebError(400, 'WORKSPACE_CHANGED') from None
        if (reader.workspace != self.workspace
                or reader.workspace_identity != self.workspace_identity):
            raise WebError(400, 'WORKSPACE_CHANGED')
        engine = adapt_tools(reader, output_path=output_path)
        try:
            provider = self.provider_factory()
        except ProviderError as error:
            raise WebError(503, error.code) from None
        trace = LiveTrace(self.directory, self.workspace,
                          lambda event: self._publish(trace.run_id, event))
        cancel = threading.Event()
        control = RunControl(cancel)
        approvals = ApprovalBroker(trace.run_id, publish=trace.emit)
        runtime = Runtime(provider, engine, trace, approvals=approvals, control=control)
        job = {'id': trace.run_id, 'file': path, 'mode': 'directory' if path is None else 'file',
               'question': question, 'output_file': output_path, 'state': 'running',
               'started': time.monotonic(), 'events': [], 'result': None,
               'cancel': cancel, 'control': control, 'approvals': approvals,
               'cancelling': False, 'revision': 0}
        with self.lock:
            self._check_start()
            self.jobs[job['id']] = job
            while len(self.jobs) > 20:
                del self.jobs[next(iter(self.jobs))]
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
            job = self.jobs[run_id]
            data = copy.deepcopy({key: value for key, value in job.items()
                                  if key not in {'cancel', 'started', 'control', 'approvals'}})
            approvals = job['approvals']
        pending = approvals.snapshot()
        data['pending_approval'] = pending if data['result'] is None else None
        if data['pending_approval'] is not None:
            data['state'] = 'waiting_approval'
        return data

    def decide(self, run_id, approval_id, decision):
        with self.lock:
            if run_id not in self.jobs:
                raise WebError(404, 'RUN_NOT_FOUND')
            approvals = self.jobs[run_id]['approvals']
        try:
            approvals.decide(approval_id, decision)
        except ApprovalError as error:
            status = {'APPROVAL_INVALID_DECISION': 400, 'APPROVAL_NOT_FOUND': 404}.get(error.code, 409)
            raise WebError(status, error.code) from None
        return self.snapshot(run_id)

    def cancel(self, run_id):
        with self.lock:
            if run_id not in self.jobs:
                raise WebError(404, 'RUN_NOT_FOUND')
            job = self.jobs[run_id]
            active = job['result'] is None
            if active:
                job['cancelling'] = True
                job['revision'] += 1
        if active:
            job['control'].cancel_run()
            job['approvals'].close()
        return self.snapshot(run_id)

    def close(self):
        with self.lock:
            self.closed = True
            jobs = tuple(self.jobs.values())
        for job in jobs:
            job['control'].cancel_run()
            job['approvals'].close()
