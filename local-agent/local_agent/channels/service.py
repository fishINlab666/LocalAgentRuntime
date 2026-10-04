"""One authorized Feishu binding feeds the existing WebRuns execution manager."""
import json
import os
import re
import threading
import time
from urllib.parse import urlencode, urlsplit

from .config import validate_config
from .store import ChannelStore, ChannelError
from ..web_runs import WebError


HELP = ('普通文字：询问本会话资料；/记录 问题：追问会话记录；'
        '/报告 相对路径.md 换行后填写问题；/状态 [完整任务编号]；/取消 完整任务编号。'
        '仅支持本人私聊文字，附件请先在本机工作台导入；撤回消息不会取消任务。'
        '审批请到本机工作台完成。')
ACTIVE = {'queued', 'running', 'waiting_approval'}
PROGRESS = ('received', 'running', 'waiting_approval')
LABELS = {'completed': '已完成', 'failed': '执行失败', 'cancelled': '已取消',
          'interrupted': '执行中断', 'validation_failed': '答案验证未通过',
          'waiting_approval': '等待本机审批', 'running': '执行中', 'queued': '等待执行'}


def parse_request(envelope):
    if envelope.get('message_type') != 'text':
        return {'action': 'reject', 'error_code': 'UNSUPPORTED_MESSAGE',
                'reply': '暂不支持此输入，请先在本机工作台导入资料。'}
    text = envelope.get('text')
    if text is None:
        try:
            content = json.loads(envelope.get('content', ''))
            text = content['text']
        except (ValueError, KeyError, TypeError, RecursionError):
            text = None
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return {'action': 'reject', 'error_code': 'INVALID_QUESTION', 'reply': '请输入 1–4000 字的文字任务。'}
    text = text.strip()
    command, _, argument = text.partition(' ')
    if text == '/状态' or command == '/状态':
        return {'action': 'status', 'run_id': argument.strip() or None}
    if command == '/取消' and re.fullmatch(r'[0-9a-f]{32}', argument.strip()):
        return {'action': 'cancel', 'run_id': argument.strip()}
    if text in {'同意', '确认写入'}:
        return {'action': 'approval_hint'}
    if text.startswith('/报告 '):
        header, newline, question = text.partition('\n')
        path = header[len('/报告 '):].strip()
        if not newline or not question.strip():
            return {'action': 'reject', 'error_code': 'INVALID_REPORT', 'reply': HELP}
        try:
            from ..web_runs import WebRuns
            WebRuns.validate_output({'output_file': path}, None)
        except WebError:
            return {'action': 'reject', 'error_code': 'INVALID_OUTPUT_FILE',
                    'reply': '输出必须是允许范围内的 .md/.txt 相对路径，且父目录已经存在。'}
        return {'action': 'run', 'task_type': 'files', 'question': question.strip(), 'output_file': path}
    if text.startswith('/记录 '):
        if not argument.strip():
            return {'action': 'reject', 'error_code': 'INVALID_QUESTION', 'reply': HELP}
        return {'action': 'run', 'task_type': 'conversation', 'question': argument.strip()}
    if text.startswith('/'):
        return {'action': 'reject', 'error_code': 'UNKNOWN_COMMAND', 'reply': HELP}
    return {'action': 'run', 'task_type': 'files', 'question': text}


