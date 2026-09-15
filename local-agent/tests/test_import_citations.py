from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest

from local_agent.answers import AnswerError
from local_agent.agents import build_file_engine, builtin_agent
from local_agent.imports import ImportStore
from local_agent.session_store import SessionStore
from local_agent.sessions import SessionService


class ImportedCitationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SessionStore.open(self.root / 'state')
        self.addCleanup(self.store.close)
        self.service = SessionService(self.store)

    def policy(self, *, agent='directory-qa'):
        raw = '第一行\n第二行\n'.encode()
        request = {'kind': 'file', 'name': '引用', 'agent_id': agent,
                   'files': [{'logical_path': '资料/原文.txt', 'bytes': len(raw)}],
                   'ignored': []}
        imports = ImportStore(self.store)
        batch = imports.begin(request)
        imports.add_file(batch.id, batch.files[0].slot_id, BytesIO(raw), len(raw))
        published = imports.start_finalize(batch.id).run()
        session = self.service.attach_import(published, request)
        resolved = self.service.resolver.resolve(session)
        engine = build_file_engine(builtin_agent(
            'combined' if agent == 'project-brief' else 'directory'),
            resolved.read_root, None, source_mapper=resolved.source_mapper)
        path = published.chunk_paths[0].relative_to(published.workspace).as_posix()
        read = engine.registry.get('read_file').execute({'path': path})
        engine.policy.accept('read_file', {'path': path}, read)
        return engine.policy, resolved.source_mapper, published, path

    @staticmethod
    def answer(path, *, citation=None):
        return json.dumps({'status': 'answered', 'answer': '两行均有记录。',
            'citations': [citation or {'path': path, 'start_line': 1, 'end_line': 2}]},
            ensure_ascii=False)

    def test_strict_validation_runs_before_location_enrichment_and_merges_text_lines(self):
        policy, _mapper, _published, path = self.policy()
        result = policy.validate(self.answer(path))
        self.assertEqual(result['citations'][0]['quote'], '第一行\n第二行')
        self.assertEqual(result['citations'][0]['source']['locations'], [
            {'kind': 'text_lines', 'start': 1, 'end': 2}])
        supplied = {'path': path, 'start_line': 1, 'end_line': 1,
                    'source': {'kind': 'forged'}}
        with self.assertRaises(AnswerError) as caught:
            policy.validate(self.answer(path, citation=supplied))
        self.assertEqual(caught.exception.code, 'INVALID_CITATION')

    def test_changed_mapping_and_snapshot_hash_are_rejected(self):
        policy, mapper, published, path = self.policy()
        published.locations.write_text('{}')
        with self.assertRaises(AnswerError) as caught:
            policy.validate(self.answer(path))
        self.assertEqual(caught.exception.code, 'INVALID_CITATION')

        policy, mapper, _published, path = self.policy()
        policy.snapshots[path]['sha256'] = '0' * 64
        with self.assertRaises(AnswerError) as caught:
            mapper.enrich(policy.validate_answer(self.answer(path)), policy.snapshots)
        self.assertEqual(caught.exception.code, 'INVALID_CITATION')

        policy, _mapper, published, path = self.policy()
        (published.workspace / path).write_text('替换行\n第二行\n')
        with self.assertRaises(AnswerError) as caught:
            policy.validate(self.answer(path))
        self.assertEqual(caught.exception.code, 'INVALID_CITATION')

        policy, _mapper, published, path = self.policy()
        (published.workspace / path).write_text('替换行\n第二行\n')
        not_found = json.dumps({'status': 'not_found', 'answer': '正文没有该信息。',
                                'citations': []}, ensure_ascii=False)
        with self.assertRaises(AnswerError) as caught:
            policy.validate(not_found)
        self.assertEqual(caught.exception.code, 'INCOMPLETE_SEARCH')

    def test_metadata_parent_replacement_and_fifo_are_rejected_without_blocking(self):
        policy, mapper, published, path = self.policy()
        moved = published.root.with_name(published.root.name + '-moved')
        published.root.rename(moved)
        published.root.symlink_to(moved, target_is_directory=True)
        try:
            self.assertFalse(mapper.search('第一')['ok'])
        finally:
            published.root.unlink()
            moved.rename(published.root)

        locations_backup = published.locations.with_suffix('.backup')
        published.locations.rename(locations_backup)
        os.mkfifo(published.locations, 0o600)
        try:
            import time
            started = time.monotonic()
            with self.assertRaises(AnswerError) as caught:
                policy.validate(self.answer(path))
            self.assertEqual(caught.exception.code, 'INVALID_CITATION')
            self.assertLess(time.monotonic() - started, 1)
        finally:
            published.locations.unlink()
            locations_backup.rename(published.locations)

    def test_combined_policy_enriches_local_paths_only_after_its_validation(self):
        from local_agent.agent_runtime import CapabilityStore, assemble
        policy, mapper, published, path = self.policy(agent='project-brief')
        assembly = assemble(builtin_agent('combined'), published.workspace, None, None,
                            CapabilityStore(self.store.state_dir), run_id='combined',
                            source_mapper=mapper)
        self.addCleanup(assembly.close)
        read = assembly.engine.registry.get('read_file').execute({'path': path})
        assembly.engine.policy.accept('read_file', {'path': path}, read)
        result = assembly.engine.policy.validate(self.answer(path))
        self.assertEqual(result['citations'][0]['source']['logical_path'], '资料/原文.txt')

    def test_pdf_pages_and_docx_units_keep_original_order(self):
        from tests.import_fixtures import write_docx, write_text_pdf

        pdf = self.root / 'two.pdf'
        docx = self.root / 'word.docx'
        write_text_pdf(pdf, ['Page one', 'Budget 42'])
        write_docx(docx)
        files = [('资料/two.pdf', pdf.read_bytes()), ('资料/word.docx', docx.read_bytes())]
        request = {'kind': 'folder', 'name': '混合引用', 'agent_id': 'directory-qa',
                   'files': [{'logical_path': name, 'bytes': len(raw)}
                             for name, raw in files], 'ignored': []}
        imports = ImportStore(self.store)
        batch = imports.begin(request)
        for slot, (_name, raw) in zip(batch.files, files):
            imports.add_file(batch.id, slot.slot_id, BytesIO(raw), len(raw))
        published = imports.start_finalize(batch.id).run()
        session = self.service.attach_import(published, request)
        resolved = self.service.resolver.resolve(session)
        engine = build_file_engine(builtin_agent('directory'), resolved.read_root, None,
                                   source_mapper=resolved.source_mapper)
        by_extension = {record['extension']: record['source_id']
                        for record in published.file_records}
        paths = {}
        for extension, source_id in by_extension.items():
            path = next(path for path in resolved.source_mapper.fact_paths
                        if path.startswith(f'documents/{source_id}/'))
            result = engine.registry.get('read_file').execute({'path': path})
            engine.policy.accept('read_file', {'path': path}, result)
            paths[extension] = (path, result['content'].splitlines())
        answer = json.dumps({'status': 'answered', 'answer': 'PDF 和 Word 均有记录。',
            'citations': [{'path': path, 'start_line': 1, 'end_line': len(lines)}
                          for path, lines in paths.values()]}, ensure_ascii=False)

        result = engine.policy.validate(answer)

        sources = {citation['source']['logical_path']: citation['source']['locations']
                   for citation in result['citations']}
        self.assertEqual(sources['资料/two.pdf'], [
            {'kind': 'pdf_page', 'page': 1}, {'kind': 'pdf_page', 'page': 2}])
        self.assertEqual(sources['资料/word.docx'], [
            {'kind': 'docx_paragraph', 'paragraph': 1},
            {'kind': 'docx_table_row', 'table': 1, 'row': 1},
            {'kind': 'docx_table_row', 'table': 1, 'row': 2},
            {'kind': 'docx_paragraph', 'paragraph': 2},
        ])


if __name__ == '__main__':
    unittest.main()
