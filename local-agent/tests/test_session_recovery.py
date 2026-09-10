from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from local_agent.approvals import ApprovalBroker, ApprovalError
from local_agent.session_store import SessionStore, StoreError
from local_agent.sessions import RunSubmission, SessionScope, SessionService
from local_agent.web import create_server


FIXTURE = Path(__file__).with_name("session_process_fixture.py")


def write_call(call_id, path="report.md", content="planned"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": json.dumps(
                {"path": path, "content": content},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    }


class SessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        self.store = SessionStore.open(self.state)
        self.service = SessionService(self.store)
        self.session = self.service.create(
            self.workspace, "恢复", SessionScope("directory", None)
        )
        self.other_session = self.service.create(
            self.workspace, "同工作区", SessionScope("directory", None)
        )
        self.process_index = 0

    def tearDown(self):
        self.store.close()

    @staticmethod
    def _stop_process(process):
        if process.poll() is None:
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def start_process_fixture(self, pause_point):
        self.process_index += 1
        case_root = self.root / "process-cases" / (
            f"{self.process_index:02d}-{pause_point}"
        )
        workspace = case_root / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "note.md").write_text(
            "项目代号：process-47\n", encoding="utf-8"
        )
        state = case_root / "state"
        activity = case_root / "activity.jsonl"
        process = subprocess.Popen(
            [
                sys.executable,
                str(FIXTURE),
                "--state-dir",
                str(state),
                "--workspace",
                str(workspace),
                "--pause-point",
                pause_point,
                "--activity-log",
                str(activity),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop_process, process)
        ready, _, _ = select.select([process.stdout], [], [], 10)
        if not ready:
            status = process.poll()
            detail = process.stderr.read() if status is not None else "timeout"
            self.fail(f"fixture did not become ready: {status}: {detail}")
        line = process.stdout.readline()
        if not line:
            detail = process.stderr.read()
            self.fail(f"fixture exited before ready: {process.poll()}: {detail}")
        payload = json.loads(line)
        self.assertEqual(payload["event"], "ready")
        self.assertEqual(payload["pause_point"], pause_point)
        return process, payload, state, workspace, activity

    def kill_process_fixture(self, pause_point):
        process, payload, state, workspace, activity = self.start_process_fixture(
            pause_point
        )
        os.kill(process.pid, signal.SIGKILL)
        self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
        return payload, state, workspace, activity

    @staticmethod
    def read_activity(path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def submit(self, label):
        return self.service.submit(
            self.session.id,
            RunSubmission(
                client_request_id=f"request-{label}",
                question=f"任务 {label}",
                task_type="files",
                scope=self.session.scope,
                output_path="report.md",
                parent_run_id=None,
                execution_options={},
            ),
        )

    @staticmethod
    def begin_write(prepared, label):
        call_id = f"call-{label}"
        prepared.journal.record_model_request({"messages": []}, {"kind": label})
        prepared.journal.record_model_reply(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [write_call(call_id)],
            },
            usage=None,
            validation="tool_calls_valid",
        )
        return call_id

    @staticmethod
    def allow(prepared, call_id, label):
        approval_id = f"approval-{label}"
        prepared.journal.record_approval_required(
            call_id,
            {
                "id": approval_id,
                "preview": {"path": "report.md", "action_summary": "创建报告"},
                "argument_hash": f"hash-{label}",
                "process_generation": "old-process",
            },
        )
        prepared.journal.record_approval_decision(approval_id, "allowed")
        return approval_id

    def test_recovery_classifies_inflight_runs_and_preserves_receipt(self):
        model = self.submit("model")
        model.journal.record_model_request({"messages": []}, {"kind": "model"})

        tool = self.submit("tool")
        tool_call_id = self.begin_write(tool, "tool")
        tool.journal.record_tool_started(tool_call_id)

        approval = self.submit("approval")
        approval_call_id = self.begin_write(approval, "approval")
        self.allow(approval, approval_call_id, "approval")

        unknown = self.submit("unknown")
        unknown_call_id = self.begin_write(unknown, "unknown")
        unknown_approval = self.allow(unknown, unknown_call_id, "unknown")
        content_hash = hashlib.sha256(b"planned").hexdigest()
        unknown.journal.record_publication_intent(
            unknown_call_id,
            {
                "path": "report.md",
                "bytes": 7,
                "sha256": content_hash,
                "approval_id": unknown_approval,
            },
        )

        confirmed = self.submit("confirmed")
        confirmed_call_id = self.begin_write(confirmed, "confirmed")
        confirmed_approval = self.allow(confirmed, confirmed_call_id, "confirmed")
        confirmed.journal.record_publication_intent(
            confirmed_call_id,
            {
                "path": "confirmed.md",
                "bytes": 7,
                "sha256": content_hash,
                "approval_id": confirmed_approval,
            },
        )
        (self.workspace / "confirmed.md").write_text("planned", encoding="utf-8")
        confirmed.journal.record_publication_receipt(
            confirmed_call_id,
            {
                "path": "confirmed.md",
                "bytes": 7,
                "sha256": content_hash,
                "operation": "created",
            },
        )

        run_ids = {
            "model": model.run_id,
            "tool": tool.run_id,
            "approval": approval.run_id,
            "unknown": unknown.run_id,
            "confirmed": confirmed.run_id,
        }
        self.store.close()
        self.store = SessionStore.open(self.state)

        recovered = self.store.recover_interrupted("new-process")

        self.assertEqual(recovered, 5)
        rows = {
            row[0]: row[1:]
            for row in self.store.connection().execute(
                "SELECT id, state, phase, stop_reason, finished_at FROM runs"
            )
        }
        for label in ("model", "tool", "approval"):
            state, _phase, reason, finished_at = rows[run_ids[label]]
            self.assertEqual((state, reason), ("interrupted", "PROCESS_INTERRUPTED"))
            self.assertIsNotNone(finished_at)
        self.assertEqual(
            (rows[unknown.run_id][0], rows[unknown.run_id][2]),
            ("interrupted", "WRITE_OUTCOME_UNKNOWN"),
        )
        self.assertEqual(rows[confirmed.run_id][0], "interrupted")
        self.assertEqual(
            self.store.connection().execute(
                """SELECT stage, publication_state, recovery_state
                   FROM tool_calls WHERE run_id=? AND call_id=?""",
                (unknown.run_id, unknown_call_id),
            ).fetchone(),
            ("unknown", "unknown", "WRITE_OUTCOME_UNKNOWN"),
        )
        self.assertEqual(
            self.store.connection().execute(
                """SELECT publication_state, recovery_state
                   FROM tool_calls WHERE run_id=? AND call_id=?""",
                (confirmed.run_id, confirmed_call_id),
            ).fetchone(),
            ("confirmed", "confirmed"),
        )
        artifact = self.store.connection().execute(
            "SELECT path, bytes, sha256 FROM artifacts WHERE run_id=?",
            (confirmed.run_id,),
        ).fetchone()
        self.assertEqual(artifact, ("confirmed.md", 7, content_hash))
        self.assertEqual(
            self.store.connection().execute(
                "SELECT decision FROM approvals WHERE id=?", (unknown_approval,)
            ).fetchone()[0],
            "expired",
        )
        self.assertEqual(self.store.recover_interrupted("new-process"), 0)

    def test_unknown_target_is_locked_across_sessions_and_inspection_is_read_only(self):
        prepared = self.submit("unknown")
        call_id = self.begin_write(prepared, "unknown")
        approval_id = self.allow(prepared, call_id, "unknown")
        content_hash = hashlib.sha256(b"planned").hexdigest()
        prepared.journal.record_publication_intent(
            call_id,
            {
                "path": "report.md",
                "bytes": 7,
                "sha256": content_hash,
                "approval_id": approval_id,
            },
        )
        self.store.recover_interrupted("new-process")

        self.assertEqual(
            self.store.inspect_unknown_publication(
                self.other_session.id, "report.md"
            ),
            "missing",
        )
        (self.workspace / "report.md").write_text("planned", encoding="utf-8")
        self.assertEqual(
            self.store.inspect_unknown_publication(
                self.other_session.id, "report.md"
            ),
            "present_same_hash",
        )
        (self.workspace / "report.md").write_text("different", encoding="utf-8")
        self.assertEqual(
            self.store.inspect_unknown_publication(
                self.other_session.id, "report.md"
            ),
            "present_different_hash",
        )
        self.assertIsNone(
            self.store.inspect_unknown_publication(
                self.other_session.id, "new-report.md"
            )
        )
        state = self.store.connection().execute(
            "SELECT recovery_state FROM tool_calls WHERE run_id=? AND call_id=?",
            (prepared.run_id, call_id),
        ).fetchone()[0]
        self.assertEqual(state, "WRITE_OUTCOME_UNKNOWN")

    def test_saved_definitive_write_failure_does_not_become_unknown(self):
        prepared = self.submit("known-failure")
        call_id = self.begin_write(prepared, "known-failure")
        approval_id = self.allow(prepared, call_id, "known-failure")
        content_hash = hashlib.sha256(b"planned").hexdigest()
        prepared.journal.record_publication_intent(
            call_id,
            {
                "path": "report.md",
                "bytes": 7,
                "sha256": content_hash,
                "approval_id": approval_id,
            },
        )
        prepared.journal.record_tool_result(
            call_id,
            {"ok": False, "error": {"code": "FILE_EXISTS"}},
        )

        self.store.recover_interrupted("new-process")

        self.assertEqual(
            self.store.connection().execute(
                """SELECT t.stage, t.recovery_state, r.stop_reason
                   FROM tool_calls t JOIN runs r ON r.id=t.run_id
                   WHERE t.run_id=? AND t.call_id=?""",
                (prepared.run_id, call_id),
            ).fetchone(),
            ("failed", "none", "PROCESS_INTERRUPTED"),
        )
        self.assertIsNone(
            self.store.inspect_unknown_publication(
                self.other_session.id, "report.md"
            )
        )

    def test_live_wal_backup_contains_relations_without_copying_artifact(self):
        prepared = self.submit("confirmed")
        call_id = self.begin_write(prepared, "confirmed")
        approval_id = self.allow(prepared, call_id, "confirmed")
        content = b"planned"
        digest = hashlib.sha256(content).hexdigest()
        prepared.journal.record_publication_intent(
            call_id,
            {
                "path": "confirmed.md",
                "bytes": len(content),
                "sha256": digest,
                "approval_id": approval_id,
            },
        )
        artifact_path = self.workspace / "confirmed.md"
        artifact_path.write_bytes(content)
        prepared.journal.record_publication_receipt(
            call_id,
            {
                "path": "confirmed.md",
                "bytes": len(content),
                "sha256": digest,
                "operation": "created",
            },
        )
        backup_dir = self.root / "backup"
        backup_dir.mkdir()
        backup = self.store.backup(backup_dir / "sessions.sqlite3")

        uri = backup.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("sessions", "runs", "messages", "tool_calls", "artifacts")
            }
            link = connection.execute(
                """SELECT a.run_id, a.call_id, t.assistant_message_id
                   FROM artifacts a JOIN tool_calls t
                     ON t.run_id=a.run_id AND t.call_id=a.call_id"""
            ).fetchone()
        self.assertEqual(counts, {"sessions": 2, "runs": 1, "messages": 2, "tool_calls": 1, "artifacts": 1})
        self.assertEqual(link[:2], (prepared.run_id, call_id))
        self.assertTrue(link[2])
        self.assertFalse((backup_dir / "confirmed.md").exists())
        self.assertEqual(artifact_path.read_bytes(), content)

    def test_sigkill_submission_model_and_read_stages_recover_without_replay(self):
        cases = {
            "after_submit": ("submitted", 0, 0),
            "model_in_flight": ("model_in_flight", 1, 0),
            "read_started": ("tool_started", 1, 1),
        }
        for pause_point, (phase, provider_calls, tool_calls) in cases.items():
            with self.subTest(pause_point=pause_point):
                payload, state, _workspace, activity = self.kill_process_fixture(
                    pause_point
                )
                before = self.read_activity(activity)
                self.assertEqual(
                    [item["kind"] for item in before].count("provider"),
                    provider_calls,
                )
                self.assertEqual(
                    [item["kind"] for item in before].count("tool"), tool_calls
                )

                store = SessionStore.open(state)
                try:
                    self.assertEqual(store.recover_interrupted("restart-process"), 1)
                    service = SessionService(store)
                    recovered = service.load_run(
                        payload["session_id"], payload["run_id"]
                    )
                    self.assertEqual(
                        (recovered.state, recovered.phase, recovered.stop_reason),
                        ("interrupted", phase, "PROCESS_INTERRUPTED"),
                    )
                    continued = service.continue_interrupted(
                        payload["session_id"],
                        payload["run_id"],
                        f"continue-{pause_point}",
                    )
                    self.assertTrue(continued.created)
                    self.assertNotEqual(continued.run_id, payload["run_id"])
                    self.assertEqual(
                        continued.submission.parent_run_id, payload["run_id"]
                    )
                    self.assertIsNone(continued.submission.output_path)
                    self.assertEqual(self.read_activity(activity), before)
                    if pause_point == "read_started":
                        call = store.connection().execute(
                            """SELECT stage, result_message_id, recovery_state
                               FROM tool_calls WHERE run_id=? AND call_id=?""",
                            (payload["run_id"], payload["call_id"]),
                        ).fetchone()
                        self.assertEqual(call, ("interrupted", None, "interrupted"))
                finally:
                    store.close()

    def test_sigkill_invalidates_old_approvals_and_new_run_requires_new_preview(self):
        for pause_point, old_decision in (
            ("approval_waiting", "pending"),
            ("approval_allowed", "allowed"),
        ):
            with self.subTest(pause_point=pause_point):
                payload, state, _workspace, activity = self.kill_process_fixture(
                    pause_point
                )
                before = self.read_activity(activity)
                self.assertEqual(
                    [item["kind"] for item in before], ["provider"]
                )
                store = SessionStore.open(state)
                broker = None
                try:
                    self.assertEqual(store.recover_interrupted("restart-process"), 1)
                    service = SessionService(store)
                    view = service.run_view(payload["session_id"], payload["run_id"])
                    self.assertEqual(view["approvals"][0]["decision"], "expired")
                    self.assertEqual(
                        view["approvals"][0]["preview"]["path"], "report.md"
                    )
                    self.assertEqual(payload["decision_before_kill"], old_decision)

                    continued = service.continue_interrupted(
                        payload["session_id"], payload["run_id"],
                        f"continue-{pause_point}",
                    )
                    broker = ApprovalBroker(
                        continued.run_id,
                        journal=continued.journal,
                        process_generation="restart-process",
                    )
                    with self.assertRaises(ApprovalError) as error:
                        broker.decide(payload["approval_id"], "allow")
                    self.assertEqual(error.exception.code, "APPROVAL_NOT_FOUND")
                    self.assertIsNone(continued.submission.output_path)

                    output_path = f"renewed-{pause_point}.md"
                    renewed = service.submit(
                        payload["session_id"],
                        RunSubmission(
                            client_request_id=f"renewed-{pause_point}",
                            question="重新生成报告",
                            task_type="files",
                            scope=SessionScope("directory", None),
                            output_path=output_path,
                            parent_run_id=payload["run_id"],
                            execution_options={},
                        ),
                    )
                    call_id = f"new-call-{pause_point}"
                    renewed.journal.record_model_request(
                        {"messages": []}, {"kind": "renewed-preview"}
                    )
                    renewed.journal.record_model_reply(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [write_call(call_id, output_path)],
                        },
                        usage=None,
                        validation="tool_calls_valid",
                    )
                    new_approval_id = f"new-approval-{pause_point}"
                    renewed.journal.record_approval_required(
                        call_id,
                        {
                            "id": new_approval_id,
                            "preview": {
                                "path": output_path,
                                "action_summary": "重新预览并创建报告",
                            },
                            "argument_hash": hashlib.sha256(
                                write_call(call_id, output_path)["function"][
                                    "arguments"
                                ].encode("utf-8")
                            ).hexdigest(),
                            "process_generation": "restart-process",
                        },
                    )
                    stored = store.connection().execute(
                        """SELECT id, run_id, call_id, decision, process_generation,
                                  argument_hash
                           FROM approvals WHERE id=?""",
                        (new_approval_id,),
                    ).fetchone()
                    new_argument_hash = hashlib.sha256(
                        write_call(call_id, output_path)["function"][
                            "arguments"
                        ].encode("utf-8")
                    ).hexdigest()
                    self.assertEqual(
                        stored,
                        (
                            new_approval_id,
                            renewed.run_id,
                            call_id,
                            "pending",
                            "restart-process",
                            new_argument_hash,
                        ),
                    )
                    self.assertNotEqual(renewed.run_id, payload["run_id"])
                    self.assertNotEqual(call_id, payload["call_id"])
                    self.assertEqual(self.read_activity(activity), before)
                finally:
                    if broker is not None:
                        broker.close()
                    store.close()

    def test_sigkill_classifies_publication_windows_without_inventing_receipts(self):
        expected = {
            "approval_allowed": {
                "stop_reason": "PROCESS_INTERRUPTED",
                "stage": "interrupted",
                "publication": "none",
                "recovery": "interrupted",
                "file": False,
                "artifacts": 0,
            },
            "publication_intent": {
                "stop_reason": "WRITE_OUTCOME_UNKNOWN",
                "stage": "unknown",
                "publication": "unknown",
                "recovery": "WRITE_OUTCOME_UNKNOWN",
                "file": True,
                "artifacts": 0,
            },
            "publication_receipt": {
                "stop_reason": "PROCESS_INTERRUPTED",
                "stage": "succeeded",
                "publication": "confirmed",
                "recovery": "confirmed",
                "file": True,
                "artifacts": 1,
            },
        }
        for pause_point, wanted in expected.items():
            with self.subTest(pause_point=pause_point):
                payload, state, workspace, activity = self.kill_process_fixture(
                    pause_point
                )
                before = self.read_activity(activity)
                store = SessionStore.open(state)
                try:
                    self.assertEqual(store.recover_interrupted("restart-process"), 1)
                    service = SessionService(store)
                    recovered = service.load_run(
                        payload["session_id"], payload["run_id"]
                    )
                    self.assertEqual(recovered.state, "interrupted")
                    self.assertEqual(recovered.stop_reason, wanted["stop_reason"])
                    call = store.connection().execute(
                        """SELECT stage, publication_state, recovery_state
                           FROM tool_calls WHERE run_id=? AND call_id=?""",
                        (payload["run_id"], payload["call_id"]),
                    ).fetchone()
                    self.assertEqual(
                        call,
                        (wanted["stage"], wanted["publication"], wanted["recovery"]),
                    )
                    artifacts = store.connection().execute(
                        "SELECT path, bytes, sha256 FROM artifacts WHERE run_id=?",
                        (payload["run_id"],),
                    ).fetchall()
                    self.assertEqual(len(artifacts), wanted["artifacts"])
                    target = workspace / "report.md"
                    self.assertEqual(target.exists(), wanted["file"])
                    if target.exists():
                        self.assertEqual(target.read_text(encoding="utf-8"), "planned")
                    if pause_point == "publication_intent":
                        self.assertEqual(
                            store.inspect_unknown_publication(
                                payload["session_id"], "report.md"
                            ),
                            "present_same_hash",
                        )
                        self.assertEqual(artifacts, [])
                    if pause_point == "publication_receipt":
                        self.assertEqual(
                            artifacts[0],
                            (
                                "report.md",
                                len(b"planned"),
                                hashlib.sha256(b"planned").hexdigest(),
                            ),
                        )
                    continued = service.continue_interrupted(
                        payload["session_id"], payload["run_id"],
                        f"continue-{pause_point}",
                    )
                    self.assertIsNone(continued.submission.output_path)
                    self.assertEqual(self.read_activity(activity), before)
                finally:
                    store.close()

    def test_second_owner_is_rejected_by_store_cli_and_web_without_corruption(self):
        process, payload, state, workspace, _activity = self.start_process_fixture(
            "after_submit"
        )
        try:
            with self.assertRaises(StoreError) as store_error:
                SessionStore.open(state)
            self.assertEqual(store_error.exception.code, "STATE_IN_USE")

            command = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "local_agent",
                    "sessions",
                    "--state-dir",
                    str(state),
                    "show",
                    payload["session_id"],
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(command.returncode, 2, command.stderr)
            self.assertEqual(json.loads(command.stdout), {"error": "STATE_IN_USE"})

            with self.assertRaises(StoreError) as web_error:
                create_server(
                    workspace,
                    self.root / "second-owner-web-runs",
                    state_dir=state,
                    port=0,
                    provider_factory=lambda: None,
                )
            self.assertEqual(web_error.exception.code, "STATE_IN_USE")
        finally:
            self._stop_process(process)

        store = SessionStore.open(state)
        try:
            self.assertEqual(store.recover_interrupted("restart-process"), 1)
            self.assertEqual(
                store.connection().execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
            self.assertEqual(
                SessionService(store).load(payload["session_id"]).id,
                payload["session_id"],
            )
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
