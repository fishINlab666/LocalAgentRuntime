import json
from pathlib import Path
import tempfile
import unittest

from local_agent.session_store import SessionStore, StoreError
from local_agent.sessions import RunSubmission, SessionScope, SessionService


def tool_call(call_id="call-1", name="read_file"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": '{"path":"missing-73.md","intent":"核对旧参数"}',
        },
    }


class RunJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "note.md").write_text("代号：青禾-47\n", encoding="utf-8")
        self.store = SessionStore.open(self.root / "state")
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)
        self.session = self.service.create(
            self.workspace, "核对", SessionScope("file", "note.md")
        )

    def submit(self, request_id="request-1"):
        return self.service.submit(
            self.session.id,
            RunSubmission(
                client_request_id=request_id,
                question="核对代号",
                task_type="files",
                scope=self.session.scope,
                output_path=None,
                parent_run_id=None,
                execution_options={"max_steps": 6},
            ),
        )

    def test_records_complete_ordered_tool_chain_and_final_result(self):
        prepared = self.submit()
        journal = prepared.journal

        journal.record_model_request(
            {"messages": [{"role": "user", "content": "核对代号"}]},
            {"prompt_version": "v1", "input_kind": "current"},
        )
        assistant_id = journal.record_model_reply(
            {"role": "assistant", "content": None, "tool_calls": [tool_call()]},
            usage={"input_tokens": 12},
            validation="tool_calls_valid",
        )
        tool_id = journal.record_tool_result(
            "call-1",
            {
                "ok": False,
                "error": {
                    "code": "PATH_NOT_DISCOVERED",
                    "message": "not discovered",
                    "owner": "model",
                },
            },
        )
        journal.record_model_request(
            {"messages": [{"role": "tool", "tool_call_id": "call-1"}]},
            {"prompt_version": "v1", "input_kind": "tool_result"},
        )
        final_id = journal.record_model_reply(
            {"role": "assistant", "content": "无法从该路径读取。"},
            usage=None,
            validation="answer_valid",
        )
        journal.finish_run(
            {"state": "completed", "answer": "无法从该路径读取。"}
        )

        history = self.store.load_run_messages(self.session.id, prepared.run_id)
        self.assertEqual(
            [item.role for item in history],
            ["user", "assistant", "tool", "assistant"],
        )
        self.assertEqual([item.session_seq for item in history], [1, 2, 3, 4])
        self.assertEqual([item.run_seq for item in history], [1, 2, 3, 4])
        self.assertEqual(history[1].id, assistant_id)
        self.assertEqual(history[2].id, tool_id)
        self.assertEqual(history[2].payload["tool_call_id"], "call-1")
        self.assertEqual(history[3].id, final_id)
        call = self.store.connection().execute(
            """SELECT assistant_message_id, result_message_id, name, stage
               FROM tool_calls WHERE run_id=? AND call_id='call-1'""",
            (prepared.run_id,),
        ).fetchone()
        self.assertEqual(call, (assistant_id, tool_id, "read_file", "failed"))
        self.assertIn("arguments", history[1].payload["tool_calls"][0]["function"])
        self.assertEqual(
            self.store.connection().execute(
                "SELECT COUNT(*) FROM context_manifests WHERE run_id=?",
                (prepared.run_id,),
            ).fetchone()[0],
            2,
        )
        stored = self.store.connection().execute(
            "SELECT state, phase, result_json, finished_at FROM runs WHERE id=?",
            (prepared.run_id,),
        ).fetchone()
        self.assertEqual((stored[0], stored[1]), ("completed", "finished"))
        self.assertEqual(json.loads(stored[2])["answer"], "无法从该路径读取。")
        self.assertIsNotNone(stored[3])

    def test_invalid_reply_is_preserved_without_indexing_calls(self):
        prepared = self.submit()
        prepared.journal.record_model_request({"messages": []}, {"kind": "test"})
        raw = {
            "role": "assistant",
            "content": None,
            "reasoning_content": "internal chain must not persist",
            "tool_calls": [{"id": "broken", "function": {"name": "read_file"}}],
        }

        message_id = prepared.journal.record_model_reply(
            raw, usage=None, validation="tool_calls_invalid"
        )

        message = self.store.load_run_messages(self.session.id, prepared.run_id)[-1]
        expected = {key: value for key, value in raw.items() if key != "reasoning_content"}
        self.assertEqual((message.id, message.payload), (message_id, expected))
        self.assertEqual(
            self.store.connection().execute(
                "SELECT COUNT(*) FROM tool_calls WHERE run_id=?", (prepared.run_id,)
            ).fetchone()[0],
            0,
        )

    def test_rejects_missing_cross_run_and_duplicate_terminal_results(self):
        first = self.submit("request-a")
        second = self.submit("request-b")
        first.journal.record_model_request({"messages": []}, {"kind": "test"})
        first.journal.record_model_reply(
            {"role": "assistant", "content": None, "tool_calls": [tool_call()]},
            usage=None,
            validation="tool_calls_valid",
        )

        for action in (
            lambda: second.journal.record_tool_result("call-1", {"ok": True}),
            lambda: first.journal.record_tool_result("missing", {"ok": True}),
        ):
            with self.subTest(action=action):
                with self.assertRaisesRegex(StoreError, "JOURNAL_ORDER_ERROR"):
                    action()

        first.journal.record_tool_started("call-1")
        first.journal.record_tool_result("call-1", {"ok": True, "data": {}})
        for action in (
            lambda: first.journal.record_tool_result("call-1", {"ok": True}),
            lambda: first.journal.record_tool_started("call-1"),
        ):
            with self.subTest(action=action):
                with self.assertRaisesRegex(StoreError, "JOURNAL_ORDER_ERROR"):
                    action()

    def test_denied_approval_can_record_its_skipped_tool_result(self):
        prepared = self.submit("request-denied")
        prepared.journal.record_model_request({"messages": []}, {"kind": "test"})
        prepared.journal.record_model_reply(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call(name="write_file")],
            },
            usage=None,
            validation="tool_calls_valid",
        )
        prepared.journal.record_approval_required(
            "call-1",
            {
                "id": "approval-denied",
                "preview": {"path": "report.md"},
                "argument_hash": "hash",
                "process_generation": "process-1",
            },
        )
        prepared.journal.record_approval_decision("approval-denied", "denied")

        result_id = prepared.journal.record_tool_result(
            "call-1",
            {"ok": False, "error": {"code": "USER_REJECTED"}},
        )

        self.assertEqual(
            self.store.connection().execute(
                """SELECT stage, result_message_id FROM tool_calls
                   WHERE run_id=? AND call_id='call-1'""",
                (prepared.run_id,),
            ).fetchone(),
            ("skipped", result_id),
        )


if __name__ == "__main__":
    unittest.main()
