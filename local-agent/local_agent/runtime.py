"""Model-selected actions with bounded execution and verifiable file evidence."""

import copy
from dataclasses import asdict, dataclass
import json
import math
import queue
import threading
import time
from typing import Callable

from .answers import AnswerError
from .approvals import JournalFailure, RunControl, RunStopped
from .tool_runtime import parse_arguments, tool_error
from .file_tools import adapt_tools, model_request, TOOLS
from .prompts import SYSTEM, DIRECTORY_SYSTEM
from .provider import ModelReply, Provider, ProviderError
from .trace import Trace


JSON_REPAIR = '''上一条最终答案不是有效的严格 JSON。请立即重新输出一个 JSON 对象，不要调用工具，不要加解释或代码围栏。
请重新检查整个对象，不要原样复制上一条。字段名和字符串边界使用英文双引号；answer 内的名称不加英文双引号，改用中文“引号”或直接表述。
正确格式示例：{"status":"not_found","answer":"文件记载了“示例名称”，但未说明所问信息。","citations":[]}。
示例仅说明 JSON 引号写法，不提供事实或决定状态；保留本次工具证据支持的事实、原状态和合法引用。'''


IDENTIFIER_REPAIR = '''上一条回答中的标识连接符与本次已读原文不一致。请依据已有工具结果逐字核对专名、代号和编号，保留原文字符，不替换成外形相似的横线。
立即重新输出一个严格 JSON 对象；不要调用工具，不添加解释或代码围栏，不通过删掉所问事实来回避核对。保留有证据支持的事实、适用的状态和合法引用。'''


@dataclass(frozen=True)
class RunConfig:
    max_steps: int = 6
    run_timeout: float = 120
    model_timeout: float = 45
    tool_timeout: float = 5
    max_tool_calls: int = 4
    max_input_bytes: int = 65536

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('Run limits must be finite and positive')
            if name in ('max_steps', 'max_tool_calls', 'max_input_bytes') and type(value) is not int:
                raise ValueError('Count limits must be integers')


StopRun = RunStopped


class NullRunJournal:
    """Explicit compatibility seam for one-shot runs without session storage."""

    session_id = None
    run_id = None

    def record_model_request(self, request, manifest):
        return None

    def record_model_reply(self, message, usage=None, validation='raw'):
        return None

    def record_tool_started(self, call_id):
        return None

    def record_tool_result(self, call_id, result):
        return None

    def record_approval_required(self, call_id, approval):
        return None

    def record_approval_decision(self, approval_id, decision):
        return None

    def record_publication_intent(self, call_id, intent):
        return None

    def record_publication_receipt(self, call_id, receipt):
        return None

    def finish_run(self, result):
        return None

    def close_thread_connection(self):
        return None


def check_stop(cancel: threading.Event, deadline: float):
    if cancel.is_set():
        raise StopRun('CANCELLED')
    if time.monotonic() >= deadline:
        raise StopRun('RUN_TIMEOUT')


def bounded_call(fn: Callable, timeout: float, cancel: threading.Event,
                 run_deadline: float, timeout_code: str, cleanup: Callable | None = None):
    check_stop(cancel, run_deadline)
    local_deadline = time.monotonic() + timeout
    result_queue = queue.Queue(maxsize=1)

    def worker():
        try:
            check_stop(cancel, run_deadline)
            if time.monotonic() >= local_deadline:
                raise StopRun(timeout_code)
            result_queue.put((True, fn()))
        except Exception as error:
            result_queue.put((False, error))
        finally:
            if cleanup is not None:
                cleanup()

    # Daemon workers never publish events or mutate run state after cancellation.
    threading.Thread(target=worker, daemon=True).start()
    while True:
        check_stop(cancel, run_deadline)
        remaining = local_deadline - time.monotonic()
        if remaining <= 0:
            raise StopRun(timeout_code)
        try:
            wait = max(0, min(.01, remaining, run_deadline - time.monotonic()))
            ok, value = result_queue.get(timeout=wait)
        except queue.Empty:
            continue
        check_stop(cancel, run_deadline)
        if time.monotonic() >= local_deadline:
            raise StopRun(timeout_code)
        if not ok:
            raise value
        return value


