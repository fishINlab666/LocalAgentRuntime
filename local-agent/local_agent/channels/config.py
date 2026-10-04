"""Explicit, non-secret Feishu identity binding configuration."""
import json
from pathlib import Path
import re


class ChannelConfigError(ValueError):
    pass


FIELDS = {'app_id', 'tenant_key', 'open_id', 'chat_id', 'session_id', 'agent_id'}


def validate_config(value):
    if (not isinstance(value, dict) or not FIELDS <= value.keys() <= FIELDS | {'enabled'}
            or any(not isinstance(value[k], str)
                   or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value[k]) for k in FIELDS)
            or type(value.get('enabled', True)) is not bool):
        raise ChannelConfigError('FEISHU_CONFIG_INVALID')
    return {**value, 'enabled': value.get('enabled', True)}


def load_config(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate field')
            result[key] = value
        return result
    try:
        with Path(path).open('rb') as source:
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ValueError('size')
        return validate_config(json.loads(raw, object_pairs_hook=unique))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ChannelConfigError('FEISHU_CONFIG_INVALID') from None
