from dataclasses import fields, FrozenInstanceError, is_dataclass
import hashlib
import importlib
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest.mock import patch

from local_agent.session_store import SessionStore
from local_agent.state_maintenance import StateBusy


class RaisingStream:
    def read(self, _size):
        raise OSError("private stream detail")


class LongChunkStream:
    def __init__(self):
        self.returned = False

    def read(self, _size):
        if self.returned:
            return b""
        self.returned = True
        return b"abcd"


class StateChangingStream:
    def __init__(self, connection, import_id, content):
        self.connection = connection
        self.import_id = import_id
        self.stream = BytesIO(content)
        self.changed = False

    def read(self, size):
        if not self.changed:
            self.connection.execute(
                "UPDATE imports SET status = 'cancelled' WHERE id = ?",
                (self.import_id,),
            )
            self.changed = True
        return self.stream.read(size)


class GateCheckingStream:
    def __init__(self, test_case, gate, activity, content):
        self.test_case = test_case
        self.gate = gate
        self.activity = activity
        self.stream = BytesIO(content)

    def read(self, size):
        with self.test_case.assertRaises(StateBusy):
            self.gate.start("import-upload", self.activity)
        return self.stream.read(size)


class CallbackStream:
    def __init__(self, content, callback):
        self.stream = BytesIO(content)
        self.callback = callback
        self.called = False

    def read(self, size):
        if not self.called:
            self.callback()
            self.called = True
        return self.stream.read(size)


def load_import_api(test_case):
    specification = importlib.util.find_spec("local_agent.imports")
    test_case.assertIsNotNone(specification, "ImportStore is not implemented")
    module = importlib.import_module("local_agent.imports")
    return module.ImportStore, module.ImportStoreError


class ImportStoreUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.session_store = SessionStore.open(self.state)
        self.addCleanup(self.session_store.close)

    def store(self, **kwargs):
        ImportStore, _ = load_import_api(self)
        return ImportStore(self.session_store, **kwargs)

    def request(self, path="notes.txt", declared_bytes=3, **changes):
        request = {
            "kind": "file",
            "name": "Notes",
            "agent_id": "agent-one",
            "files": [{"logical_path": path, "bytes": declared_bytes}],
            "ignored": [],
        }
        request.update(changes)
        return request

    @staticmethod
    def folder_request(files, ignored=()):
        return {
            "kind": "folder",
            "name": "Folder",
            "agent_id": "agent-one",
            "files": list(files),
            "ignored": list(ignored),
        }

    def assert_store_error(self, code, action):
        _, ImportStoreError = load_import_api(self)
        with self.assertRaises(ImportStoreError) as caught:
            action()
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(caught.exception.code, code)

    def original_for(self, batch, slot):
        return (
            self.state / "imports" / ".staging" / batch.id / "originals"
            / f"{slot.source_id}.bin"
        )

    def assert_pending_without_upload_files(self, store, batch, slot):
        self.assertEqual(store.snapshot(batch.id).files[0].upload_state, "pending")
        originals = self.original_for(batch, slot).parent
        self.assertEqual(list(originals.iterdir()), [])
        self.assertFalse((self.state / "imports" / batch.id).exists())

    def method(self, store, name):
        method = getattr(store, name, None)
        self.assertTrue(callable(method), f"ImportStore.{name} is not implemented")
        return method

    def test_begin_and_add_file_store_an_exact_private_copy(self):
        store = self.store()
        batch = store.begin(self.request(path="Notes.TXT"))

        self.assertEqual(batch.status, "uploading")
        self.assertEqual(len(batch.files), 1)
        slot = batch.files[0]
        self.assertEqual(slot.logical_path, "Notes.TXT")
        self.assertEqual(slot.extension, ".txt")
        self.assertEqual(slot.declared_bytes, 3)
        self.assertEqual(slot.upload_state, "pending")
        with self.assertRaises(FrozenInstanceError):
            slot.logical_path = "changed.txt"

        receipt = store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 3)

        self.assertEqual(receipt.import_id, batch.id)
        self.assertEqual(receipt.source_id, slot.source_id)
        self.assertEqual(receipt.bytes, 3)
        self.assertEqual(receipt.sha256, hashlib.sha256(b"abc").hexdigest())
        original = (
            self.state / "imports" / ".staging" / batch.id / "originals"
            / f"{slot.source_id}.bin"
        )
        self.assertEqual(original.read_bytes(), b"abc")
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o600)
        for directory in (original.parent, original.parent.parent,
                          self.state / "imports", self.state / "imports" / ".staging"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertFalse((self.state / "imports" / batch.id).exists())
        snapshot = store.snapshot(batch.id)
        self.assertEqual(snapshot.status, "uploading")
        self.assertEqual(snapshot.files[0].upload_state, "stored")
        row = self.session_store.connection().execute(
            "SELECT upload_state, sha256 FROM import_files WHERE import_id = ?",
            (batch.id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("stored", receipt.sha256))

    def test_begin_rejects_invalid_logical_paths(self):
        invalid_paths = (
            "", "/absolute.txt", ".", "..", "./notes.txt", "a/../notes.txt",
            "/notes.txt", "notes.txt/", "a//notes.txt", "a\\notes.txt",
            "nul\x00.txt", "line\nfeed.txt", "format\u202etxt.md",
        )
        store = self.store()
        for path in invalid_paths:
            with self.subTest(path=repr(path)):
                self.assert_store_error(
                    "IMPORT_PATH_INVALID", lambda path=path: store.begin(self.request(path=path))
                )
        self.assertEqual(
            self.session_store.connection().execute(
                "SELECT COUNT(*) FROM imports"
            ).fetchone()[0],
            0,
        )

    def test_begin_requires_exact_request_and_item_shapes(self):
        store = self.store()
        valid = self.request()
        invalid = [
            None,
            [],
            {key: value for key, value in valid.items() if key != "name"},
            {**valid, "extra": True},
            {**valid, "kind": "archive"},
            {**valid, "kind": []},
            {**valid, "name": ""},
            {**valid, "name": "bad\x00name"},
            {**valid, "name": "x" * 257},
            {**valid, "agent_id": ""},
            {**valid, "files": tuple(valid["files"])},
            {**valid, "ignored": None},
            {**valid, "files": [{"logical_path": "notes.txt", "bytes": 3,
                                   "extra": True}]},
            {**valid, "files": [{"logical_path": "notes.txt"}]},
            {**valid, "files": []},
            self.folder_request([]),
        ]
        for request in invalid:
            with self.subTest(request=request):
                self.assert_store_error(
                    "IMPORT_REQUEST_INVALID", lambda request=request: store.begin(request)
                )
        self.assertEqual(
            self.session_store.connection().execute(
                "SELECT COUNT(*) FROM imports"
            ).fetchone()[0],
            0,
        )

    def test_supported_extensions_are_casefolded_and_ignored_items_get_no_slots(self):
        store = self.store()
        request = self.folder_request(
            [
                {"logical_path": "one.MD", "bytes": 0},
                {"logical_path": "two.Txt", "bytes": 0},
                {"logical_path": "three.PDF", "bytes": 0},
                {"logical_path": "four.DocX", "bytes": 0},
            ],
            [{"logical_path": "ignored/image.PNG", "bytes": 123}],
        )

        batch = store.begin(request)

        self.assertEqual(
            [slot.extension for slot in batch.files],
            [".md", ".txt", ".pdf", ".docx"],
        )
        self.assertEqual(
            self.session_store.connection().execute(
                "SELECT COUNT(*) FROM import_files WHERE import_id = ?", (batch.id,)
            ).fetchone()[0],
            4,
        )

    def test_file_kind_accepts_multiple_supported_files_and_ignored_metadata(self):
        store = self.store()
        _, ImportStoreError = load_import_api(self)
        request = self.request(
            files=[
                {"logical_path": "one.txt", "bytes": 1},
                {"logical_path": "nested/two.MD", "bytes": 2},
            ],
            ignored=[{"logical_path": "image.PNG", "bytes": 3}],
        )

        try:
            batch = store.begin(request)
        except ImportStoreError as error:
            self.fail(f"file-kind multi-selection was rejected: {error.code}")

        self.assertEqual(
            [slot.logical_path for slot in batch.files],
            ["one.txt", "nested/two.MD"],
        )
        self.assertEqual(
            self.session_store.connection().execute(
                "SELECT COUNT(*) FROM import_files WHERE import_id = ?", (batch.id,)
            ).fetchone()[0],
            2,
        )

    def test_unsupported_file_and_supported_ignored_entry_are_rejected(self):
        store = self.store()
        self.assert_store_error(
            "IMPORT_FORMAT_UNSUPPORTED",
            lambda: store.begin(self.request(path="notes.rtf")),
        )
        self.assert_store_error(
            "IMPORT_REQUEST_INVALID",
            lambda: store.begin(self.folder_request(
                [{"logical_path": "notes.txt", "bytes": 3}],
                [{"logical_path": "wrong.md", "bytes": 1}],
            )),
        )

    def test_duplicate_paths_are_exact_while_case_variants_remain_distinct(self):
        store = self.store()
        duplicate = self.folder_request(
            [{"logical_path": "notes.txt", "bytes": 1}],
            [{"logical_path": "notes.txt", "bytes": 1}],
        )
        self.assert_store_error("IMPORT_PATH_INVALID", lambda: store.begin(duplicate))

        batch = store.begin(self.folder_request([
            {"logical_path": "Notes.txt", "bytes": 1},
            {"logical_path": "notes.txt", "bytes": 1},
        ]))
        self.assertEqual(
            [slot.logical_path for slot in batch.files], ["Notes.txt", "notes.txt"]
        )

    def test_item_and_supported_file_count_limits_reject_plus_one(self):
        store = self.store()
        too_many_items = self.folder_request(
            [{"logical_path": "kept.txt", "bytes": 0}],
            [
                {"logical_path": f"ignored/{number}.png", "bytes": 0}
                for number in range(500)
            ],
        )
        self.assert_store_error(
            "IMPORT_LIMIT_EXCEEDED", lambda: store.begin(too_many_items)
        )
        too_many_files = self.folder_request([
            {"logical_path": f"files/{number}.txt", "bytes": 0}
            for number in range(51)
        ])
        self.assert_store_error(
            "IMPORT_LIMIT_EXCEEDED", lambda: store.begin(too_many_files)
        )

    def test_exact_item_and_file_count_boundaries_are_accepted(self):
        store = self.store()
        request = self.folder_request(
            [
                {"logical_path": f"files/{number}.txt", "bytes": 0}
                for number in range(50)
            ],
            [
                {"logical_path": f"ignored/{number}.png", "bytes": 0}
                for number in range(450)
            ],
        )

        batch = store.begin(request)

        self.assertEqual(len(batch.files), 50)

    def test_byte_limits_reject_plus_one_and_accept_exact_boundaries(self):
        mib = 1024 * 1024
        store = self.store()
        self.assert_store_error(
            "IMPORT_LIMIT_EXCEEDED",
            lambda: store.begin(self.request(declared_bytes=20 * mib + 1)),
        )
        too_large_total = self.folder_request(
            [
                {"logical_path": f"part-{number}.txt", "bytes": 20 * mib}
                for number in range(5)
            ] + [{"logical_path": "extra.txt", "bytes": 1}]
        )
        self.assert_store_error(
            "IMPORT_LIMIT_EXCEEDED", lambda: store.begin(too_large_total)
        )

        exact = store.begin(self.folder_request([
            {"logical_path": f"exact-{number}.txt", "bytes": 20 * mib}
            for number in range(5)
        ]))
        self.assertEqual(sum(slot.declared_bytes for slot in exact.files), 100 * mib)

        small = self.store(limits={
            "max_items": 3,
            "max_files": 2,
            "max_file_bytes": 3,
            "max_total_bytes": 5,
        })
        boundary = small.begin(self.folder_request(
            [
                {"logical_path": "a.txt", "bytes": 2},
                {"logical_path": "b.md", "bytes": 3},
            ],
            [{"logical_path": "image.png", "bytes": 999}],
        ))
        self.assertEqual(len(boundary.files), 2)

    def test_declared_bytes_reject_bool_negative_and_non_integer(self):
        store = self.store()
        for value in (True, -1, 1.0, "1", None):
            with self.subTest(value=value):
                self.assert_store_error(
                    "IMPORT_REQUEST_INVALID",
                    lambda value=value: store.begin(self.request(declared_bytes=value)),
                )

    def test_begin_persists_canonical_request_fingerprint_and_uuid_ids(self):
        store = self.store(clock=lambda: 123.5)
        request = self.request(path="资料/Notes.TXT")

        batch = store.begin(request)

        row = self.session_store.connection().execute(
            """SELECT request_json, request_fingerprint, status, created_at, updated_at
               FROM imports WHERE id = ?""",
            (batch.id,),
        ).fetchone()
        canonical = json.dumps(
            request, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
        self.assertEqual(row[0], canonical)
        self.assertEqual(row[1], hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(tuple(row[2:]), ("uploading", 123.5, 123.5))
        for identifier in (batch.id, batch.files[0].source_id, batch.files[0].slot_id):
            self.assertRegex(identifier, re.compile(r"^[0-9a-f]{32}$"))

    def test_declared_content_length_must_match_before_copying(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]

        self.assert_store_error(
            "IMPORT_LENGTH_MISMATCH",
            lambda: store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 2),
        )

        original = (
            self.state / "imports" / ".staging" / batch.id / "originals"
            / f"{slot.source_id}.bin"
        )
        self.assertFalse(original.exists())
        self.assertEqual(store.snapshot(batch.id).files[0].upload_state, "pending")

    def test_content_length_rejects_bool_negative_non_integer_and_wrong_value(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        for value, code in (
            (True, "IMPORT_REQUEST_INVALID"),
            (-1, "IMPORT_REQUEST_INVALID"),
            (3.0, "IMPORT_REQUEST_INVALID"),
            ("3", "IMPORT_REQUEST_INVALID"),
            (4, "IMPORT_LENGTH_MISMATCH"),
        ):
            with self.subTest(value=value):
                self.assert_store_error(
                    code,
                    lambda value=value: store.add_file(
                        batch.id, slot.slot_id, BytesIO(b"abc"), value
                    ),
                )
                self.assert_pending_without_upload_files(store, batch, slot)

    def test_short_long_and_read_exception_leave_slot_pending_and_no_copy(self):
        cases = (
            (BytesIO(b"ab"), "IMPORT_LENGTH_MISMATCH"),
            (BytesIO(b"abcd"), "IMPORT_LENGTH_MISMATCH"),
            (LongChunkStream(), "IMPORT_LENGTH_MISMATCH"),
            (RaisingStream(), "IMPORT_STORAGE_FAILED"),
        )
        for stream, code in cases:
            with self.subTest(code=code, stream=type(stream).__name__):
                store = self.store()
                batch = store.begin(self.request())
                slot = batch.files[0]
                self.assert_store_error(
                    code,
                    lambda: store.add_file(batch.id, slot.slot_id, stream, 3),
                )
                self.assert_pending_without_upload_files(store, batch, slot)

    def test_file_system_exception_is_redacted_and_cleans_candidate(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]

        with patch("local_agent.imports.os.fsync", side_effect=OSError("secret path")):
            self.assert_store_error(
                "IMPORT_STORAGE_FAILED",
                lambda: store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 3),
            )

        self.assert_pending_without_upload_files(store, batch, slot)

    def test_unknown_import_slot_and_non_uploading_state_are_stable_errors(self):
        store = self.store()
        self.assert_store_error(
            "IMPORT_NOT_FOUND",
            lambda: store.add_file("0" * 32, "1" * 32, BytesIO(b""), 0),
        )
        self.assert_store_error("IMPORT_NOT_FOUND", lambda: store.snapshot("0" * 32))
        self.assert_store_error("IMPORT_NOT_FOUND", lambda: store.cancel("0" * 32))
        self.assert_store_error("IMPORT_NOT_FOUND", lambda: store.open("0" * 32))
        self.assert_store_error(
            "IMPORT_NOT_FOUND", lambda: store.start_finalize("0" * 32)
        )

        batch = store.begin(self.request())
        self.assert_store_error(
            "IMPORT_SLOT_NOT_FOUND",
            lambda: store.add_file(batch.id, "1" * 32, BytesIO(b"abc"), 3),
        )
        store.cancel(batch.id)
        self.assert_store_error(
            "IMPORT_STATE_CONFLICT",
            lambda: store.add_file(
                batch.id, batch.files[0].slot_id, BytesIO(b"abc"), 3
            ),
        )

    def test_state_change_before_upload_cas_removes_final_candidate(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        stream = StateChangingStream(
            self.session_store.connection(), batch.id, b"abc"
        )

        self.assert_store_error(
            "IMPORT_STATE_CONFLICT",
            lambda: store.add_file(batch.id, slot.slot_id, stream, 3),
        )

        self.assertEqual(store.snapshot(batch.id).status, "cancelled")
        self.assert_pending_without_upload_files(store, batch, slot)

    def test_swapped_originals_symlink_is_not_followed(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        originals = self.original_for(batch, slot).parent
        originals.rmdir()
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        os.symlink(outside, originals)

        self.assert_store_error(
            "IMPORT_STORAGE_FAILED",
            lambda: store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 3),
        )

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(store.snapshot(batch.id).files[0].upload_state, "pending")

    def test_replaced_originals_directory_is_rejected_before_upload(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        originals = self.original_for(batch, slot).parent
        held = originals.parent / ".held-originals"
        originals.rename(held)
        originals.mkdir(mode=0o700)
        marker = originals / "keep"
        marker.write_bytes(b"keep")

        self.assert_store_error(
            "IMPORT_STORAGE_FAILED",
            lambda: store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 3),
        )

        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual(list(held.iterdir()), [])
        row = self.session_store.connection().execute(
            """SELECT upload_state, sha256 FROM import_files
               WHERE import_id = ? AND slot_id = ?""",
            (batch.id, slot.slot_id),
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", None))

    def test_replaced_batch_directory_is_rejected_before_upload(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        staging = self.state / "imports" / ".staging"
        batch_root = staging / batch.id
        held = staging / ".held-batch"
        batch_root.rename(held)
        batch_root.mkdir(mode=0o700)
        replacement_originals = batch_root / "originals"
        replacement_originals.mkdir(mode=0o700)
        marker = replacement_originals / "keep"
        marker.write_bytes(b"keep")

        self.assert_store_error(
            "IMPORT_STORAGE_FAILED",
            lambda: store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 3),
        )

        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual(list((held / "originals").iterdir()), [])
        row = self.session_store.connection().execute(
            """SELECT upload_state, sha256 FROM import_files
               WHERE import_id = ? AND slot_id = ?""",
            (batch.id, slot.slot_id),
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", None))

    def test_directory_replacement_during_stream_read_cannot_publish_upload(self):
        for target in ("originals", "batch"):
            with self.subTest(target=target):
                store = self.store()
                batch = store.begin(self.request())
                slot = batch.files[0]
                staging = self.state / "imports" / ".staging"
                batch_root = staging / batch.id
                originals = batch_root / "originals"
                replaced = {}

                def replace_directory():
                    if target == "originals":
                        held = batch_root / ".held-originals"
                        originals.rename(held)
                        originals.mkdir(mode=0o700)
                        replacement_originals = originals
                    else:
                        held = staging / f".held-{batch.id}"
                        batch_root.rename(held)
                        batch_root.mkdir(mode=0o700)
                        replacement_originals = batch_root / "originals"
                        replacement_originals.mkdir(mode=0o700)
                    marker = replacement_originals / "keep"
                    marker.write_bytes(b"keep")
                    replaced.update(held=held, marker=marker)

                stream = CallbackStream(b"abc", replace_directory)
                self.assert_store_error(
                    "IMPORT_STORAGE_FAILED",
                    lambda: store.add_file(batch.id, slot.slot_id, stream, 3),
                )

                self.assertEqual(replaced["marker"].read_bytes(), b"keep")
                held_originals = (
                    replaced["held"] if target == "originals"
                    else replaced["held"] / "originals"
                )
                self.assertEqual(list(held_originals.iterdir()), [])
                row = self.session_store.connection().execute(
                    """SELECT upload_state, sha256 FROM import_files
                       WHERE import_id = ? AND slot_id = ?""",
                    (batch.id, slot.slot_id),
                ).fetchone()
                self.assertEqual(tuple(row), ("pending", None))

    def test_temporary_name_replacement_cannot_publish_other_content(self):
        for replacement in ("regular", "symlink"):
            with self.subTest(replacement=replacement):
                store = self.store()
                batch = store.begin(self.request())
                slot = batch.files[0]
                originals = self.original_for(batch, slot).parent
                replaced = {"done": False}

                def replace_temporary_name():
                    candidates = [
                        path for path in originals.iterdir()
                        if path.name.startswith(f".{slot.source_id}.")
                        and path.name.endswith(".tmp")
                    ]
                    if len(candidates) != 1:
                        raise RuntimeError("expected one upload candidate")
                    candidate = candidates[0]
                    candidate.unlink()
                    if replacement == "regular":
                        candidate.write_bytes(b"xyz")
                        candidate.chmod(0o600)
                    else:
                        target = Path(self.tmp.name) / f"target-{batch.id}"
                        target.write_bytes(b"xyz")
                        candidate.symlink_to(target)
                    replaced["done"] = True

                stream = CallbackStream(b"abc", replace_temporary_name)
                self.assert_store_error(
                    "IMPORT_STORAGE_FAILED",
                    lambda: store.add_file(
                        batch.id, slot.slot_id, stream, content_length=3
                    ),
                )

                self.assertTrue(replaced["done"])
                self.assertEqual(list(originals.iterdir()), [])
                row = self.session_store.connection().execute(
                    """SELECT upload_state, sha256 FROM import_files
                       WHERE import_id = ? AND slot_id = ?""",
                    (batch.id, slot.slot_id),
                ).fetchone()
                self.assertEqual(tuple(row), ("pending", None))

    def test_begin_rechecks_dynamic_directories_before_database_commit(self):
        ImportStore, _ = load_import_api(self)
        for target in ("originals", "batch"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary) / "state"
                session_store = SessionStore.open(state)
                replaced = {}

                def clock():
                    staging = state / "imports" / ".staging"
                    batch_root = next(staging.iterdir())
                    originals = batch_root / "originals"
                    if target == "originals":
                        held = batch_root / ".held-originals"
                        originals.rename(held)
                        originals.mkdir(mode=0o700)
                        replacement_originals = originals
                    else:
                        held = staging / f".held-{batch_root.name}"
                        batch_root.rename(held)
                        batch_root.mkdir(mode=0o700)
                        replacement_originals = batch_root / "originals"
                        replacement_originals.mkdir(mode=0o700)
                    marker = replacement_originals / "keep"
                    marker.write_bytes(b"keep")
                    replaced.update(held=held, marker=marker)
                    return 1.0

                try:
                    store = ImportStore(session_store, clock=clock)
                    self.assert_store_error(
                        "IMPORT_STORAGE_FAILED", lambda: store.begin(self.request())
                    )

                    self.assertEqual(replaced["marker"].read_bytes(), b"keep")
                    held_originals = (
                        replaced["held"] if target == "originals"
                        else replaced["held"] / "originals"
                    )
                    self.assertEqual(list(held_originals.iterdir()), [])
                    self.assertEqual(
                        session_store.connection().execute(
                            "SELECT COUNT(*) FROM imports"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    session_store.close()

    def test_replaced_batch_directory_is_not_deleted_by_cancel(self):
        store = self.store()
        batch = store.begin(self.request())
        staging = self.state / "imports" / ".staging"
        batch_root = staging / batch.id
        held = staging / ".held-batch"
        batch_root.rename(held)
        batch_root.mkdir(mode=0o700)
        marker = batch_root / "keep"
        marker.write_bytes(b"keep")

        self.assert_store_error("IMPORT_STORAGE_FAILED", lambda: store.cancel(batch.id))

        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertTrue((held / "originals").is_dir())
        self.assertEqual(store.snapshot(batch.id).status, "cancelled")

    def test_new_store_does_not_trust_existing_dynamic_staging_directories(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        ImportStore, _ = load_import_api(self)
        reopened = ImportStore(self.session_store)

        self.assert_store_error(
            "IMPORT_STORAGE_FAILED",
            lambda: reopened.add_file(
                batch.id, slot.slot_id, BytesIO(b"abc"), 3
            ),
        )
        self.assertEqual(reopened.snapshot(batch.id).files[0].upload_state, "pending")
        self.assert_store_error(
            "IMPORT_STORAGE_FAILED", lambda: reopened.cancel(batch.id)
        )
        self.assertEqual(reopened.snapshot(batch.id).status, "cancelled")
        self.assertTrue(self.original_for(batch, slot).parent.is_dir())

    def test_constructor_rejects_symlink_or_non_directory_managed_roots(self):
        ImportStore, _ = load_import_api(self)
        for scenario in ("root-symlink", "root-file", "staging-symlink"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary) / "state"
                session_store = SessionStore.open(state)
                self.addCleanup(session_store.close)
                outside = Path(temporary) / "outside"
                outside.mkdir()
                root = state / "imports"
                if scenario == "root-symlink":
                    os.symlink(outside, root)
                elif scenario == "root-file":
                    root.write_bytes(b"file")
                else:
                    root.mkdir(mode=0o700)
                    os.symlink(outside, root / ".staging")
                self.assert_store_error(
                    "IMPORT_STORAGE_FAILED", lambda: ImportStore(session_store)
                )

    def test_begin_database_failure_rolls_back_and_removes_staging(self):
        store = self.store()

        with patch.object(
            self.session_store, "transaction",
            side_effect=sqlite3.OperationalError("private database detail"),
        ):
            self.assert_store_error(
                "IMPORT_STORAGE_FAILED", lambda: store.begin(self.request())
            )

        self.assertEqual(list((self.state / "imports" / ".staging").iterdir()), [])
        self.assertEqual(
            self.session_store.connection().execute(
                "SELECT COUNT(*) FROM imports"
            ).fetchone()[0],
            0,
        )

    def test_begin_refuses_replaced_staging_without_writing_outside(self):
        store = self.store()
        staging = self.state / "imports" / ".staging"
        staging.rmdir()
        outside = Path(self.tmp.name) / "outside-staging"
        outside.mkdir()
        os.symlink(outside, staging)

        self.assert_store_error(
            "IMPORT_STORAGE_FAILED", lambda: store.begin(self.request())
        )

        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(
            self.session_store.connection().execute(
                "SELECT COUNT(*) FROM imports"
            ).fetchone()[0],
            0,
        )

    def test_begin_rejects_replaced_managed_directories_without_writing_them(self):
        ImportStore, _ = load_import_api(self)
        for target in ("imports", "staging"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary) / "state"
                session_store = SessionStore.open(state)
                try:
                    store = ImportStore(session_store)
                    root = state / "imports"
                    staging = root / ".staging"
                    if target == "imports":
                        root.rename(state / ".held-imports")
                        root.mkdir(mode=0o700)
                        staging.mkdir(mode=0o700)
                    else:
                        staging.rename(root / ".held-staging")
                        staging.mkdir(mode=0o700)

                    self.assert_store_error(
                        "IMPORT_STORAGE_FAILED", lambda: store.begin(self.request())
                    )

                    self.assertEqual(list(staging.iterdir()), [])
                    self.assertEqual(
                        session_store.connection().execute(
                            "SELECT COUNT(*) FROM imports"
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    session_store.close()

    def test_cancel_refuses_replaced_staging_without_deleting_outside(self):
        store = self.store()
        batch = store.begin(self.request())
        root = self.state / "imports"
        staging = root / ".staging"
        held = root / ".held-staging"
        staging.rename(held)
        outside = Path(self.tmp.name) / "outside-cancel"
        outside.mkdir()
        outside_batch = outside / batch.id
        outside_batch.mkdir()
        marker = outside_batch / "keep"
        marker.write_bytes(b"keep")
        os.symlink(outside, staging)

        self.assert_store_error("IMPORT_STORAGE_FAILED", lambda: store.cancel(batch.id))

        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual(store.snapshot(batch.id).status, "cancelled")
        self.assertTrue((held / batch.id).is_dir())

    def test_cancel_rejects_replaced_managed_directories_without_deleting_them(self):
        ImportStore, _ = load_import_api(self)
        for target in ("imports", "staging"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary) / "state"
                session_store = SessionStore.open(state)
                try:
                    store = ImportStore(session_store)
                    batch = store.begin(self.request())
                    root = state / "imports"
                    staging = root / ".staging"
                    if target == "imports":
                        root.rename(state / ".held-imports")
                        root.mkdir(mode=0o700)
                        staging.mkdir(mode=0o700)
                    else:
                        staging.rename(root / ".held-staging")
                        staging.mkdir(mode=0o700)
                    replacement_batch = staging / batch.id
                    replacement_batch.mkdir(mode=0o700)
                    marker = replacement_batch / "keep"
                    marker.write_bytes(b"keep")

                    self.assert_store_error(
                        "IMPORT_STORAGE_FAILED", lambda: store.cancel(batch.id)
                    )

                    self.assertEqual(marker.read_bytes(), b"keep")
                    self.assertEqual(store.snapshot(batch.id).status, "cancelled")
                finally:
                    session_store.close()

    def test_database_source_id_cannot_escape_originals_directory(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        unsafe_source_id = "./../escape"
        with self.session_store.transaction() as connection:
            connection.execute(
                """UPDATE import_files SET source_id = ?
                   WHERE import_id = ? AND slot_id = ?""",
                (unsafe_source_id, batch.id, slot.slot_id),
            )

        self.assert_store_error(
            "IMPORT_STORAGE_FAILED",
            lambda: store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 3),
        )

        batch_root = self.state / "imports" / ".staging" / batch.id
        self.assertEqual([path.name for path in batch_root.iterdir()], ["originals"])
        self.assertEqual(list((batch_root / "originals").iterdir()), [])
        self.assertFalse((self.state / "imports" / ".staging" / "escape").exists())
        row = self.session_store.connection().execute(
            """SELECT upload_state, sha256 FROM import_files
               WHERE import_id = ? AND slot_id = ?""",
            (batch.id, slot.slot_id),
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", None))

    def test_begin_add_and_cancel_hold_the_shared_maintenance_gate(self):
        observed = []

        def clock():
            active = set(self.session_store.maintenance_gate._active)
            self.assertEqual(len(active), 1)
            self.assertEqual(next(iter(active))[0], "import-upload")
            observed.append(next(iter(active))[1])
            return 10.0

        store = self.store(clock=clock)
        batch = store.begin(self.request())
        slot = batch.files[0]
        activity = f"{batch.id}:{slot.slot_id}"
        stream = GateCheckingStream(
            self, self.session_store.maintenance_gate, activity, b"abc"
        )

        store.add_file(batch.id, slot.slot_id, stream, 3)
        store.cancel(batch.id)

        self.assertTrue(observed[0].startswith("begin:"))
        self.assertEqual(observed[-1], "cancel:" + batch.id)

    def test_zero_byte_upload_and_snapshot_progress_are_exact(self):
        store = self.store()
        batch = store.begin(self.folder_request([
            {"logical_path": "empty.txt", "bytes": 0},
            {"logical_path": "full.md", "bytes": 3},
        ]))

        empty_receipt = store.add_file(
            batch.id, batch.files[0].slot_id, BytesIO(b""), 0
        )

        self.assertEqual(empty_receipt.bytes, 0)
        self.assertEqual(empty_receipt.sha256, hashlib.sha256(b"").hexdigest())
        snapshot = store.snapshot(batch.id)
        self.assertEqual((snapshot.total_files, snapshot.stored_files), (2, 1))
        self.assertIsNone(snapshot.job_id)
        self.assertIsNone(snapshot.session_id)
        self.assertIsNone(snapshot.error_code)

    def test_stored_slot_cannot_be_uploaded_again_or_overwritten(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 3)

        self.assert_store_error(
            "IMPORT_SLOT_ALREADY_STORED",
            lambda: store.add_file(batch.id, slot.slot_id, BytesIO(b"xyz"), 3),
        )

        original = (
            self.state / "imports" / ".staging" / batch.id / "originals"
            / f"{slot.source_id}.bin"
        )
        self.assertEqual(original.read_bytes(), b"abc")

    def test_public_records_are_frozen_and_store_exposes_fixed_methods(self):
        module = importlib.import_module("local_agent.imports")
        expected_fields = {
            "ImportFileSlot": {
                "source_id", "slot_id", "logical_path", "extension",
                "declared_bytes", "upload_state",
            },
            "ImportBatch": {"id", "status", "files"},
            "FileReceipt": {"import_id", "source_id", "bytes", "sha256"},
            "ImportSnapshot": {
                "id", "status", "files", "job_id", "session_id", "error_code",
                "total_files", "stored_files",
            },
            "ImportedWorkspace": {
                "import_id", "root", "originals", "workspace", "artifacts",
                "manifest", "locations", "workspace_device", "workspace_inode",
                "artifact_device", "artifact_inode",
            },
            "ImportJob": {"import_id", "job_id"},
        }
        for name, required in expected_fields.items():
            with self.subTest(record=name):
                record = getattr(module, name)
                self.assertTrue(is_dataclass(record))
                self.assertTrue(record.__dataclass_params__.frozen)
                self.assertTrue(required.issubset({field.name for field in fields(record)}))
        for name in ("begin", "add_file", "start_finalize", "snapshot", "cancel", "open"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(module.ImportStore, name, None)))

    def test_cancel_uploading_marks_cancelled_and_removes_staging(self):
        store = self.store()
        batch = store.begin(self.request())
        batch_root = self.state / "imports" / ".staging" / batch.id
        self.assertTrue(batch_root.is_dir())
        self.assertEqual(
            sum(batch.id in key for key in store._managed_directory_identities), 2
        )

        cancelled = self.method(store, "cancel")(batch.id)

        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(store.snapshot(batch.id).status, "cancelled")
        self.assertFalse(batch_root.exists())
        self.assertFalse((self.state / "imports" / batch.id).exists())
        self.assertFalse(
            any(batch.id in key for key in store._managed_directory_identities)
        )

    def test_finalize_does_not_open_or_create_a_session(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 3)

        job = self.method(store, "start_finalize")(batch.id)
        self.assertEqual(job.import_id, batch.id)
        self.assert_store_error(
            "IMPORT_NOT_READY", lambda: self.method(store, "open")(batch.id)
        )
        self.assertEqual(store.snapshot(batch.id).status, "finalizing")
        self.assertFalse((self.state / "imports" / batch.id).exists())

    def test_cancel_does_not_change_ready_or_delete_formal_import(self):
        store = self.store()
        batch = store.begin(self.request())
        batch_root = self.state / "imports" / ".staging" / batch.id
        formal = self.state / "imports" / batch.id
        formal.mkdir(mode=0o700)
        marker = formal / "kept"
        marker.write_bytes(b"keep")
        with self.session_store.transaction() as connection:
            connection.execute(
                "UPDATE imports SET status = 'ready' WHERE id = ?", (batch.id,)
            )

        self.assert_store_error(
            "IMPORT_STATE_CONFLICT", lambda: self.method(store, "cancel")(batch.id)
        )

        self.assertEqual(store.snapshot(batch.id).status, "ready")
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertTrue(batch_root.is_dir())


class ImportFinalizeFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.session_store = SessionStore.open(self.state)
        self.state = self.session_store.state_dir
        self.addCleanup(lambda: self.session_store.close())

    store = ImportStoreUploadTests.store
    assert_store_error = ImportStoreUploadTests.assert_store_error

    def upload(self, files, **options):
        store = self.store(**options)
        request = ImportStoreUploadTests.folder_request(
            [{"logical_path": name, "bytes": len(raw)} for name, raw in files]
        )
        batch = store.begin(request)
        for slot, (_, raw) in zip(batch.files, files):
            store.add_file(batch.id, slot.slot_id, BytesIO(raw), len(raw))
        return store, batch

    def finalize(self, store, batch):
        try:
            job = store.start_finalize(batch.id)
        except Exception as error:
            self.fail(f"finalize must start for stored slots: {error}")
        self.assertTrue(callable(getattr(job, "run", None)), "ImportJob.run missing")
        return job, job.run()

    def no_session(self, store, batch):
        self.assertIsNone(store.snapshot(batch.id).session_id)
        self.assertEqual(self.session_store.connection().execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone()[0], 0)
        self.assert_store_error("IMPORT_NOT_READY", lambda: store.open(batch.id))


class ImportFinalizeTests(ImportFinalizeFixture, unittest.TestCase):
    def test_two_long_texts_publish_exact_chunks_locations_hashes_and_permissions(self):
        raw_a = ("甲" * 5000 + "\r\n\r\n下一行\r尾").encode()
        raw_b = ("项目数据\n" * 1800 + "末行").encode()
        store, batch = self.upload([("目录/一.TXT", raw_a), ("二.md", raw_b)])
        job, published = self.finalize(store, batch)
        self.assertEqual(published.status, "published_unlinked")
        self.assertEqual(published.import_id, batch.id)
        self.assertEqual(published.job_id, job.job_id)
        self.assertEqual(published.root, self.state / "imports" / batch.id)
        self.assertFalse((store.staging / batch.id).exists())
        self.assertEqual(store.snapshot(batch.id).status, "finalizing")
        self.no_session(store, batch)
        self.assertGreaterEqual(len(published.chunk_paths), 4)
        self.assertLessEqual(len(published.chunk_paths), 256)
        manifest = json.loads(published.manifest.read_bytes())
        locations = json.loads(published.locations.read_bytes())
        self.assertEqual(manifest["status"], "parsed")
        self.assertEqual(manifest["import_id"], batch.id)
        self.assertEqual(len(published.file_records), 2)
        expected_hashes = {}
        for path in published.root.rglob("*"):
            self.assertFalse(path.is_symlink())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
            if path.is_file() and path != published.manifest:
                expected_hashes[path.relative_to(published.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(published.manifest_hashes, expected_hashes)
        self.assertEqual(manifest["hashes"], expected_hashes)
        self.assertEqual(published.manifest.read_bytes(), json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode())
        for slot, raw in zip(batch.files, (raw_a, raw_b)):
            paths = sorted(path for path in published.chunk_paths if path.parent.name == slot.source_id)
            expected = raw.decode().replace("\r\n", "\n").replace("\r", "\n")
            self.assertEqual("".join(path.read_text() for path in paths), expected)
            for path in paths:
                self.assertLessEqual(path.stat().st_size, 12 * 1024)
                mapping = locations[path.relative_to(published.workspace).as_posix()]
                lines = path.read_text().splitlines()
                self.assertEqual(set(mapping), {str(i) for i in range(1, len(lines) + 1)})
                self.assertTrue(all(value[0]["kind"] == "text_lines" for value in mapping.values()))
        index = (published.workspace / "index.md").read_text()
        self.assertLessEqual(len(index.encode()), 16 * 1024)
        self.assertIn("目录/一.TXT", index)
        self.assertNotIn("下一行", index)
        self.assertNotIn(str(self.state), index)
        for name in ("workspace", "artifacts"):
            details = (published.root / name).stat()
            self.assertEqual(published.identities[name], (details.st_dev, details.st_ino))
        with self.assertRaises(FrozenInstanceError):
            published.status = "ready"
        with self.assertRaises(TypeError):
            published.manifest_hashes["locations.json"] = "changed"

    def test_pdf_pages_and_docx_paragraph_table_order_have_original_locations(self):
        from tests.import_fixtures import write_text_pdf, write_docx
        pdf = Path(self.tmp.name) / "sample.pdf"
        docx = Path(self.tmp.name) / "sample.docx"
        write_text_pdf(pdf, ["Page one", "Page two"])
        write_docx(docx)
        store, batch = self.upload([("two.pdf", pdf.read_bytes()), ("word.docx", docx.read_bytes())])
        _, published = self.finalize(store, batch)
        locations = json.loads(published.locations.read_bytes())
        found = []
        for path in published.chunk_paths:
            found.extend(location for values in locations[path.relative_to(published.workspace).as_posix()].values()
                         for location in values)
        self.assertIn({"kind": "pdf_page", "page": 1}, found)
        self.assertIn({"kind": "pdf_page", "page": 2}, found)
        self.assertIn({"kind": "docx_paragraph", "paragraph": 1}, found)
        self.assertIn({"kind": "docx_paragraph", "paragraph": 2}, found)
        self.assertIn({"kind": "docx_table_row", "table": 1, "row": 2}, found)
        text = "".join(path.read_text() for path in published.chunk_paths)
        self.assertLess(text.index("表格之前"), text.index("项目\t青禾-47"))
        self.assertLess(text.index("负责人\t林岚"), text.index("表格之后"))

    def test_empty_text_and_docx_have_no_invented_evidence(self):
        from docx import Document
        docx = Path(self.tmp.name) / "empty.docx"
        Document().save(docx)
        store, batch = self.upload([("empty.txt", b""), ("empty.docx", docx.read_bytes())])
        _, published = self.finalize(store, batch)
        self.assertEqual(published.chunk_paths, ())
        self.assertEqual(json.loads(published.locations.read_bytes()), {})
        self.assertEqual(len(published.file_records), 2)

    def test_parser_failure_fails_the_entire_batch_without_publication(self):
        store, batch = self.upload([("good.txt", b"ok"), ("bad.txt", b"\xff")])
        job = store.start_finalize(batch.id)
        self.assert_store_error("TEXT_INVALID_UTF8", job.run)
        snapshot = store.snapshot(batch.id)
        self.assertEqual((snapshot.status, snapshot.error_code), ("failed", "TEXT_INVALID_UTF8"))
        self.assertFalse((store.root / batch.id).exists())
        self.assertFalse((store.staging / batch.id).exists())
        self.no_session(store, batch)

    def test_incomplete_upload_cannot_enter_finalizing(self):
        store = self.store()
        batch = store.begin(ImportStoreUploadTests.folder_request([
            {"logical_path": "one.txt", "bytes": 1}, {"logical_path": "two.txt", "bytes": 1},
        ]))
        store.add_file(batch.id, batch.files[0].slot_id, BytesIO(b"a"), 1)
        self.assert_store_error("IMPORT_UPLOAD_INCOMPLETE", lambda: store.start_finalize(batch.id))
        self.assertEqual(store.snapshot(batch.id).status, "uploading")
        self.assertIsNone(store.snapshot(batch.id).job_id)

    def test_repeated_start_and_run_rebuild_identical_result_without_reparsing(self):
        store, batch = self.upload([("note.txt", b"one\ntwo")])
        job, published = self.finalize(store, batch)
        self.assertEqual(store.start_finalize(batch.id), job)
        with patch("local_agent.imports.parse_document_in_worker", side_effect=AssertionError("reparsed")):
            self.assertEqual(job.run(), published)
            self.assertEqual(store.start_finalize(batch.id).run(), published)
        self.assertEqual(len(job.job_id), 32)
        with self.assertRaises(FrozenInstanceError):
            job.job_id = "changed"

    def test_chunk_count_batch_text_and_index_limits_fail_before_publication(self):
        for files, limits in (
            ([("x.txt", b"a" * 13000)], {"max_chunks": 1}),
            ([("x.txt", b"abcd"), ("y.txt", b"efgh")], {"max_extracted_total_bytes": 7}),
            ([("x.txt", b"ok")], {"max_index_bytes": 1}),
        ):
            with self.subTest(limits=limits):
                store, batch = self.upload(files, limits=limits)
                job = store.start_finalize(batch.id)
                self.assert_store_error("DOCUMENT_LIMIT_EXCEEDED", job.run)
                self.assertFalse((store.root / batch.id).exists())

    def test_original_tampering_and_formal_tampering_are_rejected(self):
        store, batch = self.upload([("x.txt", b"original")])
        original = store.staging / batch.id / "originals" / (batch.files[0].source_id + ".bin")
        original.write_bytes(b"modified")
        self.assert_store_error("IMPORT_INTEGRITY_ERROR", store.start_finalize(batch.id).run)
        store, batch = self.upload([("x.txt", b"original")])
        job, published = self.finalize(store, batch)
        published.chunk_paths[0].write_bytes(b"modified")
        self.assert_store_error("IMPORT_INTEGRITY_ERROR", job.run)
        self.assert_store_error("IMPORT_INTEGRITY_ERROR", lambda: store.start_finalize(batch.id))
        self.assertTrue(published.root.exists())

    def test_swapped_original_restored_after_worker_cannot_publish_other_text(self):
        from local_agent.import_worker import parse_document_in_worker
        store, batch = self.upload([("note.txt", b"good")])
        job = store.start_finalize(batch.id)
        original = store.staging / batch.id / "originals" / (batch.files[0].source_id + ".bin")
        extracted = []
        def swap_while_parsing(source, logical_path, limits, **kwargs):
            held = source.with_name(source.name + ".held")
            source.rename(held)
            source.write_bytes(b"evil")
            source.chmod(0o600)
            try:
                document = parse_document_in_worker(source, logical_path, limits, **kwargs)
                extracted.append("".join(unit.text for unit in document.units))
                return document
            finally:
                source.unlink()
                held.rename(source)
        with patch("local_agent.imports.parse_document_in_worker", side_effect=swap_while_parsing):
            try:
                published = job.run()
            except Exception as error:
                self.assertEqual(getattr(error, "code", None), "IMPORT_INTEGRITY_ERROR")
            else:
                self.fail(f"swapped content was published: original=good, chunk={published.chunk_paths[0].read_text()!r}")
        self.assertEqual(extracted, ["good"])
        self.assertFalse((store.root / batch.id).exists())
        self.assertEqual(store.snapshot(batch.id).status, "failed")
        self.no_session(store, batch)

    def test_worker_fd_source_uses_the_fixed_object_without_resolving_source_path(self):
        from local_agent.import_worker import parse_document_in_worker
        original = Path(self.tmp.name) / "original.bin"
        original.write_bytes(b"fixed")
        descriptor = os.open(original, os.O_RDONLY)
        try:
            details = os.fstat(descriptor)
            original.rename(original.with_suffix(".held"))
            document = parse_document_in_worker(
                original, "logical.txt", source_fd=descriptor,
                source_identity=(details.st_dev, details.st_ino),
            )
            self.assertEqual("".join(unit.text for unit in document.units), "fixed")
        finally:
            os.close(descriptor)

    def test_worker_fd_rejects_writable_directory_wrong_identity_and_oversized_source(self):
        from local_agent.import_parsers import DocumentParseError, ImportLimits
        from local_agent.import_worker import parse_document_in_worker
        original = Path(self.tmp.name) / "original.bin"
        original.write_bytes(b"fixed")
        for target, flags, wrong_identity, limit, code in (
            (original, os.O_RDWR, False, 10, "DOCUMENT_CORRUPT"),
            (original.parent, os.O_RDONLY | os.O_DIRECTORY, False, 10, "DOCUMENT_CORRUPT"),
            (original, os.O_RDONLY, True, 10, "DOCUMENT_CORRUPT"),
            (original, os.O_RDONLY, False, 4, "DOCUMENT_LIMIT_EXCEEDED"),
        ):
            with self.subTest(flags=flags, wrong_identity=wrong_identity, limit=limit):
                descriptor = os.open(target, flags)
                try:
                    details = os.fstat(descriptor)
                    identity = (details.st_dev, details.st_ino + int(wrong_identity))
                    with self.assertRaises(DocumentParseError) as caught:
                        parse_document_in_worker(
                            original, "logical.txt", ImportLimits(max_file_bytes=limit),
                            source_fd=descriptor, source_identity=identity,
                        )
                    self.assertEqual(caught.exception.code, code)
                finally:
                    os.close(descriptor)

    def test_existing_incomplete_formal_directory_is_never_overwritten(self):
        store, batch = self.upload([("x.txt", b"ok")])
        formal = store.root / batch.id
        formal.mkdir(mode=0o700)
        marker = formal / "keep"
        marker.write_bytes(b"keep")
        self.assert_store_error("IMPORT_INTEGRITY_ERROR", lambda: store.start_finalize(batch.id))
        self.assertEqual(marker.read_bytes(), b"keep")

    def test_run_holds_one_gate_activity_and_closes_its_thread_connection(self):
        from local_agent.import_worker import parse_document_in_worker
        store, batch = self.upload([("x.txt", b"ok")])
        def parser(source, logical_path, limits, **kwargs):
            self.assertEqual(source.name, batch.files[0].source_id + ".bin")
            self.assertEqual(source.parent.name, "originals")
            self.assertEqual(logical_path, "x.txt")
            with self.assertRaises(StateBusy):
                with self.session_store.maintenance_gate.maintenance():
                    pass
            self.assertEqual(len(self.session_store.maintenance_gate._active), 1)
            return parse_document_in_worker(source, logical_path, limits, **kwargs)
        with patch("local_agent.imports.parse_document_in_worker", side_effect=parser), patch.object(
            self.session_store, "close_thread_connection", wraps=self.session_store.close_thread_connection,
        ) as close:
            self.finalize(store, batch)
            self.assertTrue(close.called)
        with self.session_store.maintenance_gate.maintenance():
            pass

    def test_cancel_terminates_real_parser_process_and_cleans_unpublished_batch(self):
        store, batch = self.upload([("x.txt", b"ok")])
        job = store.start_finalize(batch.id)
        started = threading.Event()
        children = []
        real_popen = subprocess.Popen
        def slow_parser(_args, **kwargs):
            process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(process)
            started.set()
            return process
        errors = []
        def run():
            try:
                job.run()
            except Exception as error:
                errors.append(error)
        with patch("local_agent.import_worker.subprocess.Popen", side_effect=slow_parser):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(started.wait(5))
            cancelled = store.cancel(batch.id)
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual([error.code for error in errors], ["IMPORT_CANCELLED"])
        self.assertTrue(all(process.poll() is not None for process in children))
        self.assertFalse((store.staging / batch.id).exists())
        self.assertFalse((store.root / batch.id).exists())
        with self.session_store.maintenance_gate.maintenance():
            pass

    def test_cancel_after_publication_preserves_complete_directory(self):
        store, batch = self.upload([("x.txt", b"ok")])
        _, published = self.finalize(store, batch)
        self.assert_store_error("IMPORT_STATE_CONFLICT", lambda: store.cancel(batch.id))
        self.assertTrue(published.manifest.is_file())
        self.assertEqual(store.snapshot(batch.id).status, "finalizing")

    def test_publication_race_does_not_replace_an_existing_empty_directory(self):
        occupied = []
        def reserve(point):
            if point == "after_parse_fsync":
                target = store.root / batch.id
                target.mkdir(mode=0o700)
                occupied.append(target.stat().st_ino)
        store, batch = self.upload([("x.txt", b"ok")], fault=reserve)
        self.assert_store_error("IMPORT_INTEGRITY_ERROR", store.start_finalize(batch.id).run)
        self.assertEqual((store.root / batch.id).stat().st_ino, occupied[0])
        self.assertEqual(list((store.root / batch.id).iterdir()), [])
        self.no_session(store, batch)

    def test_replaced_root_during_finalize_never_leaks_error_or_deletes_replacement(self):
        moved = self.state / "held-imports"
        def replace(point):
            if point == "after_parse_fsync":
                store.root.rename(moved)
                store.root.mkdir(mode=0o700)
                (store.root / "keep").write_bytes(b"keep")
        store, batch = self.upload([("x.txt", b"ok")], fault=replace)
        self.assert_store_error("IMPORT_INTEGRITY_ERROR", store.start_finalize(batch.id).run)
        self.assertEqual((store.root / "keep").read_bytes(), b"keep")
        self.assertTrue((moved / ".staging" / batch.id / "originals").is_dir())

    def test_unexpected_parser_exception_exposes_only_stable_code(self):
        store, batch = self.upload([("x.txt", b"ok")])
        job = store.start_finalize(batch.id)
        with patch("local_agent.imports.parse_document_in_worker", side_effect=RuntimeError("private detail")):
            self.assert_store_error("IMPORT_STORAGE_FAILED", job.run)
        self.assertEqual(store.snapshot(batch.id).status, "failed")
        self.assertFalse((store.staging / batch.id).exists())

    def test_original_directory_open_failure_closes_all_held_batch_descriptors(self):
        store, batch = self.upload([("x.txt", b"ok")])
        job = store.start_finalize(batch.id)
        opened = []
        original_open = store._batch_fd
        def tracked_open(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor
        with patch.object(store, "_batch_fd", side_effect=tracked_open), patch.object(
            store, "_open_originals_directory", side_effect=OSError("failed open"),
        ):
            self.assert_store_error("IMPORT_INTEGRITY_ERROR", job.run)
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_recovery_directory_error_has_a_stable_code(self):
        store, _batch = self.upload([("x.txt", b"ok")])
        store.root.rename(self.state / "held-imports")
        store.root.mkdir(mode=0o700)
        self.assert_store_error("IMPORT_STORAGE_FAILED", store.recover_interrupted)

    def test_same_job_runs_are_serialized_and_parse_only_once(self):
        from local_agent.import_worker import parse_document_in_worker
        store, batch = self.upload([("x.txt", b"ok")])
        job = store.start_finalize(batch.id)
        results, errors = [], []
        def run():
            try:
                results.append(job.run())
            except Exception as error:
                errors.append(error)
        with patch("local_agent.imports.parse_document_in_worker", wraps=parse_document_in_worker) as parse:
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            self.assertEqual(parse.call_count, 1)

    def test_blank_pdf_page_warning_preserves_nonconsecutive_original_pages(self):
        from tests.import_fixtures import write_text_pdf
        pdf = Path(self.tmp.name) / "pages.pdf"
        write_text_pdf(pdf, ["one", None, "three"])
        store, batch = self.upload([("pages.pdf", pdf.read_bytes())])
        _, published = self.finalize(store, batch)
        source = json.loads(published.manifest.read_bytes())["sources"][0]
        self.assertEqual(source["warnings"], [{"code": "PDF_PAGE_TEXT_NOT_FOUND", "page": 2}])
        found = {loc["page"] for mapping in json.loads(published.locations.read_bytes()).values()
                 for values in mapping.values() for loc in values}
        self.assertEqual(found, {1, 3})


if __name__ == "__main__":
    unittest.main()
