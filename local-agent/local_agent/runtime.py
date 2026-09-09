"""Model-selected actions with bounded execution and verifiable file evidence."""

import copy
from dataclasses import asdict, dataclass
import json
import math
import queue
import threading
import time
from typing import Callable

from .answers import AnswerError, validate_answer
from .discovery import DirectoryTools, READ_FILE_SCHEMA
from .prompts import SYSTEM, DIRECTORY_SYSTEM
from .provider import ModelReply, Provider, ProviderError
from .trace import Trace


TOOLS = [READ_FILE_SCHEMA]


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


class StopRun(Exception):
    def __init__(self, code: str):
        self.code = code


def check_stop(cancel: threading.Event, deadline: float):
    if cancel.is_set():
        raise StopRun('CANCELLED')
    if time.monotonic() >= deadline:
        raise StopRun('RUN_TIMEOUT')


def bounded_call(fn: Callable, timeout: float, cancel: threading.Event,
                 run_deadline: float, timeout_code: str):
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


def tool_error(code: str) -> dict:
    return {'ok': False, 'error': {'code': code, 'message': code}}


def parse_arguments(text: str) -> dict:
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('duplicate field')
            obj[key] = value
        return obj
    result = json.loads(text, object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(result, dict):
        raise ValueError('not an object')
    return result


def model_request(messages: list[dict], max_input_bytes: int, tools: list[dict] | None = None) -> dict:
    request = {'messages': copy.deepcopy(messages), 'tools': TOOLS if tools is None else tools}
    for message in request['messages']:
        if message['role'] != 'tool':
            continue
        result = json.loads(message['content'])
        if result.get('ok') and isinstance(result.get('content'), str):
            # Only the model view gets line anchors; evidence and history stay unchanged.
            result['content'] = {str(number): line
                                 for number, line in enumerate(result['content'].splitlines(), 1)}
            message['content'] = json.dumps(result, ensure_ascii=False)
    if len(json.dumps(request, ensure_ascii=False).encode('utf-8')) > max_input_bytes:
        # Many short lines can cost more to number than to send as complete raw text.
        request['messages'] = messages
    return request


class Runtime:
    def __init__(self, provider: Provider, tool, trace: Trace, config: RunConfig | None = None):
        self.provider, self.tool, self.trace = provider, tool, trace
        self.config = config or RunConfig()
        self._used = False

    def run(self, question: str, target_path: str | None, cancel: threading.Event | None = None) -> dict:
        if self._used:
            raise ValueError('Create a new Runtime and Trace for each run')
        self._used = True
        cancel = cancel or threading.Event()
        started = time.monotonic()
        deadline = started + self.config.run_timeout
        calls, seen_ids, snapshots = 0, set(), {}
        answer_repairs = 0
        read_attempted = False
        directory_mode = isinstance(self.tool, DirectoryTools)
        tools = self.tool.schemas if directory_mode else TOOLS
        tool_names = {schema['function']['name'] for schema in tools}
        task = {'directory': '.', 'question': question} if directory_mode else {'file': target_path, 'question': question}
        messages = [{'role': 'system', 'content': DIRECTORY_SYSTEM if directory_mode else SYSTEM},
                    {'role': 'user', 'content': json.dumps(task, ensure_ascii=False)}]

        def finish(state, reason, answer=None):
            result = {'run_id': self.trace.run_id, 'state': state, 'stop_reason': reason,
                      'model_calls': calls, 'answer': answer,
                      'elapsed_seconds': round(time.monotonic() - started, 4),
                      'provider': self.provider.metadata, 'trace_path': str(self.trace.path)}
            if directory_mode:
                result['scope'] = self.tool.coverage()
            try:
                self.trace.emit('run.ended', result)
            except OSError:
                result.update(state='failed', stop_reason='TRACE_ERROR', answer=None)
            return result

        try:
            self.trace.emit('run.started', {'provider': self.provider.metadata, 'config': asdict(self.config),
                                           'path': target_path, 'question': question})
            valid_target = target_path is None if directory_mode else isinstance(target_path, str) and bool(target_path)
            if not isinstance(question, str) or not question.strip() or not valid_target:
                return finish('failed', 'INVALID_TASK')
            while calls < self.config.max_steps:
                check_stop(cancel, deadline)
                request = model_request(messages, self.config.max_input_bytes, tools)
                if len(json.dumps(request, ensure_ascii=False).encode('utf-8')) > self.config.max_input_bytes:
                    return finish('failed', 'CONTEXT_LIMIT')
                calls += 1
                self.trace.emit('model.requested', {'step': calls, **request})
                model_start = time.monotonic()
                timeout = min(self.config.model_timeout, deadline - model_start)
                provider_messages, provider_tools = copy.deepcopy(request['messages']), copy.deepcopy(request['tools'])
                reply = bounded_call(lambda: self.provider.complete(provider_messages, provider_tools, timeout),
                                     self.config.model_timeout, cancel, deadline, 'MODEL_TIMEOUT')
                if not isinstance(reply, ModelReply) or not isinstance(reply.message, dict):
                    return finish('failed', 'INVALID_MODEL_RESPONSE')
                message = reply.message
                if message.get('role') != 'assistant':
                    return finish('failed', 'INVALID_MODEL_RESPONSE')
                self.trace.emit('model.completed', {'step': calls, 'message': message,
                    'usage': reply.usage, 'elapsed_seconds': time.monotonic() - model_start})
                tool_calls = message.get('tool_calls')
                if not tool_calls:
                    try:
                        answer = validate_answer(message.get('content'), snapshots, target_path, read_attempted,
                                                 coverage=self.tool.coverage() if directory_mode else None)
                    except AnswerError as error:
                        if error.repairable and answer_repairs == 0 and calls < self.config.max_steps:
                            answer_repairs += 1
                            self.trace.emit('answer.rejected', {
                                'code': error.code, 'repair_attempt': answer_repairs})
                            messages.append(copy.deepcopy(message))
                            repair = IDENTIFIER_REPAIR if error.code == 'IDENTIFIER_MISMATCH' else JSON_REPAIR
                            messages.append({'role': 'user', 'content': repair})
                            continue
                        return finish('validation_failed', error.code)
                    check_stop(cancel, deadline)
                    return finish('unable' if answer['status'] == 'unable' else 'completed',
                                  'FILE_UNAVAILABLE' if answer['status'] == 'unable' else 'ANSWER_VALIDATED', answer)
                if not self._valid_calls(tool_calls, seen_ids):
                    return finish('failed', 'INVALID_MODEL_RESPONSE')
                messages.append(copy.deepcopy(message))
                for index, call in enumerate(tool_calls):
                    check_stop(cancel, deadline)
                    seen_ids.add(call['id'])
                    function = call['function']
                    self.trace.emit('tool.requested', {'id': call['id'], **function})
                    try:
                        arguments = parse_arguments(function['arguments'])
                    except (ValueError, TypeError, RecursionError):
                        arguments = None
                    if index >= self.config.max_tool_calls:
                        result = tool_error('TOOL_CALL_LIMIT')
                    elif function['name'] not in tool_names:
                        result = tool_error('TOOL_NOT_FOUND')
                    elif arguments is None:
                        result = tool_error('INVALID_ARGUMENT')
                    else:
                        if arguments.get('path') == target_path:
                            read_attempted = True
                        self.trace.emit('tool.started', {'id': call['id'], 'name': function['name'], 'path': arguments.get('path')})
                        tool_start = time.monotonic()
                        try:
                            def execute(args=copy.deepcopy(arguments), name=function['name']):
                                return self.tool.execute(name, args) if directory_mode else self.tool.execute(args)
                            result = bounded_call(execute, self.config.tool_timeout,
                                                  cancel, deadline, 'TOOL_TIMEOUT')
                        except StopRun as error:
                            if error.code == 'TOOL_TIMEOUT':
                                self.trace.emit('tool.completed', {'id': call['id'],
                                    'result': tool_error(error.code),
                                    'elapsed_seconds': time.monotonic() - tool_start})
                            raise
                        except Exception:
                            result = tool_error('READ_ERROR')
                        self.trace.emit('tool.finished', {'id': call['id'], 'elapsed_seconds': time.monotonic() - tool_start})
                    if directory_mode:
                        # Only the accepted result changes permissions; late workers cannot publish state.
                        self.tool.record(function['name'], arguments, result)
                        path = arguments.get('path') if arguments else None
                        if function['name'] == 'read_file' and isinstance(path, str):
                            if result.get('ok') and result.get('path') == path:
                                snapshots[path] = copy.deepcopy(result)
                            else:
                                snapshots.pop(path, None)
                        result = {**result, 'scope': self.tool.coverage()}
                    elif result.get('ok') and result.get('path') == target_path:
                        snapshots[target_path] = copy.deepcopy(result)
                    messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                     'content': json.dumps(result, ensure_ascii=False)})
                    self.trace.emit('tool.completed', {'id': call['id'], 'result': result})
            return finish('max_steps', 'MAX_STEPS')
        except StopRun as error:
            return finish('cancelled' if error.code == 'CANCELLED' else 'timed_out', error.code)
        except ProviderError as error:
            return finish('timed_out' if error.code == 'MODEL_TIMEOUT' else 'failed', error.code)
        except OSError:
            return {'run_id': self.trace.run_id, 'state': 'failed', 'stop_reason': 'TRACE_ERROR',
                    'model_calls': calls, 'answer': None, 'trace_path': str(self.trace.path)}
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
