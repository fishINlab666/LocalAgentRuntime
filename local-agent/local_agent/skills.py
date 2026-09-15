"""Install immutable text skill packages and expose bounded, proven reads."""

import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat

from .files import _open_directory, _path_parts, _workspace_identity
from .tool_runtime import ToolSpec, tool_error, wire_result


MAIN_BYTES = 8 * 1024
PACKAGE_BYTES = 1024 * 1024
RESULT_BYTES = 12 * 1024
_ID = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
_ERRORS = frozenset({
    'INVALID_ARGUMENT', 'SKILL_NOT_BOUND', 'SKILL_VERSION_NOT_FOUND',
    'SKILL_DISABLED', 'SKILL_DEPENDENCIES_MISSING', 'SKILL_PATH_DENIED',
    'SKILL_FILE_NOT_FOUND', 'SKILL_CHANGED', 'SKILL_CURSOR_INVALID',
    'SKILL_IO_ERROR', 'SKILL_RESULT_TOO_LARGE',
})
_USER_ERRORS = frozenset({
    'SKILL_VERSION_NOT_FOUND', 'SKILL_DISABLED', 'SKILL_DEPENDENCIES_MISSING',
    'SKILL_CHANGED', 'SKILL_IO_ERROR', 'SKILL_RESULT_TOO_LARGE',
})


class SkillError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def _read_regular(root, parts, maximum, identity=None):
    parent = _open_directory(root, parts[:-1], identity)
    try:
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise SkillError('SKILL_PATH_DENIED')
            if before.st_size > maximum:
                raise SkillError('SKILL_PACKAGE_TOO_LARGE')
            chunks, size = [], 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > maximum:
                    raise SkillError('SKILL_PACKAGE_TOO_LARGE')
            after = os.fstat(descriptor)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise SkillError('SKILL_CHANGED')
            return b''.join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _text(raw):
    try:
        result = raw.decode('utf-8')
    except UnicodeError as error:
        raise SkillError('SKILL_INVALID_ENCODING') from error
    if any(ord(char) < 32 and char not in '\t\r\n' for char in result):
        raise SkillError('SKILL_INVALID_ENCODING')
    return result


def _frontmatter(raw):
    if len(raw) > MAIN_BYTES:
        raise SkillError('SKILL_MAIN_TOO_LARGE')
    text = _text(raw)
    # Leave room for the final provenance envelope even with escaped JSON text.
    if len(_json(text).encode('utf-8')) > RESULT_BYTES - 3072:
        raise SkillError('SKILL_MAIN_TOO_LARGE')
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        raise SkillError('SKILL_INVALID_METADATA')
    end = next((number for number, line in enumerate(lines[1:], 1)
                if line.strip() == '---'), None)
    if end is None or not ''.join(lines[end + 1:]).strip():
        raise SkillError('SKILL_INVALID_METADATA')
    try:
        import yaml
    except ImportError as error:
        raise SkillError('SKILL_DEPENDENCY_MISSING', 'Install the optional PyYAML dependency.') from error

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            return _unique((self.construct_object(key, deep=deep),
                            self.construct_object(value, deep=deep)) for key, value in node.value)

    header = ''.join(lines[1:end])
    try:
        if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(header)):
            raise ValueError('YAML aliases are not supported')
        metadata = yaml.load(header, Loader=UniqueLoader)
        if (not isinstance(metadata, dict)
                or not isinstance(metadata.get('name'), str)
                or not 1 <= len(metadata['name']) <= 64 or not _ID.fullmatch(metadata['name'])
                or not isinstance(metadata.get('description'), str)
                or not 1 <= len(metadata['description'].strip()) <= 1024):
            raise ValueError('name and description are required')
        _json(metadata)
    except (ValueError, TypeError, RecursionError, yaml.YAMLError) as error:
        raise SkillError('SKILL_INVALID_METADATA') from error
    return metadata


