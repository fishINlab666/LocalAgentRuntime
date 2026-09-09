"""One non-streaming DeepSeek adapter; credentials never enter model context."""

from dataclasses import dataclass
import json
import math
import os
import socket
from typing import Callable, Protocol
import urllib.error
import urllib.request


class ProviderError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ModelReply:
    message: dict
    usage: dict | None = None


class Provider(Protocol):
    metadata: dict

    def complete(self, messages: list[dict], tools: list[dict], timeout: float) -> ModelReply: ...


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeepSeekProvider:
    endpoint = 'https://api.deepseek.com/chat/completions'
    max_response_bytes = 2 * 1024 * 1024

    def __init__(self, api_key: str, model: str, *, transport: Callable | None = None,
                 use_system_proxy: bool = False):
        if not isinstance(api_key, str) or not api_key.strip() or not isinstance(model, str) or not model.strip():
            raise ProviderError('CONFIG_MISSING')
        if any(c in api_key + model for c in '\r\n\x00') or type(use_system_proxy) is not bool:
            raise ProviderError('CONFIG_INVALID')
        self._api_key = api_key
        self.model = model
        self._use_system_proxy = use_system_proxy
        self._transport = transport or self._post
        self.metadata = {'provider': 'deepseek', 'model': model, 'simulated': False,
                         'proxy_mode': 'system' if use_system_proxy else 'direct'}

    @classmethod
    def from_env(cls):
        proxy_setting = os.environ.get('AGENT_USE_SYSTEM_PROXY', '0')
        if proxy_setting not in ('0', '1'):
            raise ProviderError('CONFIG_INVALID')
        return cls(os.environ.get('DEEPSEEK_API_KEY') or os.environ.get('AGENT_API_KEY', ''),
                   os.environ.get('AGENT_MODEL', 'deepseek-v4-flash'),
                   use_system_proxy=proxy_setting == '1')

    def _post(self, payload: dict, timeout: float) -> dict:
        request = urllib.request.Request(self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._api_key},
            method='POST')
        try:
            handlers = [NoRedirect()]
            if not self._use_system_proxy:
                handlers.insert(0, urllib.request.ProxyHandler({}))
            with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
                data = response.read(self.max_response_bytes + 1)
            if len(data) > self.max_response_bytes:
                raise ProviderError('RESPONSE_TOO_LARGE')
            return json.loads(data)
        except urllib.error.HTTPError as error:
            code = {401: 'AUTH_ERROR', 403: 'AUTH_ERROR', 429: 'RATE_LIMIT'}.get(error.code, 'HTTP_ERROR')
            error.close()
            raise ProviderError(code) from None
        except (TimeoutError, socket.timeout):
            raise ProviderError('MODEL_TIMEOUT') from None
        except (urllib.error.URLError, OSError):
            raise ProviderError('NETWORK_ERROR') from None
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderError('INVALID_MODEL_RESPONSE') from None

    def complete(self, messages: list[dict], tools: list[dict], timeout: float) -> ModelReply:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ProviderError('MODEL_TIMEOUT')
        payload = {'model': self.model, 'messages': messages, 'stream': False,
                   'thinking': {'type': 'disabled'}, 'max_tokens': 2048}
        if any(message.get('role') == 'tool' for message in messages):
            payload['response_format'] = {'type': 'json_object'}
        if tools:
            payload['tools'] = tools
            payload['tool_choice'] = 'auto'
        response = self._transport(payload, timeout)
        try:
            choices = response['choices']
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError()
            choice = choices[0]
            if choice['finish_reason'] not in ('stop', 'tool_calls'):
                raise ValueError()
            source = choice['message']
            if source['role'] != 'assistant' or not isinstance(source.get('content'), (str, type(None))):
                raise ValueError()
            message = {'role': 'assistant', 'content': source.get('content')}
            if source.get('tool_calls'):
                if choice['finish_reason'] != 'tool_calls' or not isinstance(source['tool_calls'], list):
                    raise ValueError()
                message['tool_calls'] = source['tool_calls']
            elif choice['finish_reason'] == 'tool_calls' or not message['content']:
                raise ValueError()
            usage = response.get('usage')
            if isinstance(usage, dict):
                usage = {k: v for k, v in usage.items() if k in (
                    'prompt_tokens', 'completion_tokens', 'total_tokens') and type(v) is int and v >= 0}
            else:
                usage = None
            return ModelReply(message, usage)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError('INVALID_MODEL_RESPONSE') from None
