"""File adapters and evidence policy for the generic tool execution loop."""

import copy
import json

from .answers import AnswerError, validate_answer
from .discovery import DirectoryTools, READ_FILE_SCHEMA
from .prompts import SYSTEM, DIRECTORY_SYSTEM, TOOL_PROTOCOL, JSON_REPAIR, IDENTIFIER_REPAIR
from .tool_runtime import ToolSpec, ToolRegistry, ToolRuntime, tool_error
from .files import _path_parts
from pathlib import Path


TOOLS = [READ_FILE_SCHEMA]

READ_ERROR_CODES = frozenset({
    'INVALID_ARGUMENT', 'PATH_DENIED', 'FILE_NOT_FOUND', 'UNSUPPORTED_FILE',
    'FILE_TOO_LARGE', 'FILE_CHANGED', 'READ_ERROR', 'OS_PERMISSION_DENIED',
    'WORKSPACE_CHANGED',
})
DIRECTORY_READ_ERROR_CODES = READ_ERROR_CODES | frozenset({
    'PATH_NOT_DISCOVERED', 'FILE_COUNT_LIMIT',
})
LIST_ERROR_CODES = frozenset({
    'INVALID_ARGUMENT', 'PATH_DENIED', 'PATH_NOT_DISCOVERED', 'DIRECTORY_NOT_FOUND',
    'DIRECTORY_TOO_LARGE', 'DIRECTORY_CHANGED', 'LIST_ERROR',
    'OS_PERMISSION_DENIED', 'WORKSPACE_CHANGED',
})


def _valid_hash(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in '0123456789abcdef' for char in value))


def _verify_read_success(arguments, data, max_bytes=32768):
    if (set(data) != {'path', 'content', 'line_count', 'bytes', 'sha256'}
            or data.get('path') != arguments.get('path')
            or not isinstance(data.get('path'), str)
            or _path_parts(data['path']) is None
            or Path(data['path']).suffix not in {'.md', '.txt'}
            or not isinstance(data.get('content'), str)
            or '\r' in data['content']
            or type(data.get('line_count')) is not int
            or data['line_count'] != len(data['content'].splitlines())
            or type(data.get('bytes')) is not int
            or not 0 <= data['bytes'] <= max_bytes
            or not _valid_hash(data.get('sha256'))):
        raise ValueError('invalid read result')
    if any((ord(char) < 32 and char not in '\t\n') or ord(char) == 127
           for char in data['content']):
        raise ValueError('invalid read content')
    encoded = data['content'].encode('utf-8')
    if not len(encoded) <= data['bytes'] <= len(encoded) + data['content'].count('\n'):
        raise ValueError('invalid read byte count')
    return copy.deepcopy(data)


def _verify_list_success(arguments, data):
    if (set(data) != {'path', 'entries', 'complete'}
            or data.get('path') != arguments.get('path')
            or not isinstance(data.get('path'), str)
            or _path_parts(data['path'], allow_root=True) is None
            or data.get('complete') is not True
            or not isinstance(data.get('entries'), list)
            or len(data['entries']) > 200):
        raise ValueError('invalid list result')
    parent = [] if data['path'] == '.' else data['path'].split('/')
    paths = []
    for entry in data['entries']:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise ValueError('invalid list entry')
        path = entry['path']
        parts = _path_parts(path)
        if parts is None or parts[:-1] != parent:
            raise ValueError('invalid list path')
        if entry.get('type') == 'directory':
            if set(entry) != {'path', 'type'}:
                raise ValueError('invalid directory entry')
        elif entry.get('type') == 'file':
            if (set(entry) != {'path', 'type', 'bytes'}
                    or Path(path).suffix not in {'.md', '.txt'}
                    or type(entry.get('bytes')) is not int or entry['bytes'] < 0):
                raise ValueError('invalid file entry')
        else:
            raise ValueError('invalid entry type')
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError('list entries must be unique and sorted')
    return copy.deepcopy(data)


def model_request(messages, max_input_bytes, tools=None):
    request = {'messages': copy.deepcopy(messages), 'tools': TOOLS if tools is None else tools}
    for message in request['messages']:
        if message['role'] != 'tool':
            continue
        result = json.loads(message['content'])
        data = result.get('data', result)
        if result.get('ok') and isinstance(data.get('content'), str):
            data['content'] = {str(number): line
                               for number, line in enumerate(data['content'].splitlines(), 1)}
            message['content'] = json.dumps(result, ensure_ascii=False)
    if len(json.dumps(request, ensure_ascii=False).encode('utf-8')) > max_input_bytes:
        request['messages'] = messages
    return request


