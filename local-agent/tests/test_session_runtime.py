import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent.approvals import ApprovalBroker
from local_agent.discovery import DirectoryTools
from local_agent.file_tools import adapt_tools
from local_agent.files import ReadFile
from local_agent.provider import ModelReply
from local_agent.runtime import RunConfig, Runtime
from local_agent.session_store import SessionStore, StoreError
from local_agent.sessions import RunSubmission, SessionScope, SessionService
from local_agent.trace import Trace
from report_provider import ReportProvider
from test_runtime import ScriptedProvider, call_message, final_message


class SessionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "a.md").write_text("代号：orange-731\n", encoding="utf-8")
        self.store = SessionStore.open(self.root / "state")
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)

    def prepare(self, *, scope=None, output_path=None, request_id="request-1"):
        scope = scope or SessionScope("file", "a.md")
        session = self.service.create(self.workspace, "核对", scope)
        prepared = self.service.submit(
            session.id,
            RunSubmission(
                client_request_id=request_id,
                question="读取文件并回答代号，引用原文。",
                task_type="files",
                scope=scope,
                output_path=output_path,
                parent_run_id=None,
                execution_options={"max_steps": 6},
            ),
        )
        return session, prepared

    def seed_long_conversation(self, session, count=20):
        messages = []
        for number in range(count):
            padding = chr(0x4E00 + number % 100) * 1400
            prepared = self.service.submit(session.id, RunSubmission(
                f"history-{number}", f"早期目标-{number}：{padding}",
                "conversation", session.scope, None, None, {}))
            prepared.journal.record_model_request(
                {"messages": []}, {"source_kind": "fixture"})
            prepared.journal.record_model_reply(
                {"role": "assistant", "content": f"已记录-{number}：{padding}"},
                None,
                "answer_valid",
            )
            prepared.journal.finish_run({
                "state": "completed",
                "stop_reason": "ANSWER_VALIDATED",
                "answer": f"已记录-{number}",
            })
            messages.append(self.store.load_run_messages(session.id, prepared.run_id)[0])
        return messages

    def test_runtime_persists_each_request_reply_and_tool_result_in_order(self):
        session, prepared = self.prepare()
        provider = ScriptedProvider([call_message(), final_message()])
        trace = Trace(self.root / "runs", self.workspace, run_id=prepared.run_id)

        result = Runtime(
            provider,
            ReadFile(self.workspace, {"a.md"}),
            trace,
            journal=prepared.journal,
        ).run(prepared.submission.question, "a.md")

        self.assertEqual((result["state"], result["run_id"]), ("completed", prepared.run_id))
        self.assertEqual(provider.requests[1][-1]["tool_call_id"], "c1")
        messages = self.store.load_run_messages(session.id, prepared.run_id)
        self.assertEqual([message.role for message in messages], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(messages[2].payload["tool_call_id"], "c1")
        self.assertTrue(messages[2].payload["result"]["ok"])
        manifests = self.store.connection().execute(
            "SELECT request_seq, payload_json FROM context_manifests WHERE run_id=? ORDER BY request_seq",
            (prepared.run_id,),
        ).fetchall()
        self.assertEqual([row[0] for row in manifests], [1, 2])
        self.assertEqual(json.loads(manifests[1][1])["request"]["messages"][-1]["tool_call_id"], "c1")
        stored = self.store.connection().execute(
            "SELECT state, phase, result_json FROM runs WHERE id=?", (prepared.run_id,)
        ).fetchone()
        self.assertEqual((stored[0], stored[1]), ("completed", "finished"))
        self.assertEqual(json.loads(stored[2])["answer"], result["answer"])

    def test_journal_failure_stops_before_provider_call(self):
        _, prepared = self.prepare()
        provider = ScriptedProvider([final_message()])
        trace = Trace(self.root / "runs", self.workspace, run_id=prepared.run_id)
        with patch.object(
            prepared.journal,
            "record_model_request",
            side_effect=StoreError("JOURNAL_STORE_ERROR"),
        ):
            result = Runtime(
                provider,
                ReadFile(self.workspace, {"a.md"}),
                trace,
                journal=prepared.journal,
            ).run(prepared.submission.question, "a.md")
        self.assertEqual(result["stop_reason"], "SESSION_STORE_ERROR")
        self.assertEqual(provider.requests, [])

    def _run_report_with_fault(self, method):
        (self.workspace / "plan.md").write_text("项目代号：杉木-19\n", encoding="utf-8")
        (self.workspace / "review.txt").write_text("评审人：顾宁\n", encoding="utf-8")
        scope = SessionScope("directory", None)
        session, prepared = self.prepare(scope=scope, output_path="report.md")
        trace = Trace(self.root / "runs", self.workspace, run_id=prepared.run_id)
        broker = ApprovalBroker(prepared.run_id, journal=prepared.journal)
        self.addCleanup(broker.close)
        provider = ReportProvider()

        def allow(event, data):
            if event == "approval.required":
                broker.decide(data["id"], "allow")

        broker.publish = allow
        with patch.object(
            prepared.journal,
            method,
            side_effect=StoreError("JOURNAL_STORE_ERROR"),
        ):
            result = Runtime(
                provider,
                adapt_tools(DirectoryTools(self.workspace), "report.md"),
                trace,
                approvals=broker,
                journal=prepared.journal,
            ).run(prepared.submission.question, None)
        return result, self.workspace / "report.md"

    def test_publication_intent_store_failure_prevents_file_creation(self):
        result, output = self._run_report_with_fault("record_publication_intent")
        self.assertEqual(result["stop_reason"], "SESSION_STORE_ERROR")
        self.assertFalse(output.exists())

    def test_receipt_store_failure_reports_unknown_and_keeps_actual_receipt(self):
        result, output = self._run_report_with_fault("record_publication_receipt")
        self.assertEqual(result["stop_reason"], "WRITE_OUTCOME_UNKNOWN")
        self.assertNotEqual(result["state"], "completed")
        self.assertTrue(output.is_file())
        receipt = result["unpersisted_artifacts"][0]
        raw = output.read_bytes()
        self.assertEqual(receipt["bytes"], len(raw))
        self.assertEqual(receipt["sha256"], hashlib.sha256(raw).hexdigest())

    def test_session_service_runs_file_then_conversation_and_survives_reopen(self):
        session, first = self.prepare()
        first_provider = ScriptedProvider([call_message(), final_message()])
        first_result = self.service.execute(
            first,
            first_provider,
            Trace(self.root / "runs", self.workspace, run_id=first.run_id),
        )
        self.assertEqual(first_result["state"], "completed")
        first_user = self.store.load_run_messages(session.id, first.run_id)[0]

        second = self.service.submit(session.id, RunSubmission(
            "request-2", "我上次要求做什么？", "conversation", session.scope,
            None, None, {}))
        source = first_user.payload["content"]
        second_answer = {"role": "assistant", "content": json.dumps({
            "status": "answered", "answer": "你上次要求读取文件并回答代号。",
            "references": [{"message_id": first_user.id, "start": 0, "end": len(source)}],
        }, ensure_ascii=False)}
        second_provider = ScriptedProvider([second_answer])
        second_result = self.service.execute(
            second,
            second_provider,
            Trace(self.root / "runs", self.workspace, run_id=second.run_id),
        )
        self.assertEqual(second_result["state"], "completed")
        self.assertIn(source, json.dumps(second_provider.requests[0], ensure_ascii=False))

        state_dir = self.store.state_dir
        self.store.close()
        self.store = SessionStore.open(state_dir)
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)
        third = self.service.submit(session.id, RunSubmission(
            "request-3", "继续沿用刚才的要求", "conversation", session.scope,
            None, None, {}))
        second_user = self.store.load_run_messages(session.id, second.run_id)[0]
        second_source = second_user.payload["content"]
        third_provider = ScriptedProvider([{"role": "assistant", "content": json.dumps({
            "status": "answered", "answer": "继续沿用上一轮要求。",
            "references": [{"message_id": second_user.id, "start": 0,
                            "end": len(second_source)}],
        }, ensure_ascii=False)}])
        third_result = self.service.execute(
            third,
            third_provider,
            Trace(self.root / "runs", self.workspace, run_id=third.run_id),
        )
        self.assertEqual(third_result["state"], "completed")
        self.assertIn(second_source, json.dumps(third_provider.requests[0], ensure_ascii=False))

    def test_each_file_run_re_reads_changed_source(self):
        session, first = self.prepare()
        self.assertEqual(self.service.execute(
            first, ScriptedProvider([call_message(), final_message()]),
            Trace(self.root / "runs", self.workspace, run_id=first.run_id),
        )["state"], "completed")
        (self.workspace / "a.md").write_text("代号：purple-999\n", encoding="utf-8")
        second = self.service.submit(session.id, RunSubmission(
            "request-2", "再次核对代号", "files", session.scope, None, None, {}))
        provider = ScriptedProvider([
            call_message(), final_message(quote="代号：purple-999")
        ])
        result = self.service.execute(
            second, provider,
            Trace(self.root / "runs", self.workspace, run_id=second.run_id),
        )
        self.assertEqual(result["answer"]["answer"], "代号：purple-999")
        current_result = json.loads(provider.requests[-1][-1]["content"])
        self.assertEqual(current_result["data"]["content"]["1"], "代号：purple-999")

    def test_idempotent_submit_never_executes_provider_twice(self):
        session, prepared = self.prepare()
        result = self.service.execute(
            prepared, ScriptedProvider([call_message(), final_message()]),
            Trace(self.root / "runs", self.workspace, run_id=prepared.run_id),
        )
        duplicate = self.service.submit(session.id, prepared.submission)
        self.assertFalse(duplicate.created)
        provider = ScriptedProvider([])
        replay = self.service.execute(
            duplicate, provider,
            Trace(self.root / "runs-duplicate", self.workspace, run_id=duplicate.run_id),
        )
        self.assertEqual(replay["answer"], result["answer"])
        self.assertEqual(provider.requests, [])

    def test_continue_creates_a_new_run_without_inheriting_output(self):
        session, interrupted = self.prepare()
        interrupted.journal.record_model_request({"messages": []}, {"kind": "fixture"})
        self.store.recover_interrupted("new-process")
        continued = self.service.continue_interrupted(
            session.id, interrupted.run_id, "request-continued")
        self.assertNotEqual(continued.run_id, interrupted.run_id)
        self.assertEqual(continued.submission.parent_run_id, interrupted.run_id)
        self.assertIsNone(continued.submission.output_path)
        self.assertEqual(continued.submission.execution_options, {})

    def test_missing_workspace_keeps_conversation_available_and_blocks_file_run(self):
        session, first = self.prepare()
        self.assertEqual(self.service.execute(
            first, ScriptedProvider([call_message(), final_message()]),
            Trace(self.root / "runs", self.workspace, run_id=first.run_id),
        )["state"], "completed")
        first_user = self.store.load_run_messages(session.id, first.run_id)[0]
        pending_file = self.service.submit(session.id, RunSubmission(
            "request-file", "重新读取", "files", session.scope, None, None, {}))
        conversation = self.service.submit(session.id, RunSubmission(
            "request-chat", "回顾上一轮", "conversation", session.scope, None, None, {}))
        source = first_user.payload["content"]
        moved = self.root / "workspace-moved"
        self.workspace.rename(moved)

        chat_provider = ScriptedProvider([{"role": "assistant", "content": json.dumps({
            "status": "answered", "answer": "上一轮要求读取文件并回答代号。",
            "references": [{"message_id": first_user.id, "start": 0,
                            "end": len(source)}],
        }, ensure_ascii=False)}])
        chat_result = self.service.execute(
            conversation, chat_provider,
            Trace(self.root / "runs", self.workspace, run_id=conversation.run_id),
        )
        self.assertEqual(chat_result["state"], "completed")
        file_provider = ScriptedProvider([])
        file_result = self.service.execute(
            pending_file, file_provider,
            Trace(self.root / "runs", self.workspace, run_id=pending_file.run_id),
        )
        self.assertEqual(file_result["stop_reason"], "WORKSPACE_UNAVAILABLE")
        self.assertEqual(file_provider.requests, [])

    def test_summary_and_task_calls_share_one_model_budget_and_journal(self):
        session = self.service.create(
            self.workspace, "长会话", SessionScope("directory", None))
        history = self.seed_long_conversation(session)
        prepared = self.service.submit(session.id, RunSubmission(
            "long-current", "继续上一轮", "conversation", session.scope,
            None, None, {"max_steps": 2}))
        first = history[0]
        first_text = first.payload["content"][:12]
        fact = {"text": first_text, "message_id": first.id,
                "start": 0, "end": len(first_text)}
        summary = {key: ([] if key not in {"goals", "pending", "anchors"} else [fact])
                   for key in ("goals", "constraints", "decisions", "completed",
                               "pending", "anchors")}
        recent = history[-1]
        recent_text = recent.payload["content"]
        final = {"role": "assistant", "content": json.dumps({
            "status": "answered",
            "answer": "已继续上一轮。",
            "references": [{"message_id": recent.id, "start": 0,
                            "end": len(recent_text)}],
        }, ensure_ascii=False)}

        class PurposeProvider:
            metadata = {"provider": "fixture", "model": "summary-budget", "simulated": True}

            def __init__(inner_self):
                inner_self.requests = []

            def complete(inner_self, messages, tools, timeout):
                inner_self.requests.append({
                    "messages": messages, "tools": tools, "timeout": timeout})
                reply = ({"role": "assistant", "content": json.dumps(summary, ensure_ascii=False)}
                         if len(inner_self.requests) == 1 else final)
                return ModelReply(reply, {"total_tokens": 10})

        provider = PurposeProvider()
        result = self.service.execute(
            prepared,
            provider,
            Trace(self.root / "runs", self.workspace, run_id=prepared.run_id),
            config=RunConfig(max_steps=2),
        )

        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[0]["tools"], [])
        self.assertTrue(provider.requests[1]["tools"])
        self.assertIn("session_summary", json.dumps(
            provider.requests[1]["messages"], ensure_ascii=False))
        manifests = self.store.connection().execute(
            "SELECT payload_json FROM context_manifests WHERE run_id=? ORDER BY request_seq",
            (prepared.run_id,),
        ).fetchall()
        self.assertEqual([
            json.loads(row[0])["manifest"]["source_kind"] for row in manifests
        ], ["summary", "task"])
        summary_messages = self.store.connection().execute(
            "SELECT validation_state FROM messages WHERE run_id=? AND source_kind='summary'",
            (prepared.run_id,),
        ).fetchall()
        self.assertEqual(len(summary_messages), 1)

    def test_failed_summary_is_attempted_once_then_history_tool_can_recover_detail(self):
        session = self.service.create(
            self.workspace, "降级会话", SessionScope("directory", None))
        history = self.seed_long_conversation(session)
        prepared = self.service.submit(session.id, RunSubmission(
            "long-fallback", "找回最早目标", "conversation", session.scope,
            None, None, {}))
        first = history[0]
        prefix = "早期目标-0"
        search_call = {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": "history-call", "type": "function", "function": {
                    "name": "session_history",
                    "arguments": json.dumps({
                        "action": "search", "query": prefix, "intent": "回查早期目标",
                    }, ensure_ascii=False),
                },
            }],
        }
        final = {"role": "assistant", "content": json.dumps({
            "status": "answered", "answer": prefix,
            "references": [{"message_id": first.id, "start": 0,
                            "end": len(prefix)}],
        }, ensure_ascii=False)}
        provider = ScriptedProvider([
            {"role": "assistant", "content": "not-json"},
            search_call,
            final,
        ])

        result = self.service.execute(
            prepared,
            provider,
            Trace(self.root / "runs", self.workspace, run_id=prepared.run_id),
        )

        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["model_calls"], 3)
        manifests = self.store.connection().execute(
            "SELECT payload_json FROM context_manifests WHERE run_id=? ORDER BY request_seq",
            (prepared.run_id,),
        ).fetchall()
        self.assertEqual([
            json.loads(row[0])["manifest"]["source_kind"] for row in manifests
        ], ["summary", "task", "task"])
        self.assertIsNone(self.store.load_active_summary(session.id))


