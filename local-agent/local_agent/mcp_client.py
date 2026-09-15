"""Run-owned MCP SDK session over a bounded stdio transport."""

import asyncio
import concurrent.futures
import copy
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
import uuid

from .approvals import RunStopped
from .tool_runtime import parse_arguments


class MCPFailure(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')


class MCPClient:
    """Synchronous facade; all SDK operations live on one owned event loop."""

    def __init__(self, config, *, run_id, control=None):
        self.config = copy.deepcopy(config)
        self.server_id = config['server_id']
        self.run_id, self.control = run_id, control
        self.connection_id = uuid.uuid4().hex
        self.timeout_seconds = config.get('timeout_seconds', 5)
        self.request_count = 0
        self._loop = self._process = None
        self._closed = False
        self._failure = None
        self._ready = threading.Event()
        self._receipts = {}
        self._receipt_lock = threading.Lock()
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name='mcp-' + self.server_id)
        self._thread.start()
        if not self._ready.wait(max(5, self.timeout_seconds)) or self._failure:
            self.close()
            raise MCPFailure(self._failure or 'TOOL_TIMEOUT')
        try:
            initialized = self._submit(lambda: self._exchange(self._session.initialize))
            self.capabilities = initialized['result'].get('capabilities', {})
            self.protocol_version = initialized['result']['protocolVersion']
        except Exception:
            self.close()
            raise

    @property
    def is_alive(self):
        return bool(self._process is not None and self._process.returncode is None and not self._closed)

    def _thread_main(self):
        try:
            asyncio.run(self._main())
        except BaseException:
            self._failure = self._failure or 'MCP_DISCONNECTED'
        finally:
            self._ready.set()

    async def _main(self):
        # Import only when an MCP connection is explicitly enabled.
        import anyio
        from mcp import ClientSession
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._failed = asyncio.Event()
        self._exchange_lock = asyncio.Lock()
        self._active = None
        self._pending = {}
        env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP') if key in os.environ}
        env.update(self.config.get('env', {}))
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.config['command'], *self.config.get('args', []),
                cwd=self.config.get('cwd'), env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=(os.name == 'posix'), limit=65536)
            sender, receiver = anyio.create_memory_object_stream(0)
            writer = _SDKWriter(self)
            reader_task = asyncio.create_task(self._read_messages(sender))
            try:
                async with ClientSession(receiver, writer) as self._session:
                    self._ready.set()
                    await self._stop.wait()
            finally:
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)
                await sender.aclose()
                await receiver.aclose()
        finally:
            await self._reap()

    def _fail(self, code):
        self._failure = self._failure or code
        self._failed.set()

    async def _read_messages(self, sender):
        from mcp import types
        from mcp.shared.message import SessionMessage
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    self._fail('MCP_DISCONNECTED')
                    return
                if len(line) > 65536:
                    self._fail('TOOL_RESULT_TOO_LARGE')
                    return
                raw = parse_arguments(line.decode('utf-8'))
                message = types.JSONRPCMessage.model_validate(raw)
                if isinstance(message.root, (types.JSONRPCResponse, types.JSONRPCError)):
                    request_id = raw['id']
                    pending = self._pending.pop((type(request_id), request_id), None)
                    if pending is None:
                        self._fail('MCP_PROTOCOL_ERROR')
                        return
                    pending['response'] = copy.deepcopy(raw)
                await sender.send(SessionMessage(message))
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            self._fail('TOOL_RESULT_TOO_LARGE' if 'limit' in str(error).lower() else 'MCP_PROTOCOL_ERROR')
        except Exception:
            self._fail('MCP_PROTOCOL_ERROR')

    async def _exchange(self, operation):
        async with self._exchange_lock:
            if self._failure or self._closed:
                raise MCPFailure(self._failure or 'MCP_DISCONNECTED')
            pending = {}
            self._active = pending
            operation_task = asyncio.create_task(operation())
            failed_task = asyncio.create_task(self._failed.wait())
            try:
                done, _ = await asyncio.wait({operation_task, failed_task}, return_when=asyncio.FIRST_COMPLETED)
                if failed_task in done and self._failure:
                    raise MCPFailure(self._failure)
                try:
                    await operation_task
                except Exception as error:
                    raise MCPFailure('MCP_PROTOCOL_ERROR') from error
                response = pending.get('response')
                if not response or 'result' not in response or response['id'] != pending.get('id'):
                    raise MCPFailure('MCP_PROTOCOL_ERROR')
                return {'request_id': response['id'], 'method': pending['method'],
                        'result': copy.deepcopy(response['result'])}
            finally:
                operation_task.cancel()
                failed_task.cancel()
                await asyncio.gather(operation_task, failed_task, return_exceptions=True)
                self._active = None
                if 'id' in pending:
                    self._pending.pop((type(pending['id']), pending['id']), None)

    def _submit(self, operation):
        if self._closed or self._failure:
            raise MCPFailure(self._failure or 'MCP_DISCONNECTED')
        future = asyncio.run_coroutine_threadsafe(operation(), self._loop)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                if self.control is not None:
                    self.control.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPFailure('TOOL_TIMEOUT')
                try:
                    return future.result(timeout=min(.025, remaining))
                except concurrent.futures.TimeoutError:
                    continue
        except RunStopped as error:
            future.cancel()
            self.close()
            raise MCPFailure(error.code) from error
        except Exception:
            future.cancel()
            self.close()
            raise

    def request(self, method, params=None, *, call_id=None):
        from mcp import types
        result_types = {'tools/list': types.ListToolsResult, 'resources/list': types.ListResourcesResult,
            'prompts/list': types.ListPromptsResult, 'tools/call': types.CallToolResult,
            'resources/read': types.ReadResourceResult, 'prompts/get': types.GetPromptResult}
        if method not in result_types:
            raise MCPFailure('MCP_PROTOCOL_ERROR')
        request = types.ClientRequest.model_validate({'method': method, 'params': params or {}})
        response = self._submit(lambda: self._exchange(
            lambda: self._session.send_request(request, result_types[method])))
        if call_id is not None:
            receipt = {'server_id': self.server_id, 'run_id': self.run_id,
                'connection_id': self.connection_id, 'call_id': call_id,
                'request_id': response['request_id'], 'method': method,
                'sha256': hashlib.sha256(_canonical(response['result'])).hexdigest(),
                'token': uuid.uuid4().hex}
            with self._receipt_lock:
                self._receipts[receipt['token']] = copy.deepcopy(receipt)
            response['receipt'] = receipt
        return response

    def verify_receipt(self, receipt, result):
        try:
            with self._receipt_lock:
                known = self._receipts.get(receipt.get('token'))
            return (not self._closed and known == receipt and known is not None
                    and receipt['sha256'] == hashlib.sha256(_canonical(result)).hexdigest())
        except Exception:
            return False

    async def _reap(self):
        process = self._process
        if process is None:
            return
        if process.stdin:
            process.stdin.close()
        if process.returncode is None:
            self._kill_process()
        try:
            await asyncio.wait_for(process.wait(), timeout=.5)
        except (TimeoutError, ProcessLookupError):
            pass

    def _kill_process(self):
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except PermissionError:
            # Some hosts disallow process-group signals but allow the owned child.
            try:
                process.kill()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        with self._receipt_lock:
            self._receipts.clear()
        self._kill_process()
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)


class _SDKWriter:
    def __init__(self, client):
        self.client = client

    async def send(self, message):
        raw = message.message.model_dump(by_alias=True, mode='json', exclude_none=True)
        if 'id' in raw and 'method' in raw:
            if self.client._active is None or self.client.request_count >= 128:
                raise MCPFailure('TOOL_CALL_LIMIT')
            self.client.request_count += 1
            self.client._active.update(id=raw['id'], method=raw['method'])
            self.client._pending[(type(raw['id']), raw['id'])] = self.client._active
        encoded = _canonical(raw) + b'\n'
        if len(encoded) > 65536:
            raise MCPFailure('TOOL_RESULT_TOO_LARGE')
        self.client._process.stdin.write(encoded)
        await self.client._process.stdin.drain()

    async def aclose(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
