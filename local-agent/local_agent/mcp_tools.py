"""Read-only MCP adapters preserving the host tool and receipt contracts."""

import copy
import hashlib
import json
import os
import re

from .mcp_client import MCPClient, MCPFailure
from .schema_validation import check_schema, valid_instance
from .tool_runtime import ToolSpec, ToolRegistry, input_schema, tool_error


_ERRORS = frozenset({'MCP_TOOL_ERROR', 'MCP_PROTOCOL_ERROR', 'MCP_DISCONNECTED',
    'MCP_OUTPUT_INVALID', 'TOOL_TIMEOUT', 'CANCELLED', 'RUN_TIMEOUT', 'TOOL_CALL_LIMIT',
    'TOOL_RESULT_TOO_LARGE', 'INVALID_ARGUMENT', 'PATH_DENIED'})
_USER_ERRORS = _ERRORS - {'MCP_TOOL_ERROR', 'INVALID_ARGUMENT', 'PATH_DENIED', 'TOOL_CALL_LIMIT'}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def _model_name(server_id, kind, name):
    identity = json.dumps([server_id, kind, name], ensure_ascii=False)
    prefix = re.sub('[^a-zA-Z0-9_]', '_', f'mcp_{server_id}_{kind}_{name}')[:51]
    return prefix + '_' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]


def _config(config):
    if not isinstance(config, dict):
        raise ValueError('MCP configuration must be an object')
    config = copy.deepcopy(config)
    if config.get('transport', 'stdio') != 'stdio':
        raise ValueError('Only installed stdio servers are supported')
    for key in ('server_id', 'command'):
        if not isinstance(config.get(key), str) or not config[key].strip() or '\x00' in config[key]:
            raise ValueError('Invalid MCP ' + key)
    if not re.fullmatch('[a-zA-Z0-9_-]{1,64}', config['server_id']):
        raise ValueError('Invalid MCP server_id')
    for key in ('args', 'allowed_tools', 'allowed_resources', 'allowed_prompts', 'env_names'):
        value = config.setdefault(key, [])
        if (not isinstance(value, list) or any(not isinstance(x, str) or '\x00' in x for x in value)
                or (key != 'args' and len(set(value)) != len(value))):
            raise ValueError('Invalid MCP ' + key)
    if config.get('cwd') is not None and not os.path.isdir(config['cwd']):
        raise ValueError('MCP working directory does not exist')
    env = config.get('env', {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str)
            or '\x00' in k + v or '=' in k for k, v in env.items()):
        raise ValueError('Invalid MCP environment')
    config['env'] = {**{key: os.environ[key] for key in config['env_names'] if key in os.environ}, **env}
    timeout = config.setdefault('timeout_seconds', 5)
    if type(timeout) not in (int, float) or not 0 < timeout <= 30:
        raise ValueError('Invalid MCP timeout')
    return config


