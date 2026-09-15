import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_agent.approvals import ApprovalBroker, RunControl
from local_agent.agents import build_file_engine, builtin_agent
from local_agent.directory_evaluation import context_checks
from local_agent.discovery import DirectoryTools
from local_agent.file_tools import adapt_tools
from local_agent.runtime import Runtime, RunConfig
from local_agent.trace import Trace
from report_provider import ReportProvider
from test_runtime import ScriptedProvider, call_message


class ReportWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        (self.workspace / 'plan.md').write_text('项目代号：杉木-19\n', encoding='utf-8')
        (self.workspace / 'review.txt').write_text('评审人：顾宁\n', encoding='utf-8')
        self.output = self.workspace / 'report.md'
        self.provider = ReportProvider()
        self.trace = Trace(self.root / 'runs', self.workspace, debug_content=True)
        self.engine = adapt_tools(DirectoryTools(self.workspace), 'report.md')
        self.control = RunControl(threading.Event())
        self.previews = []

    def run_report(self, decision='allow', question='请依据两份文件生成报告', before_decision=None, config=None):
        def publish(event, data):
            if event == 'approval.required':
                self.assertFalse(self.output.exists())
                preview = broker.snapshot()
                self.previews.append(preview)
                if before_decision:
                    before_decision()
                broker.decide(data['id'], decision)
        broker = ApprovalBroker(self.trace.run_id, publish)
        return Runtime(self.provider, self.engine, self.trace, config, approvals=broker,
                       control=self.control).run(question, None)

    def test_registers_writer_from_agent_permission_without_explicit_output(self):
        self.assertIsNotNone(self.engine.registry.get('write_file'))
        implicit = adapt_tools(DirectoryTools(self.workspace))
        self.assertIsNotNone(implicit.registry.get('write_file'))
        configured = build_file_engine(builtin_agent('file'), self.workspace, 'plan.md')
        self.assertIsNone(configured.policy.output_path)
        self.assertIsNotNone(configured.registry.get('write_file'))

    def run_dynamic_report(self, *, output_path=None, paths=('summary.md', 'next-steps.txt'),
                           decisions=('allow', 'allow'), retry_after_denial=False):
        self.engine = build_file_engine(builtin_agent('file'), self.workspace, 'plan.md', output_path)

        def choose_outputs(messages):
            source = messages[-1]
            self.assertEqual(source['tool_call_id'], 'read-plan')
            content = json.loads(source['content'])['data']['content']['1']
            calls = [call_message('write-' + str(index), name='write_file', arguments=json.dumps({
                'path': path, 'content': content + '\n', 'intent': '保存依据原文生成的结果'},
                ensure_ascii=False))['tool_calls'][0] for index, path in enumerate(paths)]
            return {'role': 'assistant', 'content': None, 'tool_calls': calls}

        denied = 'deny' in decisions
        if retry_after_denial:
            final = call_message('write-retry', name='write_file', arguments=json.dumps({
                'path': 'retry.md', 'content': '不得执行', 'intent': '重新写入'}))
        else:
            final = {'role': 'assistant', 'content': json.dumps({
                'status': 'unable' if denied else 'answered',
                'answer': '第一份已创建，第二份因用户拒绝而未创建。' if denied else '已依据原文生成结果。',
                'citations': [] if denied else [{'path': 'plan.md', 'start_line': 1, 'end_line': 1}]},
                ensure_ascii=False)}
        provider = ScriptedProvider([call_message('read-plan', path='plan.md'), choose_outputs, final])
        choices = iter(decisions)

        def publish(event, data):
            if event == 'approval.required':
                preview = broker.snapshot()
                self.assertFalse((self.workspace / preview['path']).exists())
                self.previews.append(preview)
                broker.decide(data['id'], next(choices))

        broker = ApprovalBroker(self.trace.run_id, publish)
        result = Runtime(provider, self.engine, self.trace, approvals=broker,
                         control=self.control).run('读取项目资料并生成两份不同用途的文件', 'plan.md')
        return result, provider

    def test_model_chooses_two_outputs_with_independent_approvals_and_actual_receipts(self):
        result, provider = self.run_dynamic_report()

        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(self.previews), 2)
        self.assertEqual(len({preview['id'] for preview in self.previews}), 2)
        self.assertEqual([artifact['path'] for artifact in result['artifacts']],
                         ['summary.md', 'next-steps.txt'])
        initial = json.dumps(provider.requests[0], ensure_ascii=False)
        self.assertNotIn('杉木-19', initial)
        self.assertNotIn('summary.md', initial)
        self.assertNotIn('next-steps.txt', initial)
        returned = [message for message in provider.requests[-1]
                    if message.get('role') == 'tool' and message['tool_call_id'].startswith('write-')]
        self.assertEqual([message['tool_call_id'] for message in returned], ['write-0', 'write-1'])
        for preview, artifact, message in zip(self.previews, result['artifacts'], returned):
            raw = (self.workspace / artifact['path']).read_bytes()
            self.assertEqual(raw, preview['content'].encode('utf-8'))
            self.assertEqual(artifact, {'path': preview['path'], 'bytes': len(raw),
                                       'sha256': hashlib.sha256(raw).hexdigest(), 'operation': 'created'})
            receipt = json.loads(message['content'])
            self.assertTrue(receipt['ok'])
            self.assertEqual(receipt['data'], artifact)
            self.assertNotIn('content', receipt['data'])

    def test_second_output_denial_preserves_first_artifact_and_truthful_final_context(self):
        result, provider = self.run_dynamic_report(decisions=('allow', 'deny'))

        self.assertEqual(result['state'], 'unable')
        self.assertEqual(result['stop_reason'], 'USER_REJECTED')
        self.assertTrue((self.workspace / 'summary.md').is_file())
        self.assertFalse((self.workspace / 'next-steps.txt').exists())
        self.assertEqual([artifact['path'] for artifact in result['artifacts']], ['summary.md'])
        self.assertEqual(len(self.previews), 2)
        self.assertEqual(len(provider.requests), 3)
        returned = [message for message in provider.requests[-1]
                    if message.get('role') == 'tool' and message['tool_call_id'].startswith('write-')]
        self.assertEqual([message['tool_call_id'] for message in returned], ['write-0', 'write-1'])
        self.assertEqual(json.loads(returned[0]['content'])['data'], result['artifacts'][0])
        self.assertEqual(json.loads(returned[1]['content'])['error']['code'], 'USER_REJECTED')
        self.assertNotIn('请求的输出没有创建', provider.requests[-1][-1]['content'])
        self.assertIn('第一份已创建', result['answer']['answer'])

    def test_second_output_denial_cannot_retry_or_remove_the_created_artifact(self):
        result, provider = self.run_dynamic_report(decisions=('allow', 'deny'), retry_after_denial=True)

        self.assertEqual(result['stop_reason'], 'USER_REJECTED')
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(len(self.previews), 2)
        self.assertEqual([artifact['path'] for artifact in result['artifacts']], ['summary.md'])
        self.assertTrue((self.workspace / 'summary.md').is_file())
        self.assertFalse((self.workspace / 'next-steps.txt').exists())
        self.assertFalse((self.workspace / 'retry.md').exists())

    def test_extra_output_does_not_satisfy_the_explicit_required_output(self):
        result, _ = self.run_dynamic_report(output_path='report.md', paths=('summary.md',),
                                            decisions=('allow',))

        self.assertEqual(result['stop_reason'], 'OUTPUT_NOT_CREATED')
        self.assertTrue((self.workspace / 'summary.md').is_file())
        self.assertFalse(self.output.exists())
        self.assertEqual([artifact['path'] for artifact in result['artifacts']], ['summary.md'])

    def test_explicit_output_can_complete_alongside_an_extra_file(self):
        result, _ = self.run_dynamic_report(output_path='report.md', paths=('summary.md', 'report.md'))

        self.assertEqual(result['state'], 'completed')
        self.assertEqual([artifact['path'] for artifact in result['artifacts']], ['summary.md', 'report.md'])
        self.assertTrue(self.output.is_file())

    def test_two_reads_approval_real_bytes_and_context_receipt_match(self):
        result = self.run_report()
        self.assertEqual(result['state'], 'completed')
        raw = self.output.read_bytes()
        self.assertEqual(raw, self.previews[0]['content'].encode('utf-8'))
        receipt = result['artifacts'][0]
        self.assertEqual(receipt, {'path': 'report.md', 'bytes': len(raw),
                                  'sha256': hashlib.sha256(raw).hexdigest(), 'operation': 'created'})
        returned = json.loads(self.provider.requests[-1][-1]['content'])
        self.assertEqual(returned['data'], receipt)
        self.assertNotIn('content', returned['data'])
        self.assertEqual(result['scope']['read_files'], ['plan.md', 'review.txt'])
        self.assertNotIn('report.md', result['scope']['discovered_files'])
        self.assertNotIn('杉木-19', json.dumps(self.provider.requests[0], ensure_ascii=False))
        events = [json.loads(line) for line in self.trace.path.read_text().splitlines()]
        self.assertTrue(all(context_checks(events).values()))
        self.assertEqual({c['path'] for c in result['answer']['citations']}, {'plan.md', 'review.txt'})

    def test_denial_has_no_file_and_one_explanation(self):
        result = self.run_report('deny')
        self.assertEqual(result['stop_reason'], 'USER_REJECTED')
        self.assertEqual(result['state'], 'unable')
        self.assertFalse(self.output.exists())
        self.assertEqual(len(self.previews), 1)
        self.assertEqual(len(self.provider.requests), 4)

    def test_target_created_during_approval_is_not_overwritten(self):
        result = self.run_report(before_decision=lambda: self.output.write_bytes(b'other writer'))
        self.assertEqual(result['stop_reason'], 'FILE_EXISTS')
        self.assertEqual(self.output.read_bytes(), b'other writer')
        self.assertEqual(result['artifacts'], [])
        self.assertEqual(len(self.provider.requests), 3)

    def test_target_race_at_publication_is_reported_as_file_exists(self):
        real_link = os.link
        def race_link(*args, **kwargs):
            self.output.write_bytes(b'other writer')
            return real_link(*args, **kwargs)
        with patch('local_agent.write_file.os.link', side_effect=race_link):
            result = self.run_report()
        self.assertEqual(result['stop_reason'], 'FILE_EXISTS')
        self.assertEqual(self.output.read_bytes(), b'other writer')
        self.assertEqual(result['artifacts'], [])

    def test_output_is_not_input_evidence_even_after_relisting(self):
        result = self.run_report()
        self.assertEqual(result['state'], 'completed')
        listing = self.engine.policy.execute_read('list_files', {'path': '.'})
        self.assertNotIn('report.md', [item['path'] for item in listing['entries']])
        denial = self.engine.policy.execute_read('read_file', {'path': 'report.md'})
        self.assertFalse(denial['ok'])
        value = {'status': 'answered', 'answer': '报告已生成',
                 'citations': [{'path': 'report.md', 'start_line': 1, 'end_line': 1}]}
        from local_agent.answers import AnswerError
        with self.assertRaises(AnswerError):
            self.engine.policy.validate(json.dumps(value))

    def test_actual_artifact_survives_final_provider_failure(self):
        result = self.run_report(question='产物后失败')
        self.assertEqual(result['stop_reason'], 'NETWORK_ERROR')
        self.assertTrue(self.output.is_file())
        self.assertEqual(result['artifacts'][0]['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())

    def test_wait_over_active_budget_does_not_expire_run(self):
        clock = [0.0]
        self.control = RunControl(threading.Event(), clock=lambda: clock[0])
        result = self.run_report(before_decision=lambda: clock.__setitem__(0, 190.0))
        self.assertEqual(result['state'], 'completed')

    def test_cancel_during_preparation_returns_without_publishing_late(self):
        self.check_stopped_preparation(cancel=True)

    def test_timeout_during_preparation_cannot_publish_late(self):
        self.check_stopped_preparation(cancel=False)

    def check_stopped_preparation(self, cancel):
        entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        outcome = []
        import local_agent.write_file as writer_module
        real_fsync = writer_module.os.fsync
        real_commit = self.engine.registry.get('write_file').commit
        def fsync(fd):
            entered.set()
            release.wait(2)
            return real_fsync(fd)
        def commit(*args, **kwargs):
            try:
                return real_commit(*args, **kwargs)
            finally:
                cleaned.set()
        with patch.object(writer_module.os, 'fsync', side_effect=fsync), \
             patch.object(self.engine.registry.get('write_file'), 'commit', side_effect=commit):
            worker = threading.Thread(target=lambda: outcome.append(self.run_report(
                config=RunConfig(tool_timeout=5 if cancel else .1))))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                if cancel:
                    self.control.cancel_run()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(outcome[0]['stop_reason'], 'CANCELLED' if cancel else 'TOOL_TIMEOUT')
                self.assertFalse(self.output.exists())
            finally:
                release.set()
                worker.join(2)
                self.assertTrue(cleaned.wait(1))
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), ['plan.md', 'review.txt'])


if __name__ == '__main__':
    unittest.main()
