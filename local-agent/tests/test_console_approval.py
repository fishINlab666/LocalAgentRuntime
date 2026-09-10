import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_agent.approvals import ApprovalError, RunControl, RunStopped
from local_agent.console_approval import ConsoleApprovalBroker


class PipeTerminal:
    def __init__(self, fd):
        self.fd = fd

    def isatty(self):
        return True

    def fileno(self):
        return self.fd


class ConsoleApprovalTests(unittest.TestCase):
    def setUp(self):
        self.read_fd, self.write_fd = os.pipe()
        self.addCleanup(os.close, self.read_fd)
        self.addCleanup(self.close_writer)
        self.stderr = io.StringIO()
        self.clock_value = 1000.0
        self.clock = lambda: self.clock_value
        self.control = RunControl(threading.Event(), clock=self.clock)
        self.ready = threading.Event()
        self.broker = ConsoleApprovalBroker('run-1', stdin=PipeTerminal(self.read_fd),
            stderr=self.stderr, clock=self.clock,
            publish=lambda event, _: self.ready.set() if event == 'approval.required' else None)
        self.addCleanup(self.broker.close)
        self.content = '# 报告\n完整正文末尾\n'
        self.arguments = {'intent': '保存摘要', 'path': 'report.md', 'content': self.content}
        self.preview = {'action_summary': '新建 report.md', 'risk': 'medium', 'source': 'builtin',
                        'operation': 'created', 'path': 'report.md',
                        'bytes': len(self.content.encode('utf-8')), 'content': self.content}

    def close_writer(self):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None

    def request(self):
        return self.broker.request('call-1', 'write_file', self.arguments, self.preview, self.control)

    def test_explicit_allow_and_deny_show_frozen_preview_only_on_stderr(self):
        for answer, expected in ((b'y\n', 'allow'), (b'\ninvalid\nn\n', 'deny')):
            with self.subTest(answer=answer):
                os.write(self.write_fd, answer)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(self.request(), expected)
                self.assertEqual(stdout.getvalue(), '')
                text = self.stderr.getvalue()
                self.assertIn('实际操作：新建 report.md', text)
                self.assertIn('模型意图：保存摘要', text)
                self.assertIn('report.md', text)
                self.assertIn(f'{self.preview["bytes"]} 字节', text)
                self.assertIn('新建', text)
                self.assertIn('内置', text)
                self.assertIn('中风险', text)
                self.assertIn(self.content, text)
                self.assertIsNone(self.broker.snapshot())

    def test_non_terminal_cannot_approve_even_with_yes_in_input(self):
        broker = ConsoleApprovalBroker('non-tty', stdin=io.StringIO('y\n'), stderr=self.stderr)
        self.addCleanup(broker.close)
        with self.assertRaises(ApprovalError) as caught:
            broker.request('call', 'write_file', self.arguments, self.preview, self.control)
        self.assertEqual(caught.exception.code, 'APPROVAL_UNAVAILABLE')
        self.assertIsNone(broker.snapshot())

    def test_eof_stops_without_approval(self):
        self.close_writer()
        with self.assertRaises(ApprovalError) as caught:
            self.request()
        self.assertEqual(caught.exception.code, 'APPROVAL_UNAVAILABLE')
        self.assertIsNone(self.broker.snapshot())

    def start_wait(self):
        result = {}

        def request():
            try:
                result['decision'] = self.request()
            except (ApprovalError, RunStopped) as error:
                result['error'] = error.code

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        self.assertTrue(self.ready.wait(1))
        return worker, result

    def test_cancel_interrupts_wait_without_terminal_input(self):
        worker, result = self.start_wait()
        self.control.cancel_run()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, {'error': 'CANCELLED'})

    def test_total_wait_expires_without_terminal_input(self):
        worker, result = self.start_wait()
        self.clock_value += 300
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, {'error': 'APPROVAL_EXPIRED'})

    def test_close_interrupts_wait_without_terminal_input(self):
        worker, result = self.start_wait()
        self.broker.close()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, {'error': 'APPROVAL_UNAVAILABLE'})


class ConsoleCliTests(unittest.TestCase):
    def test_run_output_path_uses_shared_control_and_closes_broker(self):
        from local_agent import __main__ as cli
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'workspace'
            workspace.mkdir()
            argv = ['local_agent', 'run', '--workspace', str(workspace), '--file', 'note.md',
                    '--question', '生成报告', '--output-file', 'report.md', '--log-dir', str(Path(tmp) / 'logs')]
            stdout = io.StringIO()
            with patch('sys.argv', argv), contextlib.redirect_stdout(stdout), \
                    patch.object(cli.DeepSeekProvider, 'from_env'), \
                    patch.object(cli, 'adapt_tools') as adapt, \
                    patch.object(cli, 'ConsoleApprovalBroker') as broker, \
                    patch.object(cli, 'Runtime') as runtime, \
                    patch.object(cli.signal, 'signal') as signal_handler:
                runtime.return_value.run.return_value = {'state': 'completed'}
                self.assertEqual(cli.main(), 0)
                self.assertEqual(json.loads(stdout.getvalue()), {'state': 'completed'})
                self.assertEqual(adapt.call_args.kwargs, {'output_path': 'report.md'})
                control = runtime.call_args.kwargs['control']
                self.assertIsInstance(control, RunControl)
                self.assertIs(runtime.call_args.kwargs['approvals'], broker.return_value)
                signal_handler.call_args_list[0].args[1](None, None)
                self.assertTrue(control.cancel.is_set())
                broker.return_value.close.assert_called_once()

    def test_demo_rejects_output_option(self):
        from local_agent import __main__ as cli
        with patch('sys.argv', ['local_agent', 'demo', '--output-file', 'report.md']), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main()
        self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
