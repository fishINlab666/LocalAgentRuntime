"""SDK-only child. No SessionStore, WebRuns, model, or tool execution here."""
import json
import logging
import time

from .feishu import MAX_CONTENT_BYTES, MAX_PACKET_BYTES, load_sdk


ACK_TIMEOUT = 3


def connection_error_code(error):
    """Return a bounded diagnostic code without forwarding exception text."""
    value = getattr(error, 'code', None)
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 999999999999:
        return 'SDK_CLIENT_ERROR_' + str(value)
    return {
        'SSLError': 'SDK_TLS_FAILED',
        'ProxyError': 'SDK_PROXY_FAILED',
        'ConnectTimeout': 'SDK_NETWORK_TIMEOUT',
        'ReadTimeout': 'SDK_NETWORK_TIMEOUT',
        'ConnectionError': 'SDK_NETWORK_FAILED',
    }.get(type(error).__name__, 'SDK_CONNECT_FAILED')


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def normalize_event(data):
    header = _field(data, 'header')
    event = _field(data, 'event')
    sender = _field(event, 'sender')
    message = _field(event, 'message')
    result = {key: _field(header, key) for key in ('app_id', 'tenant_key', 'event_id', 'event_type')}
    result.update({'open_id': _field(_field(sender, 'sender_id'), 'open_id'),
                   'sender_type': _field(sender, 'sender_type'),
                   'sender_tenant_key': _field(sender, 'tenant_key')})
    result.update({key: _field(message, key) for key in
                   ('message_id', 'chat_id', 'chat_type', 'message_type', 'content')})
    for key, value in result.items():
        limit = MAX_CONTENT_BYTES if key == 'content' else 256
        if not isinstance(value, str) or not value or len(value.encode('utf-8')) > limit:
            raise ValueError('INVALID_ENVELOPE')
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_PACKET_BYTES - 1024:
        raise ValueError('INVALID_ENVELOPE')
    return result


class DurableReceiver:
    def __init__(self, connection, app_id):
        self.connection = connection
        self.app_id = app_id
        self.sequence = 0

    def receive(self, event):
        try:
            envelope = normalize_event(event)
        except ValueError:
            self.connection.send_bytes(b'{"kind":"rejected"}')
            return
        self.sequence += 1
        packet = {'kind': 'event', 'id': self.sequence, 'envelope': envelope}
        self.connection.send_bytes(json.dumps(packet, ensure_ascii=False).encode('utf-8'))
        deadline = time.monotonic() + ACK_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.connection.poll(remaining):
                raise RuntimeError('INBOX_ACK_TIMEOUT')
            ack = json.loads(self.connection.recv_bytes(1024))
            # An earlier callback may finish after its timeout; do not reuse its ACK.
            if ack.get('kind') != 'ack' or ack.get('id') != self.sequence:
                continue
            if ack.get('ok') is not True:
                raise RuntimeError('INBOX_PERSIST_FAILED')
            return


def worker_main(connection, app_id, app_secret):
    logging.disable(logging.CRITICAL)
    try:
        sdk = load_sdk()
        receiver = DurableReceiver(connection, app_id)
        handler = (sdk.EventDispatcherHandler.builder('', '')
                   .register_p2_im_message_receive_v1(receiver.receive).build())

        class ObservedClient(sdk.ws.Client):
            # Private methods are confined here and pinned to the inspected release.
            async def _connect(self):
                await super()._connect()
                connection.send_bytes(b'{"kind":"status","state":"connected"}')

            async def _disconnect(self):
                connection.send_bytes(b'{"kind":"status","state":"reconnecting"}')
                await super()._disconnect()

        client = ObservedClient(app_id, app_secret, event_handler=handler,
                                log_level=sdk.LogLevel.ERROR)
        client.start()
    except BaseException as exc:
        code = str(exc) if isinstance(exc, RuntimeError) else ''
        if code not in ('SDK_MISSING', 'SDK_VERSION_MISMATCH'):
            code = connection_error_code(exc)
        try:
            connection.send_bytes(json.dumps({'kind': 'status', 'state': 'error',
                                               'error_code': code}).encode())
        except (OSError, EOFError):
            pass
    finally:
        connection.close()
