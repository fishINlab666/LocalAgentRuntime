import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from local_agent.demo import DemoProvider
from local_agent.provider import ModelReply
from local_agent.web import create_server


class SessionWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = self.root / "state"
        (self.workspace / "note.md").write_text(
            "项目代号：青禾-47\n评审人：林澄\n", encoding="utf-8"
        )
        self.providers = []

        def provider():
            value = DemoProvider()
            self.providers.append(value)
            return value

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

    def request(self, method, path, data=None, *, auth=True, origin=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        body = (
            json.dumps(data, ensure_ascii=False).encode("utf-8")
            if data is not None
            else None
        )
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["X-Session-Token"] = self.server.token
        if method == "POST":
            headers["Origin"] = origin or self.server.origin
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        raw = response.read()
        value = json.loads(raw) if raw else None
        connection.close()
        return response.status, value

    def restart_for_session(self, session_id, provider_factory=DemoProvider):
        self.close_server()
        self.server = create_server(
            None,
            self.root / "restart-runs",
            state_dir=self.state,
            session_id=session_id,
            port=0,
            provider_factory=provider_factory,
        )
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()

    def create_session(self, title="资料核对", scope=None):
        status, value = self.request(
            "POST",
            "/api/sessions",
            {"title": title, "scope": scope or {"mode": "file", "file": "note.md"}},
        )
        self.assertEqual(status, 201, value)
        return value["session"]["id"]

    def submit(self, session_id, **changes):
        body = {
            "client_request_id": "request-1",
            "task_type": "files",
            "question": "项目代号是什么？请引用原文。",
            "output_file": None,
        }
        body.update(changes)
        return self.request("POST", f"/api/sessions/{session_id}/runs", body)

    def wait_run(self, session_id, run_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, value = self.request(
                "GET", f"/api/sessions/{session_id}/runs/{run_id}"
            )
            self.assertEqual(status, 200, value)
            if value["run"]["state"] not in {
                "queued", "running", "waiting_approval"
            }:
                return value["run"]
            time.sleep(0.01)
        self.fail("session run did not finish")

    def test_session_crud_uses_fixed_server_workspace_and_scope(self):
        session_id = self.create_session()
        status, listing = self.request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in listing["sessions"]], [session_id])
        self.assertEqual(listing["sessions"][0]["scope"], {
            "mode": "file", "file": "note.md"
        })

        self.assertEqual(
            self.request(
                "POST", "/api/sessions", {
                    "title": "越权", "workspace": "/tmp", "scope": {"mode": "directory"}
                }
            ),
            (400, {"error": "INVALID_REQUEST"}),
        )
        status, renamed = self.request(
            "POST", f"/api/sessions/{session_id}/rename", {"title": "新标题"}
        )
        self.assertEqual((status, renamed["session"]["title"]), (200, "新标题"))
        self.assertEqual(
            self.request("POST", f"/api/sessions/{session_id}/archive", {})[1]["session"]["status"],
            "archived",
        )
        self.assertEqual(
            self.request("POST", f"/api/sessions/{session_id}/restore", {})[1]["session"]["status"],
            "active",
        )

    def test_run_is_durable_idempotent_and_conflicting_replay_is_409(self):
        session_id = self.create_session()
        status, started = self.submit(session_id)
        self.assertEqual(status, 202, started)
        run_id = started["run"]["id"]
        completed = self.wait_run(session_id, run_id)
        self.assertEqual(completed["state"], "completed")
        self.assertIn("青禾-47", completed["result"]["answer"]["answer"])
        provider_count = len(self.providers)

        status, replay = self.submit(session_id)
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["run"]["id"], run_id)
        self.assertTrue(replay["run"]["idempotent_replay"])
        self.assertEqual(len(self.providers), provider_count)

        status, conflict = self.submit(session_id, question="改成另一个问题")
        self.assertEqual((status, conflict), (409, {"error": "SESSION_REQUEST_CONFLICT"}))
        status, history = self.request("GET", f"/api/sessions/{session_id}/runs")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in history["runs"]], [run_id])
        self.assertEqual(history["runs"][0]["question"], "项目代号是什么？请引用原文。")

    def test_cross_session_run_and_controls_are_all_not_found(self):
        session_a = self.create_session("A")
        session_b = self.create_session("B")
        status, started = self.submit(session_a)
        self.assertEqual(status, 202)
        run_id = started["run"]["id"]
        self.wait_run(session_a, run_id)

        self.assertEqual(
            self.request("GET", f"/api/sessions/{session_b}/runs/{run_id}"),
            (404, {"error": "NOT_FOUND"}),
        )
        self.assertEqual(
            self.request("POST", f"/api/sessions/{session_b}/runs/{run_id}/cancel", {}),
            (404, {"error": "NOT_FOUND"}),
        )
        self.assertEqual(
            self.request(
                "POST",
                f"/api/sessions/{session_b}/runs/{run_id}/approvals/fake",
                {"decision": "allow"},
            ),
            (404, {"error": "NOT_FOUND"}),
        )

    def test_new_routes_reuse_origin_token_and_body_contract(self):
        body = {"title": "A", "scope": {"mode": "directory"}}
        self.assertEqual(self.request("POST", "/api/sessions", body, auth=False)[0], 403)
        self.assertEqual(
            self.request("POST", "/api/sessions", body, origin="https://outside.example")[0],
            403,
        )
        session_id = self.create_session()
        for invalid in (
            {},
            {"client_request_id": "r", "task_type": "files", "question": "q"},
            {"client_request_id": "r", "task_type": "other", "question": "q", "output_file": None},
            {"client_request_id": "r", "task_type": "files", "question": "q", "output_file": None, "scope": {}},
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    self.request("POST", f"/api/sessions/{session_id}/runs", invalid)[0],
                    400,
                )

    def test_completed_history_survives_more_than_twenty_runs(self):
        session_id = self.create_session()
        connection = self.server.runs.store.connection()
        for index in range(21):
            run_id = f"stored-{index:02d}"
            request = {
                "question": f"历史问题 {index}",
                "task_type": "files",
                "scope": {"mode": "file", "target_path": "note.md"},
                "output_path": None,
                "parent_run_id": None,
                "execution_options": {},
            }
            connection.execute(
                """INSERT INTO runs (
                       id, session_id, client_request_id, request_json,
                       request_fingerprint, task_type, question, scope_json,
                       state, phase, result_json, config_json, system_version,
                       tool_version, protocol_version, started_at, finished_at
                   ) VALUES (?, ?, ?, ?, ?, 'files', ?, ?, 'completed', 'finished',
                             ?, '{}', 's', 't', 'p', ?, ?)""",
                (
                    run_id,
                    session_id,
                    f"stored-request-{index}",
                    json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    f"fingerprint-{index}",
                    request["question"],
                    json.dumps(request["scope"], sort_keys=True, separators=(",", ":")),
                    json.dumps({"state": "completed", "answer": None}),
                    float(index),
                    float(index) + 0.5,
                ),
            )
        status, first = self.request("GET", f"/api/sessions/{session_id}/runs")
        self.assertEqual(status, 200)
        self.assertEqual(len(first["runs"]), 20)
        self.assertIsNotNone(first["next_cursor"])
        status, second = self.request(
            "GET", f"/api/sessions/{session_id}/runs?cursor={first['next_cursor']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(second["runs"]), 1)

    def test_missing_workspace_keeps_history_and_conversation_available(self):
        session_id = self.create_session()
        status, started = self.submit(session_id)
        self.assertEqual(status, 202)
        self.wait_run(session_id, started["run"]["id"])
        self.workspace.rename(self.root / "moved-workspace")
        provider_calls = []

        class ConversationProvider:
            metadata = {"provider": "conversation-test", "model": "none", "simulated": True}

            def complete(inner_self, messages, tools, timeout):
                provider_calls.append((messages, tools))
                for message in messages:
                    if message.get("role") != "user":
                        continue
                    try:
                        payload = json.loads(message.get("content", ""))
                    except (TypeError, ValueError):
                        continue
                    for record in payload.get("records", []) if isinstance(payload, dict) else []:
                        if record.get("role") == "user":
                            text = record["text"]
                            return ModelReply({"role": "assistant", "content": json.dumps({
                                "status": "answered", "answer": "之前要求核对项目代号。",
                                "references": [{"message_id": record["message_id"],
                                                "start": 0, "end": len(text)}],
                            }, ensure_ascii=False)})
                raise AssertionError("expected visible session history")

        self.server.runs.provider_factory = ConversationProvider
        status, detail = self.request("GET", f"/api/sessions/{session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["session"]["id"], session_id)
        status, error = self.submit(
            session_id, client_request_id="missing-files", question="重新核对"
        )
        self.assertEqual((status, error), (409, {"error": "WORKSPACE_UNAVAILABLE"}))
        self.assertEqual(provider_calls, [])

        status, started = self.submit(
            session_id,
            client_request_id="conversation-1",
            task_type="conversation",
            question="我之前要求了什么？",
        )
        self.assertEqual(status, 202, started)
        completed = self.wait_run(session_id, started["run"]["id"])
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["result"]["answer"]["references"][0]["quote"],
                         "项目代号是什么？请引用原文。")
        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(
            [tool["function"]["name"] for tool in provider_calls[0][1]],
            ["session_history"],
        )

    def test_selected_session_server_reopens_history_after_workspace_removed(self):
        session_id = self.create_session()
        status, started = self.submit(session_id)
        self.assertEqual(status, 202)
        run_id = started["run"]["id"]
        self.wait_run(session_id, run_id)
        self.close_server()
        self.workspace.rename(self.root / "workspace-moved-before-restart")
        calls = []

        def provider():
            calls.append(True)
            return DemoProvider()

        self.restart_for_session(session_id, provider)
        status, config = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertFalse(config["workspace_available"])
        baseline = len(calls)
        self.assertEqual(self.request("GET", f"/api/sessions/{session_id}")[0], 200)
        status, history = self.request("GET", f"/api/sessions/{session_id}/runs")
        self.assertEqual(status, 200)
        self.assertEqual(history["runs"][0]["id"], run_id)
        self.assertEqual(
            self.submit(session_id, client_request_id="files-after-restart"),
            (409, {"error": "WORKSPACE_UNAVAILABLE"}),
        )
        self.assertEqual(len(calls), baseline)


if __name__ == "__main__":
    unittest.main()
