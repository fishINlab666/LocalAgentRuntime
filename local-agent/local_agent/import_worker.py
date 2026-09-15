"""Bounded subprocess transport for local document parsing."""

from dataclasses import asdict, fields
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .import_parsers import (
    DocumentParseError,
    ImportLimits,
    ParsedDocument,
    ParsedUnit,
    parse_document,
)


MAX_WORKER_INPUT_BYTES = 128 * 1024
MAX_WORKER_OUTPUT_BYTES = 8 * 1024 * 1024
WORKER_CPU_SECONDS = 15
WORKER_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024

_ERROR_CODES = frozenset({
    "IMPORT_FORMAT_UNSUPPORTED",
    "TEXT_INVALID_UTF8",
    "TEXT_INVALID_CHARACTER",
    "PDF_TEXT_NOT_FOUND",
    "PDF_ENCRYPTED",
    "DOCUMENT_CORRUPT",
    "DOCUMENT_LIMIT_EXCEEDED",
    "DOCUMENT_PARSER_UNAVAILABLE",
    "DOCUMENT_PARSER_CANCELLED",
    "DOCUMENT_PARSER_TIMEOUT",
})
_LIMIT_FIELDS = tuple(field.name for field in fields(ImportLimits))


def _fail(code):
    raise DocumentParseError(code)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _load_json(raw):
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        _fail("DOCUMENT_CORRUPT")


def _strict_int(value, *, minimum=0):
    return type(value) is int and value >= minimum


def _validate_limits(value):
    if not isinstance(value, dict) or set(value) != set(_LIMIT_FIELDS):
        _fail("DOCUMENT_CORRUPT")
    for name in _LIMIT_FIELDS:
        item = value[name]
        if name == "max_docx_ratio":
            if (isinstance(item, bool) or not isinstance(item, (int, float))
                    or not math.isfinite(item) or item < 0):
                _fail("DOCUMENT_CORRUPT")
        elif not _strict_int(item):
            _fail("DOCUMENT_CORRUPT")
    try:
        return ImportLimits(**value)
    except (TypeError, ValueError):
        _fail("DOCUMENT_CORRUPT")


def _decode_request(raw):
    value = _load_json(raw)
    path_fields = {"source_path", "logical_path", "limits"}
    fd_fields = {"source_fd", "source_identity", "source_size", "logical_path", "limits"}
    if not isinstance(value, dict) or set(value) not in (path_fields, fd_fields):
        _fail("DOCUMENT_CORRUPT")
    if not isinstance(value["logical_path"], str) or "\x00" in value["logical_path"]:
        _fail("DOCUMENT_CORRUPT")
    limits = _validate_limits(value["limits"])
    if set(value) == fd_fields:
        details = _checked_source_fd(value["source_fd"], value["source_identity"], limits)
        if (not _strict_int(value["source_size"])
                or details.st_size != value["source_size"]):
            _fail("DOCUMENT_CORRUPT")
        os.lseek(value["source_fd"], 0, os.SEEK_SET)
        # /dev/fd duplicates the inherited object on Darwin and Linux. Never
        # resolve this path back into a name that can be replaced by a caller.
        return Path(f"/dev/fd/{value['source_fd']}"), value["logical_path"], limits
    if not isinstance(value["source_path"], str) or "\x00" in value["source_path"]:
        _fail("DOCUMENT_CORRUPT")
    return Path(value["source_path"]), value["logical_path"], limits


def _checked_source_fd(descriptor, identity, limits):
    if (not _strict_int(descriptor, minimum=3) or not isinstance(identity, (list, tuple))
            or len(identity) != 2 or not all(_strict_int(value) for value in identity)):
        _fail("DOCUMENT_CORRUPT")
    try:
        details = os.fstat(descriptor)
        access = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
    except (OSError, ValueError, OverflowError):
        _fail("DOCUMENT_CORRUPT")
    if (access != os.O_RDONLY or not stat.S_ISREG(details.st_mode)
            or (details.st_dev, details.st_ino) != tuple(identity)):
        _fail("DOCUMENT_CORRUPT")
    if details.st_size > limits.max_file_bytes:
        _fail("DOCUMENT_LIMIT_EXCEEDED")
    return details


def _document_payload(document):
    return {
        "logical_path": document.logical_path,
        "units": [asdict(unit) for unit in document.units],
        "warnings": list(document.warnings),
        "stats": document.stats,
    }


def _error_payload(code):
    return {"ok": False, "error": {"code": code}}


def _encode_payload(payload):
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return json.dumps(_error_payload("DOCUMENT_CORRUPT"), separators=(",", ":")).encode()


