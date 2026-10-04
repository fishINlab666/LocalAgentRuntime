"""First-install pairing: synthetic events, no credentials or network."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PATH = Path(__file__).parents[1] / 'trial/prepare-feishu.py'
spec = importlib.util.spec_from_file_location('feishu_prepare', PATH)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def event(**changes):
    value = dict(app_id='cli_test', tenant_key='tenant', sender_tenant_key='tenant',
                 open_id='ou_me', chat_id='oc_private', sender_type='user',
                 chat_type='p2p', message_type='text', event_type='im.message.receive_v1',
                 content=json.dumps({'text': '/绑定 test-random-challenge'}))
    return dict(value, **changes)


class PrepareTests(unittest.TestCase):
    def collect(self, events, status=None):
        instances = []

        class Transport:
            def __init__(self, app_id, secret, receive):
                self.receive = receive
                self.closed = False
                instances.append(self)

            def start(self):
                for value in events:
                    self.receive(value)

            def status(self):
                return status or {'state': 'connected'}

            def close(self):
                self.closed = True

        with patch.object(prepare.secrets, 'token_urlsafe', return_value='test-random-challenge'):
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                try:
                    return prepare.collect_identity('cli_test', 'hidden-secret',
                        timeout_seconds=0.02, transport_factory=Transport)
                finally:
                    self.assertTrue(instances[0].closed)
                    self.assertNotIn('hidden-secret', output.getvalue())

    def test_ignores_nonmatching_messages_then_extracts_only_identity(self):
        result = self.collect([event(content='{"text":"hello"}'), event()])
        self.assertEqual(result, {'app_id': 'cli_test', 'tenant_key': 'tenant',
                                 'open_id': 'ou_me', 'chat_id': 'oc_private'})

    def test_wrong_app_group_bot_tenant_code_and_malformed_payload_cannot_bind(self):
        cases = [dict(app_id='cli_other'), dict(chat_type='group'), dict(sender_type='app'),
                 dict(sender_tenant_key='other'), dict(message_type='image'),
                 dict(event_type='other'), dict(open_id='../bad'),
                 dict(content='[]'), dict(content='invalid'),
                 dict(content='{"text":"/绑定 wrong"}')]
        for change in cases:
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, 'BINDING_TIMEOUT'):
                self.collect([event(**change)])

    def test_connection_error_is_bounded_and_redacted(self):
        with self.assertRaisesRegex(RuntimeError, '^FEISHU_BINDING_CONNECTION_FAILED$'):
            self.collect([], {'state': 'error', 'error_code': 'secret-from-network'})

        with self.assertRaisesRegex(
                RuntimeError,
                '^FEISHU_BINDING_CONNECTION_FAILED:SDK_CLIENT_ERROR_1000040344$'):
            self.collect([], {'state': 'error',
                              'error_code': 'SDK_CLIENT_ERROR_1000040344'})

    def test_rejected_local_confirmation_creates_no_binding_or_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = ['prepare-feishu.py', '--listen', '--state-dir', str(root / 'state'),
                    '--output', str(root / 'binding.json')]
            ids = {'app_id': 'cli_test', 'tenant_key': 'tenant', 'open_id': 'ou_me', 'chat_id': 'oc_me'}
            with patch('sys.argv', args), patch.dict('os.environ', {'FEISHU_APP_SECRET': 'hidden-secret'}):
                with patch('builtins.input', side_effect=['cli_test', '取消']), patch('sys.stdout', new_callable=io.StringIO):
                    with patch.object(prepare, 'collect_identity', return_value=ids):
                        with self.assertRaisesRegex(RuntimeError, 'BINDING_NOT_CONFIRMED'):
                            prepare.main()
            self.assertFalse((root / 'binding.json').exists())
            self.assertFalse((root / 'state').exists())

    def test_confirmed_pairing_creates_private_binding_without_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = ['prepare-feishu.py', '--listen', '--state-dir', str(root / 'state'),
                    '--output', str(root / 'binding.json')]
            ids = {'app_id': 'cli_test', 'tenant_key': 'tenant', 'open_id': 'ou_me', 'chat_id': 'oc_me'}
            with patch('sys.argv', args), patch.dict('os.environ', {'FEISHU_APP_SECRET': 'hidden-secret'}):
                with patch('builtins.input', side_effect=['cli_test', '绑定']), patch('sys.stdout', new_callable=io.StringIO):
                    with patch.object(prepare, 'collect_identity', return_value=ids):
                        prepare.main()
            raw = (root / 'binding.json').read_text()
            self.assertNotIn('hidden-secret', raw)
            self.assertEqual(json.loads(raw)['open_id'], 'ou_me')
            self.assertEqual((root / 'binding.json').stat().st_mode & 0o777, 0o600)
