"""Manifest-bound search, fact coverage, and source mapping for imported documents."""

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat

from .answers import AnswerError, validate_answer
from .file_tools import FilePolicy
from .files import ReadFile, _open_directory, _path_parts
from .tool_runtime import ToolSpec, tool_error, wire_result


SEARCH_RESULT_BYTES = 12 * 1024
SEARCH_EXCERPT_BYTES = 384
SEARCH_MATCHES = 20


class ImportedDocumentError(ValueError):
    pass


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')


def _wire_size(value):
    return len(json.dumps(wire_result(value), ensure_ascii=False,
                          allow_nan=False).encode('utf-8'))


def _read_regular_at(directory, name, maximum):
    flags = (os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
             | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        descriptor = os.open(name, flags, dir_fd=directory)
    except OSError as error:
        raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR') from error
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) & 0o077 or before.st_size > maximum):
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        chunks, remaining = [], maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        raw = b''.join(chunks)
        if (before.st_dev, before.st_ino, before.st_mode, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_mode, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns) or len(raw) != after.st_size:
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        return raw
    except OSError as error:
        raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR') from error
    finally:
        os.close(descriptor)


def _valid_location(value, kind):
    fields = {
        'text_lines': {'kind', 'start', 'end'},
        'pdf_page': {'kind', 'page'},
        'docx_paragraph': {'kind', 'paragraph'},
        'docx_table_row': {'kind', 'table', 'row'},
    }
    allowed = {'txt': {'text_lines'}, 'md': {'text_lines'}, 'pdf': {'pdf_page'},
               'docx': {'docx_paragraph', 'docx_table_row'}}
    if (not isinstance(value, dict) or value.get('kind') not in allowed.get(kind, set())
            or set(value) != fields[value['kind']]
            or any(type(item) is not int or item < 1
                   for name, item in value.items() if name != 'kind')):
        raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
    return copy.deepcopy(value)


def _safe_excerpt(line, query):
    if len(line.encode('utf-8')) <= SEARCH_EXCERPT_BYTES:
        return line
    folded, wanted = line.casefold(), query.casefold()
    position = folded.find(wanted)
    if position < 0:
        position = 0
    start_character = 0
    folded_count = 0
    for index, character in enumerate(line):
        next_count = folded_count + len(character.casefold())
        if next_count > position:
            start_character = index
            break
        folded_count = next_count
    raw = line.encode('utf-8')
    byte_start = len(line[:start_character].encode('utf-8'))
    start = max(0, byte_start - 64)
    while start < len(raw):
        try:
            excerpt = raw[start:start + SEARCH_EXCERPT_BYTES].decode('utf-8')
            return excerpt
        except UnicodeDecodeError as error:
            if error.start == 0:
                start += 1
            else:
                return raw[start:start + error.start].decode('utf-8')
    return ''


def _merge_locations(locations):
    merged = []
    for location in locations:
        current = copy.deepcopy(location)
        if merged and current == merged[-1]:
            continue
        if (merged and current.get('kind') == 'text_lines'
                and merged[-1].get('kind') == 'text_lines'
                and current['start'] <= merged[-1]['end'] + 1):
            merged[-1]['end'] = max(merged[-1]['end'], current['end'])
            continue
        merged.append(current)
    return merged


