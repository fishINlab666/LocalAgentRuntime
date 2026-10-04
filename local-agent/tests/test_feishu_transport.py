"""Offline transport tests: no Feishu credentials, messages, or model calls."""
import importlib
import io
import json
import logging
import multiprocessing
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def envelope():
    return {'header': {'app_id': 'cli_test', 'tenant_key': 'tenant',
                       'event_id': 'ev_1', 'event_type': 'im.message.receive_v1',
                       'token': 'must-not-leave-worker'},
            'event': {'sender': {'sender_id': {'open_id': 'ou_test'},
                                 'sender_type': 'user', 'tenant_key': 'tenant'},
                      'message': {'message_id': 'om_test', 'chat_id': 'oc_test',
                                  'chat_type': 'p2p', 'message_type': 'text',
                                  'content': '{"text":"hello"}'}}}


def fake_receiver_process(conn, app_id, app_secret):
    """Real isolated process; synthetic protocol only."""
    conn.send_bytes(json.dumps({'kind': 'status', 'state': 'connected'}).encode())
    while True:
        time.sleep(0.1)


class FeishuTransportTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / 'local_agent/channels/feishu.py'
        self.assertTrue(path.exists(), 'Feishu transport implementation is missing')
        self.transport = importlib.import_module('local_agent.channels.feishu')
        self.worker = importlib.import_module('local_agent.channels.feishu_worker')

    def test_normalize_preserves_ids_and_excludes_token(self):
        result = self.worker.normalize_event(envelope())
        self.assertEqual(result['app_id'], 'cli_test')
        self.assertEqual(result['message_id'], 'om_test')
        self.assertEqual(result['sender_tenant_key'], 'tenant')
        self.assertEqual(result['content'], '{"text":"hello"}')
        self.assertNotIn('must-not-leave-worker', json.dumps(result))

    def test_normalize_rejects_missing_identity_and_oversized_content(self):
        raw = envelope()
        raw['event']['sender']['sender_id'] = {}
        with self.assertRaises(ValueError):
            self.worker.normalize_event(raw)
        raw = envelope()
        raw['event']['message']['content'] = 'x' * 32769
        with self.assertRaises(ValueError):
            self.worker.normalize_event(raw)

    def test_worker_waits_for_durable_ack_and_raises_on_failure(self):
        for success in (True, False):
            parent, child = multiprocessing.Pipe()
            self.addCleanup(parent.close)
            self.addCleanup(child.close)
            receiver = self.worker.DurableReceiver(child, 'cli_test')
            outcome = []
            def dispatch():
                try:
                    receiver.receive(envelope())
                    outcome.append('ok')
                except RuntimeError as exc:
                    outcome.append(str(exc))
            thread = threading.Thread(target=dispatch)
            thread.start()
            self.assertTrue(parent.poll(1))
            packet = json.loads(parent.recv_bytes())
            self.assertEqual(outcome, [])
            parent.send_bytes(json.dumps({'kind': 'ack', 'id': packet['id'],
                                          'ok': success}).encode())
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, ['ok' if success else 'INBOX_PERSIST_FAILED'])

    def test_worker_ack_timeout_is_finite(self):
        parent, child = multiprocessing.Pipe()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        with patch.object(self.worker, 'ACK_TIMEOUT', 0.02):
            with self.assertRaisesRegex(RuntimeError, 'INBOX_ACK_TIMEOUT'):
                self.worker.DurableReceiver(child, 'cli_test').receive(envelope())

    def test_worker_classifies_connection_failure_without_exposing_message(self):
        class ClientFailure(Exception):
            code = 1000040344

        class SSLError(Exception):
            pass

        self.assertEqual(self.worker.connection_error_code(ClientFailure('private secret')),
                         'SDK_CLIENT_ERROR_1000040344')
        self.assertEqual(self.worker.connection_error_code(SSLError('private certificate')),
                         'SDK_TLS_FAILED')
        self.assertEqual(self.worker.connection_error_code(RuntimeError('private detail')),
                         'SDK_CONNECT_FAILED')

    def test_start_reports_actual_connection_and_close_reaps_process(self):
        instance = self.transport.FeishuTransport('cli_test', 'test-secret', lambda _: True)
        self.addCleanup(instance.close)
        self.assertEqual(instance.status()['state'], 'stopped')
        with patch.object(self.worker, 'worker_main', fake_receiver_process), \
                patch.object(self.transport.importlib.metadata, 'version', return_value='1.7.3'):
            instance.start()
        deadline = time.monotonic() + 3
        while instance.status()['state'] == 'starting' and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(instance.status()['state'], 'connected')
        process = instance._process
        instance.close()
        self.assertFalse(process.is_alive())
        self.assertFalse(instance._reader.is_alive())
        self.assertEqual(instance.status()['state'], 'stopped')

    def test_missing_or_unpinned_sdk_fails_before_spawning(self):
        for value, code in [(None, 'SDK_MISSING'), ('0.0.0', 'SDK_VERSION_MISMATCH')]:
            instance = self.transport.FeishuTransport('cli_test', 'test-secret', lambda _: True)
            self.addCleanup(instance.close)
            lookup = patch.object(self.transport.importlib.metadata, 'version', return_value=value)
            if value is None:
                lookup = patch.object(self.transport.importlib.metadata, 'version',
                                      side_effect=importlib.metadata.PackageNotFoundError)
            with lookup, patch.object(self.worker, 'worker_main', fake_receiver_process):
                with self.assertRaisesRegex(RuntimeError, code):
                    instance.start()
            self.assertIsNone(instance._process)
            self.assertEqual(instance.status()['error_code'], code)

    def test_parent_durable_exception_sends_failure_without_error_text(self):
        parent, child = multiprocessing.Pipe()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        def callback(_):
            raise RuntimeError('private body and secret')
        instance = self.transport.FeishuTransport('cli_test', 'test-secret', callback)
        instance._connection = parent
        packet = {'kind': 'event', 'id': 4, 'envelope': self.worker.normalize_event(envelope())}
        instance._handle_packet(packet)
        self.assertTrue(child.poll(1))
        ack = json.loads(child.recv_bytes())
        self.assertEqual(ack, {'kind': 'ack', 'id': 4, 'ok': False})
        self.assertNotIn('private', json.dumps(instance.status()))

    def test_parent_accepts_only_bounded_connection_error_codes(self):
        instance = self.transport.FeishuTransport('cli_test', 'test-secret', lambda _: True)
        instance._handle_packet({'kind': 'status', 'state': 'error',
                                 'error_code': 'SDK_CLIENT_ERROR_1000040344'})
        self.assertEqual(instance.status()['error_code'], 'SDK_CLIENT_ERROR_1000040344')
        instance._handle_packet({'kind': 'status', 'state': 'error',
                                 'error_code': 'private-secret-from-network'})
        self.assertIsNone(instance.status()['error_code'])

    def test_send_requires_message_id_and_marks_ambiguous_failure_unknown(self):
        instance = self.transport.FeishuTransport('cli_test', 'test-secret', lambda _: True)
        for response, state in [
            (self.response(0, 'om_sent'), 'accepted'),
            (self.response(0, None), 'unknown'),
            (self.response(99991402, None, 429, {'Retry-After': '7'}), 'retry_wait'),
            (self.response(230003, None, 400), 'failed'),
            (self.response(1, None, 503), 'unknown'),
        ]:
            with self.subTest(state=state):
                with patch.object(instance, '_send_request', return_value=response):
                    result = instance.send('oc_test', 'fixed status', 'stable-uuid')
                self.assertEqual(result['state'], state)
                if state == 'retry_wait':
                    self.assertEqual(result['retry_after'], 7)
        with patch.object(instance, '_send_request', side_effect=TimeoutError('secret')):
            result = instance.send('oc_test', 'fixed status', 'stable-uuid')
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('secret', json.dumps(result))

    def test_send_rejects_invalid_target_before_sdk_import(self):
        instance = self.transport.FeishuTransport('cli_test', 'test-secret', lambda _: True)
        with patch.object(instance, '_send_request') as send:
            self.assertEqual(instance.send('', 'text', 'uuid')['state'], 'failed')
            self.assertEqual(instance.send('oc_test', 'x' * 32769, 'uuid')['state'], 'failed')
            self.assertEqual(instance.send('oc_test', '\ud800', 'uuid')['state'], 'failed')
            send.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'optional SDK is not installed')
    def test_released_sdk_request_and_failure_ack_without_network(self):
        script = r'''
import asyncio, importlib, json
from types import SimpleNamespace
from unittest.mock import patch
from local_agent.channels.feishu import FeishuTransport, load_sdk
sdk = load_sdk()
module = importlib.import_module('lark_oapi.ws.client')
from lark_oapi.api.im.v1.resource.message import Message
seen = []
def create(self, request):
    seen.append(request)
    return SimpleNamespace(code=0, data=SimpleNamespace(message_id='om_sent'),
                           raw=SimpleNamespace(status_code=200, headers={}))
transport = FeishuTransport('cli_test', 'test-secret', lambda _: True)
with patch.object(Message, 'create', create), patch('requests.request', side_effect=AssertionError('NETWORK_FORBIDDEN')):
    assert transport.send('oc_test', 'fixed state', 'stable-uuid')['state'] == 'accepted'
assert dict(seen[0].queries)['receive_id_type'] == 'chat_id'
assert seen[0].body.uuid == 'stable-uuid'
assert json.loads(seen[0].body.content) == {'text': 'fixed state'}
assert transport._client._config.timeout == 8
codes = []
for failed in (False, True):
    def callback(data):
        if failed:
            raise RuntimeError('INBOX_PERSIST_FAILED')
    handler = sdk.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(callback).build()
    client = sdk.ws.Client('cli_test', 'test-secret', event_handler=handler, log_level=sdk.LogLevel.ERROR)
    async def write(data):
        decoded = module.Frame()
        decoded.ParseFromString(data)
        codes.append(json.loads(decoded.payload)['code'])
    client._write_message = write
    frame = module.Frame()
    frame.SeqID = 0; frame.LogID = 0; frame.service = 1; frame.method = module.FrameType.DATA.value
    for key, value in [(module.HEADER_MESSAGE_ID, 'frame'), (module.HEADER_TRACE_ID, 'trace'),
                       (module.HEADER_SUM, '1'), (module.HEADER_SEQ, '0'), (module.HEADER_TYPE, 'event')]:
        header = frame.headers.add(); header.key = key; header.value = value
    frame.payload = json.dumps({'schema':'2.0', 'header':{'event_type':'im.message.receive_v1'}, 'event':{}}).encode()
    module.loop.run_until_complete(client._handle_data_frame(frame))
assert codes == [200, 500], codes
tasks = asyncio.all_tasks(module.loop)
for task in tasks:
    task.cancel()
module.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
module.loop.close()
print('released SDK builder + ACK 200/500 PASS; network forbidden')
'''
        result = subprocess.run([sys.executable, '-W', 'error::ResourceWarning', '-c', script],
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertIn('ACK 200/500 PASS', result.stdout)

    def test_sdk_logger_cannot_write_secrets_even_when_builder_changes_level(self):
        captured = io.StringIO()
        logger = logging.getLogger('Lark')
        old = logger.disabled, logger.handlers[:], logger.propagate, logger.level
        self.addCleanup(self.restore_logger, logger, old)
        logger.disabled = False
        logger.handlers = [logging.StreamHandler(captured)]
        self.transport.silence_sdk_logging()
        logger.setLevel(logging.DEBUG)
        logger.error('secret=wss-credential')
        self.assertEqual(captured.getvalue(), '')

    @staticmethod
    def restore_logger(logger, old):
        logger.disabled, logger.handlers, logger.propagate, logger.level = old

    @staticmethod
    def response(code, message_id, status=200, headers=None):
        return SimpleNamespace(code=code, data=SimpleNamespace(message_id=message_id),
                               raw=SimpleNamespace(status_code=status, headers=headers or {}))
