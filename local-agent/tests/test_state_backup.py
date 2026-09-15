import hashlib
import importlib
import importlib.util
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_agent.imports import ImportStore
from local_agent.session_store import SessionStore, StoreError
from local_agent.sessions import RunSubmission, SessionScope, SessionService
from local_agent.state_maintenance import StateBusy
from test_managed_workspace import publish_import


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


class StateBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "note.md").write_text("project: orange-731\n")
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.store = SessionStore.open(self.state)
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)

    def state_backup_class(self):
        specification = importlib.util.find_spec("local_agent.state_backup")
        self.assertIsNotNone(
            specification, "local_agent.state_backup is not implemented"
        )
        module = importlib.import_module("local_agent.state_backup")
        state_backup = getattr(module, "StateBackup", None)
        self.assertTrue(callable(state_backup), "StateBackup is not implemented")
        return state_backup

    def backup(self):
        return self.state_backup_class()(self.store).backup_bundle(self.backups)

    def ready_import(self):
        published, request = publish_import(self.store)
        session = self.service.attach_import(published, request)
        return published, session

    @staticmethod
    def bundle_paths(bundle):
        return Path(bundle.database), Path(bundle.sidecar), Path(bundle.manifest)

    def assert_busy(self, action):
        with self.assertRaises(StoreError) as caught:
            action()
        self.assertEqual(caught.exception.code, "STATE_BUSY")

    def ordinary_submission(self, session, request_id="request"):
        return RunSubmission(
            client_request_id=request_id,
            question="write a report",
            task_type="files",
            scope=session.scope,
            output_path="report.md",
            parent_run_id=None,
            execution_options={},
        )

    def assert_backup_rejects_change_after_import_open(self, change):
        published, _ = self.ready_import()
        backup = self.state_backup_class()(self.store)
        open_import = backup.imports.open
        changed = False

        def change_after_open(import_id):
            nonlocal changed
            workspace = open_import(import_id)
            if not changed:
                change(published)
                changed = True
            return workspace

        before = {path.name for path in self.backups.iterdir()}
        with patch.object(backup.imports, "open", side_effect=change_after_open):
            with self.assertRaises(StoreError):
                backup.backup_bundle(self.backups)
        self.assertTrue(changed)
        self.assertEqual({path.name for path in self.backups.iterdir()}, before)

    def ready_unknown_publication(self):
        published, session = self.ready_import()
        prepared = self.service.submit(
            session.id,
            self.ordinary_submission(session, request_id="unknown-publication"),
        )
        call_id = "write-unknown-report"
        approval_id = "approval-unknown-report"
        content = b"outcome unknown\n"
        digest = hashlib.sha256(content).hexdigest()
        prepared.journal.record_model_request({"messages": []}, {"kind": "test"})
        prepared.journal.record_model_reply(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [write_call(call_id, content=content.decode("utf-8"))],
            },
            usage=None,
            validation="tool_calls_valid",
        )
        prepared.journal.record_approval_required(
            call_id,
            {
                "id": approval_id,
                "preview": {"path": "report.md"},
                "argument_hash": "unknown-arguments",
                "process_generation": "old-process",
            },
        )
        prepared.journal.record_approval_decision(approval_id, "allow")
        prepared.journal.record_publication_intent(
            call_id,
            {
                "path": "report.md",
                "bytes": len(content),
                "sha256": digest,
                "approval_id": approval_id,
            },
        )
        artifact = published.artifacts / "report.md"
        artifact.write_bytes(content)
        artifact.chmod(0o600)
        self.assertEqual(self.store.recover_interrupted("new-process"), 1)
        self.assertEqual(
            self.store.connection().execute(
                "SELECT recovery_state FROM tool_calls WHERE run_id=? AND call_id=?",
                (prepared.run_id, call_id),
            ).fetchone()[0],
            "WRITE_OUTCOME_UNKNOWN",
        )
        return published, session, prepared

    def test_backup_rejects_chunk_changed_after_import_open_before_copy(self):
        def change(published):
            chunk = published.chunk_paths[0]
            original = chunk.read_bytes()
            changed = original.replace(b"731", b"732")
            self.assertNotEqual(changed, original)
            self.assertEqual(len(changed), len(original))
            chunk.write_bytes(changed)

        self.assert_backup_rejects_change_after_import_open(change)

    def test_backup_rejects_new_workspace_file_after_import_open_before_copy(self):
        def change(published):
            unexpected = published.workspace / "unexpected.txt"
            unexpected.write_bytes(b"not in the import manifest\n")
            unexpected.chmod(0o600)

        self.assert_backup_rejects_change_after_import_open(change)

    def test_backup_bundle_copies_sqlite_and_ready_import_as_one_pair(self):
        published, session = self.ready_import()

        bundle = self.backup()
        database, sidecar, manifest_path = self.bundle_paths(bundle)

        self.assertTrue(database.is_file())
        self.assertTrue(sidecar.is_dir())
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(sidecar, Path(str(database) + ".imports"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["database"]["name"], database.name)
        self.assertEqual(
            manifest["database"]["sha256"],
            hashlib.sha256(database.read_bytes()).hexdigest(),
        )
        self.assertEqual([item["id"] for item in manifest["imports"]], [published.import_id])
        copied = sidecar / published.import_id
        for relative in ("manifest.json", "locations.json", "workspace/index.md"):
            self.assertEqual(
                (copied / relative).read_bytes(),
                (published.root / relative).read_bytes(),
            )
        self.assertTrue(any((copied / "originals").iterdir()))
        self.assertFalse((sidecar / ".staging").exists())

        uri = database.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id,import_id FROM sessions WHERE id=?", (session.id,)
                ).fetchone(),
                (session.id, published.import_id),
            )

    def test_backup_refuses_live_run_import_and_approval_activity(self):
        backup = self.state_backup_class()(self.store)
        for kind in ("run-execute", "import-finalize", "approval-execute"):
            with self.subTest(kind=kind):
                lease = self.store.maintenance_gate.start(kind, kind + "-1")
                try:
                    self.assert_busy(lambda: backup.backup_bundle(self.backups))
                finally:
                    lease.close()
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_backup_holds_maintenance_until_bundle_publication(self):
        observed = []

        def inspect_gate(point):
            if point == "before_backup_publish":
                observed.append(point)
                with self.assertRaises(StateBusy):
                    self.store.maintenance_gate.start("run-execute", "new-run")

        backup = self.state_backup_class()(self.store, fault=inspect_gate)
        bundle = backup.backup_bundle(self.backups)

        self.assertEqual(observed, ["before_backup_publish"])
        self.assertTrue(bundle.database.is_file())

    def test_backup_refuses_unfinished_run_and_uploading_import_rows(self):
        session = self.service.create(
            self.workspace, "ordinary", SessionScope("directory", None)
        )
        self.service.submit(session.id, self.ordinary_submission(session))
        self.assert_busy(self.backup)

        other_state = self.root / "upload-state"
        other_store = SessionStore.open(other_state)
        self.addCleanup(other_store.close)
        imports = ImportStore(other_store)
        imports.begin(
            {
                "kind": "file",
                "name": "uploading",
                "agent_id": "directory-qa",
                "files": [{"logical_path": "notes.txt", "bytes": 3}],
                "ignored": [],
            }
        )
        backup = self.state_backup_class()(other_store)
        self.assert_busy(lambda: backup.backup_bundle(self.root / "upload-backups"))

    def test_backup_refuses_pending_approval_even_after_run_finished(self):
        session = self.service.create(
            self.workspace, "approval", SessionScope("directory", None)
        )
        prepared = self.service.submit(session.id, self.ordinary_submission(session))
        call_id = "write-report"
        prepared.journal.record_model_request({"messages": []}, {"kind": "test"})
        prepared.journal.record_model_reply(
            {"role": "assistant", "content": None, "tool_calls": [write_call(call_id)]},
            usage=None,
            validation="tool_calls_valid",
        )
        prepared.journal.record_approval_required(
            call_id,
            {
                "id": "approval-1",
                "preview": {"path": "report.md"},
                "argument_hash": "arguments",
                "process_generation": "test-process",
            },
        )
        prepared.journal.finish_run(
            {"state": "cancelled", "stop_reason": "CANCELLED", "answer": None}
        )
        self.assertEqual(
            self.store.connection().execute(
                "SELECT decision FROM approvals WHERE id='approval-1'"
            ).fetchone()[0],
            "pending",
        )

        self.assert_busy(self.backup)

    def test_restore_bundle_rebinds_import_identity_in_new_state(self):
        published, session = self.ready_import()
        original_identity = published.identities["workspace"]
        bundle = self.backup()
        database, sidecar, _ = self.bundle_paths(bundle)
        destination = self.root / "restored-state"

        self.state_backup_class().restore_bundle(database, sidecar, destination)

        restored_store = SessionStore.open(destination)
        self.addCleanup(restored_store.close)
        restored_service = SessionService(restored_store)
        restored_session = restored_service.load(session.id)
        resolved = restored_service.resolver.resolve(restored_session)
        relative_chunk = published.chunk_paths[0].relative_to(published.workspace)
        self.assertEqual(restored_session.import_id, published.import_id)
        self.assertEqual(
            resolved.read_root,
            destination.resolve() / "imports" / published.import_id / "workspace",
        )
        self.assertNotEqual(resolved.read_identity, original_identity)
        self.assertEqual(
            (resolved.read_root / relative_chunk).read_bytes(),
            published.chunk_paths[0].read_bytes(),
        )

    def test_restore_rejects_missing_or_tampered_parts_without_partial_target(self):
        published, _ = self.ready_import()
        cases = ("missing-sidecar", "missing-file", "tampered-database", "tampered-manifest")
        for index, damage in enumerate(cases):
            with self.subTest(damage=damage):
                bundle = self.backup()
                database, sidecar, manifest = self.bundle_paths(bundle)
                if damage == "missing-sidecar":
                    shutil.rmtree(sidecar)
                elif damage == "missing-file":
                    (sidecar / published.import_id / "workspace" / "index.md").unlink()
                elif damage == "tampered-database":
                    with database.open("ab") as stream:
                        stream.write(b"tampered")
                else:
                    manifest.write_text("{}", encoding="utf-8")
                destination = self.root / f"invalid-restore-{index}"
                before = {path.name for path in self.root.iterdir()}

                with self.assertRaises(StoreError):
                    self.state_backup_class().restore_bundle(
                        database, sidecar, destination
                    )

                self.assertFalse(destination.exists())
                self.assertEqual(
                    {path.name for path in self.root.iterdir()}, before,
                    "failed restore must clean its sibling staging directory",
                )

    def test_restore_requires_nonexistent_destination_and_preserves_existing_data(self):
        self.ready_import()
        bundle = self.backup()
        database, sidecar, _ = self.bundle_paths(bundle)
        destination = self.root / "existing-state"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaises(StoreError):
            self.state_backup_class().restore_bundle(database, sidecar, destination)

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_restore_fault_before_publish_leaves_no_visible_partial_state(self):
        self.ready_import()
        bundle = self.backup()
        database, sidecar, _ = self.bundle_paths(bundle)
        destination = self.root / "faulted-restore"
        observed = []

        def fail_before_publish(point):
            observed.append(point)
            if point == "before_publish_rename":
                raise OSError("simulated restore failure")

        before = {path.name for path in self.root.iterdir()}
        with self.assertRaises(StoreError):
            self.state_backup_class().restore_bundle(
                database, sidecar, destination, fault=fail_before_publish
            )

        self.assertIn("before_publish_rename", observed)
        self.assertFalse(destination.exists())
        self.assertEqual({path.name for path in self.root.iterdir()}, before)

    def test_restore_rejects_manifest_missing_database_unknown_publication(self):
        published, _, _ = self.ready_unknown_publication()
        bundle = self.backup()
        database, sidecar, manifest_path = self.bundle_paths(bundle)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        imported = next(
            item for item in manifest["imports"] if item["id"] == published.import_id
        )
        self.assertEqual(len(imported["unknown"]), 1)
        imported["unknown"] = []
        manifest_path.write_bytes(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        destination = self.root / "unknown-omitted-restore"

        with self.assertRaises(StoreError):
            self.state_backup_class().restore_bundle(database, sidecar, destination)

        self.assertFalse(destination.exists())

    def test_confirmed_import_artifact_survives_restore_with_original_ownership_and_digest(self):
        published, session = self.ready_import()
        prepared = self.service.submit(
            session.id,
            self.ordinary_submission(session, request_id="confirmed-artifact"),
        )
        call_id = "write-confirmed-report"
        approval_id = "approval-confirmed-report"
        content = b"# restored report\n"
        digest = hashlib.sha256(content).hexdigest()
        prepared.journal.record_model_request({"messages": []}, {"kind": "test"})
        prepared.journal.record_model_reply(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    write_call(call_id, content=content.decode("utf-8"))
                ],
            },
            usage=None,
            validation="tool_calls_valid",
        )
        prepared.journal.record_approval_required(
            call_id,
            {
                "id": approval_id,
                "preview": {"path": "report.md"},
                "argument_hash": "confirmed-arguments",
                "process_generation": "test-process",
            },
        )
        prepared.journal.record_approval_decision(approval_id, "allow")
        prepared.journal.record_publication_intent(
            call_id,
            {
                "path": "report.md",
                "bytes": len(content),
                "sha256": digest,
                "approval_id": approval_id,
            },
        )
        artifact_path = published.artifacts / "report.md"
        artifact_path.write_bytes(content)
        artifact_path.chmod(0o600)
        prepared.journal.record_publication_receipt(
            call_id,
            {
                "path": "report.md",
                "bytes": len(content),
                "sha256": digest,
                "operation": "created",
            },
        )
        prepared.journal.record_tool_result(
            call_id,
            {
                "ok": True,
                "path": "report.md",
                "bytes": len(content),
                "sha256": digest,
            },
        )
        prepared.journal.finish_run(
            {"state": "completed", "stop_reason": "completed", "answer": None}
        )
        artifact = self.service.run_view(session.id, prepared.run_id)["artifacts"][0]

        bundle = self.backup()
        database, sidecar, _ = self.bundle_paths(bundle)
        destination = self.root / "restored-artifact-state"
        self.state_backup_class().restore_bundle(database, sidecar, destination)

        restored_store = SessionStore.open(destination)
        self.addCleanup(restored_store.close)
        restored_service = SessionService(restored_store)
        restored_artifact = restored_service.open_artifact(
            session.id,
            prepared.run_id,
            artifact["id"],
            session.agent_id,
        )
        self.assertEqual(restored_artifact.content, content)
        self.assertEqual(restored_artifact.length, len(content))
        self.assertEqual(hashlib.sha256(restored_artifact.content).hexdigest(), digest)
        self.assertEqual(
            restored_service.run_view(session.id, prepared.run_id)["artifacts"][0][
                "sha256"
            ],
            digest,
        )

    def test_backup_rejects_unregistered_artifact_file_without_publishing_bundle(self):
        published, _ = self.ready_import()
        unregistered = published.artifacts / "unregistered.md"
        unregistered.write_bytes(b"not authorized\n")
        unregistered.chmod(0o600)
        before = {path.name for path in self.backups.iterdir()}

        with self.assertRaises(StoreError):
            self.backup()

        self.assertEqual({path.name for path in self.backups.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
