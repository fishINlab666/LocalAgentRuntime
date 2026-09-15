import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


class ExtensionAcceptanceTests(unittest.TestCase):
    def test_r3_requires_the_active_summary_to_retain_fixed_state(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        script = self.load_script()

        class Connection:
            @staticmethod
            def execute(_query, _parameters):
                return [(json.dumps({'manifest': {'summary_id': 'summary-1'}}),)]

        class Store:
            def __init__(self, text):
                self.summary = SimpleNamespace(
                    id='summary-1', payload={
                        'goals': [], 'constraints': [], 'decisions': [],
                        'completed': [], 'pending': [{'text': text}], 'anchors': [],
                    })

            def connection(self):
                return Connection()

            def load_active_summary(self, _session_id):
                return self.summary

        result = {
            'run_id': 'run-3', 'state': 'completed',
            'answer': {'answer': '2026-09-22；联调验证、验收记录'},
        }
        recorder = SimpleNamespace(requests=[{'purpose': 'summary'}, {
            'purpose': 'task', 'messages': [],
        }])
        case = script.FIXED_CASES[2]
        session = SimpleNamespace(id='session-1')
        with patch.object(script, '_tool_rows', return_value=[]):
            retained, _ = script._check_case(
                case, result, recorder,
                Store('旧值 2026-09-20；新值 2026-09-22；未完：联调验证、验收记录'),
                session, Path('.'), SimpleNamespace(decisions=[]), False,
            )
            missing, _ = script._check_case(
                case, result, recorder, Store('新值 2026-09-22'),
                session, Path('.'), SimpleNamespace(decisions=[]), False,
            )

        self.assertTrue(retained['summary_retains_old_new_and_pending'])
        self.assertFalse(missing['summary_retains_old_new_and_pending'])

    def test_terminal_rejection_is_refilled_once_and_stops(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        script = self.load_script()
        with tempfile.TemporaryDirectory() as temporary:
            row = {'call_id': 'denied', 'name': 'write_file', 'arguments': {},
                   'result': {'ok': False, 'error': {'code': 'USER_REJECTED'}}}
            with patch.object(script, '_tool_rows', return_value=[row]):
                checks, _ = script._check_case(script.FIXED_CASES[-1],
                    {'run_id': 'r', 'state': 'unable', 'stop_reason': 'USER_REJECTED',
                     'answer': {'status': 'unable', 'answer': 'brief-denied.md 没有创建',
                                'citations': []}},
                    SimpleNamespace(requests=[{
                        'purpose': 'task',
                        'messages': [{
                            'role': 'tool', 'tool_call_id': 'denied',
                            'content': json.dumps(row['result']),
                        }],
                    }]), None, SimpleNamespace(id='s'), Path(temporary),
                    SimpleNamespace(decisions=[{'decision': 'deny'}]), False)
            self.assertTrue(all(checks.values()), checks)

    def load_script(self):
        path = Path(__file__).parents[1] / 'scripts/evaluate_extensions.py'
        self.assertTrue(path.exists(), 'The fixed extension acceptance script is not implemented')
        spec = importlib.util.spec_from_file_location('extensions_acceptance_script', path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_dry_run_uses_real_package_stdio_file_approval_and_context_results(self):
        script = self.load_script()
        with tempfile.TemporaryDirectory() as directory:
            report = script.run_acceptance(Path(directory) / 'attempt', mode='dry-run')
            self.assertEqual(report['model_api_calls'], 0)
            self.assertEqual(report['mode'], 'dry-run')
            self.assertEqual(len(report['fixed_cases']), 6)
            self.assertEqual(len(report['case_results']), 1)
            case = report['case_results'][0]
            self.assertEqual(case['machine_status'], 'PASS', case)
            for name in ('body_not_preloaded', 'unused_skill_absent', 'main_and_reference_read',
                         'tool_results_refilled', 'local_and_mcp_citations',
                         'output_matches_approved_preview', 'prompt_read'):
                self.assertTrue(case['checks'][name], name)
            self.assertEqual(case['semantic_review']['status'], 'pending')
            self.assertFalse(report['release_ready'])
            self.assertTrue((Path(report['workspace']) / 'brief-approved.md').is_file())
            self.assertTrue(Path(case['trace_path']).is_file())
            self.assertTrue(Path(case['requests_path']).is_file())
            saved = json.loads(Path(report['report_path']).read_text(encoding='utf-8'))
            self.assertEqual(saved['case_results'][0]['machine_status'], 'PASS')

    def test_fixed_budget_counts_summary_and_repair_requests_and_stops_before_call(self):
        script = self.load_script()
        budget = script.RequestBudget()
        for number in range(1, 7):
            for _ in range(10):
                budget.consume(f'R{number}', real=True)
            with self.assertRaisesRegex(Exception, 'ACCEPTANCE_MODEL_BUDGET'):
                budget.consume(f'R{number}', real=True)
        self.assertEqual(budget.total, 60)
        self.assertEqual(budget.real_calls, 60)

    def test_prior_attempt_is_charged_to_group_but_new_run_gets_its_own_limit(self):
        script = self.load_script()
        budget = script.RequestBudget({'R1': 2})
        self.assertEqual(budget.total, 2)
        for _ in range(10):
            budget.consume('R1', real=True)
        with self.assertRaisesRegex(Exception, 'ACCEPTANCE_MODEL_BUDGET'):
            budget.consume('R1', real=True)
        self.assertEqual(budget.real_calls, 12)
        self.assertEqual(budget.by_case['R1'], 12)
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            report.write_text(json.dumps({'mode': 'real', 'model_api_calls': 2,
                                          'requests_by_case': {'R1': 2}}))
            self.assertEqual(script.load_prior_usage(report), {'R1': 2})

    def test_cli_finds_the_latest_real_report_before_starting_another_attempt(self):
        script = self.load_script()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dry = root / 'dry' / 'report.json'
            real = root / 'real' / 'report.json'
            dry.parent.mkdir(); real.parent.mkdir()
            dry.write_text(json.dumps({'mode': 'dry-run'}))
            real.write_text(json.dumps({'mode': 'real', 'model_api_calls': 2,
                                        'requests_by_case': {'R1': 2}}))
            self.assertEqual(script.latest_prior_report(root), real)

    def test_resume_clones_passed_prefix_and_rebinds_the_workspace(self):
        script = self.load_script()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior_root = root / 'prior'
            prior = script.run_acceptance(prior_root, mode='dry-run')
            prior_path = Path(prior['report_path'])
            saved = json.loads(prior_path.read_text(encoding='utf-8'))
            saved.update(mode='real', model_api_calls=10,
                         requests_by_case={'R1': 10})
            prior_path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
            original_database = (prior_root / 'state' / 'sessions.sqlite3').read_bytes()
            current_root = root / 'current'
            current_root.mkdir()

            self.assertTrue(hasattr(script, '_resume_setup'),
                            'acceptance resume setup is not implemented')
            workspace, store, service, session, library, agents, carried = \
                script._resume_setup(current_root, prior_path)
            try:
                self.assertEqual([item['id'] for item in carried], ['R1'])
                self.assertEqual(carried[0]['carried_from_report'], str(prior_path.resolve()))
                self.assertEqual(Path(session.workspace_path), workspace)
                self.assertEqual(workspace, (current_root / 'workspace').resolve())
                self.assertTrue((workspace / 'brief-approved.md').is_file())
                self.assertTrue(store.load_run_messages(session.id, carried[0]['run_id']))
                self.assertEqual(library.root, current_root / 'state')
                self.assertEqual(agents.root, current_root / 'state' / 'agents')
            finally:
                store.close()
            self.assertEqual((prior_root / 'state' / 'sessions.sqlite3').read_bytes(),
                             original_database)

    def test_existing_expansion_fixtures_are_reused_without_new_journal_rows(self):
        script = self.load_script()
        from local_agent.session_store import SessionStore
        from local_agent.sessions import SessionScope, SessionService
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'attempt'
            root.mkdir()
            workspace = root / 'workspace'
            workspace.mkdir()
            store = SessionStore.open(root / 'state')
            service = SessionService(store)
            session = service.create(
                workspace, 'fixture resume', SessionScope('directory', None)
            )
            try:
                records = script._seed_expansion(service, session)
                before = store.connection().execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id=?", (session.id,)
                ).fetchone()[0]
                self.assertTrue(hasattr(script, '_reuse_expansion'),
                                'fixture resume validation is not implemented')
                reused = script._reuse_expansion(service, session, records)
                after = store.connection().execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id=?", (session.id,)
                ).fetchone()[0]
                self.assertEqual(reused, records)
                self.assertEqual(after, before)
                self.assertTrue(workspace.is_dir())
            finally:
                store.close()

    def test_missing_key_returns_structured_no_call_report(self):
        script = self.load_script()
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            report = script.run_acceptance(Path(directory) / 'attempt', mode='real')
        self.assertEqual(report['model_api_calls'], 0)
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['blocking_reason'], 'CONFIG_MISSING')
        self.assertEqual(report['case_results'], [])


if __name__ == '__main__':
    unittest.main()
