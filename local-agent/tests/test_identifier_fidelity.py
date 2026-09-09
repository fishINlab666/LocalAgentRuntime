import json
import unittest

from local_agent.answers import AnswerError, validate_answer


class IdentifierFidelityTests(unittest.TestCase):
    def validate(self, text, source='项目代号：青禾-47。', *, directory=False,
                 status='answered', extra=None, permitted=None):
        snapshots = {'note.md': {'ok': True, 'path': 'note.md', 'content': source}}
        snapshots.update(extra or {})
        coverage = {'attempted': True, 'had_error': status == 'unable',
                    'listed_directories': ['.'], 'unlisted_directories': [],
                    'discovered_files': ['note.md'], 'read_files': permitted or ['note.md'],
                    'unread_files': ['broken.md'] if status == 'unable' else [],
                    'complete': status != 'unable'}
        value = {'status': status, 'answer': text, 'citations': [
            {'path': 'note.md', 'start_line': 1, 'end_line': 1}] if status == 'answered' else []}
        return validate_answer(json.dumps(value, ensure_ascii=False), snapshots,
                               None if directory else 'note.md', True, coverage=coverage)

    def assert_mismatch(self, text, source='项目代号：青禾-47。', **kwargs):
        with self.assertRaises(AnswerError) as caught:
            self.validate(text, source, **kwargs)
        self.assertEqual(caught.exception.code, 'IDENTIFIER_MISMATCH')
        self.assertTrue(caught.exception.repairable)

    def test_saved_web_failure_is_rejected_even_when_citation_is_exact(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                self.assert_mismatch('依据本次读取的资料，项目代号为“青禾—47”（出处：note.md）。',
                                     directory=directory)

    def test_confusable_dash_changes_are_rejected_in_both_directions(self):
        for dash in '‐‑‒–—―−﹣－':
            with self.subTest(dash=dash):
                self.assert_mismatch('项目代号为青禾' + dash + '47。')
                self.assert_mismatch('项目代号为青禾-47。', '项目代号：青禾' + dash + '47。')

    def test_correct_spelling_is_returned_without_rewriting(self):
        for code in ('青禾-47', '青禾—47', 'MODEL_2-X9', 'AB12-xy-9'):
            with self.subTest(code=code):
                text = '项目代号为“' + code + '”。'
                value = self.validate(text, '项目代号：' + code + '。')
                self.assertEqual(value['answer'], text)
                self.assertEqual(value['citations'][0]['quote'], '项目代号：' + code + '。')

    def test_dates_and_numeric_ranges_are_not_identifier_errors(self):
        for source, answer in [('日期：2026-09-10；区间：10-20。', '日期为2026年9月10日，区间10–20。'),
                               ('日期为2026-09-10。', '日期为2026–09–10。')]:
            with self.subTest(source=source):
                self.assertEqual(self.validate(answer, source)['answer'], answer)

    def test_longer_identifiers_do_not_match_a_shorter_source_prefix(self):
        for source, answer in [('编号：ABC-47。', '编号为ABC—470。'),
                               ('编号：ABC-47。', '编号为ABC—47-X9。'),
                               ('编号：ABC-47。', '编号为PREFIX-ABC—47。'),
                               ('编号：青禾-47。\n另一个编号：青禾—47乙。', '编号为青禾—47乙。'),
                               ('编号：ABC-47。\n另一个编号：XABC—47。', '编号为XABC—47。')]:
            with self.subTest(source=source):
                self.assertEqual(self.validate(answer, source)['answer'], answer)

    def test_date_suffix_does_not_exempt_an_identifier_with_a_prefix(self):
        for code in ('RUN-2026-09-10', '青禾-2026-09-10', 'ID2026-09-10'):
            with self.subTest(code=code):
                self.assert_mismatch(code.replace('-', '—'), '编号：' + code + '。')

    def test_multiple_source_spellings_do_not_get_silently_normalized(self):
        source = '一个编号：青禾-47。\n另一个编号：青禾—47。'
        for code in ('青禾-47', '青禾—47'):
            with self.subTest(code=code):
                self.assertEqual(self.validate(code, source)['answer'], code)

    def test_only_current_authorized_read_snapshots_supply_identifiers(self):
        extra = {'other.md': {'ok': True, 'path': 'other.md', 'content': '编号：OTHER-8。'}}
        for directory in (False, True):
            with self.subTest(directory=directory):
                text = 'OTHER—8'
                self.assertEqual(self.validate(text, directory=directory, extra=extra)['answer'], text)

    def test_absent_and_unable_partial_facts_are_also_checked(self):
        for status in ('not_found', 'unable'):
            with self.subTest(status=status):
                self.assert_mismatch('已读资料记载青禾—47，但所问信息未能完整核对。',
                                     directory=True, status=status)

    def test_general_semantics_and_unrelated_names_remain_outside_this_guard(self):
        text = '评审人是李四，代号为完全不同的名称。'
        self.assertEqual(self.validate(text)['answer'], text)


if __name__ == '__main__':
    unittest.main()
