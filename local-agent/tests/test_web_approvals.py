import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_agent.provider import ModelReply
from local_agent.tool_runtime import ToolRegistry, ToolRuntime, ToolSpec
from local_agent.web import create_server
from local_agent.web_runs import WebError, WebRuns


CONTENT = '预览正文 <script>should-stay-text</script>'


class PreviewTool:
    spec = ToolSpec('confirm_preview', '确认预览', {
        'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
        'required': ['path', 'content']}, risk='medium')

    def __init__(self):
        self.executions = []

    def preview(self, arguments):
        return {'action_summary': '确认 report.md 的预览', 'path': arguments['path'],
                'bytes': len(arguments['content'].encode('utf-8')), 'content': arguments['content']}

    def verify_preview(self, arguments, preview):
        expected = {'action_summary': '确认 report.md 的预览', 'path': arguments['path'],
                    'bytes': len(arguments['content'].encode('utf-8')),
                    'content': arguments['content']}
        if preview != expected:
            raise ValueError('invalid preview')
        return copy.deepcopy(preview)

    def execute(self, arguments):
        self.executions.append(copy.deepcopy(arguments))
        return {'ok': True, 'operation': 'simulated'}

    def verify_success(self, arguments, data):
        if data != {'operation': 'simulated'}:
            raise ValueError('invalid simulated result')
        return copy.deepcopy(data)


class PreviewPolicy:
    def initial_messages(self, question, target):
        return [{'role': 'user', 'content': question}]

    def model_request(self, messages, limit, tools):
        return {'messages': copy.deepcopy(messages), 'tools': tools}

    def before(self, name, arguments):
        pass

    def accept(self, name, arguments, result):
        pass

    def result_fields(self):
        return {}

    def validate(self, content):
        return json.loads(content)

    def failure_reason(self):
        return None


class PreviewProvider:
    metadata = {'provider': 'scripted-test', 'model': 'none', 'simulated': True}

    def __init__(self):
        self.requests = []

    def complete(self, messages, tools, timeout):
        self.requests.append(copy.deepcopy(messages))
        if len(self.requests) == 1:
            return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'preview-call', 'type': 'function', 'function': {'name': 'confirm_preview',
                'arguments': json.dumps({'path': 'report.md', 'content': CONTENT,
                                         'intent': 'private-intent-marker'})}}]})
        result = json.loads(messages[-1]['content'])
        return ModelReply({'role': 'assistant', 'content': json.dumps({
            'status': 'answered' if result['ok'] else 'unable', 'answer': '模拟结果', 'citations': []})})


class WebApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'note.md').write_text('只读输入')
        self.tools, self.providers, self.outputs = [], [], []
        self.ready, self.finished = threading.Event(), threading.Event()

        def adapt(reader, output_path=None):
            tool = PreviewTool()
            self.tools.append(tool)
            self.outputs.append(output_path)
            return ToolRuntime(ToolRegistry([tool]), PreviewPolicy())

        adapter = patch('local_agent.web_runs.adapt_tools', side_effect=adapt, create=True)
        adapter.start()
        self.addCleanup(adapter.stop)

        def provider():
            value = PreviewProvider()
            self.providers.append(value)
            return value

        self.server = create_server(self.workspace, root / 'runs', port=0, provider_factory=provider)
        publish, execute = self.server.runs._publish, self.server.runs._execute

        def observe_publish(run_id, event):
            publish(run_id, event)
            if event['event'] == 'approval.required':
                self.ready.set()

        def observe_execute(runtime, job):
            try:
                execute(runtime, job)
            finally:
                self.finished.set()

        self.server.runs._publish = observe_publish
        self.server.runs._execute = observe_execute
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.runs.close()
        if self.server.runs.jobs:
            self.finished.wait(1)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)

    def request(self, method, path, data=None, *, auth=True, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        request_headers = {'Content-Type': 'application/json', 'Origin': self.server.origin}
        if auth:
            request_headers['X-Session-Token'] = self.server.token
        request_headers.update(headers or {})
        body = json.dumps(data).encode('utf-8') if data is not None else None
        try:
            conn.request(method, path, body, request_headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def start(self):
        self.ready.clear()
        self.finished.clear()
        status, job = self.request('POST', '/api/runs', {
            'file': 'note.md', 'question': '确认一份预览', 'output_file': 'report.md'})
        self.assertEqual(status, 202, job)
        self.assertTrue(self.ready.wait(1), job)
        status, job = self.request('GET', '/api/runs/' + job['id'])
        self.assertEqual(status, 200)
        self.assertEqual(job['state'], 'waiting_approval')
        pending = job['pending_approval']
        return job, f"/api/runs/{job['id']}/approvals/{pending['id']}"

    def completed(self, run_id):
        self.assertTrue(self.finished.wait(1))
        status, job = self.request('GET', '/api/runs/' + run_id)
        self.assertEqual(status, 200)
        self.assertIsNone(job['pending_approval'])
        self.assertIsNotNone(job['result'])
        return job

    def test_optional_output_is_validated_without_changing_validate_return(self):
        self.assertEqual(WebRuns.validate({'file': 'note.md', 'question': ' 问 ',
                                           'output_file': 'report.md'}), ('note.md', '问'))
        self.assertEqual(WebRuns.validate({'mode': 'directory', 'question': '问',
                                           'output_file': 'reports/new.txt'}), (None, '问'))
        for output in ('', None, 'note.md', '../out.md', '/out.md', '.hidden.md',
                       'folder//out.md', 'report.pdf', 'out\\bad.md', '\ud800.md', 5):
            with self.subTest(output=repr(output)), self.assertRaises(WebError) as caught:
                WebRuns.validate({'file': 'note.md', 'question': '问', 'output_file': output})
            self.assertEqual(caught.exception.status, 400)

    def test_approval_route_reuses_auth_and_accepts_only_decision(self):
        job, route = self.start()
        for options in ({'auth': False}, {'headers': {'Origin': 'https://outside.example'}},
                        {'headers': {'Host': 'outside.example'}}):
            self.assertEqual(self.request('POST', route, {'decision': 'allow'}, **options)[0], 403)
        for body in ({}, [], {'decision': 'yes'}, {'decision': 'allow', 'path': 'other.md'},
                     {'decision': 'allow', 'content': 'changed'}, {'decision': 'allow', 'id': 'other'}):
            self.assertEqual(self.request('POST', route, body)[0], 400, body)
        self.assertEqual(self.tools[0].executions, [])
        self.assertEqual(self.request('POST', route, {'decision': 'deny'})[0], 200)
        self.completed(job['id'])
        self.assertEqual(self.tools[0].executions, [])

    def test_refresh_recovers_frozen_preview_without_event_body_leak(self):
        job, route = self.start()
        status, refreshed = self.request('GET', '/api/runs/' + job['id'])
        self.assertEqual(status, 200)
        self.assertEqual(refreshed['pending_approval']['id'], job['pending_approval']['id'])
        self.assertEqual(refreshed['pending_approval']['content'], CONTENT)
        self.assertEqual(refreshed['output_file'], 'report.md')
        self.assertEqual(self.outputs, ['report.md'])
        for value in (json.dumps(refreshed['events'], ensure_ascii=False),
                      next((self.workspace.parent / 'runs').glob('*.jsonl')).read_text()):
            self.assertNotIn(CONTENT, value)
        self.assertNotIn('private-intent-marker', next((self.workspace.parent / 'runs').glob('*.jsonl')).read_text())
        self.assertIn('private-intent-marker', json.dumps(refreshed['events'], ensure_ascii=False))
        details = next(event['detail'] for event in refreshed['events'] if event['event'] == 'approval.required')
        self.assertEqual(details['risk'], 'medium')
        self.assertEqual(details['source'], 'builtin')
        self.assertIn('action_summary', details)
        self.assertEqual(self.request('POST', route, {'decision': 'allow'})[0], 200)
        self.completed(job['id'])
        self.assertEqual(self.tools[0].executions, [{'path': 'report.md', 'content': CONTENT}])
        self.assertFalse((self.workspace / 'report.md').exists(), 'The test tool must have no file side effects')

    def test_repeated_decision_is_idempotent_and_conflict_is_409(self):
        job, route = self.start()
        self.assertEqual(self.request('POST', route, {'decision': 'allow'})[0], 200)
        self.completed(job['id'])
        self.assertEqual(self.request('POST', route, {'decision': 'allow'})[0], 200)
        status, error = self.request('POST', route, {'decision': 'deny'})
        self.assertEqual((status, error['error']), (409, 'APPROVAL_CONFLICT'))
        self.assertEqual(len(self.tools[0].executions), 1)

    def test_ids_are_bound_to_their_run(self):
        first, first_route = self.start()
        self.assertEqual(self.request('POST', first_route + '-wrong', {'decision': 'allow'})[0], 404)
        unknown = first_route.replace(first['id'], 'unknown-run')
        self.assertEqual(self.request('POST', unknown, {'decision': 'allow'})[0], 404)
        self.assertEqual(self.request('POST', first_route, {'decision': 'deny'})[0], 200)
        self.completed(first['id'])
        second, second_route = self.start()
        cross_run = first_route.replace(first['id'], second['id'])
        self.assertEqual(self.request('POST', cross_run, {'decision': 'allow'})[0], 404)
        self.assertEqual(self.tools[1].executions, [])
        self.assertEqual(self.request('POST', second_route, {'decision': 'deny'})[0], 200)
        self.completed(second['id'])

    def test_cancel_clears_pending_and_rejects_late_allow(self):
        job, route = self.start()
        status, cancelled = self.request('POST', '/api/runs/' + job['id'] + '/cancel', {})
        self.assertEqual(status, 200)
        self.assertIsNone(cancelled['pending_approval'])
        self.assertEqual(self.request('POST', route, {'decision': 'allow'})[0], 409)
        done = self.completed(job['id'])
        self.assertEqual(done['result']['stop_reason'], 'CANCELLED')
        self.assertEqual(self.tools[0].executions, [])

    def test_expired_approval_is_409_without_execution(self):
        job, route = self.start()
        control = self.server.runs.jobs[job['id']]['control']
        with control.lock:
            control.approval_timeout = 0
        status, error = self.request('POST', route, {'decision': 'allow'})
        self.assertEqual((status, error['error']), (409, 'APPROVAL_EXPIRED'))
        self.completed(job['id'])
        self.assertEqual(self.tools[0].executions, [])

    def test_broker_and_control_methods_run_outside_web_lock(self):
        job, route = self.start()
        internal = self.server.runs.jobs[job['id']]

        def unlocked(method):
            def call(*args, **kwargs):
                acquired = threading.Event()

                def inspect():
                    with self.server.runs.lock:
                        acquired.set()

                worker = threading.Thread(target=inspect, daemon=True)
                worker.start()
                self.assertTrue(acquired.wait(.5), 'Web lock was held across a broker/control call')
                worker.join(1)
                return method(*args, **kwargs)
            return call

        broker, control = internal['approvals'], internal['control']
        with patch.object(broker, 'snapshot', side_effect=unlocked(broker.snapshot)), \
                patch.object(broker, 'decide', side_effect=unlocked(broker.decide)), \
                patch.object(control, 'cancel_run', side_effect=unlocked(control.cancel_run)), \
                patch.object(broker, 'close', side_effect=unlocked(broker.close)):
            self.assertEqual(self.request('GET', '/api/runs/' + job['id'])[0], 200)
            self.assertEqual(self.request('POST', route + '-wrong', {'decision': 'allow'})[0], 404)
            self.assertEqual(self.request('POST', '/api/runs/' + job['id'] + '/cancel', {})[0], 200)
            self.server.runs.close()
        self.completed(job['id'])
