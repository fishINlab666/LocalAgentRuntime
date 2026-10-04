"""Offline channel integration; scripted transport/model are not live Feishu."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from local_agent.web_runs import WebRuns
from test_runtime import ScriptedProvider, call_message, final_message


class ChannelConfigTests(unittest.TestCase):
    def test_prepare_script_creates_private_binding_without_network(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'binding.json'
            result = subprocess.run([sys.executable, str(root / 'trial/prepare-feishu.py'),
                '--state-dir', str(Path(tmp) / 'state'), '--output', str(target)],
                input='cli_test\ntenant_test\nou_test\noc_test\n', text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(target.read_text())
            self.assertEqual(value['agent_id'], 'directory-qa')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_config_module_is_implemented(self):
        self.assertIsNotNone(importlib.util.find_spec('local_agent.channels.config'))

    def test_config_rejects_secret_or_unknown_fields(self):
        from local_agent.channels.config import load_config, ChannelConfigError
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'binding.json'
            path.write_text(json.dumps({'app_secret': 'not-real'}))
            with self.assertRaises(ChannelConfigError) as caught:
                load_config(path)
            self.assertEqual(str(caught.exception), 'FEISHU_CONFIG_INVALID')


class FeishuChannelTests(unittest.TestCase):
    def setUp(self):
        from local_agent.channels.service import ChannelService
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'a.md').write_text('代号：orange-731\n')
        self.providers = []

        def provider():
            value = ScriptedProvider([call_message(), final_message()])
            self.providers.append(value)
            return value

        self.runs = WebRuns(self.workspace, self.root / 'runs', provider,
                            state_dir=self.root / 'state')
        self.addCleanup(self.runs.close)
        session = self.runs.create_session({'title': 'channel test',
                                           'scope': {'mode': 'file', 'file': 'a.md'}})['session']
        self.config = dict(app_id='cli_test', tenant_key='tenant_test', open_id='ou_test',
                           chat_id='oc_test', session_id=session['id'],
                           agent_id=session['agent_id'], enabled=True)
        self.sent = []

        def send(chat_id, text, uuid):
            self.sent.append((chat_id, text, uuid))
            return {'state': 'accepted', 'message_id': 'om_notice'}

        self.channel = ChannelService(self.runs, self.config, 'http://127.0.0.1:8765', send=send)
        self.addCleanup(self.channel.close)

    def message(self, message_id='om_1', text='核对代号', **extra):
        return dict(event_id='ev_' + message_id, message_id=message_id,
                    event_type='im.message.receive_v1', app_id='cli_test',
                    tenant_key='tenant_test', sender_tenant_key='tenant_test',
                    open_id='ou_test', chat_id='oc_test', chat_type='p2p',
                    sender_type='user', message_type='text', text=text, **extra)

    def finish(self, event):
        for _ in range(200):
            self.channel.tick()
            current = self.channel.store.event(event['id'])
            if current.get('run_id'):
                snapshot = self.runs.session_snapshot(self.config['session_id'], current['run_id'])['run']
                if snapshot['state'] not in ('queued', 'running', 'waiting_approval'):
                    self.channel.tick()
                    return current, snapshot
            time.sleep(.01)
        self.fail('run did not finish')

    def test_real_runtime_tool_context_and_duplicate_no_new_run(self):
        event = self.channel.receive(self.message())
        current, run = self.finish(event)
        self.assertEqual(run['state'], 'completed')
        requests = self.providers[0].requests
        self.assertNotIn('orange-731', json.dumps(requests[0]))
        self.assertEqual(requests[1][-1]['tool_call_id'], 'c1')
        self.assertIn('orange-731', requests[1][-1]['content'])
        duplicate = self.message()
        duplicate['event_id'] = 'ev_redelivery'
        repeated = self.channel.receive(duplicate)
        self.channel.tick()
        self.assertEqual(repeated['id'], current['id'])
        self.assertEqual(len(self.providers), 1)
        other, _ = self.finish(self.channel.receive(self.message('om_2')))
        self.assertNotEqual(other['run_id'], current['run_id'])
        self.assertEqual(len(self.providers), 2)
        import runpy
        report = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'trial/inspect-feishu.py'))['inspect'](self.root / 'state')
        self.assertEqual(report['acceptance'], 'not_evaluated')
        observed = next(e for e in report['events'] if e['id'] == event['id'])
        self.assertTrue(observed['provider']['simulated'])
        self.assertTrue(all(t['returned_to_model'] for t in observed['tool_feedback']))

    def test_identity_and_unsupported_input_do_not_call_model(self):
        for field, wrong in [('open_id', 'ou_intruder'), ('app_id', 'cli_other'),
                             ('tenant_key', 'other'), ('sender_tenant_key', 'other'),
                             ('chat_type', 'group'), ('sender_type', 'app')]:
            envelope = self.message()
            envelope[field] = wrong
            self.assertIsNone(self.channel.receive(envelope))
        file = self.message('om_file')
        file['message_type'] = 'file'
        event = self.channel.receive(file)
        self.channel.tick()
        self.assertEqual(self.channel.store.event(event['id'])['state'], 'rejected')
        self.assertEqual(self.providers, [])
        self.assertTrue(any('暂不支持' in x[1] for x in self.sent))

    def test_failed_delivery_never_reexecutes_task(self):
        self.channel.send = lambda *args: {'state': 'unknown', 'error_code': 'SEND_UNKNOWN'}
        event, run = self.finish(self.channel.receive(self.message()))
        self.channel.deliver()
        unknown = [r for r in self.channel.store.list_outbox(event['id']) if r['state'] == 'unknown']
        self.assertTrue(unknown)
        self.channel.retry(unknown[0]['id'], self.config['agent_id'])
        self.channel.deliver()
        self.assertEqual(len(self.providers), 1)
        self.assertEqual(self.channel.store.event(event['id'])['run_id'], run['id'])

    def test_received_but_undispatched_restart_is_not_replayed(self):
        from local_agent.channels.service import ChannelService
        event = self.channel.receive(self.message())
        self.channel.close()
        other = ChannelService(self.runs, self.config, 'http://127.0.0.1:8765')
        self.addCleanup(other.close)
        other.tick()
        self.assertEqual(other.store.event(event['id'])['state'], 'interrupted_before_dispatch')
        self.assertEqual(self.providers, [])

    def test_report_path_and_command_validation_is_before_model(self):
        for index, text in enumerate(['/报告 ../escape.md\n写报告', '/报告 report.md', '/未知', '/取消 bad']):
            self.channel.receive(self.message('om_bad' + str(index), text))
        self.channel.tick()
        self.assertEqual(self.providers, [])

    def test_commands_conflict_and_delivery_does_not_hold_runtime_lock(self):
        event = self.channel.receive(self.message(text='/状态'))
        conflict = self.channel.receive(self.message(text='/取消 ' + 'a' * 32))
        self.assertIsNone(conflict)
        self.channel.tick(delivery=False)
        started, release, lock_free = threading.Event(), threading.Event(), threading.Event()
        def slow_send(*args):
            started.set()
            release.wait(2)
            return {'state': 'accepted', 'message_id': 'om_ok'}
        self.channel.send = slow_send
        def delivery():
            try:
                self.channel.deliver()
            finally:
                self.runs.close_thread_connection()
        thread = threading.Thread(target=delivery)
        thread.start()
        self.assertTrue(started.wait(1))
        def check_lock():
            with self.runs.lock:
                lock_free.set()
        checker = threading.Thread(target=check_lock)
        checker.start()
        try:
            self.assertTrue(lock_free.wait(.5))
        finally:
            release.set()
            thread.join(3)
            checker.join(3)

    def test_web_auth_and_channel_routes(self):
        import http.client
        from local_agent.web import create_server
        self.channel.close()
        self.runs.close()
        binding_file = self.root / 'binding.json'
        binding_file.write_text(json.dumps(dict(self.config, enabled=False)))
        server = create_server(self.workspace, self.root / 'runs', state_dir=self.root / 'state',
                               port=0, feishu_config=binding_file)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            conn.request('GET', '/api/channels/feishu')
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.request('GET', '/api/channels/feishu', headers={
                'X-Session-Token': server.token, 'X-Agent-ID': self.config['agent_id']})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertFalse(json.loads(response.read())['channel']['enabled'])
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_binding_change_invalidates_pending_without_sending_old_data(self):
        event = self.channel.receive(self.message())
        replacement = dict(self.config, open_id='ou_replacement')
        self.channel.configure(replacement)
        self.channel.tick()
        self.assertEqual(self.channel.store.event(event['id'])['state'], 'invalidated')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.providers, [])

    def prepare_report(self, text='/报告 report.md\n请写核对报告'):
        from report_provider import ReportProvider
        self.runs.provider_factory = ReportProvider
        event = self.channel.receive(self.message(text=text))
        for _ in range(200):
            self.channel.tick()
            current = self.channel.store.event(event['id'])
            if current.get('run_id'):
                run = self.runs.session_snapshot(current['session_id'], current['run_id'])['run']
                if run.get('pending_approval'):
                    return current, run
            time.sleep(.01)
        self.fail('report did not reach approval')

    def test_approval_real_file_hash_notification_failure_and_busy_replay(self):
        import hashlib
        event, run = self.prepare_report()
        self.assertFalse((self.workspace / 'report.md').exists())
        approval = run['pending_approval']
        self.channel.receive(self.message('om_agree', '同意'))
        busy = self.channel.receive(self.message('om_busy', '另一个问题'))
        self.channel.tick()
        self.assertEqual(self.channel.store.event(busy['id'])['state'], 'rejected')
        self.assertFalse((self.workspace / 'report.md').exists())
        self.runs.decide_session(event['session_id'], event['run_id'], approval['id'], 'allow')
        self.channel.send = lambda *args: {'state': 'unknown'}
        _, finished = self.finish(event)
        self.assertEqual(finished['state'], 'completed')
        body = (self.workspace / 'report.md').read_bytes()
        artifact = self.runs.store.connection().execute('SELECT sha256 FROM artifacts WHERE run_id=?',
                                                      (event['run_id'],)).fetchone()
        self.assertEqual(artifact[0], hashlib.sha256(body).hexdigest())
        count = self.runs.store.connection().execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        self.channel.receive(self.message('om_busy', '另一个问题'))
        self.channel.tick()
        self.assertEqual(self.runs.store.connection().execute('SELECT COUNT(*) FROM runs').fetchone()[0], count)
        self.assertTrue(any(r['state'] == 'unknown' for r in self.channel.store.list_outbox(event['id'])))
        log = self.channel.log_path.read_text()
        self.assertNotIn('orange-731', log)
        self.assertNotIn('请写核对报告', log)
        self.assertEqual(self.channel.log_path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(Exception):
            self.runs.decide_session(event['session_id'], event['run_id'], approval['id'], 'allow')

    def test_cancel_own_run_only_and_recall_does_not_authorize_cancellation(self):
        event, run = self.prepare_report()
        recalled = self.message('om_recall')
        recalled['event_type'] = 'im.message.recalled_v1'
        self.assertIsNone(self.channel.receive(recalled))
        other = self.runs.create_session({'title': 'other', 'scope': {'mode': 'file', 'file': 'a.md'}})['session']
        self.channel.receive(self.message('om_foreign', '/取消 ' + other['id']))
        self.channel.tick()
        self.assertIsNotNone(self.runs.session_snapshot(event['session_id'], event['run_id'])['run']['pending_approval'])
        self.channel.receive(self.message('om_cancel', '/取消 ' + event['run_id']))
        _, finished = self.finish(event)
        self.assertEqual(finished['state'], 'cancelled')
        self.assertFalse((self.workspace / 'report.md').exists())

    def test_approval_refusal_and_timeout_leave_no_artifact(self):
        from local_agent.approvals import RunControl
        for index, decision in enumerate(('deny', 'timeout')):
            if decision == 'timeout':
                session = self.runs.create_session({'title': 'timeout', 'scope': {'mode': 'file', 'file': 'a.md'}})['session']
                self.config['session_id'] = session['id']
                self.channel.configure(self.config)
                def short_control(cancel):
                    return RunControl(cancel, approval_timeout=.2)
                context = patch('local_agent.web_runs.RunControl', side_effect=short_control)
            else:
                from contextlib import nullcontext
                context = nullcontext()
            with context:
                # Distinct original messages are required for independent tasks.
                original = self.message
                self.message = lambda message_id='om_1', text='核对代号', **extra: original(
                    'om_refuse' + str(index) if message_id == 'om_1' else message_id, text, **extra)
                try:
                    event, run = self.prepare_report(text=f'/报告 refused{index}.md\n写报告')
                finally:
                    self.message = original
                if decision == 'deny':
                    self.runs.decide_session(event['session_id'], event['run_id'], run['pending_approval']['id'], 'deny')
                _, finished = self.finish(event)
            self.assertFalse((self.workspace / f'refused{index}.md').exists())
            self.assertNotEqual(finished['state'], 'running')

    def test_committed_run_before_event_mapping_is_reconciled_once(self):
        from local_agent.channels.service import ChannelService
        event = self.channel.receive(self.message())
        self.channel.store.update_event(event['id'], 'dispatching')
        record = self.channel.store.event(event['id'])
        value, _ = self.runs.start_session_run(record['session_id'], {
            'client_request_id': record['client_request_id'], 'task_type': 'files', 'question': '核对代号'})
        self.channel.close()
        resumed = ChannelService(self.runs, self.config, self.channel.origin)
        self.addCleanup(resumed.close)
        resumed.tick()
        self.assertEqual(resumed.store.event(event['id'])['run_id'], value['run']['id'])
        self.assertEqual(len(self.providers), 1)

    def test_cancel_terminal_reports_actual_state_without_false_cancellation(self):
        event, run = self.finish(self.channel.receive(self.message()))
        cancel = self.channel.receive(self.message('om_after', '/取消 ' + event['run_id']))
        self.channel.tick()
        reply = next(r for r in self.channel.store.list_outbox(cancel['id']) if r['kind'] == 'command')['body']
        self.assertIn('已完成', reply)
        self.assertNotIn('已请求取消', reply)
        self.assertEqual(len(self.providers), 1)


if __name__ == '__main__':
    unittest.main()
