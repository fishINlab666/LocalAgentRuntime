"""Bounded, session-scoped lookup over durable conversation records."""

import copy
import base64
import hashlib
import hmac
import json

from .tool_runtime import ToolSpec


_ERROR_CODES = frozenset({"INVALID_ARGUMENT", "NOT_FOUND", "CURSOR_INVALID", "SESSION_STORE_ERROR"})


def _error(code):
    return {"ok": False, "error": {"code": code, "message": code}}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


class SessionHistoryTool:
    SEARCH_LIMIT = 8
    PAGE_BYTES = 8192
    RESULT_BYTES = 12288

    def __init__(self, store, session_id: str, *, before_seq: int):
        self.store = store
        self.session_id = session_id
        self.before_seq = before_seq
        self._proof = None
        self._cursor_key = hashlib.sha256(
            (str(store.database_path) + "\0" + session_id + "\0" + str(before_seq)).encode("utf-8")
        ).digest()
        self.spec = ToolSpec(
            name="session_history",
            description="Search or read earlier records from this session only.",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["search", "read"]},
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "cursor": {"type": "string", "minLength": 1, "maxLength": 2048},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            risk="low", source="builtin", timeout_seconds=5,
            max_result_bytes=self.RESULT_BYTES, error_codes=_ERROR_CODES,
            user_error_codes=frozenset({"SESSION_STORE_ERROR"}),
        )

    def preview(self, arguments):
        return {"action_summary": "查询当前会话的较早记录，不执行旧工具。"}

    def verify_preview(self, arguments, preview):
        expected = {"action_summary": "查询当前会话的较早记录，不执行旧工具。"}
        if preview != expected:
            raise ValueError("invalid history preview")
        return copy.deepcopy(preview)

    def _messages(self):
        return self.store.connection().execute(
            """SELECT id, run_id, session_seq, role, source_kind,
                      payload_json, validation_state
               FROM messages WHERE session_id=? AND session_seq < ?
               ORDER BY session_seq, id""",
            (self.session_id, self.before_seq),
        ).fetchall()

    @staticmethod
    def _parsed(payload):
        try:
            value = json.loads(payload)
        except (TypeError, ValueError, RecursionError):
            return None
        return value if isinstance(value, dict) else None

    def _views(self):
        rows = self._messages()
        results = {}
        for row in rows:
            payload = self._parsed(row[5])
            if row[3] == "tool" and row[6] == "valid" and payload:
                call_id = payload.get("tool_call_id")
                if isinstance(call_id, str):
                    results[(row[1], call_id)] = (row[0], payload.get("result"))
        views = []
        for row in rows:
            message_id, run_id, seq, role, source_kind, raw, validation = row
            payload = self._parsed(raw)
            if payload is None:
                continue
            if role == "user" and source_kind == "user" and isinstance(payload.get("content"), str):
                views.append({"message_id": message_id, "source_run_id": run_id,
                              "session_seq": seq, "source_kind": "user",
                              "text": payload["content"]})
            elif role == "assistant" and validation == "answer_valid" \
                    and isinstance(payload.get("content"), str):
                views.append({"message_id": message_id, "source_run_id": run_id,
                              "session_seq": seq, "source_kind": "assistant",
                              "text": payload["content"]})
            elif role == "assistant" and validation == "tool_calls_valid" \
                    and isinstance(payload.get("tool_calls"), list):
                for call in payload["tool_calls"]:
                    function = call.get("function") if isinstance(call, dict) else None
                    call_id = call.get("id") if isinstance(call, dict) else None
                    if not isinstance(function, dict) or not isinstance(call_id, str):
                        continue
                    arguments_raw = function.get("arguments")
                    try:
                        arguments = json.loads(arguments_raw)
                    except (TypeError, ValueError, RecursionError):
                        arguments = arguments_raw
                    intent = arguments.pop("intent", None) if isinstance(arguments, dict) else None
                    text = _canonical({"call_id": call_id, "name": function.get("name"),
                                       "arguments": arguments, "intent": intent})
                    result = results.get((run_id, call_id))
                    views.append({"message_id": message_id, "source_run_id": run_id,
                                  "session_seq": seq, "source_kind": "tool_call",
                                  "call_id": call_id, "name": function.get("name"),
                                  "text": text,
                                  "result_message_id": result[0] if result else None,
                                  "result": result[1] if result else None})
        return views

    def _cursor(self, message_id, offset):
        payload = _canonical([self.session_id, self.before_seq, message_id, offset]).encode("utf-8")
        signature = hmac.new(self._cursor_key, payload, hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(payload + b"." + signature.encode("ascii")).decode("ascii")

    def _decode_cursor(self, cursor, message_id):
        try:
            raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
            payload, signature = raw.rsplit(b".", 1)
            expected = hmac.new(self._cursor_key, payload, hashlib.sha256).hexdigest().encode("ascii")
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            value = json.loads(payload.decode("utf-8"))
            if value[:3] != [self.session_id, self.before_seq, message_id] or type(value[3]) is not int:
                raise ValueError()
            return value[3]
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ValueError("CURSOR_INVALID") from None

    @staticmethod
    def _slice(text, offset, maximum):
        end = offset
        size = 0
        while end < len(text):
            amount = len(text[end].encode("utf-8"))
            if size + amount > maximum:
                break
            size += amount
            end += 1
        return text[offset:end], end

    def _search(self, arguments):
        if set(arguments) != {"action", "query"} or not isinstance(arguments.get("query"), str) \
                or not arguments["query"]:
            return _error("INVALID_ARGUMENT")
        query = arguments["query"].casefold()
        hits = []
        for view in reversed(self._views()):
            index = view["text"].casefold().find(query)
            if index < 0:
                continue
            start, end = max(0, index - 80), min(len(view["text"]), index + len(query) + 80)
            hit = {key: copy.deepcopy(view[key]) for key in (
                "message_id", "source_run_id", "session_seq", "source_kind", "call_id", "name"
            ) if key in view}
            hit.update(start=start, end=end, excerpt=view["text"][start:end])
            hits.append(hit)
            if len(hits) == self.SEARCH_LIMIT:
                break
        return {"ok": True, "action": "search", "hits": hits}

    def _read(self, arguments):
        allowed = {"action", "message_id"} | ({"cursor"} if "cursor" in arguments else set())
        if set(arguments) != allowed or not isinstance(arguments.get("message_id"), str):
            return _error("INVALID_ARGUMENT")
        message_id = arguments["message_id"]
        matches = [view for view in self._views() if view["message_id"] == message_id]
        if not matches:
            return _error("NOT_FOUND")
        view = matches[0]
        try:
            offset = self._decode_cursor(arguments["cursor"], message_id) if "cursor" in arguments else 0
        except ValueError:
            return _error("CURSOR_INVALID")
        if not 0 <= offset <= len(view["text"]):
            return _error("CURSOR_INVALID")
        text, end = self._slice(view["text"], offset, self.PAGE_BYTES)
        result = {"ok": True, "action": "read", "message_id": message_id,
                  "source_run_id": view["source_run_id"], "source_kind": view["source_kind"],
                  "start": offset, "end": end, "text": text}
        for key in ("call_id", "name", "result_message_id", "result"):
            if key in view:
                result[key] = copy.deepcopy(view[key])
        if end < len(view["text"]):
            result["cursor"] = self._cursor(message_id, end)
        return result

    def execute(self, arguments):
        self._proof = None
        try:
            if not isinstance(arguments, dict) or arguments.get("action") not in {"search", "read"}:
                return _error("INVALID_ARGUMENT")
            result = self._search(arguments) if arguments["action"] == "search" else self._read(arguments)
        except Exception:
            return _error("SESSION_STORE_ERROR")
        if result.get("ok") is True:
            data = {key: copy.deepcopy(value) for key, value in result.items() if key != "ok"}
            if len(_canonical({"ok": True, "data": data}).encode("utf-8")) > self.RESULT_BYTES:
                return _error("SESSION_STORE_ERROR")
            self._proof = (copy.deepcopy(arguments), data)
        return result

    def verify_success(self, arguments, data):
        proof, self._proof = self._proof, None
        if proof != (arguments, data):
            raise ValueError("history result did not come from this execution")
        return copy.deepcopy(data)
