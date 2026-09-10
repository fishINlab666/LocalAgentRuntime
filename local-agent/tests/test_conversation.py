from pathlib import Path
import json
import tempfile
import unittest

from local_agent.conversation import ConversationPolicy
from local_agent.session_store import SessionStore
from local_agent.sessions import RunSubmission, SessionScope, SessionService


class ConversationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        self.store = SessionStore.open(root / "state")
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)
        self.session = self.service.create(workspace, "约定", SessionScope("directory", None))
        old = self.service.submit(self.session.id, RunSubmission(
            "old", "不要表格，改成三段文字", "conversation", self.session.scope,
            None, None, {}))
        self.message = self.store.load_run_messages(self.session.id, old.run_id)[0]

    def test_answer_reference_is_verified_against_visible_original_text(self):
        policy = ConversationPolicy(self.store, self.session.id, before_seq=99)
        policy.set_visible_messages([self.message.id])
        start = self.message.payload["content"].index("三段文字")
        answer = policy.validate(json.dumps({
            "status": "answered",
            "answer": "上次要求改成三段文字。",
            "references": [{"message_id": self.message.id, "start": start,
                            "end": start + len("三段文字")}],
        }, ensure_ascii=False))
        self.assertEqual(answer["references"][0]["quote"], "三段文字")

    def test_invisible_or_out_of_range_reference_is_rejected(self):
        policy = ConversationPolicy(self.store, self.session.id, before_seq=99)
        for reference in (
            {"message_id": self.message.id, "start": 0, "end": 2},
            {"message_id": "other", "start": 0, "end": 2},
        ):
            with self.subTest(reference=reference), self.assertRaisesRegex(Exception, "INVALID_REFERENCE"):
                policy.validate(json.dumps({"status": "answered", "answer": "结论",
                                            "references": [reference]}))

    def test_not_found_requires_a_real_history_query(self):
        policy = ConversationPolicy(self.store, self.session.id, before_seq=99)
        payload = json.dumps({"status": "not_found", "answer": "未找到", "references": []})
        with self.assertRaisesRegex(Exception, "MISSING_HISTORY_QUERY"):
            policy.validate(payload)
        policy.accept("session_history", {"action": "search"}, {"ok": True, "hits": []})
        self.assertEqual(policy.validate(payload)["status"], "not_found")

    def test_current_user_message_id_is_visible_and_can_be_referenced(self):
        current = self.service.submit(self.session.id, RunSubmission(
            "current", "更正：标题改为项目复核报告", "conversation",
            self.session.scope, None, None, {}))
        message = self.store.load_run_messages(self.session.id, current.run_id)[0]
        policy = ConversationPolicy(
            self.store, self.session.id, before_seq=message.session_seq)
        initial = policy.initial_messages(message.payload["content"], None)
        self.assertEqual(json.loads(initial[-1]["content"])["message_id"], message.id)
        policy.set_visible_messages([])
        start = message.payload["content"].index("项目复核报告")
        answer = policy.validate(json.dumps({
            "status": "answered",
            "answer": "最新标题为项目复核报告。",
            "references": [{"message_id": message.id, "start": start,
                            "end": start + len("项目复核报告")}],
        }, ensure_ascii=False))
        self.assertEqual(answer["references"][0]["quote"], "项目复核报告")


if __name__ == "__main__":
    unittest.main()
