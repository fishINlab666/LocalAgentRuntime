"""Create a private Feishu binding; optional one-time listening, never model execution."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_agent.channels.config import validate_config, ChannelConfigError
from local_agent.session_store import SessionStore, StoreError
from local_agent.sessions import SessionService, SessionScope, SessionError


def collect_identity(app_id, app_secret, *, timeout_seconds=300, transport_factory=None):
    from local_agent.channels.feishu import FeishuTransport
    validate_config(dict(app_id=app_id, tenant_key='pending', open_id='pending',
                         chat_id='pending', session_id='pending', agent_id='directory-qa'))
    if not app_secret:
        raise RuntimeError('FEISHU_APP_SECRET_REQUIRED')
    phrase = '/绑定 ' + secrets.token_urlsafe(24)
    ready = threading.Event()
    identity = {}

    def receive(envelope):
        if ready.is_set() or not isinstance(envelope, dict):
            return
        if (envelope.get('app_id') != app_id
                or envelope.get('event_type') != 'im.message.receive_v1'
                or envelope.get('sender_type') != 'user'
                or envelope.get('chat_type') != 'p2p'
                or envelope.get('message_type') != 'text'
                or envelope.get('tenant_key') != envelope.get('sender_tenant_key')):
            return
        try:
            content = json.loads(envelope.get('content', ''))
            if not isinstance(content, dict) or not isinstance(content.get('text'), str):
                return
            if not secrets.compare_digest(content['text'].strip().encode(), phrase.encode()):
                return
            candidate = {key: envelope.get(key) for key in ('app_id', 'tenant_key', 'open_id', 'chat_id')}
            validate_config(dict(candidate, session_id='pending', agent_id='directory-qa'))
        except (ValueError, TypeError, UnicodeError):
            return
        # This listener has no Runtime or sender; only the one-time phrase can select an identity.
        identity.update(candidate)
        ready.set()

    transport = (transport_factory or FeishuTransport)(app_id, app_secret, receive)
    print('正在建立首次绑定连接；不会运行任务或回复消息。', flush=True)
    deadline = time.monotonic() + timeout_seconds
    announced = False
    try:
        try:
            transport.start()
        except Exception:
            raise RuntimeError('FEISHU_BINDING_CONNECTION_FAILED') from None
        while not ready.is_set():
            status = transport.status()
            if status['state'] in ('error', 'stopped'):
                error_code = status.get('error_code')
                if (isinstance(error_code, str)
                        and (error_code.startswith('SDK_'))
                        and len(error_code) <= 64):
                    raise RuntimeError('FEISHU_BINDING_CONNECTION_FAILED:' + error_code)
                raise RuntimeError('FEISHU_BINDING_CONNECTION_FAILED')
            if status['state'] == 'connected' and not announced:
                print('连接已建立。请在飞书后台保存长连接，订阅 im.message.receive_v1，确保对本人可用。')
                print('然后由你本人向此机器人私聊发送以下整行（不要转发给他人）：')
                print(phrase, flush=True)
                announced = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('FEISHU_BINDING_TIMEOUT')
            ready.wait(min(0.1, remaining))
        return identity
    finally:
        transport.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', action='store_true', help='用本人私聊随机码取得 ID；需环境变量 FEISHU_APP_SECRET')
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/share/local-agent')
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/feishu-binding.json')
    parser.add_argument('--workspace', type=Path, default=ROOT / 'examples/feishu-workspace')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('绑定文件已存在，请使用已有文件，或通过 --output 指定新文件；不会覆盖。')
    print('这里只填写非密钥 ID，不输入 App Secret 或模型 Key。')
    if args.listen:
        app_id = input('飞书 App ID：').strip()
        config = collect_identity(app_id, os.environ.get('FEISHU_APP_SECRET'))
        print('已取得与你发送的绑定码匹配的私聊身份，尚未保存：')
        print(json.dumps(config, ensure_ascii=False, indent=2))
        if input('若绑定码确为你本人在与机器人的私聊中发送，输入“绑定”保存；其他输入退出：').strip() != '绑定':
            raise RuntimeError('FEISHU_BINDING_NOT_CONFIRMED')
    else:
        config = {key: input(label + '：').strip() for key, label in (
            ('app_id', '飞书 App ID'), ('tenant_key', '租户 tenant_key'),
            ('open_id', '本人 open_id'), ('chat_id', '本人和机器人的私聊 chat_id'))}
    # Validate identifiers before creating any persistent session.
    validate_config(dict(config, session_id='pending', agent_id='directory-qa'))
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = SessionStore.open(args.state_dir)
    try:
        session = SessionService(store).create(args.workspace.resolve(), '飞书广深演练（虚构资料）',
                                               SessionScope('directory', None))
        config.update(session_id=session.id, agent_id=session.agent_id, enabled=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump(config, target, ensure_ascii=False, indent=2)
            target.write('\n')
    finally:
        store.close()
    print('绑定已保存：' + str(args.output.resolve()))
    print('Session：' + session.id)
    print('正式服务尚未启动、未调用模型。下一步运行 bash trial/start-feishu.command。')


if __name__ == '__main__':
    try:
        main()
    except (ChannelConfigError, SessionError, StoreError, RuntimeError) as error:
        print('准备失败：' + str(error) + '。若 STATE_IN_USE，请先关闭使用同一状态库的旧服务。', file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print('\n已停止绑定，未启动正式服务。', file=sys.stderr)
        raise SystemExit(130)