class FileAdapter:
    def __init__(self, schema, execute, verify_success, error_codes,
                 clear_result_proof, take_result_proof):
        function = schema['function']
        self.spec = ToolSpec(function['name'], function['description'], function['parameters'],
                             error_codes=error_codes)
        self._execute = execute
        self._verify_success = verify_success
        self._clear_result_proof = clear_result_proof
        self._take_result_proof = take_result_proof
        self._proven_result = None

    def execute(self, arguments):
        self._proven_result = None
        self._clear_result_proof()
        result = self._execute(arguments)
        proof = self._take_result_proof(arguments)
        if proof == result:
            self._proven_result = copy.deepcopy(result)
        return result

    def verify_success(self, arguments, data):
        verified = self._verify_success(arguments, data)
        expected = {'ok': True, **verified}
        proven, self._proven_result = self._proven_result, None
        if proven != expected:
            raise ValueError('success did not come from this tool execution')
        return verified

    def preview(self, arguments):
        verb = '列出目录' if self.spec.name == 'list_files' else '读取文件'
        return {'action_summary': f"{verb} {arguments['path']}，不会修改文件。", 'path': arguments['path']}

    def verify_preview(self, arguments, preview):
        verb = '列出目录' if self.spec.name == 'list_files' else '读取文件'
        expected = {'action_summary': f"{verb} {arguments['path']}，不会修改文件。",
                    'path': arguments['path']}
        if preview != expected:
            raise ValueError('file preview does not match arguments')
        return copy.deepcopy(preview)


