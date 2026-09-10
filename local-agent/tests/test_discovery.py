import errno
import importlib
import importlib.util
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from local_agent import files


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def assert_error(self, result, code):
        self.assertEqual(set(result), {"ok", "error"})
        self.assertIs(result["ok"], False)
        self.assertEqual(set(result["error"]), {"code", "message"})
        self.assertEqual(result["error"]["code"], code)
        self.assertNotIn(str(self.root), str(result))

    def write(self, path, content="private body"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target


class ListFilesTests(WorkspaceTests):
    def setUp(self):
        super().setUp()
        lister_type = getattr(files, "ListFiles", None)
        self.assertIsNotNone(lister_type, "ListFiles must provide bounded directory discovery")
        self.lister = lister_type(self.root)

    def test_lists_sorted_one_level_metadata_without_reading_content(self):
        self.write("z.txt", "last")
        self.write("a.md", "正文")
        self.write("docs/nested.md")
        with patch("local_agent.files.os.read", side_effect=AssertionError("body read")):
            result = self.lister.execute({"path": "."})
        self.assertEqual(result, {"ok": True, "path": ".", "entries": [
            {"path": "a.md", "type": "file", "bytes": 6},
            {"path": "docs", "type": "directory"},
            {"path": "z.txt", "type": "file", "bytes": 4},
        ], "complete": True})
        self.assertNotIn("private body", str(result))
        self.assertEqual(self.lister.execute({"path": "docs"})["entries"], [
            {"path": "docs/nested.md", "type": "file", "bytes": 12}])

    def test_empty_directory_is_complete(self):
        self.assertEqual(self.lister.execute({"path": "."}), {
            "ok": True, "path": ".", "entries": [], "complete": True})

    def test_requires_exact_argument_schema(self):
        for arguments in [None, [], ".", {}, {"path": 1}, {"path": True},
                          {"path": ".", "extra": 1}]:
            with self.subTest(arguments=arguments):
                self.assert_error(self.lister.execute(arguments), "INVALID_ARGUMENT")

    def test_rejects_unsafe_and_hidden_paths(self):
        for path in ["", "/", str(self.root), "..", "../docs", "./docs", "docs/..",
                     "docs//sub", "docs/", ".hidden", "docs/.hidden", "docs\\sub",
                     "bad\x00name", "bad\nname", "bad\x7fname", "bad\udcffname"]:
            with self.subTest(path=path):
                self.assert_error(self.lister.execute({"path": path}), "PATH_DENIED")

    def test_omits_hidden_unsupported_and_unsafe_names(self):
        for path in [".hidden.md", "data.json", "script.py", "noextension", "bad\nname.md",
                     "bad\tname.txt", "bad\x7fname.md", "bad\u0085name.md", "UPPER.MD"]:
            self.write(path)
        (self.root / ".hidden-dir").mkdir()
        self.write("visible.md")
        self.assertEqual(self.lister.execute({"path": "."})["entries"], [
            {"path": "visible.md", "type": "file", "bytes": 12}])

    def test_filesystem_rejects_or_listing_omits_invalid_utf8_name(self):
        invalid_name = os.fsencode(self.root) + b"/bad\xff.md"
        try:
            descriptor = os.open(invalid_name, os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError as error:
            self.assertEqual(error.errno, errno.EILSEQ)
        else:
            os.close(descriptor)
        self.assertEqual(self.lister.execute({"path": "."})["entries"], [])

    def test_omits_symlinks_hardlinks_fifo_and_socket(self):
        original = self.write("original.md")
        os.link(original, self.root / "hardlink.md")
        (self.root / "link.md").symlink_to(original)
        (self.root / "directory").mkdir()
        (self.root / "linked-directory").symlink_to(self.root / "directory", target_is_directory=True)
        os.mkfifo(self.root / "pipe.txt")
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.root / "socket.txt"))
        self.assertEqual(self.lister.execute({"path": "."})["entries"], [
            {"path": "directory", "type": "directory"}])

    def test_nested_symlink_is_denied(self):
        self.write("actual/sub/a.md")
        (self.root / "linked").symlink_to(self.root / "actual", target_is_directory=True)
        for path in ["linked", "linked/sub"]:
            self.assert_error(self.lister.execute({"path": path}), "PATH_DENIED")

    def test_missing_directory_and_file_as_directory_are_safe_errors(self):
        self.assert_error(self.lister.execute({"path": "missing"}), "DIRECTORY_NOT_FOUND")
        self.write("file.md")
        self.assert_error(self.lister.execute({"path": "file.md"}), "PATH_DENIED")

    def test_exactly_200_scanned_entries_are_allowed_and_201_has_no_partial_list(self):
        for number in range(200):
            self.write(f".omitted-{number}")
        self.assertEqual(self.lister.execute({"path": "."})["entries"], [])
        self.write("visible.md")
        self.assert_error(self.lister.execute({"path": "."}), "DIRECTORY_TOO_LARGE")

    def test_does_not_reresolve_original_workspace_alias(self):
        self.write("original/a.md", "original")
        self.write("replacement/b.md", "replacement")
        alias = self.root / "alias"
        alias.symlink_to(self.root / "original", target_is_directory=True)
        lister = files.ListFiles(alias)
        alias.unlink()
        alias.symlink_to(self.root / "replacement", target_is_directory=True)
        self.assertEqual(lister.execute({"path": "."})["entries"], [
            {"path": "a.md", "type": "file", "bytes": 8}])

    def test_pinned_workspace_replaced_with_symlink_is_denied(self):
        root = self.root / "workspace"
        root.mkdir()
        self.write("outside/secret.md")
        lister = files.ListFiles(root)
        root.rename(self.root / "original")
        root.symlink_to(self.root / "outside", target_is_directory=True)
        self.assert_error(lister.execute({"path": "."}), "PATH_DENIED")

    def test_pinned_tools_reject_ordinary_workspace_directory_replacement(self):
        root = self.root / "workspace"
        self.write("workspace/a.md", "original")
        lister = files.ListFiles(root)
        reader = files.ReadFile(root, {"a.md"})
        root.rename(self.root / "original")
        self.write("workspace/a.md", "private replacement")
        for tool, path in [(lister, "."), (reader, "a.md")]:
            result = tool.execute({"path": path})
            self.assert_error(result, "WORKSPACE_CHANGED")
            self.assertNotIn("private replacement", str(result))

    def test_directory_replaced_with_symlink_during_open_is_denied(self):
        self.write("docs/a.md")
        self.write("outside/secret.md")
        real_open = os.open

        def replace_before_open(path, flags, *args, **kwargs):
            if path == "docs":
                (self.root / "docs").rename(self.root / "original")
                (self.root / "docs").symlink_to(self.root / "outside", target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with patch("local_agent.files.os.open", side_effect=replace_before_open):
            self.assert_error(self.lister.execute({"path": "docs"}), "PATH_DENIED")

    def test_detects_directory_mutation_during_listing(self):
        self.write("a.md")
        real_scandir = os.scandir

        def mutate_after_scandir(fd):
            iterator = real_scandir(fd)
            self.write("added.md")
            return iterator

        with patch("local_agent.files.os.scandir", side_effect=mutate_after_scandir):
            self.assert_error(self.lister.execute({"path": "."}), "DIRECTORY_CHANGED")

    def test_listing_error_does_not_expose_exception_message(self):
        private = f"{self.root}/private-token-value"
        with patch("local_agent.files.os.scandir", side_effect=OSError(private)):
            result = self.lister.execute({"path": "."})
        self.assert_error(result, "LIST_ERROR")
        self.assertNotIn("private-token-value", str(result))


class DirectoryToolsTests(WorkspaceTests):
    def setUp(self):
        super().setUp()
        self.assertIsNotNone(importlib.util.find_spec("local_agent.discovery"),
                             "discovery must provide a per-run authorization ledger")
        self.discovery = importlib.import_module("local_agent.discovery")
        self.tools = self.discovery.DirectoryTools(self.root)

    def accepted(self, name, path):
        arguments = {"path": path}
        result = self.tools.execute(name, arguments)
        self.tools.record(name, arguments, result)
        return result

    def test_exports_strict_schemas_and_pinned_workspace(self):
        self.assertEqual(self.tools.workspace, self.root)
        self.assertEqual(self.tools.schemas, [self.discovery.LIST_FILES_SCHEMA,
                                             self.discovery.READ_FILE_SCHEMA])
        for schema, name in zip(self.tools.schemas, ["list_files", "read_file"]):
            self.assertEqual(schema["type"], "function")
            self.assertEqual(schema["function"]["name"], name)
            self.assertEqual(schema["function"]["parameters"], {
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False})

    def test_empty_initial_coverage_and_empty_root_completion(self):
        self.assertEqual(self.tools.coverage(), {
            "attempted": False, "had_error": False, "listed_directories": [],
            "unlisted_directories": ["."], "discovered_files": [], "read_files": [],
            "unread_files": [], "complete": False})
        self.accepted("list_files", ".")
        self.assertEqual(self.tools.coverage(), {
            "attempted": True, "had_error": False, "listed_directories": ["."],
            "unlisted_directories": [], "discovered_files": [], "read_files": [],
            "unread_files": [], "complete": True})

    def test_requires_discovery_before_listing_nested_directory_or_reading(self):
        self.write("docs/a.md")
        self.assert_error(self.tools.execute("list_files", {"path": "docs"}), "PATH_NOT_DISCOVERED")
        self.assert_error(self.tools.execute("read_file", {"path": "docs/a.md"}), "PATH_NOT_DISCOVERED")
        self.accepted("list_files", ".")
        self.assert_error(self.tools.execute("read_file", {"path": "docs/a.md"}), "PATH_NOT_DISCOVERED")
        self.accepted("list_files", "docs")
        self.assertIs(self.accepted("read_file", "docs/a.md")["ok"], True)
        self.assertIs(self.tools.coverage()["complete"], True)

    def test_execute_is_pure_until_accepted_record(self):
        self.write("docs/a.md")
        before = self.tools.coverage()
        result = self.tools.execute("list_files", {"path": "."})
        self.assertEqual(self.tools.coverage(), before)
        self.assert_error(self.tools.execute("list_files", {"path": "docs"}), "PATH_NOT_DISCOVERED")
        self.tools.record("list_files", {"path": "."}, result)
        self.accepted("list_files", "docs")
        before_read = self.tools.coverage()
        result = self.tools.execute("read_file", {"path": "docs/a.md"})
        self.assertEqual(self.tools.coverage(), before_read)
        self.tools.record("read_file", {"path": "docs/a.md"}, result)
        self.assertEqual(self.tools.coverage()["read_files"], ["docs/a.md"])

    def test_partial_coverage_is_sorted_and_returned_lists_are_independent(self):
        self.write("z.md")
        self.write("a.md")
        self.write("b/sub.md")
        (self.root / "a-dir").mkdir()
        self.accepted("list_files", ".")
        self.accepted("read_file", "z.md")
        coverage = self.tools.coverage()
        self.assertEqual(coverage, {"attempted": True, "had_error": False,
            "listed_directories": ["."], "unlisted_directories": ["a-dir", "b"],
            "discovered_files": ["a.md", "z.md"], "read_files": ["z.md"],
            "unread_files": ["a.md"], "complete": False})
        coverage["unread_files"].clear()
        self.assertEqual(self.tools.coverage()["unread_files"], ["a.md"])

    def test_four_distinct_successful_files_limit_allows_reread(self):
        for number in range(5):
            self.write(f"{number}.md")
        self.accepted("list_files", ".")
        for number in range(4):
            self.assertIs(self.accepted("read_file", f"{number}.md")["ok"], True)
        self.assertIs(self.accepted("read_file", "0.md")["ok"], True)
        with patch("local_agent.files.os.read", side_effect=AssertionError("fifth body read")):
            self.assert_error(self.accepted("read_file", "4.md"), "FILE_COUNT_LIMIT")
        self.assertEqual(self.tools.coverage()["unread_files"], ["4.md"])

    def test_failed_read_does_not_consume_distinct_success_limit(self):
        self.tools = self.discovery.DirectoryTools(self.root, max_files=1)
        self.write("bad.md").write_bytes(b"\xff")
        self.write("good.md")
        self.accepted("list_files", ".")
        self.assert_error(self.accepted("read_file", "bad.md"), "UNSUPPORTED_FILE")
        self.assertIs(self.accepted("read_file", "good.md")["ok"], True)

    def test_failed_reread_revokes_current_evidence_without_releasing_limit(self):
        self.tools = self.discovery.DirectoryTools(self.root, max_files=1)
        first = self.write("first.md")
        self.write("second.md")
        self.accepted("list_files", ".")
        self.accepted("read_file", "first.md")
        first.unlink()
        self.assert_error(self.accepted("read_file", "first.md"), "FILE_NOT_FOUND")
        self.assertEqual(self.tools.coverage()["read_files"], [])
        self.assertEqual(self.tools.coverage()["unread_files"], ["first.md", "second.md"])
        self.assert_error(self.accepted("read_file", "second.md"), "FILE_COUNT_LIMIT")
        self.write("first.md", "fresh body")
        self.assertEqual(self.accepted("read_file", "first.md")["content"], "fresh body")

    def test_record_error_marks_attempt_without_revoking_unrelated_reads(self):
        self.write("a.md")
        self.accepted("list_files", ".")
        self.accepted("read_file", "a.md")
        self.tools.record("read_file", None, {"ok": False, "error": {"code": "INVALID_ARGUMENT"}})
        self.assertEqual(self.tools.coverage()["read_files"], ["a.md"])
        self.assertIs(self.tools.coverage()["had_error"], True)
        self.assertIs(self.tools.coverage()["attempted"], True)

    def test_record_success_must_match_requested_and_discovered_path(self):
        self.write("a.md")
        self.accepted("list_files", ".")
        result = self.tools.execute("read_file", {"path": "a.md"})
        self.tools.record("read_file", {"path": "other.md"}, result)
        self.assertEqual(self.tools.coverage()["read_files"], [])
        self.tools.record("list_files", {"path": "undiscovered"}, {
            "ok": True, "path": "undiscovered", "entries": [
                {"path": "undiscovered/secret.md", "type": "file", "bytes": 1}], "complete": True})
        self.assertEqual(self.tools.coverage()["discovered_files"], ["a.md"])

    def test_repeat_listing_keeps_prior_discovered_scope(self):
        self.write("gone.md")
        self.accepted("list_files", ".")
        (self.root / "gone.md").unlink()
        self.accepted("list_files", ".")
        self.assertEqual(self.tools.coverage()["unread_files"], ["gone.md"])
        self.assertIs(self.tools.coverage()["complete"], False)

    def test_failed_relisting_revokes_coverage_and_successful_recovery_restores_it(self):
        self.write("a.md")
        self.accepted("list_files", ".")
        self.accepted("read_file", "a.md")
        self.assertIs(self.tools.coverage()["complete"], True)
        for number in range(200):
            self.write(f".omitted-{number}")
        self.assert_error(self.accepted("list_files", "."), "DIRECTORY_TOO_LARGE")
        coverage = self.tools.coverage()
        self.assertIs(coverage["complete"], False)
        self.assertEqual(coverage["listed_directories"], [])
        self.assertEqual(coverage["unlisted_directories"], ["."])
        self.assertEqual(coverage["discovered_files"], ["a.md"])
        self.assertEqual(coverage["read_files"], ["a.md"])
        for number in range(200):
            (self.root / f".omitted-{number}").unlink()
        self.accepted("list_files", ".")
        self.assertIs(self.tools.coverage()["complete"], True)
        self.assertIs(self.tools.coverage()["had_error"], True)

    def test_invalid_arguments_still_revoke_identifiable_read_and_list_paths(self):
        self.tools = self.discovery.DirectoryTools(self.root, max_files=1)
        self.write("a.md")
        self.write("b.md")
        self.accepted("list_files", ".")
        self.accepted("read_file", "a.md")
        for name, path in [("read_file", "a.md"), ("list_files", ".")]:
            arguments = {"path": path, "extra": 1}
            result = self.tools.execute(name, arguments)
            self.assert_error(result, "INVALID_ARGUMENT")
            self.tools.record(name, arguments, result)
        coverage = self.tools.coverage()
        self.assertEqual(coverage["read_files"], [])
        self.assertEqual(coverage["listed_directories"], [])
        self.assertEqual(coverage["unlisted_directories"], ["."])
        self.assertEqual(coverage["discovered_files"], ["a.md", "b.md"])
        self.assertIs(coverage["complete"], False)
        self.assert_error(self.accepted("read_file", "b.md"), "FILE_COUNT_LIMIT")
        self.assertIs(self.accepted("read_file", "a.md")["ok"], True)

    def test_retains_existing_32kib_read_limit(self):
        self.write("large.md", "x" * 32769)
        self.accepted("list_files", ".")
        self.assert_error(self.accepted("read_file", "large.md"), "FILE_TOO_LARGE")

    def test_dynamic_reader_cannot_resolve_redirected_workspace(self):
        self.write("workspace/a.md", "original")
        self.write("outside/a.md", "private replacement")
        root = self.root / "workspace"
        self.tools = self.discovery.DirectoryTools(root)
        self.accepted("list_files", ".")
        root.rename(self.root / "original")
        root.symlink_to(self.root / "outside", target_is_directory=True)
        result = self.accepted("read_file", "a.md")
        self.assert_error(result, "WORKSPACE_CHANGED")
        self.assertNotIn("private replacement", str(result))

    def test_dynamic_reader_rejects_ordinary_workspace_directory_replacement(self):
        self.write("workspace/a.md", "original")
        root = self.root / "workspace"
        self.tools = self.discovery.DirectoryTools(root)
        self.accepted("list_files", ".")
        root.rename(self.root / "original")
        self.write("workspace/a.md", "private replacement")
        with patch("local_agent.files.os.read", side_effect=AssertionError("replacement body read")):
            result = self.accepted("read_file", "a.md")
        self.assert_error(result, "WORKSPACE_CHANGED")
        self.assert_error(self.accepted("list_files", "."), "WORKSPACE_CHANGED")

    def test_root_symlink_substitution_during_dynamic_reader_open_is_denied(self):
        self.write("workspace/a.md", "original")
        self.write("outside/a.md", "private replacement")
        root = self.root / "workspace"
        self.tools = self.discovery.DirectoryTools(root)
        self.accepted("list_files", ".")
        real_open = os.open

        def replace_before_open(path, flags, *args, **kwargs):
            if path == "workspace":
                root.rename(self.root / "original")
                root.symlink_to(self.root / "outside", target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with patch("local_agent.files.os.open", side_effect=replace_before_open):
            result = self.accepted("read_file", "a.md")
        self.assert_error(result, "OS_PERMISSION_DENIED")
        self.assertNotIn("private replacement", str(result))

    def test_argument_errors_are_strict_and_do_not_change_ledger_until_record(self):
        before = self.tools.coverage()
        for name in ["list_files", "read_file"]:
            for arguments in [None, [], {}, {"path": True}, {"path": ".", "extra": 1}]:
                self.assert_error(self.tools.execute(name, arguments), "INVALID_ARGUMENT")
        self.assertEqual(self.tools.coverage(), before)


if __name__ == "__main__":
    unittest.main()