class ImportedSourceMapper:
    """Frozen facts from ImportStore.open, with current-file integrity checks."""

    def __init__(self, imported):
        hashes = imported.manifest_hashes
        records = imported.source_records
        if (not isinstance(imported.manifest_sha256, str) or len(imported.manifest_sha256) != 64
                or hashes is None or not records):
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        self.import_id = imported.import_id
        self.workspace = Path(imported.workspace)
        self.workspace_identity = (imported.workspace_device, imported.workspace_inode)
        self.root = Path(imported.root)
        self.root_identity = (imported.root_device, imported.root_inode)
        self.locations_bytes = imported.locations_bytes
        self._manifest_sha256 = imported.manifest_sha256
        self._locations_sha256 = hashes.get('locations.json')
        self._index_sha256 = hashes.get('workspace/index.md')
        self._facts = {}
        for record in records:
            try:
                source = json.loads(record['parser_json'])
                if (source['source_id'] != record['source_id']
                        or source['logical_path'] != record['logical_path']
                        or source['format'] != record['extension'][1:]):
                    raise ValueError
                for ordinal, chunk in enumerate(source['chunks'], 1):
                    path = chunk['path']
                    expected = hashes['workspace/' + path]
                    if (_path_parts(path) is None or Path(path).suffix != '.md'
                            or type(chunk['bytes']) is not int or chunk['bytes'] < 1
                            or type(chunk['line_count']) is not int or chunk['line_count'] < 1
                            or not isinstance(expected, str) or len(expected) != 64
                            or path in self._facts):
                        raise ValueError
                    self._facts[path] = {
                        'path': path, 'logical_path': source['logical_path'],
                        'name': PurePosixPath(source['logical_path']).name,
                        'format': source['format'], 'ordinal': ordinal,
                        'bytes': chunk['bytes'], 'line_count': chunk['line_count'],
                        'sha256': expected,
                    }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR') from None
        self.fact_paths = tuple(sorted(self._facts,
            key=lambda path: (self._facts[path]['logical_path'],
                              self._facts[path]['ordinal'], path)))
        if (not isinstance(self._locations_sha256, str)
                or not isinstance(self._index_sha256, str)
                or any(type(value) is not int or value < 0 for value in self.root_identity)
                or type(self.locations_bytes) is not int or self.locations_bytes < 2):
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        self._check_workspace()
        self._check_manifest()

    def _check_workspace(self):
        try:
            descriptor = _open_directory(self.workspace, [], self.workspace_identity)
        except OSError as error:
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR') from error
        try:
            details = os.fstat(descriptor)
            if (not stat.S_ISDIR(details.st_mode)
                    or stat.S_IMODE(details.st_mode) & 0o077):
                raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        finally:
            os.close(descriptor)

    def _open_root(self):
        descriptor = None
        try:
            descriptor = _open_directory(self.root, [], self.root_identity)
            details = os.fstat(descriptor)
            if (not stat.S_ISDIR(details.st_mode)
                    or stat.S_IMODE(details.st_mode) & 0o077):
                raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
            return descriptor
        except (OSError, ImportedDocumentError) as error:
            if descriptor is not None:
                os.close(descriptor)
            if isinstance(error, ImportedDocumentError):
                raise
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR') from error

    def _check_manifest(self):
        descriptor = self._open_root()
        try:
            raw = _read_regular_at(descriptor, 'manifest.json', 8 * 1024 * 1024)
            if hashlib.sha256(raw).hexdigest() != self._manifest_sha256:
                raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        finally:
            os.close(descriptor)

    def _load_locations(self):
        self._check_workspace()
        self._check_manifest()
        descriptor = self._open_root()
        try:
            raw = _read_regular_at(descriptor, 'locations.json', self.locations_bytes)
            if (len(raw) != self.locations_bytes
                    or hashlib.sha256(raw).hexdigest() != self._locations_sha256):
                raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        finally:
            os.close(descriptor)
        try:
            locations = json.loads(raw)
        except (UnicodeError, ValueError, RecursionError):
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR') from None
        if not isinstance(locations, dict) or set(locations) != set(self.fact_paths):
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        checked = {}
        for path in self.fact_paths:
            fact, mapping = self._facts[path], locations[path]
            keys = {str(number) for number in range(1, fact['line_count'] + 1)}
            if not isinstance(mapping, dict) or set(mapping) != keys:
                raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
            checked[path] = {}
            for number in range(1, fact['line_count'] + 1):
                values = mapping[str(number)]
                if not isinstance(values, list) or not values:
                    raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
                checked[path][str(number)] = [
                    _valid_location(value, fact['format']) for value in values]
        return checked

    def expected_hash(self, path):
        fact = self._facts.get(path)
        return fact['sha256'] if fact else self._index_sha256 if path == 'index.md' else None

    def verify_snapshot(self, path, result):
        expected = self.expected_hash(path)
        if (expected is None or not isinstance(result, dict) or result.get('ok') is not True
                or result.get('path') != path or result.get('sha256') != expected
                or not isinstance(result.get('content'), str)
                or hashlib.sha256(result['content'].encode('utf-8')).hexdigest() != expected):
            return False
        if path in self._facts:
            fact = self._facts[path]
            return (result.get('bytes') == fact['bytes']
                    and result.get('line_count') == fact['line_count'])
        return True

    def snapshot_is_current(self, path, snapshot):
        if path not in self._facts or not self.verify_snapshot(path, snapshot):
            return False
        try:
            reader = ReadFile(self.workspace, {path}, max_bytes=12 * 1024)
        except (OSError, RuntimeError):
            return False
        if reader.workspace_identity != self.workspace_identity:
            return False
        current = reader.execute({'path': path})
        return current == snapshot and self.verify_snapshot(path, current)

    def search(self, query):
        try:
            locations = self._load_locations()
            reader = ReadFile(self.workspace, set(self.fact_paths), max_bytes=12 * 1024)
            if reader.workspace_identity != self.workspace_identity:
                raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
            matches = []
            wanted = query.casefold()
            for path in self.fact_paths:
                result = reader.execute({'path': path})
                if not result.get('ok'):
                    return result
                if not self.verify_snapshot(path, result):
                    return tool_error('FILE_CHANGED')
                for number, line in enumerate(result['content'].splitlines(), 1):
                    if wanted not in line.casefold():
                        continue
                    match = {'path': path, 'start_line': number, 'end_line': number,
                             'excerpt': _safe_excerpt(line, query),
                             'locations': copy.deepcopy(locations[path][str(number)])}
                    candidate = {'ok': True, 'query': query, 'evidence_role': 'catalog',
                                 'matches': [*matches, match]}
                    if _wire_size(candidate) > SEARCH_RESULT_BYTES:
                        return {'ok': True, 'query': query, 'evidence_role': 'catalog',
                                'matches': matches}
                    matches.append(match)
                    if len(matches) == SEARCH_MATCHES:
                        return {'ok': True, 'query': query, 'evidence_role': 'catalog',
                                'matches': matches}
            return {'ok': True, 'query': query, 'evidence_role': 'catalog', 'matches': matches}
        except ImportedDocumentError:
            return tool_error('FILE_CHANGED')
        except OSError:
            return tool_error('READ_ERROR')

    def enrich(self, validated_answer, snapshots):
        try:
            locations = self._load_locations()
            value = copy.deepcopy(validated_answer)
            for citation in value.get('citations', []):
                if 'path' not in citation:
                    continue
                path = citation['path']
                fact = self._facts.get(path)
                snapshot = snapshots.get(path)
                if fact is None or not self.snapshot_is_current(path, snapshot):
                    raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
                covered = []
                for number in range(citation['start_line'], citation['end_line'] + 1):
                    covered.extend(locations[path].get(str(number), []))
                if not covered:
                    raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
                citation['source'] = {
                    'kind': 'imported_document', 'name': fact['name'],
                    'logical_path': fact['logical_path'],
                    'locations': _merge_locations(covered),
                }
            return value
        except (ImportedDocumentError, KeyError, TypeError, ValueError):
            raise AnswerError('INVALID_CITATION', 'Imported citation mapping is invalid.',
                              repairable=True) from None


