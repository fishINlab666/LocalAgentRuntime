"""Loopback-only HTTP interface for the single-file runtime."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .provider import DeepSeekProvider
from .web_runs import WebError, WebRuns


ASSETS = Path(__file__).with_name('static')
ROUTES = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
          '/app.css': ('app.css', 'text/css')}


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, runs):
        self.runs = runs
        super().__init__(address, handler)

    def server_close(self):
        self.runs.close()
        super().server_close()


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

    def authorize(self, api=False):
        if self.headers.get_all('Host') != [self.server.origin.removeprefix('http://')]:
            raise WebError(403, 'ORIGIN_DENIED')
        origin = self.headers.get('Origin')
        if origin is not None and origin != self.server.origin:
            raise WebError(403, 'ORIGIN_DENIED')
        if self.command == 'POST' and origin != self.server.origin:
            raise WebError(403, 'ORIGIN_DENIED')
        if api and not hmac.compare_digest(self.headers.get('X-Session-Token', ''), self.server.token):
            raise WebError(403, 'SESSION_EXPIRED')

    def dispatch(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        api = path.startswith('/api/')
        self.authorize(api)
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
                    archived=archived_value == '1', cursor=query.get('cursor', [None])[0]))
            if len(parts) == 4 and parts[1:3] == ['api', 'sessions']:
                return self.respond(200, self.server.runs.session_view(parts[3]))
            if len(parts) == 5 and parts[1:3] == ['api', 'sessions'] and parts[4] == 'runs':
                if set(query) - {'cursor'} or any(len(value) != 1 for value in query.values()):
                    raise WebError(400, 'INVALID_REQUEST')
                return self.respond(200, self.server.runs.list_session_runs(
                    parts[3], cursor=query.get('cursor', [None])[0]))
            if (len(parts) == 6 and parts[1:3] == ['api', 'sessions']
                    and parts[4] == 'runs'):
                return self.respond(200, self.server.runs.session_snapshot(parts[3], parts[5]))
            raise WebError(404, 'NOT_FOUND')
        if not api:
            raise WebError(404, 'NOT_FOUND')
        if self.headers.get_content_type() != 'application/json':
            raise WebError(415, 'JSON_REQUIRED')
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get('Transfer-Encoding'):
            raise WebError(400, 'INVALID_REQUEST')
        length = int(lengths[0])
        if not 0 < length <= 16384:
            raise WebError(413, 'REQUEST_TOO_LARGE')
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError, RecursionError):
            raise WebError(400, 'INVALID_REQUEST') from None
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