def _write_payload(payload):
    encoded = _encode_payload(payload)
    if len(encoded) > MAX_WORKER_OUTPUT_BYTES:
        encoded = _encode_payload(_error_payload("DOCUMENT_LIMIT_EXCEEDED"))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _darwin_address_space_limit():
    """Allow 512 MiB beyond Darwin's very large shared-cache mappings."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "vsz=", "-p", str(os.getpid())],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            timeout=1,
            check=True,
        )
        rendered = result.stdout.decode("ascii", errors="strict").strip()
        if not rendered or not rendered.isdigit():
            _fail("DOCUMENT_PARSER_UNAVAILABLE")
        current_vsz_bytes = int(rendered) * 1024
        if current_vsz_bytes <= 0:
            _fail("DOCUMENT_PARSER_UNAVAILABLE")
        return current_vsz_bytes + WORKER_ADDRESS_SPACE_BYTES
    except DocumentParseError:
        raise
    except (OSError, ValueError, UnicodeDecodeError, subprocess.SubprocessError):
        _fail("DOCUMENT_PARSER_UNAVAILABLE")


def _install_resource_limits():
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (WORKER_CPU_SECONDS, WORKER_CPU_SECONDS))
        address_space_limit = (
            _darwin_address_space_limit()
            if sys.platform == "darwin"
            else WORKER_ADDRESS_SPACE_BYTES
        )
        resource.setrlimit(
            resource.RLIMIT_AS,
            (address_space_limit, address_space_limit),
        )
    except DocumentParseError:
        raise
    except (ImportError, AttributeError, OSError, ValueError):
        _fail("DOCUMENT_PARSER_UNAVAILABLE")


def _valid_location(value):
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        return False
    kind = value["kind"]
    if kind == "text_lines":
        return (set(value) == {"kind", "start", "end"}
                and _strict_int(value["start"], minimum=1)
                and value["end"] == value["start"])
    if kind == "pdf_page":
        return set(value) == {"kind", "page"} and _strict_int(value["page"], minimum=1)
    if kind == "docx_paragraph":
        return (set(value) == {"kind", "paragraph"}
                and _strict_int(value["paragraph"], minimum=1))
    if kind == "docx_table_row":
        return (set(value) == {"kind", "table", "row"}
                and _strict_int(value["table"], minimum=1)
                and _strict_int(value["row"], minimum=1))
    return False


def _valid_stats(value, kind):
    if not isinstance(value, dict):
        return False
    common = {"format", "source_bytes", "extracted_bytes", "unit_count"}
    expected = set(common)
    if kind == "pdf":
        expected.update({"page_count", "pdf_content_bytes"})
    elif kind == "docx":
        expected.update({"docx_entries", "docx_uncompressed_bytes"})
    if set(value) != expected or value.get("format") != kind:
        return False
    return all(_strict_int(value[name]) for name in expected - {"format"})


def _decode_document(value, logical_path):
    if not isinstance(value, dict) or set(value) != {
            "logical_path", "units", "warnings", "stats"}:
        _fail("DOCUMENT_CORRUPT")
    if value["logical_path"] != logical_path:
        _fail("DOCUMENT_CORRUPT")
    kind = value["stats"].get("format") if isinstance(value["stats"], dict) else None
    if kind not in {"txt", "md", "pdf", "docx"} or not _valid_stats(value["stats"], kind):
        _fail("DOCUMENT_CORRUPT")
    units_raw = value["units"]
    warnings_raw = value["warnings"]
    if not isinstance(units_raw, list) or not isinstance(warnings_raw, list):
        _fail("DOCUMENT_CORRUPT")
    units = []
    for unit in units_raw:
        if (not isinstance(unit, dict) or set(unit) != {"text", "location"}
                or not isinstance(unit["text"], str) or not _valid_location(unit["location"])):
            _fail("DOCUMENT_CORRUPT")
        units.append(ParsedUnit(unit["text"], unit["location"]))
    warnings = []
    for warning in warnings_raw:
        if (not isinstance(warning, dict)
                or set(warning) != {"code", "page"}
                or warning.get("code") != "PDF_PAGE_TEXT_NOT_FOUND"
                or not _strict_int(warning.get("page"), minimum=1)):
            _fail("DOCUMENT_CORRUPT")
        warnings.append(warning)
    allowed_kinds = {
        "txt": {"text_lines"},
        "md": {"text_lines"},
        "pdf": {"pdf_page"},
        "docx": {"docx_paragraph", "docx_table_row"},
    }
    if any(unit.location["kind"] not in allowed_kinds[kind] for unit in units):
        _fail("DOCUMENT_CORRUPT")
    if kind == "pdf" and not units:
        _fail("DOCUMENT_CORRUPT")
    if kind != "pdf" and warnings:
        _fail("DOCUMENT_CORRUPT")
    if value["stats"]["unit_count"] != len(units):
        _fail("DOCUMENT_CORRUPT")
    return ParsedDocument(logical_path, tuple(units), tuple(warnings), value["stats"])


def _decode_response(raw, logical_path):
    value = _load_json(raw)
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        _fail("DOCUMENT_CORRUPT")
    if value["ok"] is False:
        if set(value) != {"ok", "error"} or not isinstance(value["error"], dict):
            _fail("DOCUMENT_CORRUPT")
        if set(value["error"]) != {"code"} or value["error"]["code"] not in _ERROR_CODES:
            _fail("DOCUMENT_CORRUPT")
        _fail(value["error"]["code"])
    if set(value) != {"ok", "document"}:
        _fail("DOCUMENT_CORRUPT")
    return _decode_document(value["document"], logical_path)


def _minimal_environment():
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")
        if key in os.environ
    }
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0"})
    return environment


def _reap_process(process):
    try:
        process.wait()
    except (OSError, ChildProcessError):
        pass


def _kill_and_wait(process):
    if process.returncode is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except (AttributeError, OSError):
                pass
    try:
        process.communicate(timeout=1)
    except (AttributeError, OSError, subprocess.TimeoutExpired, ValueError):
        pass
    for stream_name in ("stdin", "stdout"):
        stream = getattr(process, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        threading.Thread(
            target=_reap_process,
            args=(process,),
            daemon=True,
            name="document-parser-reaper",
        ).start()
    except (OSError, ChildProcessError):
        pass


def parse_document_in_worker(
        source: Path,
        logical_path: str,
        limits: ImportLimits = ImportLimits(),
        *,
        timeout_seconds=20,
        cancelled=None,
        source_fd=None,
        source_identity=None,
):
    """Parse in a resource-limited child and return a validated document."""
    if not isinstance(source, Path) or not isinstance(logical_path, str):
        _fail("DOCUMENT_CORRUPT")
    if not isinstance(limits, ImportLimits):
        _fail("DOCUMENT_CORRUPT")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds < 0):
        _fail("DOCUMENT_CORRUPT")
    if cancelled is not None and not callable(cancelled):
        _fail("DOCUMENT_CORRUPT")
    if cancelled is not None and cancelled():
        _fail("DOCUMENT_PARSER_CANCELLED")
    payload = {"logical_path": logical_path, "limits": asdict(limits)}
    if source_fd is None:
        if source_identity is not None:
            _fail("DOCUMENT_CORRUPT")
        payload["source_path"] = str(source.resolve())
        inherited = ()
    else:
        details = _checked_source_fd(source_fd, source_identity, limits)
        payload.update({"source_fd": source_fd, "source_identity": list(source_identity),
                        "source_size": details.st_size})
        inherited = (source_fd,)
    request = _encode_payload(payload)
    if len(request) > MAX_WORKER_INPUT_BYTES:
        _fail("DOCUMENT_LIMIT_EXCEEDED")
    with tempfile.TemporaryFile() as stdout_file:
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "local_agent.import_worker"],
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=subprocess.DEVNULL,
                cwd=Path(__file__).resolve().parents[1],
                env=_minimal_environment(),
                start_new_session=True,
                pass_fds=inherited,
            )
        except (OSError, ValueError):
            _fail("DOCUMENT_PARSER_UNAVAILABLE")
        deadline = time.monotonic() + timeout_seconds
        pending_input = request
        try:
            while True:
                if cancelled is not None and cancelled():
                    _kill_and_wait(process)
                    _fail("DOCUMENT_PARSER_CANCELLED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _kill_and_wait(process)
                    _fail("DOCUMENT_PARSER_TIMEOUT")
                try:
                    process.communicate(
                        input=pending_input,
                        timeout=min(0.05, remaining),
                    )
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
                    if os.fstat(stdout_file.fileno()).st_size > MAX_WORKER_OUTPUT_BYTES:
                        _kill_and_wait(process)
                        _fail("DOCUMENT_LIMIT_EXCEEDED")
        except DocumentParseError:
            raise
        except Exception:
            _kill_and_wait(process)
            _fail("DOCUMENT_PARSER_UNAVAILABLE")
        if process.returncode != 0:
            _fail("DOCUMENT_CORRUPT")
        stdout_file.seek(0)
        stdout = stdout_file.read(MAX_WORKER_OUTPUT_BYTES + 1)
    if len(stdout) > MAX_WORKER_OUTPUT_BYTES:
        _fail("DOCUMENT_LIMIT_EXCEEDED")
    return _decode_response(stdout, logical_path)


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > MAX_WORKER_INPUT_BYTES:
            _fail("DOCUMENT_LIMIT_EXCEEDED")
        source, logical_path, limits = _decode_request(raw)
        _install_resource_limits()
        document = parse_document(source, logical_path, limits)
        payload = {"ok": True, "document": _document_payload(document)}
    except DocumentParseError as error:
        payload = _error_payload(error.code)
    except BaseException:
        payload = _error_payload("DOCUMENT_CORRUPT")
    _write_payload(payload)


if __name__ == "__main__":
    main()
