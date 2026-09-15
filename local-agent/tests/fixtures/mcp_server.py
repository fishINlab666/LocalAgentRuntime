"""Deterministic adversarial stdio peer. Test data only; no filesystem access."""
import json
import os
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else 'normal'
name = sys.argv[2] if len(sys.argv) > 2 else 'status'
input_schema = {'type': 'object', 'properties': {'intent': {'type': 'string'},
    'items': {'type': 'array', 'items': {'type': 'integer'}}},
    'required': ['intent', 'items'], 'additionalProperties': False}
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method, params = request['method'], request.get('params', {})
    result = {}
    if method == 'initialize':
        result = {'protocolVersion': '2025-11-25', 'capabilities': {
            'tools': {}, 'resources': {}, 'prompts': {}},
            'serverInfo': {'name': 'synthetic-project', 'version': '1.0'}}
    elif method == 'tools/list':
        if params.get('cursor'):
            result = {'tools': [{'name': 'plain', 'description': 'plain text', 'inputSchema': {'type': 'object'}},
                                {'name': 'write_remote', 'inputSchema': {'type': 'object'}}]}
        else:
            result = {'tools': [{'name': name, 'description': 'Synthetic project status', 'inputSchema': input_schema,
                'outputSchema': {'type': 'object', 'properties': {'project': {'type': 'string'}, 'count': {'type': 'integer'}},
                    'required': ['project', 'count'], 'additionalProperties': False}}], 'nextCursor': 'second'}
        if mode == 'cursor_loop':
            result['nextCursor'] = 'second'
    elif method == 'resources/list':
        result = {'resources': [{'uri': 'file:///not-a-local-file.txt', 'name': 'project', 'mimeType': 'text/plain'}]}
    elif method == 'prompts/list':
        result = {'prompts': [{'name': 'brief', 'description': 'Brief template', 'arguments': [{'name': 'audience', 'required': True}]}]}
    elif method == 'tools/call':
        if mode == 'disconnect':
            sys.exit(0)
        if mode in {'hang', 'late'}:
            time.sleep(2 if mode == 'late' else 20)
        if mode == 'error':
            result = {'isError': True, 'content': [{'type': 'text', 'text': 'Unknown project'}]}
        elif mode == 'bad_output':
            result = {'content': [], 'structuredContent': {'project': 7, 'count': 'bad'}}
        elif mode == 'oversize':
            result = {'content': [{'type': 'text', 'text': 'x' * 200000}]}
        elif params['name'] == 'plain':
            result = {'content': [{'type': 'text', 'text': '合成项目 青禾-47 已完成 3 项。'}]}
        else:
            result = {'content': [{'type': 'text', 'text': json.dumps(params['arguments'], ensure_ascii=False)}],
                      'structuredContent': {'project': '青禾-47', 'count': sum(params['arguments']['items'])}}
    elif method == 'resources/read':
        result = {'contents': [{'uri': params['uri'], 'mimeType': 'text/plain', 'text': '项目：青禾-47\n状态：完成'}]}
    elif method == 'prompts/get':
        result = {'description': 'Synthetic template', 'messages': [{'role': 'user', 'content': {
            'type': 'text', 'text': '请为 ' + params['arguments']['audience'] + ' 写一段带来源的项目简报。'}}]}
    response_id = request['id']
    if mode == 'wrong_id' and method == 'tools/call':
        response_id += 100
    if mode == 'protocol_error' and method == 'tools/call':
        response = {'jsonrpc': '2.0', 'id': response_id, 'error': {'code': -32602, 'message': 'Wrong arguments'}}
    else:
        response = {'jsonrpc': '2.0', 'id': response_id, 'result': result}
    print(json.dumps(response, ensure_ascii=False), flush=True)
