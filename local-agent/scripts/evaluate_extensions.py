#!/usr/bin/env python3
"""Fixed six-run extension acceptance. Default: one offline scripted dry-run.

Real mode sends only newly-created synthetic workspace/history and public clock
results to DeepSeek. Synthetic approvals allow only brief-approved.md and deny
brief-denied.md; they exercise the real broker but are not human acceptance.
Each invocation owns a fresh output directory. An authorized rerun clones the
last report's consecutive passed prefix and session into that directory, resumes
at the first incomplete case, and never overwrites the original evidence.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from local_agent.agent_runtime import CapabilityStore
from local_agent.agents import AgentCatalog, builtin_agent
from local_agent.approvals import ApprovalBroker
from local_agent.context import SUMMARY_SYSTEM
from local_agent.provider import DeepSeekProvider, ModelReply, ProviderError
from local_agent.session_store import SessionStore
from local_agent.sessions import RunSubmission, SessionScope, SessionService
from local_agent.trace import Trace


FIXED_CASES = (
    {'id': 'R1', 'name': 'first_joint_task', 'covers': ['S1', 'M1', 'E1'],
     'question': '最初约定：项目青禾-47截止2026-09-20。请根据project-note.md和项目状态服务的本轮最新返回，'
        '为研发团队准备简报，核对负责人、完成数/总数和未完事项。选择合适的已绑定Skill并读取它的引用，'
        '读取项目资源与本轮指定的project_brief模板，然后保存到brief-approved.md。日期与项目编号保留原样。',
     'output': 'brief-approved.md', 'prompt': {'server_id': 'project-demo', 'name': 'project_brief'}},
    {'id': 'R2', 'name': 'restart_and_correction', 'covers': ['A2', 'E2'],
     'question': '更正约定：青禾-47截止日期从2026-09-20改为2026-09-22。未完事项仍是联调验证、验收记录。'
        '请回顾本会话旧约定，区分旧日期与本次更正，并重新查询项目状态服务的最新进展。日期保留原样。'},
    {'id': 'R3', 'name': 'compressed_history_followup', 'covers': ['S2', 'E2'],
     'question': '扩容记录都是离线合成占位资料。请先使用session_history找回本会话真实R1/R2中的旧约定、'
        '更正与未完事项，再重新读取指定Skill的方法；告诉我现在采用哪个截止日期、还剩哪些事项，'
        '并重新查询项目服务获取最新状态。历史值注明当时记录；日期保留YYYY-MM-DD格式。',
     'skill': 'project-brief'},
    {'id': 'R4', 'name': 'second_skill_and_time_server', 'covers': ['E3'],
     'question': '请使用本轮指定的状态检查Skill和已绑定的Time服务，读取Asia/Shanghai当前时间。'
        '报告时区、服务返回的完整时间及来源；不要沿用其他会话或项目的值。', 'skill': 'status-triage'},
    {'id': 'R5', 'name': 'mcp_error', 'covers': ['M3'],
     'question': '请调用项目状态服务查询不存在的项目不存在-99。这是错误收口验收：若服务返回错误，'
        '说明没有取得该项目数据并合理结束，不替换为其他项目，也不要把错误写成成功。'},
    {'id': 'R6', 'name': 'rejected_write', 'covers': ['E1'],
     'question': '这是合成文件的拒绝写入验收。请读取project-note.md及当前项目状态，整理成brief-denied.md。'
        '本次审批会拒绝；收到拒绝后结束并说明文件没有创建，不能再尝试其他输出路径。',
     'output': 'brief-denied.md'},
)
MAIN_MARKER = '先用 read_skill 读取本包 references/source-checklist.md'
SECOND_MARKER = '只把结果中实际出现的异常写入清单'
NOTE = ('以下全部为本地扩展验收的合成资料，不对应真实项目。\n项目：青禾-47\n'
        '负责人：林澄\n最初约定：截止2026-09-20。\n未完事项：联调验证、验收记录。\n')


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _save(path, value):
    with Path(path).open('x', encoding='utf-8') as output:
        output.write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n')


class RequestBudget:
    """Every Provider.complete, including summaries and repairs, consumes one."""
    def __init__(self, prior=None):
        prior = {} if prior is None else copy.deepcopy(prior)
        valid_ids = {case['id'] for case in FIXED_CASES}
        if (not isinstance(prior, dict) or set(prior) - valid_ids
                or any(type(value) is not int or not 0 <= value <= 60 for value in prior.values())
                or sum(prior.values()) > 60):
            raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID')
        self.total = self.real_calls = sum(prior.values())
        self.by_case = prior
        self.attempt_by_case = {}
        self._lock = threading.Lock()

    def consume(self, case_id, *, real):
        with self._lock:
            if (case_id not in {case['id'] for case in FIXED_CASES}
                    or self.total >= 60 or self.attempt_by_case.get(case_id, 0) >= 10):
                raise ProviderError('ACCEPTANCE_MODEL_BUDGET')
            self.total += 1
            self.real_calls += int(real)
            self.by_case[case_id] = self.by_case.get(case_id, 0) + 1
            self.attempt_by_case[case_id] = self.attempt_by_case.get(case_id, 0) + 1


def load_prior_usage(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        usage = value['requests_by_case']
        if (value.get('mode') != 'real' or not isinstance(usage, dict)
                or value.get('model_api_calls') != sum(usage.values())):
            raise ValueError('not a real fixed-group report')
        RequestBudget(usage)
        return copy.deepcopy(usage)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID') from error


def latest_prior_report(root):
    candidates = []
    for path in Path(root).rglob('report.json') if Path(root).exists() else ():
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID') from error
        if value.get('mode') == 'real':
            load_prior_usage(path)
            candidates.append(path)
    return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None


def _passed_prefix(report):
    results = report.get('case_results')
    if not isinstance(results, list) or len(results) > len(FIXED_CASES):
        raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID')
    passed = []
    failed = False
    for index, entry in enumerate(results):
        if (not isinstance(entry, dict) or entry.get('id') != FIXED_CASES[index]['id']
                or entry.get('machine_status') not in {'PASS', 'FAIL'} or failed):
            raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID')
        if entry['machine_status'] == 'FAIL':
            failed = True
            continue
        passed.append(copy.deepcopy(entry))
    return passed


def _resume_setup(root, prior_report):
    path = Path(prior_report).resolve()
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
        load_prior_usage(path)
        passed = _passed_prefix(report)
        prior_root = path.parent
        old_workspace = Path(report['workspace']).resolve()
        if (Path(report['report_path']).resolve() != path
                or old_workspace != (prior_root / 'workspace').resolve()
                or not passed or not (prior_root / 'state' / 'sessions.sqlite3').is_file()
                or not old_workspace.is_dir()):
            raise ValueError('invalid resumable evidence')
        shutil.copytree(prior_root / 'state', root / 'state')
        shutil.copytree(old_workspace, root / 'workspace')
        workspace = (root / 'workspace').resolve()
        identity = workspace.stat()
        store = SessionStore.open(root / 'state')
        try:
            changed = store.connection().execute(
                """UPDATE sessions
                   SET workspace_path=?, workspace_device=?, workspace_inode=?
                   WHERE workspace_path=?""",
                (str(workspace), identity.st_dev, identity.st_ino, str(old_workspace)),
            ).rowcount
            store.connection().commit()
            if changed < 1:
                raise ValueError('prior session workspace not found')
            service = SessionService(store)
            snapshot = report['agent_snapshot']
            session = service.load(report['session_id'], agent_id=snapshot['id'])
            library = CapabilityStore(root / 'state')
            agents = AgentCatalog(root / 'state' / 'agents')
        except Exception:
            store.close()
            raise
        carried = []
        for entry in passed:
            entry['carried_from_report'] = str(path)
            carried.append(entry)
        return workspace, store, service, session, library, agents, carried
    except ProviderError:
        raise
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID') from error


class RecordingProvider:
    def __init__(self, provider, budget, case_id, path, *, real):
        self.provider, self.budget, self.case_id = provider, budget, case_id
        self.path, self.real = Path(path), real
        self.path.touch(mode=0o600, exist_ok=False)
        self.metadata = copy.deepcopy(provider.metadata)
        self.requests, self.usages = [], []

    def complete(self, messages, tools, timeout):
        self.budget.consume(self.case_id, real=self.real)
        purpose = 'summary' if messages and messages[0].get('content') == SUMMARY_SYSTEM else 'task'
        request = {'purpose': purpose, 'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools)}
        self.requests.append(request)
        with self.path.open('a', encoding='utf-8') as output:
            output.write(_json({'event': 'request', 'number': len(self.requests), **request}) + '\n')
        reply = self.provider.complete(messages, tools, timeout)
        self.usages.append(copy.deepcopy(reply.usage))
        message = {key: copy.deepcopy(reply.message[key]) for key in ('role', 'content', 'tool_calls')
                   if key in reply.message}
        with self.path.open('a', encoding='utf-8') as output:
            output.write(_json({'event': 'reply', 'number': len(self.requests),
                                'message': message, 'usage': reply.usage}) + '\n')
        return reply


class SyntheticApprovalBroker(ApprovalBroker):
    """Fixed decisions only for the two files in this newly-created fixture."""
    def __init__(self, run_id, workspace, output_path, *, journal, publish):
        super().__init__(run_id, journal=journal, publish=publish)
        self.workspace, self.output_path = Path(workspace).resolve(), output_path
        self.decisions = []

    def _emit(self, event, approval):
        super()._emit(event, approval)
        if event != 'approval.required':
            return
        data = approval.data
        allowed = (data.get('name') == 'write_file' and data.get('source') == 'builtin'
                   and data.get('path') == self.output_path
                   and self.output_path in {'brief-approved.md', 'brief-denied.md'}
                   and (self.workspace / self.output_path).resolve().parent == self.workspace
                   and isinstance(data.get('content'), str))
        decision = 'allow' if allowed and self.output_path == 'brief-approved.md' else 'deny'
        self.decide(data['approval_id'], decision)
        self.decisions.append({'decision': decision, 'path': data.get('path'),
            'name': data.get('name'), 'call_id': data.get('call_id'),
            'preview_sha256': hashlib.sha256(data.get('content', '').encode('utf-8')).hexdigest(),
            'kind': 'synthetic_fixture_decision', 'human_approval': False})


def _call(name, arguments, call_id):
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': _json(arguments)}}


def _calls(*calls):
    return ModelReply({'role': 'assistant', 'content': None, 'tool_calls': list(calls)})


def _remote_name(tools, server_id, remote_name):
    # Resolve from discovered definitions; never derive or hardcode a generated name.
    matches = [tool['function']['name'] for tool in tools
               if tool['function'].get('description', '').startswith(f'MCP {server_id}/{remote_name}:')]
    if len(matches) != 1:
        raise ValueError('The expected discovered MCP capability is unavailable')
    return matches[0]


def _content(value):
    if isinstance(value, dict):
        return '\n'.join(value[key] for key in sorted(value, key=lambda key: int(key)))
    return value if isinstance(value, str) else ''


class DryRunProvider:
    """Script R1 only. Answers are constructed after observing actual results."""
    metadata = {'provider': 'scripted-acceptance', 'simulated': True}

    def __init__(self):
        self.step = 0

    def complete(self, messages, tools, timeout):
        self.step += 1
        results = {message['tool_call_id']: json.loads(message['content']) for message in messages
                   if message.get('role') == 'tool'}
        if self.step == 1:
            return _calls(_call('read_skill', {'skill_id': 'project-brief', 'relative_path': 'SKILL.md',
                'intent': '按目录选择项目简报方法'}, 'skill-main'),
                _call('list_files', {'path': '.', 'intent': '发现合成项目资料'}, 'list'))
        if self.step == 2:
            assert results['skill-main']['ok'] and MAIN_MARKER in _content(results['skill-main']['data']['content'])
            return _calls(_call('read_skill', {'skill_id': 'project-brief',
                'relative_path': 'references/source-checklist.md', 'intent': '读取来源检查表'}, 'skill-reference'),
                _call('read_file', {'path': 'project-note.md', 'intent': '取得本地项目事实'}, 'local-note'),
                _call(_remote_name(tools, 'project-demo', 'project_status'),
                    {'intent': '取得本轮项目进展', 'arguments': {'project_id': '青禾-47'}}, 'remote-status'),
                _call(_remote_name(tools, 'project-demo', 'read_resource'),
                    {'intent': '读取项目资源', 'arguments': {'uri': 'project://qinghe/notes'}}, 'remote-resource'))
        if self.step == 3:
            assert all(results[key]['ok'] for key in ('local-note', 'remote-status', 'remote-resource', 'skill-reference'))
            return _calls(_call(_remote_name(tools, 'project-demo', 'get_prompt'),
                {'intent': '取得用户选择的研发团队模板', 'arguments': {
                    'name': 'project_brief', 'arguments': {'audience': '研发团队'}}}, 'remote-prompt'))
        status = results['remote-status']['data']['structured_content']
        assert status['project_id'] == '青禾-47' and '2026-09-20' in _content(results['local-note']['data']['content'])
        content = (f"项目：{status['project_id']}\n负责人：{status['owner']}\n"
            f"完成：{status['completed']}/{status['total']}\n截止：2026-09-20\n"
            '未完事项：联调验证、验收记录。\n来源：project-note.md 与本轮项目状态服务。\n')
        if self.step == 4:
            assert results['remote-prompt']['ok']
            return _calls(_call('write_file', {'path': 'brief-approved.md', 'content': content,
                'intent': '保存本轮有来源的合成简报'}, 'write-report'))
        assert results['write-report']['ok']
        return ModelReply({'role': 'assistant', 'content': _json({'status': 'answered', 'answer': content,
            'citations': [{'path': 'project-note.md', 'start_line': 2, 'end_line': 5},
                {'source_id': results['remote-status']['data']['source_id'], 'start_line': 1, 'end_line': 1}]})})


def _server_config(server_id, args, tools, resources=(), prompts=()):
    return {'id': server_id, 'command': sys.executable, 'args': list(args), 'cwd': str(PROJECT),
            'env_names': [], 'allowed_tools': list(tools), 'allowed_resources': list(resources),
            'allowed_prompts': list(prompts), 'timeout_seconds': 5}


def _setup(root):
    workspace = root / 'workspace'
    workspace.mkdir(mode=0o700)
    (workspace / 'project-note.md').write_text(NOTE, encoding='utf-8')
    state = root / 'state'
    library, agents = CapabilityStore(state), AgentCatalog(state / 'agents')
    installed = [library.skills.install(PROJECT / 'examples/skills' / name)
                 for name in ('project-brief', 'status-triage')]
    agent = builtin_agent('combined').to_dict()
    agent['id'], agent['name'] = 'acceptance-project', '合成项目验收'
    agent['skills'] = [{'id': item['id'], 'version': item['version']} for item in installed]
    primary = library.install_server(_server_config('project-demo',
        ['-B', str(PROJECT / 'examples/mcp-project/server.py')], ['project_status'],
        ['project://qinghe/notes'], ['project_brief']))
    agent['mcp'] = [{'id': primary['id'], 'version': primary['version'],
        'tools': primary['allowed_tools'], 'resources': primary['allowed_resources'], 'prompts': primary['allowed_prompts']}]
    saved = agents.save(agent)
    store = SessionStore.open(state)
    service = SessionService(store)
    session = service.create(workspace, '固定联合验收', SessionScope('directory', None), agent_snapshot=saved.to_dict())
    return workspace, store, service, session, library, agents


def _seed_expansion(service, session):
    """Explicit fixture records, never counted as real user/model interactions."""
    records = []
    for number in range(6):
        marker = f'离线合成扩容记录-{number + 1}：只用于触发上下文压缩，不是用户约定或模型执行证明。'
        padding = '占位资料' * 700
        prepared = service.submit(session.id, RunSubmission(f'fixture-{number + 1}', marker + padding,
            'conversation', session.scope, None, None, {}))
        prepared.journal.record_model_request({'messages': []}, {'source_kind': 'offline_fixture',
            'simulated': True, 'model_api_calls': 0})
        prepared.journal.record_model_reply({'role': 'assistant', 'content': marker + padding}, None, 'answer_valid')
        prepared.journal.finish_run({'state': 'completed', 'stop_reason': 'OFFLINE_FIXTURE',
            'answer': marker, 'model_calls': 0, 'provider': {'simulated': True, 'provider': 'fixture-journal'}})
        records.append({'run_id': prepared.run_id, 'source': 'offline_synthetic_fixture', 'model_api_calls': 0})
    return records


def _reuse_expansion(service, session, records):
    """Validate and reuse cloned offline fixtures without appending journal rows."""
    if not isinstance(records, list) or len(records) != 6:
        raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID')
    reused = []
    seen = set()
    for number, item in enumerate(records, 1):
        if (not isinstance(item, dict)
                or set(item) != {'run_id', 'source', 'model_api_calls'}
                or not isinstance(item.get('run_id'), str)
                or not item['run_id']
                or item['run_id'] in seen
                or item.get('source') != 'offline_synthetic_fixture'
                or item.get('model_api_calls') != 0):
            raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID')
        row = service.store.connection().execute(
            """SELECT client_request_id, state, stop_reason, result_json
               FROM runs WHERE session_id=? AND id=?""",
            (session.id, item['run_id']),
        ).fetchone()
        try:
            result = json.loads(row[3]) if row is not None else None
        except (TypeError, ValueError, json.JSONDecodeError):
            result = None
        messages = (
            service.store.load_run_messages(session.id, item['run_id'])
            if row is not None else ()
        )
        if (row is None
                or row[0] != f'fixture-{number}'
                or row[1] != 'completed'
                or row[2] != 'OFFLINE_FIXTURE'
                or not isinstance(result, dict)
                or result.get('state') != 'completed'
                or result.get('stop_reason') != 'OFFLINE_FIXTURE'
                or result.get('model_calls') != 0
                or result.get('provider', {}).get('simulated') is not True
                or len(messages) != 2
                or messages[0].role != 'user'
                or messages[1].role != 'assistant'
                or messages[1].validation_state != 'answer_valid'):
            raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID')
        seen.add(item['run_id'])
        reused.append(copy.deepcopy(item))
    return reused


def _tool_rows(store, session_id, run_id):
    rows, calls = [], {}
    for message in store.load_run_messages(session_id, run_id):
        if message.role == 'assistant':
            for call in message.payload.get('tool_calls', []):
                calls[call['id']] = call['function']
        if message.role == 'tool':
            call_id = message.payload['tool_call_id']
            function = calls.get(call_id, {})
            rows.append({'call_id': call_id, 'name': function.get('name'),
                'arguments': json.loads(function.get('arguments', '{}')), 'result': message.payload['result']})
    return rows


def _check_case(case, result, recorder, store, session, workspace, broker, restarted):
    rows = _tool_rows(store, session.id, result['run_id'])
    successful = [row for row in rows if row['result'].get('ok')]
    data = [row['result'].get('data', {}) for row in successful]
    task_requests = [request for request in recorder.requests if request['purpose'] == 'task']
    first = _json(task_requests[0]) if task_requests else ''
    observed = {message['tool_call_id'] for request in task_requests for message in request['messages']
                if message.get('role') == 'tool'}
    all_checks = {'tool_results_refilled': bool(rows) and all(row['call_id'] in observed for row in rows),
                  'model_budget': len(recorder.requests) <= 10}
    answer = result.get('answer') or {}
    citations = answer.get('citations', [])
    local_cited = any('path' in citation for citation in citations)
    mcp_cited = any(citation.get('source', {}).get('type') == 'mcp' for citation in citations)
    skill_paths = {(row['arguments'].get('skill_id'), row['arguments'].get('relative_path'))
                   for row in successful if row['name'] == 'read_skill'}
    if case['id'] == 'R1':
        path = workspace / case['output']
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        all_checks.update(completed=result.get('state') == 'completed', body_not_preloaded=MAIN_MARKER not in first,
            unused_skill_absent=all(SECOND_MARKER not in _json(request) for request in task_requests),
            main_and_reference_read={('project-brief', 'SKILL.md'), ('project-brief', 'references/source-checklist.md')} <= skill_paths,
            local_and_mcp_citations=local_cited and mcp_cited,
            resource_read=any(item.get('source_kind') == 'mcp_resource' for item in data),
            prompt_read=any(item.get('source_kind') == 'mcp_prompt' for item in data),
            output_matches_approved_preview=bool(actual_hash) and any(
                decision['decision'] == 'allow' and decision['preview_sha256'] == actual_hash for decision in broker.decisions)
                and any(item.get('sha256') == actual_hash and item.get('path') == case['output']
                        for item in result.get('artifacts', [])))
    elif case['id'] == 'R2':
        all_checks.update(completed=result.get('state') == 'completed', restart_preserved=restarted,
            correction_present='2026-09-22' in answer.get('answer', ''),
            latest_status_read=any(item.get('remote_name') == 'project_status' for item in data))
    elif case['id'] == 'R3':
        manifests = [json.loads(row[0])['manifest'] for row in store.connection().execute(
            'SELECT payload_json FROM context_manifests WHERE run_id=?', (result['run_id'],))]
        active_summary = store.load_active_summary(session.id)
        summary_text = '\n'.join(
            fact.get('text', '')
            for field in ('goals', 'constraints', 'decisions', 'completed', 'pending', 'anchors')
            for fact in (active_summary.payload.get(field, []) if active_summary else [])
            if isinstance(fact, dict)
        )
        all_checks.update(completed=result.get('state') == 'completed',
            summary_saved=active_summary is not None,
            summary_used=active_summary is not None
                and any(item.get('summary_id') == active_summary.id for item in manifests),
            summary_retains_old_new_and_pending=all(text in summary_text
                for text in ('2026-09-20', '2026-09-22', '联调验证', '验收记录')),
            summary_request_counted=any(item['purpose'] == 'summary' for item in recorder.requests),
            history_read=any(row['name'] == 'session_history' for row in successful),
            skill_reread=('project-brief', 'SKILL.md') in skill_paths,
            correction_and_pending=all(text in answer.get('answer', '')
                for text in ('2026-09-22', '联调验证', '验收记录')),
            latest_status_read=any(item.get('remote_name') == 'project_status' for item in data))
    elif case['id'] == 'R4':
        all_checks.update(completed=result.get('state') == 'completed',
            second_skill_read=('status-triage', 'SKILL.md') in skill_paths,
            independent_time_server=any(item.get('server_id') == 'official-time'
                and item.get('remote_name') == 'get_current_time' for item in data),
            correct_timezone=any(row['arguments'].get('arguments', {}).get('timezone') == 'Asia/Shanghai'
                for row in successful), source_citation=mcp_cited)
    elif case['id'] == 'R5':
        all_checks.update(reasonable_error_end=result.get('state') == 'unable',
            server_error_received=any(row['result'].get('error', {}).get('code') == 'MCP_TOOL_ERROR' for row in rows))
    else:
        rejection_indexes = [index for index, row in enumerate(rows)
            if row['result'].get('error', {}).get('code') == 'USER_REJECTED']
        rejected_ids = {rows[index]['call_id'] for index in rejection_indexes}
        rejection_refills = sum(
            1 for request in task_requests
            if any(message.get('role') == 'tool'
                and message.get('tool_call_id') in rejected_ids
                for message in request['messages'])
        )
        first_rejection = rejection_indexes[0] if rejection_indexes else len(rows)
        all_checks.update(preceding_results_refilled=all(
            row['call_id'] in observed for row in rows[:first_rejection]),
            terminal_rejection_recorded=len(rejection_indexes) == 1
                and result.get('stop_reason') == 'USER_REJECTED',
            rejection_refilled_once=rejection_refills == 1,
            no_write_retry_after_rejection=not any(
                row['name'] == 'write_file' for row in rows[first_rejection + 1:]))
        all_checks.update(reasonable_rejection_end=result.get('state') == 'unable',
            rejection_explained=answer.get('status') == 'unable'
                and case['output'] in answer.get('answer', '')
                and any(text in answer.get('answer', '') for text in ('没有创建', '未创建')),
            rejection_received=any(row['result'].get('error', {}).get('code') == 'USER_REJECTED' for row in rows),
            approval_denied=any(item['decision'] == 'deny' for item in broker.decisions),
            no_output_file=not (workspace / case['output']).exists(), no_artifact=not result.get('artifacts'))
    return all_checks, rows


def run_acceptance(root, *, mode='dry-run', progress=None, prior_usage=None,
                   prior_report=None):
    if mode not in {'dry-run', 'real'}:
        raise ValueError('Mode must be dry-run or real')
    root = Path(root).resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    report_path = root / 'report.json'
    report = {'mode': mode, 'status': 'running', 'report_path': str(report_path),
        'workspace': str(root / 'workspace'), 'fixed_cases': list(FIXED_CASES), 'case_results': [],
        'model_api_calls': 0, 'model_requests_including_simulation': 0,
        'limits': {'runs': 6, 'requests_per_run_including_summary_and_repair': 10, 'total_requests': 60},
        'fixture_records': [], 'release_ready': False,
        'semantic_review': {'status': 'pending', 'reviewer': None},
        'user_acceptance': {'status': 'pending'},
        'approval_mode': 'synthetic_fixture_only_allow_and_deny_not_human_acceptance',
        'rerun_policy': ('No retry inside one invocation. An authorized later invocation clones the passed prefix, '
                         'resumes at the first incomplete case, and charges new calls to the remaining group budget.')}
    if (prior_usage or prior_report) and mode != 'real':
        raise ValueError('Prior usage is only valid for real mode')
    if prior_report is not None:
        evidence_usage = load_prior_usage(prior_report)
        if prior_usage is None:
            prior_usage = evidence_usage
        elif prior_usage != evidence_usage:
            raise ProviderError('ACCEPTANCE_PRIOR_REPORT_INVALID')
    budget, store = RequestBudget(prior_usage), None
    prior_calls = budget.real_calls
    report['prior_model_api_calls'] = prior_calls
    report['prior_requests_by_case'] = copy.deepcopy(prior_usage or {})
    started = time.monotonic()
    try:
        provider = DeepSeekProvider.from_env() if mode == 'real' else None
        versions = {}
        for package in ('mcp', 'PyYAML', 'jsonschema', 'mcp-server-time'):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        report['dependency_versions'] = versions
        required = ('mcp', 'PyYAML', 'jsonschema') + (('mcp-server-time',) if mode == 'real' else ())
        if any(not versions[package] for package in required):
            raise ProviderError('EXTENSION_DEPENDENCY_MISSING')
        if mode == 'real' and versions['mcp-server-time'] != '2026.8.18':
            raise ProviderError('TIME_SERVER_VERSION_MISMATCH')
        carried = []
        if prior_report is not None:
            prior_value = json.loads(Path(prior_report).read_text(encoding='utf-8'))
            if _passed_prefix(prior_value):
                workspace, store, service, session, library, agents, carried = \
                    _resume_setup(root, prior_report)
        if not carried:
            workspace, store, service, session, library, agents = _setup(root)
        report['case_results'].extend(carried)
        if carried:
            report['resumed_from_report'] = str(Path(prior_report).resolve())
            report['resumed_passed_cases'] = [entry['id'] for entry in carried]
            prior_fixtures = prior_value.get('fixture_records', [])
            if prior_fixtures:
                report['fixture_records'] = _reuse_expansion(
                    service, session, prior_fixtures
                )
            for entry in carried:
                _save(root / f"{entry['id']}-result.json", entry)
        report['session_id'] = session.id
        report['agent_snapshot'] = dict(session.agent_snapshot)
        cases = FIXED_CASES[:1] if mode == 'dry-run' else FIXED_CASES[len(carried):]
        for case in cases:
            if progress:
                progress(f"{case['id']} {case['name']}")
            restarted = False
            if case['id'] == 'R2':
                revision = session.agent_revision
                store.close()
                store = SessionStore.open(root / 'state')
                service = SessionService(store)
                restored = service.load(session.id, agent_id=session.agent_id)
                restarted = restored.agent_revision == revision
                session = restored
            if case['id'] == 'R3':
                if not report['fixture_records']:
                    report['fixture_records'] = _seed_expansion(service, session)
            selected_session = session
            if case['id'] == 'R4':
                server = library.install_server(_server_config('official-time',
                    ['-m', 'mcp_server_time', '--local-timezone=Asia/Shanghai'], ['get_current_time']))
                second = builtin_agent('combined').to_dict()
                second.update(id='acceptance-time', name='第二套时间验收')
                second['skills'] = [binding for binding in dict(session.agent_snapshot)['skills']
                                    if binding['id'] == 'status-triage']
                second['mcp'] = [{'id': server['id'], 'version': server['version'],
                                  'tools': ['get_current_time'], 'resources': [], 'prompts': []}]
                saved = agents.save(second)
                selected_session = service.create(workspace, '第二套能力', SessionScope('directory', None),
                                                   agent_snapshot=saved.to_dict())
            request_id = case['id'] if not carried else f"{case['id']}:{root.name}"
            prepared = service.submit(selected_session.id, RunSubmission(request_id, case['question'], 'files',
                selected_session.scope, case.get('output'), None, {},
                skill_id=case.get('skill'), mcp_prompt=case.get('prompt')))
            trace = Trace(root / 'traces', workspace, debug_content=True, run_id=prepared.run_id)
            broker = SyntheticApprovalBroker(prepared.run_id, workspace, case.get('output'),
                journal=prepared.journal, publish=trace.emit)
            recorder = RecordingProvider(provider if mode == 'real' else DryRunProvider(), budget,
                case['id'], root / f"{case['id']}-requests.jsonl", real=mode == 'real')
            case_started = time.monotonic()
            try:
                result = service.execute(prepared, recorder, trace, approvals=broker)
            finally:
                broker.close()
            checks, rows = _check_case(case, result, recorder, store, selected_session, workspace, broker, restarted)
            machine_status = 'PASS' if all(checks.values()) else 'FAIL'
            entry = {'id': case['id'], 'name': case['name'], 'run_id': prepared.run_id,
                'session_id': selected_session.id, 'machine_status': machine_status, 'checks': checks,
                'failed_checks': [name for name, passed in checks.items() if not passed],
                'state': result.get('state'), 'stop_reason': result.get('stop_reason'),
                'model_requests': len(recorder.requests), 'usage_per_request': recorder.usages,
                'summary_requests': sum(item['purpose'] == 'summary' for item in recorder.requests),
                'elapsed_seconds': round(time.monotonic() - case_started, 3),
                'trace_path': str(trace.path), 'requests_path': str(recorder.path),
                'approvals': broker.decisions, 'result': result,
                'tool_calls': [{'call_id': row['call_id'], 'name': row['name'], 'ok': row['result'].get('ok'),
                               'error': row['result'].get('error', {}).get('code')} for row in rows],
                'semantic_review': {'status': 'pending', 'reviewer': None,
                    'requirements': ['核对标准事实、日期更正和未完事项', '逐项核对引用与原文',
                                     '核对真实文件与审批内容', '检查是否把历史值当作最新值或虚报成功']}}
            report['case_results'].append(entry)
            _save(root / f"{case['id']}-result.json", entry)
            if machine_status == 'FAIL':
                report.update(status='failed', blocking_reason=case['id'] + ':' + ','.join(entry['failed_checks']))
                break
        else:
            report['status'] = 'dry_run_passed' if mode == 'dry-run' else 'machine_checks_passed'
    except Exception as error:
        report.update(status='blocked' if not budget.total else 'failed',
                      blocking_reason=getattr(error, 'code', type(error).__name__))
    finally:
        if store is not None:
            store.close()
        report.update(model_api_calls=budget.real_calls,
            model_api_calls_this_attempt=budget.real_calls - prior_calls,
            model_requests_including_simulation=budget.total,
            requests_by_case=budget.by_case,
            requests_this_attempt_by_case=budget.attempt_by_case,
            elapsed_seconds=round(time.monotonic() - started, 3),
            not_run=[case['id'] for case in FIXED_CASES
                     if case['id'] not in {item['id'] for item in report['case_results']}],
            remaining_model_request_budget=60 - budget.real_calls)
        _save(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real', action='store_true', help='Run the fixed six cases using configured DeepSeek credentials.')
    parser.add_argument('--output-root', type=Path, help='A new directory; existing paths are refused to preserve evidence.')
    parser.add_argument('--prior-report', type=Path,
        help='Previous real report whose calls remain charged to this fixed acceptance group.')
    arguments = parser.parse_args()
    root = arguments.output_root or PROJECT / 'trial/results' / (
        'extensions-acceptance-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    prior_report = arguments.prior_report
    if arguments.real and prior_report is None:
        prior_report = latest_prior_report(PROJECT / 'trial/results')
    prior_usage = load_prior_usage(prior_report) if prior_report else None
    report = run_acceptance(root, mode='real' if arguments.real else 'dry-run', prior_usage=prior_usage,
                            prior_report=prior_report,
                            progress=lambda message: print(message, flush=True))
    print(_json({'status': report['status'], 'report_path': report['report_path'],
                 'model_api_calls': report['model_api_calls'], 'blocking_reason': report.get('blocking_reason')}))
    return 0 if report['status'] in {'dry_run_passed', 'machine_checks_passed'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
