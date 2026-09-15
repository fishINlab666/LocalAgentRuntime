import json
from pathlib import Path
import tempfile
import threading
import unittest

from local_agent.agents import builtin_agent
from local_agent.demo import DemoProvider
from local_agent.web_runs import WebRuns, WebError
from test_runtime import ScriptedProvider, call_message, final_message


class AgentWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.runs = WebRuns(self.workspace, self.root / 'runs', DemoProvider)
        self.addCleanup(self.runs.close)

    def test_create_custom_agent_session_and_filter_ownership(self):
        self.assertTrue(hasattr(self.runs, 'save_agent'), 'assistant management not implemented')
        raw = builtin_agent('directory').to_dict()
        raw.update(id='project-a', name='项目A')
        saved = self.runs.save_agent(raw)['agent']
        created = self.runs.create_session({'title': '持续项目', 'scope': {'mode': 'directory'},
                                          'agent_id': saved['id']})['session']
        self.assertEqual(created['agent_id'], 'project-a')
        self.assertEqual(created['agent_revision'], saved['revision'])
        self.assertEqual(len(self.runs.list_sessions(agent_id='project-a')['sessions']), 1)
        self.assertEqual(self.runs.list_sessions(agent_id='file-qa')['sessions'], [])
        with self.assertRaises(WebError):
            self.runs.require_agent_session(created['id'], 'file-qa')
        self.assertEqual(self.runs.require_agent_session(created['id'], 'project-a').id, created['id'])

    def test_disabled_agent_cannot_create_session(self):
        self.assertTrue(hasattr(self.runs, 'set_agent_enabled'), 'assistant management not implemented')
        self.runs.set_agent_enabled('directory-qa', False)
        with self.assertRaises(WebError):
            self.runs.create_session({'title': '不可执行', 'scope': {'mode': 'directory'},
                                     'agent_id': 'directory-qa'})

    def test_capability_management_preserves_old_session_snapshot(self):
        self.assertTrue(hasattr(self.runs, 'capability_action'))
        old = self.runs.create_session({'title': '原配置', 'scope': {'mode': 'directory'},
                                       'agent_id': 'project-brief'})['session']
        source = Path(__file__).parents[1] / 'examples/skills/project-brief'
        installed = self.runs.capability_action('skill', None, 'install', {'path': str(source)})['skill']
        self.runs.bind_capability('project-brief', {'kind': 'skill', 'id': installed['id'],
                                                  'version': installed['version']})
        fresh = self.runs.create_session({'title': '新配置', 'scope': {'mode': 'directory'},
                                         'agent_id': 'project-brief'})['session']
        self.assertEqual(old['agent_snapshot']['skills'], [])
        self.assertEqual(fresh['agent_snapshot']['skills'][0]['id'], installed['id'])
        self.assertEqual(len(self.runs.list_capabilities()['skills']), 1)

    def test_dynamic_files_get_separate_approvals_and_receipts_in_both_web_entries(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                case = self.root / ('persistent' if persistent else 'one-shot')
                case.mkdir()
                workspace = case / 'workspace'
                workspace.mkdir()
                (workspace / 'a.md').write_text('代号：orange-731\n', encoding='utf-8')
                contents = {'brief.md': '# 摘要\n代号：orange-731\n', 'tasks.txt': '核对 orange-731\n'}
                replies = [call_message()]
                replies.extend(call_message(f'write-{index}', name='write_file', arguments=json.dumps({
                    'intent': '保存任务结果', 'path': path, 'content': content}))
                    for index, (path, content) in enumerate(contents.items()))
                replies.append(final_message())
                provider = ScriptedProvider(replies)
                runs = WebRuns(workspace, case / 'runs', lambda: provider)
                self.addCleanup(runs.close)
                approvals, done = [], threading.Event()

                if persistent:
                    publish, execute = runs._publish_session, runs._execute_session
                    def observe_publish(session_id, run_id, event):
                        publish(session_id, run_id, event)
                        if event['event'] == 'approval.required':
                            broker = runs.session_jobs[(session_id, run_id)]['approvals']
                            approvals.append(broker.snapshot())
                            runs.decide_session(session_id, run_id, approvals[-1]['id'], 'allow')
                    runs._publish_session = observe_publish
                    def observe_execute(key, job):
                        try:
                            execute(key, job)
                        finally:
                            done.set()
                    runs._execute_session = observe_execute
                    session = runs.create_session({'title': '动态多文件',
                        'scope': {'mode': 'file', 'file': 'a.md'}})['session']
                    started, _ = runs.start_session_run(session['id'], {
                        'client_request_id': 'write-two', 'task_type': 'files', 'question': '生成两份文件'})
                    run_id = started['run']['id']
                else:
                    publish, execute = runs._publish, runs._execute
                    def observe_publish(run_id, event):
                        publish(run_id, event)
                        if event['event'] == 'approval.required':
                            approvals.append(runs.jobs[run_id]['approvals'].snapshot())
                            runs.decide(run_id, approvals[-1]['id'], 'allow')
                    runs._publish = observe_publish
                    def observe_execute(runtime, job):
                        try:
                            execute(runtime, job)
                        finally:
                            done.set()
                    runs._execute = observe_execute
                    run_id = runs.start({'file': 'a.md', 'question': '生成两份文件'})['id']

                self.assertTrue(done.wait(3), 'the dynamic file run did not finish')
                snapshot = (runs.session_snapshot(session['id'], run_id)['run']
                            if persistent else runs.snapshot(run_id))
                self.assertEqual(snapshot['state'], 'completed', snapshot.get('result'))
                self.assertIsNone(snapshot['output_file'])
                self.assertEqual([item['path'] for item in approvals], list(contents))
                self.assertEqual(len({item['id'] for item in approvals}), 2)
                self.assertEqual([item['call_id'] for item in approvals], ['write-0', 'write-1'])
                self.assertEqual([item['path'] for item in snapshot['result']['artifacts']], list(contents))
                for path, content in contents.items():
                    self.assertEqual((workspace / path).read_text(encoding='utf-8'), content)
                for index in range(2):
                    message = next(item for item in provider.requests[index + 2]
                                   if item.get('tool_call_id') == f'write-{index}')
                    self.assertTrue(json.loads(message['content'])['ok'])
                if persistent:
                    self.assertEqual(len(snapshot['approvals']), 2)
                    self.assertEqual(len(snapshot['artifacts']), 2)
