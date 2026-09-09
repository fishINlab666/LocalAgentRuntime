import json
import unittest

from local_agent.answers import AnswerError, validate_answer


class AnswerValidationTests(unittest.TestCase):
    def setUp(self):
        self.target = "docs/notes.md"
        self.snapshots = {self.target: {
            "ok": True,
            "path": self.target,
            "content": "第一行\nsecond line\n\nlast line\n",
            "line_count": 4,
            "bytes": 37,
            "sha256": "test-snapshot",
        }}
        self.citation = {"path": self.target, "start_line": 1, "end_line": 2,
                         "quote": "第一行\nsecond line"}
        self.answer = {"status": "answered", "answer": "答案", "citations": [self.citation]}

    def validate(self, value=None, *, snapshots=None, attempted=True):
        return validate_answer(json.dumps(self.answer if value is None else value, ensure_ascii=False),
                               self.snapshots if snapshots is None else snapshots,
                               self.target, attempted)

    def assert_invalid(self, value, code="INVALID_ANSWER", *, snapshots=None, attempted=True):
        with self.assertRaises(AnswerError) as caught:
            self.validate(value, snapshots=snapshots, attempted=attempted)
        self.assertEqual(caught.exception.code, code)

    def test_accepts_answer_with_exact_multiline_citation(self):
        self.assertEqual(self.validate(), self.answer)

    def test_builds_exact_quote_from_this_runs_snapshot(self):
        snapshot = {self.target: {**self.snapshots[self.target],
                                 'content': '# 记录\n\n先接 API。  \n\t再读文件。\n'}}
        anchor = {'path': self.target, 'start_line': 2, 'end_line': 4}
        answer = {**self.answer, 'citations': [anchor]}
        value = self.validate(answer, snapshots=snapshot)
        self.assertEqual(value['citations'], [{**anchor, 'quote': '\n先接 API。  \n\t再读文件。'}])
        self.assertEqual(value['answer'], answer['answer'])

    def test_anchor_requires_current_read_and_valid_path_and_range(self):
        anchor = {'path': self.target, 'start_line': 1, 'end_line': 2}
        self.assert_invalid({**self.answer, 'citations': [anchor]}, 'MISSING_READ', snapshots={})
        for changed in [{'path': 'other.md'}, {'start_line': 0}, {'end_line': 5},
                        {'start_line': True}, {'end_line': '2'}, {'start_line': 3},
                        {'quote': '改写过的内容'}]:
            with self.subTest(changed=changed):
                self.assert_invalid({**self.answer, 'citations': [{**anchor, **changed}]}, 'INVALID_CITATION')

    def test_anchor_uses_latest_snapshot_not_earlier_content(self):
        anchor = {'path': self.target, 'start_line': 1, 'end_line': 1}
        snapshot = {self.target: {**self.snapshots[self.target], 'content': '新的文件内容。  \n'}}
        result = self.validate({**self.answer, 'citations': [anchor]}, snapshots=snapshot)
        self.assertEqual(result['citations'][0]['quote'], '新的文件内容。  ')

    def test_error_is_value_error_with_stable_code(self):
        self.assertTrue(issubclass(AnswerError, ValueError))
        with self.assertRaises(AnswerError) as caught:
            validate_answer("bad", self.snapshots, self.target, True)
        self.assertEqual(caught.exception.code, "INVALID_ANSWER")

    def test_rejects_non_json_wrappers_and_multiple_values(self):
        encoded = json.dumps(self.answer)
        for text in ["```json\n" + encoded + "\n```", "Here: " + encoded,
                     encoded + " explanation", encoded + encoded, "", "{bad}"]:
            with self.subTest(text=text):
                with self.assertRaises(AnswerError) as caught:
                    validate_answer(text, self.snapshots, self.target, True)
                self.assertEqual(caught.exception.code, "INVALID_ANSWER")

    def test_rejects_non_text_input(self):
        for text in [None, b"{}", {}, 1]:
            with self.subTest(text=text):
                with self.assertRaises(AnswerError) as caught:
                    validate_answer(text, self.snapshots, self.target, True)
                self.assertEqual(caught.exception.code, "INVALID_ANSWER")

    def test_rejects_duplicate_json_fields(self):
        text = '{"status":"unable","status":"unable","answer":"x","citations":[]}'
        with self.assertRaises(AnswerError) as caught:
            validate_answer(text, {}, self.target, True)
        self.assertEqual(caught.exception.code, "INVALID_ANSWER")

    def test_rejects_nonstandard_json_numbers(self):
        for constant in ["NaN", "Infinity", "-Infinity"]:
            text = '{"status":"unable","answer":' + constant + ',"citations":[]}'
            with self.subTest(constant=constant), self.assertRaises(AnswerError) as caught:
                validate_answer(text, {}, self.target, True)
            self.assertEqual(caught.exception.code, "INVALID_ANSWER")

    def test_huge_json_integer_has_stable_answer_error(self):
        text = '{"status":"unable","answer":' + ("9" * 10000) + ',"citations":[]}'
        with self.assertRaises(ValueError) as caught:
            validate_answer(text, {}, self.target, True)
        self.assertIsInstance(caught.exception, AnswerError)
        self.assertEqual(caught.exception.code, "INVALID_ANSWER")

    def test_requires_exact_root_fields(self):
        for value in [[], None, "text", 4, {**self.answer, "extra": True},
                      {k: v for k, v in self.answer.items() if k != "answer"}]:
            with self.subTest(value=value):
                text = json.dumps(value)
                with self.assertRaises(AnswerError) as caught:
                    validate_answer(text, self.snapshots, self.target, True)
                self.assertEqual(caught.exception.code, "INVALID_ANSWER")

    def test_requires_valid_status_nonempty_answer_and_citation_list(self):
        for field, values in [("status", ["ok", "ANSWERED", None, 1, []]),
                              ("answer", ["", " \n\t", 1, [], None]),
                              ("citations", [None, {}, "quote"])]:
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_invalid({**self.answer, field: value})

    def test_answered_requires_successful_target_snapshot(self):
        bad_snapshots = [{}, {"other.md": self.snapshots[self.target]},
                         {self.target: {"ok": False, "error": {"code": "READ_ERROR"}}},
                         {self.target: {**self.snapshots[self.target], "path": "other.md"}}]
        for snapshots in bad_snapshots:
            with self.subTest(snapshots=snapshots):
                self.assert_invalid(self.answer, "MISSING_READ", snapshots=snapshots)

    def test_answered_requires_at_least_one_citation(self):
        self.assert_invalid({**self.answer, "citations": []}, "MISSING_CITATION")

    def test_not_found_requires_successful_target_read(self):
        value = {"status": "not_found", "answer": "没有找到", "citations": []}
        self.assertEqual(self.validate(value), value)
        self.assert_invalid(value, "MISSING_READ", snapshots={})

    def test_not_found_allows_valid_citations(self):
        value = {**self.answer, "status": "not_found"}
        self.assertEqual(self.validate(value), value)

    def test_unable_requires_attempt_without_success(self):
        value = {"status": "unable", "answer": "无法读取", "citations": []}
        self.assertEqual(self.validate(value, snapshots={}), value)
        self.assert_invalid(value, "MISSING_READ", snapshots={}, attempted=False)
        self.assert_invalid(value, "INVALID_ANSWER")

    def test_unable_rejects_citations(self):
        self.assert_invalid({**self.answer, "status": "unable"}, "INVALID_CITATION", snapshots={})

    def test_citation_requires_exact_fields(self):
        for citation in [None, "text", [], {**self.citation, "extra": True},
                         {k: v for k, v in self.citation.items() if k != "end_line"}]:
            with self.subTest(citation=citation):
                self.assert_invalid({**self.answer, "citations": [citation]}, "INVALID_CITATION")

    def test_citation_must_use_exact_target_path(self):
        for path in ["notes.md", "docs/../docs/notes.md", "/docs/notes.md", 1, None]:
            with self.subTest(path=path):
                citation = {**self.citation, "path": path}
                self.assert_invalid({**self.answer, "citations": [citation]}, "INVALID_CITATION")

    def test_line_numbers_are_strict_integers(self):
        for field in ["start_line", "end_line"]:
            for number in [True, False, 1.0, "1", None]:
                with self.subTest(field=field, number=number):
                    citation = {**self.citation, field: number}
                    self.assert_invalid({**self.answer, "citations": [citation]}, "INVALID_CITATION")

    def test_line_range_must_be_valid(self):
        for start, end in [(0, 1), (-1, 1), (3, 2), (1, 5), (5, 5)]:
            with self.subTest(start=start, end=end):
                citation = {**self.citation, "start_line": start, "end_line": end}
                self.assert_invalid({**self.answer, "citations": [citation]}, "INVALID_CITATION")

    def test_quote_must_equal_complete_lines_exactly(self):
        for quote in ["第一行", "第一行\nsecond line\n", "第一行\r\nsecond line",
                      " 第一行\nsecond line", "第一行\nSECOND line", None, 42]:
            with self.subTest(quote=quote):
                citation = {**self.citation, "quote": quote}
                self.assert_invalid({**self.answer, "citations": [citation]}, "INVALID_CITATION")

    def test_blank_line_is_a_valid_exact_citation(self):
        citation = {"path": self.target, "start_line": 3, "end_line": 3, "quote": ""}
        value = {**self.answer, "citations": [citation]}
        self.assertEqual(self.validate(value), value)

    def test_uses_current_snapshot_content(self):
        newer = {self.target: {**self.snapshots[self.target], "content": "new content\n"}}
        self.assert_invalid(self.answer, "INVALID_CITATION", snapshots=newer)

    def test_does_not_judge_semantics_of_answer(self):
        value = {**self.answer, "answer": "这段答案的语义与引用无关，但结构合法。"}
        self.assertEqual(self.validate(value), value)


