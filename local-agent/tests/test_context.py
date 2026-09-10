import json
from pathlib import Path
import tempfile
import unittest

from local_agent.context import ContextBuilder
from local_agent.session_store import SessionStore
from local_agent.sessions import RunSubmission, SessionScope, SessionService


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.store = SessionStore.open(root / "state")
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)
        self.session = self.service.create(
            self.workspace, "连续对话", SessionScope("directory", None)
        )
        self.counter = 0

    def complete(self, question, answer, *, tool=False):
        self.counter += 1
        prepared = self.service.submit(
            self.session.id,
            RunSubmission(
                client_request_id=f"request-{self.counter}",
                question=question,
                task_type="conversation",
                scope=self.session.scope,
                output_path=None,
                parent_run_id=None,
                execution_options={},
            ),
        )
        prepared.journal.record_model_request({"messages": []}, {"kind": "fixture"})
        if tool:
            call = {
                "id": f"call-{self.counter}",
                "type": "function",
                "function": {
                    "name": "session_history",
                    "arguments": json.dumps(
                        {"action": "search", "query": "旧约定", "intent": "回查"},
                        ensure_ascii=False,
                    ),
                },
            }
            prepared.journal.record_model_reply(
                {"role": "assistant", "content": None, "tool_calls": [call]},
                None,
                "tool_calls_valid",
            )
            prepared.journal.record_tool_result(
                call["id"], {"ok": True, "data": {"hits": []}}
            )
            prepared.journal.record_model_request({"messages": []}, {"kind": "fixture-2"})
        prepared.journal.record_model_reply(
            {"role": "assistant", "content": answer}, None, "answer_valid"
        )
        prepared.journal.finish_run(
            {"state": "completed", "stop_reason": "ANSWER_VALIDATED", "answer": answer}
        )
        return prepared.run_id

    def prepare_current(self, question="按上次要求继续"):
        self.counter += 1
        return self.service.submit(
            self.session.id,
            RunSubmission(
                client_request_id=f"request-{self.counter}",
                question=question,
                task_type="conversation",
                scope=self.session.scope,
                output_path=None,
                parent_run_id=None,
                execution_options={},
            ),
        )

    def test_recent_context_contains_original_correction_with_ids(self):
        self.complete("报告用表格", "已记录")
        self.complete("不要表格，改成三段文字", "已更正")
        prepared = self.prepare_current()

        built = ContextBuilder(
            self.store, self.session.id, prepared.run_id
        ).build(
            {"messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "按上次要求继续"},
            ], "tools": []},
            65536,
        )

        projection = json.dumps(built.request["messages"], ensure_ascii=False)
        self.assertIn("报告用表格", projection)
        self.assertIn("不要表格，改成三段文字", projection)
        selected = built.manifest["selected_message_ids"]
        self.assertEqual(len(selected), 4)
        self.assertLessEqual(built.manifest["input_bytes"], 65536)

    def test_history_tool_chain_is_data_and_current_chain_stays_native(self):
        self.complete("回查旧约定", "没有找到", tool=True)
        prepared = self.prepare_current()
        current_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "current-call",
                "type": "function",
                "function": {"name": "session_history", "arguments": "{}"},
            }],
        }
        current_tool = {
            "role": "tool", "tool_call_id": "current-call", "content": "{}"
        }
        built = ContextBuilder(self.store, self.session.id, prepared.run_id).build(
            {"messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "继续"},
                current_call,
                current_tool,
            ], "tools": []},
            65536,
        )
        historical = []
        for message in built.request["messages"]:
            try:
                payload = json.loads(message.get("content", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("historical") is True:
                historical.append(message)
        self.assertTrue(historical)
        self.assertFalse(any("tool_calls" in message for message in historical))
        self.assertFalse(any(message["role"] == "tool" for message in historical))
        self.assertEqual(built.request["messages"][-2:], [current_call, current_tool])

    def test_only_four_recent_complete_runs_are_selected(self):
        for number in range(5):
            self.complete(f"约定-{number}", f"确认-{number}")
        prepared = self.prepare_current()
        built = ContextBuilder(self.store, self.session.id, prepared.run_id).build(
            {"messages": [{"role": "system", "content": "s"},
                          {"role": "user", "content": "继续"}], "tools": []},
            65536,
        )
        payload = json.dumps(built.request, ensure_ascii=False)
        self.assertNotIn("约定-0", payload)
        for number in range(1, 5):
            self.assertIn(f"约定-{number}", payload)
        self.assertEqual(built.manifest["selected_run_count"], 4)

    def test_current_chain_over_limit_fails_without_truncation(self):
        prepared = self.prepare_current()
        with self.assertRaisesRegex(ValueError, "CONTEXT_LIMIT"):
            ContextBuilder(self.store, self.session.id, prepared.run_id).build(
                {"messages": [{"role": "system", "content": "x" * 1000},
                              {"role": "user", "content": "继续"}], "tools": []},
                200,
            )


if __name__ == "__main__":
    unittest.main()
