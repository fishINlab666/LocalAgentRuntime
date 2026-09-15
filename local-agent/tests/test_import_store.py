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

    def test_finalize_and_open_refuse_until_task_four_without_changing_state(self):
        store = self.store()
        batch = store.begin(self.request())
        slot = batch.files[0]
        store.add_file(batch.id, slot.slot_id, BytesIO(b"abc"), 3)

        self.assert_store_error(
            "IMPORT_NOT_READY", lambda: self.method(store, "start_finalize")(batch.id)
        )
        self.assert_store_error(
            "IMPORT_NOT_READY", lambda: self.method(store, "open")(batch.id)
        )
        self.assertEqual(store.snapshot(batch.id).status, "uploading")
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


if __name__ == "__main__":
    unittest.main()
