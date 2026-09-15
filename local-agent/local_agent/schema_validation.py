"""Optional JSON Schema validation with bounded, local-only schema references."""

import copy
import json


MAX_SCHEMA_BYTES = 16384
_ANNOTATIONS = {'title', 'description', 'default', 'examples', 'deprecated', 'readOnly', 'writeOnly'}
_SINGLE = {'items', 'contains', 'additionalProperties', 'propertyNames', 'not', 'if', 'then', 'else',
           'unevaluatedProperties', 'unevaluatedItems'}
_MAPS = {'properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'}
_LISTS = {'allOf', 'anyOf', 'oneOf', 'prefixItems'}


def _library():
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry
    except ImportError as error:
        raise ValueError('Extension dependencies are not installed') from error
    return Draft202012Validator, FormatChecker, Registry


def _pointer(root, ref):
    if not isinstance(ref, str) or not (ref == '#' or ref.startswith('#/')):
        raise ValueError('Only local JSON Pointer references are supported')
    target = root
    for part in ref[2:].split('/') if ref != '#' else ():
        part = part.replace('~1', '/').replace('~0', '~')
        try:
            target = target[int(part)] if isinstance(target, list) else target[part]
        except (KeyError, IndexError, ValueError, TypeError) as error:
            raise ValueError('Unresolved schema reference') from error
    return target


def check_schema(schema, *, max_bytes=MAX_SCHEMA_BYTES):
    validator, formats, _ = _library()
    try:
        encoded = json.dumps(schema, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > max_bytes:
            raise ValueError('Schema exceeds capability budget')
        validator.check_schema(schema)
        allowed = set(validator.VALIDATORS) | _ANNOTATIONS | {'$schema', '$defs', 'definitions'}
        def visit(node, ancestors=()):
            if type(node) is bool:
                return
            if not isinstance(node, dict) or len(ancestors) >= 48 or id(node) in ancestors:
                raise ValueError('Unsupported recursive or deep schema')
            if set(node) - allowed:
                raise ValueError('Unsupported schema keyword')
            if '$schema' in node and node['$schema'] not in {
                    'https://json-schema.org/draft/2020-12/schema',
                    'http://json-schema.org/draft-07/schema#'}:
                raise ValueError('Unsupported schema dialect')
            if 'format' in node and node['format'] not in formats.checkers:
                raise ValueError('Unsupported format')
            path = (*ancestors, id(node))
            if '$ref' in node:
                visit(_pointer(schema, node['$ref']), path)
            for key in _SINGLE & node.keys():
                visit(node[key], path)
            for key in _MAPS & node.keys():
                for child in node[key].values():
                    visit(child, path)
            for key in _LISTS & node.keys():
                for child in node[key]:
                    visit(child, path)
        visit(schema)
    except Exception as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError('Unsupported JSON Schema') from error
    return copy.deepcopy(schema)


def valid_instance(schema, instance):
    try:
        validator, formats, registry = _library()
        check_schema(schema)
        json.dumps(instance, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return validator(schema, format_checker=formats(), registry=registry()).is_valid(instance)
    except Exception:
        return False


def wrap_arguments(schema, intent_schema):
    remote = check_schema(schema)
    def relocate(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == '$ref':
                    node[key] = '#/properties/arguments' + value[1:]
                else:
                    relocate(value)
        elif isinstance(node, list):
            for child in node:
                relocate(child)
    relocate(remote)
    return {'type': 'object', 'properties': {'intent': copy.deepcopy(intent_schema), 'arguments': remote},
            'required': ['intent', 'arguments'], 'additionalProperties': False}
