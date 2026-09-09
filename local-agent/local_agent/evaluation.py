"""Fixed acceptance scenarios. Automatic checks never imply human approval."""

from datetime import date, datetime, timezone
import itertools
import json
from pathlib import Path
import re
import secrets
import tempfile
import threading
import uuid

from .files import ReadFile
from .provider import ProviderError
from .runtime import Runtime, RunConfig
from .trace import Trace


def contains_fact(answer: object, expected: str) -> bool:
    if not isinstance(answer, str):
        return False
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', expected):
        return expected in answer
    try:
        expected_date = date.fromisoformat(expected)
    except ValueError:
        return False
    for match in re.finditer(
            r'(?<!\d)(\d{4})(?:-(\d{1,2})-(\d{1,2})(?!\d)|年(\d{1,2})月(\d{1,2})日)', answer):
        year, iso_month, iso_day, chinese_month, chinese_day = match.groups()
        try:
            observed = date(int(year), int(iso_month or chinese_month), int(iso_day or chinese_day))
        except ValueError:
            continue
        if observed == expected_date:
            return True
    return False


def explicit_absence(answer: object, subject: str) -> bool:
    if not isinstance(answer, str) or not isinstance(subject, str) or not subject:
        return False
    compact = ''.join(answer.split())
    markers = ('未说明', '没有说明', '未提及', '没有提及',
               '未记载', '没有记载', '未记录', '没有记录', '未找到')
    return any(marker + qualifier + subject in compact or subject + marker in compact
               for marker in markers for qualifier in ('', '任何', '所问的'))


def evaluate(provider_factory, directory: Path, repeats: int = 3,
             cancel: threading.Event | None = None) -> dict:
    if type(repeats) is not int or repeats != 3:
        raise ValueError('Acceptance requires exactly three repetitions per scenario')
    cancel = cancel or threading.Event()
    output = Path(directory).resolve() / ('evaluation-' + uuid.uuid4().hex)
    output.mkdir(parents=True, mode=0o700)
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'gate': 'NOT_RUN',
              'automatic_checks_passed': False, 'human_review': 'pending', 'trials': [],
              'expected_trials': repeats * 4, 'report_path': str(output / 'report.json')}

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
    with tempfile.TemporaryDirectory(prefix='local-agent-evaluation-') as tmp:
        workspace = Path(tmp)
        base = '青禾-' + secrets.token_hex(4)
        changed = '紫杉-' + secrets.token_hex(4)
        for scenario, repetition in itertools.product(('known', 'not_found', 'missing_file', 'changed'), range(repeats)):
            if cancel.is_set():
                break
            code = changed if scenario == 'changed' else base
            facts = [code, '林澄', '2026-09-16']
            (workspace / 'note.md').write_text(
                f'项目代号：{code}\n评审人：林澄\n演示日期：2026-09-16\n', encoding='utf-8')
            target = 'missing.md' if scenario == 'missing_file' else 'note.md'
            question = '文件中写的预算是多少？' if scenario == 'not_found' else '文件中项目代号、评审人和演示日期是什么？引用原文。'
            trace = Trace(output / 'traces', workspace, debug_content=True)
            result = Runtime(provider, ReadFile(workspace, {target}), trace, RunConfig()).run(question, target, cancel)
            answer = result.get('answer') or {}
            expected_status = {'known': 'answered', 'changed': 'answered',
                               'not_found': 'not_found', 'missing_file': 'unable'}[scenario]
            checks = {'expected_state': result['state'] == ('unable' if scenario == 'missing_file' else 'completed'),
                      'expected_status': answer.get('status') == expected_status}
            if scenario in ('known', 'changed'):
                checks['key_facts'] = all(contains_fact(answer.get('answer'), fact) for fact in facts)
            elif scenario == 'not_found':
                checks['explicit_absence'] = explicit_absence(answer.get('answer'), '预算')
            else:
                events = [json.loads(line) for line in trace.path.read_text().splitlines()]
                checks['observed_file_not_found'] = any(e['event'] == 'tool.completed' and
                    e['data']['result'].get('error', {}).get('code') == 'FILE_NOT_FOUND' for e in events)
            report['trials'].append({'scenario': scenario, 'repetition': repetition + 1,
                'question': question, 'expected_status': expected_status,
                'expected_facts': facts if scenario in ('known', 'changed') else [],
                'checks': checks, 'automatic_pass': all(checks.values()), 'result': result})
    passed = len(report['trials']) == report['expected_trials'] and all(t['automatic_pass'] for t in report['trials'])
    report['automatic_checks_passed'] = passed
    report['gate'] = ('FAILED' if not passed else 'SIMULATED_ONLY' if provider.metadata.get('simulated', True)
                      else 'PENDING_HUMAN_REVIEW')
    return save()
