import json
from pathlib import Path
import tempfile
import unittest

from local_agent.directory_evaluation import evaluate_directory, context_checks
from local_agent.provider import ModelReply, ProviderError
from test_directory_runtime import listing, reads


class DirectoryEvaluationProvider:
    metadata = {'provider': 'directory-evaluation-test', 'model': 'none', 'simulated': True}

    def __init__(self, omit_citation=False):
        self.omit_citation = omit_citation

    def complete(self, messages, tools, timeout):
        results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        if not results:
            return ModelReply(listing())
        if len(results) == 1:
            return ModelReply(reads(*(entry['path'] for entry in results[0]['data']['entries'])))
        question = json.loads(messages[1]['content'])['question']
        returned = results[1:]
        if any(not item['ok'] for item in returned):
            status, answer, citations = 'unable', '一份资料无法读取，无法完成核对。', []
        elif '预算' in question:
            status, answer, citations = 'not_found', '本次发现的文件未说明预算。', []
        else:
            status, answer, citations = 'answered', '', []
            for item in returned:
                lines = list(item['data']['content'].values()) if isinstance(item['data']['content'], dict) else item['data']['content'].splitlines()
                answer += '\n'.join(lines) + '\n'
                citations.append({'path': item['data']['path'], 'start_line': 1, 'end_line': len(lines)})
            if self.omit_citation:
                citations = citations[:1]
        return ModelReply({'role': 'assistant', 'content': json.dumps({
            'status': status, 'answer': answer, 'citations': citations}, ensure_ascii=False)})


class DirectoryEvaluationTests(unittest.TestCase):
    def test_all_twelve_trials_check_real_tool_round_trip_but_simulation_is_not_live_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_directory(DirectoryEvaluationProvider, Path(tmp))
            self.assertEqual(len(report['trials']), 12)
            self.assertEqual(report['gate'], 'SIMULATED_ONLY')
            self.assertTrue(report['automatic_checks_passed'])
            self.assertEqual(report['human_review'], 'pending')
            self.assertEqual({t['scenario'] for t in report['trials']}, {'known', 'changed', 'not_found', 'read_error'})
            self.assertTrue(all(t['checks']['tool_results_in_next_context'] for t in report['trials']))
            self.assertTrue(all(t['checks']['both_reads_attempted'] for t in report['trials'] if t['scenario'] != 'read_error'))
            self.assertTrue(all(t['checks']['failed_file_attempted'] for t in report['trials'] if t['scenario'] == 'read_error'))
            filenames = [tuple(t['files']) for t in report['trials']]
            self.assertEqual(len(set(filenames)), 9)
            self.assertEqual(filenames[:3], filenames[3:6])
            for original, changed in zip(report['trials'][:3], report['trials'][3:6]):
                self.assertNotEqual(original['expected_facts'][0], changed['expected_facts'][0])
            self.assertEqual(json.loads(Path(report['report_path']).read_text()), report)

    def test_one_file_citation_cannot_pass_cross_file_fact_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_directory(lambda: DirectoryEvaluationProvider(omit_citation=True), Path(tmp))
            self.assertEqual(report['gate'], 'FAILED')
            self.assertEqual(len(report['trials']), 12)
            self.assertTrue(any(not t['checks'].get('both_files_cited', True) for t in report['trials']))

    def test_equivalent_date_format_preserves_cross_file_citation_checks(self):
        class ChineseDateProvider(DirectoryEvaluationProvider):
            def complete(self, messages, tools, timeout):
                reply = super().complete(messages, tools, timeout)
                if reply.message.get('content'):
                    value = json.loads(reply.message['content'])
                    value['answer'] = value['answer'].replace('2026-10-11', '2026年10月11日')
                    reply.message['content'] = json.dumps(value, ensure_ascii=False)
                return reply
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_directory(ChineseDateProvider, Path(tmp))
            self.assertTrue(report['automatic_checks_passed'])
            self.assertEqual(report['gate'], 'SIMULATED_ONLY')
            self.assertTrue(all(t['checks']['both_files_cited'] for t in report['trials']
                                if t['scenario'] in ('known', 'changed')))

    def test_actual_read_error_can_stop_without_reading_the_other_file(self):
        class StopOnError(DirectoryEvaluationProvider):
            def complete(self, messages, tools, timeout):
                results = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
                if len(results) == 1:
                    tiny = [entry['path'] for entry in results[0]['data']['entries'] if entry.get('bytes', 100) < 20]
                    if tiny:
                        return ModelReply(reads(*tiny))
                return super().complete(messages, tools, timeout)
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_directory(StopOnError, Path(tmp))
            self.assertTrue(report['automatic_checks_passed'])

    def test_context_audit_detects_modified_or_missing_correlated_tool_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_directory(DirectoryEvaluationProvider, Path(tmp))
            trial = report['trials'][0]
            events = [json.loads(line) for line in Path(trial['result']['trace_path']).read_text().splitlines()]
            self.assertTrue(context_checks(events)['tool_results_in_next_context'])
            requested = [e for e in events if e['event'] == 'model.requested'][-1]
            requested['data']['messages'][-1]['tool_call_id'] = 'wrong-id'
            self.assertFalse(context_checks(events)['tool_results_in_next_context'])

    def test_missing_key_records_not_run(self):
        def missing():
            raise ProviderError('CONFIG_MISSING')
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_directory(missing, Path(tmp))
            self.assertEqual(report['gate'], 'NOT_RUN')
            self.assertEqual(report['trials'], [])

    def test_malformed_first_call_is_a_failed_check_not_a_broken_report(self):
        events = [{'event': 'tool.requested', 'data': {'id': 'x', 'name': 'list_files', 'arguments': '{bad'}}]
        self.assertFalse(context_checks(events)['first_tool_is_root_listing'])

    def test_required_repetitions_cannot_be_reduced(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                evaluate_directory(DirectoryEvaluationProvider, Path(tmp), repeats=1)


if __name__ == '__main__':
    unittest.main()
