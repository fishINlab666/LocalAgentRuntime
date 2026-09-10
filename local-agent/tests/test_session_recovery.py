from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_agent.session_store import SessionStore
from local_agent.sessions import RunSubmission, SessionScope, SessionService


def write_call(call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": '{"path":"report.md","content":"planned"}',
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

    def tearDown(self):
        self.store.close()

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


if __name__ == "__main__":
    unittest.main()
