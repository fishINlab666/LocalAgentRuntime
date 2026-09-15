"""Loopback-only HTTP interface for the single-file runtime."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import socket
from urllib.parse import parse_qs, quote_from_bytes, urlsplit
import webbrowser

from .provider import DeepSeekProvider
from .web_runs import WebError, WebRuns


ASSETS = Path(__file__).with_name('static')
ROUTES = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
          '/app.css': ('app.css', 'text/css')}
IMPORT_ID = re.compile(r"[0-9a-f]{32}\Z")


class ContentLengthReader:
    def __init__(self, stream, length):
        self.stream = stream
        self.remaining = length

    def read(self, size=-1):
        if self.remaining == 0:
            return b""
        amount = self.remaining if size is None or size < 0 else min(size, self.remaining)
        raw = self.stream.read(amount)
        if not isinstance(raw, bytes):
            return raw
        self.remaining -= len(raw)
        return raw


class WebServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, handler, runs):
        self.runs = runs
        super().__init__(address, handler)

    def server_close(self):
        super().server_close()
        self.runs.close()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass

    def finish(self):
        try:
            super().finish()
        finally:
            self.server.runs.close_thread_connection()

    def respond(self, status, value, content_type='application/json'):
        if content_type == 'application/json':
            value = json.dumps(value, ensure_ascii=False)
        body = value.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
                         "frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def respond_file(self, artifact):
        name = artifact.download_name
        try:
            if (not isinstance(name, str) or not name or '/' in name or '\\' in name
                    or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)):
                raise WebError(409, 'IMPORT_INTEGRITY_ERROR')
            if (not isinstance(artifact.content, bytes)
                    or len(artifact.content) != artifact.length):
                raise WebError(409, 'IMPORT_INTEGRITY_ERROR')
            encoded = quote_from_bytes(name.encode('utf-8'), safe='')
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(artifact.length))
            self.send_header('Content-Disposition',
                             f"attachment; filename=\"artifact\"; filename*=UTF-8''{encoded}")
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                             "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(artifact.content)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def authorize(self, api=False):
        if self.headers.get_all('Host') != [self.server.origin.removeprefix('http://')]:
            raise WebError(403, 'ORIGIN_DENIED')
        origins = self.headers.get_all('Origin') or []
        if len(origins) > 1 or origins and origins[0] != self.server.origin:
            raise WebError(403, 'ORIGIN_DENIED')
        if self.command == 'POST' and origins != [self.server.origin]:
            raise WebError(403, 'ORIGIN_DENIED')
        if api:
            tokens = self.headers.get_all('X-Session-Token') or []
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.server.token):
                raise WebError(403, 'SESSION_EXPIRED')

    def require_content_type(self, expected):
        values = self.headers.get_all('Content-Type') or []
        if len(values) != 1 or values[0].casefold() != expected:
            raise WebError(415, 'JSON_REQUIRED' if expected == 'application/json'
                           else 'BINARY_REQUIRED')

    def content_length(self, maximum, *, allow_zero=False):
        if self.headers.get_all('Transfer-Encoding') is not None:
            self.close_connection = True
            raise WebError(400, 'INVALID_REQUEST')
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1:
            self.close_connection = True
            raise WebError(400, 'INVALID_REQUEST')
        raw = lengths[0]
        if (not raw.isascii() or not re.fullmatch(r"0|[1-9][0-9]*", raw)
                or len(raw) > 20):
            self.close_connection = True
            raise WebError(400, 'INVALID_REQUEST')
        length = int(raw)
        if length == 0 and not allow_zero:
            self.close_connection = True
            raise WebError(400, 'INVALID_REQUEST')
        if length > maximum:
            self.close_connection = True
            raise WebError(413, 'REQUEST_TOO_LARGE')
        return length

    def read_json(self, maximum):
        self.require_content_type('application/json')
        length = self.content_length(maximum)
        raw = ContentLengthReader(self.rfile, length).read()
        if len(raw) != length:
            self.close_connection = True
            raise WebError(400, 'INVALID_REQUEST')

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError('duplicate key')
                value[key] = item
            return value

        def reject_constant(_value):
            raise ValueError('invalid JSON constant')

        try:
            return json.loads(
                raw.decode('utf-8', errors='strict'),
                object_pairs_hook=unique,
                parse_constant=reject_constant,
            )
        except (ValueError, UnicodeError, RecursionError):
            raise WebError(400, 'INVALID_REQUEST') from None

    @staticmethod
    def import_route(path):
        parts = path.split('/')
        if path == '/api/imports':
            return 'begin', None, None
        if (len(parts) == 4 and parts[1:3] == ['api', 'imports']
                and IMPORT_ID.fullmatch(parts[3])):
            return 'snapshot', parts[3], None
        if (len(parts) == 5 and parts[1:3] == ['api', 'imports']
                and IMPORT_ID.fullmatch(parts[3])
                and parts[4] in {'complete', 'cancel'}):
            return parts[4], parts[3], None
        if (len(parts) == 6 and parts[1:3] == ['api', 'imports']
                and IMPORT_ID.fullmatch(parts[3]) and parts[4] == 'files'
                and IMPORT_ID.fullmatch(parts[5])):
            return 'file', parts[3], parts[5]
        return None

    def dispatch(self):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise WebError(400, 'INVALID_REQUEST')
        path = parsed.path
        api = path.startswith('/api/')
        self.authorize(api)
        parts = path.split('/')
        import_route = self.import_route(path)
        import_prefix = path == '/api/imports' or path.startswith('/api/imports/')
        if import_prefix and import_route is None:
            raise WebError(404, 'NOT_FOUND')
        if import_route is not None:
            agent_headers = self.headers.get_all('X-Agent-ID') or []
            if len(agent_headers) != 1 or not agent_headers[0]:
                raise WebError(404, 'NOT_FOUND')
            agent_id = agent_headers[0]
            if parsed.query:
                raise WebError(400, 'INVALID_REQUEST')
        else:
            agent_id = self.headers.get('X-Agent-ID') or None
        if len(parts) >= 4 and parts[1:3] == ['api', 'sessions']:
            self.server.runs.require_agent_session(parts[3], agent_id)
        if self.command == 'GET':
            if parsed.query and path not in {'/api/sessions'} and not (
                    path.startswith('/api/sessions/') and path.endswith('/runs')):
                raise WebError(400, 'INVALID_REQUEST')
            if path in ROUTES:
                name, kind = ROUTES[path]
                content = (ASSETS / name).read_text(encoding='utf-8')
                if name == 'index.html':
                    content = content.replace('__SESSION_TOKEN__', self.server.token)
                return self.respond(200, content, kind)
            if path == '/api/config':
                return self.respond(200, self.server.runs.config())
            if path == '/api/agents':
                return self.respond(200, self.server.runs.list_agents())
            if path == '/api/capabilities':
                return self.respond(200, self.server.runs.list_capabilities(agent_id))
            if import_route is not None and import_route[0] == 'snapshot':
                return self.respond(200, self.server.runs.import_snapshot(
                    import_route[1], agent_id))
            if path.startswith('/api/runs/'):
                return self.respond(200, self.server.runs.snapshot(path.removeprefix('/api/runs/')))
            query = parse_qs(parsed.query, keep_blank_values=True)
            parts = path.split('/')
            if path == '/api/sessions':
                if set(query) - {'archived', 'cursor'} or any(len(value) != 1 for value in query.values()):
                    raise WebError(400, 'INVALID_REQUEST')
                archived_value = query.get('archived', ['0'])[0]
                if archived_value not in {'0', '1'}:
                    raise WebError(400, 'INVALID_REQUEST')
                return self.respond(200, self.server.runs.list_sessions(
                    archived=archived_value == '1', cursor=query.get('cursor', [None])[0], agent_id=agent_id))
            if len(parts) == 4 and parts[1:3] == ['api', 'sessions']:
                return self.respond(200, self.server.runs.session_view(parts[3]))
            if len(parts) == 5 and parts[1:3] == ['api', 'sessions'] and parts[4] == 'runs':
                if set(query) - {'cursor'} or any(len(value) != 1 for value in query.values()):
                    raise WebError(400, 'INVALID_REQUEST')
                return self.respond(200, self.server.runs.list_session_runs(
                    parts[3], cursor=query.get('cursor', [None])[0]))
            if (len(parts) == 9 and parts[1:3] == ['api', 'sessions']
                    and parts[4] == 'runs' and parts[6] == 'artifacts'
                    and parts[8] == 'download'):
                return self.respond_file(self.server.runs.open_artifact(
                    parts[3], parts[5], parts[7], agent_id))
            if (len(parts) == 6 and parts[1:3] == ['api', 'sessions']
                    and parts[4] == 'runs'):
                return self.respond(200, self.server.runs.session_snapshot(parts[3], parts[5]))
            raise WebError(404, 'NOT_FOUND')
        if not api:
            raise WebError(404, 'NOT_FOUND')
        if import_route is not None:
            action, import_id, slot_id = import_route
            if action == 'begin':
                data = self.read_json(128 * 1024)
                return self.respond(
                    201, self.server.runs.begin_import(data, agent_id))
            if action == 'file':
                length = self.content_length(20 * 1024 * 1024, allow_zero=True)
                self.require_content_type('application/octet-stream')
                self.close_connection = True
                stream = ContentLengthReader(self.rfile, length)
                return self.respond(200, self.server.runs.upload_import_file(
                    import_id, slot_id, stream, length, agent_id))
            if action in {'complete', 'cancel'}:
                data = self.read_json(16384)
                if data != {}:
                    raise WebError(400, 'INVALID_REQUEST')
                if action == 'complete':
                    value, accepted = self.server.runs.complete_import(
                        import_id, agent_id)
                    return self.respond(202 if accepted else 200, value)
                return self.respond(
                    200, self.server.runs.cancel_import(import_id, agent_id))
            raise WebError(404, 'NOT_FOUND')
        data = self.read_json(16384)
        if path == '/api/agents':
            return self.respond(200, self.server.runs.save_agent(data))
        if len(parts) == 5 and parts[1:3] == ['api', 'agents']:
            if parts[4] == 'bind':
                return self.respond(200, self.server.runs.bind_capability(parts[3], data))
            if parts[4] == 'enabled' and isinstance(data, dict) and set(data) == {'enabled'}:
                return self.respond(200, self.server.runs.set_agent_enabled(parts[3], data['enabled']))
        if len(parts) in {4, 5} and parts[1] == 'api' and parts[2] in {'skills', 'mcp'}:
            return self.respond(200, self.server.runs.capability_action(
                'skill' if parts[2] == 'skills' else 'mcp',
                parts[3] if len(parts) == 5 else None, parts[-1], data))
        if path == '/api/runs':
            return self.respond(202, self.server.runs.start(data))
        if path == '/api/sessions':
            return self.respond(201, self.server.runs.create_session(data))
        parts = path.split('/')
        if len(parts) == 5 and parts[1:3] == ['api', 'runs'] and parts[4] == 'cancel':
            if data != {}:
                raise WebError(400, 'INVALID_REQUEST')
            return self.respond(200, self.server.runs.cancel(parts[3]))
        if len(parts) == 6 and parts[1:3] == ['api', 'runs'] and parts[4] == 'approvals':
            if not isinstance(data, dict) or set(data) != {'decision'}:
                raise WebError(400, 'INVALID_REQUEST')
            return self.respond(200, self.server.runs.decide(parts[3], parts[5], data['decision']))
        if (len(parts) == 5 and parts[1:3] == ['api', 'sessions']
                and parts[4] in {'rename', 'archive', 'restore'}):
            return self.respond(200, self.server.runs.change_session(parts[3], parts[4], data))
        if (len(parts) == 5 and parts[1:3] == ['api', 'sessions']
                and parts[4] == 'runs'):
            value, created = self.server.runs.start_session_run(parts[3], data)
            return self.respond(202 if created else 200, value)
        if (len(parts) == 5 and parts[1:3] == ['api', 'sessions']
                and parts[4] == 'continue'):
            value, created = self.server.runs.continue_session(parts[3], data)
            return self.respond(202 if created else 200, value)
        if (len(parts) == 7 and parts[1:3] == ['api', 'sessions']
                and parts[4] == 'runs' and parts[6] == 'cancel'):
            if data != {}:
                raise WebError(400, 'INVALID_REQUEST')
            return self.respond(200, self.server.runs.cancel_session(parts[3], parts[5]))
        if (len(parts) == 8 and parts[1:3] == ['api', 'sessions']
                and parts[4] == 'runs' and parts[6] == 'approvals'):
            if not isinstance(data, dict) or set(data) != {'decision'}:
                raise WebError(400, 'INVALID_REQUEST')
            return self.respond(200, self.server.runs.decide_session(
                parts[3], parts[5], parts[7], data['decision']))
        raise WebError(404, 'NOT_FOUND')

    def do_GET(self):
        try:
            self.dispatch()
        except WebError as error:
            self.respond(error.status, {'error': error.code})
        except (TimeoutError, socket.timeout):
            self.respond(408, {'error': 'REQUEST_TIMEOUT'})
        except Exception:
            self.respond(500, {'error': 'LOCAL_SERVER_ERROR'})

    do_POST = do_GET


def create_server(workspace, directory, *, state_dir=None, session_id=None, port=8765,
                  provider_factory=DeepSeekProvider.from_env):
    runs = WebRuns(workspace, directory, provider_factory, state_dir=state_dir,
                   selected_session_id=session_id)
    server = WebServer(('127.0.0.1', port), Handler, runs)
    server.token = secrets.token_hex(32)
    server.origin = f'http://127.0.0.1:{server.server_port}'
    return server


def serve(workspace, directory, *, state_dir=None, session_id=None, port=8765,
          provider_factory=DeepSeekProvider.from_env, open_browser=False):
    server = create_server(workspace, directory, state_dir=state_dir,
                           session_id=session_id, port=port,
                           provider_factory=provider_factory)
    print(f'本地资料会话：{server.origin}\n工作区：{server.runs.workspace}\n按 Ctrl+C 关闭服务。', flush=True)
    if open_browser:
        webbrowser.open(server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
