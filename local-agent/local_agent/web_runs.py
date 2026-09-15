"""Loopback web orchestration for one-shot runs and durable sessions."""

import copy
import json
import os
from pathlib import Path
import threading
import time
import uuid

from .files import ReadFile
from .discovery import DirectoryTools
from .approvals import ApprovalBroker, ApprovalError, RunControl
from .file_tools import adapt_tools
from .provider import ProviderError
from .runtime import Runtime
from .session_store import SessionStore, StoreError
from .sessions import RunSubmission, SessionError, SessionScope, SessionService
from .trace import Trace


class WebError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


def _session_web_error(error: SessionError | StoreError) -> WebError:
    code = error.code
    status = {
        "NOT_FOUND": 404,
        "SESSION_REQUEST_CONFLICT": 409,
        "SESSION_ARCHIVED": 409,
        "RUN_NOT_INTERRUPTED": 409,
        "WORKSPACE_CHANGED": 409,
        "WORKSPACE_NOT_FOUND": 409,
        "IMPORT_INTEGRITY_ERROR": 409,
        "IMPORT_UNAVAILABLE": 409,
        "IMPORT_OUTPUT_UNSUPPORTED": 409,
        "STATE_IN_USE": 409,
        "CURSOR_INVALID": 400,
    }.get(code, 503 if code.startswith(("SESSION_STORE", "STATE_")) else 400)
    public_code = "WORKSPACE_UNAVAILABLE" if code in {
        "WORKSPACE_CHANGED", "WORKSPACE_NOT_FOUND"
    } else code
    return WebError(status, public_code)


class LiveTrace(Trace):
    def __init__(self, directory, workspace, publish, *, run_id=None):
        super().__init__(directory, workspace, run_id=run_id)
        self.publish = publish

    def emit(self, event, data):
        super().emit(event, data)
        detail = {key: data[key] for key in (
            'step', 'intent', 'id', 'approval_id', 'call_id', 'name', 'path', 'code', 'stop_reason',
            'action_summary', 'source', 'risk', 'bytes', 'operation', 'decision',
            'created_at', 'expires_at', 'elapsed_seconds') if key in data}
        if event == 'tool.completed':
            result = data['result']
            detail.update(ok=result.get('ok', False), code=result.get('error', {}).get('code'))
        self.publish({'event': event, 'detail': detail})


from .agents import AgentCatalog, AgentDefinition, AgentError, builtin_agent
from .agent_runtime import CapabilityStore, assemble
from .skills import SkillError
from .provider import DeepSeekProvider


