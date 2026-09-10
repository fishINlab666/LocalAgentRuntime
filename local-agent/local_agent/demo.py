"""A visibly simulated provider for demonstrating the runtime without a key."""

import json
from .provider import ModelReply


class DemoProvider:
    metadata = {'provider': 'scripted_demo', 'model': 'none', 'simulated': True}

    def complete(self, messages: list[dict], tools: list[dict], timeout: float) -> ModelReply:
        task = json.loads(messages[1]['content'])
        if 'directory' in task:
            return self._directory_reply(messages)
        if messages[-1]['role'] != 'tool':
            return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'demo_read', 'type': 'function', 'function': {'name': 'read_file',
                    'arguments': json.dumps({'path': task['file'], 'intent': '读取演示资料'})}}]})
        result = json.loads(messages[-1]['content'])
        if result['ok']:
            content = result['data']['content']
            lines = list(content.values()) if isinstance(content, dict) else content.splitlines()
            answer = {'status': 'answered', 'answer': '\n'.join(lines), 'citations': [{
                'path': task['file'], 'start_line': 1, 'end_line': len(lines)}]}
        else:
            answer = {'status': 'unable', 'answer': '无法读取演示文件。', 'citations': []}
        return ModelReply({'role': 'assistant', 'content': json.dumps(answer, ensure_ascii=False)})

    def _directory_reply(self, messages: list[dict]) -> ModelReply:
        results = [json.loads(message['content']) for message in messages if message['role'] == 'tool']
        calls = []
        if not results:
            calls = [('list_files', '.')]
        elif any(not result['ok'] for result in results):
            answer = {'status': 'unable', 'answer': '模拟演示：目录或资料读取失败，无法合并内容。', 'citations': []}
        else:
            scope = results[-1]['scope']
            if scope['unlisted_directories']:
                calls = [('list_files', scope['unlisted_directories'][0])]
            elif scope['unread_files']:
                calls = [('read_file', path) for path in scope['unread_files'][:4]]
            else:
                current = {result['data']['path']: result['data']['content'] for result in results
                           if 'content' in result['data'] and result['data']['path'] in scope['read_files']}
                sections, citations = [], []
                for path, content in current.items():
                    lines = list(content.values()) if isinstance(content, dict) else content.splitlines()
                    if not lines or not any(line.strip() for line in lines):
                        continue
                    sections.append(path + '\n' + '\n'.join(lines))
                    citations.append({'path': path, 'start_line': 1, 'end_line': len(lines)})
                answer = {'status': 'answered' if sections else 'not_found',
                          'answer': '模拟演示：按原文合并已读资料，未理解问题。\n' + '\n\n'.join(sections)
                                    if sections else '模拟演示：已检查的目录没有可展示的文本内容。',
                          'citations': citations}
        if calls:
            return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'demo_{len(messages)}_{index}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps({'path': path, 'intent': '发现并读取演示资料'})}}
                for index, (name, path) in enumerate(calls)]})
        return ModelReply({'role': 'assistant', 'content': json.dumps(answer, ensure_ascii=False)})
