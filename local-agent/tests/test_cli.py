import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        env = dict(os.environ)
        env.pop('AGENT_API_KEY', None)
        env.pop('DEEPSEEK_API_KEY', None)
        return subprocess.run([sys.executable, '-m', 'local_agent', *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)

    def test_demo_runs_with_no_key_and_is_explicitly_simulated(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.invoke('demo', '--log-dir', tmp)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result['state'], 'completed')
            self.assertTrue(result['provider']['simulated'])
            self.assertEqual(result['model_calls'], 2)
            self.assertIn('青禾-47', result['answer']['answer'])
            self.assertTrue(Path(result['trace_path']).is_file())

    def test_evaluation_without_key_returns_nonzero_and_not_run_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.invoke('evaluate', '--log-dir', tmp)
            self.assertEqual(run.returncode, 2)
            report = json.loads(run.stdout)
            self.assertEqual(report['gate'], 'NOT_RUN')

    def test_live_command_requires_key_before_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.invoke('run', '--workspace', str(ROOT / 'examples/workspace'),
                              '--file', 'demo-note.md', '--question', '项目代号？', '--log-dir', tmp)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(json.loads(run.stdout)['error'], 'CONFIG_MISSING')

    def test_directory_demo_reads_two_small_files_with_separate_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.invoke('demo', '--discover', '--log-dir', tmp)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertTrue(result['provider']['simulated'])
            self.assertEqual(result['state'], 'completed')
            self.assertTrue(result['scope']['complete'])
            self.assertEqual({c['path'] for c in result['answer']['citations']}, {'plan.md', 'review.md'})
            self.assertIn('项目代号', result['answer']['answer'])
            self.assertIn('评审人', result['answer']['answer'])
            self.assertIn('演示日期', result['answer']['answer'])
            self.assertIn('未调用真实模型', result['notice'])
            for name in ('plan.md', 'review.md'):
                self.assertLess((ROOT / 'examples/discovery-workspace' / name).stat().st_size, 2048)

    def test_directory_live_command_requires_key_and_exactly_one_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ('run', '--workspace', str(ROOT / 'examples/workspace'),
                    '--question', '项目代号？', '--log-dir', tmp)
            run = self.invoke(*args, '--discover')
            self.assertEqual(run.returncode, 2)
            self.assertEqual(json.loads(run.stdout)['error'], 'CONFIG_MISSING')
            for mode in ((), ('--discover', '--file', 'demo-note.md')):
                invalid = self.invoke(*args, *mode)
                self.assertEqual(invalid.returncode, 2)
                self.assertIn('usage:', invalid.stderr)

    def test_directory_evaluation_without_key_returns_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.invoke('evaluate-directory', '--log-dir', tmp)
            self.assertEqual(run.returncode, 2, run.stderr)
            self.assertEqual(json.loads(run.stdout)['gate'], 'NOT_RUN')


if __name__ == '__main__':
    unittest.main()
