"""Run-local approval waits and cooperative cancellation boundaries."""

from contextlib import contextmanager
import copy
from dataclasses import dataclass
import hashlib
import json
import threading
import time
from typing import Callable, TypeVar
import uuid


class RunStopped(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ApprovalError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class JournalFailure(Exception):
    """A durable run record could not be advanced safely."""

    def __init__(self, cause: Exception | None = None):
        self.code = 'SESSION_STORE_ERROR'
        self.cause = cause
        super().__init__(self.code)


T = TypeVar('T')


class RunControl:
    def __init__(self, cancel: threading.Event, run_timeout: float = 120,
                 approval_timeout: float = 300, clock: Callable[[], float] = time.monotonic):
        self.cancel = cancel
        self.run_timeout = run_timeout
        self.approval_timeout = approval_timeout
        self.clock = clock
        self.lock = threading.RLock()
        self._started = clock()
        self._waited = 0.0
        self._waiting_since = None
        self._suspend_depth = 0

    def remaining(self) -> float:
        with self.lock:
            active_until = self.clock() if self._waiting_since is None else self._waiting_since
            return max(0.0, self.run_timeout - (active_until - self._started - self._waited))

    def approval_remaining(self) -> float:
        with self.lock:
            current_wait = 0.0 if self._waiting_since is None else self.clock() - self._waiting_since
            return max(0.0, self.approval_timeout - self._waited - current_wait)

    def check(self) -> None:
        with self.lock:
            if self.cancel.is_set():
                raise RunStopped('CANCELLED')
            if self.remaining() <= 0:
                raise RunStopped('RUN_TIMEOUT')

    def cancel_run(self) -> None:
        with self.lock:
            self.cancel.set()

    @contextmanager
    def suspend(self):
        with self.lock:
            self.check()
            if self._suspend_depth == 0:
                self._waiting_since = self.clock()
            self._suspend_depth += 1
        try:
            yield
        finally:
            with self.lock:
                self._suspend_depth -= 1
                if self._suspend_depth == 0:
                    self._waited += self.clock() - self._waiting_since
                    self._waiting_since = None

    def commit(self, fn: Callable[[], T]) -> T:
        """Serialize final publication with cancel_run; this does not terminate workers."""
        with self.lock:
            self.check()
            return fn()


@dataclass
class _Approval:
    data: dict
    control: RunControl
    decision: str | None = None
    error: str | None = None


class ApprovalBroker:
    def __init__(self, run_id: str, publish: Callable[[str, dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, *, journal=None,
                 process_generation: str | None = None):
        self.run_id = run_id
        self.publish = publish
        self.clock = clock
        self.journal = journal
        self.process_generation = process_generation or uuid.uuid4().hex
        self._condition = threading.Condition(threading.RLock())
        self._pending: _Approval | None = None
        self._history: dict[str, _Approval] = {}
        self._closed = False

    def _emit(self, event: str, approval: _Approval) -> None:
        if self.publish is None:
            return
        # Content is available only through the live snapshot, never event history.
        keys = ('id', 'approval_id', 'run_id', 'call_id', 'name', 'action_summary',
                'risk', 'source', 'path', 'bytes', 'operation', 'created_at', 'expires_at')
        data = {key: copy.deepcopy(approval.data[key]) for key in keys if key in approval.data}
        if approval.decision is not None:
            data['decision'] = approval.decision
        if approval.error is not None:
            data['code'] = approval.error
        self.publish(event, data)

    def _check(self, approval: _Approval) -> None:
        approval.control.check()
        if self._closed:
            raise ApprovalError('APPROVAL_UNAVAILABLE')
        if approval.error:
            raise ApprovalError(approval.error)
        if approval.control.approval_remaining() <= 0:
            raise ApprovalError('APPROVAL_EXPIRED')

    def request(self, call_id: str, name: str, arguments: dict, preview: dict,
                control: RunControl) -> str:
        arguments, preview = copy.deepcopy(arguments), copy.deepcopy(preview)
        with control.suspend():
            with self._condition:
                if self._closed:
                    raise ApprovalError('APPROVAL_UNAVAILABLE')
                if self._pending is not None:
                    raise ApprovalError('APPROVAL_BUSY')
                control.check()
                remaining = control.approval_remaining()
                if remaining <= 0:
                    raise ApprovalError('APPROVAL_EXPIRED')
                created = self.clock()
                approval_id = uuid.uuid4().hex
                data = {key: preview[key] for key in (
                    'action_summary', 'risk', 'source', 'path', 'bytes', 'operation', 'content')
                        if key in preview}
                data.update(id=approval_id, approval_id=approval_id, run_id=self.run_id,
                            call_id=call_id, name=name, arguments=arguments, preview=preview,
                            created_at=created, expires_at=created + remaining,
                            argument_hash=hashlib.sha256(json.dumps(
                                arguments, ensure_ascii=False, allow_nan=False,
                                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest(),
                            process_generation=self.process_generation)
                if 'content' not in data and 'content' in arguments:
                    data['content'] = arguments['content']
                approval = _Approval(data, control)
                if self.journal is not None:
                    try:
                        self.journal.record_approval_required(call_id, data)
                    except Exception as error:
                        raise JournalFailure(error) from error
                self._pending = approval
            try:
                self._emit('approval.required', approval)
                with self._condition:
                    while True:
                        self._check(approval)
                        if approval.decision is not None:
                            return approval.decision
                        self._condition.wait(min(.05, control.approval_remaining()))
            except Exception as error:
                with self._condition:
                    approval.error = getattr(error, 'code', 'APPROVAL_UNAVAILABLE')
                raise
            finally:
                with self._condition:
                    self._pending = None
                    self._history[approval_id] = approval
                    self._condition.notify_all()
                self._emit('approval.resolved', approval)

    def snapshot(self) -> dict | None:
        with self._condition:
            approval = self._pending
            if self._closed or approval is None or approval.decision or approval.error:
                return None
            data = copy.deepcopy(approval.data)
            data['remaining_seconds'] = approval.control.approval_remaining()
            data['status'] = 'pending'
            return data

    def decide(self, approval_id: str, decision: str) -> str:
        if decision not in ('allow', 'deny'):
            raise ApprovalError('APPROVAL_INVALID_DECISION')
        with self._condition:
            if self._closed:
                raise ApprovalError('APPROVAL_UNAVAILABLE')
            approval = self._pending
            is_pending = approval is not None and approval.data['id'] == approval_id
            if not is_pending:
                approval = self._history.get(approval_id)
            if approval is None:
                raise ApprovalError('APPROVAL_NOT_FOUND')
            with approval.control.lock:
                try:
                    approval.control.check()
                    if approval.error:
                        raise ApprovalError(approval.error)
                    if is_pending and approval.control.approval_remaining() <= 0:
                        raise ApprovalError('APPROVAL_EXPIRED')
                except RunStopped as error:
                    self._condition.notify_all()
                    raise ApprovalError(error.code) from None
                except ApprovalError:
                    self._condition.notify_all()
                    raise
                if approval.decision is not None:
                    if approval.decision != decision:
                        raise ApprovalError('APPROVAL_CONFLICT')
                    return decision
                if self.journal is not None:
                    try:
                        self.journal.record_approval_decision(approval_id, decision)
                    except Exception as error:
                        raise JournalFailure(error) from error
                approval.decision = decision
                self._condition.notify_all()
                return decision

    def close(self) -> None:
        with self._condition:
            self._closed = True
            if self._pending is not None:
                self._pending.error = 'APPROVAL_UNAVAILABLE'
            self._condition.notify_all()
