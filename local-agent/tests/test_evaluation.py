import importlib
import json
from pathlib import Path
import tempfile
import unittest


class EvaluationProvider:
    metadata = {'provider': 'evaluation-test', 'model': 'test', 'simulated': True}

    def __init__(self, fail=False):
        self.fail = fail

    def complete(self, messages, tools, timeout):
        from local_agent.provider import ModelReply
        task = json.loads(messages[1]['content'])
        if messages[-1]['role'] != 'tool':
            self.initial = json.dumps(messages, ensure_ascii=False)
            return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'read1', 'type': 'function', 'function': {'name': 'read_file',
                'arguments': json.dumps({'path': task['file'], 'intent': '核对测试资料'})}}]})
        observed = json.loads(messages[-1]['content'])
        status, answer, citations = 'unable', '文件无法读取。', []
        if observed['ok']:
            content = observed['data']['content']
            lines = list(content.values()) if isinstance(content, dict) else content.splitlines()
            content = '\n'.join(lines)
            assert content not in self.initial
            if '预算' in task['question']:
                status, answer = 'not_found', '文件未说明预算。'
            else:
                status, answer = 'answered', content
                citations = [{'path': task['file'], 'start_line': 1, 'end_line': len(lines),
                              'quote': '\n'.join(lines)}]
        if self.fail:
            answer = '故意遗漏关键事实'
        return ModelReply({'role': 'assistant', 'content': json.dumps(
            {'status': status, 'answer': answer, 'citations': citations}, ensure_ascii=False)})


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('local_agent.evaluation'),
                             'Evaluation implementation is missing')
        self.m = importlib.import_module('local_agent.evaluation')

    def test_all_12_trials_recorded_but_simulation_never_passes_live_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.m.evaluate(EvaluationProvider, Path(tmp))
            self.assertEqual(len(report['trials']), 12)
            self.assertTrue(report['automatic_checks_passed'])
            self.assertEqual(report['gate'], 'SIMULATED_ONLY')
            self.assertEqual(report['human_review'], 'pending')
            self.assertEqual({t['scenario'] for t in report['trials']},
                             {'known', 'not_found', 'missing_file', 'changed'})
            self.assertTrue(Path(report['report_path']).is_file())

    def test_explicit_absence_accepts_clear_equivalent_phrasings(self):
        for answer in ('文件未说明预算。', '文件中没有提及预算信息。',
                       '预算未记载。', '没有说明预算金额。',
                       '全部已读文件均未记载任何预算金额。', '两份资料均未提及任何预算数额。',
                       '本次发现的受支持文件未说明所问的预算信息。'):
            with self.subTest(answer=answer):
                self.assertTrue(self.m.explicit_absence(answer, '预算'))

    def test_explicit_absence_rejects_vague_or_positive_answers(self):
        for answer in ('不知道。', '文件没有说明日期。', '预算是 100 万元。',
                       '未记载任何日期，预算为 100 万元。', '文件已记载预算。',
                       '未说明所问的日期，预算为 100 万元。', None):
            with self.subTest(answer=answer):
                self.assertFalse(self.m.explicit_absence(answer, '预算'))

    def test_date_fact_accepts_only_equivalent_calendar_dates(self):
        for answer in ('演示日期：2026-09-06。', '演示日期为2026年9月6日。',
                       '演示日期为2026年09月06日。'):
            with self.subTest(answer=answer):
                self.assertTrue(self.m.contains_fact(answer, '2026-09-06'))
        for answer in ('2026年9月7日', '2026年10月6日', '2025年9月6日',
                       '2026年9月', '12026-09-06', '2026-09-060',
                       '12026年9月6日', '2026-99-99', None):
            with self.subTest(answer=answer):
                self.assertFalse(self.m.contains_fact(answer, '2026-09-06'))
        self.assertFalse(self.m.contains_fact('2026年2月30日', '2026-02-30'))

    def test_non_date_facts_still_require_exact_text(self):
        self.assertTrue(self.m.contains_fact('项目代号：青禾-123；评审人：林澄-abcd。', '青禾-123'))
        self.assertTrue(self.m.contains_fact('评审人：林澄-abcd。', '林澄-abcd'))
        self.assertFalse(self.m.contains_fact('项目代号：紫杉-123。', '青禾-123'))
        self.assertFalse(self.m.contains_fact('评审人：林澄。', '林澄-abcd'))

    def test_equivalent_date_format_passes_single_file_fact_checks(self):
        class ChineseDateProvider(EvaluationProvider):
            def complete(self, messages, tools, timeout):
                reply = super().complete(messages, tools, timeout)
                if reply.message.get('content'):
                    value = json.loads(reply.message['content'])
                    value['answer'] = value['answer'].replace('2026-09-16', '2026年9月16日')
                    reply.message['content'] = json.dumps(value, ensure_ascii=False)
                return reply
        with tempfile.TemporaryDirectory() as tmp:
            report = self.m.evaluate(ChineseDateProvider, Path(tmp))
            self.assertTrue(report['automatic_checks_passed'])
            self.assertEqual(report['gate'], 'SIMULATED_ONLY')

    def test_any_semantic_failure_blocks_entire_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self.m.evaluate(lambda: EvaluationProvider(fail=True), Path(tmp))
            self.assertFalse(report['automatic_checks_passed'])
            self.assertEqual(report['gate'], 'FAILED')
            self.assertEqual(len(report['trials']), 12)

    def test_acceptance_cannot_reduce_required_repetitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            for repeats in (1, 2, True):
                with self.subTest(repeats=repeats), self.assertRaises(ValueError):
                    self.m.evaluate(EvaluationProvider, Path(tmp), repeats=repeats)

    def test_missing_key_is_not_run_and_never_a_pass(self):
        from local_agent.provider import ProviderError
        def missing():
            raise ProviderError('CONFIG_MISSING')
        with tempfile.TemporaryDirectory() as tmp:
            report = self.m.evaluate(missing, Path(tmp))
            self.assertEqual(report['gate'], 'NOT_RUN')
            self.assertFalse(report['automatic_checks_passed'])
            self.assertEqual(report['trials'], [])


if __name__ == '__main__':
    unittest.main()
