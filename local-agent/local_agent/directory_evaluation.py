"""Cross-file acceptance with auditable discovery and context round trips."""

import copy
from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import secrets
import tempfile
import threading
import uuid

from .discovery import DirectoryTools
from .evaluation import contains_fact, explicit_absence
from .provider import ProviderError
from .runtime import Runtime
from .trace import Trace


def context_checks(events: list[dict]) -> dict:
    """Compare accepted tool results to the very next model request, including IDs."""
    pending, first_call, round_trip = [], None, True
    for event in events:
        data = event['data']
        if event['event'] == 'tool.requested' and first_call is None:
            first_call = data
        elif event['event'] == 'tool.completed':
            pending.append(data)
        elif event['event'] == 'model.requested':
            messages = data['messages']
            declarations = {call['id']: call for message in messages if message['role'] == 'assistant'
                            for call in message.get('tool_calls') or []}
            returned = {message['tool_call_id']: json.loads(message['content'])
                        for message in messages if message['role'] == 'tool'}
            for completed in pending:
                call_id, raw = completed['id'], completed['result']
                numbered = copy.deepcopy(raw)
                if raw.get('ok') and isinstance(raw.get('content'), str):
                    numbered['content'] = {str(i): line for i, line in enumerate(raw['content'].splitlines(), 1)}
                round_trip &= call_id in declarations and returned.get(call_id) in (raw, numbered)
            pending = []
    try:
        first_is_list = (first_call is not None and first_call['name'] == 'list_files'
                         and json.loads(first_call['arguments']) == {'path': '.'})
    except (ValueError, TypeError, RecursionError):
        first_is_list = False
    return {'first_tool_is_root_listing': first_is_list,
            'tool_results_in_next_context': round_trip and not pending and first_call is not None}


def evaluate_directory(provider_factory, directory: Path, repeats: int = 3,
                       cancel: threading.Event | None = None) -> dict:
    if type(repeats) is not int or repeats != 3:
        raise ValueError('Acceptance requires exactly three repetitions per scenario')
    cancel = cancel or threading.Event()
    output = Path(directory).resolve() / ('directory-evaluation-' + uuid.uuid4().hex)
    output.mkdir(parents=True, mode=0o700)
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'gate': 'NOT_RUN',
              'automatic_checks_passed': False, 'human_review': 'pending', 'trials': [],
              'expected_trials': 12, 'report_path': str(output / 'report.json')}

    def save():
        with Path(report['report_path']).open('x', encoding='utf-8') as file:
            json.dump(report, file, ensure_ascii=False, indent=2)
        return report

    try:
        provider = provider_factory()
    except ProviderError as error:
        report['error'] = error.code
        return save()
    report['provider'] = provider.metadata
    with tempfile.TemporaryDirectory(prefix='local-agent-directory-eval-') as tmp:
        workspace = Path(tmp)
        known_inputs = {}
        for scenario, repetition in itertools.product(('known', 'changed', 'not_found', 'read_error'), range(repeats)):
            if cancel.is_set():
                break
            # Changed trials reuse an earlier path so stale cross-run caches are detectable.
            paths = ['project-' + secrets.token_hex(4) + '.md', 'review-' + secrets.token_hex(4) + '.txt']
            base = '青禾-' + secrets.token_hex(4)
            changed = '紫杉-' + secrets.token_hex(4)
            reviewer = '林澄-' + secrets.token_hex(2)
            if scenario == 'known':
                known_inputs[repetition] = (paths, base, reviewer)
            elif scenario == 'changed':
                paths, base, reviewer = known_inputs[repetition]
            facts = [changed if scenario == 'changed' else base, reviewer, '2026-10-11']
            (workspace / paths[0]).write_text(f'项目代号：{base}\n本页仅记录项目资料。\n', encoding='utf-8')
            (workspace / paths[1]).write_text(f'评审人：{reviewer}\n演示日期：2026-10-11\n', encoding='utf-8')
            if scenario == 'changed':
                (workspace / paths[0]).write_text(f'项目代号：{changed}\n本页是更新后的项目资料。\n', encoding='utf-8')
            elif scenario == 'read_error':
                (workspace / paths[1]).write_bytes(b'\xff\xfeinvalid-utf8')
            question = ('这些资料说明的预算是多少？请核对全部可读取资料。' if scenario == 'not_found'
                        else '请结合项目资料与评审资料，回答项目代号、评审人和演示日期，分别引用依据。')
            trace = Trace(output / 'traces', workspace, debug_content=True)
            result = Runtime(provider, DirectoryTools(workspace), trace).run(question, None, cancel)
            events = [json.loads(line) for line in trace.path.read_text().splitlines()]
            requested = [e['data'] for e in events if e['event'] == 'model.requested']
            answer = result.get('answer') or {}
            expected = {'known': 'answered', 'changed': 'answered', 'not_found': 'not_found', 'read_error': 'unable'}[scenario]
            checks = {'expected_state': result['state'] == ('unable' if scenario == 'read_error' else 'completed'),
                      'expected_status': answer.get('status') == expected, **context_checks(events)}
            initial = json.dumps(requested[0]['messages'], ensure_ascii=False) if requested else ''
            checks['no_preloaded_filenames_or_facts'] = bool(requested) and all(value not in initial for value in paths + facts)
            read_paths = set()
            for event in events:
                if event['event'] == 'tool.requested' and event['data']['name'] == 'read_file':
                    try:
                        path = json.loads(event['data']['arguments']).get('path')
                        if isinstance(path, str):
                            read_paths.add(path)
                    except (ValueError, AttributeError, RecursionError):
                        pass
            if scenario != 'read_error':
                checks['both_reads_attempted'] = set(paths).issubset(read_paths)
                checks['complete_scope'] = result.get('scope', {}).get('complete') is True
            if scenario in ('known', 'changed'):
                checks['key_facts'] = all(contains_fact(answer.get('answer'), fact) for fact in facts)
                checks['both_files_cited'] = set(paths).issubset({c['path'] for c in answer.get('citations', [])})
                if scenario == 'changed':
                    checks['old_fact_absent'] = base not in answer.get('answer', '')
            elif scenario == 'not_found':
                checks['explicit_absence'] = explicit_absence(answer.get('answer'), '预算')
            else:
                checks['failed_file_attempted'] = paths[1] in read_paths
                checks['observed_read_error'] = any(e['event'] == 'tool.completed' and
                    e['data']['result'].get('error', {}).get('code') == 'UNSUPPORTED_FILE' for e in events)
                checks['incomplete_scope'] = result.get('scope', {}).get('complete') is False
            report['trials'].append({'scenario': scenario, 'repetition': repetition + 1,
                'question': question, 'files': paths, 'attempted_reads': sorted(read_paths), 'expected_status': expected,
                'expected_facts': facts if scenario in ('known', 'changed') else [],
                'checks': checks, 'automatic_pass': all(checks.values()), 'result': result})
            for path in paths:
                (workspace / path).unlink()
    passed = len(report['trials']) == 12 and all(t['automatic_pass'] for t in report['trials'])
    report['automatic_checks_passed'] = passed
    report['gate'] = ('FAILED' if not passed else 'SIMULATED_ONLY' if provider.metadata.get('simulated', True)
                      else 'PENDING_SEMANTIC_REVIEW')
    return save()