class Runtime:
    def __init__(self, provider: Provider, tool, trace: Trace, config: RunConfig | None = None,
                 *, approvals=None, control=None, journal=None, request_builder=None):
        self.provider, self.tool, self.trace = provider, tool, trace
        self.config = config or RunConfig()
        self.approvals, self.control = approvals, control
        self.journal = journal if journal is not None else NullRunJournal()
        self.request_builder = request_builder
        if self.journal.run_id is not None and self.journal.run_id != self.trace.run_id:
            raise ValueError('Trace and journal must use the same run ID')
        self._used = False

    def _build_request(self, current_request: dict, step: int):
        if self.request_builder is None:
            return current_request, {'source_kind': 'current_run', 'request_seq': step}
        builder = (self.request_builder.build
                   if callable(getattr(self.request_builder, 'build', None))
                   else self.request_builder)
        built = builder(copy.deepcopy(current_request), self.config.max_input_bytes)
        request = getattr(built, 'request', None)
        manifest = getattr(built, 'manifest', None)
        if not isinstance(request, dict) or not isinstance(manifest, dict):
            raise JournalFailure(ValueError('Invalid request builder result'))
        return copy.deepcopy(request), copy.deepcopy(manifest)

    def run(self, question: str, target_path: str | None, cancel: threading.Event | None = None) -> dict:
        if self._used:
            raise ValueError('Create a new Runtime and Trace for each run')
        self._used = True
        cancel = cancel or threading.Event()
        started = time.monotonic()
        control = self.control or RunControl(cancel, self.config.run_timeout, clock=time.monotonic)
        cancel = control.cancel
        finalize_only, final_reason = False, None
        calls, seen_ids = 0, set()
        answer_repairs = 0
        engine = adapt_tools(self.tool)
        tools = engine.registry.schemas()
        persistent = not isinstance(self.journal, NullRunJournal)
        trace_errors = []
        unpersisted_artifacts = []

        def emit(event, data):
            try:
                self.trace.emit(event, data)
            except OSError:
                if not persistent:
                    raise
                trace_errors.append('TRACE_ERROR')

        def journal_call(method, *args):
            try:
                return getattr(self.journal, method)(*args)
            except JournalFailure:
                raise
            except Exception as error:
                raise JournalFailure(error) from error

        def finish(state, reason, answer=None, *, persist=True):
            result = {'run_id': self.journal.run_id or self.trace.run_id,
                      'state': state, 'stop_reason': reason,
                      'model_calls': calls, 'answer': answer,
                      'elapsed_seconds': round(time.monotonic() - started, 4),
                      'provider': self.provider.metadata, 'trace_path': str(self.trace.path)}
            with control.lock:
                result.update(engine.policy.result_fields())
            if unpersisted_artifacts:
                result['unpersisted_artifacts'] = copy.deepcopy(unpersisted_artifacts)
            if trace_errors:
                result['diagnostics'] = sorted(set(trace_errors))
            if persist:
                journal_call('finish_run', result)
            try:
                self.trace.emit('run.ended', result)
            except OSError:
                if persistent:
                    result.setdefault('diagnostics', []).append('TRACE_ERROR')
                else:
                    result.update(state='failed', stop_reason='TRACE_ERROR', answer=None)
            return result

        try:
            emit('run.started', {'provider': self.provider.metadata, 'config': asdict(self.config),
                                 'path': target_path, 'question': question})
            try:
                messages = engine.policy.initial_messages(question, target_path)
            except ValueError:
                return finish('failed', 'INVALID_TASK')
            while calls < self.config.max_steps:
                control.check()
                deadline = time.monotonic() + control.remaining()
                current_request = engine.policy.model_request(
                    messages, self.config.max_input_bytes, tools)
                request, manifest = self._build_request(current_request, calls + 1)
                if len(json.dumps(request, ensure_ascii=False).encode('utf-8')) > self.config.max_input_bytes:
                    return finish('failed', 'CONTEXT_LIMIT')
                journal_call('record_model_request', request, manifest)
                calls += 1
                emit('model.requested', {'step': calls, **request})
                model_start = time.monotonic()
                timeout = min(self.config.model_timeout, deadline - model_start)
                provider_messages, provider_tools = copy.deepcopy(request['messages']), copy.deepcopy(request['tools'])
                reply = bounded_call(lambda: self.provider.complete(provider_messages, provider_tools, timeout),
                                     self.config.model_timeout, cancel, deadline, 'MODEL_TIMEOUT',
                                     self.journal.close_thread_connection)
                if not isinstance(reply, ModelReply) or not isinstance(reply.message, dict):
                    return finish('failed', 'INVALID_MODEL_RESPONSE')
                message = reply.message
                if message.get('role') != 'assistant':
                    journal_call('record_model_reply', message, reply.usage,
                                 'invalid_model_response')
                    return finish('failed', 'INVALID_MODEL_RESPONSE')
                tool_calls = message.get('tool_calls')
                if not tool_calls:
                    try:
                        answer = engine.policy.validate(message.get('content'))
                    except AnswerError as error:
                        journal_call('record_model_reply', message, reply.usage,
                                     'answer_invalid:' + error.code)
                        emit('model.completed', {'step': calls, 'message': message,
                            'usage': reply.usage, 'elapsed_seconds': time.monotonic() - model_start})
                        if not finalize_only and error.repairable and answer_repairs == 0 and calls < self.config.max_steps:
                            answer_repairs += 1
                            emit('answer.rejected', {
                                'code': error.code, 'repair_attempt': answer_repairs})
                            messages.append(copy.deepcopy(message))
                            repair_builder = getattr(engine.policy, 'repair_prompt', None)
                            repair = (repair_builder(error.code) if callable(repair_builder)
                                      else (IDENTIFIER_REPAIR if error.code == 'IDENTIFIER_MISMATCH'
                                            else JSON_REPAIR))
                            messages.append({'role': 'user', 'content': repair})
                            continue
                        return finish('validation_failed', error.code)
                    journal_call('record_model_reply', message, reply.usage, 'answer_valid')
                    emit('model.completed', {'step': calls, 'message': message,
                        'usage': reply.usage, 'elapsed_seconds': time.monotonic() - model_start})
                    control.check()
                    return finish('unable' if answer['status'] == 'unable' else 'completed',
                                  (final_reason or engine.policy.failure_reason() or 'FILE_UNAVAILABLE')
                                  if answer['status'] == 'unable' else 'ANSWER_VALIDATED', answer)
                if not self._valid_calls(tool_calls, seen_ids):
                    journal_call('record_model_reply', message, reply.usage,
                                 'tool_calls_invalid')
                    emit('model.completed', {'step': calls, 'message': message,
                        'usage': reply.usage, 'elapsed_seconds': time.monotonic() - model_start})
                    return finish('failed', 'INVALID_MODEL_RESPONSE')
                journal_call('record_model_reply', message, reply.usage, 'tool_calls_valid')
                emit('model.completed', {'step': calls, 'message': message,
                    'usage': reply.usage, 'elapsed_seconds': time.monotonic() - model_start})
                messages.append(copy.deepcopy(message))
                stopped = finalize_only
                decision = 'stop' if finalize_only else 'continue'
                stop_code = final_reason
                for index, call in enumerate(tool_calls):
                    seen_ids.add(call['id'])
                    if stopped:
                        emit('tool.requested', {'id': call['id'], **call['function']})
                        result = engine.skipped()
                        journal_call('record_tool_result', call['id'], result)
                    else:
                        outcome = engine.invoke(call, budget_ok=index < self.config.max_tool_calls,
                            execute_bounded=lambda fn, limit: bounded_call(fn,
                                min(self.config.tool_timeout, limit), cancel,
                                time.monotonic() + control.remaining(), 'TOOL_TIMEOUT',
                                self.journal.close_thread_connection),
                            emit=emit, control=control, approvals=self.approvals,
                            tool_timeout=self.config.tool_timeout, journal=self.journal)
                        result, decision = outcome.result, outcome.decision
                        if outcome.artifact_receipt is not None:
                            unpersisted_artifacts.append(outcome.artifact_receipt)
                        if decision != 'continue':
                            stopped = True
                            stop_code = outcome.reason or result['error']['code']
                    messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                     'content': json.dumps(result, ensure_ascii=False)})
                    emit('tool.completed', {'id': call['id'], 'result': result})
                if decision == 'stop':
                    state = 'cancelled' if stop_code == 'CANCELLED' else (
                        'timed_out' if stop_code in {'RUN_TIMEOUT', 'TOOL_TIMEOUT', 'APPROVAL_EXPIRED'}
                        else 'unable' if stop_code == 'USER_REJECTED' else 'failed')
                    return finish(state, stop_code)
                if decision == 'finalize_only':
                    finalize_only, final_reason = True, stop_code
            if finalize_only:
                return finish('unable', final_reason)
            return finish('max_steps', 'MAX_STEPS')
        except StopRun as error:
            return finish('cancelled' if error.code == 'CANCELLED' else 'timed_out', error.code)
        except ProviderError as error:
            return finish('timed_out' if error.code == 'MODEL_TIMEOUT' else 'failed', error.code)
        except JournalFailure:
            return finish('failed', 'SESSION_STORE_ERROR', persist=False)
        except ValueError as error:
            if str(error) == 'CONTEXT_LIMIT':
                return finish('failed', 'CONTEXT_LIMIT')
            return finish('failed', 'INTERNAL_ERROR')
        except OSError:
            return finish('failed', 'TRACE_ERROR')
        except Exception:
            return finish('failed', 'INTERNAL_ERROR')

    @staticmethod
    def _valid_calls(calls, seen_ids):
        if not isinstance(calls, list) or not 1 <= len(calls) <= 32:
            return False
        batch_ids = set()
        for call in calls:
            if not isinstance(call, dict) or call.get('type') != 'function':
                return False
            call_id, function = call.get('id'), call.get('function')
            if not isinstance(call_id, str) or not call_id or call_id in seen_ids or call_id in batch_ids:
                return False
            if not isinstance(function, dict) or not isinstance(function.get('name'), str) or not isinstance(function.get('arguments'), str):
                return False
            batch_ids.add(call_id)
        return True