class ChannelService:
    def __init__(self, runs, config, origin, *, send=None):
        parsed = urlsplit(origin)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or not parsed.port or parsed.path or parsed.query or parsed.fragment):
            raise ChannelError('INVALID_LOCAL_ORIGIN')
        self.runs, self.origin = runs, origin
        self.store = ChannelStore(runs.store)
        self.send = send
        self.transport = None
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.threads = []
        self.log_lock = threading.Lock()
        self.delivery_lock = threading.RLock()
        self.log_path = runs.store.state_dir / 'channel-events.jsonl'
        self.configure(config)
        self.recover()

    def log(self, event, **fields):
        allowed = {'event_id', 'run_id', 'session_id', 'state', 'code', 'outbox_id',
                   'attempts', 'revision', 'message_id'}
        record = {'time': time.time(), 'event': event}
        record.update({k: v for k, v in fields.items() if k in allowed})
        raw = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
        with self.log_lock:
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, raw)
            finally:
                os.close(fd)

    def configure(self, config):
        config = validate_config(config)
        with self.delivery_lock, self.runs.lock:
            self.runs.require_agent_session(config['session_id'], config['agent_id'])
            previous = self.store.binding()
            binding = self.store.configure(config)
            if previous and previous['revision'] != binding['revision']:
                for event in self.store.events(states=['dispatched']):
                    if event['binding_revision'] == previous['revision'] and event.get('run_id'):
                        self.runs.cancel_session(event['session_id'], event['run_id'])
            return binding

    def _current(self, event):
        current = self.store.binding()
        return (current and current['enabled'] and current['id'] == event['binding_id']
                and current['revision'] == event['binding_revision'])

    def receive(self, envelope):
        """Return only after durable receipt; persistence errors propagate to SDK ACK."""
        if self.stopped.is_set():
            raise ChannelError('CHANNEL_STOPPED')
        if not isinstance(envelope, dict):
            self.log('rejected', code='INVALID_ENVELOPE')
            return None
        with self.runs.lock:
            binding = self.store.binding()
            required = ('app_id', 'tenant_key', 'sender_tenant_key', 'open_id', 'chat_id',
                        'event_id', 'message_id', 'event_type')
            valid_ids = all(isinstance(envelope.get(k), str)
                            and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', envelope[k]) for k in required)
            authorized = (valid_ids and binding and binding['enabled']
                          and all(envelope.get(k) == binding[k] for k in
                                  ('app_id', 'tenant_key', 'open_id', 'chat_id'))
                          and envelope['sender_tenant_key'] == binding['tenant_key']
                          and envelope.get('sender_type') == 'user'
                          and envelope.get('chat_type') == 'p2p'
                          and envelope.get('event_type') == 'im.message.receive_v1')
            if not authorized:
                self.log('rejected', code='IDENTITY_DENIED')
                return None
            self.runs.require_agent_session(binding['session_id'], binding['agent_id'])
            request = parse_request(envelope)
            # Store only a normalized bounded text payload; never SDK headers/tokens.
            clean = {k: envelope.get(k, '') for k in required}
            clean['message_type'] = envelope.get('message_type', '')[:64]
            # Commands also need conflict detection even though they contain no question.
            clean['text'] = json.dumps(request, ensure_ascii=False, sort_keys=True)
            try:
                event, created = self.store.receive(binding, clean, request)
            except ChannelError as error:
                if error.code in {'CHANNEL_EVENT_CONFLICT', 'EVENT_CONFLICT'}:
                    self.log('rejected', code='EVENT_CONFLICT')
                    return None
                raise
            if created and request['action'] == 'run':
                try:
                    self.runs._check_start()
                    other = [e for e in self.store.events(states=['received', 'dispatching'])
                             if e['id'] != event['id'] and e['request'].get('action') == 'run']
                    if other:
                        raise WebError(409, 'RUN_ACTIVE')
                except WebError as error:
                    self.reject(event, error.code, '当前任务尚未结束，请结束后另发一条新消息。')
                    event = self.store.event(event['id'])
            self.log('received' if created else 'duplicate', event_id=event['id'],
                     session_id=event['session_id'], revision=event['binding_revision'])
        self.wake.set()
        return event

    def link(self, event, run_id=None):
        args = {'agent': event['agent_id'], 'session': event['session_id']}
        if run_id or event.get('run_id'):
            args['run'] = run_id or event['run_id']
        return self.origin + '/#' + urlencode(args)

    def reject(self, event, code, body):
        self.store.update_event(event['id'], 'rejected', error_code=code)
        self.store.suppress(event['id'], kinds=PROGRESS)
        self.store.enqueue(event['id'], 'rejected', body)
        self.log('rejected', event_id=event['id'], code=code)

    def recover(self):
        self.store.recover_delivery()
        for event in self.store.events(states=['received', 'dispatching']):
            row = self.runs.store.connection().execute(
                'SELECT id FROM runs WHERE session_id=? AND client_request_id=?',
                (event['session_id'], event['client_request_id'])).fetchone()
            if row:
                self.store.update_event(event['id'], 'dispatched', run_id=row[0])
                self.log('reconciled', event_id=event['id'], run_id=row[0])
            else:
                self.store.update_event(event['id'], 'interrupted_before_dispatch',
                                        error_code='INTERRUPTED_BEFORE_DISPATCH')
                self.store.suppress(event['id'], kinds=PROGRESS)
                if self._current(event):
                    self.store.enqueue(event['id'], 'interrupted', '请求在启动前中断，未执行。请重新发起。')

    def dispatch(self, event):
        with self.runs.lock:
            if not self._current(event):
                self.store.update_event(event['id'], 'invalidated')
                self.store.suppress(event['id'])
                return
            self.runs.require_agent_session(event['session_id'], event['agent_id'])
            request = event['request']
            action = request['action']
            if action == 'reject':
                return self.reject(event, request['error_code'], request['reply'])
            if action != 'run':
                return self.command(event, request)
            self.store.update_event(event['id'], 'dispatching')
            existing = self.runs.store.connection().execute(
                'SELECT id FROM runs WHERE session_id=? AND client_request_id=?',
                (event['session_id'], event['client_request_id'])).fetchone()
            if existing:
                run_id = existing[0]
            else:
                data = {k: v for k, v in request.items() if k != 'action'}
                data['client_request_id'] = event['client_request_id']
                try:
                    value, _ = self.runs.start_session_run(event['session_id'], data)
                except WebError as error:
                    return self.reject(event, error.code, '任务未启动，请在本机工作台检查范围或结束当前任务。')
                run_id = value['run']['id']
            self.store.update_event(event['id'], 'dispatched', run_id=run_id)
            self.log('dispatched', event_id=event['id'], session_id=event['session_id'], run_id=run_id)

    def command(self, event, request):
        run_id = request.get('run_id')
        if run_id is None:
            events = [e for e in self.store.events() if e.get('run_id') and self._current(e)]
            target = max(events, key=lambda e: e['received_at'], default=None)
        else:
            target = self.store.find_run(event['binding_id'], event['binding_revision'], run_id)
        if not target:
            body = '没有此绑定可操作的任务。' + HELP
        elif request['action'] == 'approval_hint':
            body = '文字不能批准写入，请在本机工作台查看并确认。\n' + self.link(target)
        else:
            if request['action'] == 'cancel':
                state = self.runs.cancel_session(target['session_id'], target['run_id'])['run']['state']
                body = ('已请求取消；已生成的文件不会删除。\n' if state in ACTIVE else
                        '任务状态：' + LABELS.get(state, '已停止') + '；任务已结束，无需再次取消。\n')
            else:
                state = self.runs.session_snapshot(target['session_id'], target['run_id'])['run']['state']
                body = '任务状态：' + LABELS.get(state, '已停止') + '\n'
            body += '任务编号：' + target['run_id'] + '\n' + self.link(target)
        self.store.suppress(event['id'], kinds=PROGRESS)
        self.store.enqueue(event['id'], 'command', body)
        self.store.update_event(event['id'], 'handled')

    def _adopt_continuations(self):
        rows = self.runs.store.connection().execute('''
            SELECT r.id AS child_id, e.id AS parent_event FROM runs r
            JOIN channel_events e ON r.parent_run_id=e.run_id AND r.session_id=e.session_id
            WHERE NOT EXISTS(SELECT 1 FROM channel_events c WHERE c.run_id=r.id) LIMIT 32
        ''').fetchall()
        for child_id, parent_id in rows:
            parent = self.store.event(parent_id)
            if not self._current(parent):
                continue
            binding = parent['binding']
            envelope = {k: binding[k] for k in ('app_id', 'tenant_key', 'open_id', 'chat_id')}
            envelope.update(event_id='continue:' + child_id, message_id='continue:' + child_id,
                            event_type='local.continue', message_type='control', text='')
            event, _ = self.store.receive(binding, envelope, {'action': 'observe'})
            self.store.update_event(event['id'], 'dispatched', run_id=child_id)

    def sync(self):
        self._adopt_continuations()
        for event in self.store.events(states=['dispatched']):
            if not self._current(event):
                self.store.suppress(event['id'])
                continue
            try:
                run = self.runs.session_snapshot(event['session_id'], event['run_id'])['run']
            except WebError:
                continue
            state = run['state']
            if state in ACTIVE:
                kind = 'waiting_approval' if state == 'waiting_approval' else 'running'
                if kind == 'waiting_approval':
                    self.store.suppress(event['id'], kinds=('received', 'running'))
                self.store.enqueue(event['id'], kind, LABELS[state] + '。\n' + self.link(event),
                                   run_id=event['run_id'])
            else:
                count = self.runs.store.connection().execute('SELECT COUNT(*) FROM artifacts WHERE run_id=?',
                                                            (event['run_id'],)).fetchone()[0]
                body = (f"任务状态：{LABELS.get(state, '已停止')}；已登记产物 {count} 份。"
                        f"\n任务编号：{event['run_id']}\n请在本机核对答案和文件：{self.link(event)}")
                self.store.suppress(event['id'], kinds=PROGRESS)
                self.store.enqueue(event['id'], 'terminal', body, run_id=event['run_id'])
                self.store.update_event(event['id'], 'terminal')
                self.log('run.terminal', event_id=event['id'], run_id=event['run_id'], state=state)

    def deliver(self):
        if self.send is None or self.stopped.is_set():
            return
        for item in self.store.due_outbox(time.time()):
            if self.stopped.is_set():
                return
            # Binding changes serialize with sends, but network I/O never holds the
            # Runtime lock: slow delivery cannot block tool results or durable ACK.
            with self.delivery_lock:
                with self.runs.lock:
                    event = self.store.event(item['event_id'])
                    if not self._current(event):
                        self.store.suppress(event['id'])
                        continue
                    if not self.store.claim(item['id']):
                        continue
                try:
                    reply = self.send(item['binding']['chat_id'], item['body'], item['uuid'])
                except Exception:
                    reply = {'state': 'unknown', 'error_code': 'SEND_UNKNOWN'}
            state = reply.get('state') if isinstance(reply, dict) else 'unknown'
            if state not in {'accepted', 'retry_wait', 'unknown', 'failed'}:
                state = 'unknown'
            message_id = reply.get('message_id') if isinstance(reply, dict) else None
            if state == 'accepted' and (not isinstance(message_id, str)
                                       or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', message_id)):
                state, message_id = 'unknown', None
            attempts = item['attempts'] + 1
            if state == 'retry_wait' and attempts >= 3:
                state = 'failed'
            self.store.finish_delivery(item['id'], state, message_id=message_id,
                error_code=None if state == 'accepted' else 'DELIVERY_' + state.upper(),
                retry_after=min(300, max(2 ** attempts, float(reply.get('retry_after', 0) or 0))))
            self.log('delivery.' + state, event_id=event['id'], outbox_id=item['id'],
                     run_id=item.get('run_id'), message_id=message_id, attempts=attempts)

    def tick(self, *, delivery=True):
        if self.stopped.is_set():
            return
        for event in self.store.events(states=['received']):
            try:
                self.dispatch(event)
            except WebError as error:
                self.reject(event, error.code, '任务无法启动，请在本机工作台检查会话。')
        self.sync()
        if delivery:
            self.deliver()

    def status(self, agent_id):
        binding = self.store.binding()
        if not binding or binding['agent_id'] != agent_id:
            return {'channel': {'enabled': False}}
        events = [e for e in self.store.events() if self._current(e)][-30:]
        outbox = [r for r in self.store.list_outbox() if r['binding']['revision'] == binding['revision']][-30:]
        return {'channel': {'enabled': bool(binding['enabled']),
            'binding': {k: binding[k] for k in ('agent_id', 'session_id', 'revision')},
            'transport': self.transport.status() if self.transport else {'state': 'stopped'},
            'events': [{k: e.get(k) for k in ('id', 'run_id', 'state', 'error_code')} for e in events],
            'outbox': [{k: r.get(k) for k in ('id', 'run_id', 'kind', 'state', 'attempts', 'error_code')}
                       for r in outbox]}}

    def retry(self, outbox_id, agent_id):
        with self.runs.lock:
            binding = self.store.binding()
            if not binding or binding['agent_id'] != agent_id:
                raise WebError(404, 'NOT_FOUND')
            item = next((r for r in self.store.list_outbox() if r['id'] == outbox_id), None)
            if not item or not self._current(self.store.event(item['event_id'])):
                raise WebError(404, 'NOT_FOUND')
            try:
                self.store.retry_unknown(outbox_id)
            except ChannelError as error:
                raise WebError(409, error.code) from None
        self.wake.set()
        return self.status(agent_id)

    def start(self, transport):
        self.transport, self.send = transport, transport.send
        transport.start()
        def loop(delivery):
            try:
                while not self.stopped.is_set():
                    try:
                        self.deliver() if delivery else self.tick(delivery=False)
                    except Exception:
                        self.log('worker.error', code='CHANNEL_WORKER_ERROR')
                    self.wake.wait(.5)
                    self.wake.clear()
            finally:
                self.runs.close_thread_connection()
        self.threads = [threading.Thread(target=loop, args=(delivery,), daemon=True,
                                        name='feishu-delivery' if delivery else 'feishu-dispatch')
                        for delivery in (False, True)]
        for thread in self.threads:
            thread.start()

    def close(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        self.wake.set()
        if self.transport:
            self.transport.close()
        for thread in self.threads:
            thread.join(20)
