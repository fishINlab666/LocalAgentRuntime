import json
from pathlib import Path
import tempfile
import unittest

from local_agent.context import ContextBuilder
from local_agent.session_history import SessionHistoryTool
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

    def seed_long_history(self, count=50, *, prefix="早期目标"):
        run_ids = []
        for number in range(count):
            padding = chr(0x4E00 + number % 100) * 1400
            run_ids.append(self.complete(
                f"{prefix}-{number}：{padding}",
                f"已记录-{number}：{padding}",
            ))
        return run_ids

    @staticmethod
    def summary_from_request(request, *, overrides=None):
        source = json.loads(request["messages"][-1]["content"])
        records = [
            record
            for run in source["source_runs"]
            for record in run["records"]
            if record.get("source_kind") == "user"
        ]
        record = records[0]
        text = record["text"][:12]
        fact = {
            "text": text,
            "message_id": record["message_id"],
            "start": 0,
            "end": len(text),
        }
        payload = {
            "goals": [fact],
            "constraints": [],
            "decisions": [],
            "completed": [],
            "pending": [fact],
            "anchors": [fact],
        }
        if overrides:
            payload.update(overrides)
        return payload

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

    def test_fifty_runs_create_a_bounded_traceable_summary_and_keep_raw_history(self):
        run_ids = self.seed_long_history()
        prepared = self.prepare_current()
        summary_requests = []

        def summarize(request, manifest):
            summary_requests.append((request, manifest))
            payload = self.summary_from_request(request)
            return {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
                "model": {"provider": "fixture", "model": "summary-test"},
            }

        built = ContextBuilder(
            self.store, self.session.id, prepared.run_id
        ).build(
            {
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "继续"},
                ],
                "tools": [],
            },
            65536,
            summarize=summarize,
        )

        self.assertEqual(len(summary_requests), 1)
        summary_request, summary_manifest = summary_requests[0]
        self.assertEqual(summary_request["tools"], [])
        self.assertLessEqual(
            len(json.dumps(summary_request, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")),
            65536,
        )
        self.assertEqual(summary_manifest["source_kind"], "summary")
        self.assertLessEqual(built.manifest["input_bytes"], 65536)
        self.assertIsNotNone(built.manifest["summary_id"])
        self.assertEqual(built.manifest["selected_run_ids"], run_ids[-4:])
        active = self.store.load_active_summary(self.session.id)
        self.assertEqual(active.id, built.manifest["summary_id"])
        self.assertGreater(active.covered_through_seq, 0)
        self.assertLessEqual(
            len(json.dumps(active.payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")),
            6144,
        )
        rendered = json.dumps(built.request, ensure_ascii=False)
        self.assertIn("session_summary", rendered)
        self.assertIn("history_omission", rendered)

        first_user = self.store.load_run_messages(self.session.id, run_ids[0])[0]
        result = SessionHistoryTool(
            self.store,
            self.session.id,
            before_seq=built.manifest["cutoff_seq"],
        ).execute({"action": "read", "message_id": first_user.id})
        self.assertTrue(result["ok"])
        self.assertIn("早期目标-0", result["text"])

    def test_invalid_summary_variants_leave_old_summary_and_raw_messages_unchanged(self):
        run_ids = self.seed_long_history(12)
        first_message = self.store.load_run_messages(self.session.id, run_ids[0])[0]
        fact_text = first_message.payload["content"][:12]
        old_payload = {
            key: ([] if key not in {"goals", "anchors"} else [{
                "text": fact_text,
                "message_id": first_message.id,
                "start": 0,
                "end": len(fact_text),
            }])
            for key in ("goals", "constraints", "decisions", "completed", "pending", "anchors")
        }
        covered = max(
            message.session_seq
            for message in self.store.load_run_messages(self.session.id, run_ids[0])
        )
        old = self.store.save_summary(
            self.session.id,
            covered,
            old_payload,
            {"provider": "fixture", "model": "old"},
            "session-summary-v1",
        )
        prepared = self.prepare_current()
        before_count = self.store.connection().execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (self.session.id,)
        ).fetchone()[0]

        variants = {
            "malformed": lambda request: "not-json",
            "oversized": lambda request: json.dumps({
                **self.summary_from_request(request),
                "pending": [{
                    **self.summary_from_request(request)["pending"][0],
                    "text": "x" * 7000,
                    "end": 7000,
                }],
            }),
            "wrong-message": lambda request: json.dumps({
                **self.summary_from_request(request),
                "goals": [{
                    **self.summary_from_request(request)["goals"][0],
                    "message_id": "other-session-message",
                }],
            }, ensure_ascii=False),
            "mismatched-excerpt": lambda request: json.dumps({
                **self.summary_from_request(request),
                "goals": [{
                    **self.summary_from_request(request)["goals"][0],
                    "text": "不匹配原文",
                    "end": 6,
                }],
            }, ensure_ascii=False),
        }
        current_request = {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "继续"},
            ],
            "tools": [],
        }
        for label, content in variants.items():
            with self.subTest(label=label):
                calls = []

                def summarize(request, manifest):
                    calls.append(manifest)
                    return {
                        "message": {"role": "assistant", "content": content(request)},
                        "model": {"provider": "fixture", "model": label},
                    }

                built = ContextBuilder(
                    self.store, self.session.id, prepared.run_id
                ).build(current_request, 65536, summarize=summarize)
                self.assertEqual(len(calls), 1)
                self.assertLessEqual(built.manifest["input_bytes"], 65536)
                self.assertEqual(self.store.load_active_summary(self.session.id).id, old.id)

        def raises(_request, _manifest):
            raise RuntimeError("summary unavailable")

        built = ContextBuilder(self.store, self.session.id, prepared.run_id).build(
            current_request, 65536, summarize=raises)
        self.assertEqual(self.store.load_active_summary(self.session.id).id, old.id)
        self.assertLessEqual(built.manifest["input_bytes"], 65536)
        self.assertEqual(self.store.connection().execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?", (self.session.id,)
        ).fetchone()[0], before_count)

    def test_current_native_tool_chain_is_never_split_when_history_is_removed(self):
        self.seed_long_history(10)
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
            "role": "tool",
            "tool_call_id": "current-call",
            "content": json.dumps({"ok": True, "data": {"text": "z" * 12000}}),
        }
        current = {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "继续"},
                current_call,
                current_tool,
            ],
            "tools": [],
        }
        current_bytes = len(json.dumps(
            current, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"))
        built = ContextBuilder(self.store, self.session.id, prepared.run_id).build(
            current, current_bytes + 600)
        self.assertEqual(built.request["messages"][-2:], [current_call, current_tool])
        self.assertLessEqual(built.manifest["input_bytes"], current_bytes + 600)


if __name__ == "__main__":
    unittest.main()
