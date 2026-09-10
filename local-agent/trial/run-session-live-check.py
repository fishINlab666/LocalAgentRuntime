#!/usr/bin/env python3
"""Run the bounded real-model acceptance for durable local sessions."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from local_agent.approvals import ApprovalBroker  # noqa: E402
from local_agent.provider import DeepSeekProvider, ProviderError  # noqa: E402
from local_agent.session_store import SessionStore  # noqa: E402
from local_agent.sessions import (  # noqa: E402
    RunSubmission,
    SessionScope,
    SessionService,
)
from local_agent.trace import Trace  # noqa: E402


MAX_SUBMISSIONS = 6
MAX_MODEL_REQUESTS = 36
MARKER = "legacy-only-7f3a.md"


class CheckFailed(Exception):
    pass


def canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class AutoApprovalBroker(ApprovalBroker):
    """Approve only the exact synthetic report expected by this acceptance."""

    EXPECTED = ("项目复核报告", "紫杉-88", "林澄", "2026-10-15")

    def __init__(self, run_id, trace, *, journal):
        super().__init__(run_id, journal=journal)
        self.trace = trace
        self.preview = None
        self.preview_valid = False

    def request(self, call_id, name, arguments, preview, control):
        self.preview = copy.deepcopy(preview)
        content = preview.get("content") if isinstance(preview, dict) else None
        self.preview_valid = (
            isinstance(content, str)
            and preview.get("path") == "project-review.md"
            and all(value in content for value in self.EXPECTED)
            and "青禾-47" not in content
        )

        def publish(event, data):
            self.trace.emit(event, data)
            if event == "approval.required":
                self.decide(data["id"], "allow" if self.preview_valid else "deny")

        self.publish = publish
        return super().request(call_id, name, arguments, preview, control)

    def evidence(self):
        if not isinstance(self.preview, dict):
            return None
        content = self.preview.get("content", "")
        encoded = content.encode("utf-8") if isinstance(content, str) else b""
        return {
            "path": self.preview.get("path"),
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "required_facts_present": self.preview_valid,
        }


class LiveCheck:
    def __init__(self, output: Path, provider):
        self.output = output
        self.provider = provider
        self.real_runs = []
        self.report = {
            "gate": "RUNNING",
            "provider": copy.deepcopy(provider.metadata),
            "limits": {
                "max_user_submissions": MAX_SUBMISSIONS,
                "max_model_requests": MAX_MODEL_REQUESTS,
            },
            "runs": [],
            "checks": [],
            "failures": [],
        }

    def check(self, name: str, passed, detail: str) -> None:
        passed = bool(passed)
        self.report["checks"].append(
            {"name": name, "passed": passed, "detail": detail}
        )
        if not passed:
            self.report["failures"].append(name)
            raise CheckFailed(name)

    def used_requests(self) -> int:
        return sum(item["model_calls"] for item in self.report["runs"])

    def _reserve_request_budget(self) -> None:
        self.check(
            f"budget-before-submission-{len(self.real_runs) + 1}",
            len(self.real_runs) < MAX_SUBMISSIONS
            and self.used_requests() + 6 <= MAX_MODEL_REQUESTS,
            "为下一次提交预留最多 6 次模型请求",
        )

    def _manifest_evidence(self, view: dict) -> list[dict]:
        evidence = []
        for item in view["context_manifests"]:
            payload = item["payload"]
            request = payload["request"]
            manifest = payload["manifest"]
            encoded = canonical(request)
            self.check(
                f"manifest-budget-{view['id']}-{item['request_seq']}",
                item["input_bytes"] <= 65536
                and manifest.get("input_bytes", item["input_bytes"]) <= 65536,
                "实际模型请求不超过 64 KiB",
            )
            self.check(
                f"manifest-hash-{view['id']}-{item['request_seq']}",
                item["input_sha256"] == hashlib.sha256(encoded).hexdigest(),
                "保存的 Context 哈希与实际请求一致",
            )
            evidence.append(
                {
                    "request_seq": item["request_seq"],
                    "source_kind": manifest.get("source_kind"),
                    "input_bytes": item["input_bytes"],
                    "input_sha256": item["input_sha256"],
                    "summary_id": manifest.get("summary_id"),
                    "active_summary_id": manifest.get("active_summary_id"),
                    "omitted_seq_range": manifest.get("omitted_seq_range"),
                    "tool_names": [
                        tool.get("function", {}).get("name")
                        for tool in request.get("tools", [])
                    ],
                }
            )
        return evidence

    def execute(
        self,
        store,
        service,
        session,
        *,
        request_id,
        question,
        task_type,
        output_path=None,
    ):
        self._reserve_request_budget()
        prepared = service.submit(
            session.id,
            RunSubmission(
                request_id,
                question,
                task_type,
                session.scope,
                output_path,
                None,
                {},
            ),
        )
        trace = Trace(
            self.output / ("segment1" if request_id.startswith("live-seg1") else "segment2") / "runs",
            Path(session.workspace_path),
            run_id=prepared.run_id,
        )
        broker = (
            AutoApprovalBroker(prepared.run_id, trace, journal=prepared.journal)
            if output_path is not None
            else None
        )
        try:
            result = service.execute(
                prepared, self.provider, trace, approvals=broker
            )
            view = service.run_view(session.id, prepared.run_id)
        finally:
            if broker is not None:
                broker.close()

        manifests = self._manifest_evidence(view)
        model_calls = result.get("model_calls", 0)
        self.check(
            f"journal-count-{request_id}",
            len(manifests) == model_calls <= 6,
            "每次调用前先保存 manifest，且单次提交最多 6 次模型请求",
        )
        run_evidence = {
            "client_request_id": request_id,
            "session_id": session.id,
            "run_id": prepared.run_id,
            "state": result.get("state"),
            "stop_reason": result.get("stop_reason"),
            "model_calls": model_calls,
            "answer": copy.deepcopy(result.get("answer")),
            "usage": copy.deepcopy((view.get("provider") or {}).get("usage")),
            "manifests": manifests,
            "tool_calls": [
                {
                    "call_id": call["call_id"],
                    "name": call["name"],
                    "stage": call["stage"],
                    "publication_state": call["publication_state"],
                }
                for call in view["tool_calls"]
            ],
            "artifacts": copy.deepcopy(view["artifacts"]),
            "approval_preview": None if broker is None else broker.evidence(),
            "trace_path": str(trace.path),
        }
        self.real_runs.append((store.state_dir, session.id, prepared.run_id))
        self.report["runs"].append(run_evidence)
        self.check(
            f"terminal-{request_id}",
            result.get("state") == "completed",
            f"真实提交应完成；实际 {result.get('state')}/{result.get('stop_reason')}",
        )
        return result, view, run_evidence

    def run_segment1(self):
        root = self.output / "segment1"
        workspace = root / "workspace"
        workspace.mkdir(parents=True, mode=0o700)
        (workspace / "project.md").write_text(
            "项目代号：青禾-47\n项目名称：本地会话验证\n", encoding="utf-8"
        )
        (workspace / "review.md").write_text(
            "负责人：林澄\n演示日期：2026-09-30\n", encoding="utf-8"
        )
        state = root / "state"
        store = SessionStore.open(state)
        try:
            service = SessionService(store)
            session = service.create(
                workspace, "真实连续会话", SessionScope("directory", None)
            )
            first, _, _ = self.execute(
                store,
                service,
                session,
                request_id="live-seg1-1",
                task_type="files",
                question=(
                    "请发现并读取当前工作区内的项目与评审资料，回答项目代号、负责人和演示日期，"
                    "逐项引用原文。以后生成报告时请使用简洁中文，标题暂定“项目核对”，"
                    "末尾列出原文依据。"
                ),
            )
            answer = json.dumps(first["answer"], ensure_ascii=False)
            self.check(
                "segment1-initial-file-values",
                all(value in answer for value in ("青禾-47", "林澄", "2026-09-30")),
                "第一轮回答包含三项初始资料事实",
            )

            second, second_view, _ = self.execute(
                store,
                service,
                session,
                request_id="live-seg1-2",
                task_type="conversation",
                question=(
                    "更正本会话的报告约定：标题改为“项目复核报告”；继续使用简洁中文并在末尾"
                    "列出原文依据。请复述现在生效的完整约定，并且只引用这条更正原文。"
                ),
            )
            current_user = next(
                message
                for message in second_view["messages"]
                if message["role"] == "user"
            )
            references = second["answer"].get("references", [])
            self.check(
                "segment1-current-correction-reference",
                "项目复核报告" in second["answer"].get("answer", "")
                and any(item.get("message_id") == current_user["id"] for item in references),
                "第二轮更正覆盖旧标题，并引用当前用户原话",
            )
            session_id = session.id
        finally:
            store.close()

        (workspace / "project.md").write_text(
            "项目代号：紫杉-88\n项目名称：本地会话验证\n", encoding="utf-8"
        )
        (workspace / "review.md").write_text(
            "负责人：林澄\n演示日期：2026-10-15\n", encoding="utf-8"
        )
        store = SessionStore.open(state)
        try:
            self.check(
                "segment1-clean-restart", store.recover_interrupted(uuid.uuid4().hex) == 0,
                "正常关闭后重开没有伪造中断运行",
            )
            service = SessionService(store)
            session = service.load(session_id)
            third, _, _ = self.execute(
                store,
                service,
                session,
                request_id="live-seg1-3",
                task_type="files",
                question=(
                    "按本会话最新约定继续。请重新读取当前资料，回答现在的项目代号、负责人和"
                    "演示日期并逐项引用；不要沿用旧值。"
                ),
            )
            answer = json.dumps(third["answer"], ensure_ascii=False)
            self.check(
                "segment1-restart-rereads-current-files",
                all(value in answer for value in ("紫杉-88", "林澄", "2026-10-15"))
                and "青禾-47" not in answer,
                "重启后的资料轮重新读取并采用新值",
            )

            fourth, _, run = self.execute(
                store,
                service,
                session,
                request_id="live-seg1-4",
                task_type="files",
                output_path="project-review.md",
                question=(
                    "请重新读取当前资料，按本会话现在生效的报告约定生成报告；回答也要引用原文。"
                ),
            )
            self.check(
                "segment1-report-approved-from-exact-preview",
                run["approval_preview"] is not None
                and run["approval_preview"]["required_facts_present"],
                "只批准路径和四项内容均正确的完整预览",
            )
            artifacts = fourth.get("artifacts", [])
            target = workspace / "project-review.md"
            raw = target.read_bytes() if target.is_file() else b""
            self.check(
                "segment1-artifact-receipt",
                len(artifacts) == 1
                and artifacts[0]["path"] == "project-review.md"
                and artifacts[0]["bytes"] == len(raw)
                and artifacts[0]["sha256"] == hashlib.sha256(raw).hexdigest(),
                "磁盘文件与 Runtime/数据库回执的字节数和哈希一致",
            )
        finally:
            store.close()

    @staticmethod
    def _seed_long_history(service, store, session):
        early_id = None
        special = {}
        for number in range(50):
            if number == 0:
                prefix = "最早交付要求：最终交付必须保留“霜叶规范-91”，且标题不超过十个字。"
            else:
                prefix = f"历史记录第 {number:02d} 轮：仅用于上下文预算验证。"
            question = prefix + chr(0x4E00 + number) * 1400
            prepared = service.submit(
                session.id,
                RunSubmission(
                    f"fixture-{number:02d}", question, "conversation",
                    session.scope, None, None, {},
                ),
            )
            user = store.load_run_messages(session.id, prepared.run_id)[0]
            if number == 0:
                early_id = user.id
            prepared.journal.record_model_request(
                {"messages": []}, {"source_kind": "fixture"})
            if number == 1:
                call_id = "legacy-failed-call"
                call = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps(
                            {"path": MARKER, "intent": "核对旧附件"},
                            ensure_ascii=False,
                        ),
                    },
                }
                assistant_id = prepared.journal.record_model_reply(
                    {"role": "assistant", "content": None, "tool_calls": [call]},
                    None,
                    "tool_calls_valid",
                )
                prepared.journal.record_tool_started(call_id)
                result_id = prepared.journal.record_tool_result(
                    call_id,
                    {
                        "ok": False,
                        "error": {
                            "code": "PATH_NOT_DISCOVERED",
                            "message": "未发现该路径",
                        },
                    },
                )
                prepared.journal.record_model_request(
                    {"messages": []}, {"source_kind": "fixture-2"})
                special = {
                    "run_id": prepared.run_id,
                    "call_id": call_id,
                    "assistant_message_id": assistant_id,
                    "result_message_id": result_id,
                }
            prepared.journal.record_model_reply(
                {"role": "assistant", "content": f"第 {number:02d} 轮记录已保存。"},
                None,
                "answer_valid",
            )
            prepared.journal.finish_run(
                {
                    "state": "completed",
                    "stop_reason": "ANSWER_VALIDATED",
                    "answer": f"第 {number:02d} 轮记录已保存。",
                }
            )
        return early_id, special

    def run_segment2(self):
        root = self.output / "segment2"
        workspace = root / "workspace"
        workspace.mkdir(parents=True, mode=0o700)
        state = root / "state"
        store = SessionStore.open(state)
        try:
            service = SessionService(store)
            session = service.create(
                workspace, "真实长会话", SessionScope("directory", None)
            )
            early_id, special = self._seed_long_history(service, store, session)
            connection = store.connection()
            history_bytes = connection.execute(
                "SELECT SUM(LENGTH(CAST(payload_json AS BLOB))) FROM messages WHERE session_id=?",
                (session.id,),
            ).fetchone()[0]
            marker_messages = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND instr(payload_json, ?)>0",
                (session.id, MARKER),
            ).fetchone()[0]
            marker_runs = connection.execute(
                """SELECT COUNT(*) FROM runs WHERE session_id=?
                   AND (instr(request_json, ?)>0 OR instr(COALESCE(result_json,''), ?)>0)""",
                (session.id, MARKER, MARKER),
            ).fetchone()[0]
            self.check(
                "segment2-seed-size", history_bytes > 128 * 1024,
                "50 轮原始消息超过 128 KiB",
            )
            self.check(
                "segment2-marker-only-in-tool-call",
                marker_messages == 1 and marker_runs == 0,
                "路径标记只存在于旧 ToolCall 参数",
            )
            session_id = session.id
        finally:
            store.close()

        store = SessionStore.open(state)
        try:
            self.check(
                "segment2-clean-restart", store.recover_interrupted(uuid.uuid4().hex) == 0,
                "离线预置完成后重开没有自动执行",
            )
            service = SessionService(store)
            session = service.load(session_id)
            fifth, fifth_view, fifth_run = self.execute(
                store,
                service,
                session,
                request_id="live-seg2-1",
                task_type="conversation",
                question=(
                    "请先使用 session_history 回查本会话最早的交付要求（search 的 query 使用“交付要求”），"
                    "再回答其中的规范代号和标题限制。"
                    "最终只能引用找到的那条最早用户消息，不要引用本条提问。"
                ),
            )
            active = store.load_active_summary(session.id)
            self.check(
                "segment2-real-summary",
                active is not None
                and set(active.payload) == {
                    "goals", "constraints", "decisions", "completed", "pending", "anchors"
                }
                and len(canonical(active.payload)) <= 6144,
                "真实模型生成的结构化摘要已通过锚点和大小校验",
            )
            self.check(
                "segment2-summary-in-task-context",
                any(item["source_kind"] == "summary" for item in fifth_run["manifests"])
                and any(
                    item["source_kind"] == "task"
                    and item["summary_id"] == active.id
                    and item["omitted_seq_range"] is not None
                    for item in fifth_run["manifests"]
                ),
                "摘要计入预算并进入同一轮任务 Context",
            )
            references = fifth["answer"].get("references", [])
            self.check(
                "segment2-early-requirement-recalled",
                "霜叶规范-91" in fifth["answer"].get("answer", "")
                and any(item.get("message_id") == early_id for item in references)
                and any(call["name"] == "session_history" for call in fifth_run["tool_calls"]),
                "早期要求通过真实 history 调用找回并引用原用户消息",
            )

            sixth_question = (
                "请先用 session_history 的 search 定位本会话中那次旧的失败文件读取（query 使用 "
                "read_file），再用 read "
                "取得完整调用记录。回答它的 path、intent 和错误类型并引用原调用记录；不要执行文件工具。"
            )
            self.check(
                "segment2-query-does-not-leak-marker", MARKER not in sixth_question,
                "最后提问不预先提供旧路径标记",
            )
            sixth, sixth_view, sixth_run = self.execute(
                store,
                service,
                session,
                request_id="live-seg2-2",
                task_type="conversation",
                question=sixth_question,
            )
            answer = sixth["answer"].get("answer", "")
            self.check(
                "segment2-old-call-recalled",
                all(value in answer for value in (MARKER, "核对旧附件", "PATH_NOT_DISCOVERED"))
                and sixth_run["tool_calls"]
                and all(call["name"] == "session_history" for call in sixth_run["tool_calls"]),
                "旧参数、意图和错误经 history 找回，没有重放 read_file",
            )
            linked = False
            for message in sixth_view["messages"]:
                if message["role"] != "tool":
                    continue
                result = message["payload"].get("result", {})
                data = result.get("data", {}) if result.get("ok") is True else {}
                if (
                    data.get("action") == "read"
                    and data.get("source_run_id") == special["run_id"]
                    and data.get("call_id") == special["call_id"]
                    and data.get("result_message_id") == special["result_message_id"]
                    and data.get("result", {}).get("error", {}).get("code")
                    == "PATH_NOT_DISCOVERED"
                ):
                    linked = True
            self.check(
                "segment2-old-call-linkage", linked,
                "history 结果保持 source run/call/result message 关联",
            )
        finally:
            store.close()

    def finish_budget(self):
        requests = self.used_requests()
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        usage_records = 0
        for run in self.report["runs"]:
            for record in run.get("usage") or []:
                if not isinstance(record, dict):
                    continue
                usage_records += 1
                for key in usage:
                    value = record.get(key)
                    if type(value) is int and value >= 0:
                        usage[key] += value
        self.report["budget"] = {
            "user_submissions": len(self.report["runs"]),
            "model_requests": requests,
            "usage_records": usage_records,
            "usage_totals": usage if usage_records else None,
        }
        self.check(
            "final-real-call-budget",
            len(self.report["runs"]) <= MAX_SUBMISSIONS
            and requests <= MAX_MODEL_REQUESTS,
            "真实调用没有超过 6 次用户提交或 36 次模型请求",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--results-root", type=Path, default=PROJECT_ROOT / "trial" / "results"
    )
    args = parser.parse_args()
    output = args.results_root.resolve() / f"session-live-{uuid.uuid4().hex}"
    report_path = output / "report.json"
    output.mkdir(parents=True, mode=0o700)
    try:
        provider = DeepSeekProvider.from_env()
    except ProviderError as error:
        report = {
            "gate": "NOT_RUN",
            "error": error.code,
            "report_path": str(report_path),
            "failures": ["provider-not-configured"],
        }
        write_report(report_path, report)
        print(json.dumps({"gate": report["gate"], "report_path": str(report_path)}))
        return 2

    check = LiveCheck(output, provider)
    check.report["report_path"] = str(report_path)
    try:
        check.run_segment1()
        check.run_segment2()
        check.finish_budget()
        check.report["gate"] = "PENDING_SEMANTIC_REVIEW"
        return_code = 3
    except CheckFailed:
        check.report["gate"] = "FAILED"
        check.report.setdefault("budget", {
            "user_submissions": len(check.report["runs"]),
            "model_requests": check.used_requests(),
            "usage_records": None,
            "usage_totals": None,
        })
        return_code = 1
    except Exception as error:
        check.report["gate"] = "FAILED"
        check.report["failures"].append(
            f"unexpected:{type(error).__name__}:{getattr(error, 'code', '')}"
        )
        check.report.setdefault("budget", {
            "user_submissions": len(check.report["runs"]),
            "model_requests": check.used_requests(),
            "usage_records": None,
            "usage_totals": None,
        })
        return_code = 1
    finally:
        write_report(report_path, check.report)
        print(json.dumps({
            "gate": check.report["gate"],
            "report_path": str(report_path),
            "user_submissions": len(check.report["runs"]),
            "model_requests": check.used_requests(),
        }))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
