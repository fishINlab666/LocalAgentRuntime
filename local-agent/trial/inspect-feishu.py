"""Read recorded channel evidence; never sends, reruns, or declares acceptance."""
import argparse
import json
from pathlib import Path
import sqlite3


def inspect(state_dir):
    database = Path(state_dir).resolve() / 'sessions.sqlite3'
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        result = []
        for event in connection.execute('SELECT * FROM channel_events ORDER BY received_at'):
            run = (connection.execute('SELECT * FROM runs WHERE id=?', (event['run_id'],)).fetchone()
                   if event['run_id'] else None)
            item = {key: event[key] for key in ('id', 'event_id', 'message_id', 'session_id', 'run_id', 'state')}
            if run:
                provider = json.loads(run['result_json'] or '{}').get('provider', {})
                requests = [json.loads(row[0])['request'] for row in connection.execute(
                    'SELECT payload_json FROM context_manifests WHERE run_id=? ORDER BY request_seq', (run['id'],))]
                feedback = []
                for call in connection.execute('SELECT * FROM tool_calls WHERE run_id=?', (run['id'],)):
                    message = connection.execute('SELECT payload_json FROM messages WHERE id=?',
                                                 (call['result_message_id'],)).fetchone()
                    payload = json.loads(message[0]) if message else None
                    expected = payload.get('result') if payload else None
                    numbered = json.loads(json.dumps(expected))
                    data = numbered.get('data', numbered) if isinstance(numbered, dict) else {}
                    if numbered and numbered.get('ok') and isinstance(data.get('content'), str):
                        data['content'] = {str(n): line for n, line in enumerate(data['content'].splitlines(), 1)}
                    fed = bool(payload and any(any(m.get('role') == 'tool'
                               and m.get('tool_call_id') == call['call_id']
                               and json.loads(m.get('content', '{}')) in (expected, numbered)
                               for m in request['messages']) for request in requests))
                    feedback.append({'call_id': call['call_id'], 'name': call['name'],
                                     'stage': call['stage'], 'returned_to_model': fed})
                item.update(run_state=run['state'], stop_reason=run['stop_reason'],
                            provider=provider, model_requests=len(requests),
                            trace_path=run['trace_path'], tool_feedback=feedback,
                            registered_artifacts=[dict(row) for row in connection.execute(
                                'SELECT id,path,bytes,sha256,recovery_state FROM artifacts WHERE run_id=?', (run['id'],))])
            item['deliveries'] = [dict(row) for row in connection.execute(
                'SELECT kind,state,attempts,message_id,error_code FROM channel_outbox WHERE event_id=?', (event['id'],))]
            result.append(item)
        return {'acceptance': 'not_evaluated', 'note': '仅导出已记录证据；离线替身、缺失和失败不能冒充真实验收。',
                'events': result}
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/share/local-agent')
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(args.state_dir), ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, ValueError):
        parser.exit(2, '无法读取渠道证据：请核对 --state-dir，并先启动一次当前版本。\n')
