import json
from pathlib import Path
import tempfile
import unittest

from local_agent.session_history import SessionHistoryTool
from local_agent.session_store import SessionStore
from local_agent.sessions import RunSubmission, SessionScope, SessionService
from local_agent.tool_runtime import valid_arguments


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
