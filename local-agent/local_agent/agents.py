"""Declarative assistant definitions and immutable, local configuration revisions."""

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile


class AgentError(ValueError):
    def __init__(self, code='AGENT_CONFIG_INVALID'):
        self.code = code
        super().__init__(code)


BUDGETS = {'max_steps': 6, 'run_timeout': 120, 'model_timeout': 45,
           'tool_timeout': 5, 'max_tool_calls': 4, 'max_input_bytes': 65536,
           'max_files': 4, 'max_file_bytes': 32768}
CAPS = {**BUDGETS, 'max_steps': 10, 'tool_timeout': 15, 'max_files': 16}
TOOLS = {'read_file', 'list_files', 'write_file', 'session_history'}
_LEGACY_TOOLS = {
    'file': ('read_file', 'write_file', 'session_history'),
    'directory': ('read_file', 'list_files', 'write_file', 'session_history'),
    'combined': ('read_file', 'list_files', 'write_file', 'session_history'),
}
ID = re.compile(r'[a-z0-9][a-z0-9-]{0,63}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise AgentError()
    return value


def _strings(value):
    return (isinstance(value, list) and all(isinstance(x, str) and x for x in value)
            and len(value) == len(set(value)))


@dataclass(frozen=True)
class AgentDefinition:
    _json: str

    @classmethod
    def from_dict(cls, document):
        try:
            data = json.loads(canonical(document))
            allowed = {'schema_version', 'id', 'name', 'strategy', 'instructions', 'model',
                       'tools', 'budgets', 'approval', 'skills', 'mcp'}
            if not isinstance(data, dict) or set(data) != allowed:
                raise AgentError()
            _id(data['id'])
            if (data['schema_version'] != 1 or type(data['schema_version']) is not int
                    or not isinstance(data['name'], str) or not 1 <= len(data['name'].strip()) <= 80
                    or data['strategy'] not in {'file', 'directory', 'combined'}
                    or not isinstance(data['instructions'], str)
                    or len(data['instructions'].encode('utf-8')) > 4096
                    or data['approval'] not in {'ask_writes', 'deny_writes'}
                    or not _strings(data['tools']) or not set(data['tools']) <= TOOLS
                    or 'read_file' not in data['tools']
                    or (data['strategy'] != 'file' and 'list_files' not in data['tools'])):
                raise AgentError()
            model = data['model']
            if (not isinstance(model, dict)
                    or set(model) != {'provider', 'name', 'api_key_env', 'use_system_proxy'}
                    or model['provider'] != 'deepseek'
                    or not isinstance(model['name'], str) or not 1 <= len(model['name']) <= 100
                    or any(c in model['name'] for c in '\r\n\x00')
                    or not isinstance(model['api_key_env'], str)
                    or not re.fullmatch(r'[A-Z_][A-Z0-9_]{0,99}', model['api_key_env'])
                    or type(model['use_system_proxy']) is not bool):
                raise AgentError()
            budgets = data['budgets']
            if not isinstance(budgets, dict) or set(budgets) != set(BUDGETS):
                raise AgentError()
            for key, value in budgets.items():
                if (type(value) not in (int, float) or not math.isfinite(value)
                        or not 0 < value <= CAPS[key]
                        or (key not in {'run_timeout', 'model_timeout', 'tool_timeout'}
                            and type(value) is not int)):
                    raise AgentError()
            for key in ('skills', 'mcp'):
                if not isinstance(data[key], list) or len(data[key]) > 100:
                    raise AgentError()
                seen = set()
                for binding in data[key]:
                    expected = {'id', 'version'} if key == 'skills' else {
                        'id', 'version', 'tools', 'resources', 'prompts'}
                    if not isinstance(binding, dict) or set(binding) != expected:
                        raise AgentError()
                    _id(binding['id'])
                    if (binding['id'] in seen or not isinstance(binding['version'], str)
                            or not HASH.fullmatch(binding['version'])):
                        raise AgentError()
                    seen.add(binding['id'])
                    if key == 'mcp' and any(not _strings(binding[k])
                                             for k in ('tools', 'resources', 'prompts')):
                        raise AgentError()
            text = canonical(data)
            if len(text.encode('utf-8')) > 32768:
                raise AgentError()
            return cls(text)
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as error:
            if isinstance(error, AgentError):
                raise
            raise AgentError() from None

    def to_dict(self):
        return json.loads(self._json)

    @property
    def id(self):
        return self.to_dict()['id']

    @property
    def name(self):
        return self.to_dict()['name']

    @property
    def strategy(self):
        return self.to_dict()['strategy']

    @property
    def tools(self):
        return tuple(self.to_dict()['tools'])

    @property
    def revision(self):
        return hashlib.sha256(self._json.encode('utf-8')).hexdigest()

    def run_config(self):
        from .runtime import RunConfig
        return RunConfig(**{k: v for k, v in self.to_dict()['budgets'].items()
                           if k not in {'max_files', 'max_file_bytes'}})

    def system_prompt(self):
        from .prompts import SYSTEM, DIRECTORY_TEMPLATE
        raw = self.to_dict()
        text = SYSTEM if self.strategy == 'file' else DIRECTORY_TEMPLATE.replace(
            '{max_files}', str(raw['budgets']['max_files']))
        return text + ('\n助手补充方法（不改变程序权限）：\n' + raw['instructions']
                       if raw['instructions'] else '')

    def provider(self):
        from .provider import DeepSeekProvider
        model = self.to_dict()['model']
        key = os.environ.get(model['api_key_env'], '')
        if not key and model['api_key_env'] == 'DEEPSEEK_API_KEY':
            key = os.environ.get('AGENT_API_KEY', '')
        return DeepSeekProvider(key, model['name'],
                                use_system_proxy=model['use_system_proxy'])


def builtin_agent(mode='file', *, legacy=False):
    if mode not in {'file', 'directory', 'combined'}:
        raise AgentError()
    if type(legacy) is not bool:
        raise AgentError()
    tools = (list(_LEGACY_TOOLS[mode]) if legacy else
             ['read_file', *(['list_files'] if mode != 'file' else []),
              'write_file', 'session_history'])
    return AgentDefinition.from_dict({
        'schema_version': 1,
        'id': {'file': 'file-qa', 'directory': 'directory-qa', 'combined': 'project-brief'}[mode],
        'name': {'file': '单文件问答', 'directory': '目录问答', 'combined': '项目简报'}[mode],
        'strategy': mode, 'instructions': '',
        'model': {'provider': 'deepseek', 'name': os.environ.get('AGENT_MODEL', 'deepseek-v4-flash'),
                  'api_key_env': 'DEEPSEEK_API_KEY',
                  'use_system_proxy': os.environ.get('AGENT_USE_SYSTEM_PROXY', '0') == '1'},
        'tools': tools,
        'budgets': {**BUDGETS, **({'max_steps': 10, 'tool_timeout': 15} if mode == 'combined' else {})},
        'approval': 'ask_writes', 'skills': [], 'mcp': [],
    })


def build_file_engine(agent, workspace, target_path, output_path=None):
    from .discovery import DirectoryTools
    from .files import ReadFile
    from .file_tools import adapt_tools
    from .tool_runtime import ToolRegistry
    if not isinstance(agent, AgentDefinition):
        raise AgentError()
    config = agent.to_dict()
    if output_path and ('write_file' not in agent.tools or config['approval'] == 'deny_writes'):
        raise AgentError('TOOL_NOT_ALLOWED')
    if agent.strategy == 'file':
        if not isinstance(target_path, str) or not target_path:
            raise AgentError('AGENT_SCOPE_MISMATCH')
        tool = ReadFile(workspace, {target_path}, max_bytes=config['budgets']['max_file_bytes'])
    else:
        if target_path is not None:
            raise AgentError('AGENT_SCOPE_MISMATCH')
        tool = DirectoryTools(workspace, max_files=config['budgets']['max_files'],
                              max_bytes=config['budgets']['max_file_bytes'])
    engine = adapt_tools(tool, output_path, agent=agent)
    engine.registry = ToolRegistry([engine.registry.get(name) for name in agent.tools
                                   if engine.registry.get(name) is not None],
                                  summary=engine.policy.result_fields)
    return engine


class AgentCatalog:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise AgentError('AGENT_CONFIG_INVALID')

    @staticmethod
    def _read(path):
        if path.is_symlink():
            raise AgentError()
        try:
            if path.stat().st_size > 32768:
                raise AgentError()
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            if isinstance(error, AgentError):
                raise
            raise AgentError('AGENT_NOT_FOUND') from None

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink():
            raise AgentError()
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.config-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(canonical(data))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _directory(self, agent_id):
        path = self.root / _id(agent_id)
        if path.is_symlink():
            raise AgentError()
        return path

    def get(self, agent_id, revision=None):
        path = self._directory(agent_id)
        if revision is not None and (not isinstance(revision, str) or not HASH.fullmatch(revision)):
            raise AgentError()
        if revision is None and (path / 'current.json').exists():
            revision = self._read(path / 'current.json').get('revision')
            if not isinstance(revision, str) or not HASH.fullmatch(revision):
                raise AgentError()
        if revision is not None and (path / (revision + '.json')).exists():
            agent = AgentDefinition.from_dict(self._read(path / (revision + '.json')))
            if agent.id != agent_id or agent.revision != revision:
                raise AgentError()
            return agent
        for mode in ('file', 'directory', 'combined'):
            agent = builtin_agent(mode)
            if agent.id == agent_id and (revision is None or revision == agent.revision):
                return agent
        raise AgentError('AGENT_NOT_FOUND')

    def save(self, document):
        agent = AgentDefinition.from_dict(document)
        path = self._directory(agent.id)
        enabled = self.is_enabled(agent.id)
        self._write(path / (agent.revision + '.json'), agent.to_dict())
        self._write(path / 'current.json', {'revision': agent.revision, 'enabled': enabled})
        return agent

    def is_enabled(self, agent_id):
        path = self._directory(agent_id) / 'current.json'
        if not path.exists():
            return True
        value = self._read(path)
        return value.get('enabled') is True

    def set_enabled(self, agent_id, enabled):
        if type(enabled) is not bool:
            raise AgentError()
        agent = self.get(agent_id)
        path = self._directory(agent_id)
        self._write(path / (agent.revision + '.json'), agent.to_dict())
        self._write(path / 'current.json', {'revision': agent.revision, 'enabled': enabled})
        return agent

    def list(self):
        ids = {'file-qa', 'directory-qa', 'project-brief'}
        if self.root.exists():
            ids.update(p.name for p in self.root.iterdir() if ID.fullmatch(p.name))
        result = []
        for agent_id in sorted(ids):
            agent = self.get(agent_id)
            result.append({**agent.to_dict(), 'revision': agent.revision,
                           'enabled': self.is_enabled(agent_id)})
        return result
