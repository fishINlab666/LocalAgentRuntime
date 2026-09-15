# 本地合成项目 MCP 示例

使用官方 Python SDK `mcp==1.30.0` 的 FastMCP，通过 stdio 提供三个能力：

| 类型 | 标识 | 内容 |
| --- | --- | --- |
| Tool | `project_status(project_id: str)` | `青禾-47` 的结构化状态：负责人林澄，完成 3 / 5，日期 `2026-09-14` |
| Resource | `project://qinghe/notes` | 固定文本项目笔记 |
| Prompt | `project_brief(audience: str)` | 仅提供简报组织方法，不包含项目事实或访问授权 |

所有项目资料均为合成数据。Server 不连接网络、不读取密钥、不读写用户项目文件，也不提供写入工具。SDK 的只读标记只是描述，宿主仍须执行自己的允许列表和权限检查。此示例验证本地官方 SDK Server 与本项目适配器的连接，不代表已验证任何第三方服务的互操作性或真实模型效果。

## 依赖和启动

在 `local-agent/` 工作目录使用已安装扩展依赖的 `.venv/bin/python`。如需从零准备环境，扩展依赖清单为 [`requirements-extensions.txt`](../../requirements-extensions.txt)：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-extensions.txt
```

确认 SDK 版本：

```sh
.venv/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("mcp"))'
```

通常由宿主启动此子进程；单独运行以下命令时会等待 stdin 中的 MCP 消息，可按 Ctrl-C 结束。stdout 留给协议，不能加入调试 `print`。

```sh
.venv/bin/python -B examples/mcp-project/server.py
```

## 本项目适配器配置示例

在 `local-agent/` 运行下面的片段。路径取自当前工作目录，解释器取自当前 `.venv` 进程；无需硬编码用户目录。它只启动本地 Server 并发现能力，不调用模型。

```sh
.venv/bin/python - <<'PY'
from pathlib import Path
import sys

from local_agent.mcp_tools import build_mcp_tools

root = Path.cwd().resolve()
config = {
    'server_id': 'project-demo',
    'transport': 'stdio',
    'command': sys.executable,
    'args': ['-B', str(root / 'examples/mcp-project/server.py')],
    'cwd': str(root),
    'env_names': [],
    'allowed_tools': ['project_status'],
    'allowed_resources': ['project://qinghe/notes'],
    'allowed_prompts': ['project_brief'],
    'timeout_seconds': 5,
}
handle = build_mcp_tools(config, run_id='project-demo-probe')
try:
    for kind, rows in handle.catalog.items():
        print(kind, [(row.get('name', row.get('uri')), row['enabled']) for row in rows])
    print('host tools:', [tool.spec.name for tool in handle.tools])
finally:
    handle.close()
PY
```

`allowed_*` 是精确允许列表；资源 URI 属于远端 MCP 命名空间，不是本地文件路径。`project_status` 的参数示例为 `{"project_id": "青禾-47"}`，未知项目返回工具错误，不编造状态。

Prompt 还需要宿主根据用户本次明确选择调用 `handle.select_prompt('project_brief')`，才能通过本项目适配器读取；允许列表本身不等于选择。参数为 `{"name": "project_brief", "arguments": {"audience": "研发团队"}}`。它的内容只能作为方法资料，项目事实必须来自实际工具或资源结果。
