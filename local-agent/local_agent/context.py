"""Bounded projection of durable session history into one model request."""

import copy
from dataclasses import dataclass
import hashlib
import json

from .session_store import SessionStore, StoreError


@dataclass(frozen=True)
class BuiltRequest:
    request: dict
    manifest: dict


def _encoded(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _payload(text: str) -> dict:
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        raise StoreError("SESSION_STORE_ERROR") from None
    if not isinstance(value, dict):
        raise StoreError("SESSION_STORE_ERROR")
    return value


class ContextBuilder:
    """Select complete recent runs; never turns old calls into live protocol messages."""

    RECENT_RUNS = 4

    def __init__(self, store: SessionStore, session_id: str, run_id: str):
        self.store = store
        self.session_id = session_id
        self.run_id = run_id

    def _cutoff(self) -> int:
        row = self.store.connection().execute(
            """SELECT MIN(session_seq) FROM messages
               WHERE session_id=? AND run_id=? AND role='user'""",
            (self.session_id, self.run_id),
        ).fetchone()
        if row is None or row[0] is None:
            raise StoreError("SESSION_STORE_ERROR")
        return row[0]

    def _recent_run_ids(self, cutoff: int) -> tuple[list[str], list[str]]:
        rows = self.store.connection().execute(
            """SELECT r.id, MIN(m.session_seq) AS first_seq
               FROM runs r JOIN messages m ON m.run_id=r.id AND m.session_id=r.session_id
               WHERE r.session_id=? AND r.id<>? AND r.finished_at IS NOT NULL
                     AND m.session_seq < ?
               GROUP BY r.id ORDER BY first_seq DESC, r.id DESC""",
            (self.session_id, self.run_id, cutoff),
        ).fetchall()
        newest = [row[0] for row in rows]
        selected = list(reversed(newest[: self.RECENT_RUNS]))
        omitted = list(reversed(newest[self.RECENT_RUNS :]))
        return selected, omitted

    def _project_run(self, run_id: str, cutoff: int) -> tuple[dict, list[str]]:
        rows = self.store.connection().execute(
            """SELECT id, session_seq, role, source_kind, payload_json,
                      validation_state
               FROM messages WHERE session_id=? AND run_id=? AND session_seq < ?
               ORDER BY run_seq""",
            (self.session_id, run_id, cutoff),
        ).fetchall()
        results = {}
        parsed = []
        for row in rows:
            payload = _payload(row[4])
            parsed.append((row, payload))
            if row[2] == "tool" and row[5] == "valid":
                call_id = payload.get("tool_call_id")
                if isinstance(call_id, str):
                    results[call_id] = {
                        "message_id": row[0], "status": payload.get("result", {}).get("ok"),
                        "result": payload.get("result"),
                    }

        records = []
        selected_ids = []
        for row, payload in parsed:
            message_id, session_seq, role, source_kind, _, validation = row
            if role == "user" and source_kind == "user":
                content = payload.get("content")
                if isinstance(content, str):
                    records.append({"message_id": message_id, "session_seq": session_seq,
                                    "role": "user", "source_kind": "user", "text": content})
                    selected_ids.append(message_id)
            elif role == "assistant" and validation == "answer_valid":
                content = payload.get("content")
                if isinstance(content, str):
                    records.append({"message_id": message_id, "session_seq": session_seq,
                                    "role": "assistant", "source_kind": "answer", "text": content})
                    selected_ids.append(message_id)
            elif role == "assistant" and validation == "tool_calls_valid":
                calls = payload.get("tool_calls")
                if not isinstance(calls, list):
                    continue
                projected = []
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    call_id = call.get("id") if isinstance(call, dict) else None
                    if not isinstance(function, dict) or not isinstance(call_id, str):
                        continue
                    projected.append({
                        "call_id": call_id,
                        "name": function.get("name"),
                        "arguments": function.get("arguments"),
                        "result": results.get(call_id),
                    })
                    result = results.get(call_id)
                    if result is not None:
                        selected_ids.append(result["message_id"])
                if projected:
                    records.append({"message_id": message_id, "session_seq": session_seq,
                                    "role": "assistant", "source_kind": "tool_chain",
                                    "tool_calls": projected})
                    selected_ids.append(message_id)
            elif role == "control" and source_kind == "recovery" and validation == "valid":
                records.append({"message_id": message_id, "session_seq": session_seq,
                                "role": "control", "source_kind": "recovery",
                                "recovery": payload})
                selected_ids.append(message_id)
        projection = {
            "role": "user",
            "content": json.dumps({
                "historical": True,
                "source_kind": "session_history_projection",
                "source_run_id": run_id,
                "records": records,
            }, ensure_ascii=False, allow_nan=False, sort_keys=True,
                separators=(",", ":")),
        }
        return projection, selected_ids

    def build(self, current_request: dict, max_input_bytes: int, *, summarize=None) -> BuiltRequest:
        if not isinstance(current_request, dict) or type(max_input_bytes) is not int or max_input_bytes <= 0:
            raise ValueError("CONTEXT_LIMIT")
        current = copy.deepcopy(current_request)
        if not isinstance(current.get("messages"), list) or not isinstance(current.get("tools"), list):
            raise ValueError("CONTEXT_LIMIT")
        if len(_encoded(current)) > max_input_bytes:
            raise ValueError("CONTEXT_LIMIT")

        cutoff = self._cutoff()
        selected_runs, omitted_runs = self._recent_run_ids(cutoff)
        projections = []
        ids_by_run = []
        for run_id in selected_runs:
            projection, message_ids = self._project_run(run_id, cutoff)
            projections.append(projection)
            ids_by_run.append(message_ids)

        def merged(items):
            request = copy.deepcopy(current)
            insertion = 1 if request["messages"] and request["messages"][0].get("role") == "system" else 0
            request["messages"][insertion:insertion] = copy.deepcopy(items)
            return request

        request = merged(projections)
        while projections and len(_encoded(request)) > max_input_bytes:
            omitted_runs.insert(0, selected_runs.pop(0))
            projections.pop(0)
            ids_by_run.pop(0)
            request = merged(projections)
        if len(_encoded(request)) > max_input_bytes:
            raise ValueError("CONTEXT_LIMIT")

        encoded = _encoded(request)
        selected_message_ids = [item for group in ids_by_run for item in group]
        manifest = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "cutoff_seq": cutoff,
            "selected_run_ids": selected_runs,
            "selected_run_count": len(selected_runs),
            "selected_message_ids": selected_message_ids,
            "omitted_run_ids": omitted_runs,
            "scope": "same_session_before_current_input",
            "input_bytes": len(encoded),
            "input_sha256": hashlib.sha256(encoded).hexdigest(),
            "summary_id": None,
        }
        return BuiltRequest(request=request, manifest=manifest)