class ApprovalJournalTests(unittest.TestCase):
    def test_required_and_decision_are_durable_before_they_become_visible(self):
        events = []

        class Journal:
            def record_approval_required(self, call_id, approval):
                events.append(("stored-required", call_id, approval))

            def record_approval_decision(self, approval_id, decision):
                events.append(("stored-decision", approval_id, decision))

        broker = None

        def publish(event, data):
            events.append((event, data["id"]))
            if event == "approval.required":
                broker.decide(data["id"], "allow")

        broker = ApprovalBroker("run-1", publish=publish, journal=Journal(), process_generation="process-1")
        self.addCleanup(broker.close)
        from local_agent.approvals import RunControl
        import threading

        decision = broker.request(
            "call-1",
            "write_file",
            {"path": "report.md", "content": "正文", "intent": "生成报告"},
            {"action_summary": "新建 report.md", "path": "report.md", "content": "正文"},
            RunControl(threading.Event()),
        )

        self.assertEqual(decision, "allow")
        names = [item[0] for item in events]
        self.assertLess(names.index("stored-required"), names.index("approval.required"))
        self.assertLess(names.index("stored-decision"), names.index("approval.resolved"))
        required = next(item[2] for item in events if item[0] == "stored-required")
        expected = json.dumps(
            {"content": "正文", "intent": "生成报告", "path": "report.md"},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(required["argument_hash"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(required["process_generation"], "process-1")


if __name__ == "__main__":
    unittest.main()
