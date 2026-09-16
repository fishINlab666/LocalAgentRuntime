import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_agent.import_evaluation import _has_follow_up_facts, evaluate_imports
from local_agent.provider import ModelReply


ROOT = Path(__file__).resolve().parents[1]


def _tool_call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(
                {**arguments, "intent": "定位并核对导入原文"},
                ensure_ascii=False,
            ),
        },
    }


def _tool_payload(message):
    raw = message["content"].split("\n\n", 1)[0]
    return json.loads(raw)


class ImportAcceptanceProvider:
    metadata = {"provider": "scripted-acceptance", "model": "none", "simulated": True}

    def __init__(self, *, localized_follow_up=False, wrong_follow_up=False):
        self.requests = []
        self.pdf_path = None
        self.docx_path = None
        self.text_path = None
        self.localized_follow_up = localized_follow_up
        self.wrong_follow_up = wrong_follow_up

    def complete(self, messages, tools, timeout):
        self.requests.append({
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(tools),
            "timeout": timeout,
        })
        step = len(self.requests)
        if step == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("search-1", "search_documents", {
                    "query": "Acceptance fact",
                })],
            }
        elif step == 2:
            search = _tool_payload(messages[-1])["data"]
            by_kind = {
                item["locations"][0]["kind"]: item["path"]
                for item in search["matches"]
            }
            self.pdf_path = by_kind["pdf_page"]
            self.docx_path = by_kind["docx_table_row"]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call("read-pdf-1", "read_file", {"path": self.pdf_path}),
                    _tool_call("read-docx-1", "read_file", {"path": self.docx_path}),
                ],
            }
        elif step == 3:
            message = {
                "role": "assistant",
                "content": json.dumps({
                    "status": "answered",
                    "answer": "预算是 Budget 42，负责人是 Owner Mei。",
                    "citations": [
                        {"path": self.pdf_path, "start_line": 2, "end_line": 2},
                        {"path": self.docx_path, "start_line": 2, "end_line": 2},
                    ],
                }, ensure_ascii=False),
            }
        elif step == 4:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("search-2", "search_documents", {
                    "query": "Release checkpoint",
                })],
            }
        elif step == 5:
            search = _tool_payload(messages[-1])["data"]
            self.text_path = search["matches"][0]["path"]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("read-text-2", "read_file", {
                    "path": self.text_path,
                })],
            }
        elif step == 6:
            if self.wrong_follow_up:
                answer = (
                    "上一轮确认预算是 142、负责人是 Meier；"
                    "本轮核对 Release checkpoint: TXT-READY-old。"
                )
            elif self.localized_follow_up:
                answer = (
                    "本轮核对 Release checkpoint: TXT-READY，"
                    "并沿用上一轮预算 42、负责人 Mei 的结论。"
                )
            else:
                answer = (
                    "上一轮确认预算是 Budget 42、负责人是 Owner Mei；"
                    "本轮核对 Release checkpoint: TXT-READY。"
                )
            message = {
                "role": "assistant",
                "content": json.dumps({
                    "status": "answered",
                    "answer": answer,
                    "citations": [
                        {"path": self.text_path, "start_line": 1, "end_line": 1},
                    ],
                }, ensure_ascii=False),
            }
        else:
            raise AssertionError("acceptance exceeded its fixed six model requests")
        return ModelReply(message)


class ImportAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_fixed_mixed_document_loop_survives_restore(self):
        provider = ImportAcceptanceProvider()

        report = evaluate_imports(lambda: provider, self.root / "reports")

        self.assertEqual(report["gate"], "SIMULATED_ONLY", report)
        self.assertTrue(report["automatic_checks_passed"], report)
        self.assertEqual(report["expected_runs"], 2)
        self.assertEqual(report["completed_runs"], 2)
        self.assertEqual(report["model_calls"], 6)
        self.assertLessEqual(report["model_calls"], 12)
        self.assertTrue(all(report["checks"].values()), report["checks"])

        first_request_json = json.dumps(provider.requests[0], ensure_ascii=False)
        for hidden in ("Budget 42", "Owner\tMei", "TXT-READY", "MD-ALPHA"):
            self.assertNotIn(hidden, first_request_json)

        request_after_search = provider.requests[1]
        self.assertEqual(request_after_search["messages"][-1]["tool_call_id"], "search-1")
        search = _tool_payload(request_after_search["messages"][-1])
        self.assertEqual(search["data"]["evidence_role"], "catalog")

        request_after_reads = provider.requests[2]
        returned = [
            message for message in request_after_reads["messages"]
            if message.get("role") == "tool"
            and message.get("tool_call_id") in {"read-pdf-1", "read-docx-1"}
        ]
        self.assertEqual([item["tool_call_id"] for item in returned], [
            "read-pdf-1", "read-docx-1",
        ])
        self.assertIn("Budget 42", json.dumps(_tool_payload(returned[0]), ensure_ascii=False))
        self.assertTrue(any(
            "Owner Mei" in line
            for line in _tool_payload(returned[1])["data"]["content"].values()
        ))

        first = report["runs"][0]["answer"]
        self.assertEqual(first["citations"][0]["source"]["locations"], [
            {"kind": "pdf_page", "page": 2},
        ])
        self.assertEqual(first["citations"][1]["source"]["locations"], [
            {"kind": "docx_table_row", "table": 1, "row": 1},
        ])

        restored_request = provider.requests[3]
        history = [
            json.loads(message["content"])
            for message in restored_request["messages"]
            if message.get("role") == "user"
            and message.get("content", "").startswith("{")
        ]
        projection = next(item for item in history
                          if item.get("source_kind") == "session_history_projection")
        self.assertIn("Budget 42", json.dumps(projection, ensure_ascii=False))
        self.assertFalse(any(message.get("role") == "tool"
                             for message in restored_request["messages"]))

        second = report["runs"][1]["answer"]
        self.assertEqual(second["citations"][0]["source"], {
            "kind": "imported_document",
            "name": "checkpoint.txt",
            "logical_path": "资料/checkpoint.txt",
            "locations": [{"kind": "text_lines", "start": 1, "end": 1}],
        })

    def test_follow_up_accepts_localized_labels_with_exact_values(self):
        provider = ImportAcceptanceProvider(localized_follow_up=True)

        report = evaluate_imports(lambda: provider, self.root / "localized")

        self.assertTrue(report["checks"]["follow_up_facts"], report)
        self.assertEqual(report["gate"], "SIMULATED_ONLY", report)

    def test_follow_up_rejects_values_embedded_in_different_identifiers(self):
        provider = ImportAcceptanceProvider(wrong_follow_up=True)

        report = evaluate_imports(lambda: provider, self.root / "wrong-values")

        self.assertFalse(report["checks"]["follow_up_facts"], report)
        self.assertEqual(report["gate"], "FAILED", report)

    def test_follow_up_rejects_correct_values_attached_to_wrong_labels(self):
        answer = (
            "预算 99、负责人 Lee；附件 Mei 的版本 42，"
            "Release checkpoint: TXT-READY。"
        )

        self.assertFalse(_has_follow_up_facts(answer))

    def test_follow_up_rejects_checkpoint_with_identifier_suffix(self):
        answer = "上一轮预算 42、负责人 Mei；当前标识 TXT-READY-old。"

        self.assertFalse(_has_follow_up_facts(answer))

    def test_follow_up_rejects_decimal_budget(self):
        answer = "上一轮预算 42.5、负责人 Mei；当前标识 TXT-READY。"

        self.assertFalse(_has_follow_up_facts(answer))

    def test_follow_up_rejects_checkpoint_with_dot_version(self):
        answer = "上一轮预算 42、负责人 Mei；当前标识 TXT-READY.v2。"

        self.assertFalse(_has_follow_up_facts(answer))

    def test_cli_without_key_records_not_run(self):
        environment = dict(os.environ)
        environment.pop("DEEPSEEK_API_KEY", None)
        environment.pop("AGENT_API_KEY", None)
        run = subprocess.run(
            [sys.executable, "-m", "local_agent", "evaluate-imports",
             "--log-dir", str(self.root / "missing-key")],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(run.returncode, 2, run.stderr or run.stdout)
        report = json.loads(run.stdout)
        self.assertEqual(report["gate"], "NOT_RUN")
        self.assertEqual(report["error"], "CONFIG_MISSING")
        self.assertEqual(report["model_calls"], 0)

    def test_late_evidence_error_cannot_be_reported_as_passed(self):
        provider = ImportAcceptanceProvider()
        valid_context = {
            "first_tool_is_root_listing": False,
            "tool_results_in_next_context": True,
        }
        with patch(
            "local_agent.import_evaluation.context_checks",
            side_effect=[valid_context, RuntimeError("late evidence failure")],
        ):
            report = evaluate_imports(lambda: provider, self.root / "late-error")

        self.assertEqual(report["completed_runs"], 2, report)
        self.assertEqual(report["gate"], "FAILED", report)
        self.assertFalse(report["automatic_checks_passed"])
        self.assertEqual(report["error"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
