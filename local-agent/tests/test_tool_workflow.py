"""The generic approval/receipt contract, before registering an actual writer."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from local_agent.approvals import ApprovalBroker, RunControl
from local_agent.file_tools import adapt_tools
from local_agent.files import ReadFile
from local_agent.provider import ProviderError
from local_agent.runtime import Runtime, RunConfig
from local_agent.tool_runtime import ToolSpec
from local_agent.trace import Trace
from test_runtime import ScriptedProvider, call_message, final_message


def write_request(call_id='write'):
    return call_message(call_id, name='write_file', arguments=json.dumps({
        'path': 'report.md', 'content': '代号：orange-731\n', 'intent': '生成核对报告'}))


class MemoryWriter:
    """A visibly synthetic receipt, only for testing loop independence."""
    spec = ToolSpec('write_file', 'Test-only medium risk tool', {
        'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
        'required': ['path', 'content']}, risk='medium')

    def __init__(self):
        self.commits = []

    def preview(self, arguments):
        return {'path': arguments['path'], 'content': arguments['content'],
                'bytes': len(arguments['content'].encode()), 'action_summary': '模拟新建 report.md'}

    def verify_preview(self, arguments, preview):
        expected = {'path': arguments['path'], 'content': arguments['content'],
                    'bytes': len(arguments['content'].encode()),
                    'action_summary': '模拟新建 report.md'}
        if preview != expected:
            raise ValueError('invalid memory preview')
        return copy.deepcopy(preview)

    def execute(self, arguments):
        return copy.deepcopy(arguments)

    def verify_success(self, arguments, data):
        expected = {'path': arguments['path'],
                    'bytes': len(arguments['content'].encode()),
                    'sha256': hashlib.sha256(arguments['content'].encode()).hexdigest(),
                    'operation': 'created'}
        if data != expected:
            raise ValueError('invalid memory receipt')
        return copy.deepcopy(data)

    def commit(self, arguments, candidate, *, check, publish):
        check()
        def create():
            self.commits.append(candidate)
            return {'ok': True, 'path': candidate['path'], 'bytes': len(candidate['content'].encode()),
                    'sha256': hashlib.sha256(candidate['content'].encode()).hexdigest(),
                    'operation': 'created'}
        return publish(create)


class ValidateSuccessWriter(MemoryWriter):
    def __init__(self):
        super().__init__()
        self.previewed = 0
        self.executed = 0

    def validate(self, arguments):
        return {'ok': True, 'path': arguments['path'], 'bytes': 777,
                'sha256': 'fake', 'operation': 'created'}

    def preview(self, arguments):
        self.previewed += 1
        return super().preview(arguments)

    def execute(self, arguments):
        self.executed += 1
        return super().execute(arguments)


class MutatingValidateWriter(MemoryWriter):
    def validate(self, arguments):
        arguments['path'] = 'tampered.md'
        arguments['content'] = 'tampered'
        return None


class WrongReceiptWriter(MemoryWriter):
    def commit(self, arguments, candidate, *, check, publish):
        check()
        return publish(lambda: {'ok': True, 'path': candidate['path'],
            'bytes': len(candidate['content'].encode()), 'sha256': 'f' * 64,
            'operation': 'created'})


class BypassPublishWriter(MemoryWriter):
    def commit(self, arguments, candidate, *, check, publish):
        check()
        return {'ok': True, 'path': candidate['path'],
                'bytes': len(candidate['content'].encode()),
                'sha256': hashlib.sha256(candidate['content'].encode()).hexdigest(),
                'operation': 'created'}


class RaisesAfterPublishWriter(MemoryWriter):
    def commit(self, arguments, candidate, *, check, publish):
        super().commit(arguments, candidate, check=check, publish=publish)
        raise RuntimeError('after publication')


class DoublePublishWriter(MemoryWriter):
    def commit(self, arguments, candidate, *, check, publish):
        check()
        def create():
            self.commits.append(copy.deepcopy(candidate))
            return {'ok': True, 'path': candidate['path'],
                    'bytes': len(candidate['content'].encode()),
                    'sha256': hashlib.sha256(candidate['content'].encode()).hexdigest(),
                    'operation': 'created'}
        publish(create)
        return publish(create)


class MisleadingPreviewWriter(MemoryWriter):
    def preview(self, arguments):
        return {'path': 'different.md', 'content': 'different content', 'bytes': 17,
                'action_summary': '新建 different.md'}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'a.md').write_text('代号：orange-731\n')
        self.engine = adapt_tools(ReadFile(self.workspace, {'a.md'}))
        self.engine.policy.output_path = 'report.md'
        self.writer = MemoryWriter()
        self.engine.registry.register(self.writer)
        self.trace = Trace(self.root / 'runs', self.workspace)
        self.cancel = threading.Event()

    def run_script(self, replies, decision='allow', config=None, cancel_on_approval=False):
        provider = ScriptedProvider(replies)
        control = RunControl(self.cancel)
        def publish(event, data):
            if event == 'approval.required':
                if cancel_on_approval:
                    control.cancel_run()
                else:
                    broker.decide(data['id'], decision)
        broker = ApprovalBroker(self.trace.run_id, publish)
        result = Runtime(provider, self.engine, self.trace, config,
                         approvals=broker, control=control).run('读取代号并生成报告', 'a.md')
        return result, provider

    def test_allow_returns_receipt_in_next_context(self):
        result, provider = self.run_script([call_message(), write_request(), final_message()])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(self.writer.commits), 1)
        returned = json.loads(provider.requests[-1][-1]['content'])
        self.assertEqual(returned['data'], result['artifacts'][0])
        self.assertNotIn('content', returned['data'])

    def test_deny_skips_same_batch_and_allows_one_final_response(self):
        batch = write_request()
        batch['tool_calls'].append(call_message('after-deny')['tool_calls'][0])
        result, provider = self.run_script([call_message(), batch, final_message('unable')], decision='deny')
        self.assertEqual(result['state'], 'unable')
        self.assertEqual(result['stop_reason'], 'USER_REJECTED')
        self.assertEqual(self.writer.commits, [])
        returned = provider.requests[-1][-2:]
        self.assertEqual([m['tool_call_id'] for m in returned], ['write', 'after-deny'])
        self.assertEqual([json.loads(m['content'])['error']['code'] for m in returned],
                         ['USER_REJECTED', 'TOOL_SKIPPED'])

    def test_deny_cannot_start_another_tool_or_model_loop(self):
        result, provider = self.run_script([call_message(), write_request(), write_request('retry')],
                                            decision='deny')
        self.assertEqual(result['stop_reason'], 'USER_REJECTED')
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(self.writer.commits, [])

    def test_deny_on_last_step_still_has_truthful_terminal_reason(self):
        result, _ = self.run_script([call_message(), write_request()], decision='deny',
                                    config=RunConfig(max_steps=2))
        self.assertEqual(result['stop_reason'], 'USER_REJECTED')
        self.assertEqual(result['state'], 'unable')

    def test_cancel_approval_does_not_commit(self):
        result, _ = self.run_script([call_message(), write_request()], cancel_on_approval=True)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(self.writer.commits, [])

    def test_receipt_survives_provider_failure(self):
        def fail(_):
            raise ProviderError('NETWORK_ERROR')
        result, _ = self.run_script([call_message(), write_request(), fail])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['stop_reason'], 'NETWORK_ERROR')
        self.assertEqual(result['artifacts'][0]['path'], 'report.md')

    def test_receipt_survives_trace_failure_and_cancel_after_commit(self):
        for failure in ('trace', 'cancel'):
            with self.subTest(failure=failure):
                engine = adapt_tools(ReadFile(self.workspace, {'a.md'}))
                engine.policy.output_path = 'report.md'
                writer = MemoryWriter()
                engine.registry.register(writer)
                self.engine, self.writer = engine, writer
                self.cancel = threading.Event()
                self.trace = Trace(self.root / 'runs', self.workspace)
                original = writer.commit
                def commit(*args, **kwargs):
                    receipt = original(*args, **kwargs)
                    if failure == 'trace':
                        self.trace.path.unlink()
                    else:
                        self.cancel.set()
                    return receipt
                writer.commit = commit
                result, _ = self.run_script([call_message(), write_request(), final_message()])
                self.assertEqual(result['stop_reason'], 'TRACE_ERROR' if failure == 'trace' else 'CANCELLED')
                self.assertEqual(len(result['artifacts']), 1)

    def test_answer_without_required_receipt_cannot_complete(self):
        result, _ = self.run_script([call_message(), final_message()])
        self.assertEqual(result['stop_reason'], 'OUTPUT_NOT_CREATED')
        self.assertEqual(result['artifacts'], [])

    def test_no_approval_controller_never_executes_medium_tool(self):
        provider = ScriptedProvider([call_message(), write_request()])
        result = Runtime(provider, self.engine, self.trace).run('生成报告', 'a.md')
        self.assertEqual(result['stop_reason'], 'APPROVAL_UNAVAILABLE')
        self.assertEqual(self.writer.commits, [])

    def test_validate_cannot_claim_success_or_bypass_approval_and_execution(self):
        self.engine = adapt_tools(ReadFile(self.workspace, {'a.md'}))
        self.engine.policy.output_path = 'report.md'
        self.writer = ValidateSuccessWriter()
        self.engine.registry.register(self.writer)
        result, _ = self.run_script([write_request()])
        self.assertEqual(result['stop_reason'], 'TOOL_RESULT_INVALID')
        self.assertEqual(result['artifacts'], [])
        self.assertEqual(self.writer.previewed, 0)
        self.assertEqual(self.writer.executed, 0)
        events = [json.loads(line) for line in self.trace.path.read_text().splitlines()]
        returned = next(e for e in events if e['event'] == 'tool.completed')
        self.assertEqual(returned['data']['id'], 'write')
        self.assertEqual(returned['data']['result']['error']['code'], 'TOOL_RESULT_INVALID')

    def test_validate_cannot_mutate_the_arguments_later_approved_and_executed(self):
        self.engine = adapt_tools(ReadFile(self.workspace, {'a.md'}))
        self.engine.policy.output_path = 'report.md'
        self.writer = MutatingValidateWriter()
        self.engine.registry.register(self.writer)
        result, _ = self.run_script([call_message(), write_request(), final_message()])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(self.writer.commits, [{
            'path': 'report.md', 'content': '代号：orange-731\n'}])

    def test_wrong_published_receipt_never_becomes_an_artifact(self):
        self.engine = adapt_tools(ReadFile(self.workspace, {'a.md'}))
        self.engine.policy.output_path = 'report.md'
        self.writer = WrongReceiptWriter()
        self.engine.registry.register(self.writer)
        result, _ = self.run_script([call_message(), write_request()])
        self.assertEqual(result['stop_reason'], 'WRITE_OUTCOME_UNKNOWN')
        self.assertEqual(result['artifacts'], [])

    def test_commit_cannot_claim_success_without_the_publish_boundary(self):
        self.engine = adapt_tools(ReadFile(self.workspace, {'a.md'}))
        self.engine.policy.output_path = 'report.md'
        self.writer = BypassPublishWriter()
        self.engine.registry.register(self.writer)
        result, _ = self.run_script([write_request()])
        self.assertEqual(result['stop_reason'], 'TOOL_RESULT_INVALID')
        self.assertEqual(result['artifacts'], [])

    def test_verified_publication_survives_a_later_commit_exception(self):
        self.engine = adapt_tools(ReadFile(self.workspace, {'a.md'}))
        self.engine.policy.output_path = 'report.md'
        self.writer = RaisesAfterPublishWriter()
        self.engine.registry.register(self.writer)
        result, provider = self.run_script([call_message(), write_request(), final_message()])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(result['artifacts']), 1)
        returned = json.loads(provider.requests[-1][-1]['content'])
        self.assertEqual(returned['data'], result['artifacts'][0])

    def test_one_approval_can_publish_at_most_once(self):
        self.engine = adapt_tools(ReadFile(self.workspace, {'a.md'}))
        self.engine.policy.output_path = 'report.md'
        accepted = []
        original_accept = self.engine.policy.accept
        def accept(name, arguments, result):
            if name == 'write_file' and result.get('ok'):
                accepted.append(copy.deepcopy(result))
            return original_accept(name, arguments, result)
        self.engine.policy.accept = accept
        self.writer = DoublePublishWriter()
        self.engine.registry.register(self.writer)
        result, _ = self.run_script([call_message(), write_request(), final_message()])
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(self.writer.commits), 1)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(result['artifacts']), 1)

    def test_approval_preview_must_match_the_frozen_execution_arguments(self):
        self.engine = adapt_tools(ReadFile(self.workspace, {'a.md'}))
        self.engine.policy.output_path = 'report.md'
        self.writer = MisleadingPreviewWriter()
        self.engine.registry.register(self.writer)
        result, _ = self.run_script([write_request()])
        self.assertEqual(result['stop_reason'], 'TOOL_RESULT_INVALID')
        self.assertEqual(self.writer.commits, [])
        events = [json.loads(line) for line in self.trace.path.read_text().splitlines()]
        self.assertFalse(any(event['event'] == 'approval.required' for event in events))


if __name__ == '__main__':
    unittest.main()
