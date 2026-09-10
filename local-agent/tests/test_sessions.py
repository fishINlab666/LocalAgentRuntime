import dataclasses
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from local_agent.session_store import SessionStore
from local_agent.sessions import (
    RunSubmission,
    SessionError,
    SessionScope,
    SessionService,
)


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        self.value += 1.0
        return self.value


class SessionServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "note.md").write_text("代号：青禾-47\n", encoding="utf-8")
        self.clock = MutableClock()
        self.store = SessionStore.open(self.root / "state", clock=self.clock)
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store, clock=self.clock)

    def create_session(self, *, title="  项目核对  ", scope=None):
        return self.service.create(
            self.workspace,
            title,
            scope or SessionScope(mode="file", target_path="note.md"),
        )

    @staticmethod
    def submission(**changes):
        values = {
            "client_request_id": "request-1",
            "question": "核对代号",
            "task_type": "files",
            "scope": SessionScope(mode="file", target_path="note.md"),
            "output_path": None,
            "parent_run_id": None,
            "execution_options": {"max_steps": 6},
        }
        values.update(changes)
        return RunSubmission(**values)

    def test_value_objects_are_frozen_and_fingerprint_is_canonical(self):
        first = self.submission(
            execution_options={"nested": {"z": 1, "a": 2}, "max_steps": 6}
        )
        second = self.submission(
            execution_options={"max_steps": 6, "nested": {"a": 2, "z": 1}}
        )
        self.assertEqual(first.fingerprint(), second.fingerprint())
        self.assertEqual(len(first.fingerprint()), 64)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.question = "change"
        with self.assertRaises(TypeError):
            first.execution_options["max_steps"] = 5
        with self.assertRaises(TypeError):
            first.execution_options["nested"]["a"] = 9
        with self.assertRaises(ValueError):
            self.submission(execution_options={"temperature": math.nan}).fingerprint()

    def test_create_load_rename_archive_and_restore(self):
        created = self.create_session()
        self.assertEqual(created.title, "项目核对")
        self.assertEqual(created.workspace_path, str(self.workspace.resolve()))
        identity = os.stat(self.workspace)
        self.assertEqual(
            (created.workspace_device, created.workspace_inode),
            (identity.st_dev, identity.st_ino),
        )

        loaded = self.service.load(created.id)
        self.assertEqual(loaded, created)
        renamed = self.service.rename(created.id, "  新标题  ")
        self.assertEqual((renamed.title, renamed.revision), ("新标题", 2))
        archived = self.service.archive(created.id)
        self.assertEqual((archived.status, archived.revision), ("archived", 3))
        restored = self.service.restore(created.id)
        self.assertEqual((restored.status, restored.revision), ("active", 4))

    def test_title_scope_and_workspace_validation_have_stable_error_codes(self):
        invalid = [
            (lambda: self.service.create(self.workspace, "   ", SessionScope("directory", None)), "SESSION_TITLE_INVALID"),
            (lambda: self.service.create(self.workspace, "x" * 121, SessionScope("directory", None)), "SESSION_TITLE_INVALID"),
            (lambda: self.service.create(self.root / "missing", "x", SessionScope("directory", None)), "WORKSPACE_NOT_FOUND"),
            (lambda: self.service.create(self.workspace, "x", SessionScope("file", None)), "SESSION_SCOPE_INVALID"),
            (lambda: self.service.create(self.workspace, "x", SessionScope("file", "/note.md")), "SESSION_SCOPE_INVALID"),
            (lambda: self.service.create(self.workspace, "x", SessionScope("file", "../note.md")), "SESSION_SCOPE_INVALID"),
            (lambda: self.service.create(self.workspace, "x", SessionScope("file", "note.pdf")), "SESSION_SCOPE_INVALID"),
            (lambda: self.service.create(self.workspace, "x", SessionScope("directory", "subdir")), "SESSION_SCOPE_INVALID"),
            (lambda: self.service.create(self.workspace, "x", SessionScope("other", None)), "SESSION_SCOPE_INVALID"),
        ]
        for action, code in invalid:
            with self.subTest(code=code):
                with self.assertRaisesRegex(SessionError, code) as caught:
                    action()
                self.assertEqual(caught.exception.code, code)

    def test_list_is_workspace_bound_archived_filtered_and_stably_paginated(self):
        records = [self.create_session(title=f"会话 {index:02d}") for index in range(22)]
        other = self.root / "other"
        other.mkdir()
        self.service.create(other, "其他", SessionScope("directory", None))
        self.service.archive(records[0].id)

        first = self.service.list(self.workspace)
        second = self.service.list(self.workspace, cursor=first.next_cursor)
        active_ids = [record.id for record in first.items + second.items]
        self.assertEqual(len(first.items), 20)
        self.assertEqual(len(second.items), 1)
        self.assertEqual(len(set(active_ids)), 21)
        self.assertNotIn(records[0].id, active_ids)
        self.assertIsNotNone(first.next_cursor)
        self.assertIsNone(second.next_cursor)
        self.assertEqual(
            [item.id for item in self.service.list(self.workspace, archived=True).items],
            [records[0].id],
        )
        with self.assertRaisesRegex(SessionError, "CURSOR_INVALID"):
            self.service.list(self.workspace, cursor="not-a-cursor")

    def test_workspace_identity_prevents_same_path_replacement_from_matching(self):
        created = self.create_session()
        moved = self.root / "old-workspace"
        self.workspace.rename(moved)
        self.workspace.mkdir()

        self.assertEqual(self.service.list(self.workspace).items, ())
        self.assertEqual(self.service.load(created.id).workspace_path, str(self.workspace.resolve()))
        with self.assertRaisesRegex(SessionError, "WORKSPACE_CHANGED"):
            self.service.submit(created.id, self.submission())
        conversation = self.service.submit(
            created.id,
            self.submission(
                client_request_id="conversation-request",
                task_type="conversation",
            ),
        )
        self.assertTrue(conversation.created)

    def test_submit_is_atomic_idempotent_and_uses_complete_fingerprint(self):
        session = self.create_session()
        parent = self.service.submit(
            session.id,
            self.submission(client_request_id="parent-request", question="旧任务"),
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='interrupted', phase='interrupted' WHERE id=?",
                (parent.run_id,),
            )

        base = self.submission()
        first = self.service.submit(session.id, base)
        again = self.service.submit(session.id, base)
        self.assertEqual((again.run_id, again.created), (first.run_id, False))
        self.assertEqual(first.submission, base)

        for changed in (
            dataclasses.replace(base, question="核对日期"),
            dataclasses.replace(base, task_type="conversation"),
            dataclasses.replace(base, scope=SessionScope("file", "other.md")),
            dataclasses.replace(base, output_path="report.md"),
            dataclasses.replace(base, parent_run_id=parent.run_id),
            dataclasses.replace(base, execution_options={"max_steps": 5}),
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(SessionError, "SESSION_REQUEST_CONFLICT"):
                    self.service.submit(session.id, changed)

        rows = self.store.connection().execute(
            "SELECT role, payload_json, session_seq, run_seq FROM messages WHERE run_id=?",
            (first.run_id,),
        ).fetchall()
        self.assertEqual(rows, [("user", json.dumps({"content": "核对代号"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")), 2, 1)])
        run = self.service.load_run(session.id, first.run_id)
        self.assertEqual((run.state, run.phase, run.submission), ("queued", "submitted", base))
        self.assertEqual(self.service.load(session.id).revision, 3)

    def test_submit_rolls_back_run_message_and_revision_when_message_insert_fails(self):
        session = self.create_session()
        before = self.service.load(session.id).revision
        connection = self.store.connection()
        connection.execute(
            """CREATE TRIGGER reject_user_message BEFORE INSERT ON messages
               BEGIN SELECT RAISE(ABORT, 'reject message'); END"""
        )
        with self.assertRaisesRegex(SessionError, "SESSION_STORE_ERROR"):
            self.service.submit(session.id, self.submission())

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.service.load(session.id).revision, before)
        self.assertFalse(connection.in_transaction)

    def test_archived_session_rejects_submission_and_scope_is_fixed(self):
        session = self.create_session()
        self.service.archive(session.id)
        with self.assertRaisesRegex(SessionError, "SESSION_ARCHIVED"):
            self.service.submit(session.id, self.submission())

        restored = self.service.restore(session.id)
        with self.assertRaisesRegex(SessionError, "SESSION_SCOPE_MISMATCH"):
            self.service.submit(
                restored.id,
                self.submission(
                    client_request_id="different-request",
                    scope=SessionScope("file", "other.md"),
                ),
            )

    def test_run_and_message_reads_are_scoped_to_session(self):
        session_a = self.create_session(title="A")
        session_b = self.create_session(title="B")
        prepared = self.service.submit(session_a.id, self.submission())
        message_id = self.store.connection().execute(
            "SELECT id FROM messages WHERE run_id=?", (prepared.run_id,)
        ).fetchone()[0]

        self.assertEqual(self.service.load_run(session_a.id, prepared.run_id).id, prepared.run_id)
        self.assertEqual(self.service.load_message(session_a.id, message_id).id, message_id)
        for action in (
            lambda: self.service.load_run(session_b.id, prepared.run_id),
            lambda: self.service.load_message(session_b.id, message_id),
        ):
            with self.assertRaisesRegex(SessionError, "NOT_FOUND") as caught:
                action()
            self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_continue_interrupted_creates_new_clean_run_and_is_idempotent(self):
        session = self.create_session()
        parent_submission = self.submission(
            client_request_id="parent-request",
            output_path="old-output.md",
            execution_options={"max_steps": 99, "max_tool_calls": 20},
        )
        parent = self.service.submit(session.id, parent_submission)
        connection = self.store.connection()
        connection.execute(
            """UPDATE runs SET state='interrupted', phase='tool_started',
                              config_json='{"max_steps":99}' WHERE id=?""",
            (parent.run_id,),
        )
        parent_user_id = connection.execute(
            "SELECT id FROM messages WHERE run_id=?", (parent.run_id,)
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO messages (
                   id, session_id, run_id, session_seq, run_seq, role,
                   source_kind, payload_json, validation_state, created_at
               ) VALUES (?, ?, ?, 2, 2, 'assistant', 'model', '{}', 'valid', ?)""",
            ("parent-assistant", session.id, parent.run_id, self.clock()),
        )
        connection.execute(
            """INSERT INTO tool_calls (
                   run_id, call_id, session_id, assistant_message_id, name, stage
               ) VALUES (?, 'call-1', ?, 'parent-assistant', 'write_file', 'interrupted')""",
            (parent.run_id, session.id),
        )
        connection.execute(
            """INSERT INTO approvals (
                   id, session_id, run_id, call_id, preview_json, argument_hash,
                   decision, process_generation, created_at
               ) VALUES ('approval-1', ?, ?, 'call-1', '{}', 'hash', 'expired',
                         'old-process', ?)""",
            (session.id, parent.run_id, self.clock()),
        )
        self.assertIsNotNone(parent_user_id)

        continued = self.service.continue_interrupted(
            session.id, parent.run_id, "continue-request"
        )
        again = self.service.continue_interrupted(
            session.id, parent.run_id, "continue-request"
        )
        self.assertEqual((again.run_id, again.created), (continued.run_id, False))
        self.assertNotEqual(continued.run_id, parent.run_id)
        self.assertEqual(continued.submission.question, parent_submission.question)
        self.assertEqual(continued.submission.task_type, parent_submission.task_type)
        self.assertEqual(continued.submission.scope, parent_submission.scope)
        self.assertIsNone(continued.submission.output_path)
        self.assertEqual(continued.submission.parent_run_id, parent.run_id)
        self.assertEqual(continued.submission.execution_options, {})
        row = connection.execute(
            "SELECT config_json, output_path, parent_run_id FROM runs WHERE id=?",
            (continued.run_id,),
        ).fetchone()
        self.assertEqual(row, ("{}", None, parent.run_id))
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE run_id=?", (continued.run_id,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM approvals WHERE run_id=?", (continued.run_id,)
            ).fetchone()[0],
            0,
        )

    def test_continue_requires_owned_interrupted_run_and_new_request_id(self):
        session_a = self.create_session(title="A")
        session_b = self.create_session(title="B")
        parent = self.service.submit(session_a.id, self.submission())
        with self.assertRaisesRegex(SessionError, "RUN_NOT_INTERRUPTED"):
            self.service.continue_interrupted(session_a.id, parent.run_id, "continue")
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state='interrupted' WHERE id=?", (parent.run_id,)
            )
        with self.assertRaisesRegex(SessionError, "NOT_FOUND"):
            self.service.continue_interrupted(session_b.id, parent.run_id, "continue")
        with self.assertRaisesRegex(SessionError, "CLIENT_REQUEST_ID_INVALID"):
            self.service.continue_interrupted(session_a.id, parent.run_id, "  ")

    def test_missing_records_and_invalid_submission_report_stable_codes(self):
        session = self.create_session()
        checks = [
            (lambda: self.service.load("missing"), "NOT_FOUND"),
            (lambda: self.service.rename(session.id, ""), "SESSION_TITLE_INVALID"),
            (lambda: self.service.submit(session.id, self.submission(client_request_id="")), "CLIENT_REQUEST_ID_INVALID"),
            (lambda: self.service.submit(session.id, self.submission(question="")), "QUESTION_INVALID"),
            (lambda: self.service.submit(session.id, self.submission(task_type="other")), "TASK_TYPE_INVALID"),
            (lambda: self.service.submit(session.id, self.submission(execution_options=[])), "EXECUTION_OPTIONS_INVALID"),
            (lambda: self.service.submit(session.id, self.submission(output_path="../out.md")), "OUTPUT_PATH_INVALID"),
            (lambda: self.service.submit(session.id, self.submission(parent_run_id="missing", client_request_id="new")), "NOT_FOUND"),
        ]
        for action, code in checks:
            with self.subTest(code=code):
                with self.assertRaisesRegex(SessionError, code) as caught:
                    action()
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