class DirectoryAnswerTests(unittest.TestCase):
    def setUp(self):
        self.snapshots = {path: {'ok': True, 'path': path, 'content': content} for path, content in {
            'plan.md': '项目代号：杉木-19\n目标：先读文件。  \n',
            'notes/review.txt': '评审人：顾宁\n日期：2026-10-11\n',
        }.items()}
        self.coverage = {'attempted': True, 'had_error': False, 'listed_directories': ['.', 'notes'],
                         'unlisted_directories': [], 'discovered_files': list(self.snapshots),
                         'read_files': list(self.snapshots), 'unread_files': [], 'complete': True}
        self.answer = {'status': 'answered', 'answer': '杉木-19，顾宁于 2026-10-11 评审。', 'citations': [
            {'path': 'plan.md', 'start_line': 1, 'end_line': 2},
            {'path': 'notes/review.txt', 'start_line': 1, 'end_line': 2}]}

    def validate(self, value=None, snapshots=None, coverage=None):
        return validate_answer(json.dumps(self.answer if value is None else value, ensure_ascii=False),
                               self.snapshots if snapshots is None else snapshots, None, False,
                               coverage=self.coverage if coverage is None else coverage)

    def test_resolves_each_citation_from_its_own_successful_snapshot(self):
        result = self.validate()
        self.assertEqual([c['quote'] for c in result['citations']],
                         ['项目代号：杉木-19\n目标：先读文件。  ', '评审人：顾宁\n日期：2026-10-11'])

    def test_unread_or_failed_file_is_not_citable(self):
        for snapshots in [{'plan.md': self.snapshots['plan.md']},
                          {**self.snapshots, 'notes/review.txt': {'ok': False}},
                          {**self.snapshots, 'notes/review.txt': {**self.snapshots['notes/review.txt'], 'path': 'wrong.txt'}}]:
            with self.subTest(snapshots=snapshots), self.assertRaises(AnswerError) as caught:
                self.validate(snapshots=snapshots)
            self.assertEqual(caught.exception.code, 'INVALID_CITATION')

    def test_answer_cannot_use_revoked_snapshot_after_failed_reread(self):
        coverage = {**self.coverage, 'read_files': ['plan.md'], 'unread_files': ['notes/review.txt'],
                    'had_error': True, 'complete': False}
        with self.assertRaises(AnswerError) as caught:
            self.validate(coverage=coverage)
        self.assertEqual(caught.exception.code, 'INVALID_CITATION')

    def test_listing_alone_does_not_authorize_answer(self):
        coverage = {**self.coverage, 'read_files': [], 'unread_files': list(self.snapshots), 'complete': False}
        with self.assertRaises(AnswerError) as caught:
            self.validate(snapshots={}, coverage=coverage)
        self.assertEqual(caught.exception.code, 'MISSING_READ')

    def test_not_found_requires_complete_scope_and_valid_reads(self):
        absent = {'status': 'not_found', 'answer': '本次发现的文件未说明预算。', 'citations': []}
        self.assertEqual(self.validate(absent), absent)
        for coverage in [{**self.coverage, 'complete': False},
                         {**self.coverage, 'unlisted_directories': ['notes']},
                         {**self.coverage, 'unread_files': ['notes/review.txt']}]:
            with self.subTest(coverage=coverage), self.assertRaises(AnswerError) as caught:
                self.validate(absent, coverage=coverage)
            self.assertEqual(caught.exception.code, 'INCOMPLETE_SEARCH')
        with self.assertRaises(AnswerError) as caught:
            self.validate(absent, snapshots={})
        self.assertEqual(caught.exception.code, 'INCOMPLETE_SEARCH')

    def test_empty_supported_scope_can_return_not_found(self):
        coverage = {**self.coverage, 'listed_directories': ['.'], 'discovered_files': [], 'read_files': []}
        absent = {'status': 'not_found', 'answer': '工作区没有可读取的文本文件。', 'citations': []}
        self.assertEqual(self.validate(absent, snapshots={}, coverage=coverage), absent)

    def test_unable_needs_actual_error_and_incomplete_scope(self):
        unable = {'status': 'unable', 'answer': '文件无法读取，无法完成核对。', 'citations': []}
        failed = {**self.coverage, 'had_error': True, 'read_files': [],
                  'unread_files': list(self.snapshots), 'complete': False}
        self.assertEqual(self.validate(unable, snapshots={}, coverage=failed), unable)
        for coverage in [self.coverage, {**failed, 'had_error': False}, {**failed, 'attempted': False}]:
            with self.subTest(coverage=coverage), self.assertRaises(AnswerError):
                self.validate(unable, coverage=coverage)

    def test_supplied_bad_quote_is_still_rejected_in_directory_mode(self):
        self.answer['citations'][0]['quote'] = '模型改写过的原文'
        with self.assertRaises(AnswerError) as caught:
            self.validate()
        self.assertEqual(caught.exception.code, 'INVALID_CITATION')


if __name__ == "__main__":
    unittest.main()
