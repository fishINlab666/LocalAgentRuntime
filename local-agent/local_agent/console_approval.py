"""Interactive terminal decisions for the existing run-local approval broker."""

import os
import select
import sys

from .approvals import ApprovalBroker, ApprovalError


def _display(value) -> str:
    """Keep terminal control sequences from changing the approval display."""
    return ''.join(character if character.isprintable() or character in '\n\t'
                   else f'\\u{ord(character):04x}' for character in str(value))


class ConsoleApprovalBroker(ApprovalBroker):
    def __init__(self, run_id, *, stdin=None, stderr=None, **kwargs):
        super().__init__(run_id, **kwargs)
        self.stdin = sys.stdin if stdin is None else stdin
        self.stderr = sys.stderr if stderr is None else stderr

    def request(self, call_id, name, arguments, preview, control):
        try:
            interactive = self.stdin.isatty()
        except (AttributeError, OSError, ValueError):
            interactive = False
        if not interactive:
            raise ApprovalError('APPROVAL_UNAVAILABLE')
        return super().request(call_id, name, arguments, preview, control)

    def _emit(self, event, approval):
        super()._emit(event, approval)
        if event != 'approval.required':
            return
        data = approval.data
        source = {'builtin': '内置', 'user': '用户工具', 'mcp': 'MCP'}.get(data.get('source'), data.get('source'))
        risk = {'low': '低风险', 'medium': '中风险', 'high': '高风险'}.get(data.get('risk'), data.get('risk'))
        operation = '新建，不覆盖' if data.get('operation') in ('create', 'created') else data.get('operation')
        try:
            print(f'\n实际操作：{_display(data.get("action_summary", data["name"]))}', file=self.stderr)
            print(f'目标：{_display(data.get("path"))} · {data.get("bytes")} 字节 · {_display(operation)}', file=self.stderr)
            print(f'来源：{_display(source)} · {_display(risk)}', file=self.stderr)
            print(f'模型意图：{_display(data["arguments"].get("intent", "未提供"))}（用于说明目的）', file=self.stderr)
            print('完整内容预览（不可见控制字符以转义显示）：', file=self.stderr)
            print(_display(data.get('content', '')), file=self.stderr)
            print('——预览结束——', file=self.stderr)
            print('确认这次新建？输入 y 确认 / n 拒绝，按回车提交：', file=self.stderr, flush=True)
            self._read_decision(approval)
        except (OSError, ValueError, TypeError):
            raise ApprovalError('APPROVAL_UNAVAILABLE') from None

    def _read_decision(self, approval):
        fd = self.stdin.fileno()
        line = bytearray()
        while True:
            self._check(approval)
            readable, _, _ = select.select([fd], [], [], min(.1, approval.control.approval_remaining()))
            if not readable:
                continue
            self._check(approval)
            character = os.read(fd, 1)
            if not character:
                raise ApprovalError('APPROVAL_UNAVAILABLE')
            if character not in (b'\n', b'\r'):
                line.extend(character)
                continue
            answer = line.decode('ascii', errors='replace').strip().lower()
            line.clear()
            if answer in ('y', 'n'):
                self.decide(approval.data['approval_id'], 'allow' if answer == 'y' else 'deny')
                return
            print('未提交决定。请输入 y 确认或 n 拒绝：', file=self.stderr, flush=True)
