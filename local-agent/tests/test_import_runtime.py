import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from local_agent.import_tools import SearchDocumentsAdapter
from local_agent.imports import ImportStore
from local_agent.provider import ModelReply
from local_agent.runtime import RunConfig
from local_agent.session_store import SessionStore
from local_agent.sessions import RunSubmission, SessionScope, SessionService
from local_agent.tool_runtime import valid_arguments, wire_result
from local_agent.trace import Trace


def tool_call(call_id, name, arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function', 'function': {
            'name': name,
            'arguments': json.dumps({**arguments, 'intent': '定位并核对导入原文'}, ensure_ascii=False),
        }}]}


def tool_calls(items):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        tool_call(call_id, name, arguments)['tool_calls'][0]
        for call_id, name, arguments in items
    ]}


def final(path, *, status='answered', lines=(1, 1), answer='预算已记录。'):
    return {'role': 'assistant', 'content': json.dumps({
        'status': status,
        'answer': answer,
        'citations': ([{'path': path, 'start_line': lines[0], 'end_line': lines[1]}]
                      if status == 'answered' else []),
    }, ensure_ascii=False)}


class FullRequestProvider:
    metadata = {'provider': 'scripted-test', 'model': 'none', 'simulated': True}

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def complete(self, messages, tools, timeout):
        self.requests.append({'messages': copy.deepcopy(messages),
                              'tools': copy.deepcopy(tools)})
        return ModelReply(next(self.replies))


class ImportedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SessionStore.open(self.root / 'state')
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)

    def publish(self, files, *, agent_id='directory-qa'):
        request = {'kind': 'folder' if len(files) > 1 else 'file',
                   'name': '导入资料', 'agent_id': agent_id,
                   'files': [{'logical_path': name, 'bytes': len(raw)} for name, raw in files],
                   'ignored': []}
        imports = ImportStore(self.store)
        batch = imports.begin(request)
        for slot, (_name, raw) in zip(batch.files, files):
            imports.add_file(batch.id, slot.slot_id, BytesIO(raw), len(raw))
        published = imports.start_finalize(batch.id).run()
        return published, self.service.attach_import(published, request)

    def execute(self, session, provider, *, request_id='request'):
        prepared = self.service.submit(session.id, RunSubmission(
            request_id, '查找预算并引用原文。', 'files', session.scope, None, None, {}))
        trace = Trace(self.root / 'runs', Path(session.workspace_path), run_id=prepared.run_id)
        return self.service.execute(prepared, provider, trace)

    def test_search_round_trip_is_catalog_then_manifest_read_becomes_fact(self):
        published, session = self.publish([('项目/预算.txt', '说明\n预算：42 万元\n'.encode())])
        chunk = published.chunk_paths[0].relative_to(published.workspace).as_posix()
        provider = FullRequestProvider([
            tool_call('s1', 'search_documents', {'query': '预算'}),
            tool_call('r1', 'read_file', {'path': chunk}),
            final(chunk, lines=(2, 2)),
        ])

        result = self.execute(session, provider)

        self.assertEqual(result['state'], 'completed', result)
        search_message = provider.requests[1]['messages'][-1]
        self.assertEqual(search_message['tool_call_id'], 's1')
        search = json.loads(search_message['content'])
        self.assertEqual(search['data']['evidence_role'], 'catalog')
        self.assertEqual(search['data']['matches'][0]['path'], chunk)
        self.assertEqual(result['scope']['discovered_files'], [chunk])
        self.assertEqual(result['scope']['read_files'], [chunk])
        self.assertNotIn('index.md', result['scope']['discovered_files'])
        citation = result['answer']['citations'][0]
        self.assertEqual(citation['quote'], '预算：42 万元')
        self.assertEqual(citation['source'], {
            'kind': 'imported_document', 'name': '预算.txt', 'logical_path': '项目/预算.txt',
            'locations': [{'kind': 'text_lines', 'start': 2, 'end': 2}],
        })

    def test_catalog_is_not_citable_and_does_not_complete_fact_scope(self):
        published, session = self.publish([('notes.txt', b'alpha\n')])
        provider = FullRequestProvider([
            tool_call('catalog', 'read_file', {'path': 'index.md'}),
            final('index.md'),
        ])
        rejected = self.execute(session, provider, request_id='catalog-citation')
        self.assertEqual(rejected['stop_reason'], 'INVALID_CITATION')
        self.assertEqual(rejected['scope']['read_files'], [])
        self.assertFalse(rejected['scope']['complete'])

        chunk = published.chunk_paths[0].relative_to(published.workspace).as_posix()
        provider = FullRequestProvider([
            tool_call('catalog', 'read_file', {'path': 'index.md'}),
            tool_call('read', 'read_file', {'path': chunk}),
            final(chunk, status='not_found', answer='全部正文块都没有所问预算。'),
        ])
        accepted = self.execute(session, provider, request_id='complete-not-found')
        self.assertEqual(accepted['state'], 'completed', accepted)
        self.assertTrue(accepted['scope']['complete'])

    def test_search_contract_is_bounded_stable_and_cannot_change_scope(self):
        lines = [('预算 %02d ' % number + '界' * 500) for number in range(30)]
        published, session = self.publish([
            ('zeta.txt', ('\n'.join('Z ' + line for line in lines) + '\n').encode()),
            ('alpha.txt', ('\n'.join('A ' + line for line in lines) + '\n').encode()),
        ])
        mapper = self.service.resolver.resolve(session).source_mapper
        adapter = SearchDocumentsAdapter(mapper)
        result = adapter.execute({'query': '预算'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(adapter.spec.timeout_seconds, 1)
        self.assertEqual(len(result['matches']), 20)
        self.assertTrue(all(len(item['excerpt'].encode()) <= 384 for item in result['matches']))
        self.assertTrue(all(item['excerpt'].startswith('A ') for item in result['matches']))
        wire = wire_result(result)
        self.assertLessEqual(len(json.dumps(wire, ensure_ascii=False,
                                             allow_nan=False).encode()), 12 * 1024)
        self.assertFalse(valid_arguments(adapter.spec, {
            'query': '预算', 'import_id': published.import_id, 'intent': '越权'}))
        self.assertEqual(adapter.execute({'query': 'x' * 257})['error']['code'],
                         'INVALID_ARGUMENT')
        self.assertEqual(adapter.execute({'query': '界' * 86})['error']['code'],
                         'INVALID_ARGUMENT')
        self.assertTrue(adapter.execute({'query': '界' * 85 + 'a'})['ok'])
        self.assertEqual(mapper.fact_paths,
                         self.service.resolver.resolve(session).source_mapper.fact_paths)

        other, _ = self.publish([('other.txt', b'private\n')])
        other_path = other.chunk_paths[0].relative_to(other.workspace).as_posix()
        from local_agent.agents import build_file_engine, builtin_agent
        engine = build_file_engine(builtin_agent('directory'), mapper.workspace, None,
                                   source_mapper=mapper)
        denied = engine.registry.get('read_file').execute({'path': other_path})
        self.assertEqual(denied['error']['code'], 'PATH_DENIED')

    def test_mapper_accepts_valid_locations_larger_than_eight_mib(self):
        raw = b'\n' * 32768
        published, session = self.publish([
            (f'empty-lines-{number}.txt', raw) for number in range(6)
        ])
        self.assertGreater(published.locations.stat().st_size, 8 * 1024 * 1024)
        resolved = self.service.resolver.resolve(session)
        self.assertIsNotNone(resolved.source_mapper)
        self.assertEqual(len(resolved.source_mapper.fact_paths), len(published.chunk_paths))

    def test_complete_provider_request_keeps_two_escaped_chunks_under_64_kib(self):
        def line(number):
            prefix = f'预算{number:02d} '
            body = ('\\"' * 590)
            return (prefix + body)[:1198] + '\n'
        raw = ''.join(line(number) for number in range(20)).encode()
        published, session = self.publish([('escaped.txt', raw)])
        chunks = [path.relative_to(published.workspace).as_posix()
                  for path in published.chunk_paths]
        self.assertEqual(len(chunks), 2)
        provider = FullRequestProvider([
            tool_call('search', 'search_documents', {'query': '预算'}),
            tool_calls([('read-1', 'read_file', {'path': chunks[0]}),
                        ('read-2', 'read_file', {'path': chunks[1]})]),
            final(chunks[0], answer='预算条目已记录。'),
        ])
        result = self.execute(session, provider, request_id='budget')
        self.assertEqual(result['state'], 'completed', result)
        self.assertIn('excerpt', provider.requests[1]['messages'][-1]['content'])
        final_request = provider.requests[2]
        encoded = json.dumps(final_request, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(',', ':')).encode()
        self.assertLessEqual(len(encoded), RunConfig().max_input_bytes)
        projected_search = next(message for message in final_request['messages']
                                if message.get('tool_call_id') == 'search')
        self.assertNotIn('excerpt', projected_search['content'])
        for call_id, path in [('read-1', chunks[0]), ('read-2', chunks[1])]:
            message = next(item for item in final_request['messages']
                           if item.get('tool_call_id') == call_id)
            content = (published.workspace / path).read_text()
            self.assertTrue(message['content'].endswith('\n\n' + content))


if __name__ == '__main__':
    unittest.main()
