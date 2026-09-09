import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from local_agent.demo import DemoProvider
from local_agent.provider import ProviderError


class WebTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_agent.web'), 'Local web entry is missing')
        from local_agent.web import create_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'note.md').write_text('代号：test-481\n<script>alert(1)</script>\n')
        self.factory = DemoProvider
        self.server = create_server(self.workspace, self.root / 'runs', port=0,
                                    provider_factory=lambda: self.factory())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, data=None, *, auth=True, origin=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        body = json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
        request_headers = {'Content-Type': 'application/json'}
        if auth:
            request_headers['X-Session-Token'] = self.server.token
        if method == 'POST':
            request_headers['Origin'] = origin or self.server.origin
        request_headers.update(headers or {})
        conn.request(method, path, body, request_headers)
        response = conn.getresponse()
        status, response_headers, raw = response.status, dict(response.getheaders()), response.read()
        conn.close()
        value = json.loads(raw) if 'application/json' in response_headers.get('Content-Type', '') else raw.decode()
        return status, response_headers, value

    def start_run(self, **fields):
        status, _, value = self.request('POST', '/api/runs', {'file': 'note.md', 'question': '读取代号', **fields})
        self.assertEqual(status, 202, value)
        return value['id']

    def wait_run(self, run_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, value = self.request('GET', '/api/runs/' + run_id)
            self.assertEqual(status, 200)
            if value['result'] is not None:
                return value
            time.sleep(.01)
        self.fail('Run never reached a terminal state')

    def test_page_and_static_assets_are_served_with_browser_guards(self):
        status, headers, page = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(self.server.token, page)
        self.assertIn('单文件问答', page)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for path in ('/app.js', '/app.css'):
            self.assertEqual(self.request('GET', path)[0], 200)
        for path in ('/../provider.py', '/runs/log.jsonl', '/favicon.ico'):
            self.assertEqual(self.request('GET', path)[0], 404)

    def test_real_runtime_round_trip_and_redacted_event_display(self):
        value = self.wait_run(self.start_run())
        self.assertEqual(value['mode'], 'file')
        self.assertEqual(value['state'], 'completed')
        self.assertIn('test-481', value['result']['answer']['answer'])
        self.assertEqual(value['result']['model_calls'], 2)
        names = [event['event'] for event in value['events']]
        self.assertIn('tool.completed', names)
        self.assertEqual(names[-1], 'run.ended')
        self.assertNotIn('test-481', json.dumps(value['events']))
        log = Path(value['result']['trace_path']).read_text()
        self.assertNotIn('test-481', log)
        self.assertEqual(self.request('GET', '/api/config')[2]['latest_run_id'], value['id'])

    def test_missing_file_is_displayed_as_unable(self):
        value = self.wait_run(self.start_run(file='missing.md'))
        self.assertEqual(value['state'], 'unable')
        self.assertEqual(value['result']['answer']['citations'], [])
        self.assertIn('FILE_NOT_FOUND', json.dumps(value['events']))

    def test_directory_round_trip_reads_two_files_and_keeps_progress_redacted(self):
        (self.workspace / 'review.txt').write_text('评审人：顾青\n演示日期：2026-09-10\n')
        status, _, job = self.request('POST', '/api/runs', {'mode': 'directory', 'question': '合并资料'})
        self.assertEqual(status, 202, job)
        value = self.wait_run(job['id'])
        self.assertEqual(value['mode'], 'directory')
        self.assertIsNone(value['file'])
        self.assertEqual(value['state'], 'completed', value['result'])
        self.assertEqual({c['path'] for c in value['result']['answer']['citations']}, {'note.md', 'review.txt'})
        self.assertIn('顾青', value['result']['answer']['answer'])
        self.assertTrue(value['result']['scope']['complete'])
        requested = [e['detail']['name'] for e in value['events'] if e['event'] == 'tool.requested']
        self.assertEqual(requested, ['list_files', 'read_file', 'read_file'])
        for text in (json.dumps(value['events']), Path(value['result']['trace_path']).read_text()):
            self.assertNotIn('test-481', text)
            self.assertNotIn('顾青', text)

    def test_directory_payload_is_strict_and_unicode_validated(self):
        from local_agent.web_runs import WebError, WebRuns
        self.assertEqual(WebRuns.validate({'mode': 'directory', 'question': ' 问题 '}), (None, '问题'))
        calls = []
        self.factory = lambda: calls.append(True)
        for fields in ({'mode': 'file'}, {'mode': 'other'}, {'mode': None}, {'file': 'note.md'},
                       {'workspace': '/tmp'}, {'extra': True}, {'question': ' '}, {'question': '问' * 4001}):
            with self.subTest(fields=str(fields)[:80]):
                status, _, value = self.request('POST', '/api/runs', {'mode': 'directory', 'question': '问', **fields})
                self.assertEqual(status, 400, value)
        with self.assertRaises(WebError) as invalid:
            WebRuns.validate({'mode': 'directory', 'question': '\ud800'})
        self.assertEqual(invalid.exception.code, 'INVALID_TASK')
        self.assertEqual(calls, [])

    def test_directory_missing_key_does_not_fall_back_to_demo(self):
        def missing():
            raise ProviderError('CONFIG_MISSING')
        self.factory = missing
        status, _, value = self.request('POST', '/api/runs', {'mode': 'directory', 'question': '问'})
        self.assertEqual((status, value), (503, {'error': 'CONFIG_MISSING'}))
        self.assertEqual(self.server.runs.jobs, {})

    def test_replaced_ordinary_root_is_rejected_before_provider_for_both_modes(self):
        self.workspace.rename(self.root / 'original')
        self.workspace.mkdir()
        (self.workspace / 'note.md').write_text('replacement-private-marker')
        calls = []
        self.factory = lambda: calls.append(True)
        for body in ({'file': 'note.md', 'question': '问'}, {'mode': 'directory', 'question': '问'}):
            status, _, value = self.request('POST', '/api/runs', body)
            self.assertEqual((status, value), (400, {'error': 'WORKSPACE_CHANGED'}))
        self.assertEqual(calls, [])

    def test_missing_root_is_rejected_before_provider_for_both_modes(self):
        self.workspace.rename(self.root / 'original')
        calls = []
        self.factory = lambda: calls.append(True)
        for body in ({'file': 'note.md', 'question': '问'}, {'mode': 'directory', 'question': '问'}):
            status, _, value = self.request('POST', '/api/runs', body)
            self.assertEqual((status, value), (400, {'error': 'WORKSPACE_CHANGED'}))
        self.assertEqual(calls, [])

    def test_directory_demo_lists_subdirectories_and_handles_empty_scope(self):
        (self.workspace / 'note.md').unlink()
        (self.workspace / 'nested').mkdir()
        status, _, job = self.request('POST', '/api/runs', {'mode': 'directory', 'question': '问'})
        self.assertEqual(status, 202, job)
        value = self.wait_run(job['id'])
        self.assertEqual(value['result']['answer']['status'], 'not_found')
        self.assertEqual(value['result']['scope']['listed_directories'], ['.', 'nested'])
        self.assertTrue(value['result']['scope']['complete'])

    def test_directory_demo_reports_actual_read_error(self):
        (self.workspace / 'note.md').write_bytes(b'\xff')
        status, _, job = self.request('POST', '/api/runs', {'mode': 'directory', 'question': '问'})
        self.assertEqual(status, 202, job)
        value = self.wait_run(job['id'])
        self.assertEqual(value['state'], 'unable')
        self.assertEqual(value['result']['answer']['citations'], [])

    def test_directory_demo_stops_at_four_distinct_files(self):
        for index in range(4):
            (self.workspace / f'file-{index}.md').write_text(f'合成资料 {index}\n')
        status, _, job = self.request('POST', '/api/runs', {'mode': 'directory', 'question': '问'})
        self.assertEqual(status, 202, job)
        value = self.wait_run(job['id'])
        self.assertEqual(value['state'], 'unable')
        self.assertEqual(len(value['result']['scope']['read_files']), 4)
        self.assertEqual(len(value['result']['scope']['unread_files']), 1)
        self.assertFalse(value['result']['scope']['complete'])
        self.assertIn('FILE_COUNT_LIMIT', json.dumps(value['events']))

    def test_invalid_input_never_reaches_provider(self):
        calls = []
        self.factory = lambda: calls.append(True)
        for fields in ({'file': '../secret.md'}, {'file': '/tmp/secret.md'},
                       {'file': '.hidden.md'}, {'file': 'file.pdf'}, {'question': ' '},
                       {'question': '问' * 4001}, {'workspace': '/tmp'}, {'file': 'a\\b.md'}):
            with self.subTest(fields=str(fields)[:80]):
                status, _, value = self.request('POST', '/api/runs', {'file': 'note.md', 'question': '读取', **fields})
                self.assertEqual(status, 400, value)
        self.assertEqual(calls, [])

    def test_api_rejects_cross_site_and_missing_token(self):
        body = {'file': 'note.md', 'question': '读取'}
        self.assertEqual(self.request('POST', '/api/runs', body, auth=False)[0], 403)
        self.assertEqual(self.request('GET', '/api/config', auth=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/runs', body, origin='https://other.example')[0], 403)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'other.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/runs', body, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/runs', {'question': 'x' * 17000})[0], 413)

    def test_missing_key_keeps_ui_available_and_prevents_run(self):
        def missing():
            raise ProviderError('CONFIG_MISSING')
        self.factory = missing
        config = self.request('GET', '/api/config')[2]
        self.assertFalse(config['ready'])
        self.assertEqual(config['error'], 'CONFIG_MISSING')
        self.assertEqual(self.request('GET', '/')[0], 200)
        self.assertEqual(self.request('POST', '/api/runs', {'file': 'note.md', 'question': '问'})[0], 503)

    def test_cancel_stops_run_and_rejects_concurrent_submission(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(DemoProvider):
            def complete(self, *args):
                entered.set()
                release.wait(3)
                return super().complete(*args)
        self.factory = Slow
        self.addCleanup(release.set)
        run_id = self.start_run()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.request('POST', '/api/runs', {'file': 'note.md', 'question': '问'})[0], 409)
        self.assertEqual(self.request('POST', '/api/runs/' + run_id + '/cancel', {})[0], 200)
        value = self.wait_run(run_id)
        self.assertEqual(value['state'], 'cancelled')
        self.assertIsNone(value['result']['answer'])
        self.assertFalse(any(e['event'] == 'tool.started' for e in value['events']))
        release.set()
        self.assertEqual(self.request('POST', '/api/runs/' + run_id + '/cancel', {})[2]['state'], 'cancelled')

    def test_unknown_job_is_not_found(self):
        self.assertEqual(self.request('GET', '/api/runs/unknown')[0], 404)
        self.assertEqual(self.request('POST', '/api/runs/unknown/cancel', {})[0], 404)

    def test_workspace_replaced_by_symlink_cannot_expand_access(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'note.md').write_text('outside-private-marker')
        self.workspace.rename(self.root / 'original')
        self.workspace.symlink_to(outside, target_is_directory=True)
        for body in ({'file': 'note.md', 'question': '读取'}, {'mode': 'directory', 'question': '读取'}):
            status, _, value = self.request('POST', '/api/runs', body)
            self.assertEqual(status, 400, value.get('state'))
            self.assertEqual(value['error'], 'WORKSPACE_CHANGED')

    def test_snapshots_have_monotonic_revisions(self):
        run_id = self.start_run()
        first = self.request('GET', '/api/runs/' + run_id)[2]
        final = self.wait_run(run_id)
        self.assertIn('revision', first)
        self.assertGreaterEqual(final['revision'], first['revision'])
        self.assertGreaterEqual(final['revision'], len(final['events']) + 1)


if __name__ == '__main__':
    unittest.main()
