"""CLI and local web entry points sharing Runtime and SessionService."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import signal
import threading
import uuid

from .approvals import RunControl
from .console_approval import ConsoleApprovalBroker
from .demo import DemoProvider
from .directory_evaluation import evaluate_directory
from .discovery import DirectoryTools
from .evaluation import evaluate
from .files import ReadFile
from .file_tools import adapt_tools
from .provider import DeepSeekProvider, ProviderError
from .runtime import RunConfig, Runtime
from .session_store import SessionStore, StoreError
from .sessions import RunSubmission, SessionError, SessionScope, SessionService
from .trace import Trace


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "local-agent"


def _session_data(record):
    value = asdict(record)
    value["scope"] = asdict(record.scope)
    return value


def _json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _open_service(state_dir):
    store = SessionStore.open(Path(state_dir).expanduser())
    store.recover_interrupted(uuid.uuid4().hex)
    return store, SessionService(store)


def _sessions_command(args):
    store = None
    try:
        store, service = _open_service(args.state_dir)
        action = args.sessions_action
        if action == "create":
            scope = (SessionScope("directory", None) if args.discover
                     else SessionScope("file", args.file))
            record = service.create(args.workspace, args.title, scope)
            return {"session_id": record.id, "session": _session_data(record)}, 0
        if action == "list":
            page = service.list(args.workspace, archived=args.archived, cursor=args.cursor)
            return {"sessions": [_session_data(item) for item in page.items],
                    "next_cursor": page.next_cursor}, 0
        if action == "show":
            return {"session": _session_data(service.load(args.session_id))}, 0
        if action == "rename":
            record = service.rename(args.session_id, args.title)
            return {"session_id": record.id, "session": _session_data(record)}, 0
        if action == "archive":
            record = service.archive(args.session_id)
            return {"session_id": record.id, "session": _session_data(record)}, 0
        if action == "restore":
            record = service.restore(args.session_id)
            return {"session_id": record.id, "session": _session_data(record)}, 0
        if action == "backup":
            return service.backup(args.destination), 0
        raise SessionError("SUBMISSION_INVALID")
    except (SessionError, StoreError) as error:
        return {"error": error.code}, 2
    finally:
        if store is not None:
            store.close()


def _persistent_run(args, control):
    store = None
    approvals = None
    try:
        store, service = _open_service(args.state_dir)
        session = service.load(args.session)
        if args.continue_run is not None:
            prepared = service.continue_interrupted(
                session.id, args.continue_run,
                args.client_request_id or uuid.uuid4().hex,
            )
        else:
            prepared = service.submit(session.id, RunSubmission(
                client_request_id=args.client_request_id or uuid.uuid4().hex,
                question=args.question,
                task_type="conversation" if args.conversation else "files",
                scope=session.scope,
                output_path=args.output_file,
                parent_run_id=None,
                execution_options={},
            ))
        provider = DeepSeekProvider.from_env()
        trace = Trace(args.log_dir, Path(session.workspace_path),
                      debug_content=args.debug_content, run_id=prepared.run_id)
        if prepared.submission.output_path is not None:
            approvals = ConsoleApprovalBroker(
                prepared.run_id, publish=trace.emit, journal=prepared.journal
            )
        output = service.execute(
            prepared, provider, trace, approvals=approvals, control=control
        )
        return output, 0 if output["state"] == "completed" else 1
    except ProviderError as error:
        return {"error": error.code,
                "message": "请在环境变量中配置模型密钥；不要将密钥写入代码或聊天。"}, 2
    except (SessionError, StoreError) as error:
        return {"error": error.code}, 2
    except (OSError, ValueError):
        return {"error": "LOCAL_CONFIG_ERROR",
                "message": "请检查状态目录、日志目录和会话配置。"}, 2
    finally:
        if approvals is not None:
            approvals.close()
        if store is not None:
            store.close()


def _build_parser():
    parser = argparse.ArgumentParser(description="本地资料 Agent：一次性问答与持久会话。")
    commands = parser.add_subparsers(dest="command", required=True)

    web = commands.add_parser("serve", help="启动本地资料问答页面")
    web_scope = web.add_mutually_exclusive_group(required=True)
    web_scope.add_argument("--workspace", type=Path)
    web_scope.add_argument("--session", help="从状态库打开一个已有会话；工作区缺失时仍可看历史")
    web.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    web.add_argument("--log-dir", type=Path, default=ROOT / "runs")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--demo", action="store_true", help="显式使用测试替身，不调用真实模型")
    web.add_argument("--open", action="store_true", help="启动后在浏览器打开页面")

    demo = commands.add_parser("demo", help="无需密钥的模拟演示，不代表真实模型验收")
    demo.add_argument("--log-dir", type=Path, default=ROOT / "runs")
    demo.add_argument("--discover", action="store_true", help="演示先列出目录再读取资料")

    run = commands.add_parser("run", help="执行一次性任务或持久会话任务")
    run.add_argument("--log-dir", type=Path, default=ROOT / "runs")
    run.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    run.add_argument("--session")
    run.add_argument("--client-request-id")
    run.add_argument("--workspace", type=Path)
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--file")
    mode.add_argument("--discover", action="store_true", help="由模型在目录中发现资料")
    run.add_argument("--conversation", action="store_true", help="询问同一会话的历史记录")
    run.add_argument("--question", required=True)
    run.add_argument("--output-file", help="逐次确认后新建的工作区相对路径")
    run.add_argument("--debug-content", action="store_true")

    for name, help_text in (
        ("evaluate", "真实模型四类场景各三次，全部使用合成数据"),
        ("evaluate-directory", "真实模型目录发现四类场景各三次，全部使用合成数据"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--log-dir", type=Path, default=ROOT / "runs")

    sessions = commands.add_parser("sessions", help="管理持久会话；这些操作不调用模型")
    sessions.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    actions = sessions.add_subparsers(dest="sessions_action", required=True)
    create = actions.add_parser("create")
    create.add_argument("--workspace", type=Path, required=True)
    create.add_argument("--title", required=True)
    create_mode = create.add_mutually_exclusive_group(required=True)
    create_mode.add_argument("--file")
    create_mode.add_argument("--discover", action="store_true")
    listing = actions.add_parser("list")
    listing.add_argument("--workspace", type=Path, required=True)
    listing.add_argument("--archived", action="store_true")
    listing.add_argument("--cursor")
    show = actions.add_parser("show")
    show.add_argument("session_id")
    rename = actions.add_parser("rename")
    rename.add_argument("session_id")
    rename.add_argument("--title", required=True)
    for name in ("archive", "restore"):
        action = actions.add_parser(name)
        action.add_argument("session_id")
    backup = actions.add_parser("backup")
    backup.add_argument("destination", type=Path)
    continued = actions.add_parser("continue")
    continued.add_argument("session_id")
    continued.add_argument("--run", dest="continue_run", required=True)
    continued.add_argument("--client-request-id")
    continued.add_argument("--log-dir", type=Path, default=ROOT / "runs")
    continued.add_argument("--debug-content", action="store_true")
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "run":
        if args.session:
            if args.workspace is not None or args.file is not None or args.discover:
                parser.error("--session 使用会话固定范围，不能传 --workspace/--file/--discover")
            if args.conversation and args.output_file is not None:
                parser.error("--conversation 不能创建输出文件")
        elif (args.workspace is None or bool(args.file) == bool(args.discover)
              or args.conversation or args.client_request_id):
            parser.error("一次性 run 需要 --workspace 以及 --file/--discover 二选一")
    if args.command == "serve":
        if not 0 <= args.port <= 65535:
            parser.error("--port 必须在 0–65535 之间")
        from .web import serve
        try:
            return serve(args.workspace, args.log_dir, state_dir=args.state_dir,
                         session_id=args.session, port=args.port,
                         provider_factory=DemoProvider if args.demo else DeepSeekProvider.from_env,
                         open_browser=args.open)
        except (OSError, ValueError, SessionError, StoreError):
            print("无法启动页面：请检查工作区、日志目录和端口是否可用；可通过 --port 更换端口。")
            return 2
    if args.command == "sessions" and args.sessions_action != "continue":
        output, code = _sessions_command(args)
        _json(output)
        return code

    cancel = threading.Event()
    control = RunControl(cancel, run_timeout=120)
    previous = signal.signal(signal.SIGINT, lambda *_: control.cancel_run())
    try:
        if args.command == "sessions":
            args.session = args.session_id
            args.question = ""
            args.conversation = False
            args.output_file = None
            output, code = _persistent_run(args, control)
        elif args.command == "run" and args.session:
            args.continue_run = None
            output, code = _persistent_run(args, control)
        elif args.command in {"evaluate", "evaluate-directory"}:
            evaluator = evaluate_directory if args.command == "evaluate-directory" else evaluate
            output = evaluator(DeepSeekProvider.from_env, args.log_dir, cancel=cancel)
            code = 2 if output["gate"] == "NOT_RUN" else 1 if output["gate"] == "FAILED" else 3
        else:
            simulated = args.command == "demo"
            provider = DemoProvider() if simulated else DeepSeekProvider.from_env()
            workspace = (ROOT / ("examples/discovery-workspace" if args.discover else "examples/workspace")
                         if simulated else args.workspace)
            target = None if args.discover else "demo-note.md" if simulated else args.file
            question = (("项目代号、评审人和演示日期分别是什么？请合并资料并引用原文。"
                         if args.discover else "读取并概括演示资料，引用原文。")
                        if simulated else args.question)
            trace = Trace(args.log_dir, workspace, debug_content=simulated or args.debug_content)
            reader = DirectoryTools(workspace) if args.discover else ReadFile(workspace, {target})
            output_path = None if simulated else args.output_file
            tool = adapt_tools(reader, output_path=output_path)
            approvals = ConsoleApprovalBroker(trace.run_id, publish=trace.emit) if output_path else None
            try:
                output = Runtime(provider, tool, trace, RunConfig(), approvals=approvals,
                                 control=control).run(question, target, cancel)
            finally:
                if approvals is not None:
                    approvals.close()
            if simulated:
                output["notice"] = "这是测试替身演示，文件读取真实执行；未调用真实模型，不能作为产品验收。"
            code = 0 if output["state"] == "completed" else 1
    except ProviderError as error:
        output = {"error": error.code,
                  "message": "请在环境变量中配置模型密钥；不要将密钥写入代码或聊天。"}
        code = 2
    except (SessionError, StoreError) as error:
        output, code = {"error": error.code}, 2
    except (OSError, ValueError):
        output = {"error": "LOCAL_CONFIG_ERROR",
                  "message": "请检查工作区、文件路径和日志目录。"}
        code = 2
    finally:
        signal.signal(signal.SIGINT, previous)
    _json(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