class MCPToolSet:
    def __init__(self, config, *, run_id, control=None):
        self.config = _config(config)
        self.client = MCPClient(self.config, run_id=run_id, control=control)
        self.tools = []
        self.catalog = {}
        self._selected_prompt = None
        try:
            self._discover()
            ToolRegistry(self.tools)
            if _size([{'description': t.spec.description, 'schema': input_schema(t.spec)} for t in self.tools]) > 16384:
                raise ValueError('MCP capability definitions exceed 16 KiB')
        except Exception:
            self.close()
            raise

    def _discover(self):
        total_bytes = 0
        for kind, identity in (('tools', 'name'), ('resources', 'uri'), ('prompts', 'name')):
            rows, cursor, seen_cursors, seen_names = [], None, set(), set()
            if kind not in self.client.capabilities:
                self.catalog[kind] = []
                continue
            for _ in range(8):
                result = self.client.request(kind + '/list', {'cursor': cursor} if cursor else {})['result']
                page = result.get(kind)
                if not isinstance(page, list):
                    raise ValueError('Invalid MCP discovery page')
                total_bytes += _size(result)
                if total_bytes > 16384 or len(rows) + len(page) > 128:
                    raise ValueError('MCP discovery exceeds the capability budget')
                for item in page:
                    key = item.get(identity)
                    if not isinstance(key, str) or not key or key in seen_names:
                        raise ValueError('MCP discovery contains duplicate or invalid names')
                    seen_names.add(key)
                    row = copy.deepcopy(item)
                    row['enabled'] = key in self.config['allowed_' + kind]
                    if kind == 'tools' and row['enabled']:
                        try:
                            check_schema(row['inputSchema'])
                            if row['inputSchema'].get('type') != 'object':
                                raise ValueError('Tool arguments must be an object')
                            if row.get('outputSchema') is not None:
                                check_schema(row['outputSchema'])
                        except ValueError as error:
                            row['enabled'], row['reason'] = False, str(error)
                    elif kind == 'resources' and row['enabled'] and not _text_mime(row.get('mimeType')):
                        row['enabled'], row['reason'] = False, 'Only text resources are supported'
                    rows.append(row)
                cursor = result.get('nextCursor')
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                    raise ValueError('MCP discovery cursor repeats or is invalid')
                seen_cursors.add(cursor)
            else:
                raise ValueError('MCP discovery exceeds the page budget')
            self.catalog[kind] = rows
        for row in self.catalog['tools']:
            if row['enabled']:
                self.tools.append(MCPAdapter(self, 'tool', row['name'], row['inputSchema'],
                                            row.get('description', ''), row.get('outputSchema')))
        resources = [r for r in self.catalog['resources'] if r['enabled']]
        if resources:
            self.tools.append(MCPAdapter(self, 'resource', 'read_resource', {
                'type': 'object', 'properties': {'uri': {'type': 'string', 'enum': [r['uri'] for r in resources]}},
                'required': ['uri'], 'additionalProperties': False},
                'Read a configured MCP text resource. ' + json.dumps(resources, ensure_ascii=False)))
        prompts = [p for p in self.catalog['prompts'] if p['enabled']]
        if prompts:
            self.tools.append(MCPAdapter(self, 'prompt', 'get_prompt', {
                'type': 'object', 'properties': {
                    'name': {'type': 'string', 'enum': [p['name'] for p in prompts]},
                    'arguments': {'type': 'object', 'additionalProperties': {'type': 'string'}}},
                'required': ['name', 'arguments'], 'additionalProperties': False},
                'Read only the template explicitly selected by the user for this run. '
                + json.dumps(prompts, ensure_ascii=False)))

    def select_prompt(self, name):
        if name is not None and not any(p['name'] == name and p['enabled'] for p in self.catalog['prompts']):
            raise ValueError('MCP prompt is not allowed')
        self._selected_prompt = name

    def close(self):
        self.client.close()


def _text_mime(mime):
    return mime is None or (isinstance(mime, str) and
        (mime.startswith('text/') or mime.split(';', 1)[0] in {'application/json', 'application/xml'}))


def _text(value):
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('Unsupported MCP text')
    value.encode('utf-8')
    return value