class FilePolicy:
    def __init__(self, tool, output_path=None):
        self.tool = tool
        self.directory = isinstance(tool, DirectoryTools)
        self.snapshots = {}
        self.read_attempted = False
        self.target = None
        self.output_path = output_path
        self.artifacts = []
        self.write_failure = None

    def failure_reason(self):
        return self.write_failure

    def initial_messages(self, question, target):
        valid = target is None if self.directory else isinstance(target, str) and bool(target)
        if not isinstance(question, str) or not question.strip() or not valid:
            raise ValueError('INVALID_TASK')
        if self.output_path is not None and (not isinstance(self.output_path, str)
                or _path_parts(self.output_path) is None
                or Path(self.output_path).suffix not in {'.md', '.txt'} or self.output_path == target):
            raise ValueError('INVALID_TASK')
        self.target = target
        task = {'directory': '.', 'question': question} if self.directory else {'file': target, 'question': question}
        system = (DIRECTORY_SYSTEM if self.directory else SYSTEM) + TOOL_PROTOCOL
        if self.output_path is not None:
            task['output_file'] = self.output_path
            system += WRITE_TASK
        return [{'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(task, ensure_ascii=False)}]

    def before(self, name, arguments):
        if arguments.get('path') == self.target:
            self.read_attempted = True

    def accept(self, name, arguments, result):
        if name == 'write_file':
            if result.get('ok') and result.get('operation') == 'created' and result.get('path') == self.output_path:
                self.artifacts = [{key: result[key] for key in ('path', 'bytes', 'sha256', 'operation')}]
                self.write_failure = None
            elif not result.get('ok'):
                self.write_failure = result.get('error', {}).get('code')
            return
        if self.directory:
            self.tool.record(name, arguments, result)
            path = arguments.get('path') if arguments else None
            if name == 'read_file' and isinstance(path, str):
                if result.get('ok') and result.get('path') == path:
                    self.snapshots[path] = copy.deepcopy(result)
                else:
                    self.snapshots.pop(path, None)
        elif (name == 'read_file' and isinstance(arguments, dict)
              and arguments.get('path') == self.target):
            if result.get('ok') and result.get('path') == self.target:
                self.snapshots[self.target] = copy.deepcopy(result)
            elif result.get('error', {}).get('code') not in {'TOOL_CALL_LIMIT', 'TOOL_SKIPPED'}:
                self.snapshots.pop(self.target, None)

    def result_fields(self):
        result = {'scope': self.tool.coverage()} if self.directory else {}
        if self.output_path is not None:
            result['artifacts'] = copy.deepcopy(self.artifacts)
        return result

    def validate(self, content):
        answer = validate_answer(content, self.snapshots, self.target, self.read_attempted,
                                 coverage=self.tool.coverage() if self.directory else None,
                                 task_failure=bool(self.output_path and self.write_failure and not self.artifacts))
        if self.output_path is not None and answer['status'] != 'unable' and not self.artifacts:
            raise AnswerError('OUTPUT_NOT_CREATED', 'The requested output has no actual creation receipt.')
        return answer

    @staticmethod
    def repair_prompt(code):
        return IDENTIFIER_REPAIR if code == 'IDENTIFIER_MISMATCH' else JSON_REPAIR

    def execute_read(self, name, arguments):
        if self.output_path is not None and arguments.get('path') == self.output_path:
            return tool_error('PATH_DENIED')
        result = self.tool.execute(name, arguments) if self.directory else self.tool.execute(arguments)
        if (self.output_path is not None and name == 'list_files'
                and isinstance(result, dict) and result.get('ok') is True
                and isinstance(result.get('entries'), list)
                and all(isinstance(item, dict) and isinstance(item.get('path'), str)
                        for item in result['entries'])):
            result = {**result, 'entries': [item for item in result['entries']
                                          if item['path'] != self.output_path]}
        return result

    def clear_read_proof(self, name):
        if self.directory:
            self.tool.clear_result_proof(name)
        elif hasattr(self.tool, 'clear_result_proof'):
            self.tool.clear_result_proof()

    def take_read_proof(self, name, arguments):
        proof = (self.tool.take_result_proof(name, arguments) if self.directory else
                 self.tool.take_result_proof(arguments)
                 if hasattr(self.tool, 'take_result_proof') else None)
        if (proof is not None and self.output_path is not None and name == 'list_files'
                and proof.get('ok') is True and isinstance(proof.get('entries'), list)):
            proof = {**proof, 'entries': [item for item in proof['entries']
                                         if item.get('path') != self.output_path]}
        return proof

    def model_request(self, messages, limit, schemas):
        return model_request(messages, limit, schemas)


def adapt_tools(tool, output_path=None):
    if isinstance(tool, ToolRuntime):
        return tool
    policy = FilePolicy(tool, output_path)
    if policy.directory:
        adapters = []
        for schema in tool.schemas:
            name = schema['function']['name']
            verifier = _verify_list_success if name == 'list_files' else _verify_read_success
            errors = LIST_ERROR_CODES if name == 'list_files' else DIRECTORY_READ_ERROR_CODES
            adapters.append(FileAdapter(schema,
                lambda args, name=name: policy.execute_read(name, args), verifier, errors,
                lambda name=name: policy.clear_read_proof(name),
                lambda args, name=name: policy.take_read_proof(name, args)))
    else:
        adapters = [FileAdapter(READ_FILE_SCHEMA, lambda args: policy.execute_read('read_file', args),
                                lambda args, data: _verify_read_success(
                                    args, data, getattr(tool, 'max_bytes', 32768)),
                                READ_ERROR_CODES,
                                lambda: policy.clear_read_proof('read_file'),
                                lambda args: policy.take_read_proof('read_file', args))]
    if output_path is not None:
        from .write_file import WriteFile
        adapters.append(WriteFile(tool.workspace, output_path, tool.workspace_identity))
    return ToolRuntime(ToolRegistry(adapters, summary=policy.result_fields), policy)


WRITE_TASK = '''\n用户还要求生成 output_file 指定的新文件。先读取资料，依据原文生成报告，再调用 write_file。
write_file 只允许这个精确路径，不会覆盖或自动更名。只支持 UTF-8 .md/.txt，正文最多 32768 字节，一次任务最多创建一个文件。
调用时提供完整 content 和 intent；程序会等待用户查看实际路径及完整正文后确认。不要自行宣称已获批准。
只有工具返回 ok=true 且 data.operation=created 才实际生成了文件；最终回答仍须引用已读取的原文，产物不能作为来源。
写入失败或被拒绝时可返回 unable（即使资料读取成功），仅解释失败原因，不声称已生成文件。拒绝后不得再调用任何工具。
'''
