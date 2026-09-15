# MCP 扩展实施与互操作记录

2026-09-14，D 阶段实施及定向协议验证已核实。模型调用 **0 次**；本记录不代表真实模型联合任务验收。

## 已证明的范围

- `mcp==1.30.0` 的官方 `ClientSession` 负责握手及协议；宿主限制 stdio 消息、调用预算、发现页数和总大小，冻结本 Run 清单。
- Tools/Resources/Prompts 走统一 ToolRuntime；MCP 参数使用 `{intent, arguments}`，保留远端字段、嵌套类型与本地引用，非本地引用及不支持 Schema 拒绝启用。
- 本地明确白名单、来源授权、用户本轮选择模板分别生效。模板仅作为方法来源；成功事实内容也只证明收到并校验了该 Server 的本次返回。
- 回执绑定 Server、Run、连接、Host ToolCall ID、实际远端请求 ID 与响应摘要；旧回执不能移到后续调用或另一连接。取消、超时、错 ID、断线和超限不会成为成功。
- `MCPAdapter.result_fields()` 返回空字典，MCP 信封不会附加本地文件目录 scope。最终信封上限 12 KiB。

本机向进程组发信号曾返回 `PermissionError`；已补上仅终止本批直属子进程的兼容路径。固定测试确认直属进程结束且没有管道 ResourceWarning；未宣称 stdio 是沙箱或已验证任意 Server 派生进程树。

## 定向验证

以下命令均在 `local-agent/` 执行；相同通过版本不重复回归。

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_mcp.py'` | 10 项 PASS，1.640 秒 |
| `.venv/bin/python -m unittest discover -s tests -p 'test_tool_runtime.py'` | 14 项 PASS，0.056 秒 |
| `python3 -m unittest discover -s tests -p 'test_tool_runtime.py'` | 14 项，13 PASS、1 个依赖扩展的用例跳过，0.030 秒 |
| `git diff --check -- local_agent/tool_runtime.py tests/test_tool_runtime.py` | PASS |

新增来源授权、嵌套参数、缺失 Adapter、外层取消等用例均先出现预期失败，再实现并通过。真实独立进程与故障固定组使用 `tests/fixtures/mcp_server.py`，不需要模型。

## 两种真进程证据

原始及修订结果保存在 [协议证据目录](results/mcp-interop-20260914-ce74dd3a/README.md)。

1. [合成官方 SDK Server](../examples/mcp-project/README.md)：`examples/mcp-project/server.py` 使用 FastMCP，Tools/Resources/Prompts、模板选择限制、错误和回执验证通过，0.518 秒。它是自建演示，不是第三方互操作证据。
2. [独立维护的官方 Time Server](https://github.com/modelcontextprotocol/servers/tree/main/src/time)：锁定 `mcp-server-time==2026.8.18`，仅调用一次 `get_current_time("Asia/Shanghai")`。成功返回 `2026-09-14T17:50:45+08:00`，远端请求 ID 为 `2`，Host ToolCall ID 为 `official-time-call-1`，进程已回收。发现 `get_current_time` 和 `convert_time`，只启用前者；原始输入 Schema 完整保存在报告中，没有 outputSchema 的合法文本/JSON 返回通过验证。

Time 原探测脚本将返回的秒精度时间与微秒精度调用起点直接比较，产生 `AssertionError`；原报告保留 FAIL，未覆盖。随后仅回放已保存结果：实际工具 `ok=True`、响应摘要和原调用 ID 匹配；返回时间与原报告落盘时间相差 0.164422 秒，符合服务源码的 `isoformat(timespec="seconds")`。修订结论为 **协议互操作 PASS**。没有第二次工具调用，也没有补造未保存的调用前后微秒值。

包安装访问 PyPI。Time 执行只向本地 stdio 子进程传入时区字符串，源码使用 `datetime`/`zoneinfo` 计算；没有提供模型密钥、业务文件或原始会议资料，没有模型调用。记录证明 ToolRuntime 返回了已校验结果；没有声称真实 Provider 收到了下一轮请求。

## 环境及复现

本次使用 `local-agent/.venv/bin/python`，Python 3.14.6。核心可选扩展依赖固定在 [requirements-extensions.txt](../requirements-extensions.txt)：MCP 1.30.0、PyYAML 6.0.3、jsonschema 4.26.0。Time 是单独安装的互操作对象，不强制作为所有扩展用户的运行依赖。

```sh
.venv/bin/python -m pip install -r requirements-extensions.txt
.venv/bin/python -m pip install mcp-server-time==2026.8.18 mcp==1.30.0
```

以下为以后复现用命令，本记录编写时没有再运行；每执行一次会新增一次本地 Time 工具调用，仍不调用模型。

```sh
.venv/bin/python - <<'PY'
import json
import sys
import threading
from types import SimpleNamespace
from local_agent.approvals import RunControl
from local_agent.mcp_tools import build_mcp_tools
from local_agent.tool_runtime import ToolRegistry, ToolRuntime

handle = build_mcp_tools({
    'server_id': 'official-time', 'command': sys.executable,
    'args': ['-m', 'mcp_server_time', '--local-timezone=Asia/Shanghai'],
    'env_names': [], 'allowed_tools': ['get_current_time'],
    'allowed_resources': [], 'allowed_prompts': [], 'timeout_seconds': 5,
}, run_id='time-reproduction')
try:
    tool = handle.tools[0]
    policy = SimpleNamespace(before=lambda *a: None, accept=lambda *a: None,
        result_fields=lambda: {}, authorize_tool=lambda t, a: t in handle.tools)
    result = ToolRuntime(ToolRegistry(handle.tools), policy).invoke({
        'id': 'time-reproduction-call', 'function': {'name': tool.spec.name,
            'arguments': json.dumps({'intent': '核对本地时区时间',
                'arguments': {'timezone': 'Asia/Shanghai'}}, ensure_ascii=False)}},
        budget_ok=True, execute_bounded=lambda f, timeout: f(),
        emit=lambda *a: None, control=RunControl(threading.Event())).result
    assert result['ok'], result
    print(result['data']['content'])
    print('remote request ID:', result['data']['receipt']['request_id'])
finally:
    handle.close()
PY
```
