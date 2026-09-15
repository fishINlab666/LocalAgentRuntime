# 助手、Skill 与 MCP 配置示例

这里说明当前配置和页面操作。实现与验收状态以[当前阶段记录](../../trial/directory-check.md)为准；导入配置、连接成功和真实模型完成任务是不同的结论。

## 三个内置助手

| 助手 ID | 页面名称 | 资料范围与用途 |
| --- | --- | --- |
| `file-qa` | 单文件问答 | 固定一个工作区内的文件，按需读取并核对原文 |
| `directory-qa` | 目录问答 | 先发现目录，再在配置预算内读取资料 |
| `project-brief` | 项目简报 | 使用目录范围，可绑定 Skill 方法和 MCP 只读数据，按来源整理简报 |

三个助手由同一套配置解析，定义在 [`agents.py`](../../local_agent/agents.py)。内置模板初始没有绑定 Skill 或 MCP；“项目简报”这个名称不意味着项目数据已经安装。

页面“当前助手”保留“自动选择”，兼容单文件与目录模式。选定具体助手后，新会话的资料范围跟随该助手；切换助手会清空旧会话视图，再加载所选助手的会话。

展开“管理助手、Skills 与 MCP”，从模板复制，填写新的 ID、名称、模型名称和助手说明，然后点击“创建助手”。“高级：工具与预算”只编辑 `tools` 和 `budgets`，其余字段从模板保留。创建完成后，新建会话即可使用。

完整配置采用 `schema_version: 1`：

| 字段 | 当前含义 |
| --- | --- |
| `id`、`name`、`instructions` | 助手标识、显示名称和补充方法；说明文本不能扩大权限 |
| `strategy` | `file`、`directory` 或 `combined`；后两者使用目录范围 |
| `model` | `provider`、`name`、`api_key_env`、`use_system_proxy`；当前 Provider 为 `deepseek` |
| `tools` | 从 `read_file`、`list_files`、`write_file`、`session_history` 中明确选择 |
| `budgets` | 完整保留模板里的调用次数、超时、输入大小、文件数量及文件大小限制 |
| `approval` | `ask_writes` 或 `deny_writes`；输出路径仍须由本轮用户请求指定 |
| `skills` | 每项固定为 `{"id": "技能ID", "version": "安装返回的64位摘要"}` |
| `mcp` | 每项包含 `id`、`version`、`tools`、`resources`、`prompts` |

`revision` 和 `enabled` 是管理接口返回的状态，不放进提交的纯助手配置。模型密钥只放在 `api_key_env` 指向的本机环境变量中，不填写到助手说明、JSON 或页面输入框。

## 导入并使用 Skill

可先使用本项目的两份文本示例：

- [project-brief](../skills/project-brief/SKILL.md)：项目简报方法，要求按需读取包内[来源检查表](../skills/project-brief/references/source-checklist.md)。
- [status-triage](../skills/status-triage/SKILL.md)：状态异常、影响与下一步整理方法。

在页面“Skill 文件夹路径”填写包含 `SKILL.md` 的本地文件夹路径，点击“导入 Skill”。导入后，在该能力条目点击“绑定到所选助手”，再新建会话。

安装会复制整个文本包并计算内容摘要。相同内容重复导入复用原版本；修改源文件再导入产生新版本，已有会话继续使用旧摘要。包内软链接、越界路径、非 UTF-8 文本和超限内容会被拒绝。主文件最多 8 KiB，包内文本合计最多 1 MiB，工具结果最多 12 KiB；长引用通过签名游标续读。

普通 Skill 只需 `SKILL.md` 的 YAML 头含 `name` 和 `description`，以及正文。`manifest.json` 是可选的本项目依赖声明，例如：

```json
{
  "required_tools": ["read_file"],
  "required_env": [],
  "requires_scripts": false
}
```

明确声明的工具或环境缺失时，能力显示缺项；未知结构化依赖保持不可用。自然语言 `compatibility` 只供阅读，不代表程序已经验证全部运行条件。`allowed-tools` 不会授予工具权限。包内脚本可以保留，但本版不执行；声明必须执行脚本的 Skill 不可用。

初始上下文只放可用 Skill 的名称、描述、ID 与版本，正文需要模型调用 `read_skill` 后才进入上下文。“本轮指定 Skill”可明确选择一个已绑定方法，仍需要工具实际读取。Skill 内容提供方法，不充当项目事实证据。

## 导入本地只读 MCP Server

页面导入的是本项目的配置文件，字段以 [`CapabilityStore.install_server`](../../local_agent/agent_runtime.py) 为准。它使用 `id`；直接调用 Adapter 的示例使用 `server_id`，两者不要混用。

在 `local-agent/` 工作目录运行下面的命令会新建 `project-demo.json`，已有同名文件时会停止，不覆盖。它不启动 Server，不调用模型。解释器和工作目录取自当前环境，均为绝对路径。

```sh
.venv/bin/python - <<'PY'
import json
from pathlib import Path
import sys

root = Path.cwd().resolve()
config = {
    "id": "project-demo",
    "command": sys.executable,
    "args": ["-B", str(root / "examples/mcp-project/server.py")],
    "cwd": str(root),
    "env_names": [],
    "allowed_tools": ["project_status"],
    "allowed_resources": ["project://qinghe/notes"],
    "allowed_prompts": ["project_brief"],
    "timeout_seconds": 5
}
with (root / "project-demo.json").open("x", encoding="utf-8") as target:
    json.dump(config, target, ensure_ascii=False, indent=2)
PY
```

这个配置对应[本地合成项目示例](../mcp-project/README.md)，需先准备好示例要求的扩展依赖。一般 Server 的 `command` 应指向已安装、可执行的程序，`args` 为参数数组，`cwd` 使用已存在的绝对目录；不写 shell 命令串，不隐式下载安装器。

`env_names` 只填写需要传给 Server 的环境变量名称，不填写值。只把已确认的只读工具、资源 URI 和模板名称加入对应 `allowed_*` 精确白名单；Server 自报只读不构成授权。资源 URI 交给该 Server 处理，不作为本机文件路径打开。

页面操作顺序：

1. 在“MCP 配置文件路径”填写刚生成文件的本地路径，点击“导入 MCP 配置”。
2. 查看状态，点击“测试连接”。这一步会启动配置中的本地程序，完成协议连接与能力发现后关闭连接。
3. 选择助手，点击对应 Server 的“绑定到所选助手”；绑定复制配置中的允许列表并保存新助手修订。
4. 新建会话并提出问题。模型可自主使用已绑定的只读 Tools 和 Resources；Prompt 还需要在“本轮 MCP 模板”明确选择，模板所需参数由模型按工具 Schema 提供。

本示例只提供合成项目状态，不代表已经取得真实项目数据。需要保存报告时，仍通过内置 `write_file` 的内容预览、用户决定和真实落盘回执。

## 导入、启用、绑定与会话版本

- **导入**：本机已有受管理的 Skill 包或 MCP 配置及版本；不表示依赖满足或 Server 连接成功。
- **启用**：允许该能力接受新调用。首次导入当前默认启用，重复导入保留原开关；仍需检查界面状态。
- **绑定**：某个助手修订明确使用哪些能力版本。导入到能力库不会自动绑定给所有助手。
- **会话快照**：会话创建时固定助手修订和绑定；后续新绑定只影响新会话。页面旧会话只显示原快照中的 Skill 和模板。

停用助手或能力不删除历史，也不能通过旧会话快照恢复已撤销权限。会话压缩后，Skill 正文若已不在当前上下文中，需要重新读取固定版本；旧 MCP 结果标识当时数据，询问最新状态必须重新调用。
