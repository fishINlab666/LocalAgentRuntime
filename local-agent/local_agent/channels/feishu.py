"""Optional Feishu transport; channel authorization and storage live elsewhere."""
import importlib
import importlib.metadata
import json
import logging
import math
import multiprocessing
import re
import threading
import time
from collections.abc import Callable


SDK_VERSION = '1.7.3'
MAX_CONTENT_BYTES = 32768
MAX_PACKET_BYTES = 65536
STARTUP_TIMEOUT = 20


def silence_sdk_logging():
    # The SDK changes its logger level in builders; disabled survives those calls.
    logger = logging.getLogger('Lark')
    logger.disabled = True
    logger.propagate = False
    logger.handlers = [logging.NullHandler()]


def require_sdk_version():
    try:
        version = importlib.metadata.version('lark-oapi')
    except importlib.metadata.PackageNotFoundError:
        raise RuntimeError('SDK_MISSING') from None
    if version != SDK_VERSION:
        raise RuntimeError('SDK_VERSION_MISMATCH')


def load_sdk():
    silence_sdk_logging()
    require_sdk_version()
    sdk = importlib.import_module('lark_oapi')
    silence_sdk_logging()
    return sdk


def _result(state, code=None, message_id=None, retry_after=None):
    return {'state': state, 'message_id': message_id,
            'error_code': code, 'retry_after': retry_after}


