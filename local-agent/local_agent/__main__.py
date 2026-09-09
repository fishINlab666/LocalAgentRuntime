"""CLI and local web entry points sharing the same Runtime."""

import argparse
import json
from pathlib import Path
import signal
import threading

from .demo import DemoProvider
from .directory_evaluation import evaluate_directory
from .discovery import DirectoryTools
from .evaluation import evaluate
from .files import ReadFile
from .provider import DeepSeekProvider, ProviderError
from .runtime import RunConfig, Runtime
from .trace import Trace


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description='单文件与目录证据问答：真实模型执行与显式模拟演示。')
    commands = parser.add_subparsers(dest='command', required=True)
    web = commands.add_parser('serve', help='启动本地资料问答页面')
    web.add_argument('--workspace', type=Path, required=True)
    web.add_argument('--log-dir', type=Path, default=ROOT / 'runs')
    web.add_argument('--port', type=int, default=8765)
    web.add_argument('--demo', action='store_true', help='显式使用测试替身，不调用真实模型')
    web.add_argument('--open', action='store_true', help='启动后在浏览器打开页面')
    for name, help_text in (('demo', '无需密钥的模拟演示，不代表真实模型验收'),
                            ('run', '使用真实模型读取指定文件或发现目录资料'),
                            ('evaluate', '真实模型四类场景各三次，全部使用合成数据'),
                            ('evaluate-directory', '真实模型目录发现四类场景各三次，全部使用合成数据')):
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--log-dir', type=Path, default=ROOT / 'runs')
        if name == 'demo':
            command.add_argument('--discover', action='store_true', help='演示先列出目录再读取资料')
        if name == 'run':
            command.add_argument('--workspace', type=Path, required=True)
            mode = command.add_mutually_exclusive_group(required=True)
            mode.add_argument('--file')
            mode.add_argument('--discover', action='store_true', help='由模型在工作区目录中发现资料')
            command.add_argument('--question', required=True)
            command.add_argument('--debug-content', action='store_true', help='在本地日志保留完整模型输入/输出与文件内容')
    args = parser.parse_args()
    if args.command == 'serve':
        if not 0 <= args.port <= 65535:
            parser.error('--port 必须在 0–65535 之间')
        from .web import serve
        try:
            return serve(args.workspace, args.log_dir, port=args.port,
                         provider_factory=DemoProvider if args.demo else DeepSeekProvider.from_env,
                         open_browser=args.open)
        except (OSError, ValueError):
            print('无法启动页面：请检查工作区、日志目录和端口是否可用；可通过 --port 更换端口。')
            return 2
    cancel = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
    try:
        if args.command in {'evaluate', 'evaluate-directory'}:
            evaluator = evaluate_directory if args.command == 'evaluate-directory' else evaluate
            output = evaluator(DeepSeekProvider.from_env, args.log_dir, cancel=cancel)
            code = 2 if output['gate'] == 'NOT_RUN' else 1 if output['gate'] == 'FAILED' else 3
        else:
            simulated = args.command == 'demo'
            provider = DemoProvider() if simulated else DeepSeekProvider.from_env()
            workspace = (ROOT / ('examples/discovery-workspace' if args.discover else 'examples/workspace')
                         if simulated else args.workspace)
            target = None if args.discover else 'demo-note.md' if simulated else args.file
            question = ('项目代号、评审人和演示日期分别是什么？请合并资料并引用原文。' if args.discover
                        else '读取并概括演示资料，引用原文。') if simulated else args.question
            trace = Trace(args.log_dir, workspace, debug_content=simulated or args.debug_content)
            tool = DirectoryTools(workspace) if args.discover else ReadFile(workspace, {target})
            output = Runtime(provider, tool, trace, RunConfig()).run(question, target, cancel)
            if simulated:
                output['notice'] = '这是测试替身演示，文件读取真实执行；未调用真实模型，不能作为产品验收。'
            code = 0 if output['state'] == 'completed' else 1
    except ProviderError as error:
        output = {'error': error.code, 'message': '请在环境变量中配置模型密钥；不要将密钥写入代码或聊天。'}
        code = 2
    except (OSError, ValueError):
        output = {'error': 'LOCAL_CONFIG_ERROR', 'message': '请检查工作区、文件路径和日志目录；日志必须位于工作区之外。'}
        code = 2
    finally:
        signal.signal(signal.SIGINT, previous)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
