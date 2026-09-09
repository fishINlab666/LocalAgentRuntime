"""Run the four existing acceptance scenarios through the keyed local web service."""

from datetime import datetime, timezone
from html.parser import HTMLParser
import itertools
import json
from pathlib import Path
import secrets
import tempfile
import time
import urllib.request
import uuid

from local_agent.evaluation import explicit_absence


class PageToken(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'meta' and attributes.get('name') == 'session-token':
            self.token = attributes.get('content')


def main():
    base = 'http://127.0.0.1:8765'
    workspace = (Path(__file__).resolve().parent / 'workspace').resolve()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    page = PageToken()
    with opener.open(base, timeout=10) as response:
        page.feed(response.read().decode('utf-8'))
    if not page.token:
        raise RuntimeError('Local page session is unavailable')

    def api(path, payload=None):
        headers = {'X-Session-Token': page.token, 'Origin': base}
        body = None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        with opener.open(urllib.request.Request(base + path, data=body, headers=headers), timeout=10) as response:
            return json.load(response)

    config = api('/api/config')
    if (config.get('ready') is not True or config.get('provider', {}).get('simulated') is not False
            or config['provider'].get('provider') != 'deepseek' or config.get('workspace') != str(workspace)):
        raise RuntimeError('Requires the real DeepSeek service using trial/workspace; no trials executed')
    output = Path(__file__).resolve().parent / 'results' / ('baseline-web-' + uuid.uuid4().hex)
    output.mkdir(parents=True, mode=0o700)
    report_path = output / 'report.json'
    report_path.touch(mode=0o600, exist_ok=False)
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'gate': 'NOT_RUN',
              'automatic_checks_passed': False, 'semantic_review': 'pending',
              'provider': config['provider'], 'expected_trials': 12, 'trials': []}
    active = None
    try:
        with tempfile.TemporaryDirectory(prefix='baseline-', dir=workspace) as temporary:
            folder = Path(temporary)
            original, changed = '青禾-' + secrets.token_hex(4), '紫杉-' + secrets.token_hex(4)
            for scenario, repetition in itertools.product(('known', 'not_found', 'missing_file', 'changed'), range(1, 4)):
                code = changed if scenario == 'changed' else original
                facts = [code, '林澄', '2026-09-16']
                source = f'项目代号：{code}\n评审人：林澄\n演示日期：2026-09-16\n'
                (folder / 'note.md').write_text(source, encoding='utf-8')
                target = (folder / ('missing.md' if scenario == 'missing_file' else 'note.md')).relative_to(workspace).as_posix()
                question = ('文件中写的预算是多少？' if scenario == 'not_found'
                            else '文件中项目代号、评审人和演示日期是什么？引用原文。')
                active = api('/api/runs', {'file': target, 'question': question})['id']
                deadline = time.monotonic() + 150
                while True:
                    run = api('/api/runs/' + active)
                    if run.get('result') is not None:
                        active = None
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Local run did not finish within 150 seconds')
                    time.sleep(.25)
                result = run['result']
                answer = result.get('answer') or {}
                expected_status = {'known': 'answered', 'changed': 'answered',
                                   'not_found': 'not_found', 'missing_file': 'unable'}[scenario]
                checks = {'expected_state': result['state'] == ('unable' if scenario == 'missing_file' else 'completed'),
                          'expected_status': answer.get('status') == expected_status,
                          'real_provider': result.get('provider', {}).get('simulated') is False}
                if scenario == 'missing_file':
                    checks['observed_file_not_found'] = any(e['event'] == 'tool.completed'
                        and e['detail'].get('code') == 'FILE_NOT_FOUND' for e in run['events'])
                else:
                    checks['successful_read'] = any(e['event'] == 'tool.completed'
                        and e['detail'].get('ok') is True for e in run['events'])
                if scenario in ('known', 'changed'):
                    checks['key_facts'] = all(fact in answer.get('answer', '') for fact in facts)
                    citations = answer.get('citations', [])
                    lines = source.splitlines()
                    checks['exact_citations'] = bool(citations) and all(
                        c.get('path') == target and type(c.get('start_line')) is int and type(c.get('end_line')) is int
                        and 1 <= c['start_line'] <= c['end_line'] <= len(lines)
                        and c.get('quote') == '\n'.join(lines[c['start_line'] - 1:c['end_line']]) for c in citations)
                elif scenario == 'not_found':
                    checks['explicit_absence'] = explicit_absence(answer.get('answer'), '预算')
                trial = {'scenario': scenario, 'repetition': repetition, 'file': target, 'question': question,
                         'source': None if scenario == 'missing_file' else source,
                         'expected_facts': facts if scenario in ('known', 'changed') else [],
                         'checks': checks, 'automatic_pass': all(checks.values()), 'run': run}
                report['trials'].append(trial)
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(f'{scenario} {repetition}: {result["state"]}, checks={json.dumps(checks)}', flush=True)
        passed = len(report['trials']) == 12 and all(t['automatic_pass'] for t in report['trials'])
        report['automatic_checks_passed'] = passed
        report['gate'] = 'PENDING_SEMANTIC_REVIEW' if passed else 'FAILED'
        return 0 if passed else 1
    except Exception as error:
        report['gate'] = 'FAILED' if report['trials'] else 'NOT_RUN'
        report['error_type'] = type(error).__name__
        if active is not None:
            try:
                api('/api/runs/' + active + '/cancel', {})
            except Exception:
                pass
        return 1
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Report:', report_path, flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
