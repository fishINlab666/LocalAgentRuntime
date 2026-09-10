"""Leave one durable session run at a named crash boundary for process tests."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local_agent.session_store import SessionStore  # noqa: E402
from local_agent.sessions import (  # noqa: E402
    RunSubmission,
    SessionScope,
    SessionService,
)


PAUSE_POINTS = (
    "after_submit",
    "model_in_flight",
    "read_started",
    "approval_waiting",
    "approval_allowed",
    "publication_intent",
    "publication_receipt",
)
CONTENT = b"planned"


def _arguments(path="report.md"):
    return json.dumps(
        {"path": path, "content": CONTENT.decode("utf-8")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_call(call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": _arguments(),
        },
    }


def _read_call(call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"path":"note.md"}',
        },
    }


def _append_activity(path, kind):
    raw = (json.dumps({"kind": kind}, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC,
        0o600,
    )
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish(path):
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        offset = 0
        while offset < len(CONTENT):
            offset += os.write(descriptor, CONTENT[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _pause(payload):
    print(json.dumps({"event": "ready", **payload}, sort_keys=True), flush=True)
    while True:
        signal.pause()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--pause-point", choices=PAUSE_POINTS, required=True)
    parser.add_argument("--activity-log", type=Path, required=True)
    args = parser.parse_args()

    store = SessionStore.open(args.state_dir)
    service = SessionService(store)
    scope = SessionScope("directory", None)
    session = service.create(args.workspace, "process recovery", scope)
    prepared = service.submit(
        session.id,
        RunSubmission(
            client_request_id=f"request-{args.pause_point}",
            question=f"pause at {args.pause_point}",
            task_type="files",
            scope=scope,
            output_path="report.md",
            parent_run_id=None,
            execution_options={},
        ),
    )
    base = {
        "pause_point": args.pause_point,
        "session_id": session.id,
        "run_id": prepared.run_id,
    }
    if args.pause_point == "after_submit":
        _pause(base)

    prepared.journal.record_model_request(
        {"messages": []}, {"kind": "process-fixture"}
    )
    _append_activity(args.activity_log, "provider")
    if args.pause_point == "model_in_flight":
        _pause(base)

    call_id = f"call-{args.pause_point}"
    if args.pause_point == "read_started":
        prepared.journal.record_model_reply(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_read_call(call_id)],
            },
            usage=None,
            validation="tool_calls_valid",
        )
        prepared.journal.record_tool_started(call_id)
        _append_activity(args.activity_log, "tool")
        _pause({**base, "call_id": call_id})

    prepared.journal.record_model_reply(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_write_call(call_id)],
        },
        usage=None,
        validation="tool_calls_valid",
    )
    approval_id = f"approval-{args.pause_point}"
    prepared.journal.record_approval_required(
        call_id,
        {
            "id": approval_id,
            "preview": {
                "path": "report.md",
                "content": CONTENT.decode("utf-8"),
                "action_summary": "创建 report.md",
            },
            "argument_hash": hashlib.sha256(_arguments().encode("utf-8")).hexdigest(),
            "process_generation": "fixture-process",
        },
    )
    approval_payload = {
        **base,
        "call_id": call_id,
        "approval_id": approval_id,
        "decision_before_kill": "pending",
    }
    if args.pause_point == "approval_waiting":
        _pause(approval_payload)

    prepared.journal.record_approval_decision(approval_id, "allowed")
    approval_payload["decision_before_kill"] = "allowed"
    if args.pause_point == "approval_allowed":
        _pause(approval_payload)

    digest = hashlib.sha256(CONTENT).hexdigest()
    prepared.journal.record_publication_intent(
        call_id,
        {
            "path": "report.md",
            "bytes": len(CONTENT),
            "sha256": digest,
            "approval_id": approval_id,
        },
    )
    _append_activity(args.activity_log, "tool")
    target = args.workspace / "report.md"
    _publish(target)
    if args.pause_point == "publication_intent":
        _pause(approval_payload)

    prepared.journal.record_publication_receipt(
        call_id,
        {
            "path": "report.md",
            "bytes": len(CONTENT),
            "sha256": digest,
            "operation": "created",
        },
    )
    _pause(approval_payload)


if __name__ == "__main__":
    main()