class WebRuns:
    def __init__(self, workspace: Path | None, directory: Path, provider_factory,
                 *, state_dir: Path | None = None, selected_session_id: str | None = None):
        self.directory = Path(directory).resolve()
        self.provider_factory = provider_factory
        self.lock = threading.RLock()
        self.jobs = {}  # Compatibility-only one-shot history.
        self.session_jobs = {}  # Durable sessions keep only in-flight controls here.
        self.closed = False
        self._store_closed = False

        initial_workspace = None
        initial_identity = None
        state_path = Path(state_dir or self.directory / 'session-state').resolve()
        if selected_session_id is None:
            if workspace is None:
                raise ValueError('A workspace or session is required')
            initial_workspace = Path(workspace).resolve(strict=True)
            info = os.stat(initial_workspace)
            if not initial_workspace.is_dir():
                raise ValueError('Workspace must be a directory')
            initial_identity = (info.st_dev, info.st_ino)
            if (self.directory == initial_workspace
                    or self.directory.is_relative_to(initial_workspace)
                    or state_path == initial_workspace
                    or state_path.is_relative_to(initial_workspace)):
                raise ValueError('Logs and session state must be outside the readable workspace')

        self.agent_catalog = AgentCatalog(state_path / "agents")
        self.capabilities = CapabilityStore(state_path)
        self.store = SessionStore.open(state_path)
        try:
            self.store.recover_interrupted(uuid.uuid4().hex)
            self.service = SessionService(self.store)
            self.service.recover_imports()
            self.selected_session_id = selected_session_id
            if selected_session_id is not None:
                selected = self.service.load(selected_session_id)
                self.workspace = self.service.trace_root(selected, task_type='conversation')
                self.workspace_identity = (selected.workspace_device, selected.workspace_inode)
            else:
                self.workspace = initial_workspace
                self.workspace_identity = initial_identity
            if self.directory == self.workspace or self.directory.is_relative_to(self.workspace):
                raise ValueError('Logs must be outside the readable workspace')
        except Exception:
            self.store.close()
            raise

    def _workspace_available(self) -> bool:
        try:
            if self.selected_session_id is not None:
                selected = self.service.load(self.selected_session_id)
                if selected.import_id is not None:
                    self.service.resolver.resolve(selected)
                    return True
            resolved = self.workspace.resolve(strict=True)
            info = os.stat(resolved)
            return (resolved == self.workspace and resolved.is_dir()
                    and (info.st_dev, info.st_ino) == self.workspace_identity)
        except (OSError, RuntimeError, ValueError, SessionError):
            return False

    def _visible(self, record) -> bool:
        if self.selected_session_id is not None and record.id != self.selected_session_id:
            return False
        if record.import_id is not None:
            try:
                self.service.resolver.resolve(record)
                return True
            except SessionError as error:
                if not error.code.startswith('IMPORT_'):
                    raise
                return False
        return (record.workspace_path == str(self.workspace)
                and (record.workspace_device, record.workspace_inode) == self.workspace_identity)

    def _require_session(self, session_id):
        try:
            record = self.service.load(session_id)
        except (SessionError, StoreError) as error:
            raise _session_web_error(error) from None
        history = (record.import_id is not None and self.service.import_history_bound(record)
                   and (self.selected_session_id is None or record.id == self.selected_session_id))
        if not history and not self._visible(record):
            raise WebError(404, 'NOT_FOUND')
        return record

    @staticmethod
    def _session_data(record):
        scope = {"mode": record.scope.mode}
        if record.scope.mode == "file":
            scope["file"] = record.scope.target_path
        return {"id": record.id, "title": record.title, "scope": scope,
                "status": record.status, "revision": record.revision,
                "created_at": record.created_at, "updated_at": record.updated_at,
                "agent_id": record.agent_id, "agent_revision": record.agent_revision,
                "agent_snapshot": json.loads(json.dumps(record.agent_snapshot))}

    @staticmethod
    def _run_data(record):
        return {"id": record.id, "session_id": record.session_id,
                "client_request_id": record.client_request_id,
                "task_type": record.submission.task_type,
                "question": record.submission.question,
                "output_file": record.submission.output_path,
                "parent_run_id": record.submission.parent_run_id,
                "state": record.state, "phase": record.phase,
                "stop_reason": record.stop_reason,
                "started_at": record.started_at, "finished_at": record.finished_at}

    def require_agent_session(self, session_id, agent_id):
        record = self._require_session(session_id)
        if agent_id and record.agent_id != agent_id:
            raise WebError(404, 'NOT_FOUND')
        return record

    def list_agents(self):
        return {'agents': self.agent_catalog.list()}

    def save_agent(self, document):
        try:
            agent = self.agent_catalog.save(document)
            return {'agent': {**agent.to_dict(), 'revision': agent.revision,
                              'enabled': self.agent_catalog.is_enabled(agent.id)}}
        except AgentError as error:
            raise WebError(400, error.code) from None

    def set_agent_enabled(self, agent_id, enabled):
        try:
            self.agent_catalog.set_enabled(agent_id, enabled)
            if not enabled:
                with self.lock:
                    for job in list(self.session_jobs.values()) + list(self.jobs.values()):
                        job_agent = (self.service.load(job['session_id']).agent_id if job.get('session_id')
                                     else job.get('agent_snapshot', {}).get('id'))
                        if job_agent == agent_id:
                            job['control'].cancel_run()
            return self.list_agents()
        except AgentError as error:
            raise WebError(400, error.code) from None

    def _agent_provider(self, session):
        agent = AgentDefinition.from_dict(dict(session.agent_snapshot))
        if not self.agent_catalog.is_enabled(agent.id):
            raise AgentError('AGENT_DISABLED')
        if self.provider_factory == DeepSeekProvider.from_env:
            return agent.provider()
        return self.provider_factory()

    def list_capabilities(self, agent_id=None):
        try:
            result = self.capabilities.list()
            if agent_id:
                agent = self.agent_catalog.get(agent_id)
                _, statuses = self.capabilities.skills.resolve(agent.to_dict()['skills'], agent.tools)
                result['agent_statuses'] = statuses
            return result
        except (AgentError, SkillError) as error:
            raise WebError(400, error.code) from None

    def bind_capability(self, agent_id, data):
        if not isinstance(data, dict) or set(data) != {'kind', 'id', 'version'}:
            raise WebError(400, 'INVALID_REQUEST')
        try:
            agent = self.capabilities.bind(self.agent_catalog, agent_id, data['kind'], data['id'], data['version'])
            return {'agent': {**agent.to_dict(), 'revision': agent.revision,
                              'enabled': self.agent_catalog.is_enabled(agent.id)}}
        except (AgentError, SkillError) as error:
            raise WebError(400, error.code) from None

    def capability_action(self, kind, capability_id, action, data):
        try:
            if not isinstance(data, dict):
                raise WebError(400, 'INVALID_REQUEST')
            if action == 'install' and set(data) == {'path'} and isinstance(data['path'], str):
                path = Path(data['path']).expanduser()
                if kind == 'skill':
                    return {'skill': self.capabilities.skills.install(path)}
                if path.stat().st_size > 16384:
                    raise WebError(413, 'REQUEST_TOO_LARGE')
                return {'server': self.capabilities.install_server(json.loads(path.read_text(encoding='utf-8')))}
            if action == 'probe' and kind == 'mcp' and data == {}:
                return self.capabilities.probe(capability_id)
            if action == 'enabled' and set(data) == {'enabled'} and type(data['enabled']) is bool:
                result = (self.capabilities.skills.set_enabled(capability_id, data['enabled']) if kind == 'skill'
                          else self.capabilities.set_server_enabled(capability_id, data['enabled']))
                if not data['enabled']:
                    binding_key = 'skills' if kind == 'skill' else 'mcp'
                    with self.lock:
                        for job in list(self.session_jobs.values()) + list(self.jobs.values()):
                            snapshot = (self.service.load(job['session_id']).agent_snapshot if job.get('session_id')
                                        else job.get('agent_snapshot', {}))
                            if any(item['id'] == capability_id for item in snapshot.get(binding_key, [])):
                                job['control'].cancel_run()
                return {('skill' if kind == 'skill' else 'server'): result}
            raise WebError(400, 'INVALID_REQUEST')
        except (AgentError, SkillError) as error:
            raise WebError(400, error.code) from None
        except (OSError, ValueError, TypeError) as error:
            raise WebError(400, getattr(error, 'code', 'CAPABILITY_CONFIG_INVALID')) from None

    def config(self):
        available = self._workspace_available()
        with self.lock:
            result = {'workspace': str(self.workspace), 'latest_run_id': next(reversed(self.jobs), None),
                      'ready': False, 'provider': None, 'error': None,
                      'workspace_available': available,
                      'selected_session_id': self.selected_session_id,
                      'example_file': ('demo-note.md' if available
                                       and (self.workspace / 'demo-note.md').is_file() else None)}
        try:
            result.update(ready=True, provider=self.provider_factory().metadata)
        except ProviderError as error:
            result['error'] = error.code
        return result

    @staticmethod
    def validate(data):
        if not isinstance(data, dict):
            raise WebError(400, 'INVALID_TASK')
        fields = set(data) - {'output_file', 'agent_id', 'skill_id', 'mcp_prompt'}
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
        if any(job['result'] is None for job in self.jobs.values()) or self.session_jobs:
            raise WebError(409, 'RUN_ACTIVE')

    # Backward-compatible one-shot execution.
    def start(self, data):
        if (self.selected_session_id is not None
                and self.service.load(self.selected_session_id).import_id is not None):
            raise WebError(409, 'IMPORT_SESSION_REQUIRED')
        path, question = self.validate(data)
        output_path = data.get('output_file')
        with self.lock:
            self._check_start()
        if not self._workspace_available():
            raise WebError(400, 'WORKSPACE_CHANGED')
        try:
            agent = (self.agent_catalog.get(data['agent_id']) if data.get('agent_id')
                     else self.agent_catalog.get(builtin_agent('directory' if path is None else 'file').id))
            provider = agent.provider() if self.provider_factory == DeepSeekProvider.from_env else self.provider_factory()
        except (ProviderError, AgentError) as error:
            raise WebError(503, error.code) from None
        trace = LiveTrace(self.directory, self.workspace,
                          lambda event: self._publish(trace.run_id, event))
        cancel = threading.Event()
        control = RunControl(cancel)
        approvals = ApprovalBroker(trace.run_id, publish=trace.emit)
        try:
            assembly = assemble(agent, self.workspace, path, output_path, self.capabilities,
                run_id=trace.run_id, control=control, agent_catalog=self.agent_catalog,
                selected_skill=data.get('skill_id'), selected_prompt=data.get('mcp_prompt'))
        except Exception as error:
            raise WebError(400, getattr(error, 'code', 'CAPABILITY_UNAVAILABLE')) from None
        engine = assembly.engine
        runtime = Runtime(provider, engine, trace, approvals=approvals, control=control)
        job = {'id': trace.run_id, 'file': path, 'mode': 'directory' if path is None else 'file',
               'question': question, 'output_file': output_path, 'state': 'running',
               'started': time.monotonic(), 'events': [], 'result': None,
               'cancel': cancel, 'control': control, 'approvals': approvals,
               'cancelling': False, 'revision': 0, 'thread': None,
               'assembly': assembly, 'agent_snapshot': agent.to_dict()}
        with self.lock:
            try:
                self._check_start()
            except WebError:
                assembly.close()
                raise
            self.jobs[job['id']] = job
            while len(self.jobs) > 20:
                del self.jobs[next(iter(self.jobs))]
        thread = threading.Thread(target=self._execute, args=(runtime, job), daemon=True)
        job['thread'] = thread
        thread.start()
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
        finally:
            job['assembly'].close()
        with self.lock:
            job.update(result=result, state=result['state'], cancelling=False)
            job['revision'] += 1

    def snapshot(self, run_id):
        with self.lock:
            if run_id not in self.jobs:
                raise WebError(404, 'RUN_NOT_FOUND')
            job = self.jobs[run_id]
            data = copy.deepcopy({key: value for key, value in job.items()
                                  if key not in {'cancel', 'started', 'control', 'approvals', 'thread', 'assembly'}})
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

    # Durable session API.
    def list_sessions(self, *, archived=False, cursor=None, agent_id=None):
        try:
            if self.selected_session_id is not None:
                record = self._require_session(self.selected_session_id)
                status = 'archived' if archived else 'active'
                items = [record] if (record.status == status
                    and (record.import_id is None or self._visible(record))
                    and (agent_id is None or record.agent_id == agent_id)) else []
                return {"sessions": [self._session_data(item) for item in items],
                        "next_cursor": None}
            page = self.service.list_bound(str(self.workspace), *self.workspace_identity,
                                           archived=archived, cursor=cursor, agent_id=agent_id,
                                           include_imports=True)
            return {"sessions": [self._session_data(item) for item in page.items],
                    "next_cursor": page.next_cursor}
        except (SessionError, StoreError) as error:
            raise _session_web_error(error) from None

    @staticmethod
    def _scope_from_web(value):
        if not isinstance(value, dict):
            raise WebError(400, 'INVALID_REQUEST')
        if set(value) == {'mode'} and value.get('mode') == 'directory':
            return SessionScope('directory', None)
        if set(value) == {'mode', 'file'} and value.get('mode') == 'file':
            path, _ = WebRuns.validate({'file': value.get('file'), 'question': 'scope-check'})
            return SessionScope('file', path)
        raise WebError(400, 'INVALID_REQUEST')

    def create_session(self, data):
        if not isinstance(data, dict) or not {'title', 'scope'} <= set(data) <= {'title', 'scope', 'agent_id'}:
            raise WebError(400, 'INVALID_REQUEST')
        if self.selected_session_id is not None:
            raise WebError(409, 'SESSION_SELECTION_FIXED')
        if not self._workspace_available():
            raise WebError(409, 'WORKSPACE_UNAVAILABLE')
        try:
            agent = self.agent_catalog.get(data['agent_id']) if data.get('agent_id') else None
            if agent and not self.agent_catalog.is_enabled(agent.id):
                raise WebError(409, 'AGENT_DISABLED')
            record = self.service.create(self.workspace, data['title'], self._scope_from_web(data['scope']),
                                         agent_snapshot=agent.to_dict() if agent else None)
        except AgentError as error:
            raise WebError(400, error.code) from None
        except (SessionError, StoreError) as error:
            raise _session_web_error(error) from None
        return {"session": self._session_data(record)}

    def session_view(self, session_id):
        return {"session": self._session_data(self._require_session(session_id))}

    def open_artifact(self, session_id, run_id, artifact_id, agent_id):
        if not isinstance(agent_id, str) or not agent_id:
            raise WebError(404, 'NOT_FOUND')
        self.require_agent_session(session_id, agent_id)
        try:
            return self.service.open_artifact(session_id, run_id, artifact_id, agent_id)
        except (SessionError, StoreError) as error:
            raise _session_web_error(error) from None

    def change_session(self, session_id, action, data):
        self._require_session(session_id)
        try:
            if action == 'rename':
                if not isinstance(data, dict) or set(data) != {'title'}:
                    raise WebError(400, 'INVALID_REQUEST')
                record = self.service.rename(session_id, data['title'])
            else:
                if data != {}:
                    raise WebError(400, 'INVALID_REQUEST')
                record = getattr(self.service, action)(session_id)
        except WebError:
            raise
        except (SessionError, StoreError) as error:
            raise _session_web_error(error) from None
        return {"session": self._session_data(record)}

    def list_session_runs(self, session_id, *, cursor=None):
        self._require_session(session_id)
        try:
            page = self.service.list_runs(session_id, cursor=cursor)
        except (SessionError, StoreError) as error:
            raise _session_web_error(error) from None
        return {"runs": [self._run_data(item) for item in page.items],
                "next_cursor": page.next_cursor}

    @staticmethod
    def _validate_session_run(data):
        required = {'client_request_id', 'task_type', 'question'}
        if (not isinstance(data, dict)
                or not required <= set(data) <= required | {'output_file', 'skill_id', 'mcp_prompt'}):
            raise WebError(400, 'INVALID_REQUEST')
        request_id, question, task_type = data['client_request_id'], data['question'], data['task_type']
        if (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
                or any(ord(char) < 32 or ord(char) == 127 for char in request_id)):
            raise WebError(400, 'INVALID_REQUEST')
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise WebError(400, 'INVALID_QUESTION')
        if task_type not in {'files', 'conversation'}:
            raise WebError(400, 'INVALID_TASK')
        output = data.get('output_file')
        if output is not None:
            WebRuns.validate_output({'output_file': output}, None)
        if task_type == 'conversation' and output is not None:
            raise WebError(400, 'INVALID_OUTPUT_FILE')
        try:
            (request_id + question + (output or '')).encode('utf-8')
        except UnicodeEncodeError:
            raise WebError(400, 'INVALID_REQUEST') from None
        return request_id, task_type, question.strip(), output

    def _start_prepared(self, session, prepared):
        if not prepared.created:
            view = self.session_snapshot(session.id, prepared.run_id)
            view['run']['idempotent_replay'] = True
            return view, False
        try:
            read_root = self.service.trace_root(session, task_type=prepared.submission.task_type)
            provider = self._agent_provider(session)
        except (ProviderError, AgentError, SessionError) as error:
            result = {'run_id': prepared.run_id, 'state': 'failed',
                      'stop_reason': error.code, 'model_calls': 0, 'answer': None,
                      'provider': None, 'trace_path': ''}
            prepared.journal.finish_run(result)
            return self.session_snapshot(session.id, prepared.run_id), True
        try:
            trace = LiveTrace(self.directory, read_root,
                lambda event: self._publish_session(session.id, prepared.run_id, event),
                run_id=prepared.run_id)
        except (OSError, ValueError):
            result = {'run_id': prepared.run_id, 'state': 'failed',
                      'stop_reason': 'TRACE_ERROR', 'model_calls': 0, 'answer': None,
                      'provider': provider.metadata, 'trace_path': ''}
            prepared.journal.finish_run(result)
            return self.session_snapshot(session.id, prepared.run_id), True
        cancel = threading.Event()
        control = RunControl(cancel)
        approvals = ApprovalBroker(prepared.run_id, publish=trace.emit, journal=prepared.journal)
        job = {'id': prepared.run_id, 'session_id': session.id,
               'client_request_id': prepared.submission.client_request_id,
               'task_type': prepared.submission.task_type, 'question': prepared.submission.question,
               'output_file': prepared.submission.output_path,
               'parent_run_id': prepared.submission.parent_run_id,
               'state': 'running', 'phase': 'submitted', 'stop_reason': None,
               'started_at': time.time(), 'finished_at': None,
               'started': time.monotonic(), 'events': [], 'result': None,
               'cancel': cancel, 'control': control, 'approvals': approvals,
               'cancelling': False, 'revision': 0, 'thread': None,
               'prepared': prepared, 'provider': provider, 'trace': trace}
        key = (session.id, prepared.run_id)
        self.session_jobs[key] = job
        thread = threading.Thread(target=self._execute_session, args=(key, job), daemon=True)
        job['thread'] = thread
        thread.start()
        return self.session_snapshot(session.id, prepared.run_id), True

    def start_session_run(self, session_id, data):
        session = self._require_session(session_id)
        request_id, task_type, question, output = self._validate_session_run(data)
        submission = RunSubmission(request_id, question, task_type, session.scope, output, None, {},
                                   skill_id=data.get('skill_id'), mcp_prompt=data.get('mcp_prompt'))
        with self.lock:
            self._check_start()
            try:
                prepared = self.service.submit(session_id, submission)
            except (SessionError, StoreError) as error:
                raise _session_web_error(error) from None
            return self._start_prepared(session, prepared)

    def continue_session(self, session_id, data):
        session = self._require_session(session_id)
        if not isinstance(data, dict) or set(data) != {'run_id', 'client_request_id'}:
            raise WebError(400, 'INVALID_REQUEST')
        with self.lock:
            self._check_start()
            try:
                prepared = self.service.continue_interrupted(
                    session_id, data['run_id'], data['client_request_id'])
            except (SessionError, StoreError) as error:
                raise _session_web_error(error) from None
            return self._start_prepared(session, prepared)

    def _publish_session(self, session_id, run_id, event):
        with self.lock:
            job = self.session_jobs.get((session_id, run_id))
            if job is None:
                return
            event['elapsed'] = round(time.monotonic() - job['started'], 2)
            job['events'].append(event)
            job['revision'] += 1

    def _execute_session(self, key, job):
        try:
            result = self.service.execute(job['prepared'], job['provider'], job['trace'],
                                          approvals=job['approvals'], control=job['control'])
        except Exception:
            result = {'run_id': job['id'], 'state': 'failed',
                      'stop_reason': 'INTERNAL_ERROR', 'answer': None}
            try:
                job['prepared'].journal.finish_run(result)
            except Exception:
                pass
        finally:
            job['prepared'].journal.close_thread_connection()
        with self.lock:
            job.update(result=result, state=result['state'],
                       stop_reason=result.get('stop_reason'), finished_at=time.time(), cancelling=False)
            job['revision'] += 1
            self.session_jobs.pop(key, None)

    def session_snapshot(self, session_id, run_id):
        self._require_session(session_id)
        key = (session_id, run_id)
        with self.lock:
            job = self.session_jobs.get(key)
            if job is not None:
                data = copy.deepcopy({name: value for name, value in job.items()
                    if name not in {'cancel', 'control', 'approvals', 'thread', 'started',
                                    'prepared', 'provider', 'trace'}})
                pending = job['approvals'].snapshot()
                data['pending_approval'] = pending if data['result'] is None else None
                if data['pending_approval'] is not None:
                    data['state'] = 'waiting_approval'
                return {"run": data}
        try:
            data = self.service.run_view(session_id, run_id)
        except (SessionError, StoreError) as error:
            raise _session_web_error(error) from None
        data.update(pending_approval=None, events=[], cancelling=False)
        return {"run": data}

    def decide_session(self, session_id, run_id, approval_id, decision):
        self._require_session(session_id)
        try:
            self.service.load_run(session_id, run_id)
        except (SessionError, StoreError) as error:
            raise _session_web_error(error) from None
        with self.lock:
            job = self.session_jobs.get((session_id, run_id))
            if job is None:
                raise WebError(409, 'APPROVAL_UNAVAILABLE')
            approvals = job['approvals']
        try:
            approvals.decide(approval_id, decision)
        except ApprovalError as error:
            status = {'APPROVAL_INVALID_DECISION': 400,
                      'APPROVAL_NOT_FOUND': 404}.get(error.code, 409)
            raise WebError(status, error.code) from None
        return self.session_snapshot(session_id, run_id)

    def cancel_session(self, session_id, run_id):
        self._require_session(session_id)
        try:
            self.service.load_run(session_id, run_id)
        except (SessionError, StoreError) as error:
            raise _session_web_error(error) from None
        with self.lock:
            job = self.session_jobs.get((session_id, run_id))
            if job is None:
                return self.session_snapshot(session_id, run_id)
            job['cancelling'] = True
            job['revision'] += 1
        job['control'].cancel_run()
        job['approvals'].close()
        return self.session_snapshot(session_id, run_id)

    def close_thread_connection(self):
        if not self._store_closed:
            self.store.close_thread_connection()

    def close(self):
        with self.lock:
            if self._store_closed:
                return
            self.closed = True
            jobs = tuple(self.jobs.values()) + tuple(self.session_jobs.values())
        for job in jobs:
            job['control'].cancel_run()
            job['approvals'].close()
        for job in jobs:
            thread = job.get('thread')
            if thread is not None and thread is not threading.current_thread():
                thread.join(2)
        self.store.close_thread_connection()
        self.store.close()
        self._store_closed = True
