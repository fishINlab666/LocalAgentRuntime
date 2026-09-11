"""Bounded, session-scoped lookup over durable conversation records."""

import copy
import base64
import hashlib
import hmac
import json

from .tool_runtime import ToolSpec, wire_result


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

    def __init__(self, store, session_id: str, *, before_seq: int,
                 result_fields=None):
        if result_fields is not None and not callable(result_fields):
            raise ValueError("result_fields must be callable")
        self.store = store
        self.session_id = session_id
        self.before_seq = before_seq
        self._result_fields = result_fields or (lambda: None)
        self._omit_result_fields = False
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
                    results[(row[1], call_id)] = {
                        "message_id": row[0],
                        "session_seq": row[2],
                        "result": payload.get("result"),
                    }
        call_states = {
            (row[0], row[1]): {
                "assistant_message_id": row[2],
                "result_message_id": row[3],
                "stage": row[4],
                "recovery_state": row[5],
            }
            for row in self.store.connection().execute(
                """SELECT t.run_id, t.call_id, t.assistant_message_id,
                          t.result_message_id, t.stage, t.recovery_state
                   FROM tool_calls t
                   JOIN messages m
                     ON m.id=t.assistant_message_id
                    AND m.session_id=t.session_id
                    AND m.run_id=t.run_id
                   WHERE t.session_id=? AND m.session_seq < ?""",
                (self.session_id, self.before_seq),
            ).fetchall()
        }
        views = []
        calls_by_key = {}
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
                call_views = []
                call_links = []
                segments = []
                offset = 0
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
                    if call_views:
                        offset += 1
                    start = offset
                    offset += len(text)
                    state = call_states.get((run_id, call_id), {})
                    result = results.get((run_id, call_id))
                    result_message_id = (
                        result["message_id"] if result else state.get("result_message_id")
                    )
                    link = {
                        "call_id": call_id,
                        "name": function.get("name"),
                        "result_message_id": result_message_id,
                        "status": state.get("recovery_state")
                        if state.get("recovery_state") not in {None, "none"}
                        else state.get("stage"),
                    }
                    call_views.append(text)
                    call_links.append(link)
                    segments.append({"start": start, "end": offset,
                                     "call_id": call_id,
                                     "name": function.get("name")})
                    calls_by_key[(run_id, call_id)] = {
                        "call_message_id": message_id,
                        "name": function.get("name"),
                    }
                if call_views:
                    view = {"message_id": message_id, "source_run_id": run_id,
                            "session_seq": seq, "source_kind": "tool_call",
                            "text": "\n".join(call_views), "calls": call_links,
                            "segments": segments}
                    if len(call_links) == 1:
                        link = call_links[0]
                        view.update(link)
                        result = results.get((run_id, link["call_id"]))
                        if result:
                            view["inline_result"] = result["result"]
                    views.append(view)
        for (run_id, call_id), result in results.items():
            call = calls_by_key.get((run_id, call_id))
            if call is None:
                continue
            raw_result = result["result"]
            data = raw_result.get("data") if isinstance(raw_result, dict) else None
            if (isinstance(raw_result, dict) and raw_result.get("ok") is True
                    and isinstance(data, dict) and isinstance(data.get("content"), str)):
                text = data["content"]
            else:
                text = _canonical(raw_result)
            views.append({
                "message_id": result["message_id"],
                "source_run_id": run_id,
                "session_seq": result["session_seq"],
                "source_kind": "tool_result",
                "call_id": call_id,
                "name": call["name"],
                "call_message_id": call["call_message_id"],
                "result_message_id": result["message_id"],
                "text": text,
            })
        views.sort(key=lambda view: (view["session_seq"], view["message_id"]))
        return views

    def _signed_cursor(self, value):
        payload = _canonical(value).encode("utf-8")
        signature = hmac.new(self._cursor_key, payload, hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(payload + b"." + signature.encode("ascii")).decode("ascii")

    def _unsigned_cursor(self, cursor):
        try:
            raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
            payload, signature = raw.rsplit(b".", 1)
            expected = hmac.new(self._cursor_key, payload, hashlib.sha256).hexdigest().encode("ascii")
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, list):
                raise ValueError()
            return value
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ValueError("CURSOR_INVALID") from None

    def _cursor(self, message_id, offset):
        return self._signed_cursor([self.session_id, self.before_seq, message_id, offset])

    def _decode_cursor(self, cursor, message_id):
        value = self._unsigned_cursor(cursor)
        if (len(value) != 4
                or value[:3] != [self.session_id, self.before_seq, message_id]
                or type(value[3]) is not int):
            raise ValueError("CURSOR_INVALID")
        return value[3]

    @staticmethod
    def _query_digest(query):
        return hashlib.sha256(query.encode("utf-8")).hexdigest()

    def _search_cursor(self, query, view):
        return self._signed_cursor([
            "search-v1", self.session_id, self.before_seq,
            self._query_digest(query), view["session_seq"], view["message_id"],
        ])

    def _decode_search_cursor(self, cursor, query):
        value = self._unsigned_cursor(cursor)
        if (len(value) != 6
                or value[:4] != ["search-v1", self.session_id, self.before_seq,
                                 self._query_digest(query)]
                or type(value[4]) is not int
                or not isinstance(value[5], str)):
            raise ValueError("CURSOR_INVALID")
        return value[4], value[5]

    def _wire_bytes(self, result):
        wired = wire_result(
            result, self.result_fields(),
            user_error_codes=self.spec.user_error_codes,
        )
        return len(json.dumps(
            wired, ensure_ascii=False, allow_nan=False
        ).encode("utf-8"))

    def result_fields(self):
        return {} if self._omit_result_fields else self._result_fields()

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
        allowed = {"action", "query"} | ({"cursor"} if "cursor" in arguments else set())
        if (set(arguments) != allowed
                or not isinstance(arguments.get("query"), str)
                or not arguments["query"]):
            return _error("INVALID_ARGUMENT")
        raw_query = arguments["query"]
        query = raw_query.casefold()
        try:
            resume_before = self._decode_search_cursor(
                arguments["cursor"], raw_query
            ) if "cursor" in arguments else None
        except ValueError:
            return _error("CURSOR_INVALID")
        candidates = []
        for view in reversed(self._views()):
            key = view["session_seq"], view["message_id"]
            if resume_before is not None and key >= resume_before:
                continue
            index = view["text"].casefold().find(query)
            if index < 0:
                continue
            start, end = max(0, index - 80), min(len(view["text"]), index + len(query) + 80)
            hit = {key: copy.deepcopy(view[key]) for key in (
                "message_id", "source_run_id", "session_seq", "source_kind",
                "call_id", "name", "call_message_id", "result_message_id"
            ) if key in view}
            for segment in view.get("segments", ()):
                if segment["start"] <= index < segment["end"]:
                    hit["call_id"] = segment["call_id"]
                    hit["name"] = segment["name"]
                    link = next((item for item in view["calls"]
                                 if item["call_id"] == segment["call_id"]), None)
                    if link is not None:
                        hit["result_message_id"] = link["result_message_id"]
                    break
            hit.update(start=start, end=end, excerpt=view["text"][start:end])
            candidates.append(hit)
            if len(candidates) == self.SEARCH_LIMIT + 1:
                break
        if not candidates:
            return {"ok": True, "action": "search", "hits": []}
        for count in range(min(self.SEARCH_LIMIT, len(candidates)), 0, -1):
            result = {"ok": True, "action": "search", "hits": candidates[:count]}
            if len(candidates) > count:
                result["cursor"] = self._search_cursor(raw_query, candidates[count - 1])
            if self._wire_bytes(result) <= self.RESULT_BYTES:
                return result
        return _error("SESSION_STORE_ERROR")

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
        _, maximum_end = self._slice(view["text"], offset, self.PAGE_BYTES)

        def page(end):
            result = {"ok": True, "action": "read", "message_id": message_id,
                      "source_run_id": view["source_run_id"],
                      "source_kind": view["source_kind"],
                      "start": offset, "end": end, "text": view["text"][offset:end]}
            for key in ("call_id", "name", "call_message_id", "result_message_id",
                        "status", "calls"):
                if key in view:
                    result[key] = copy.deepcopy(view[key])
            if end < len(view["text"]):
                result["cursor"] = self._cursor(message_id, end)
            return result

        result = page(maximum_end)
        if self._wire_bytes(result) > self.RESULT_BYTES:
            low, high, best = offset + 1, maximum_end - 1, None
            while low <= high:
                middle = (low + high) // 2
                candidate = page(middle)
                if self._wire_bytes(candidate) <= self.RESULT_BYTES:
                    best = candidate
                    low = middle + 1
                else:
                    high = middle - 1
            if best is None:
                return _error("SESSION_STORE_ERROR")
            result = best
        if "inline_result" in view:
            with_inline = copy.deepcopy(result)
            with_inline["result"] = copy.deepcopy(view["inline_result"])
            if self._wire_bytes(with_inline) <= self.RESULT_BYTES:
                result = with_inline
        return result

    def execute(self, arguments):
        self._proof = None
        self._omit_result_fields = False
        try:
            if not isinstance(arguments, dict) or arguments.get("action") not in {"search", "read"}:
                return _error("INVALID_ARGUMENT")
            result = self._search(arguments) if arguments["action"] == "search" else self._read(arguments)
        except Exception:
            return _error("SESSION_STORE_ERROR")
        if result.get("ok") is True:
            data = {key: copy.deepcopy(value) for key, value in result.items() if key != "ok"}
            if self._wire_bytes(result) > self.RESULT_BYTES:
                result = _error("SESSION_STORE_ERROR")
            else:
                self._proof = (copy.deepcopy(arguments), data)
        if self._wire_bytes(result) > self.RESULT_BYTES:
            self._omit_result_fields = True
        return result

    def verify_success(self, arguments, data):
        proof, self._proof = self._proof, None
        if proof != (arguments, data):
            raise ValueError("history result did not come from this execution")
        return copy.deepcopy(data)
