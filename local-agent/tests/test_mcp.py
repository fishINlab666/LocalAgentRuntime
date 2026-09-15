import importlib.util
import json
import sys
import threading
import time
import unittest
from pathlib import Path

from local_agent.tool_runtime import ToolSpec, input_schema, valid_arguments

HAS_EXTENSIONS = importlib.util.find_spec('mcp') is not None


@unittest.skipUnless(HAS_EXTENSIONS, 'optional MCP dependencies are not installed')
class SchemaTests(unittest.TestCase):
    def test_wrapper_preserves_remote_intent_nested_types_and_local_refs(self):
        schema = {'type': 'object', 'properties': {
            'intent': {'type': 'string'}, 'items': {'type': 'array', 'items': {'$ref': '#/$defs/item'}}},
            'required': ['intent', 'items'], 'additionalProperties': False,
            '$defs': {'item': {'type': 'object', 'properties': {'amount': {'type': 'number', 'minimum': 0}},
                'required': ['amount'], 'additionalProperties': False}}}
        spec = ToolSpec('remote', 'remote', schema, source='mcp')
        wrapped = {'intent': 'outer', 'arguments': {'intent': 'inner', 'items': [{'amount': 2.5}]}}
        self.assertTrue(valid_arguments(spec, wrapped))
        self.assertEqual(input_schema(spec)['properties']['arguments']['properties']['intent'], {'type': 'string'})
        wrapped['arguments']['items'][0]['amount'] = True
        self.assertFalse(valid_arguments(spec, wrapped))

    def test_unsupported_schemas_fail_closed(self):
        from local_agent.schema_validation import check_schema
        for schema in ({'type': 'mystery'}, {'$ref': 'https://example.invalid/schema'},
                       {'type': 'object', 'properties': {'x': {'$ref': '#/missing'}}},
                       {'type': 'object', 'mysteryConstraint': 1},
                       {'type': 'object', 'description': 'x' * 17000}):
            with self.subTest(schema=str(schema)[:80]), self.assertRaises(ValueError):
                check_schema(schema)


