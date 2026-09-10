"""Tool registration and execution, independent of particular file operations."""

import copy
from dataclasses import dataclass
import hashlib
import json
import time

from .approvals import ApprovalError, JournalFailure, RunStopped


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    risk: str = 'low'
    source: str = 'builtin'
    timeout_seconds: float = 5
    max_result_bytes: int = 65536
    error_codes: frozenset[str] = frozenset()
    user_error_codes: frozenset[str] = frozenset()


class ToolRegistry:
    def __init__(self, tools=(), summary=None):
        self._tools = {}
        self._summary = summary or (lambda: {})
        for tool in tools:
            self.register(tool)

    def register(self, tool):
        if not tool.spec.name or tool.spec.name in self._tools:
            raise ValueError('Tool names must be nonempty and unique')
        if (not callable(getattr(tool, 'verify_success', None))
                or (hasattr(tool, 'preview')
                    and not callable(getattr(tool, 'verify_preview', None)))
                or type(tool.spec.error_codes) is not frozenset
                or type(tool.spec.user_error_codes) is not frozenset
                or not tool.spec.user_error_codes <= tool.spec.error_codes):
            raise ValueError('Tools must declare and verify their result contract')
        self._tools[tool.spec.name] = tool

    def get(self, name):
        return self._tools.get(name)

    def schemas(self):
        return [{'type': 'function', 'function': {
            'name': tool.spec.name, 'description': tool.spec.description,
            'parameters': input_schema(tool.spec)}} for tool in self._tools.values()]

    def summary(self):
        return copy.deepcopy(self._summary())


def input_schema(spec):
    schema = copy.deepcopy(spec.input_schema)
    schema.setdefault('properties', {})['intent'] = {
        'type': 'string', 'minLength': 1, 'maxLength': 200,
        'description': '用一句话说明此次调用的目的，不是权限或执行结果。'}
    schema['required'] = list(dict.fromkeys([*schema.get('required', []), 'intent']))
    schema['additionalProperties'] = False
    return schema


USER_ERRORS = {'OS_PERMISSION_DENIED', 'WORKSPACE_CHANGED', 'DISK_FULL', 'IO_ERROR',
               'READ_ERROR', 'LIST_ERROR', 'WRITE_ERROR', 'WRITE_OUTCOME_UNKNOWN',
               'APPROVAL_UNAVAILABLE', 'APPROVAL_EXPIRED', 'TOOL_RESULT_INVALID',
               'TOOL_RESULT_TOO_LARGE'}


def tool_error(code, message=None, user_error_codes=frozenset()):
    return {'ok': False, 'error': {'code': code, 'message': message or code,
        'owner': 'user' if code in USER_ERRORS or code in user_error_codes else 'model'}}


def wire_result(raw, fields=None, user_error_codes=frozenset()):
    if not isinstance(raw, dict) or type(raw.get('ok')) is not bool:
        raw = tool_error('TOOL_RESULT_INVALID')
    if raw['ok']:
        result = {'ok': True, 'data': {k: v for k, v in raw.items() if k != 'ok'}}
    else:
        error = raw.get('error')
        valid = (set(raw) == {'ok', 'error'} and isinstance(error, dict)
                 and set(error) <= {'code', 'message', 'owner'}
                 and isinstance(error.get('code'), str) and bool(error['code'].strip())
                 and isinstance(error.get('message'), str) and bool(error['message'].strip()))
        if not valid:
            result = tool_error('TOOL_RESULT_INVALID')
        else:
            # Adapter-provided owner is never authoritative.
            result = tool_error(error['code'], error['message'], user_error_codes)
    if fields and 'scope' in fields:
        result['scope'] = fields['scope']
    return result


def parse_arguments(text):
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


def valid_arguments(spec, arguments):
    schema = input_schema(spec)
    if (not isinstance(arguments, dict)
            or set(arguments) - schema['properties'].keys()
            or set(schema['required']) - arguments.keys()):
        return False
    # This batch deliberately supports only flat string-parameter tools.
    for name, value in arguments.items():
        rule = schema['properties'][name]
        if rule.get('type') != 'string' or not isinstance(value, str):
            return False
        if len(value) < rule.get('minLength', 0) or len(value) > rule.get('maxLength', float('inf')):
            return False
        allowed = rule.get('enum')
        if allowed is not None and value not in allowed:
            return False
        try:
            value.encode('utf-8')
        except UnicodeError:
            return False
    return bool(arguments['intent'].strip())


@dataclass(frozen=True)
class Invocation:
    result: dict
    decision: str = 'continue'
    reason: str | None = None
    artifact_receipt: dict | None = None


