import json
from pathlib import Path
import tempfile
import unittest

from local_agent.session_history import SessionHistoryTool
from local_agent.session_store import SessionStore
from local_agent.sessions import RunSubmission, SessionScope, SessionService
from local_agent.tool_runtime import ToolRegistry, ToolRuntime, valid_arguments, wire_result


class SessionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        self.store = SessionStore.open(root / "state")
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)
        self.session = self.service.create(workspace, "历史", SessionScope("directory", None))
        old = self.service.submit(self.session.id, RunSubmission(
            "old", "核对旧调用", "conversation", self.session.scope, None, None, {}))
        old.journal.record_model_request({"messages": []}, {"kind": "fixture"})
        self.call = {"id": "call-1", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({
                "path": "missing-73.md", "intent": "回查失败参数"}, ensure_ascii=False)}}
        self.assistant_id = old.journal.record_model_reply(
            {"role": "assistant", "content": None, "tool_calls": [self.call]},
            None, "tool_calls_valid")
        self.result_id = old.journal.record_tool_result("call-1", {
            "ok": False, "error": {"code": "FILE_NOT_FOUND", "message": "missing"}})
        old.journal.record_model_request({"messages": []}, {"kind": "fixture-2"})
        old.journal.record_model_reply({"role": "assistant", "content": "未找到"}, None,
                                       "answer_valid")
        old.journal.finish_run({"state": "completed", "answer": "未找到"})
        self.cutoff = self.store.connection().execute(
            "SELECT COALESCE(MAX(session_seq),0)+1 FROM messages WHERE session_id=?",
            (self.session.id,)).fetchone()[0]
        self.tool = SessionHistoryTool(self.store, self.session.id, before_seq=self.cutoff)

    def _complete(self, request_id, question, answer="已记录"):
        prepared = self.service.submit(self.session.id, RunSubmission(
            request_id, question, "conversation", self.session.scope, None, None, {}))
        prepared.journal.record_model_request({"messages": []}, {"kind": "fixture"})
        prepared.journal.record_model_reply(
            {"role": "assistant", "content": answer}, None, "answer_valid")
        prepared.journal.finish_run({"state": "completed", "answer": answer})
        return prepared.run_id

    @staticmethod
    def _wire_bytes(result, fields=None):
        return len(json.dumps(
            wire_result(result, fields), ensure_ascii=False, allow_nan=False
        ).encode("utf-8"))

    def test_search_and_read_preserve_original_call_arguments_and_result_link(self):
        found = self.tool.execute({"action": "search", "query": "missing-73.md"})
        self.assertTrue(found["ok"])
        hit = found["hits"][0]
        self.assertEqual(hit["source_kind"], "tool_call")
        page = self.tool.execute({"action": "read", "message_id": hit["message_id"]})
        self.assertEqual(page["call_id"], "call-1")
        self.assertEqual(page["name"], "read_file")
        self.assertEqual(page["source_run_id"], hit["source_run_id"])
        self.assertIn('"path":"missing-73.md"', page["text"])
        self.assertEqual(page["result_message_id"], self.result_id)
        verified = self.tool.verify_success(
            {"action": "read", "message_id": hit["message_id"]},
            {key: value for key, value in page.items() if key != "ok"},
        )
        self.assertEqual(verified["call_id"], "call-1")

    def test_verified_tool_result_is_searchable_and_paginated_by_its_message_id(self):
        old = self.service.submit(self.session.id, RunSubmission(
            "large-result", "保存长工具结果", "conversation", self.session.scope,
            None, None, {}))
        old.journal.record_model_request({"messages": []}, {"kind": "fixture"})
        call = {"id": "call-large", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({
                "path": "large.md", "intent": "核对长结果"}, ensure_ascii=False)}}
        assistant_id = old.journal.record_model_reply(
            {"role": "assistant", "content": None, "tool_calls": [call]},
            None, "tool_calls_valid")
        content = "页首-" + "甲" * 6000 + "-result-tail-91"
        result_id = old.journal.record_tool_result("call-large", {
            "ok": True, "data": {"content": content}})
        old.journal.record_model_request({"messages": []}, {"kind": "fixture-2"})
        old.journal.record_model_reply(
            {"role": "assistant", "content": "已读取"}, None, "answer_valid")
        old.journal.finish_run({"state": "completed", "answer": "已读取"})
        cutoff = self.store.connection().execute(
            "SELECT MAX(session_seq)+1 FROM messages WHERE session_id=?",
            (self.session.id,),
        ).fetchone()[0]
        tool = SessionHistoryTool(self.store, self.session.id, before_seq=cutoff)

        hit = tool.execute({"action": "search", "query": "result-tail-91"})["hits"][0]
        self.assertEqual(hit["source_kind"], "tool_result")
        self.assertEqual(hit["message_id"], result_id)
        self.assertEqual(hit["call_message_id"], assistant_id)
        self.assertEqual(hit["call_id"], "call-large")

        call_page = tool.execute({"action": "read", "message_id": assistant_id})
        self.assertTrue(call_page["ok"], call_page)
        self.assertEqual(call_page["result_message_id"], result_id)
        self.assertNotIn("result", call_page)

        pages = []
        arguments = {"action": "read", "message_id": result_id}
        while True:
            page = tool.execute(arguments)
            self.assertTrue(page["ok"], page)
            pages.append(page)
            if "cursor" not in page:
                break
            arguments = {"action": "read", "message_id": result_id,
                         "cursor": page["cursor"]}
        self.assertEqual("".join(page["text"] for page in pages), content)
        self.assertTrue(all(page["source_kind"] == "tool_result" for page in pages))
        self.assertTrue(all(page["call_message_id"] == assistant_id for page in pages))

    def test_multiple_tool_calls_share_one_stable_message_view(self):
        old = self.service.submit(self.session.id, RunSubmission(
            "multi-call", "保存多工具调用", "conversation", self.session.scope,
            None, None, {}))
        old.journal.record_model_request({"messages": []}, {"kind": "fixture"})
        calls = [
            {"id": "call-first", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({
                    "path": "first.md", "intent": "读取第一份"}, ensure_ascii=False)}},
            {"id": "call-second", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({
                    "path": "second-only-42.md", "intent": "读取第二份"}, ensure_ascii=False)}},
        ]
        assistant_id = old.journal.record_model_reply(
            {"role": "assistant", "content": None, "tool_calls": calls},
            None, "tool_calls_valid")
        first_result_id = old.journal.record_tool_result(
            "call-first", {"ok": False, "error": {
                "code": "FILE_NOT_FOUND", "message": "first missing"}})
        second_result_id = old.journal.record_tool_result(
            "call-second", {"ok": False, "error": {
                "code": "FILE_NOT_FOUND", "message": "second missing"}})
        old.journal.record_model_request({"messages": []}, {"kind": "fixture-2"})
        old.journal.record_model_reply(
            {"role": "assistant", "content": "均未找到"}, None, "answer_valid")
        old.journal.finish_run({"state": "completed", "answer": "均未找到"})
        cutoff = self.store.connection().execute(
            "SELECT MAX(session_seq)+1 FROM messages WHERE session_id=?",
            (self.session.id,),
        ).fetchone()[0]
        tool = SessionHistoryTool(self.store, self.session.id, before_seq=cutoff)

        hit = tool.execute({"action": "search", "query": "second-only-42.md"})["hits"][0]
        self.assertEqual(hit["message_id"], assistant_id)
        self.assertEqual(hit["call_id"], "call-second")
        page = tool.execute({"action": "read", "message_id": assistant_id})
        self.assertTrue(page["ok"], page)
        self.assertIn('"path":"first.md"', page["text"])
        self.assertIn('"path":"second-only-42.md"', page["text"])
        self.assertEqual(
            [(call["call_id"], call["result_message_id"]) for call in page["calls"]],
            [("call-first", first_result_id), ("call-second", second_result_id)],
        )
        result_page = tool.execute({"action": "read", "message_id": second_result_id})
        self.assertEqual(result_page["call_message_id"], assistant_id)
        self.assertEqual(result_page["call_id"], "call-second")

    def test_cutoff_and_session_boundary_hide_later_or_foreign_messages(self):
        later = self.service.submit(self.session.id, RunSubmission(
            "later", "later-secret-91", "conversation", self.session.scope, None, None, {}))
        other = self.service.create(Path(self.session.workspace_path), "其他", SessionScope("directory", None))
        foreign = self.service.submit(other.id, RunSubmission(
            "foreign", "foreign-secret-88", "conversation", other.scope, None, None, {}))
        self.assertEqual(self.tool.execute({"action": "search", "query": "later-secret-91"})["hits"], [])
        for message_id in (
            self.store.load_run_messages(self.session.id, later.run_id)[0].id,
            self.store.load_run_messages(other.id, foreign.run_id)[0].id,
        ):
            with self.subTest(message_id=message_id):
                result = self.tool.execute({"action": "read", "message_id": message_id})
                self.assertEqual(result["error"]["code"], "NOT_FOUND")

    def test_action_enum_is_enforced_by_generic_argument_validation(self):
        self.assertTrue(valid_arguments(self.tool.spec, {
            "action": "search", "query": "missing", "intent": "回查"}))
        self.assertFalse(valid_arguments(self.tool.spec, {
            "action": "delete", "query": "missing", "intent": "回查"}))

    def test_result_proof_rejects_forged_success(self):
        result = self.tool.execute({"action": "search", "query": "missing-73.md"})
        data = {key: value for key, value in result.items() if key != "ok"}
        data["hits"] = []
        with self.assertRaises(ValueError):
            self.tool.verify_success({"action": "search", "query": "missing-73.md"}, data)

    def test_search_pages_all_matches_in_stable_order_without_duplicates(self):
        for number in range(17):
            self._complete(
                f"search-page-{number}",
                f"共同标记 page-item-{number:02d}",
            )
        cutoff = self.store.connection().execute(
            "SELECT MAX(session_seq)+1 FROM messages WHERE session_id=?",
            (self.session.id,),
        ).fetchone()[0]
        tool = SessionHistoryTool(self.store, self.session.id, before_seq=cutoff)

        pages = []
        arguments = {"action": "search", "query": "共同标记"}
        while True:
            page = tool.execute(arguments)
            self.assertTrue(page["ok"], page)
            self.assertLessEqual(self._wire_bytes(page), tool.RESULT_BYTES)
            pages.append(page)
            if "cursor" not in page:
                break
            arguments = {
                "action": "search", "query": "共同标记", "cursor": page["cursor"]
            }

        self.assertEqual([len(page["hits"]) for page in pages], [8, 8, 1])
        hits = [hit for page in pages for hit in page["hits"]]
        keys = [(hit["session_seq"], hit["message_id"]) for hit in hits]
        self.assertEqual(keys, sorted(keys, reverse=True))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(hits), 17)
        self.assertNotIn("cursor", pages[-1])

    def test_search_cursor_rejects_changed_query_cutoff_session_tampering_and_read(self):
        for number in range(9):
            self._complete(f"cursor-page-{number}", f"cursor-marker-{number}")
        cutoff = self.store.connection().execute(
            "SELECT MAX(session_seq)+1 FROM messages WHERE session_id=?",
            (self.session.id,),
        ).fetchone()[0]
        tool = SessionHistoryTool(self.store, self.session.id, before_seq=cutoff)
        first = tool.execute({"action": "search", "query": "cursor-marker"})
        self.assertTrue(first["ok"], first)
        self.assertIn("cursor", first)
        cursor = first["cursor"]

        invalid_searches = [
            tool.execute({"action": "search", "query": "other", "cursor": cursor}),
            SessionHistoryTool(
                self.store, self.session.id, before_seq=cutoff + 1
            ).execute({"action": "search", "query": "cursor-marker", "cursor": cursor}),
        ]
        other = self.service.create(
            Path(self.session.workspace_path), "搜索游标隔离", SessionScope("directory", None))
        invalid_searches.append(SessionHistoryTool(
            self.store, other.id, before_seq=cutoff
        ).execute({"action": "search", "query": "cursor-marker", "cursor": cursor}))
        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        invalid_searches.append(tool.execute({
            "action": "search", "query": "cursor-marker", "cursor": tampered
        }))
        self.assertTrue(all(
            result["error"]["code"] == "CURSOR_INVALID"
            for result in invalid_searches
        ))

        message_id = first["hits"][0]["message_id"]
        mixed = tool.execute({
            "action": "read", "message_id": message_id, "cursor": cursor
        })
        self.assertEqual(mixed["error"]["code"], "CURSOR_INVALID")

        long_run_id = self._complete(
            "read-cursor-source", "read-cursor-source-" + "甲" * 9000)
        long_message = self.store.load_run_messages(self.session.id, long_run_id)[0]
        later_cutoff = self.store.connection().execute(
            "SELECT MAX(session_seq)+1 FROM messages WHERE session_id=?",
            (self.session.id,),
        ).fetchone()[0]
        later_tool = SessionHistoryTool(
            self.store, self.session.id, before_seq=later_cutoff)
        read_first = later_tool.execute({
            "action": "read", "message_id": long_message.id
        })
        self.assertIn("cursor", read_first)
        reverse_mixed = later_tool.execute({
            "action": "search", "query": "read-cursor-source",
            "cursor": read_first["cursor"],
        })
        self.assertEqual(reverse_mixed["error"]["code"], "CURSOR_INVALID")

    def test_read_pages_by_actual_wire_size_and_preserves_unicode_exactly(self):
        content = ('"\\\n' * 2100) + "🙂-最终字符"
        self.assertLess(len(content.encode("utf-8")), SessionHistoryTool.PAGE_BYTES)
        run_id = self._complete("escaped-wire", content)
        message = self.store.load_run_messages(self.session.id, run_id)[0]
        cutoff = self.store.connection().execute(
            "SELECT MAX(session_seq)+1 FROM messages WHERE session_id=?",
            (self.session.id,),
        ).fetchone()[0]
        fields = {"scope": {"discovered_files": ["x" * 600]}}
        tool = SessionHistoryTool(
            self.store, self.session.id, before_seq=cutoff,
            result_fields=lambda: fields,
        )

        pages = []
        arguments = {"action": "read", "message_id": message.id}
        while True:
            page = tool.execute(arguments)
            self.assertTrue(page["ok"], page)
            self.assertLessEqual(
                self._wire_bytes(page, fields), tool.RESULT_BYTES
            )
            pages.append(page)
            if "cursor" not in page:
                break
            self.assertGreater(page["end"], page["start"])
            arguments = {
                "action": "read", "message_id": message.id, "cursor": page["cursor"]
            }

        self.assertGreater(len(pages), 1)
        self.assertEqual("".join(page["text"] for page in pages), content)

    def test_policy_fields_too_large_return_a_bounded_history_error(self):
        fields = {"scope": {"discovered_files": ["x" * 13000]}}

        class Policy:
            def before(inner_self, _name, _arguments):
                return None

            def accept(inner_self, _name, _arguments, _result):
                return None

            def result_fields(inner_self):
                return fields

        class Control:
            @staticmethod
            def check():
                return None

        policy = Policy()
        tool = SessionHistoryTool(
            self.store, self.session.id, before_seq=self.cutoff,
            result_fields=policy.result_fields,
        )
        runtime = ToolRuntime(ToolRegistry([tool]), policy)
        invocation = runtime.invoke(
            {"id": "history-overhead", "type": "function", "function": {
                "name": "session_history",
                "arguments": json.dumps({
                    "action": "search", "query": "absent-value", "intent": "查找旧记录"
                }, ensure_ascii=False),
            }},
            budget_ok=True,
            execute_bounded=lambda callback, _timeout: callback(),
            emit=lambda _event, _data: None,
            control=Control(),
        )

        self.assertFalse(invocation.result["ok"])
        self.assertEqual(invocation.result["error"]["code"], "SESSION_STORE_ERROR")
        self.assertLessEqual(
            len(json.dumps(
                invocation.result, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")),
            tool.RESULT_BYTES,
        )

        invalid_tool = SessionHistoryTool(
            self.store, self.session.id, before_seq=self.cutoff,
            result_fields=policy.result_fields,
        )
        invalid = ToolRuntime(ToolRegistry([invalid_tool]), policy).invoke(
            {"id": "history-invalid", "type": "function", "function": {
                "name": "session_history",
                "arguments": json.dumps({
                    "action": "search", "intent": "查找旧记录"
                }, ensure_ascii=False),
            }},
            budget_ok=True,
            execute_bounded=lambda callback, _timeout: callback(),
            emit=lambda _event, _data: None,
            control=Control(),
        )
        self.assertEqual(invalid.result["error"]["code"], "INVALID_ARGUMENT")
        self.assertLessEqual(
            len(json.dumps(
                invalid.result, ensure_ascii=False, allow_nan=False
            ).encode("utf-8")),
            invalid_tool.RESULT_BYTES,
        )

    def test_read_cursor_is_bound_to_session_cutoff_message_and_offset(self):
        old = self.service.submit(self.session.id, RunSubmission(
            "long-old", "页首-" + "甲" * 9000 + "-页尾", "conversation",
            self.session.scope, None, None, {}))
        old.journal.record_model_request({"messages": []}, {"kind": "fixture"})
        old.journal.record_model_reply(
            {"role": "assistant", "content": "已保存长消息"}, None, "answer_valid")
        old.journal.finish_run({"state": "completed", "answer": "已保存长消息"})
        message = self.store.load_run_messages(self.session.id, old.run_id)[0]
        cutoff = self.store.connection().execute(
            "SELECT MAX(session_seq)+1 FROM messages WHERE session_id=?",
            (self.session.id,),
        ).fetchone()[0]
        tool = SessionHistoryTool(self.store, self.session.id, before_seq=cutoff)

        first = tool.execute({"action": "read", "message_id": message.id})
        self.assertTrue(first["ok"])
        self.assertIn("cursor", first)
        pages = [first]
        while "cursor" in pages[-1]:
            page = tool.execute({
                "action": "read",
                "message_id": message.id,
                "cursor": pages[-1]["cursor"],
            })
            self.assertTrue(page["ok"])
            pages.append(page)
        self.assertEqual(
            "".join(page["text"] for page in pages), message.payload["content"])
        self.assertTrue(all(page["source_run_id"] == old.run_id for page in pages))

        changed_cutoff = SessionHistoryTool(
            self.store, self.session.id, before_seq=cutoff + 1)
        self.assertEqual(changed_cutoff.execute({
            "action": "read", "message_id": message.id, "cursor": first["cursor"]
        })["error"]["code"], "CURSOR_INVALID")

        other = self.service.create(
            Path(self.session.workspace_path), "游标隔离", SessionScope("directory", None))
        foreign = SessionHistoryTool(self.store, other.id, before_seq=10_000)
        self.assertEqual(foreign.execute({
            "action": "read", "message_id": message.id, "cursor": first["cursor"]
        })["error"]["code"], "NOT_FOUND")

        tampered = first["cursor"][:-1] + ("A" if first["cursor"][-1] != "A" else "B")
        self.assertEqual(tool.execute({
            "action": "read", "message_id": message.id, "cursor": tampered
        })["error"]["code"], "CURSOR_INVALID")


if __name__ == "__main__":
    unittest.main()
