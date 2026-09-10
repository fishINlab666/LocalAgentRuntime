"""Synthetic provider for tool-layer tests; never contacts a model service."""

import copy
import json

from local_agent.provider import ModelReply, ProviderError


class ReportProvider:
    metadata = {'provider': 'scripted_report', 'model': 'none', 'simulated': True}

    def __init__(self):
        self.requests = []

    def complete(self, messages, tools, timeout):
        self.requests.append(copy.deepcopy(messages))
        task = json.loads(messages[1]['content'])
        results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        requested = [call for m in messages if m['role'] == 'assistant' for call in m.get('tool_calls', [])]
        writes = {call['id'] for call in requested if call['function']['name'] == 'write_file'}
        wrote = [json.loads(m['content']) for m in messages if m['role'] == 'tool' and m['tool_call_id'] in writes]
        readings = [r['data'] for r in results if r.get('ok') and 'content' in r.get('data', {})]
        if wrote:
            if '产物后失败' in task['question']:
                raise ProviderError('NETWORK_ERROR')
            success = wrote[-1]['ok']
            value = {'status': 'answered' if success else 'unable',
                     'answer': '已依据本次读取资料生成报告。' if success else '用户拒绝生成报告，未创建文件。',
                     'citations': [{'path': r['path'], 'start_line': 1, 'end_line': 1}
                                   for r in readings] if success else []}
            return ModelReply({'role': 'assistant', 'content': json.dumps(value, ensure_ascii=False)})
        if not results:
            calls = [('list_files', {'path': '.', 'intent': '发现相关资料'})] if 'directory' in task else [
                ('read_file', {'path': task['file'], 'intent': '读取用户选择的资料'})]
        elif not readings:
            entries = results[-1]['data']['entries']
            calls = [('read_file', {'path': entry['path'], 'intent': '读取资料以生成报告'})
                     for entry in entries if entry['type'] == 'file'][:2]
        else:
            sections = []
            for reading in readings:
                body = reading['content']
                text = '\n'.join(body.values()) if isinstance(body, dict) else body
                sections.append('来源：' + reading['path'] + '\n' + text)
            content = '# 核对报告\n\n' + '\n\n'.join(sections) + '\n'
            calls = [('write_file', {'path': task['output_file'], 'content': content,
                                      'intent': '将核对结果保存为指定报告'})]
        return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': f'call-{len(messages)}-{index}', 'type': 'function',
             'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}
            for index, (name, arguments) in enumerate(calls)]})