class FeishuTransport:
    def __init__(self, app_id: str, app_secret: str, receive_callback: Callable):
        if not isinstance(app_id, str) or not app_id or not isinstance(app_secret, str) or not app_secret:
            raise ValueError('FEISHU_CREDENTIALS_REQUIRED')
        self._app_id = app_id
        self._app_secret = app_secret
        self._receive_callback = receive_callback
        self._process = None
        self._connection = None
        self._reader = None
        self._client = None
        self._sdk = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._state = 'stopped'
        self._error_code = None
        self._started_at = None
        self._rejected = 0

    def _set_state(self, state, error_code=None):
        with self._lock:
            self._state = state
            self._error_code = error_code

    def status(self):
        with self._lock:
            state, code = self._state, self._error_code
            rejected = self._rejected
        if state not in ('stopped', 'error') and self._process is not None and not self._process.is_alive():
            state, code = 'error', 'SDK_PROCESS_EXITED'
        return {'state': state, 'connected': state == 'connected',
                'error_code': code, 'rejected_count': rejected}

    def start(self):
        if self._process is not None and self._process.is_alive():
            return
        if self._reader is not None and self._reader.is_alive():
            raise RuntimeError('TRANSPORT_READER_BUSY')
        try:
            require_sdk_version()
        except RuntimeError as exc:
            self._set_state('error', str(exc))
            raise
        if self._connection is not None:
            self._connection.close()
        from .feishu_worker import worker_main
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe(duplex=True)
        self._connection = parent
        self._process = context.Process(target=worker_main,
                                        args=(child, self._app_id, self._app_secret),
                                        name='feishu-transport', daemon=True)
        self._stop.clear()
        self._started_at = time.monotonic()
        self._set_state('starting')
        try:
            self._process.start()
        except Exception:
            parent.close()
            child.close()
            self._set_state('error', 'SDK_PROCESS_START_FAILED')
            raise RuntimeError('SDK_PROCESS_START_FAILED') from None
        child.close()
        self._reader = threading.Thread(target=self._read_loop,
                                        name='feishu-inbox', daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            while not self._stop.is_set():
                if self._connection.poll(0.1):
                    packet = json.loads(self._connection.recv_bytes(MAX_PACKET_BYTES))
                    self._handle_packet(packet)
                elif not self._process.is_alive():
                    if self._state != 'error':
                        self._set_state('error', 'SDK_PROCESS_EXITED')
                    break
                elif self._state == 'starting' and time.monotonic() - self._started_at > STARTUP_TIMEOUT:
                    self._set_state('error', 'SDK_STARTUP_TIMEOUT')
                    self._process.terminate()
                    break
        except (EOFError, OSError, ValueError, TypeError):
            if not self._stop.is_set() and self._state != 'error':
                self._set_state('error', 'SDK_IPC_CLOSED')

    def _handle_packet(self, packet):
        if not isinstance(packet, dict):
            raise ValueError('INVALID_IPC_PACKET')
        kind = packet.get('kind')
        if kind == 'status':
            state = packet.get('state')
            if state in ('connected', 'reconnecting', 'error'):
                allowed = {'SDK_MISSING', 'SDK_VERSION_MISMATCH', 'SDK_CONNECT_FAILED',
                           'SDK_PROCESS_FAILED', 'SDK_TLS_FAILED', 'SDK_PROXY_FAILED',
                           'SDK_NETWORK_TIMEOUT', 'SDK_NETWORK_FAILED'}
                error = packet.get('error_code')
                safe_client_code = (isinstance(error, str)
                                    and re.fullmatch(r'SDK_CLIENT_ERROR_[0-9]{1,12}', error))
                self._set_state(state, error if error in allowed or safe_client_code else None)
        elif kind == 'rejected':
            with self._lock:
                self._rejected += 1
        elif kind == 'event':
            ok = False
            try:
                if not self._stop.is_set() and isinstance(packet.get('envelope'), dict):
                    ok = self._receive_callback(packet['envelope']) is not False
            except Exception:
                # Error text can contain input or paths; only a bounded code crosses IPC.
                ok = False
            self._connection.send_bytes(json.dumps({'kind': 'ack', 'id': packet.get('id'),
                                                    'ok': ok}).encode())

    def send(self, chat_id: str, text: str, uuid: str):
        values = (chat_id, text, uuid)
        try:
            text_size = len(text.encode('utf-8')) if isinstance(text, str) else 0
        except UnicodeError:
            return _result('failed', 'INVALID_SEND_REQUEST')
        if (not all(isinstance(value, str) and value for value in values)
                or len(chat_id) > 256 or len(uuid) > 64
                or text_size > MAX_CONTENT_BYTES):
            return _result('failed', 'INVALID_SEND_REQUEST')
        try:
            with self._send_lock:
                response = self._send_request(chat_id, text, uuid)
        except RuntimeError as exc:
            if str(exc) in ('SDK_MISSING', 'SDK_VERSION_MISMATCH'):
                return _result('failed', str(exc))
            return _result('unknown', 'SEND_EXCEPTION')
        except Exception:
            return _result('unknown', 'SEND_EXCEPTION')
        status = getattr(getattr(response, 'raw', None), 'status_code', None)
        code = getattr(response, 'code', None)
        message_id = getattr(getattr(response, 'data', None), 'message_id', None)
        if isinstance(status, int) and 200 <= status < 300 and code == 0:
            if isinstance(message_id, str) and 0 < len(message_id) <= 256:
                return _result('accepted', message_id=message_id)
            return _result('unknown', 'MISSING_MESSAGE_ID')
        if status == 429 or code == 99991402:
            headers = getattr(getattr(response, 'raw', None), 'headers', {}) or {}
            retry_after = None
            try:
                value = next((v for k, v in headers.items() if k.lower() == 'retry-after'), None)
                parsed = float(value)
                if math.isfinite(parsed) and parsed >= 0:
                    retry_after = parsed
            except (TypeError, ValueError):
                pass
            return _result('retry_wait', 'RATE_LIMITED', retry_after=retry_after)
        if not isinstance(status, int) or status >= 500 or code is None:
            return _result('unknown', 'SEND_RESPONSE_UNKNOWN')
        return _result('failed', 'SEND_REJECTED')

    def _send_request(self, chat_id, text, uuid):
        if self._client is None:
            self._sdk = load_sdk()
            self._client = (self._sdk.Client.builder().app_id(self._app_id)
                            .app_secret(self._app_secret).timeout(8)
                            .log_level(self._sdk.LogLevel.ERROR).build())
            silence_sdk_logging()
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = (CreateMessageRequestBody.builder().receive_id(chat_id).msg_type('text')
                .content(json.dumps({'text': text}, ensure_ascii=False)).uuid(uuid).build())
        request = (CreateMessageRequest.builder().receive_id_type('chat_id')
                   .request_body(body).build())
        return self._client.im.v1.message.create(request)

    def close(self):
        self._stop.set()
        process = self._process
        if process is not None and process.is_alive():
            process.terminate()
            process.join(1)
            if process.is_alive():
                process.kill()
                process.join(1)
        elif process is not None and process.pid is not None:
            process.join(0)
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(1)
        if self._connection is not None:
            self._connection.close()
        if self._reader is not None and self._reader.is_alive():
            self._set_state('error', 'INBOX_CALLBACK_STILL_RUNNING')
        else:
            self._set_state('stopped')
