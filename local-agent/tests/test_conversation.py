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
        service = SessionService(self.store)
        self.session = service.create(workspace, "约定", SessionScope("directory", None))
        old = service.submit(self.session.id, RunSubmission(
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


if __name__ == "__main__":
    unittest.main()
