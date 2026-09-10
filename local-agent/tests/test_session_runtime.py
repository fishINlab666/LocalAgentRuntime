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
from local_agent.runtime import Runtime
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
