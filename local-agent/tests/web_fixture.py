"""Synthetic, local-only browser test server. Never calls a cloud provider."""

import argparse
import json
from pathlib import Path
import tempfile
import time

from local_agent.demo import DemoProvider
from local_agent.provider import ModelReply, ProviderError
from local_agent.web import serve
from report_provider import ReportProvider


class BrowserProvider(DemoProvider):
    def complete(self, messages, tools, timeout):
        task = json.loads(messages[1]['content'])
        question = task['question']
        if task.get('output_file'):
            if not hasattr(self, 'report_provider'):
                self.report_provider = ReportProvider()
            return self.report_provider.complete(messages, tools, timeout)
        if '慢速' in question:
            time.sleep(1.5)
        if '网络失败' in question:
            raise ProviderError('NETWORK_ERROR')
        if '标识纠错' in question and any(message['role'] == 'tool' for message in messages):
            corrected = '成功' in question and messages[-1]['role'] == 'user'
            return ModelReply({'role': 'assistant', 'content': json.dumps({
                'status': 'answered', 'answer': '项目代号为浏览器' + ('-' if corrected else '—') + '481。',
                'citations': [{'path': 'demo-note.md', 'start_line': 1, 'end_line': 1}]}, ensure_ascii=False)})
        if '先读错' in question:
            if messages[-1]['role'] == 'tool':
                raise ProviderError('AUTH_ERROR')
            return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'wrong_file', 'type': 'function', 'function': {'name': 'read_file',
                    'arguments': json.dumps({'path': 'other.md', 'intent': '验证读取失败'})}}]})
        if messages[-1]['role'] == 'tool' and '预算' in question:
            return ModelReply({'role': 'assistant', 'content': json.dumps({
                'status': 'not_found', 'answer': '文件未说明预算。', 'citations': []})})
        return super().complete(messages, tools, timeout)


def missing_provider():
    raise ProviderError('CONFIG_MISSING')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--missing', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='web-browser-test-') as tmp:
        root = Path(tmp)
        workspace = root / 'workspace'
        workspace.mkdir()
        (workspace / 'demo-note.md').write_text('项目代号：浏览器-481\n评审人：林澄\n<script>window.injected = true</script>\n')
        (workspace / 'review.md').write_text('演示日期：2026-09-10\n')
        serve(workspace, root / 'runs', port=args.port,
              provider_factory=missing_provider if args.missing else BrowserProvider)
