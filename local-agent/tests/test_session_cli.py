import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SessionCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "note.md").write_text("代号：青禾-47\n", encoding="utf-8")
        self.state = self.root / "state"

    def invoke(self, *args):
        environment = dict(os.environ)
        environment.pop("AGENT_API_KEY", None)
        environment.pop("DEEPSEEK_API_KEY", None)
        return subprocess.run(
            [sys.executable, "-m", "local_agent", *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def json_run(self, *args, expected=0):
        run = self.invoke(*args)
        self.assertEqual(run.returncode, expected, run.stderr or run.stdout)
        return json.loads(run.stdout)

    def test_management_commands_work_without_model_configuration(self):
        created = self.json_run(
            "sessions", "--state-dir", str(self.state), "create",
            "--workspace", str(self.workspace), "--title", "资料整理",
            "--file", "note.md",
        )
        session_id = created["session_id"]
        listed = self.json_run(
            "sessions", "--state-dir", str(self.state), "list",
            "--workspace", str(self.workspace),
        )
        self.assertEqual([item["id"] for item in listed["sessions"]], [session_id])
        shown = self.json_run(
            "sessions", "--state-dir", str(self.state), "show", session_id,
        )
        self.assertEqual(shown["session"]["scope"], {"mode": "file", "target_path": "note.md"})
        renamed = self.json_run(
            "sessions", "--state-dir", str(self.state), "rename", session_id,
            "--title", "新标题",
        )
        self.assertEqual(renamed["session"]["title"], "新标题")
        self.assertEqual(self.json_run(
            "sessions", "--state-dir", str(self.state), "archive", session_id,
        )["session"]["status"], "archived")
        self.assertEqual(self.json_run(
            "sessions", "--state-dir", str(self.state), "restore", session_id,
        )["session"]["status"], "active")
        backup = self.json_run(
            "sessions", "--state-dir", str(self.state), "backup",
            str(self.root / "backups"),
        )
        backup_path = Path(backup["backup_path"])
        self.assertTrue(backup_path.is_file())
        uri = backup_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        finally:
            connection.close()
        self.assertEqual(backup["sessions"], 1)

    def test_persistent_run_uses_fixed_scope_and_preserves_submission_when_key_missing(self):
        created = self.json_run(
            "sessions", "--state-dir", str(self.state), "create",
            "--workspace", str(self.workspace), "--title", "资料整理",
            "--file", "note.md",
        )
        run = self.json_run(
            "run", "--state-dir", str(self.state), "--session", created["session_id"],
            "--question", "项目代号是什么？", "--client-request-id", "cli-request-1",
            "--log-dir", str(self.root / "runs"),
            expected=2,
        )
        self.assertEqual(run["error"], "CONFIG_MISSING")
        connection = sqlite3.connect(self.state / "sessions.sqlite3")
        try:
            stored = connection.execute(
                "SELECT task_type, question, scope_json FROM runs"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(stored[:2], ("files", "项目代号是什么？"))
        self.assertEqual(json.loads(stored[2]), {"mode": "file", "target_path": "note.md"})

    def test_session_run_rejects_workspace_or_mode_override(self):
        for extra in (
            ("--workspace", str(self.workspace)),
            ("--file", "other.md"),
            ("--discover",),
        ):
            with self.subTest(extra=extra):
                run = self.invoke(
                    "run", "--state-dir", str(self.state), "--session", "session-id",
                    "--question", "问题", *extra,
                )
                self.assertEqual(run.returncode, 2)


if __name__ == "__main__":
    unittest.main()
