import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from local_agent.skills import SkillError, SkillLibrary, SkillTool
from local_agent.tool_runtime import wire_result


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = SkillLibrary(self.root / 'library')

    def package(self, name='project-brief', body='Compare project facts with sources.\n', **files):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        (directory / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: "Prepare a concise, sourced brief"\n---\n{body}',
            encoding='utf-8')
        for relative, content in files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        return directory

    def install_tool(self, **files):
        installed = self.library.install(self.package(**files))
        bindings = [{'id': installed['id'], 'version': installed['version']}]
        return installed, SkillTool(self.library, bindings, 'agent-a', 'run-a')

    def assert_error(self, code, callback):
        with self.assertRaises(SkillError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_install_copies_fixed_version_and_catalog_has_no_body(self):
        package = self.package(**{'references/checklist.md': 'Sources before conclusions.\n'})
        installed = self.library.install(package)
        bindings = [{'id': installed['id'], 'version': installed['version']}]
        catalog, statuses = self.library.resolve(bindings, [])
        self.assertEqual(set(catalog[0]), {'id', 'version', 'name', 'description'})
        self.assertEqual(statuses[0]['status'], 'ready')
        self.assertEqual(len(installed['version']), 64)
        (package / 'SKILL.md').write_text('changed source', encoding='utf-8')
        fresh = SkillLibrary(self.root / 'library')
        result = SkillTool(fresh, bindings, 'agent-a', 'run-a').execute(
            {'skill_id': installed['id'], 'relative_path': 'SKILL.md'})
        self.assertTrue(result['ok'])
        self.assertIn('Compare project facts', result['content'])
        self.assertEqual(result['source']['type'], 'skill')
        self.assertEqual(result['source']['version'], installed['version'])
        self.assertEqual(result['sha256'], result['source']['sha256'])
        self.assertEqual(fresh.list()[0]['id'], installed['id'])

    def test_same_version_is_idempotent_and_changed_content_gets_new_version(self):
        source = self.package()
        original = self.library.install(source)
        self.assertEqual(self.library.install(source)['version'], original['version'])
        (source / 'SKILL.md').write_text(
            '---\nname: project-brief\ndescription: Revised method\n---\nNew method.\n', encoding='utf-8')
        revised = self.library.install(source)
        self.assertNotEqual(original['version'], revised['version'])
        result = SkillTool(self.library, [original], 'agent-a', 'run-a').execute(
            {'skill_id': original['id'], 'relative_path': 'SKILL.md'})
        self.assertIn('Compare project facts', result['content'])

    def test_symlink_and_invalid_encoding_are_rejected(self):
        source = self.package()
        (self.root / 'outside.txt').write_text('private', encoding='utf-8')
        (source / 'reference.txt').symlink_to(self.root / 'outside.txt')
        self.assert_error('SKILL_PATH_DENIED', lambda: self.library.install(source))
        (source / 'reference.txt').unlink()
        (source / 'reference.txt').write_bytes(b'\xff')
        self.assert_error('SKILL_INVALID_ENCODING', lambda: self.library.install(source))

    def test_managed_package_symlink_cannot_create_directories_outside_library(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.library.root / 'packages').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((SkillError, OSError)):
            self.library.install(self.package())
        self.assertEqual(list(outside.iterdir()), [])

    def test_installed_package_mutation_and_symlink_are_not_read(self):
        installed, tool = self.install_tool(**{'references/check.txt': 'original'})
        args = {'skill_id': installed['id'], 'relative_path': 'references/check.txt'}
        target = self.library.root / 'packages' / installed['id'] / installed['version'] / args['relative_path']
        target.chmod(0o600)
        target.write_text('changed!', encoding='utf-8')
        self.assertEqual(tool.execute(args)['error']['code'], 'SKILL_CHANGED')
        target.unlink()
        outside = self.root / 'secret.txt'
        outside.write_text('secret', encoding='utf-8')
        target.symlink_to(outside)
        self.assertEqual(tool.execute(args)['error']['code'], 'SKILL_CHANGED')

    def test_main_file_and_package_size_limits(self):
        source = self.package(body='x' * 8192)
        self.assert_error('SKILL_MAIN_TOO_LARGE', lambda: self.library.install(source))
        self.package()
        (source / 'reference.txt').write_text('x' * (1024 * 1024), encoding='utf-8')
        self.assert_error('SKILL_PACKAGE_TOO_LARGE', lambda: self.library.install(source))

    def test_safe_yaml_requires_name_description_and_rejects_duplicate_keys(self):
        source = self.package()
        for header in ('name: project-brief\n', 'name: a\nname: b\ndescription: hi\n',
                       'name: !!python/object:builtins.object {}\ndescription: hi\n'):
            (source / 'SKILL.md').write_text('---\n' + header + '---\nbody', encoding='utf-8')
            self.assert_error('SKILL_INVALID_METADATA', lambda: self.library.install(source))

    def test_structured_dependencies_block_while_compatibility_is_only_displayed(self):
        source = self.package(**{'manifest.json': json.dumps({
            'required_tools': ['read_file'], 'required_env': ['DEV_AGENT_SKILL_TEST_MISSING']})})
        installed = self.library.install(source)
        with mock.patch.dict(os.environ, {}, clear=True):
            catalog, states = self.library.resolve([installed], [])
        self.assertEqual(catalog, [])
        self.assertIn('tool:read_file', states[0]['missing'])
        self.assertIn('environment:DEV_AGENT_SKILL_TEST_MISSING', states[0]['missing'])
        (source / 'manifest.json').write_text(json.dumps({'dependencies': {'unknown': True}}), encoding='utf-8')
        unknown = self.library.install(source)
        self.assertEqual(self.library.resolve([unknown], ['read_file'])[0], [])
        self.assertTrue(any('unsupported' in item for item in self.library.resolve([unknown], [])[1][0]['missing']))
        plain = self.package(name='compatibility-guide')
        (plain / 'SKILL.md').write_text('---\nname: compatibility-guide\ndescription: help\n'
            'compatibility: Requires a suitable browser\nallowed-tools: shell\n---\nText method.\n', encoding='utf-8')
        metadata = self.library.install(plain)
        catalog, states = self.library.resolve([metadata], [])
        self.assertEqual(len(catalog), 1)
        self.assertEqual(states[0]['compatibility'], 'Requires a suitable browser')
        self.assertEqual(metadata['allowed_tools'], 'shell')

    def test_scripts_are_retained_but_required_execution_is_unavailable(self):
        source = self.package(**{'scripts/check.py': 'raise RuntimeError("never run")\n'})
        installed = self.library.install(source)
        self.assertTrue(self.library.resolve([installed], [])[0])
        (source / 'manifest.json').write_text('{"requires_scripts": true}', encoding='utf-8')
        required = self.library.install(source)
        self.assertEqual(self.library.resolve([required], [])[0], [])
        self.assertIn('script_execution_unsupported', self.library.resolve([required], [])[1][0]['missing'])

    def test_unknown_structured_compatibility_or_manifest_requirements_are_unavailable(self):
        source = self.package()
        (source / 'SKILL.md').write_text('---\nname: project-brief\ndescription: help\n'
            'compatibility:\n  operating_system: exotic\n---\nText method.\n', encoding='utf-8')
        installed = self.library.install(source)
        self.assertEqual(self.library.resolve([installed], [])[0], [])
        self.package()
        (source / 'manifest.json').write_text('{"requirements": {"gpu": true}}', encoding='utf-8')
        installed = self.library.install(source)
        self.assertEqual(self.library.resolve([installed], [])[0], [])

    def test_frontmatter_optional_metadata_and_declared_version_are_preserved(self):
        source = self.package()
        (source / 'SKILL.md').write_text('---\nname: project-brief\ndescription: help\n'
            'license: MIT\nmetadata:\n  version: "1.2"\n  author: example\n---\nText method.\n', encoding='utf-8')
        installed = self.library.install(source)
        self.assertEqual(installed['declared_version'], '1.2')
        self.assertEqual(installed['frontmatter']['license'], 'MIT')

    def test_unbound_missing_version_and_disabled_skill_are_explicit(self):
        installed, tool = self.install_tool()
        args = {'skill_id': installed['id'], 'relative_path': 'SKILL.md'}
        self.assertEqual(tool.execute({**args, 'skill_id': 'not-bound'})['error']['code'], 'SKILL_NOT_BOUND')
        missing = SkillTool(self.library, [{'id': installed['id'], 'version': '0' * 64}], 'agent-a', 'run-a')
        self.assertEqual(missing.execute(args)['error']['code'], 'SKILL_VERSION_NOT_FOUND')
        self.library.set_enabled(installed['id'], False)
        self.assertEqual(tool.execute(args)['error']['code'], 'SKILL_DISABLED')
        self.assertFalse(tool.is_available(installed['id']))
        self.library.set_enabled(installed['id'], True)
        self.assertTrue(tool.execute(args)['ok'])

    def test_reader_rejects_path_escape_and_missing_reference(self):
        installed, tool = self.install_tool()
        for path in ('../outside.txt', '/tmp/file', 'references/../../file', 'x\\y', './SKILL.md'):
            result = tool.execute({'skill_id': installed['id'], 'relative_path': path})
            self.assertEqual(result['error']['code'], 'SKILL_PATH_DENIED')
        result = tool.execute({'skill_id': installed['id'], 'relative_path': 'missing.md'})
        self.assertEqual(result['error']['code'], 'SKILL_FILE_NOT_FOUND')

    def test_reference_pagination_is_bounded_and_bound_to_run_agent_and_path(self):
        body = ('资料 "quoted" \\ test\n' * 2200)
        installed, tool = self.install_tool(**{'references/long.txt': body, 'references/other.txt': body})
        args = {'skill_id': installed['id'], 'relative_path': 'references/long.txt'}
        first = tool.execute(args)
        self.assertTrue(first['ok'])
        cursor = first['next_cursor']
        for forbidden in (
            {**args, 'relative_path': 'references/other.txt', 'cursor': cursor},
            {**args, 'cursor': cursor[:-2] + 'xx'},
        ):
            self.assertEqual(tool.execute(forbidden)['error']['code'], 'SKILL_CURSOR_INVALID')
        for agent, run in (('agent-a', 'run-b'), ('agent-b', 'run-a')):
            other = SkillTool(self.library, [installed], agent, run)
            self.assertEqual(other.execute({**args, 'cursor': cursor})['error']['code'], 'SKILL_CURSOR_INVALID')
        chunks = []
        result = first
        while True:
            self.assertLessEqual(len(json.dumps(wire_result(result), ensure_ascii=False).encode('utf-8')), 12 * 1024)
            chunks.append(result['content'])
            if result['next_cursor'] is None:
                break
            result = tool.execute({**args, 'cursor': result['next_cursor']})
            self.assertTrue(result['ok'])
        self.assertEqual(''.join(chunks), body)

    def test_verification_requires_current_execution_and_exact_arguments(self):
        installed, tool = self.install_tool()
        args = {'skill_id': installed['id'], 'relative_path': 'SKILL.md'}
        result = tool.execute(args)
        data = {key: value for key, value in result.items() if key != 'ok'}
        forged = copy.deepcopy(data)
        forged['content'] = 'fabricated'
        with self.assertRaises(ValueError):
            tool.verify_success(args, forged)
        result = tool.execute(args)
        data = {key: value for key, value in result.items() if key != 'ok'}
        self.assertEqual(tool.verify_success(args, data), data)
        with self.assertRaises(ValueError):
            tool.verify_success(args, data)
        preview = tool.preview(args)
        self.assertEqual(tool.verify_preview(args, preview), preview)
        with self.assertRaises(ValueError):
            tool.verify_preview(args, {**preview, 'path': 'other.md'})


if __name__ == '__main__':
    unittest.main()
