import errno
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_agent.write_file import WriteFile
from local_agent.approvals import RunControl, RunStopped


class WriteFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / 'report.md'
        self.writer = WriteFile(self.root, 'report.md')
        self.arguments = {'path': 'report.md', 'content': '# 报告\r\n确认的内容\n'}

    def assert_error(self, result, code):
        self.assertIs(result['ok'], False)
        self.assertEqual(result['error']['code'], code)
        self.assertIsInstance(result['error']['message'], str)
        self.assertNotIn(str(self.root), result['error']['message'])

    def publish(self, writer=None, arguments=None, **kwargs):
        writer = self.writer if writer is None else writer
        arguments = self.arguments if arguments is None else arguments
        return writer.commit(arguments, writer.execute(arguments), **kwargs)

    def test_declares_only_business_parameters_and_medium_risk(self):
        spec = self.writer.spec
        self.assertEqual(spec.name, 'write_file')
        self.assertEqual((spec.risk, spec.source, spec.timeout_seconds), ('medium', 'builtin', 5))
        self.assertEqual(spec.input_schema, {
            'type': 'object',
            'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
            'required': ['path', 'content'], 'additionalProperties': False,
        })

    def test_validation_and_preview_do_not_create_any_file(self):
        self.assertIsNone(self.writer.validate(self.arguments))
        preview = self.writer.preview(self.arguments)
        self.assertEqual(preview['path'], 'report.md')
        self.assertEqual(preview['operation'], 'created')
        self.assertEqual(preview['content'], self.arguments['content'])
        self.assertEqual(preview['bytes'], len(self.arguments['content'].encode('utf-8')))
        self.assertIn('report.md', preview['action_summary'])
        self.assertIn(str(preview['bytes']), preview['action_summary'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_execute_is_pure_memory_and_freezes_the_approved_bytes(self):
        with patch('os.open', side_effect=AssertionError('execute must not open files')):
            candidate = self.writer.execute(self.arguments)
        original = dict(self.arguments)
        self.arguments['content'] = 'changed after preparation'
        result = self.writer.commit(original, candidate)
        self.assertIs(result['ok'], True)
        self.assertEqual(self.output.read_bytes(), original['content'].encode('utf-8'))

    def test_created_receipt_matches_actual_file_and_no_temp_remains(self):
        result = self.publish()
        raw = self.output.read_bytes()
        self.assertEqual(result, {'ok': True, 'path': 'report.md', 'bytes': len(raw),
                                  'sha256': hashlib.sha256(raw).hexdigest(),
                                  'operation': 'created'})
        self.assertEqual(raw, self.arguments['content'].encode('utf-8'))
        self.assertEqual(list(self.root.iterdir()), [self.output])
        self.assertEqual(self.output.stat().st_nlink, 1)

    def test_exact_argument_schema_and_output_path_are_required(self):
        for arguments in [None, [], {}, {'path': 'report.md'},
                          {'path': 1, 'content': 'text'}, {'path': 'report.md', 'content': b'x'},
                          {**self.arguments, 'overwrite': True}, {**self.arguments, 'intent': 'x'}]:
            with self.subTest(arguments=arguments):
                self.assert_error(self.writer.validate(arguments), 'INVALID_ARGUMENT')
        self.assert_error(self.writer.validate({'path': 'another.md', 'content': 'x'}), 'PATH_DENIED')
        paths = ['', '../outside.md', str(self.output), './report.md', '.hidden.md',
                 'docs//report.md', 'docs/.hidden/report.md', 'docs\\report.md']
        for path in paths:
            with self.subTest(path=path):
                writer = WriteFile(self.root, path)
                self.assert_error(writer.validate({'path': path, 'content': 'x'}), 'PATH_DENIED')

    def test_content_limit_counts_utf8_bytes_and_rejects_non_text(self):
        accepted = {'path': 'report.md', 'content': '中' * 10922 + 'ab'}
        self.assertIsNone(self.writer.validate(accepted))
        self.assertEqual(self.publish(arguments=accepted)['bytes'], 32768)
        writer = WriteFile(self.root, 'next.txt')
        for content, code in [('中' * 10923, 'FILE_TOO_LARGE'), ('\ud800', 'UNSUPPORTED_FILE'),
                              ('text\0binary', 'UNSUPPORTED_FILE')]:
            with self.subTest(code=code):
                self.assert_error(writer.validate({'path': 'next.txt', 'content': content}), code)
        for path in ['report.json', 'report']:
            writer = WriteFile(self.root, path)
            self.assert_error(writer.validate({'path': path, 'content': 'x'}), 'UNSUPPORTED_FILE')

    def test_existing_regular_symbolic_and_hardlinked_targets_are_never_changed(self):
        source = self.root / 'source.txt'
        source.write_bytes(b'original')
        for kind in ['regular', 'symlink', 'hardlink', 'directory']:
            with self.subTest(kind=kind):
                if kind == 'regular':
                    self.output.write_bytes(b'original')
                elif kind == 'symlink':
                    self.output.symlink_to(source)
                elif kind == 'hardlink':
                    os.link(source, self.output)
                else:
                    self.output.mkdir()
                self.assert_error(self.writer.validate(self.arguments), 'FILE_EXISTS')
                self.assertEqual(self.writer.validate(self.arguments)['error']['owner'], 'user')
                self.assert_error(self.publish(), 'FILE_EXISTS')
                self.assertEqual(source.read_bytes(), b'original')
                if kind == 'directory':
                    self.output.rmdir()
                else:
                    self.assertEqual(self.output.read_bytes(), b'original')
                    self.output.unlink()

    def test_missing_parent_is_not_created_and_symlink_parent_is_denied(self):
        for path, code in [('missing/report.md', 'DIRECTORY_NOT_FOUND'),
                           ('linked/report.md', 'PATH_DENIED')]:
            with self.subTest(path=path):
                if path.startswith('linked'):
                    (self.root / 'linked').symlink_to(self.root, target_is_directory=True)
                writer = WriteFile(self.root, path)
                arguments = {'path': path, 'content': 'x'}
                self.assert_error(writer.validate(arguments), code)
                if code == 'DIRECTORY_NOT_FOUND':
                    self.assertEqual(writer.validate(arguments)['error']['owner'], 'user')
                self.assert_error(self.publish(writer, arguments), code)
        self.assertFalse((self.root / 'missing').exists())
        self.assertFalse(self.output.exists())

    def test_workspace_identity_is_pinned_until_commit(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        writer = WriteFile(workspace, 'report.md')
        self.assertIsNone(writer.validate(self.arguments))
        workspace.rename(self.root / 'old-workspace')
        workspace.mkdir()
        self.assert_error(writer.validate(self.arguments), 'WORKSPACE_CHANGED')
        self.assert_error(self.publish(writer), 'WORKSPACE_CHANGED')
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertEqual(list((self.root / 'old-workspace').iterdir()), [])

    def test_target_created_after_preflight_is_preserved(self):
        self.assertIsNone(self.writer.validate(self.arguments))
        candidate = self.writer.execute(self.arguments)
        self.output.write_bytes(b'created while waiting for approval')
        self.assert_error(self.writer.commit(self.arguments, candidate), 'FILE_EXISTS')
        self.assertEqual(self.output.read_bytes(), b'created while waiting for approval')

    def test_second_success_is_forbidden_even_if_first_file_is_removed(self):
        self.assertIs(self.publish()['ok'], True)
        self.output.unlink()
        self.assert_error(self.writer.validate(self.arguments), 'OUTPUT_LIMIT')
        self.assert_error(self.publish(), 'OUTPUT_LIMIT')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancel_before_commit_and_during_chunk_write_leaves_no_file(self):
        class Cancelled(Exception):
            pass

        def always_cancel():
            raise Cancelled()

        with self.assertRaises(Cancelled):
            self.publish(check=always_cancel)
        self.assertEqual(list(self.root.iterdir()), [])
        cancelled = False
        real_write = os.write

        def write_then_cancel(fd, data):
            nonlocal cancelled
            count = real_write(fd, data)
            cancelled = True
            return count

        def check():
            if cancelled:
                raise Cancelled()

        with patch('local_agent.write_file.os.write', side_effect=write_then_cancel):
            with self.assertRaises(Cancelled):
                self.publish(arguments={'path': 'report.md', 'content': 'x' * 16384}, check=check)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancellation_is_checked_after_verification_before_final_publication(self):
        class Cancelled(Exception):
            pass

        cancelled = False
        real_fstat = os.fstat

        def fstat_then_cancel(fd):
            nonlocal cancelled
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode):
                cancelled = True
            return info

        def check():
            if cancelled:
                raise Cancelled()

        with patch('local_agent.write_file.os.fstat', side_effect=fstat_then_cancel):
            with self.assertRaises(Cancelled):
                self.publish(check=check)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancel_acquires_publication_lock_while_temp_preparation_is_paused(self):
        control = RunControl(threading.Event())
        preparation_paused = threading.Event()
        release_preparation = threading.Event()
        cancellation_accepted = threading.Event()
        outcomes = []
        real_fsync = os.fsync

        def paused_fsync(fd):
            preparation_paused.set()
            if not release_preparation.wait(2):
                raise AssertionError('The test did not release file preparation.')
            return real_fsync(fd)

        def write():
            try:
                outcomes.append(self.publish(check=control.check, publish=control.commit))
            except BaseException as error:
                outcomes.append(error)

        def cancel():
            control.cancel_run()
            cancellation_accepted.set()

        worker = threading.Thread(target=write)
        canceller = threading.Thread(target=cancel)
        with patch('local_agent.write_file.os.fsync', side_effect=paused_fsync):
            worker.start()
            try:
                self.assertTrue(preparation_paused.wait(1), f'Preparation did not start: {outcomes}')
                canceller.start()
                self.assertTrue(cancellation_accepted.wait(1),
                                'File preparation must not hold the publication lock.')
                self.assertFalse(self.output.exists())
            finally:
                release_preparation.set()
                worker.join(2)
                if canceller.ident is not None:
                    canceller.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(canceller.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], RunStopped)
        self.assertEqual(outcomes[0].code, 'CANCELLED')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancel_after_successful_link_preserves_receipt(self):
        class Cancelled(Exception):
            pass

        cancelled = False
        real_link = os.link

        def publish_then_cancel(*args, **kwargs):
            nonlocal cancelled
            real_link(*args, **kwargs)
            cancelled = True

        def check():
            if cancelled:
                raise Cancelled()

        with patch('local_agent.write_file.os.link', side_effect=publish_then_cancel):
            receipt = self.publish(check=check)
        self.assertIs(receipt['ok'], True)
        self.assertEqual(receipt['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertEqual(list(self.root.iterdir()), [self.output])

    def test_parent_replaced_during_temp_write_prevents_publication(self):
        parent = self.root / 'reports'
        parent.mkdir()
        moved = self.root / 'moved-reports'
        writer = WriteFile(self.root, 'reports/report.md')
        real_write = os.write

        def write_then_replace(fd, data):
            count = real_write(fd, data)
            parent.rename(moved)
            parent.mkdir()
            return count

        with patch('local_agent.write_file.os.write', side_effect=write_then_replace):
            result = self.publish(writer, {'path': 'reports/report.md', 'content': 'frozen'})
        self.assert_error(result, 'WORKSPACE_CHANGED')
        self.assertEqual(list(parent.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])

    def test_root_replaced_during_temp_write_prevents_publication(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        moved = self.root / 'old-workspace'
        writer = WriteFile(workspace, 'report.md')
        real_write = os.write

        def write_then_replace(fd, data):
            count = real_write(fd, data)
            workspace.rename(moved)
            workspace.mkdir()
            return count

        with patch('local_agent.write_file.os.write', side_effect=write_then_replace):
            self.assert_error(self.publish(writer), 'WORKSPACE_CHANGED')
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertEqual(list(moved.iterdir()), [])

    def test_target_race_at_link_preserves_the_other_file(self):
        real_link = os.link

        def race_link(*args, **kwargs):
            self.output.write_bytes(b'other writer won')
            return real_link(*args, **kwargs)

        with patch('local_agent.write_file.os.link', side_effect=race_link):
            self.assert_error(self.publish(), 'FILE_EXISTS')
        self.assertEqual(self.output.read_bytes(), b'other writer won')
        self.assertEqual(list(self.root.iterdir()), [self.output])

    def test_disk_and_permission_errors_are_distinct_and_leave_no_partial_file(self):
        for err, code in [(errno.ENOSPC, 'DISK_FULL'), (errno.EACCES, 'OS_PERMISSION_DENIED'),
                          (errno.EIO, 'WRITE_ERROR')]:
            with self.subTest(code=code):
                with patch('local_agent.write_file.os.write', side_effect=OSError(err, 'private detail')):
                    self.assert_error(self.publish(), code)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_cleanup_failure_after_publication_keeps_successful_receipt(self):
        real_unlink, real_close, real_fstat = os.unlink, os.close, os.fstat

        def failed_unlink(path, *args, **kwargs):
            if str(path).startswith('.local-agent-'):
                raise OSError(errno.EIO, 'private cleanup detail')
            return real_unlink(path, *args, **kwargs)

        def failed_close(fd):
            regular = stat.S_ISREG(real_fstat(fd).st_mode)
            real_close(fd)
            if regular:
                raise OSError(errno.EIO, 'private cleanup detail')

        for operation, replacement in [('unlink', failed_unlink), ('close', failed_close)]:
            with self.subTest(operation=operation):
                path = f'{operation}.md'
                writer = WriteFile(self.root, path)
                with patch(f'local_agent.write_file.os.{operation}', side_effect=replacement):
                    receipt = self.publish(writer, {'path': path, 'content': 'approved'})
                self.assertIs(receipt['ok'], True)
                self.assertEqual(receipt['sha256'], hashlib.sha256((self.root / path).read_bytes()).hexdigest())
                self.assertIn('cleanup_warning', receipt)
                self.assertNotIn('private cleanup detail', str(receipt))

    def test_failed_temp_creation_never_unlinks_a_preexisting_file(self):
        temporary = self.root / '.local-agent-fixed.tmp'
        temporary.write_bytes(b'not ours')

        class FixedId:
            hex = 'fixed'

        with patch('local_agent.write_file.uuid4', return_value=FixedId()):
            self.assert_error(self.publish(), 'WRITE_ERROR')
        self.assertEqual(temporary.read_bytes(), b'not ours')
        self.assertFalse(self.output.exists())

    def test_replaced_temp_source_is_not_published_under_an_approved_receipt(self):
        original = self.root / 'unapproved.txt'
        original.write_bytes(b'unapproved source')
        real_fstat = os.fstat
        replaced = False

        def replace_after_file_check(fd):
            nonlocal replaced
            info = real_fstat(fd)
            if stat.S_ISREG(info.st_mode) and not replaced:
                replaced = True
                temporary = next(self.root.glob('.local-agent-*.tmp'))
                temporary.unlink()
                temporary.symlink_to(original)
            return info

        with patch('local_agent.write_file.os.fstat', side_effect=replace_after_file_check):
            self.assert_error(self.publish(), 'WORKSPACE_CHANGED')
        self.assertFalse(self.output.exists())
        self.assertEqual(original.read_bytes(), b'unapproved source')


if __name__ == '__main__':
    unittest.main()