class SearchDocumentsAdapter:
    def __init__(self, mapper):
        self.mapper = mapper
        self.spec = ToolSpec(
            'search_documents',
            '在当前导入资料的正文块中定位关键词。结果只是目录线索；必须再用 read_file 读取正文后才能回答和引用。',
            {'type': 'object', 'properties': {
                'query': {'type': 'string', 'minLength': 1, 'maxLength': 256}},
             'required': ['query'], 'additionalProperties': False},
            timeout_seconds=1, max_result_bytes=SEARCH_RESULT_BYTES,
            error_codes=frozenset({'INVALID_ARGUMENT', 'FILE_CHANGED', 'READ_ERROR',
                                   'OS_PERMISSION_DENIED', 'WORKSPACE_CHANGED'}),
        )
        self._proven_result = None

    def result_fields(self):
        return {}

    def execute(self, arguments):
        self._proven_result = None
        query = arguments.get('query') if isinstance(arguments, dict) else None
        try:
            size = len(query.encode('utf-8')) if isinstance(query, str) else 0
        except UnicodeError:
            size = 0
        if (not isinstance(arguments, dict) or set(arguments) != {'query'}
                or not isinstance(query, str) or not query.strip() or not 1 <= size <= 256):
            return tool_error('INVALID_ARGUMENT')
        result = self.mapper.search(query)
        if result.get('ok') and _wire_size(result) <= self.spec.max_result_bytes:
            self._proven_result = copy.deepcopy(result)
        return result

    def verify_success(self, arguments, data):
        expected = {'ok': True, **copy.deepcopy(data)}
        proof, self._proven_result = self._proven_result, None
        if proof != expected or set(data) != {'query', 'evidence_role', 'matches'}:
            raise ValueError('unproven search result')
        if data['query'] != arguments.get('query') or data['evidence_role'] != 'catalog':
            raise ValueError('invalid search result')
        if not isinstance(data['matches'], list) or len(data['matches']) > SEARCH_MATCHES:
            raise ValueError('invalid search matches')
        for match in data['matches']:
            if (not isinstance(match, dict) or set(match) != {
                    'path', 'start_line', 'end_line', 'excerpt', 'locations'}
                    or match['path'] not in self.mapper.fact_paths
                    or type(match['start_line']) is not int
                    or match['end_line'] != match['start_line']
                    or not isinstance(match['excerpt'], str)
                    or len(match['excerpt'].encode('utf-8')) > SEARCH_EXCERPT_BYTES
                    or not isinstance(match['locations'], list)):
                raise ValueError('invalid search match')
        return copy.deepcopy(data)

    def preview(self, arguments):
        return {'action_summary': '在当前导入资料中搜索关键词，不会修改文件。'}

    def verify_preview(self, arguments, preview):
        expected = {'action_summary': '在当前导入资料中搜索关键词，不会修改文件。'}
        if preview != expected:
            raise ValueError('invalid search preview')
        return copy.deepcopy(preview)