class MCPAdapter:
    def __init__(self, owner, kind, remote_name, schema, description, output_schema=None):
        self.owner, self.kind, self.remote_name = owner, kind, remote_name
        self.server_id = owner.client.server_id
        self.output_schema = copy.deepcopy(output_schema)
        self.spec = ToolSpec(_model_name(self.server_id, kind, remote_name),
            f'MCP {self.server_id}/{remote_name}: {description}', check_schema(schema), source='mcp',
            timeout_seconds=owner.client.timeout_seconds, max_result_bytes=12288,
            error_codes=_ERRORS, user_error_codes=_USER_ERRORS)
        self._call_id = self._proven = None

    def result_fields(self):
        return {}

    def cancel(self):
        self._call_id = self._proven = None
        self.owner.close()

    def bind_call(self, call_id):
        if not isinstance(call_id, str) or not call_id:
            raise ValueError('Missing host invocation ID')
        self._call_id, self._proven = call_id, None

    def validate(self, values):
        arguments = values.get('arguments')
        if not valid_instance(self.spec.input_schema, arguments):
            return tool_error('INVALID_ARGUMENT')
        if self.kind == 'prompt':
            if self.owner._selected_prompt != arguments['name']:
                return tool_error('PATH_DENIED')
            row = next(p for p in self.owner.catalog['prompts'] if p['name'] == arguments['name'])
            rules = {p['name']: p for p in row.get('arguments', [])}
            if (set(arguments['arguments']) - rules.keys()
                    or any(p.get('required') and name not in arguments['arguments'] for name, p in rules.items())):
                return tool_error('INVALID_ARGUMENT')
        return None

    def preview(self, values):
        return {'action_summary': f'从 MCP {self.server_id} 获取 {self.remote_name} 的返回内容。'}

    def verify_preview(self, values, preview):
        if preview != self.preview(values):
            raise ValueError('Invalid MCP preview')
        return preview

    def execute(self, values):
        self._proven = None
        error = self.validate(values)
        if error:
            return error
        if not self._call_id:
            return tool_error('MCP_PROTOCOL_ERROR')
        arguments = copy.deepcopy(values['arguments'])
        method = {'tool': 'tools/call', 'resource': 'resources/read', 'prompt': 'prompts/get'}[self.kind]
        params = {'name': self.remote_name, 'arguments': arguments} if self.kind == 'tool' else arguments
        try:
            response = self.owner.client.request(method, params, call_id=self._call_id)
            raw = response['result']
            if self.kind == 'tool' and raw.get('isError') is True:
                return tool_error('MCP_TOOL_ERROR', 'MCP server reported a tool error')
            content, structured = self._content(raw, arguments)
            receipt = response['receipt']
            if not self.owner.client.verify_receipt(receipt, raw):
                raise ValueError('Unverified MCP response')
            data = {'source_id': 'mcp:' + self._call_id,
                'source': {'type': 'mcp', 'server_id': self.server_id,
                    'run_id': self.owner.client.run_id, 'call_id': self._call_id, 'method': method,
                    'evidence_role': 'method' if self.kind == 'prompt' else 'fact'},
                'source_kind': 'mcp_' + self.kind, 'server_id': self.server_id,
                'remote_name': self.remote_name, 'content': content,
                'structured_content': structured, 'raw_result': raw, 'receipt': receipt,
                'snapshot_id': hashlib.sha256((receipt['connection_id'] + ':' + str(receipt['request_id'])
                                              + ':' + receipt['sha256']).encode()).hexdigest(),
                'verification': 'received_from_mcp_server'}
            if _size({'ok': True, 'data': data}) > self.spec.max_result_bytes:
                return tool_error('TOOL_RESULT_TOO_LARGE')
            self._proven = (copy.deepcopy(values), copy.deepcopy(data))
            return {'ok': True, **data}
        except MCPFailure as error:
            return tool_error(error.code)
        except Exception:
            return tool_error('MCP_OUTPUT_INVALID')

    def _content(self, raw, arguments):
        structured = None
        if self.kind == 'tool':
            if type(raw.get('isError', False)) is not bool or not isinstance(raw.get('content'), list):
                raise ValueError('Invalid tool content')
            text = []
            for block in raw['content']:
                if not isinstance(block, dict) or block.get('type') != 'text':
                    raise ValueError('Only MCP text content is supported')
                text.append(_text(block.get('text')))
            structured = raw.get('structuredContent')
            if structured is not None and not isinstance(structured, dict):
                raise ValueError('Invalid structured content')
            if self.output_schema is not None and not valid_instance(self.output_schema, structured):
                raise ValueError('Output does not match declared schema')
            if not text and structured is None:
                raise ValueError('Empty MCP success')
            if structured is not None and not text:
                text = [json.dumps(structured, sort_keys=True, ensure_ascii=False, allow_nan=False)]
        elif self.kind == 'resource':
            if not isinstance(raw.get('contents'), list) or not raw['contents']:
                raise ValueError('Invalid resource content')
            text = []
            for block in raw['contents']:
                if (block.get('uri') != arguments['uri'] or 'blob' in block or not _text_mime(block.get('mimeType'))):
                    raise ValueError('Unsupported resource response')
                text.append(_text(block.get('text')))
        else:
            if not isinstance(raw.get('messages'), list) or not raw['messages']:
                raise ValueError('Invalid prompt content')
            text = []
            for message in raw['messages']:
                block = message.get('content', {})
                if message.get('role') not in {'user', 'assistant'} or block.get('type') != 'text':
                    raise ValueError('Unsupported prompt content')
                text.append('[' + message['role'] + ']\n' + _text(block.get('text')))
        return '\n'.join(text), copy.deepcopy(structured)

    def verify_success(self, values, data):
        proven, self._proven = self._proven, None
        if proven != (values, data) or data['receipt']['call_id'] != self._call_id:
            raise ValueError('MCP result did not originate in this invocation')
        if not self.owner.client.verify_receipt(data['receipt'], data['raw_result']):
            raise ValueError('Invalid MCP receipt')
        return copy.deepcopy(data)


def build_mcp_tools(config, *, run_id, control=None):
    return MCPToolSet(config, run_id=run_id, control=control)