def _dependencies(frontmatter, manifest):
    tools, environment, unsupported = set(), set(), set()
    scripts = False
    for declaration in (frontmatter, manifest):
        for field, target in (('required_tools', tools), ('required_env', environment)):
            values = declaration.get(field, [])
            if (not isinstance(values, list)
                    or any(not isinstance(item, str) or not item.strip() for item in values)):
                unsupported.add('unsupported_dependency:' + field)
            else:
                target.update(values)
        if type(declaration.get('requires_scripts', False)) is not bool:
            unsupported.add('unsupported_dependency:requires_scripts')
        scripts = scripts or declaration.get('requires_scripts') is True
        for field in ('dependencies', 'requires'):
            if field in declaration:
                unsupported.add('unsupported_dependency:' + field)
        for field, value in declaration.items():
            if (field.startswith(('require', 'depend'))
                    and field not in {'required_tools', 'required_env', 'requires_scripts'}):
                unsupported.add('unsupported_dependency:' + field)
            if field == 'compatibility' and not isinstance(value, str):
                unsupported.add('unsupported_dependency:compatibility')
    return {'tools': sorted(tools), 'environment': sorted(environment),
            'scripts': scripts, 'unsupported': sorted(unsupported)}


def _mkdirs_at(root_descriptor, parts):
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class SkillLibrary:
    def __init__(self, root):
        requested = Path(root).absolute()
        if requested.is_symlink():
            raise SkillError('SKILL_PATH_DENIED')
        requested.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = requested.resolve()
        self._identity = _workspace_identity(self.root)

    def _index(self):
        try:
            raw = _read_regular(self.root, ['library.json'], 4 * PACKAGE_BYTES, self._identity)
        except FileNotFoundError:
            return {'skills': {}}
        try:
            value = json.loads(raw, object_pairs_hook=_unique)
            if not isinstance(value, dict) or not isinstance(value.get('skills'), dict):
                raise ValueError('invalid index')
            return value
        except (ValueError, UnicodeError) as error:
            raise SkillError('SKILL_IO_ERROR') from error

    def _save_index(self, index):
        root_descriptor = _open_directory(self.root, [], self._identity)
        temporary = '.library-' + secrets.token_hex(12)
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=root_descriptor)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                output.write(_json(index))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, 'library.json', src_dir_fd=root_descriptor, dst_dir_fd=root_descriptor)
        finally:
            try:
                os.unlink(temporary, dir_fd=root_descriptor)
            except FileNotFoundError:
                pass
            os.close(root_descriptor)

    @staticmethod
    def _collect(source):
        if source.is_symlink() or not source.is_dir():
            raise SkillError('SKILL_PATH_DENIED')
        source = source.resolve()
        identity = _workspace_identity(source)
        files, total = {}, 0

        def visit(parts):
            nonlocal total
            descriptor = _open_directory(source, parts, identity)
            try:
                entries = sorted(os.listdir(descriptor))
                for name in entries:
                    relative = '/'.join([*parts, name])
                    if _path_parts(relative) is None or len(relative) > 240:
                        raise SkillError('SKILL_PATH_DENIED')
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        visit([*parts, name])
                    elif stat.S_ISREG(info.st_mode):
                        if len(files) >= 512:
                            raise SkillError('SKILL_PACKAGE_TOO_LARGE')
                        raw = _read_regular(source, [*parts, name], PACKAGE_BYTES - total, identity)
                        _text(raw)
                        files[relative] = raw
                        total += len(raw)
                    else:
                        raise SkillError('SKILL_PATH_DENIED')
            finally:
                os.close(descriptor)

        try:
            visit([])
        except OSError as error:
            raise SkillError('SKILL_PATH_DENIED') from error
        return files

    def install(self, source_dir):
        files = self._collect(Path(source_dir))
        if 'SKILL.md' not in files:
            raise SkillError('SKILL_INVALID_METADATA', 'The package requires SKILL.md.')
        frontmatter = _frontmatter(files['SKILL.md'])
        try:
            manifest = json.loads(files.get('manifest.json', b'{}'), object_pairs_hook=_unique)
            if not isinstance(manifest, dict):
                raise ValueError('manifest must be an object')
        except (ValueError, UnicodeError) as error:
            raise SkillError('SKILL_INVALID_METADATA') from error
        skill_id = manifest.get('id', frontmatter['name'])
        if not isinstance(skill_id, str) or len(skill_id) > 64 or not _ID.fullmatch(skill_id):
            raise SkillError('SKILL_INVALID_METADATA')
        file_index = {path: {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
                      for path, raw in files.items()}
        version = hashlib.sha256(_json(file_index).encode('utf-8')).hexdigest()
        optional = frontmatter.get('metadata', {})
        declared_version = manifest.get('version', frontmatter.get('version',
            optional.get('version') if isinstance(optional, dict) else None))
        metadata = {'id': skill_id, 'version': version, 'name': frontmatter['name'],
                    'description': frontmatter['description'],
                    'compatibility': frontmatter.get('compatibility'),
                    'declared_version': declared_version,
                    'allowed_tools': frontmatter.get('allowed-tools'),
                    'metadata': frontmatter.get('metadata', {}),
                    'frontmatter': copy.deepcopy(frontmatter),
                    'dependencies': _dependencies(frontmatter, manifest),
                    'files': file_index, 'bytes': sum(len(raw) for raw in files.values())}
        index = self._index()
        existing = index['skills'].get(skill_id)
        if ((existing is not None and existing['name'] != metadata['name'])
                or any(item['name'] == metadata['name'] and other_id != skill_id
                       for other_id, item in index['skills'].items())):
            raise SkillError('SKILL_CONFLICT')
        if existing and version in existing['versions']:
            return {**copy.deepcopy(existing['versions'][version]), 'enabled': existing['enabled']}
        root_descriptor = _open_directory(self.root, [], self._identity)
        try:
            parent_descriptor = _mkdirs_at(root_descriptor, ['packages', skill_id])
        finally:
            os.close(root_descriptor)
        temporary = '.install-' + secrets.token_hex(12)
        staging_descriptor = None
        try:
            os.mkdir(temporary, mode=0o700, dir_fd=parent_descriptor)
            staging_descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                         dir_fd=parent_descriptor)
            for relative, raw in files.items():
                parts = relative.split('/')
                target_parent = _mkdirs_at(staging_descriptor, parts[:-1])
                try:
                    target = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o400, dir_fd=target_parent)
                    with os.fdopen(target, 'wb') as output:
                        output.write(raw)
                finally:
                    os.close(target_parent)
            try:
                info = os.stat(version, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISDIR(info.st_mode):
                    raise SkillError('SKILL_PATH_DENIED')
                for relative, expected in file_index.items():
                    actual = _read_regular(self.root, ['packages', skill_id, version, *relative.split('/')],
                                           PACKAGE_BYTES, self._identity)
                    if hashlib.sha256(actual).hexdigest() != expected['sha256']:
                        raise SkillError('SKILL_CHANGED')
            else:
                os.rename(temporary, version, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
            entry = index['skills'].setdefault(skill_id, {
                'name': metadata['name'], 'enabled': True, 'versions': {}})
            entry['versions'][version] = metadata
            self._save_index(index)
        finally:
            if staging_descriptor is not None:
                os.close(staging_descriptor)
            try:
                shutil.rmtree(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent_descriptor)
        return {**copy.deepcopy(metadata), 'enabled': entry['enabled']}

    def list(self):
        return [{**copy.deepcopy(metadata), 'enabled': item['enabled']}
                for _, item in sorted(self._index()['skills'].items())
                for _, metadata in sorted(item['versions'].items())]

    def set_enabled(self, skill_id, enabled):
        if type(enabled) is not bool:
            raise SkillError('INVALID_ARGUMENT')
        index = self._index()
        if skill_id not in index['skills']:
            raise SkillError('SKILL_VERSION_NOT_FOUND')
        index['skills'][skill_id]['enabled'] = enabled
        self._save_index(index)
        return {'id': skill_id, 'enabled': enabled}

    def resolve(self, bindings, available_tools):
        index = self._index()
        available = set(available_tools)
        catalog, statuses, seen = [], [], set()
        for binding in bindings:
            skill_id, version = binding.get('id'), binding.get('version')
            if not isinstance(skill_id, str) or not isinstance(version, str) or skill_id in seen:
                raise SkillError('INVALID_ARGUMENT', 'Each binding requires a unique id and fixed version.')
            seen.add(skill_id)
            entry = index['skills'].get(skill_id)
            metadata = entry and entry['versions'].get(version)
            state = {'id': skill_id, 'version': version, 'status': 'unavailable', 'missing': []}
            if metadata is None:
                state['error'] = 'SKILL_VERSION_NOT_FOUND'
            elif not entry['enabled']:
                state.update(status='disabled', error='SKILL_DISABLED')
            else:
                required = metadata['dependencies']
                missing = [f'tool:{name}' for name in required['tools'] if name not in available]
                missing += [f'environment:{name}' for name in required['environment'] if not os.environ.get(name)]
                missing += required['unsupported']
                if required['scripts']:
                    missing.append('script_execution_unsupported')
                state.update(missing=missing, compatibility=metadata.get('compatibility'))
                if missing:
                    state['error'] = 'SKILL_DEPENDENCIES_MISSING'
                else:
                    state['status'] = 'ready'
                    catalog.append({key: metadata[key] for key in ('id', 'version', 'name', 'description')})
            statuses.append(state)
        return copy.deepcopy(catalog), copy.deepcopy(statuses)

    def read(self, skill_id, version, relative_path):
        parts = _path_parts(relative_path)
        if parts is None:
            raise SkillError('SKILL_PATH_DENIED')
        entry = self._index()['skills'].get(skill_id)
        metadata = entry and entry['versions'].get(version)
        if metadata is None:
            raise SkillError('SKILL_VERSION_NOT_FOUND')
        if not entry['enabled']:
            raise SkillError('SKILL_DISABLED')
        expected = metadata['files'].get(relative_path)
        if expected is None:
            raise SkillError('SKILL_FILE_NOT_FOUND')
        try:
            raw = _read_regular(self.root, ['packages', skill_id, version, *parts],
                                PACKAGE_BYTES, self._identity)
        except (OSError, SkillError) as error:
            raise SkillError('SKILL_CHANGED') from error
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected['sha256'] or len(raw) != expected['bytes']:
            raise SkillError('SKILL_CHANGED')
        return _text(raw), digest, len(raw)


class SkillTool:
    def __init__(self, library, bindings, agent_id, run_id, available_tools=()):
        self.library = library
        self.bindings = copy.deepcopy(list(bindings))
        self.agent_id, self.run_id = agent_id, run_id
        self.available_tools = available_tools
        self._key, self._proof = secrets.token_bytes(32), None
        self.spec = ToolSpec('read_skill',
            '读取已绑定 Skill 的方法或包内文本引用。仅作为方法来源，不证明项目事实；长引用使用 next_cursor 继续。',
            {'type': 'object', 'properties': {
                'skill_id': {'type': 'string', 'minLength': 1, 'maxLength': 64},
                'relative_path': {'type': 'string', 'minLength': 1, 'maxLength': 240},
                'cursor': {'type': 'string', 'minLength': 1, 'maxLength': 2048}},
             'required': ['skill_id', 'relative_path'], 'additionalProperties': False},
            source='skill', max_result_bytes=RESULT_BYTES,
            error_codes=_ERRORS, user_error_codes=_USER_ERRORS)

    def _binding(self, skill_id):
        binding = next((item for item in self.bindings if item.get('id') == skill_id), None)
        if binding is None:
            raise SkillError('SKILL_NOT_BOUND')
        available = self.available_tools() if callable(self.available_tools) else self.available_tools
        _, statuses = self.library.resolve([binding], available)
        if statuses[0]['status'] != 'ready':
            raise SkillError(statuses[0]['error'])
        return binding

    def is_available(self, skill_id):
        try:
            self._binding(skill_id)
            return True
        except (SkillError, OSError):
            return False

    def validate(self, arguments):
        try:
            if (not isinstance(arguments, dict) or set(arguments) - {'skill_id', 'relative_path', 'cursor'}
                    or any(not isinstance(arguments.get(key), str) for key in ('skill_id', 'relative_path'))
                    or ('cursor' in arguments and not isinstance(arguments['cursor'], str))):
                raise SkillError('INVALID_ARGUMENT')
            if _path_parts(arguments['relative_path']) is None:
                raise SkillError('SKILL_PATH_DENIED')
            self._binding(arguments['skill_id'])
        except SkillError as error:
            return tool_error(error.code, str(error), _USER_ERRORS)
        except OSError:
            return tool_error('SKILL_IO_ERROR', user_error_codes=_USER_ERRORS)
        return None

    def _cursor(self, identity, offset):
        payload = _json([*identity, offset]).encode('utf-8')
        signature = hmac.new(self._key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature + payload).decode('ascii')

    def _offset(self, cursor, identity, length):
        try:
            if len(cursor) > 2048:
                raise ValueError('cursor too large')
            raw = base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True)
            signature, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.new(self._key, payload, hashlib.sha256).digest()):
                raise ValueError('invalid signature')
            value = json.loads(payload)
            if (not isinstance(value, list) or value[:-1] != identity
                    or type(value[-1]) is not int or not 0 < value[-1] < length):
                raise ValueError('invalid cursor binding')
            return value[-1]
        except (ValueError, TypeError, UnicodeError, IndexError) as error:
            raise SkillError('SKILL_CURSOR_INVALID') from error

    def execute(self, arguments):
        self._proof = None
        invalid = self.validate(arguments)
        if invalid is not None:
            return invalid
        try:
            skill_id, path = arguments['skill_id'], arguments['relative_path']
            binding = self._binding(skill_id)
            version = binding['version']
            content, digest, total = self.library.read(skill_id, version, path)
            identity = [self.agent_id, self.run_id, skill_id, version, path, digest]
            offset = self._offset(arguments['cursor'], identity, len(content)) if 'cursor' in arguments else 0
            if path == 'SKILL.md' and 'cursor' in arguments:
                raise SkillError('SKILL_CURSOR_INVALID')

            def page(end):
                chunk = content[offset:end]
                return {'ok': True, 'skill_id': skill_id, 'version': version, 'path': path,
                        'content': chunk, 'sha256': digest, 'bytes': len(chunk.encode('utf-8')),
                        'total_bytes': total, 'offset': offset,
                        'next_cursor': self._cursor(identity, end) if end < len(content) else None,
                        'source': {'type': 'skill', 'id': skill_id, 'version': version,
                                   'path': path, 'sha256': digest, 'agent_id': self.agent_id,
                                   'run_id': self.run_id, 'evidence_role': 'method'}}

            result = page(len(content))
            if len(_json(wire_result(result)).encode('utf-8')) > RESULT_BYTES:
                if path == 'SKILL.md':
                    raise SkillError('SKILL_RESULT_TOO_LARGE')
                low, high = offset, len(content)
                while low < high:
                    middle = (low + high + 1) // 2
                    if len(_json(wire_result(page(middle))).encode('utf-8')) <= RESULT_BYTES:
                        low = middle
                    else:
                        high = middle - 1
                if low == offset:
                    raise SkillError('SKILL_RESULT_TOO_LARGE')
                result = page(low)
            self._binding(skill_id)
            self._proof = (copy.deepcopy(arguments), {key: copy.deepcopy(value)
                                                     for key, value in result.items() if key != 'ok'})
            return result
        except SkillError as error:
            return tool_error(error.code if error.code in _ERRORS else 'SKILL_CHANGED', str(error), _USER_ERRORS)
        except (OSError, ValueError, TypeError):
            return tool_error('SKILL_IO_ERROR', user_error_codes=_USER_ERRORS)

    def verify_success(self, arguments, data):
        proof, self._proof = self._proof, None
        if proof != (arguments, data):
            raise ValueError('skill result did not come from this execution')
        self._binding(arguments['skill_id'])
        return copy.deepcopy(data)

    def preview(self, arguments):
        return {'action_summary': f"读取 Skill {arguments['skill_id']} 的 {arguments['relative_path']}。",
                'path': arguments['relative_path']}

    def verify_preview(self, arguments, preview):
        if preview != self.preview(arguments):
            raise ValueError('skill preview does not match arguments')
        return copy.deepcopy(preview)

    def result_fields(self):
        return {}
