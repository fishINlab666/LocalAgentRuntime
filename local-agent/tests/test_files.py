import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_agent.files import ReadFile


class ReadFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "notes.md"
        self.path.write_text("第一行\nsecond line\n", encoding="utf-8")
        self.reader = ReadFile(self.root, {"notes.md"})

    def assert_error(self, result, code):
        self.assertEqual(set(result), {"ok", "error"})
        self.assertIs(result["ok"], False)
        self.assertEqual(set(result["error"]), {"code", "message"})
        self.assertEqual(result["error"]["code"], code)
        self.assertIsInstance(result["error"]["message"], str)
        self.assertNotIn(str(self.root), result["error"]["message"])

    def test_reads_utf8_with_raw_hash_and_normalized_newlines(self):
        raw = "第一行\r\nsecond line\rlast\n".encode("utf-8")
        self.path.write_bytes(raw)
        self.assertEqual(self.reader.execute({"path": "notes.md"}), {
            "ok": True,
            "path": "notes.md",
            "content": "第一行\nsecond line\nlast\n",
            "line_count": 3,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })

    def test_empty_file_has_zero_lines(self):
        self.path.write_bytes(b"")
        result = self.reader.execute({"path": "notes.md"})
        self.assertIs(result["ok"], True)
        self.assertEqual(result["content"], "")
        self.assertEqual(result["line_count"], 0)

    def test_line_count_matches_splitlines(self):
        content = "one\n\ntwo\u2028three\n"
        self.path.write_text(content, encoding="utf-8")
        self.assertEqual(self.reader.execute({"path": "notes.md"})["line_count"],
                         len(content.splitlines()))

    def test_reads_nested_allowed_text_file(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "memo.txt").write_text("memo", encoding="utf-8")
        result = ReadFile(self.root, {"docs/memo.txt"}).execute({"path": "docs/memo.txt"})
        self.assertIs(result["ok"], True)
        self.assertEqual(result["content"], "memo")

    def test_requires_exact_argument_schema(self):
        for arguments in [None, [], "notes.md", {}, {"path": 1}, {"path": True},
                          {"path": "notes.md", "extra": 1}]:
            with self.subTest(arguments=arguments):
                self.assert_error(self.reader.execute(arguments), "INVALID_ARGUMENT")

    def test_rejects_unsafe_path_shapes_even_if_allowlisted(self):
        paths = ["", str(self.path), "../notes.md", "docs/../notes.md", "./notes.md",
                 "notes.md/", "docs//notes.md", "notes\x00.md", ".secret.md",
                 "docs/.private/file.md", "C:\\notes.md", "docs\\notes.md"]
        for path in paths:
            with self.subTest(path=path):
                self.assert_error(ReadFile(self.root, {path}).execute({"path": path}), "PATH_DENIED")

    def test_rejects_unlisted_file(self):
        self.assert_error(ReadFile(self.root, set()).execute({"path": "notes.md"}), "PATH_DENIED")

    def test_allowlist_is_pinned_at_construction(self):
        allowed = {"notes.md"}
        reader = ReadFile(self.root, allowed)
        allowed.add("later.md")
        (self.root / "later.md").write_text("later", encoding="utf-8")
        self.assert_error(reader.execute({"path": "later.md"}), "PATH_DENIED")

    def test_reports_missing_file_and_missing_parent(self):
        for path in ["missing.md", "absent/missing.md"]:
            with self.subTest(path=path):
                self.assert_error(ReadFile(self.root, {path}).execute({"path": path}), "FILE_NOT_FOUND")

    def test_only_md_and_txt_are_supported(self):
        for path in ["notes.json", "notes", "notes.py"]:
            with self.subTest(path=path):
                (self.root / path).write_text("content", encoding="utf-8")
                self.assert_error(ReadFile(self.root, {path}).execute({"path": path}), "UNSUPPORTED_FILE")

    def test_rejects_file_symlink(self):
        (self.root / "link.md").symlink_to(self.path)
        self.assert_error(ReadFile(self.root, {"link.md"}).execute({"path": "link.md"}), "PATH_DENIED")

    def test_rejects_directory_symlink(self):
        (self.root / "real").mkdir()
        (self.root / "real" / "nested.md").write_text("nested", encoding="utf-8")
        (self.root / "linked").symlink_to(self.root / "real", target_is_directory=True)
        path = "linked/nested.md"
        self.assert_error(ReadFile(self.root, {path}).execute({"path": path}), "PATH_DENIED")

    def test_rejects_hardlinked_file(self):
        os.link(self.path, self.root / "alias.md")
        self.assert_error(self.reader.execute({"path": "notes.md"}), "PATH_DENIED")

    def test_rejects_directory_named_like_text_file(self):
        (self.root / "folder.md").mkdir()
        self.assert_error(ReadFile(self.root, {"folder.md"}).execute({"path": "folder.md"}), "UNSUPPORTED_FILE")

    def test_rejects_fifo_without_blocking(self):
        os.mkfifo(self.root / "pipe.md")
        self.assert_error(ReadFile(self.root, {"pipe.md"}).execute({"path": "pipe.md"}), "UNSUPPORTED_FILE")

    def test_accepts_exact_size_limit_and_rejects_larger(self):
        reader = ReadFile(self.root, {"notes.md"}, max_bytes=4)
        self.path.write_bytes(b"1234")
        self.assertIs(reader.execute({"path": "notes.md"})["ok"], True)
        self.path.write_bytes(b"12345")
        self.assert_error(reader.execute({"path": "notes.md"}), "FILE_TOO_LARGE")

    def test_rejects_invalid_utf8_and_binary_controls(self):
        for raw in [b"\xff", b"hello\x00world", b"hello\x01world", b"hello\x7fworld"]:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.assert_error(self.reader.execute({"path": "notes.md"}), "UNSUPPORTED_FILE")

    def test_does_not_cache_file_content(self):
        first = self.reader.execute({"path": "notes.md"})
        self.path.write_text("replacement", encoding="utf-8")
        second = self.reader.execute({"path": "notes.md"})
        self.assertEqual(second["content"], "replacement")
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_resolves_workspace_once(self):
        (self.root / "original").mkdir()
        (self.root / "replacement").mkdir()
        (self.root / "original" / "notes.md").write_text("original", encoding="utf-8")
        (self.root / "replacement" / "notes.md").write_text("replacement", encoding="utf-8")
        alias = self.root / "workspace"
        alias.symlink_to(self.root / "original", target_is_directory=True)
        reader = ReadFile(alias, {"notes.md"})
        alias.unlink()
        alias.symlink_to(self.root / "replacement", target_is_directory=True)
        self.assertEqual(reader.execute({"path": "notes.md"})["content"], "original")

    def test_path_replaced_with_symlink_during_open_is_denied(self):
        real_open = os.open

        def replace_before_open(path, flags, *args, **kwargs):
            if path == "notes.md":
                self.path.unlink()
                self.path.symlink_to(self.root / "other.md")
            return real_open(path, flags, *args, **kwargs)

        (self.root / "other.md").write_text("must stay private", encoding="utf-8")
        with patch("local_agent.files.os.open", side_effect=replace_before_open):
            result = self.reader.execute({"path": "notes.md"})
        self.assert_error(result, "PATH_DENIED")
        self.assertNotIn("must stay private", str(result))

    def test_detects_mutation_during_real_read(self):
        real_read = os.read
        changed = False

        def mutate_after_read(fd, count):
            nonlocal changed
            data = real_read(fd, count)
            if not changed:
                changed = True
                self.path.write_bytes(b"new content")
            return data

        with patch("local_agent.files.os.read", side_effect=mutate_after_read):
            self.assert_error(self.reader.execute({"path": "notes.md"}), "FILE_CHANGED")

    def test_read_error_does_not_expose_exception_message(self):
        private = f"{self.root}/private-token-value"
        with patch("local_agent.files.os.read", side_effect=OSError(private)):
            result = self.reader.execute({"path": "notes.md"})
        self.assert_error(result, "READ_ERROR")
        self.assertNotIn("private-token-value", str(result))


if __name__ == "__main__":
    unittest.main()