class ToolRuntime:
    def __init__(self, registry, policy):
        self.registry, self.policy = registry, policy

    def skipped(self, code='TOOL_SKIPPED'):
        return wire_result(tool_error(code), self.policy.result_fields())

    @staticmethod
    def _user_errors(tool):
        return tool.spec.user_error_codes if tool is not None else frozenset()

    def _adapter_result(self, tool, arguments, raw, *, allow_success=True):
        try:
            if not isinstance(raw, dict) or type(raw.get('ok')) is not bool:
                raise ValueError('invalid envelope')
            if not raw['ok']:
                wired = wire_result(raw, user_error_codes=self._user_errors(tool))
                if (wired['error']['code'] == 'TOOL_RESULT_INVALID'
                        or wired['error']['code'] not in tool.spec.error_codes):
                    raise ValueError('invalid tool error')
                return copy.deepcopy(wired)
            if not allow_success:
                raise ValueError('preflight cannot succeed')
            data = {key: copy.deepcopy(value) for key, value in raw.items() if key != 'ok'}
            verified = tool.verify_success(copy.deepcopy(arguments), data)
            if not isinstance(verified, dict) or 'ok' in verified:
                raise ValueError('invalid success contract')
            result = {'ok': True, **copy.deepcopy(verified)}
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            return result
        except Exception:
            return tool_error('TOOL_RESULT_INVALID')

    @staticmethod
    def _fits(tool, raw):
        try:
            wired = wire_result(raw, user_error_codes=tool.spec.user_error_codes)
            return len(json.dumps(wired, ensure_ascii=False, allow_nan=False).encode('utf-8')) \
                <= tool.spec.max_result_bytes
        except Exception:
            return False

    @staticmethod
    def _verified_preview(tool, arguments):
        try:
            if hasattr(tool, 'preview'):
                preview = tool.preview(copy.deepcopy(arguments))
                preview = tool.verify_preview(copy.deepcopy(arguments),
                                              copy.deepcopy(preview))
            else:
                preview = {'action_summary': tool.spec.name}
            if (not isinstance(preview, dict)
                    or not isinstance(preview.get('action_summary'), str)
                    or not preview['action_summary'].strip()):
                raise ValueError('invalid preview')
            json.dumps(preview, ensure_ascii=False, allow_nan=False)
            return preview
        except Exception:
            return None

    @staticmethod
    def _journal(journal, method, *args):
        if journal is None:
            return None
        try:
            return getattr(journal, method)(*args)
        except JournalFailure:
            raise
        except Exception as error:
            raise JournalFailure(error) from error

    def invoke(self, call, *, budget_ok, execute_bounded, emit, control, approvals=None,
               tool_timeout=5, journal=None):
        function = call['function']
        name = function['name']
        emit('tool.requested', {'id': call['id'], **function})
        try:
            arguments = parse_arguments(function['arguments'])
        except (ValueError, TypeError, RecursionError):
            arguments = None
        tool = self.registry.get(name)
        values = {k: copy.deepcopy(v) for k, v in (arguments or {}).items() if k != 'intent'}
        decision, reason = 'continue', None
        verify_result, allow_success = False, True
        if not budget_ok:
            result = tool_error('TOOL_CALL_LIMIT')
        elif tool is None:
            result = tool_error('TOOL_NOT_FOUND')
        elif not valid_arguments(tool.spec, arguments):
            result = tool_error('INVALID_ARGUMENT')
        elif tool.spec.risk not in {'low', 'medium'} or tool.spec.source != 'builtin':
            result = tool_error('PATH_DENIED')
        else:
            values = copy.deepcopy(values)
            self.policy.before(name, copy.deepcopy(values))
            # Every stage sees its own copy of the same validated arguments.
            try:
                result = execute_bounded(lambda: tool.validate(copy.deepcopy(values)), tool.spec.timeout_seconds) \
                    if hasattr(tool, 'validate') else None
                verify_result, allow_success = result is not None, False
                if result is None:
                    preview = self._verified_preview(tool, values)
                    if preview is None:
                        result = tool_error('TOOL_RESULT_INVALID')
                    else:
                        preview = {**preview, 'risk': tool.spec.risk, 'source': tool.spec.source}
            except RunStopped as error:
                result, decision = tool_error(error.code), 'stop'
            except Exception:
                result = tool_error('IO_ERROR')
            if result is None:
                emit('tool.described', {'id': call['id'], 'name': name, 'intent': arguments['intent'],
                     **{k: preview[k] for k in ('action_summary', 'risk', 'source', 'path', 'bytes') if k in preview}})
                if tool.spec.risk == 'medium':
                    try:
                        if approvals is None:
                            raise ApprovalError('APPROVAL_UNAVAILABLE')
                        response = approvals.request(call['id'], name, copy.deepcopy(arguments),
                                                     copy.deepcopy(preview), control)
                        if response == 'deny':
                            result, decision = tool_error('USER_REJECTED'), 'finalize_only'
                    except (ApprovalError, RunStopped) as error:
                        result, decision = tool_error(error.code), 'stop'
                if result is None:
                    self._journal(journal, 'record_tool_started', call['id'])
                    emit('tool.started', {'id': call['id'], 'name': name,
                         **{k: preview[k] for k in ('action_summary', 'risk', 'source', 'path') if k in preview}})
                    started = time.monotonic()
                    tool_deadline = started + min(tool_timeout, tool.spec.timeout_seconds)
                    def check_commit():
                        control.check()
                        if time.monotonic() >= tool_deadline:
                            raise RunStopped('TOOL_TIMEOUT')
                    published, publication_outcomes, unpersisted_receipts = [], [], []
                    publication_attempted = False
                    def publish(callback):
                        def accept_publication():
                            nonlocal publication_attempted
                            check_commit()
                            if publication_attempted:
                                return tool_error('TOOL_RESULT_INVALID')
                            # Approval authorizes one publication attempt. Consume it before
                            # calling adapter code because that callback may have side effects.
                            publication_attempted = True
                            if name == 'write_file':
                                content = values.get('content')
                                raw = content.encode('utf-8') if isinstance(content, str) else b''
                                self._journal(journal, 'record_publication_intent', call['id'], {
                                    'path': values.get('path'), 'bytes': len(raw),
                                    'sha256': hashlib.sha256(raw).hexdigest(),
                                })
                            try:
                                receipt = callback()
                            except OSError:
                                # The built-in file tool maps errno while it still knows whether
                                # publication started (for example EEXIST versus unknown outcome).
                                raise
                            except Exception:
                                outcome = tool_error('WRITE_OUTCOME_UNKNOWN')
                                publication_outcomes.append(copy.deepcopy(outcome))
                                return outcome
                            checked = self._adapter_result(tool, values, receipt)
                            # Publication happened before its receipt returned. If that receipt cannot
                            # prove the outcome, stop without claiming either success or rollback.
                            if not checked['ok'] or not self._fits(tool, checked):
                                outcome = tool_error('WRITE_OUTCOME_UNKNOWN')
                                publication_outcomes.append(copy.deepcopy(outcome))
                                return outcome
                            if name == 'write_file':
                                try:
                                    self._journal(journal, 'record_publication_receipt',
                                                  call['id'], checked)
                                except JournalFailure:
                                    unpersisted_receipts.append(copy.deepcopy(checked))
                                    outcome = tool_error('WRITE_OUTCOME_UNKNOWN')
                                    publication_outcomes.append(copy.deepcopy(outcome))
                                    return outcome
                            self.policy.accept(name, copy.deepcopy(values), copy.deepcopy(checked))
                            published.append(copy.deepcopy(checked))
                            publication_outcomes.append(copy.deepcopy(checked))
                            return checked
                        return control.commit(accept_publication)
                    try:
                        result = execute_bounded(lambda: tool.execute(copy.deepcopy(values)),
                                                 tool.spec.timeout_seconds)
                        if hasattr(tool, 'commit'):
                            candidate = result
                            result = execute_bounded(lambda: tool.commit(copy.deepcopy(values), candidate,
                                check=check_commit, publish=publish),
                                max(.000001, tool_deadline - time.monotonic()))
                            if publication_outcomes:
                                checked_return = self._adapter_result(tool, values, result)
                                result = (checked_return if checked_return['ok'] and published
                                          else publication_outcomes[0])
                                verify_result = False
                            elif isinstance(result, dict) and result.get('ok') is True:
                                result = tool_error('TOOL_RESULT_INVALID')
                                verify_result = False
                            else:
                                verify_result = True
                        else:
                            verify_result = True
                        allow_success = True
                    except RunStopped as error:
                        # A publication already in progress wins its lock; preserve the actual receipt.
                        with control.lock:
                            result = published[0] if published else tool_error(error.code)
                        decision, reason = 'stop', error.code
                    except JournalFailure:
                        raise
                    except Exception:
                        with control.lock:
                            result = published[0] if published else tool_error('IO_ERROR')
                        verify_result = False
                    # Accept receipts before writing a log that can itself fail.
                    outcome = self._outcome(tool, name, values, result, decision, reason,
                                            verify_result=verify_result,
                                            allow_success=allow_success,
                                            accepted=bool(published), journal=journal,
                                            call_id=call['id'], artifact_receipt=(
                                                unpersisted_receipts[0]
                                                if unpersisted_receipts else None))
                    emit('tool.finished', {'id': call['id'], 'elapsed_seconds': time.monotonic() - started})
                    return outcome
        return self._outcome(tool, name, values, result, decision, reason,
                             verify_result=verify_result, allow_success=allow_success,
                             journal=journal, call_id=call['id'])

    def _outcome(self, tool, name, arguments, raw, decision, reason, *,
                 verify_result=False, allow_success=True, accepted=False,
                 journal=None, call_id=None, artifact_receipt=None):
        if verify_result and tool is not None:
            raw = self._adapter_result(tool, arguments, raw, allow_success=allow_success)
        result = wire_result(raw, user_error_codes=self._user_errors(tool))
        if tool is not None and not self._fits(tool, raw):
            raw = tool_error('TOOL_RESULT_TOO_LARGE')
            result = wire_result(raw, user_error_codes=self._user_errors(tool))
        if not result['ok']:
            raw = result
        if tool is not None and not accepted:
            self.policy.accept(name, copy.deepcopy(arguments), copy.deepcopy(raw))
        result = wire_result(raw, self.policy.result_fields(), self._user_errors(tool))
        if not result['ok'] and result['error']['owner'] == 'user':
            decision = 'stop'
        if call_id is not None:
            self._journal(journal, 'record_tool_result', call_id, result)
        return Invocation(result, decision, reason, artifact_receipt)