@unittest.skipUnless(HAS_EXTENSIONS, 'optional MCP dependencies are not installed')
class MCPProcessTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_agent.mcp_tools'), 'MCP adapters are not implemented')
        from local_agent.mcp_tools import build_mcp_tools
        self.build = build_mcp_tools
        self.handles = []

    def tearDown(self):
        for handle in self.handles:
            handle.close()

    def connect(self, mode='normal', **extra):
        config = {'server_id': 'project', 'command': sys.executable,
            'args': [str(Path(__file__).parent / 'fixtures' / 'mcp_server.py'), mode],
            'allowed_tools': ['status', 'plain'], 'allowed_resources': ['file:///not-a-local-file.txt'],
            'allowed_prompts': ['brief'], 'timeout_seconds': 1.5, **extra}
        handle = self.build(config, run_id='run-one')
        self.handles.append(handle)
        return handle

    def call(self, handle, name='status', arguments=None, call_id='call-one'):
        from local_agent.tool_runtime import ToolRuntime, ToolRegistry
        from local_agent.approvals import RunControl
        class Policy:
            def before(self, *args): pass
            def accept(self, *args): pass
            def result_fields(self): return {}
            def authorize_tool(self, tool, args): return tool in handle.tools
        tool = next(t for t in handle.tools if t.remote_name == name)
        values = {'intent': '核对合成资料', 'arguments': {'intent': 'remote intent', 'items': [1, 2]}
                  if arguments is None else arguments}
        return ToolRuntime(ToolRegistry(handle.tools), Policy()).invoke({
            'id': call_id, 'function': {'name': tool.spec.name, 'arguments': json.dumps(values)}},
            budget_ok=True, execute_bounded=lambda f, t: f(), emit=lambda *args: None,
            control=RunControl(threading.Event())).result

    def test_discovery_nested_call_and_real_response_receipt(self):
        handle = self.connect()
        self.assertEqual(len(handle.catalog['tools']), 3)
        self.assertFalse(next(x for x in handle.catalog['tools'] if x['name'] == 'write_remote')['enabled'])
        self.assertEqual(handle.tools[0].result_fields(), {})
        result = self.call(handle)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['structured_content'], {'project': '青禾-47', 'count': 3})
        self.assertEqual(result['data'].get('source_id'), 'mcp:call-one')
        self.assertEqual(result['data']['source']['evidence_role'], 'fact')
        receipt = result['data']['receipt']
        self.assertEqual((receipt['server_id'], receipt['run_id'], receipt['call_id']), ('project', 'run-one', 'call-one'))
        self.assertIs(type(receipt['request_id']), int)
        self.assertIn('remote intent', result['data']['content'])
        self.assertFalse(handle.client.verify_receipt({**receipt, 'call_id': 'other'}, result['data']['raw_result']))

    def test_excess_mcp_call_is_returned_to_the_model_instead_of_stopping_the_run(self):
        from local_agent.approvals import RunControl
        from local_agent.tool_runtime import ToolRegistry, ToolRuntime
        handle = self.connect()
        tool = next(item for item in handle.tools if item.remote_name == 'status')
        class Policy:
            def before(self, *args): pass
            def accept(self, *args): pass
            def result_fields(self): return {}
            def authorize_tool(self, candidate, arguments): return candidate in handle.tools
        invocation = ToolRuntime(ToolRegistry(handle.tools), Policy()).invoke({
            'id': 'fifth-call', 'function': {'name': tool.spec.name, 'arguments': json.dumps({
                'intent': '等待下一轮再调用',
                'arguments': {'intent': 'remote', 'items': [1]},
            })}}, budget_ok=False, execute_bounded=lambda function, timeout: function(),
            emit=lambda *args: None, control=RunControl(threading.Event()))
        self.assertEqual(invocation.result['error']['code'], 'TOOL_CALL_LIMIT')
        self.assertEqual(invocation.result['error']['owner'], 'model')
        self.assertEqual(invocation.decision, 'continue')

    def test_text_resource_and_explicit_prompt_remain_source_content(self):
        handle = self.connect()
        self.assertTrue(self.call(handle, 'plain', {})['ok'])
        resource = self.call(handle, 'read_resource', {'uri': 'file:///not-a-local-file.txt'})
        self.assertTrue(resource['ok'], resource)
        self.assertIn('青禾-47', resource['data']['content'])
        denied = self.call(handle, 'get_prompt', {'name': 'brief', 'arguments': {'audience': '研发'}})
        self.assertEqual(denied['error']['code'], 'PATH_DENIED')
        handle.select_prompt('brief')
        prompt = self.call(handle, 'get_prompt', {'name': 'brief', 'arguments': {'audience': '研发'}})
        self.assertTrue(prompt['ok'], prompt)
        self.assertIn('研发', prompt['data']['content'])
        self.assertEqual(prompt['data']['source_kind'], 'mcp_prompt')
        self.assertEqual(prompt['data']['source']['evidence_role'], 'method')

    def test_invalid_input_never_reaches_server_and_no_call_replay(self):
        handle = self.connect()
        before = handle.client.request_count
        result = self.call(handle, arguments={'intent': 'x', 'items': [True]})
        self.assertEqual(result['error']['code'], 'INVALID_ARGUMENT')
        self.assertEqual(handle.client.request_count, before)

    def test_failure_modes_never_produce_success(self):
        expected = {'error': 'MCP_TOOL_ERROR', 'bad_output': 'MCP_OUTPUT_INVALID',
            'wrong_id': 'MCP_PROTOCOL_ERROR', 'disconnect': 'MCP_DISCONNECTED',
            'protocol_error': 'MCP_PROTOCOL_ERROR', 'oversize': 'TOOL_RESULT_TOO_LARGE',
            'late': 'TOOL_TIMEOUT'}
        for mode, code in expected.items():
            with self.subTest(mode=mode):
                handle = self.connect(mode, timeout_seconds=.4)
                result = self.call(handle)
                self.assertEqual(result['error']['code'], code, result)
                handle.close()

    def test_cancel_reclaims_process_and_new_run_is_independent(self):
        from local_agent.approvals import RunControl
        control = RunControl(threading.Event())
        handle = self.connect('hang')
        handle.client.control = control
        timer = threading.Timer(.1, control.cancel.set)
        timer.start()
        started = time.monotonic()
        result = self.call(handle)
        timer.join()
        self.assertEqual(result['error']['code'], 'CANCELLED')
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(handle.client.is_alive)
        self.assertIsNotNone(handle.client._process.returncode)
        fresh = self.connect()
        self.assertTrue(self.call(fresh)['ok'])

    def test_duplicate_cursor_refuses_partial_catalog(self):
        with self.assertRaises(ValueError):
            self.connect('cursor_loop')

    def test_second_server_needs_no_core_special_case(self):
        fixture = str(Path(__file__).parent / 'fixtures' / 'mcp_server.py')
        handle = self.connect(args=[fixture, 'normal', 'renamed'], server_id='second', allowed_tools=['renamed'])
        self.assertTrue(self.call(handle, 'renamed')['ok'])

    def test_verified_result_cannot_be_replayed_or_transferred(self):
        handle = self.connect()
        tool = next(t for t in handle.tools if t.remote_name == 'plain')
        values = {'arguments': {}}
        tool.bind_call('original')
        raw = tool.execute(values)
        self.assertTrue(raw['ok'])
        data = {k: v for k, v in raw.items() if k != 'ok'}
        tool.verify_success(values, data)
        with self.assertRaises(ValueError):
            tool.verify_success(values, data)
        tool.bind_call('different')
        with self.assertRaises(ValueError):
            tool.verify_success(values, data)
        handle.close()
        self.assertFalse(handle.client.verify_receipt(data['receipt'], data['raw_result']))
