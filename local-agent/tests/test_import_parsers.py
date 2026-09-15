import io
import builtins
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from local_agent import import_worker
from local_agent.import_parsers import (
    DocumentParseError,
    ImportLimits,
    ParsedDocument,
    ParsedUnit,
    SUPPORTED_EXTENSIONS,
    parse_document,
)
from local_agent.import_worker import (
    MAX_WORKER_INPUT_BYTES,
    MAX_WORKER_OUTPUT_BYTES,
    parse_document_in_worker,
)
from tests.import_fixtures import (
    DOCX_PARAGRAPH_AFTER,
    DOCX_PARAGRAPH_BEFORE,
    DOCX_TABLE_ROWS,
    write_docx,
    write_image_pdf,
    write_nested_form_pdf,
    write_text_pdf,
)


class ImportParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def assert_parse_error(self, code, action):
        with self.assertRaises(DocumentParseError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(caught.exception.args, (code,))

    def test_public_contract_and_default_limits_are_stable(self):
        self.assertEqual(SUPPORTED_EXTENSIONS, frozenset({".md", ".txt", ".pdf", ".docx"}))
        self.assertEqual(ImportLimits(), ImportLimits(
            max_file_bytes=20 * 1024 * 1024,
            max_extracted_bytes=512 * 1024,
            max_pdf_pages=500,
            max_pdf_content_bytes=64 * 1024 * 1024,
            max_docx_entries=2000,
            max_docx_uncompressed_bytes=100 * 1024 * 1024,
            max_docx_ratio=100,
        ))
        unit = ParsedUnit("text", {"kind": "text_lines", "start": 1, "end": 1})
        parsed = ParsedDocument("a.txt", (unit,), (), {"source_bytes": 4})
        with self.assertRaises((AttributeError, TypeError)):
            parsed.logical_path = "other.txt"

    def test_txt_and_markdown_preserve_unicode_and_normalize_only_crlf_and_cr(self):
        raw = "第一行\r\nsecond\rlast\n保留\u2028分隔\t尾\n".encode("utf-8")
        for suffix in (".txt", ".MD"):
            with self.subTest(suffix=suffix):
                path = self.root / ("notes" + suffix)
                path.write_bytes(raw)
                parsed = parse_document(path, "docs/notes" + suffix)
                self.assertEqual(parsed.logical_path, "docs/notes" + suffix)
                self.assertEqual(
                    parsed.units,
                    (
                        ParsedUnit("第一行\n", {"kind": "text_lines", "start": 1, "end": 1}),
                        ParsedUnit("second\n", {"kind": "text_lines", "start": 2, "end": 2}),
                        ParsedUnit("last\n", {"kind": "text_lines", "start": 3, "end": 3}),
                        ParsedUnit("保留\u2028分隔\t尾\n", {
                            "kind": "text_lines", "start": 4, "end": 4,
                        }),
                    ),
                )
                self.assertEqual(
                    "".join(unit.text for unit in parsed.units),
                    "第一行\nsecond\nlast\n保留\u2028分隔\t尾\n",
                )
                self.assertEqual(parsed.warnings, ())
                self.assertEqual(parsed.stats["source_bytes"], len(raw))
                self.assertEqual(
                    parsed.stats["extracted_bytes"],
                    len("第一行\nsecond\nlast\n保留\u2028分隔\t尾\n".encode("utf-8")),
                )

    def test_text_units_keep_a_missing_final_newline_and_empty_file_has_no_units(self):
        path = self.root / "notes.txt"
        path.write_bytes("青禾-47\r\n预算：42\r".encode("utf-8"))
        parsed = parse_document(path, "notes.txt")
        self.assertEqual([unit.text for unit in parsed.units], ["青禾-47\n", "预算：42\n"])
        self.assertEqual("".join(unit.text for unit in parsed.units), "青禾-47\n预算：42\n")

        path.write_text("甲\n乙", encoding="utf-8")
        parsed = parse_document(path, "notes.txt")
        self.assertEqual([unit.text for unit in parsed.units], ["甲\n", "乙"])
        self.assertEqual("".join(unit.text for unit in parsed.units), "甲\n乙")
        path.write_bytes(b"")
        self.assertEqual(parse_document(path, "notes.txt").units, ())

    def test_logical_extension_controls_format_dispatch(self):
        source = self.root / "random-source-id"
        source.write_text("内容", encoding="utf-8")
        parsed = parse_document(source, "资料/note.TXT")
        self.assertEqual([unit.text for unit in parsed.units], ["内容"])
        self.assertEqual(parsed.stats["format"], "txt")

        misleading_source = self.root / "looks-like-text.txt"
        misleading_source.write_text("内容", encoding="utf-8")
        self.assert_parse_error(
            "IMPORT_FORMAT_UNSUPPORTED",
            lambda: parse_document(misleading_source, "资料/note.rtf"),
        )

    def test_text_rejects_invalid_utf8_nul_and_other_control_characters(self):
        path = self.root / "notes.txt"
        path.write_bytes(b"bad\xff")
        self.assert_parse_error("TEXT_INVALID_UTF8", lambda: parse_document(path, "notes.txt"))
        for raw in (b"nul\x00byte", b"c0\x01byte", b"del\x7fbyte", "c1\u0085byte".encode()):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                self.assert_parse_error(
                    "TEXT_INVALID_CHARACTER", lambda: parse_document(path, "notes.txt")
                )

    def test_unsupported_extension_has_stable_error(self):
        path = self.root / "notes.rtf"
        path.write_text("text", encoding="utf-8")
        self.assert_parse_error(
            "IMPORT_FORMAT_UNSUPPORTED", lambda: parse_document(path, "notes.rtf")
        )

    def test_file_size_limit_accepts_equal_and_rejects_one_byte_more(self):
        path = self.root / "notes.txt"
        path.write_bytes(b"1234")
        limits = ImportLimits(max_file_bytes=4, max_extracted_bytes=20)
        self.assertEqual(parse_document(path, "notes.txt", limits).units[0].text, "1234")
        path.write_bytes(b"12345")
        self.assert_parse_error(
            "DOCUMENT_LIMIT_EXCEEDED", lambda: parse_document(path, "notes.txt", limits)
        )

    def test_extracted_size_limit_accepts_equal_and_rejects_one_byte_more(self):
        path = self.root / "notes.md"
        path.write_text("é", encoding="utf-8")
        limits = ImportLimits(max_file_bytes=20, max_extracted_bytes=2)
        self.assertEqual(parse_document(path, "notes.md", limits).units[0].text, "é")
        path.write_text("éa", encoding="utf-8")
        self.assert_parse_error(
            "DOCUMENT_LIMIT_EXCEEDED", lambda: parse_document(path, "notes.md", limits)
        )

    def test_pdf_extracts_each_page_with_one_based_location(self):
        path = self.root / "brief.PDF"
        write_text_pdf(path, ("First page", "Second page"))
        parsed = parse_document(path, "brief.PDF")
        self.assertEqual([unit.location for unit in parsed.units], [
            {"kind": "pdf_page", "page": 1},
            {"kind": "pdf_page", "page": 2},
        ])
        self.assertEqual([unit.text.strip() for unit in parsed.units], ["First page", "Second page"])
        self.assertEqual(parsed.warnings, ())

    def test_pdf_partial_empty_page_warns_but_keeps_text_pages(self):
        path = self.root / "partial.pdf"
        write_text_pdf(path, ("Visible", None, "Also visible"))
        parsed = parse_document(path, "partial.pdf")
        self.assertEqual([unit.location["page"] for unit in parsed.units], [1, 3])
        self.assertEqual(parsed.warnings, ({"code": "PDF_PAGE_TEXT_NOT_FOUND", "page": 2},))

    def test_pdf_with_only_an_image_has_no_extractable_text(self):
        path = self.root / "scan.pdf"
        write_image_pdf(path)
        self.assert_parse_error(
            "PDF_TEXT_NOT_FOUND", lambda: parse_document(path, "scan.pdf")
        )

    def test_encrypted_pdf_is_rejected_without_password_attempt(self):
        from pypdf import PdfReader, PdfWriter

        plain = self.root / "plain.pdf"
        encrypted = self.root / "secret.pdf"
        write_text_pdf(plain, ("Secret",))
        reader = PdfReader(io.BytesIO(plain.read_bytes()))
        writer = PdfWriter()
        writer.append_pages_from_reader(reader)
        writer.encrypt("password")
        with encrypted.open("wb") as target:
            writer.write(target)
        self.assert_parse_error(
            "PDF_ENCRYPTED", lambda: parse_document(encrypted, "secret.pdf")
        )

    def test_corrupt_pdf_and_docx_do_not_expose_third_party_errors(self):
        for suffix in (".pdf", ".docx"):
            with self.subTest(suffix=suffix):
                path = self.root / ("broken" + suffix)
                path.write_bytes(b"private-parser-detail: /Users/example/secret")
                self.assert_parse_error(
                    "DOCUMENT_CORRUPT", lambda: parse_document(path, path.name)
                )

    def test_parser_dependency_failure_has_stable_error(self):
        pdf = self.root / "one.pdf"
        docx = self.root / "one.docx"
        write_text_pdf(pdf, ("One",))
        write_docx(docx)
        for module, path in (("pypdf", pdf), ("docx", docx)):
            with self.subTest(module=module), patch.dict(sys.modules, {module: None}):
                self.assert_parse_error(
                    "DOCUMENT_PARSER_UNAVAILABLE", lambda: parse_document(path, path.name)
                )

        real_import = builtins.__import__

        def broken_import(name, *args, **kwargs):
            if name == "pypdf":
                raise RuntimeError("private dependency loader detail")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=broken_import):
            self.assert_parse_error(
                "DOCUMENT_PARSER_UNAVAILABLE", lambda: parse_document(pdf, pdf.name)
            )

    def test_pdf_page_limit_accepts_equal_and_rejects_one_page_more(self):
        path = self.root / "pages.pdf"
        write_text_pdf(path, ("One", "Two"))
        passing = ImportLimits(max_pdf_pages=2)
        self.assertEqual(len(parse_document(path, "pages.pdf", passing).units), 2)
        failing = ImportLimits(max_pdf_pages=1)
        self.assert_parse_error(
            "DOCUMENT_LIMIT_EXCEEDED", lambda: parse_document(path, "pages.pdf", failing)
        )

    def test_pdf_decoded_content_limit_accepts_equal_and_rejects_one_byte_more(self):
        from pypdf import PdfReader

        path = self.root / "content.pdf"
        write_text_pdf(path, ("Bounded",))
        reader = PdfReader(io.BytesIO(path.read_bytes()))
        content_bytes = sum(len(page.get_contents().get_data()) for page in reader.pages)
        base = dict(max_file_bytes=path.stat().st_size, max_extracted_bytes=100)
        self.assertEqual(
            len(parse_document(path, "content.pdf", ImportLimits(
                **base, max_pdf_content_bytes=content_bytes
            )).units),
            1,
        )
        self.assert_parse_error(
            "DOCUMENT_LIMIT_EXCEEDED",
            lambda: parse_document(path, "content.pdf", ImportLimits(
                **base, max_pdf_content_bytes=content_bytes - 1
            )),
        )

    def test_pdf_content_limit_counts_unique_nested_forms_but_not_images(self):
        path = self.root / "forms.pdf"
        metrics = write_nested_form_pdf(path)
        reachable_content = metrics["page_content_bytes"] + metrics["form_content_bytes"]
        self.assertGreater(metrics["image_content_bytes"], metrics["form_content_bytes"])
        base = dict(max_file_bytes=path.stat().st_size, max_extracted_bytes=1000)
        self.assert_parse_error(
            "DOCUMENT_LIMIT_EXCEEDED",
            lambda: parse_document(path, path.name, ImportLimits(
                **base, max_pdf_content_bytes=reachable_content - 1
            )),
        )
        parsed = parse_document(path, path.name, ImportLimits(
            **base, max_pdf_content_bytes=reachable_content
        ))
        self.assertEqual(len(parsed.units), 2)
        self.assertTrue(all("Nested form text" in unit.text for unit in parsed.units))
        self.assertEqual(parsed.stats["pdf_content_bytes"], reachable_content)

    def test_docx_preserves_paragraph_table_row_paragraph_order(self):
        path = self.root / "ordered.DOCX"
        write_docx(path)
        parsed = parse_document(path, "ordered.DOCX")
        expected = [
            ParsedUnit(DOCX_PARAGRAPH_BEFORE, {"kind": "docx_paragraph", "paragraph": 1}),
            ParsedUnit("\t".join(DOCX_TABLE_ROWS[0]), {
                "kind": "docx_table_row", "table": 1, "row": 1,
            }),
            ParsedUnit("\t".join(DOCX_TABLE_ROWS[1]), {
                "kind": "docx_table_row", "table": 1, "row": 2,
            }),
            ParsedUnit(DOCX_PARAGRAPH_AFTER, {"kind": "docx_paragraph", "paragraph": 2}),
        ]
        self.assertEqual(parsed.units, tuple(expected))
        self.assertEqual(parsed.warnings, ())

    def _docx_archive_metrics(self, path):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
        ratios = [
            info.file_size / info.compress_size
            for info in infos
            if info.compress_size
        ]
        return len(infos), sum(info.file_size for info in infos), max(ratios, default=0)

    def test_docx_entry_limit_accepts_equal_and_rejects_one_entry_more(self):
        path = self.root / "entries.docx"
        write_docx(path)
        with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("custom/empty.bin", b"")
        entries, uncompressed, ratio = self._docx_archive_metrics(path)
        base = dict(max_file_bytes=path.stat().st_size, max_docx_uncompressed_bytes=uncompressed,
                    max_docx_ratio=ratio)
        self.assertTrue(parse_document(path, path.name, ImportLimits(
            **base, max_docx_entries=entries
        )).units)
        self.assert_parse_error(
            "DOCUMENT_LIMIT_EXCEEDED",
            lambda: parse_document(path, path.name, ImportLimits(
                **base, max_docx_entries=entries - 1
            )),
        )

    def test_docx_uncompressed_limit_accepts_equal_and_rejects_one_byte_more(self):
        path = self.root / "uncompressed.docx"
        write_docx(path)
        entries, uncompressed, ratio = self._docx_archive_metrics(path)
        base = dict(max_file_bytes=path.stat().st_size, max_docx_entries=entries,
                    max_docx_ratio=ratio)
        self.assertTrue(parse_document(path, path.name, ImportLimits(
            **base, max_docx_uncompressed_bytes=uncompressed
        )).units)
        self.assert_parse_error(
            "DOCUMENT_LIMIT_EXCEEDED",
            lambda: parse_document(path, path.name, ImportLimits(
                **base, max_docx_uncompressed_bytes=uncompressed - 1
            )),
        )

    def test_docx_compression_ratio_accepts_equal_and_rejects_one_ratio_unit_more(self):
        path = self.root / "ratio.docx"
        write_docx(path)
        with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("custom/compressible.bin", b"A" * 10_000)
        entries, uncompressed, ratio = self._docx_archive_metrics(path)
        base = dict(max_file_bytes=path.stat().st_size, max_docx_entries=entries,
                    max_docx_uncompressed_bytes=uncompressed)
        self.assertTrue(parse_document(path, path.name, ImportLimits(
            **base, max_docx_ratio=ratio
        )).units)
        self.assert_parse_error(
            "DOCUMENT_LIMIT_EXCEEDED",
            lambda: parse_document(path, path.name, ImportLimits(
                **base, max_docx_ratio=ratio - 1
            )),
        )

    def test_docx_extracted_limit_accepts_equal_and_rejects_one_byte_more(self):
        path = self.root / "extracted.docx"
        write_docx(path)
        parsed = parse_document(path, path.name)
        extracted = parsed.stats["extracted_bytes"]
        self.assertTrue(parse_document(path, path.name, ImportLimits(
            max_file_bytes=path.stat().st_size, max_extracted_bytes=extracted
        )).units)
        self.assert_parse_error(
            "DOCUMENT_LIMIT_EXCEEDED",
            lambda: parse_document(path, path.name, ImportLimits(
                max_file_bytes=path.stat().st_size, max_extracted_bytes=extracted - 1
            )),
        )


class ImportWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def assert_worker_error(self, code, action):
        with self.assertRaises(DocumentParseError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_worker_round_trips_a_parsed_document(self):
        path = self.root / "notes.txt"
        path.write_text("第一行\nsecond", encoding="utf-8")
        expected = parse_document(path, "notes.txt")
        actual = parse_document_in_worker(path, "notes.txt")
        self.assertEqual(actual, expected)

    def test_worker_round_trips_an_empty_docx(self):
        from docx import Document

        path = self.root / "empty.docx"
        Document().save(path)
        expected = parse_document(path, "empty.docx")
        self.assertEqual(expected.units, ())
        self.assertEqual(parse_document_in_worker(path, "empty.docx"), expected)

    def test_darwin_address_space_limit_adds_budget_to_current_vsz(self):
        calls = []

        class Resource:
            RLIMIT_CPU = 1
            RLIMIT_AS = 2

            @staticmethod
            def setrlimit(kind, value):
                calls.append((kind, value))

        current_vsz_kib = 445_644_800
        completed = subprocess.CompletedProcess(
            [], 0, stdout=f" {current_vsz_kib}\n".encode("ascii"), stderr=b""
        )
        with patch.dict(sys.modules, {"resource": Resource}), \
                patch.object(import_worker.sys, "platform", "darwin"), \
                patch.object(import_worker.subprocess, "run", return_value=completed) as run:
            import_worker._install_resource_limits()
        target = current_vsz_kib * 1024 + import_worker.WORKER_ADDRESS_SPACE_BYTES
        self.assertEqual(calls, [
            (Resource.RLIMIT_CPU, (15, 15)),
            (Resource.RLIMIT_AS, (target, target)),
        ])
        run.assert_called_once_with(
            ["/bin/ps", "-o", "vsz=", "-p", str(os.getpid())],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            timeout=1,
            check=True,
        )

    def test_darwin_vsz_probe_failure_is_parser_unavailable(self):
        class Resource:
            RLIMIT_CPU = 1
            RLIMIT_AS = 2

            @staticmethod
            def setrlimit(_kind, _value):
                pass

        invalid_results = (
            subprocess.CompletedProcess([], 0, stdout=b"not-a-number\n", stderr=b""),
            subprocess.TimeoutExpired("/bin/ps", 1),
        )
        for result in invalid_results:
            with self.subTest(result=type(result).__name__), \
                    patch.dict(sys.modules, {"resource": Resource}), \
                    patch.object(import_worker.sys, "platform", "darwin"), \
                    patch.object(import_worker.subprocess, "run",
                                 side_effect=result if isinstance(result, Exception) else None,
                                 return_value=None if isinstance(result, Exception) else result):
                self.assert_worker_error(
                    "DOCUMENT_PARSER_UNAVAILABLE", import_worker._install_resource_limits
                )

    def test_non_darwin_address_space_limit_is_absolute(self):
        calls = []

        class Resource:
            RLIMIT_CPU = 1
            RLIMIT_AS = 2

            @staticmethod
            def setrlimit(kind, value):
                calls.append((kind, value))

        with patch.dict(sys.modules, {"resource": Resource}), \
                patch.object(import_worker.sys, "platform", "linux"), \
                patch.object(import_worker.subprocess, "run") as run:
            import_worker._install_resource_limits()
        self.assertEqual(calls, [
            (Resource.RLIMIT_CPU, (15, 15)),
            (Resource.RLIMIT_AS, (
                import_worker.WORKER_ADDRESS_SPACE_BYTES,
                import_worker.WORKER_ADDRESS_SPACE_BYTES,
            )),
        ])
        run.assert_not_called()

    def test_worker_child_rejects_unknown_request_fields_and_oversized_input(self):
        command = [sys.executable, "-m", "local_agent.import_worker"]
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")
            if key in os.environ
        }
        invalid = json.dumps({
            "source_path": "/tmp/nope.txt", "logical_path": "nope.txt", "limits": {},
            "extra": True,
        }).encode()
        run = subprocess.run(
            command, input=invalid, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=Path(__file__).parents[1], env=environment, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {
            "ok": False, "error": {"code": "DOCUMENT_CORRUPT"},
        })
        oversized = b" " * (MAX_WORKER_INPUT_BYTES + 1)
        run = subprocess.run(
            command, input=oversized, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=Path(__file__).parents[1], env=environment, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["error"]["code"], "DOCUMENT_LIMIT_EXCEEDED")

    def test_worker_stops_growing_stdout_before_process_completion(self):
        path = self.root / "notes.txt"
        path.write_text("text", encoding="utf-8")

        class FakeProcess:
            pid = 2468
            returncode = None

            def __init__(self):
                self.stdout_target = None
                self.killed = False
                self.waited = False
                self.returned_oversized_buffer = False

            def communicate(self, input=None, timeout=None):
                if hasattr(self.stdout_target, "write"):
                    if self.killed:
                        self.returncode = -signal.SIGKILL
                        return None, None
                    self.stdout_target.write(b"x" * 17)
                    self.stdout_target.flush()
                    raise subprocess.TimeoutExpired("worker", timeout)
                self.returned_oversized_buffer = True
                self.returncode = 0
                return b"x" * 33, None

            def wait(self, timeout=None):
                self.waited = True
                self.returncode = -signal.SIGKILL
                return self.returncode

        process = FakeProcess()

        def fake_popen(*args, **kwargs):
            process.stdout_target = kwargs["stdout"]
            return process

        def killed(_group, _signal):
            process.killed = True

        with patch.object(import_worker, "MAX_WORKER_OUTPUT_BYTES", 32), \
                patch("local_agent.import_worker.subprocess.Popen", side_effect=fake_popen), \
                patch("local_agent.import_worker.os.getpgid", return_value=process.pid), \
                patch("local_agent.import_worker.os.killpg", side_effect=killed) as killpg:
            self.assert_worker_error(
                "DOCUMENT_LIMIT_EXCEEDED",
                lambda: parse_document_in_worker(path, "notes.txt"),
            )
        self.assertFalse(process.returned_oversized_buffer)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertTrue(process.waited)

    def test_worker_cancellation_kills_process_group_and_waits(self):
        path = self.root / "notes.txt"
        path.write_text("text", encoding="utf-8")

        class FakeProcess:
            pid = 1234
            returncode = None

            def __init__(self):
                self.communications = 0
                self.waited = False

            def communicate(self, input=None, timeout=None):
                self.communications += 1
                raise subprocess.TimeoutExpired("worker", timeout)

            def wait(self, timeout=None):
                self.waited = True
                self.returncode = -signal.SIGKILL
                return self.returncode

        process = FakeProcess()
        checks = iter((False, False, True))
        with patch("local_agent.import_worker.subprocess.Popen", return_value=process), \
                patch("local_agent.import_worker.os.getpgid", return_value=1234), \
                patch("local_agent.import_worker.os.killpg") as killpg:
            self.assert_worker_error(
                "DOCUMENT_PARSER_CANCELLED",
                lambda: parse_document_in_worker(
                    path, "notes.txt", cancelled=lambda: next(checks), timeout_seconds=20
                ),
            )
        killpg.assert_called_once_with(1234, signal.SIGKILL)
        self.assertTrue(process.waited)

    def test_worker_cleanup_defers_an_unbounded_wait_to_a_daemon_reaper(self):
        class FakeProcess:
            pid = 1357
            returncode = None

            def __init__(self):
                self.wait_timeouts = []
                self.reaper_daemon = None
                self.reaped = threading.Event()

            def communicate(self, input=None, timeout=None):
                if self.returncode is not None:
                    return None, None
                raise subprocess.TimeoutExpired("worker", timeout)

            def wait(self, timeout=None):
                if self.returncode is not None:
                    return self.returncode
                self.wait_timeouts.append(timeout)
                if timeout is None:
                    if threading.current_thread() is threading.main_thread():
                        raise AssertionError("cleanup used an unbounded wait on the caller")
                    self.reaper_daemon = threading.current_thread().daemon
                    self.returncode = -signal.SIGKILL
                    self.reaped.set()
                    return self.returncode
                raise subprocess.TimeoutExpired("worker", timeout)

        process = FakeProcess()
        with patch("local_agent.import_worker.os.getpgid", return_value=process.pid), \
                patch("local_agent.import_worker.os.killpg") as killpg:
            import_worker._kill_and_wait(process)
            self.assertTrue(process.reaped.wait(1))
            import_worker._kill_and_wait(process)
        self.assertEqual(process.wait_timeouts[0], 5)
        self.assertTrue(process.reaper_daemon)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)

    def test_worker_timeout_has_stable_error_and_reaps_process(self):
        path = self.root / "notes.txt"
        path.write_text("text", encoding="utf-8")
        self.assert_worker_error(
            "DOCUMENT_PARSER_TIMEOUT",
            lambda: parse_document_in_worker(path, "notes.txt", timeout_seconds=0),
        )

    def test_worker_uses_minimal_environment_and_validates_full_output_schema(self):
        path = self.root / "notes.txt"
        path.write_text("text", encoding="utf-8")
        valid = json.dumps({
            "ok": True,
            "document": {
                "logical_path": "notes.txt",
                "units": [{
                    "text": "text",
                    "location": {"kind": "text_lines", "start": 1, "end": 1},
                }],
                "warnings": [],
                "stats": {
                    "format": "txt", "source_bytes": 4, "extracted_bytes": 4,
                    "unit_count": 1,
                },
            },
        }).encode()

        class FakeProcess:
            pid = 4321
            returncode = 0

            def __init__(self, output=valid):
                self.output = output
                self.stdout_target = None

            def communicate(self, input=None, timeout=None):
                self.stdout_target.write(self.output)
                self.stdout_target.flush()
                return None, None

        captured = {}

        def fake_popen(*args, **kwargs):
            captured.update(kwargs)
            process = FakeProcess()
            process.stdout_target = kwargs["stdout"]
            return process

        with patch.dict(os.environ, {"PRIVATE_IMPORT_SECRET": "must-not-pass"}), \
                patch("local_agent.import_worker.subprocess.Popen", side_effect=fake_popen):
            parsed = parse_document_in_worker(path, "notes.txt")
        self.assertEqual(parsed.units[0].text, "text")
        self.assertTrue(captured["start_new_session"])
        self.assertIs(captured["stderr"], subprocess.DEVNULL)
        self.assertNotIn("PRIVATE_IMPORT_SECRET", captured["env"])
        self.assertLessEqual(set(captured["env"]), {
            "PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "PYTHONIOENCODING",
            "PYTHONHASHSEED",
        })

        malformed = valid[:-1] + b', "extra": true}'

        def malformed_popen(*args, **kwargs):
            process = FakeProcess(malformed)
            process.stdout_target = kwargs["stdout"]
            return process

        with patch("local_agent.import_worker.subprocess.Popen", side_effect=malformed_popen):
            self.assert_worker_error(
                "DOCUMENT_CORRUPT", lambda: parse_document_in_worker(path, "notes.txt")
            )

    def test_worker_rejects_nonzero_exit_and_oversized_output(self):
        path = self.root / "notes.txt"
        path.write_text("text", encoding="utf-8")

        class FakeProcess:
            pid = 9876

            def __init__(self, returncode, output):
                self.returncode = returncode
                self.output = output
                self.stdout_target = None

            def communicate(self, input=None, timeout=None):
                self.stdout_target.write(self.output)
                self.stdout_target.flush()
                return None, None

        def completed_process(returncode, output):
            process = FakeProcess(returncode, output)

            def popen(*args, **kwargs):
                process.stdout_target = kwargs["stdout"]
                return process

            return popen

        with patch("local_agent.import_worker.subprocess.Popen",
                   side_effect=completed_process(4, b"{}")):
            self.assert_worker_error(
                "DOCUMENT_CORRUPT", lambda: parse_document_in_worker(path, "notes.txt")
            )
        with patch.object(import_worker, "MAX_WORKER_OUTPUT_BYTES", 32), \
                patch("local_agent.import_worker.subprocess.Popen",
                      side_effect=completed_process(0, b"x" * 33)):
            self.assert_worker_error("DOCUMENT_LIMIT_EXCEEDED",
                                     lambda: parse_document_in_worker(path, "notes.txt"))


if __name__ == "__main__":
    unittest.main()
