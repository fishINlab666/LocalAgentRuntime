import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_agent.demo import DemoProvider
from local_agent.imports import ImportStoreError
from local_agent.sessions import SessionError
from local_agent.web import create_server


_DEFAULT = object()


class ImportWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        self.provider_calls = 0

        def provider():
            self.provider_calls += 1
            return DemoProvider()

        self.server = create_server(
            self.workspace,
            self.root / "runs",
            state_dir=self.state,
            port=0,
            provider_factory=provider,
        )
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01),
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        server = getattr(self, "server", None)
        if server is None:
            return
        self.server = None
        server.shutdown()
        server.server_close()
        self.thread.join(2)

    def raw_request(
        self,
        method,
        path,
        body=None,
        *,
        content_type=None,
        content_length=_DEFAULT,
        transfer_encoding=None,
        host=_DEFAULT,
        origin=_DEFAULT,
        token=_DEFAULT,
        agent_id=_DEFAULT,
    ):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.putrequest(
            method, path, skip_host=True, skip_accept_encoding=True
        )
        host = (
            self.server.origin.removeprefix("http://")
            if host is _DEFAULT
            else host
        )
        if host is not None:
            connection.putheader("Host", host)
        if content_type is not None:
            connection.putheader("Content-Type", content_type)
        if content_length is _DEFAULT:
            if method == "POST":
                connection.putheader("Content-Length", str(len(body or b"")))
        elif content_length is not None:
            values = (
                content_length
                if isinstance(content_length, (tuple, list))
                else (content_length,)
            )
            for value in values:
                connection.putheader("Content-Length", str(value))
        if transfer_encoding is not None:
            connection.putheader("Transfer-Encoding", transfer_encoding)
        origin = self.server.origin if origin is _DEFAULT and method == "POST" else origin
        if origin is not _DEFAULT and origin is not None:
            connection.putheader("Origin", origin)
        token = self.server.token if token is _DEFAULT else token
        if token is not None:
            connection.putheader("X-Session-Token", token)
        agent_id = "directory-qa" if agent_id is _DEFAULT else agent_id
        if agent_id is not None:
            connection.putheader("X-Agent-ID", agent_id)
        connection.putheader("Connection", "close")
        connection.endheaders(body)
        response = connection.getresponse()
        raw = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, headers, raw

    def request(self, method, path, data=None, **options):
        body = None
        if data is not None:
            body = json.dumps(
                data, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        status, headers, raw = self.raw_request(
            method,
            path,
            body,
            content_type="application/json" if data is not None else None,
            **options,
        )
        return status, headers, json.loads(raw) if raw else None

    @staticmethod
    def metadata(content, *, logical_path="资料/notes.txt", name="导入资料"):
        return {
            "kind": "file",
            "name": name,
            "agent_id": "directory-qa",
            "files": [{"logical_path": logical_path, "bytes": len(content)}],
            "ignored": [],
        }

    def begin(self, content="项目：青禾-47\n".encode("utf-8"), **changes):
        metadata = self.metadata(content)
        metadata.update(changes)
        status, _, value = self.request("POST", "/api/imports", metadata)
        self.assertEqual(status, 201, value)
        return value

    def upload(self, batch, content):
        slot_id = batch["files"][0]["slot_id"]
        return self.raw_request(
            "POST",
            f"/api/imports/{batch['id']}/files/{slot_id}",
            content,
            content_type="application/octet-stream",
        )

    def begin_and_upload(self, content="项目：青禾-47\n".encode("utf-8")):
        batch = self.begin(content)
        status, _, raw = self.upload(batch, content)
        self.assertEqual(status, 200, raw)
        return batch

    def wait_import(self, import_id, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, _, snapshot = self.request(
                "GET", f"/api/imports/{import_id}"
            )
            self.assertEqual(status, 200, snapshot)
            if snapshot["status"] not in {"uploading", "finalizing"}:
                return snapshot
            time.sleep(0.01)
        self.fail("import did not reach a terminal state")

    @staticmethod
    def encoded(value):
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def sized_import_metadata(self, target_bytes):
        data = self.metadata(b"x")
        data["ignored"] = [{"logical_path": "ignored-.bin", "bytes": 0}]
        padding = target_bytes - len(self.encoded(data))
        self.assertGreaterEqual(padding, 0)
        data["ignored"][0]["logical_path"] = f"ignored-{'x' * padding}.bin"
        raw = self.encoded(data)
        self.assertEqual(len(raw), target_bytes)
        return raw

    def test_begin_upload_complete_poll_ready_and_get_snapshot(self):
        content = "项目：青禾-47\n评审人：林澄\n".encode("utf-8")
        batch = self.begin(content)
        self.assertEqual(batch["status"], "uploading")
        self.assertEqual(len(batch["files"]), 1)

        status, _, snapshot = self.request("GET", f"/api/imports/{batch['id']}")
        self.assertEqual(status, 200, snapshot)
        self.assertEqual(snapshot["stored_files"], 0)
        status, _, raw = self.upload(batch, content)
        self.assertEqual(status, 200, raw)

        status, _, completing = self.request(
            "POST", f"/api/imports/{batch['id']}/complete", {}
        )
        self.assertEqual(status, 202, completing)
        ready = self.wait_import(batch["id"])
        self.assertEqual(ready["status"], "ready", ready)
        self.assertIsInstance(ready["session_id"], str)
        self.assertTrue(ready["session_id"])

        status, _, session = self.request(
            "GET", f"/api/sessions/{ready['session_id']}"
        )
        self.assertEqual(status, 200, session)
        self.assertEqual(session["session"]["id"], ready["session_id"])
        self.assertEqual(session["session"]["agent_id"], "directory-qa")
        imported = session["session"]["import"]
        self.assertEqual(set(imported), {"id", "files"})
        self.assertEqual(imported["id"], batch["id"])
        self.assertEqual(len(imported["files"]), 1)
        source = imported["files"][0]
        self.assertEqual(
            set(source),
            {"logical_path", "format", "bytes", "sha256", "stats",
             "warnings", "chunks"},
        )
        self.assertEqual(source["logical_path"], "资料/notes.txt")
        self.assertEqual(source["format"], "txt")
        self.assertEqual(source["bytes"], len(content))
        self.assertEqual(len(source["sha256"]), 64)
        serialized = json.dumps(imported, ensure_ascii=False)
        self.assertNotIn(str(self.state), serialized)
        self.assertNotIn("originals", serialized)
        self.assertNotIn("locations.json", serialized)
        self.assertNotIn("chunk-0001.md", serialized)
        self.assertEqual(self.provider_calls, 0)

        status, _, config = self.request("GET", "/api/config")
        self.assertEqual(status, 200, config)
        self.assertEqual(
            [item["extension"] for item in config["imports"]["formats"]],
            [".md", ".txt", ".pdf", ".docx"],
        )
        self.assertTrue(config["imports"]["formats"][0]["available"])
        self.assertTrue(config["imports"]["formats"][1]["available"])
        self.assertEqual(config["imports"]["limits"]["max_files"], 50)
        self.assertEqual(
            config["imports"]["limits"]["max_file_bytes"], 20 * 1024 * 1024
        )
        self.assertEqual(
            config["imports"]["limits"]["max_total_bytes"], 100 * 1024 * 1024
        )

    def test_cancel_cleans_staging_and_does_not_create_a_session(self):
        content = b"cancel this import\n"
        batch = self.begin_and_upload(content)
        staging = self.state / "imports" / ".staging" / batch["id"]
        self.assertTrue(staging.is_dir())

        status, _, cancelled = self.request(
            "POST", f"/api/imports/{batch['id']}/cancel", {}
        )
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["session_id"])
        self.assertFalse(staging.exists())

        status, _, snapshot = self.request("GET", f"/api/imports/{batch['id']}")
        self.assertEqual(status, 200, snapshot)
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertIsNone(snapshot["session_id"])
        self.assertEqual(self.provider_calls, 0)

    def test_parse_failure_does_not_create_session_or_call_model(self):
        content = b"\xff"
        batch = self.begin(
            content,
            files=[{"logical_path": "bad.txt", "bytes": len(content)}],
        )
        self.assertEqual(self.upload(batch, content)[0], 200)

        status, _, completing = self.request(
            "POST", f"/api/imports/{batch['id']}/complete", {}
        )
        self.assertEqual(status, 202, completing)
        failed = self.wait_import(batch["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "TEXT_INVALID_UTF8")
        self.assertIsNone(failed["session_id"])
        self.assertEqual(self.provider_calls, 0)

    def test_import_metadata_has_128_kib_limit_and_old_json_stays_at_16_kib(self):
        exact = self.sized_import_metadata(128 * 1024)
        status, _, batch = self.raw_request(
            "POST", "/api/imports", exact, content_type="application/json"
        )
        self.assertEqual(status, 201, batch)
        batch = json.loads(batch)
        self.assertEqual(
            self.request("POST", f"/api/imports/{batch['id']}/cancel", {})[0],
            200,
        )

        too_large = self.sized_import_metadata(128 * 1024 + 1)
        self.assertEqual(
            self.raw_request(
                "POST", "/api/imports", too_large, content_type="application/json"
            )[0],
            413,
        )

        old_body = {"padding": ""}
        padding = 16 * 1024 - len(self.encoded(old_body))
        old_body["padding"] = "x" * padding
        exact_old = self.encoded(old_body)
        self.assertEqual(len(exact_old), 16 * 1024)
        self.assertEqual(
            self.raw_request(
                "POST", "/api/sessions", exact_old, content_type="application/json"
            )[0],
            400,
        )
        self.assertEqual(
            self.raw_request(
                "POST",
                "/api/sessions",
                exact_old + b" ",
                content_type="application/json",
            )[0],
            413,
        )

    def test_binary_upload_enforces_framing_type_length_and_single_use(self):
        content = b"abc"
        batch = self.begin(content)
        path = (
            f"/api/imports/{batch['id']}/files/"
            f"{batch['files'][0]['slot_id']}"
        )

        cases = (
            ("wrong content type", {"body": content, "content_type": "text/plain"}, 415),
            ("missing length", {"content_length": None}, 400),
            (
                "duplicate length",
                {
                    "body": content,
                    "content_type": "application/octet-stream",
                    "content_length": (len(content), len(content)),
                },
                400,
            ),
            (
                "transfer encoding",
                {
                    "body": content,
                    "content_type": "application/octet-stream",
                    "content_length": len(content),
                    "transfer_encoding": "chunked",
                },
                400,
            ),
            (
                "declared slot mismatch",
                {
                    "body": content[:2],
                    "content_type": "application/octet-stream",
                },
                422,
            ),
        )
        for name, options, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    self.raw_request("POST", path, **options)[0], expected
                )

        self.assertEqual(
            self.raw_request(
                "POST", path, content, content_type="application/octet-stream"
            )[0],
            200,
        )
        self.assertEqual(
            self.raw_request(
                "POST", path, content, content_type="application/octet-stream"
            )[0],
            409,
        )

    def test_import_routes_enforce_loopback_token_and_agent_isolation(self):
        body = self.encoded(self.metadata(b"x"))
        attempts = (
            ("wrong host", {"host": "outside.example"}),
            ("missing origin", {"origin": None}),
            ("wrong origin", {"origin": "https://outside.example"}),
            ("missing token", {"token": None}),
            ("wrong token", {"token": "wrong"}),
        )
        for name, options in attempts:
            with self.subTest(name=name):
                self.assertEqual(
                    self.raw_request(
                        "POST",
                        "/api/imports",
                        body,
                        content_type="application/json",
                        **options,
                    )[0],
                    403,
                )
        for name, agent_id in (("missing agent", None), ("wrong agent", "file-qa")):
            with self.subTest(name=name):
                self.assertEqual(
                    self.raw_request(
                        "POST",
                        "/api/imports",
                        body,
                        content_type="application/json",
                        agent_id=agent_id,
                    )[0],
                    404,
                )

        batch = self.begin(b"x")
        path = f"/api/imports/{batch['id']}"
        self.assertEqual(self.request("GET", path, agent_id=None)[0], 404)
        self.assertEqual(self.request("GET", path, agent_id="file-qa")[0], 404)
        self.assertEqual(self.request("GET", path)[0], 200)
        self.assertEqual(
            self.request("POST", f"{path}/cancel", {})[0],
            200,
        )

    def test_second_finalize_is_rejected_while_first_parser_is_active(self):
        first = self.begin_and_upload(b"first import\n")
        second = self.begin_and_upload(b"second import\n")
        started = threading.Event()
        release = threading.Event()

        from local_agent import imports as imports_module

        original = imports_module.parse_document_in_worker

        def blocked_parser(*args, **kwargs):
            started.set()
            release.wait(5)
            return original(*args, **kwargs)

        with patch.object(imports_module, "parse_document_in_worker", blocked_parser):
            try:
                status, _, first_job = self.request(
                    "POST", f"/api/imports/{first['id']}/complete", {}
                )
                self.assertEqual(status, 202, first_job)
                self.assertTrue(started.wait(2), "first parser worker did not start")

                status, _, replay = self.request(
                    "POST", f"/api/imports/{first['id']}/complete", {}
                )
                self.assertEqual(status, 202, replay)
                self.assertEqual(replay["job_id"], first_job["job_id"])
                self.server.runs.agent_catalog.set_enabled("directory-qa", False)
                status, _, disabled_replay = self.request(
                    "POST", f"/api/imports/{first['id']}/complete", {}
                )
                self.assertEqual(status, 202, disabled_replay)
                self.assertEqual(disabled_replay["job_id"], first_job["job_id"])
                self.server.runs.agent_catalog.set_enabled("directory-qa", True)
                status, _, conflict = self.request(
                    "POST", f"/api/imports/{second['id']}/complete", {}
                )
                self.assertEqual(status, 409)
                self.assertEqual(conflict["error"], "IMPORT_STATE_CONFLICT")
            finally:
                release.set()
            self.assertEqual(self.wait_import(first["id"])["status"], "ready")
        self.assertEqual(self.provider_calls, 0)

    def test_ready_complete_replay_ignores_current_agent_enabled_state(self):
        batch = self.begin_and_upload(b"ready replay\n")
        self.assertEqual(
            self.request("POST", f"/api/imports/{batch['id']}/complete", {})[0],
            202,
        )
        ready = self.wait_import(batch["id"])
        self.assertEqual(ready["status"], "ready", ready)

        self.server.runs.agent_catalog.set_enabled("directory-qa", False)
        status, _, replay = self.request(
            "POST", f"/api/imports/{batch['id']}/complete", {}
        )
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["status"], "ready")
        self.assertEqual(replay["job_id"], ready["job_id"])
        self.assertEqual(replay["session_id"], ready["session_id"])
        self.assertEqual(self.provider_calls, 0)

    def test_retryable_session_link_failure_has_terminal_http_view(self):
        batch = self.begin_and_upload(b"retry session link\n")

        with patch.object(
            self.server.runs.service,
            "attach_import",
            side_effect=SessionError("AGENT_DISABLED"),
        ):
            status, _, value = self.request(
                "POST", f"/api/imports/{batch['id']}/complete", {}
            )
            self.assertEqual(status, 202, value)
            failed = self.wait_import(batch["id"])

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "AGENT_DISABLED")
        self.assertIsNone(failed["session_id"])

        status, _, retry = self.request(
            "POST", f"/api/imports/{batch['id']}/complete", {}
        )
        self.assertEqual(status, 202, retry)
        ready = self.wait_import(batch["id"])
        self.assertEqual(ready["status"], "ready", ready)
        self.assertTrue(ready["session_id"])
        self.assertEqual(self.provider_calls, 0)

    def test_worker_start_failure_releases_admission_and_can_retry(self):
        batch = self.begin_and_upload(b"retry worker start\n")

        with patch.object(
            self.server.runs,
            "_start_import_worker",
            side_effect=RuntimeError("thread unavailable"),
        ):
            status, _, failed_start = self.request(
                "POST", f"/api/imports/{batch['id']}/complete", {}
            )
        self.assertEqual(status, 503, failed_start)
        self.assertEqual(failed_start["error"], "IMPORT_STORAGE_FAILED")
        snapshot = self.request("GET", f"/api/imports/{batch['id']}")[2]
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["error_code"], "IMPORT_STORAGE_FAILED")

        status, _, retry = self.request(
            "POST", f"/api/imports/{batch['id']}/complete", {}
        )
        self.assertEqual(status, 202, retry)
        ready = self.wait_import(batch["id"])
        self.assertEqual(ready["status"], "ready", ready)
        self.assertEqual(self.provider_calls, 0)

    def test_server_close_cancels_and_joins_finalize_worker(self):
        batch = self.begin_and_upload(b"wait for close\n")
        started = threading.Event()
        cancelled = threading.Event()
        worker_done = threading.Event()
        release = threading.Event()
        close_errors = []

        def blocked_parser(*_args, **kwargs):
            try:
                started.set()
                while not kwargs["cancelled"]():
                    if release.wait(0.01):
                        break
                if kwargs["cancelled"]():
                    cancelled.set()
                raise ImportStoreError("IMPORT_CANCELLED")
            finally:
                worker_done.set()

        def close():
            try:
                self.close_server()
            except BaseException as error:
                close_errors.append(error)

        from local_agent import imports as imports_module

        with patch.object(imports_module, "parse_document_in_worker", blocked_parser):
            status, _, value = self.request(
                "POST", f"/api/imports/{batch['id']}/complete", {}
            )
            self.assertEqual(status, 202, value)
            self.assertTrue(started.wait(2), "parser worker did not start")

            closer = threading.Thread(target=close, daemon=True)
            closer.start()
            closer.join(2)
            needed_fallback = closer.is_alive()
            release.set()
            closer.join(2)

        self.assertFalse(needed_fallback, "server close waited on an uncancelled worker")
        self.assertFalse(closer.is_alive(), "server close did not join the worker")
        self.assertEqual(close_errors, [])
        self.assertTrue(cancelled.is_set(), "server close did not cancel the parser")
        self.assertTrue(worker_done.is_set(), "server close returned before worker exit")
        self.assertEqual(self.provider_calls, 0)


if __name__ == "__main__":
    unittest.main()
