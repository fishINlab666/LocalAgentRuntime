import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


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
        database_path = Path(backup["database_path"])
        sidecar_path = Path(backup["sidecar_path"])
        manifest_path = Path(backup["manifest_path"])
        self.assertEqual(backup_path, database_path)
        self.assertTrue(backup_path.is_file())
        self.assertTrue(sidecar_path.is_dir())
        self.assertTrue(manifest_path.is_file())
        uri = backup_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
        finally:
            connection.close()
        self.assertEqual(backup["sessions"], 1)
        self.assertEqual(backup["imports"], 0)

    def test_restore_backup_runs_before_opening_destination_state_dir(self):
        from local_agent.session_store import SessionStore
        from local_agent.sessions import SessionService

        published, session, _ = self.imported_session()
        backup = self.json_run(
            "sessions", "--state-dir", str(self.state), "backup",
            str(self.root / "import-backups"),
        )
        database_path = Path(backup["database_path"])
        sidecar_path = Path(backup["sidecar_path"])
        manifest_path = Path(backup["manifest_path"])
        self.assertEqual(Path(backup["backup_path"]), database_path)
        self.assertTrue(database_path.is_file())
        self.assertTrue(sidecar_path.is_dir())
        self.assertTrue(manifest_path.is_file())
        destination = self.root / "restored-state"
        self.assertFalse(destination.exists())

        self.json_run(
            "sessions", "--state-dir", str(destination), "restore-backup",
            str(database_path), str(sidecar_path), str(destination),
        )

        self.assertTrue(destination.is_dir())
        store = SessionStore.open(destination)
        try:
            service = SessionService(store)
            record = service.load(session.id)
            resolved = service.resolver.resolve(record)
            self.assertEqual(record.import_id, published.import_id)
            self.assertEqual(
                resolved.read_root,
                destination.resolve() / "imports" / published.import_id / "workspace",
            )
            self.assertTrue(resolved.read_root.is_dir())
        finally:
            store.close()

    def test_persistent_run_uses_fixed_scope_and_finishes_submission_when_key_missing(self):
        created = self.json_run(
            "sessions", "--state-dir", str(self.state), "create",
            "--workspace", str(self.workspace), "--title", "资料整理",
            "--file", "note.md",
        )
        run = self.json_run(
            "run", "--state-dir", str(self.state), "--session", created["session_id"],
            "--question", "项目代号是什么？", "--client-request-id", "cli-request-1",
            "--log-dir", str(self.root / "runs"),
            expected=1,
        )
        self.assertEqual((run['state'], run['stop_reason'], run['model_calls']),
                         ('failed', 'CONFIG_MISSING', 0))
        connection = sqlite3.connect(self.state / "sessions.sqlite3")
        try:
            stored = connection.execute(
                "SELECT task_type, question, scope_json, state, result_json FROM runs"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(stored[:2], ("files", "项目代号是什么？"))
        self.assertEqual(json.loads(stored[2]), {"mode": "file", "target_path": "note.md"})
        self.assertEqual(stored[3], 'failed')
        self.assertEqual(json.loads(stored[4]), run)
        again = self.json_run(
            'run', '--state-dir', str(self.state), '--session', created['session_id'],
            '--question', '项目代号是什么？', '--client-request-id', 'cli-request-1',
            '--log-dir', str(self.root / 'runs'), expected=1)
        self.assertEqual(again, {**run, 'idempotent_replay': True})

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


    def imported_session(self, *, pending=False):
        from local_agent.session_store import SessionStore
        from local_agent.sessions import SessionService, RunSubmission
        from test_managed_workspace import publish_import
        store = SessionStore.open(self.state)
        try:
            service = SessionService(store)
            published, request = publish_import(store)
            session = service.attach_import(published, request)
            prepared = service.submit(session.id, RunSubmission(
                'interrupted', '读取代号', 'files', session.scope, None, None, {})) if pending else None
            store.connection().execute('UPDATE sessions SET workspace_path=? WHERE id=?',
                                       (str(self.root), session.id))
            return published, session, prepared
        finally:
            store.close()

    def test_import_run_and_continue_share_resolver_and_trusted_trace_root(self):
        from local_agent import __main__ as cli
        from local_agent.approvals import RunControl
        from local_agent.trace import Trace
        from test_managed_workspace import imported_provider
        import threading
        for continued in (False, True):
            with self.subTest(continued=continued):
                published, session, pending = self.imported_session(pending=continued)
                args = cli._build_parser().parse_args([
                    'run', '--state-dir', str(self.state), '--session', session.id,
                    '--question', '读取', '--log-dir', str(self.root / 'logs')])
                args.continue_run = pending.run_id if pending else None
                seen = []
                def trace(directory, workspace, *positional, **kwargs):
                    seen.append(workspace)
                    return Trace(directory, workspace, *positional, **kwargs)
                provider = imported_provider(published)
                with patch('local_agent.agents.AgentDefinition.provider', return_value=provider), \
                        patch.object(cli, 'Trace', side_effect=trace):
                    output, code = cli._persistent_run(args, RunControl(threading.Event()))
                self.assertEqual(code, 0, output)
                self.assertEqual(seen, [published.workspace])
                self.assertEqual(output['answer']['citations'][0]['quote'], '代号：orange-731')

    def test_tampered_import_cli_fails_before_provider_creation(self):
        from local_agent import __main__ as cli
        from local_agent.approvals import RunControl
        import threading
        published, session, _ = self.imported_session()
        published.chunk_paths[0].write_text('tampered\n')
        args = cli._build_parser().parse_args([
            'run', '--state-dir', str(self.state), '--session', session.id,
            '--question', '读取', '--log-dir', str(self.root / 'logs')])
        args.continue_run = None
        with patch('local_agent.agents.AgentDefinition.provider') as provider:
            output, code = cli._persistent_run(args, RunControl(threading.Event()))
        self.assertEqual(code, 2)
        self.assertIn(output['error'], {'IMPORT_INTEGRITY_ERROR', 'IMPORT_UNAVAILABLE'})
        provider.assert_not_called()

    def test_import_cli_persists_failure_when_integrity_changes_after_submission(self):
        from local_agent import __main__ as cli
        from local_agent.approvals import RunControl
        from local_agent.sessions import ManagedWorkspaceResolver
        import threading
        published, session, _ = self.imported_session()
        args = cli._build_parser().parse_args([
            'run', '--state-dir', str(self.state), '--session', session.id,
            '--question', '读取', '--log-dir', str(self.root / 'logs')])
        args.continue_run = None
        resolve = ManagedWorkspaceResolver.resolve
        calls = []
        def changed(resolver, record):
            calls.append(record.id)
            if len(calls) == 2:
                published.chunk_paths[0].write_text('tampered\n')
            return resolve(resolver, record)
        with patch.object(ManagedWorkspaceResolver, 'resolve', changed), \
                patch('local_agent.agents.AgentDefinition.provider') as provider:
            output, code = cli._persistent_run(args, RunControl(threading.Event()))
        self.assertEqual(code, 1, output)
        self.assertEqual((output['state'], output['stop_reason'], output['model_calls']),
                         ('failed', 'IMPORT_INTEGRITY_ERROR', 0))
        provider.assert_not_called()
        connection = sqlite3.connect(self.state / 'sessions.sqlite3')
        try:
            row = connection.execute('SELECT state,result_json FROM runs WHERE session_id=?',
                                     (session.id,)).fetchone()
            self.assertEqual(row[0], 'failed')
            self.assertEqual(json.loads(row[1])['stop_reason'], 'IMPORT_INTEGRITY_ERROR')
        finally:
            connection.close()

    def test_trace_setup_failure_finishes_queued_run_and_replays_without_provider(self):
        from local_agent import __main__ as cli
        from local_agent.approvals import RunControl
        from test_managed_workspace import imported_provider
        import threading
        published, session, _ = self.imported_session()
        log_dir = self.root / 'logs'
        log_dir.write_text('not a directory')
        args = cli._build_parser().parse_args([
            'run', '--state-dir', str(self.state), '--session', session.id,
            '--question', '读取', '--client-request-id', 'trace-failure', '--log-dir', str(log_dir)])
        args.continue_run = None
        provider = imported_provider(published)
        with patch('local_agent.agents.AgentDefinition.provider', return_value=provider):
            output, code = cli._persistent_run(args, RunControl(threading.Event()))
        self.assertEqual(code, 1, output)
        self.assertEqual((output['state'], output['stop_reason'], output['model_calls']),
                         ('failed', 'TRACE_ERROR', 0))
        self.assertEqual(provider.requests, [])
        with patch('local_agent.agents.AgentDefinition.provider') as create_provider, \
                patch.object(cli, 'Trace') as trace:
            again, code = cli._persistent_run(args, RunControl(threading.Event()))
        self.assertEqual(code, 1, again)
        self.assertEqual(again, {**output, 'idempotent_replay': True})
        create_provider.assert_not_called()
        trace.assert_not_called()

    def test_cli_replay_does_not_reopen_workspace_or_create_execution_dependencies(self):
        from local_agent import __main__ as cli
        from local_agent.approvals import RunControl
        from local_agent.session_store import SessionStore
        from local_agent.sessions import SessionService, SessionScope, RunSubmission
        import threading
        for imported in (False, True):
            with self.subTest(imported=imported):
                if imported:
                    _, session, _ = self.imported_session()
                store = SessionStore.open(self.state)
                try:
                    service = SessionService(store)
                    if not imported:
                        session = service.create(self.workspace, 'ordinary', SessionScope('file', 'note.md'))
                    prepared = service.submit(session.id, RunSubmission(
                        'completed-request', '读取', 'files', session.scope, None, None, {}))
                    expected = {'run_id': prepared.run_id, 'state': 'completed', 'stop_reason': 'completed',
                                'model_calls': 1, 'answer': {'answer': 'stored result'}}
                    prepared.journal.finish_run(expected)
                    if imported:
                        store.connection().execute("UPDATE imports SET status='unavailable' WHERE id=?",
                                                   (session.import_id,))
                    else:
                        self.workspace.rename(self.root / 'moved-workspace')
                finally:
                    store.close()
                args = cli._build_parser().parse_args([
                    'run', '--state-dir', str(self.state), '--session', session.id,
                    '--question', '读取', '--client-request-id', 'completed-request'])
                args.continue_run = None
                with patch.object(SessionService, 'trace_root', side_effect=AssertionError('resolve replay')), \
                        patch('local_agent.agents.AgentDefinition.provider') as provider, \
                        patch.object(cli, 'Trace') as trace:
                    output, code = cli._persistent_run(args, RunControl(threading.Event()))
                self.assertEqual(code, 0, output)
                self.assertEqual(output, {**expected, 'idempotent_replay': True})
                provider.assert_not_called()
                trace.assert_not_called()

    def test_disabled_agent_does_not_block_replay_but_still_blocks_new_run(self):
        from local_agent import __main__ as cli
        from local_agent.agents import AgentCatalog
        from local_agent.approvals import RunControl
        from local_agent.session_store import SessionStore
        from local_agent.sessions import SessionService, SessionScope, RunSubmission
        import threading
        store = SessionStore.open(self.state)
        try:
            service = SessionService(store)
            session = service.create(self.workspace, 'ordinary', SessionScope('file', 'note.md'))
            prepared = service.submit(session.id, RunSubmission(
                'completed-request', '读取', 'files', session.scope, None, None, {}))
            expected = {'run_id': prepared.run_id, 'state': 'completed', 'stop_reason': 'completed',
                        'model_calls': 1, 'answer': {'answer': 'stored result'}}
            prepared.journal.finish_run(expected)
            AgentCatalog(self.state / 'agents').set_enabled(session.agent_id, False)
        finally:
            store.close()
        args = cli._build_parser().parse_args([
            'run', '--state-dir', str(self.state), '--session', session.id,
            '--question', '读取', '--client-request-id', 'completed-request'])
        args.continue_run = None
        with patch.object(cli, '_catalog', wraps=cli._catalog) as catalog, \
                patch.object(SessionService, 'trace_root') as resolver, \
                patch('local_agent.agents.AgentDefinition.provider') as provider, \
                patch.object(cli, 'Trace') as trace:
            output, code = cli._persistent_run(args, RunControl(threading.Event()))
            self.assertEqual(code, 0, output)
            self.assertEqual(output, {**expected, 'idempotent_replay': True})
            catalog.assert_not_called()
            resolver.assert_not_called()
            args.client_request_id = 'new-request'
            output, code = cli._persistent_run(args, RunControl(threading.Event()))
            self.assertEqual(code, 1, output)
            self.assertEqual((output['state'], output['stop_reason'], output['model_calls']),
                             ('failed', 'AGENT_DISABLED', 0))
            provider.assert_not_called()
            trace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