def _search_was_consumed(messages, index, paths):
    successful = {message.get('tool_call_id') for message in messages[index + 1:]
                  if message.get('role') == 'tool' and _successful_tool_message(message)}
    for message in messages[index + 1:]:
        if message.get('role') != 'assistant':
            continue
        for call in message.get('tool_calls') or []:
            function = call.get('function', {})
            if call.get('id') not in successful or function.get('name') != 'read_file':
                continue
            try:
                arguments = json.loads(function.get('arguments'))
            except (TypeError, ValueError):
                continue
            if arguments.get('path') in paths:
                return True
    return False


def _successful_tool_message(message):
    try:
        return json.loads(message['content']).get('ok') is True
    except (KeyError, TypeError, ValueError):
        return False


class ImportedDocumentPolicy(FilePolicy):
    def __init__(self, tool, mapper, output_path=None, *, agent=None):
        super().__init__(tool, output_path, agent=agent)
        if not self.directory or tool.workspace_identity != mapper.workspace_identity:
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        self.mapper = mapper
        self._reader = ReadFile(tool.workspace, set(mapper.fact_paths) | {'index.md'},
                                max_bytes=tool.max_bytes)
        if self._reader.workspace_identity != mapper.workspace_identity:
            raise ImportedDocumentError('IMPORT_INTEGRITY_ERROR')
        self._attempted = False
        self._had_error = False
        self._read = set()
        self._ever_read = set()
        self.catalog_snapshots = {}
        self.catalog_results = []

    def initial_messages(self, question, target):
        messages = super().initial_messages(question, target)
        messages[0]['content'] += ('\n导入资料可先用 search_documents 定位候选正文块；搜索和 index.md '
                                   '只是目录线索，回答前必须 read_file，且只能引用实际读取的正文块。')
        return messages

    def before(self, name, arguments):
        if name in {'search_documents', 'list_files', 'read_file'}:
            self._attempted = True

    def execute_read(self, name, arguments):
        if name == 'list_files':
            return self.tool.execute(name, arguments)
        path = arguments.get('path') if isinstance(arguments, dict) else None
        if path not in set(self.mapper.fact_paths) | {'index.md'}:
            return tool_error('PATH_DENIED')
        if path not in self._ever_read and len(self._ever_read) >= self.tool.max_files:
            return tool_error('FILE_COUNT_LIMIT')
        try:
            self.mapper._check_manifest()
        except ImportedDocumentError:
            return tool_error('FILE_CHANGED')
        result = self._reader.execute(arguments)
        if result.get('ok') and not self.mapper.verify_snapshot(path, result):
            self._reader.clear_result_proof()
            return tool_error('FILE_CHANGED')
        return result

    def clear_read_proof(self, name):
        if name == 'list_files':
            self.tool.clear_result_proof(name)
        else:
            self._reader.clear_result_proof()

    def take_read_proof(self, name, arguments):
        return (self.tool.take_result_proof(name, arguments) if name == 'list_files'
                else self._reader.take_result_proof(arguments))

    def accept(self, name, arguments, result):
        if name == 'write_file':
            return super().accept(name, arguments, result)
        self._attempted = True
        if name == 'search_documents':
            if result.get('ok') and result.get('evidence_role') == 'catalog':
                self.catalog_results.append(copy.deepcopy(result))
            else:
                self._had_error = True
            return
        if name == 'list_files':
            self.tool.record(name, arguments, result)
            if not result.get('ok'):
                self._had_error = True
            return
        path = arguments.get('path') if isinstance(arguments, dict) else None
        if path == 'index.md':
            if result.get('ok') and self.mapper.verify_snapshot(path, result):
                self.catalog_snapshots[path] = copy.deepcopy(result)
                self._ever_read.add(path)
            else:
                self.catalog_snapshots.pop(path, None)
                self._had_error = True
            return
        if path in self.mapper.fact_paths and result.get('ok') and self.mapper.verify_snapshot(path, result):
            self.snapshots[path] = copy.deepcopy(result)
            self._read.add(path)
            self._ever_read.add(path)
        else:
            self.snapshots.pop(path, None)
            self._read.discard(path)
            self._had_error = True

    def fact_coverage(self):
        discovered = list(self.mapper.fact_paths)
        read = [path for path in discovered if path in self._read]
        unread = [path for path in discovered if path not in self._read]
        return {'attempted': self._attempted, 'had_error': self._had_error,
                'discovered_files': discovered, 'read_files': read,
                'unread_files': unread, 'complete': not unread}

    def result_fields(self):
        result = {'scope': self.fact_coverage()}
        if self.output_path is not None:
            result['artifacts'] = copy.deepcopy(self.artifacts)
        return result

    def validate_answer(self, content):
        for path in tuple(self.snapshots):
            if not self.mapper.snapshot_is_current(path, self.snapshots[path]):
                self.snapshots.pop(path, None)
                self._read.discard(path)
                self._had_error = True
        return validate_answer(content, self.snapshots, None, False,
                               coverage=self.fact_coverage(), imported=True,
                               task_failure=bool(self.output_path and self.write_failure
                                                 and not self.artifacts))

    def enrich_validated_answer(self, answer):
        return self.mapper.enrich(answer, self.snapshots)

    def validate(self, content):
        answer = self.validate_answer(content)
        if self.output_path is not None and answer['status'] != 'unable' and not self.artifacts:
            raise AnswerError('OUTPUT_NOT_CREATED', 'The requested output has no actual creation receipt.')
        return self.enrich_validated_answer(answer)

    def model_request(self, messages, limit, schemas):
        from .file_tools import model_request
        standard = model_request(messages, limit, schemas)
        if len(_encoded(standard)) <= limit:
            return standard
        projected = copy.deepcopy(messages)
        for index, message in enumerate(projected):
            if message.get('role') != 'tool':
                continue
            try:
                result = json.loads(message['content'])
            except (KeyError, TypeError, ValueError):
                continue
            result.pop('scope', None)
            data = result.get('data', {})
            if (result.get('ok') and data.get('evidence_role') == 'catalog'
                    and isinstance(data.get('matches'), list)):
                paths = {match.get('path') for match in data['matches'] if isinstance(match, dict)}
                if _search_was_consumed(projected, index, paths):
                    data['matches'] = [{key: value for key, value in match.items() if key != 'excerpt'}
                                       for match in data['matches']]
                message['content'] = _encoded(result).decode('utf-8')
            elif (result.get('ok') and isinstance(data.get('content'), str)
                  and data.get('path') in self.mapper.fact_paths):
                body = data.pop('content')
                data['content_format'] = 'verbatim_utf8_after_blank_line'
                data['citation_lines'] = 'body_1_based'
                message['content'] = _encoded(result).decode('utf-8') + '\n\n' + body
            else:
                message['content'] = _encoded(result).decode('utf-8')
        return {'messages': projected, 'tools': copy.deepcopy(schemas)}
