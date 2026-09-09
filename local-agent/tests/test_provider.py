import importlib
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_agent.provider'),
                             'Provider implementation is missing')
        self.m = importlib.import_module('local_agent.provider')

    def test_request_and_response_contract(self):
        captured = []
        def transport(payload, timeout):
            captured.append((payload, timeout))
            return {'choices': [{'finish_reason': 'stop', 'message': {
                'role': 'assistant', 'content': 'hello', 'reasoning_content': 'private reasoning'}}],
                'usage': {'prompt_tokens': 8, 'completion_tokens': 3}}
        p = self.m.DeepSeekProvider('test-not-a-secret', 'test-model', transport=transport)
        reply = p.complete([{'role': 'user', 'content': 'hi'}], [], 2)
        self.assertEqual(reply.message, {'role': 'assistant', 'content': 'hello'})
        self.assertEqual(reply.usage['prompt_tokens'], 8)
        self.assertEqual(captured[0][0]['thinking'], {'type': 'disabled'})
        self.assertIs(captured[0][0]['stream'], False)
        self.assertNotIn('api_key', captured[0][0])
        self.assertEqual(captured[0][1], 2)

    def test_final_request_after_tool_result_enables_json_output(self):
        captured = []
        provider = self.m.DeepSeekProvider('test', 'test', transport=lambda payload, _: (
            captured.append(payload) or {'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': '{}'}}]}))
        provider.complete([{'role': 'tool', 'tool_call_id': 'c1', 'content': '{}'}], [], 1)
        self.assertEqual(captured[0]['response_format'], {'type': 'json_object'})

    def test_initial_tool_selection_request_does_not_force_json_output(self):
        captured = []
        provider = self.m.DeepSeekProvider('test', 'test', transport=lambda payload, _: (
            captured.append(payload) or {'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': '{}'}}]}))
        provider.complete([{'role': 'user', 'content': 'read a file'}], [], 1)
        self.assertNotIn('response_format', captured[0])

    def test_default_http_opener_bypasses_system_proxy(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        with patch('urllib.request.build_opener') as build:
            build.return_value.open.return_value = response
            provider = self.m.DeepSeekProvider('test', 'test')
            provider._post({'messages': []}, 1)
        proxy_handlers = [h for h in build.call_args.args
                          if isinstance(h, self.m.urllib.request.ProxyHandler)]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})
        self.assertEqual(provider.metadata['proxy_mode'], 'direct')

    def test_system_proxy_can_be_enabled_explicitly(self):
        provider = self.m.DeepSeekProvider('test', 'test', use_system_proxy=True,
                                           transport=lambda *_: {})
        self.assertEqual(provider.metadata['proxy_mode'], 'system')

    def test_environment_can_enable_system_proxy(self):
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test',
                                       'AGENT_USE_SYSTEM_PROXY': '1'}, clear=True):
            provider = self.m.DeepSeekProvider.from_env()
        self.assertEqual(provider.metadata['proxy_mode'], 'system')

    def test_tool_call_is_preserved_and_usage_can_be_missing(self):
        call = {'id': 'c1', 'type': 'function', 'function': {
            'name': 'read_file', 'arguments': '{"path":"a.md"}'}}
        p = self.m.DeepSeekProvider('test', 'test', transport=lambda *_: {
            'choices': [{'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [call]}}]})
        reply = p.complete([], [], 1)
        self.assertEqual(reply.message['tool_calls'], [call])
        self.assertIsNone(reply.usage)

    def test_truncated_and_malformed_responses_fail(self):
        for response in ({}, {'choices': []}, {'choices': [{'finish_reason': 'length',
                'message': {'role': 'assistant', 'content': '{}'}}]},
                {'choices': [{'finish_reason': 'stop', 'message': {'role': 'user', 'content': 'bad'}}]}):
            with self.subTest(response=response):
                p = self.m.DeepSeekProvider('test', 'test', transport=lambda *_: response)
                with self.assertRaises(self.m.ProviderError):
                    p.complete([], [], 1)

    def test_missing_config_does_not_expose_environment(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(self.m.ProviderError) as ctx:
                self.m.DeepSeekProvider.from_env()
            self.assertEqual(ctx.exception.code, 'CONFIG_MISSING')

    def test_http_error_does_not_echo_remote_body_or_credentials(self):
        error = HTTPError('https://api.deepseek.com/chat/completions', 401,
                          'secret in remote error', {}, None)
        p = self.m.DeepSeekProvider('sensitive-test-token', 'test')
        with patch('urllib.request.OpenerDirector.open', side_effect=error):
            with self.assertRaises(self.m.ProviderError) as ctx:
                p.complete([], [], 1)
        self.assertEqual(ctx.exception.code, 'AUTH_ERROR')
        self.assertNotIn('secret', str(ctx.exception))
        self.assertNotIn('sensitive-test-token', repr(p))
        self.assertTrue(error.closed)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(self.m.NoRedirect().redirect_request(None, None, 302, '', {},
                                                              'https://other.example'))

    def test_http_rate_limit_timeout_and_network_errors_are_distinct(self):
        for error, code in (
                (HTTPError('https://api.deepseek.com', 429, 'remote body', {}, None), 'RATE_LIMIT'),
                (TimeoutError('private detail'), 'MODEL_TIMEOUT'),
                (URLError('private detail'), 'NETWORK_ERROR')):
            with self.subTest(code=code), patch('urllib.request.OpenerDirector.open', side_effect=error):
                with self.assertRaises(self.m.ProviderError) as raised:
                    self.m.DeepSeekProvider('test', 'test').complete([], [], 1)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn('private detail', str(raised.exception))

    def test_oversized_or_non_json_http_body_is_rejected(self):
        for raw, code in ((b'x' * 65, 'RESPONSE_TOO_LARGE'),
                          (b'<html>upstream error</html>', 'INVALID_MODEL_RESPONSE')):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = raw
            provider = self.m.DeepSeekProvider('test', 'test')
            provider.max_response_bytes = 64
            with self.subTest(code=code), patch('urllib.request.OpenerDirector.open', return_value=response):
                with self.assertRaises(self.m.ProviderError) as raised:
                    provider.complete([], [], 1)
                self.assertEqual(raised.exception.code, code)
                response.__enter__.return_value.read.assert_called_once_with(65)


if __name__ == '__main__':
    unittest.main()
