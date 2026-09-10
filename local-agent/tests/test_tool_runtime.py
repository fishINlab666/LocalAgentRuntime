import importlib
import importlib.util
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from test_runtime import ScriptedProvider, final_message
from local_agent.discovery import DirectoryTools
from local_agent.file_tools import adapt_tools
from local_agent.files import ReadFile
from local_agent.runtime import Runtime
from local_agent.trace import Trace


def request(name='read_file', arguments=None, call_id='r1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function', 'function': {'name': name,
        'arguments': json.dumps(arguments if arguments is not None else
                                {'path': 'a.md', 'intent': '核对资料中的项目代号'})}}]}


class ToolContractTests(unittest.TestCase):
    def test_intent_validation_and_exact_result_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'a.md').write_text('代号：orange-731\n')
            provider = ScriptedProvider([request(arguments={'path': 'a.md'}),
                                         request(call_id='r2'), final_message()])
            result = Runtime(provider, ReadFile(workspace, {'a.md'}),
                             Trace(root / 'runs', workspace)).run('核对代号', 'a.md')
            self.assertEqual(result['state'], 'completed')
            rejected = json.loads(provider.requests[1][-1]['content'])
            self.assertEqual(rejected['error']['owner'], 'model')
            self.assertEqual(rejected['error']['code'], 'INVALID_ARGUMENT')
            actual = json.loads(provider.requests[2][-1]['content'])
            self.assertEqual(set(actual), {'ok', 'data'})
            self.assertEqual(actual['data']['content'], {'1': '代号：orange-731'})
            self.assertEqual(provider.requests[2][-1]['tool_call_id'], 'r2')

    def test_os_permission_error_stops_without_model_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            provider = ScriptedProvider([request(), final_message()])
            trace = Trace(root / 'runs', workspace, debug_content=True)
            with patch.object(ReadFile, '_open_file', side_effect=PermissionError(13, 'denied')):
                result = Runtime(provider, ReadFile(workspace, {'a.md'}), trace).run('核对代号', 'a.md')
            self.assertEqual(result['stop_reason'], 'OS_PERMISSION_DENIED')
            self.assertEqual(len(provider.requests), 1)
            events = [json.loads(line) for line in trace.path.read_text().splitlines()]
            returned = next(e['data']['result'] for e in events if e['event'] == 'tool.completed')
            self.assertEqual(returned['error']['owner'], 'user')

    def test_contradictory_success_result_is_rejected_before_evidence_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'a.md').write_text('真实事实\n')
            reader = ReadFile(workspace, {'a.md'})
            provider = ScriptedProvider([request()])
            trace = Trace(root / 'runs', workspace, debug_content=True)
            forged = {'ok': True, 'path': 'a.md', 'content': '伪造事实',
                      'line_count': 1, 'bytes': 999, 'sha256': 'not-a-sha256',
                      'extra': 'must not survive'}
            with patch.object(reader, 'execute', return_value=forged):
                result = Runtime(provider, reader, trace).run('核对代号', 'a.md')
            self.assertEqual(result['stop_reason'], 'TOOL_RESULT_INVALID')
            self.assertEqual(len(provider.requests), 1)
            events = [json.loads(line) for line in trace.path.read_text().splitlines()]
            returned = next(e for e in events if e['event'] == 'tool.completed')
            self.assertEqual(returned['data']['id'], 'r1')
            self.assertEqual(returned['data']['result']['error']['code'], 'TOOL_RESULT_INVALID')

    def test_well_shaped_but_unverified_read_cannot_become_answer_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'a.md').write_text('真实事实\n')
            reader = ReadFile(workspace, {'a.md'})
            forged_content = '伪造事实'
            forged = {'ok': True, 'path': 'a.md', 'content': forged_content,
                      'line_count': 1, 'bytes': len(forged_content.encode('utf-8')),
                      'sha256': '0' * 64}
            answer = {'role': 'assistant', 'content': json.dumps({
                'status': 'answered', 'answer': forged_content,
                'citations': [{'path': 'a.md', 'start_line': 1,
                               'end_line': 1, 'quote': forged_content}]}, ensure_ascii=False)}
            provider = ScriptedProvider([request(), answer])
            trace = Trace(root / 'runs', workspace, debug_content=True)
            with patch.object(reader, 'execute', return_value=forged):
                result = Runtime(provider, reader, trace).run('核对事实', 'a.md')
            self.assertEqual(result['stop_reason'], 'TOOL_RESULT_INVALID')
            self.assertEqual(len(provider.requests), 1)

    def test_malformed_error_result_is_correlated_and_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'a.md').write_text('真实事实\n')
            reader = ReadFile(workspace, {'a.md'})
            provider = ScriptedProvider([request()])
            trace = Trace(root / 'runs', workspace, debug_content=True)
            with patch.object(reader, 'execute', return_value={'ok': False, 'error': 'broken'}):
                result = Runtime(provider, reader, trace).run('核对代号', 'a.md')
            self.assertEqual(result['stop_reason'], 'TOOL_RESULT_INVALID')
            events = [json.loads(line) for line in trace.path.read_text().splitlines()]
            returned = next(e for e in events if e['event'] == 'tool.completed')
            self.assertEqual(returned['data']['id'], 'r1')
            self.assertEqual(returned['data']['result']['error']['code'], 'TOOL_RESULT_INVALID')

    def test_error_owner_is_derived_by_runtime_instead_of_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'a.md').write_text('真实事实\n')
            reader = ReadFile(workspace, {'a.md'})
            provider = ScriptedProvider([request()])
            trace = Trace(root / 'runs', workspace, debug_content=True)
            forged = {'ok': False, 'error': {
                'code': 'OS_PERMISSION_DENIED', 'message': 'access denied', 'owner': 'model'}}
            with patch.object(reader, 'execute', return_value=forged):
                result = Runtime(provider, reader, trace).run('核对代号', 'a.md')
            self.assertEqual(result['stop_reason'], 'OS_PERMISSION_DENIED')
            self.assertEqual(len(provider.requests), 1)
            events = [json.loads(line) for line in trace.path.read_text().splitlines()]
            returned = next(e['data']['result'] for e in events if e['event'] == 'tool.completed')
            self.assertEqual(returned['error']['owner'], 'user')

    def test_model_error_cannot_be_promoted_to_user_stop_by_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            reader = ReadFile(workspace, {'a.md'})
            provider = ScriptedProvider([request(), final_message('unable')])
            trace = Trace(root / 'runs', workspace, debug_content=True)
            forged = {'ok': False, 'error': {
                'code': 'FILE_NOT_FOUND', 'message': 'not found', 'owner': 'user'}}
            with patch.object(reader, 'execute', return_value=forged):
                result = Runtime(provider, reader, trace).run('核对代号', 'a.md')
            self.assertEqual(result['state'], 'unable')
            self.assertEqual(len(provider.requests), 2)
            returned = provider.requests[1][-1]
            self.assertEqual(returned['tool_call_id'], 'r1')
            self.assertEqual(json.loads(returned['content'])['error']['owner'], 'model')

    def test_oversized_error_result_is_bounded_and_correlated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            reader = ReadFile(workspace, {'a.md'})
            provider = ScriptedProvider([request()])
            trace = Trace(root / 'runs', workspace, debug_content=True)
            oversized = {'ok': False, 'error': {
                'code': 'FILE_NOT_FOUND', 'message': 'x' * 70000}}
            with patch.object(reader, 'execute', return_value=oversized):
                result = Runtime(provider, reader, trace).run('核对代号', 'a.md')
            self.assertEqual(result['stop_reason'], 'TOOL_RESULT_TOO_LARGE')
            events = [json.loads(line) for line in trace.path.read_text().splitlines()]
            returned = next(e for e in events if e['event'] == 'tool.completed')
            self.assertEqual(returned['data']['id'], 'r1')
            self.assertEqual(returned['data']['result']['error']['owner'], 'user')

    def test_malformed_listing_is_rejected_before_output_filter_or_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            directory = DirectoryTools(workspace)
            engine = adapt_tools(directory, 'report.md')
            malformed = {'ok': True, 'path': '.', 'entries': [{}], 'complete': True}
            provider = ScriptedProvider([request(
                name='list_files', arguments={'path': '.', 'intent': '查找资料'})])
            trace = Trace(root / 'runs', workspace, debug_content=True)
            with patch.object(directory, 'execute', return_value=malformed):
                result = Runtime(provider, engine, trace).run('查找资料', None)
            self.assertEqual(result['stop_reason'], 'TOOL_RESULT_INVALID')
            self.assertEqual(result['scope']['discovered_files'], [])
            events = [json.loads(line) for line in trace.path.read_text().splitlines()]
            returned = next(e for e in events if e['event'] == 'tool.completed')
            self.assertEqual(returned['data']['id'], 'r1')

    def test_failed_single_file_reread_revokes_the_older_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            target = workspace / 'a.md'
            target.write_text('代号：orange-731\n')

            def delete_then_reread(_messages):
                target.unlink()
                return request(call_id='r2')

            provider = ScriptedProvider([
                request(), delete_then_reread, final_message(),
            ])
            result = Runtime(provider, ReadFile(workspace, {'a.md'}),
                             Trace(root / 'runs', workspace)).run('核对代号', 'a.md')
            self.assertEqual(result['state'], 'validation_failed')
            self.assertEqual(result['stop_reason'], 'MISSING_READ')


class RegistryTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('local_agent.tool_runtime'),
                             'A generic tool runtime is required')
        return importlib.import_module('local_agent.tool_runtime')

    def test_registry_rejects_duplicate_names(self):
        api = self.api()
        spec = api.ToolSpec('echo', 'Return supplied text', {
            'type': 'object', 'properties': {'text': {'type': 'string'}},
            'required': ['text'], 'additionalProperties': False})
        class Echo:
            def execute(self, arguments):
                return {'ok': True, 'text': arguments['text']}
            def verify_success(self, arguments, data):
                if data != {'text': arguments['text']}:
                    raise ValueError('invalid echo result')
                return dict(data)
        tool = Echo()
        tool.spec = spec
        registry = api.ToolRegistry([tool])
        self.assertEqual(registry.get('echo').execute({'text': 'hello'}),
                         {'ok': True, 'text': 'hello'})
        self.assertIsNone(registry.get('unknown'))
        with self.assertRaises(ValueError):
            registry.register(tool)

    def test_exported_schemas_cannot_mutate_registry(self):
        api = self.api()
        class Echo:
            spec = api.ToolSpec('echo', 'Echo', {'type': 'object', 'properties': {}})
            def verify_success(self, arguments, data):
                return dict(data)
        registry = api.ToolRegistry([Echo()])
        first = registry.schemas()
        first[0]['function']['parameters']['properties']['injected'] = {}
        self.assertEqual(set(registry.schemas()[0]['function']['parameters']['properties']), {'intent'})


if __name__ == '__main__':
    unittest.main()
