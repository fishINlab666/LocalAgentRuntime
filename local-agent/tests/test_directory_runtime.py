import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from local_agent.discovery import DirectoryTools
from local_agent.runtime import Runtime, RunConfig
from local_agent.trace import Trace
from test_runtime import ScriptedProvider, call_message


def listing(call_id='list', path='.'):
    return call_message(call_id, path, 'list_files')


def reads(*paths):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        call_message(f'read-{i}', path)['tool_calls'][0] for i, path in enumerate(paths)]}


def final(status='answered', paths=('plan.md', 'review.txt')):
    return {'role': 'assistant', 'content': json.dumps({
        'status': status, 'answer': '杉木-19，由顾宁评审。' if status == 'answered' else '资料未说明或无法读取。',
        'citations': [{'path': path, 'start_line': 1, 'end_line': 1} for path in paths]
        if status == 'answered' else []}, ensure_ascii=False)}


class DirectoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'plan.md').write_text('项目代号：杉木-19\n')
        (self.workspace / 'review.txt').write_text('评审人：顾宁\n')

    def run_script(self, replies, tool=None, config=None):
        provider = ScriptedProvider(replies)
        trace = Trace(self.root / 'runs', self.workspace, debug_content=True)
        runtime = Runtime(provider, tool or DirectoryTools(self.workspace), trace, config)
        result = runtime.run('项目代号和评审人是什么？', None)
        events = [json.loads(line) for line in trace.path.read_text().splitlines()]
        return result, provider, events

    def test_list_then_two_reads_return_exact_context_and_evidence(self):
        result, provider, events = self.run_script([listing(), reads('plan.md', 'review.txt'), final()])
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['scope']['complete'])
        initial = json.dumps(provider.requests[0], ensure_ascii=False)
        for value in ['plan.md', 'review.txt', '杉木-19', '顾宁']:
            self.assertNotIn(value, initial)
        self.assertEqual(json.loads(provider.requests[0][1]['content']),
                         {'directory': '.', 'question': '项目代号和评审人是什么？'})
        listed = json.loads(provider.requests[1][-1]['content'])
        self.assertEqual(provider.requests[1][-1]['tool_call_id'], 'list')
        self.assertEqual([entry['path'] for entry in listed['data']['entries']], ['plan.md', 'review.txt'])
        self.assertNotIn('杉木', json.dumps(listed, ensure_ascii=False))
        self.assertFalse(listed['scope']['complete'])
        self.assertEqual([m['role'] for m in provider.requests[2]],
                         ['system', 'user', 'assistant', 'tool', 'assistant', 'tool', 'tool'])
        self.assertEqual([m['tool_call_id'] for m in provider.requests[2][-2:]], ['read-0', 'read-1'])
        returned = [json.loads(m['content']) for m in provider.requests[2][-2:]]
        self.assertEqual([m['data']['content'] for m in returned], [{'1': '项目代号：杉木-19'}, {'1': '评审人：顾宁'}])
        self.assertTrue(returned[-1]['scope']['complete'])
        self.assertEqual([c['quote'] for c in result['answer']['citations']], ['项目代号：杉木-19', '评审人：顾宁'])
        requested = [e['data'] for e in events if e['event'] == 'model.requested']
        self.assertEqual(requested[-1]['messages'], provider.requests[-1])
        self.assertEqual([t['function']['name'] for t in requested[0]['tools']], ['list_files', 'read_file'])

    def test_undiscovered_read_error_returns_to_model_and_can_recover(self):
        result, provider, _ = self.run_script([
            call_message('denied', 'plan.md'), listing(), reads('plan.md', 'review.txt'), final()])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(provider.requests[1][-1]['tool_call_id'], 'denied')
        self.assertEqual(json.loads(provider.requests[1][-1]['content'])['error']['code'], 'PATH_NOT_DISCOVERED')

    def test_listing_alone_cannot_answer_and_partial_search_cannot_deny(self):
        for replies, code in [([final()], 'MISSING_READ'),
                              ([listing(), final()], 'MISSING_READ'),
                              ([listing(), reads('plan.md'), final('not_found')], 'INCOMPLETE_SEARCH')]:
            with self.subTest(code=code):
                result, _, _ = self.run_script(replies)
                self.assertEqual(result['stop_reason'], code)
                self.assertIsNone(result['answer'])

    def test_complete_scope_allows_missing_information(self):
        result, _, _ = self.run_script([listing(), reads('plan.md', 'review.txt'), final('not_found')])
        self.assertEqual(result['answer']['status'], 'not_found')
        self.assertTrue(result['scope']['complete'])

    def test_unlisted_child_blocks_not_found(self):
        (self.workspace / 'notes').mkdir()
        result, _, _ = self.run_script([listing(), reads('plan.md', 'review.txt'), final('not_found')])
        self.assertEqual(result['stop_reason'], 'INCOMPLETE_SEARCH')
        self.assertEqual(result['scope']['unlisted_directories'], ['notes'])

    def test_empty_scope_can_report_no_supported_files(self):
        for path in self.workspace.iterdir():
            path.unlink()
        result, _, _ = self.run_script([listing(), final('not_found')])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['scope']['read_files'], [])

    def test_failed_reread_revokes_earlier_snapshot(self):
        def remove_then_reread(_):
            (self.workspace / 'review.txt').unlink()
            return call_message('again', 'review.txt')
        result, provider, _ = self.run_script([
            listing(), reads('plan.md', 'review.txt'), remove_then_reread, final()])
        self.assertEqual(result['stop_reason'], 'INVALID_CITATION')
        self.assertEqual(result['scope']['read_files'], ['plan.md'])
        self.assertFalse(json.loads(provider.requests[-1][-1]['content'])['ok'])

    def test_actual_read_error_allows_unable_with_partial_reads(self):
        (self.workspace / 'review.txt').write_bytes(b'\xff')
        result, _, _ = self.run_script([listing(), reads('plan.md', 'review.txt'), final('unable')])
        self.assertEqual(result['state'], 'unable')
        self.assertTrue(result['scope']['had_error'])
        self.assertFalse(result['scope']['complete'])

    def test_timeout_result_never_grants_discovery_permission(self):
        class SlowTools(DirectoryTools):
            def execute(self, name, arguments):
                result = super().execute(name, arguments)
                time.sleep(.08)
                return result
        tool = SlowTools(self.workspace)
        result, _, _ = self.run_script([listing()], tool=tool, config=RunConfig(tool_timeout=.02))
        self.assertEqual(result['stop_reason'], 'TOOL_TIMEOUT')
        time.sleep(.1)
        self.assertEqual(tool.coverage()['discovered_files'], [])
        self.assertEqual(result['scope']['discovered_files'], [])

    def test_cancelled_run_does_not_start_model(self):
        provider = ScriptedProvider([])
        cancel = threading.Event()
        cancel.set()
        result = Runtime(provider, DirectoryTools(self.workspace),
                         Trace(self.root / 'runs', self.workspace)).run('问题', None, cancel)
        self.assertEqual(result['stop_reason'], 'CANCELLED')
        self.assertEqual(provider.requests, [])


if __name__ == '__main__':
    unittest.main()
