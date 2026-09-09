# 单文件证据问答 Implementation Plan

> **For agentic workers:** Use test-driven-development for implementation and requesting-code-review for an independent review. 用户已批准修订及直接开发，不再逐步骤请求确认。

**Goal:** 完成可运行的单文件 Tool Calling 内核、受控样例、自动测试与真实模型验收入口；没有密钥则报告真实验收未执行。

**Architecture:** Python 单进程，Runtime 依赖可注入 Provider 与只读工具。所有模型/工具结果经主循环回填和落日志；引用校验独立于语义验收。取消使用共享 Event 和有界等待，后台在途 HTTP 不保证被服务端中止。

**Tech Stack:** Python 3.11+ 标准库，unittest、dataclasses、urllib、threading、JSONL。无外部依赖；本机 Python 3.14。当前非 Git 仓库，在 `local-agent/` 子目录开发，不自动创建远端。

## 文件与固定接口

- `local-agent/local_agent/files.py`：`ReadFile(workspace, allowed_paths, max_bytes=32768).execute(arguments)` 返回可 JSON 序列化 dict；成功字段 `ok,path,content,line_count,bytes,sha256`，失败字段 `ok,error:{code,message}`。仅读所允许的相对路径，拒绝链接和非文本。
- `local-agent/local_agent/answers.py`：`validate_answer(text, snapshots, target_path, read_attempted)` 返回合法答案 dict；不合法抛 `AnswerError(code)`。snapshots 是 path → read_file 成功结果的映射。约定 JSON、引用和状态见修订设计稿 6.3。
- `local-agent/local_agent/provider.py`：`ModelReply(message, usage=None)`；`Provider.complete(messages, tools, timeout)`。真实 Provider 管理 HTTP 和响应转换，禁止跟随携带凭据的重定向，错误不回显请求正文/凭据。
- `local-agent/local_agent/runtime.py`：`Runtime(provider, tool, trace, config).run(question, target_path, cancel=None)` 返回结果 dict，包含 run_id、state、stop_reason、model_calls、answer。约束轮数、单次/总时限、输入大小和工具调用数。
- `local-agent/local_agent/trace.py`：`Trace(directory, workspace, debug_content=False).emit(event, data)`；JSONL 保存在可读工作区之外，默认仅元数据；受控测试可启用正文。
- `local-agent/local_agent/__main__.py`：`python3 -m local_agent`，提供 run、demo、evaluate；命令中的 demo 明示测试替身，不冒充模型。
- `local-agent/local_agent/evaluation.py`：四类场景各三次，全量记录与自动预筛；人工评审默认为 pending，失败/未执行不放行。
- `local-agent/tests/test_files.py`、`test_answers.py`、`test_runtime.py`、`test_provider.py`、`test_cli.py`、`test_evaluation.py`：验证各公共接口与完整闭环。
- `local-agent/README.md`、`.gitignore`、`examples/workspace/demo-note.md`：操作说明、忽略凭据/日志、无敏感信息的样例。

模型与工具消息遵守以下次序，ToolCall 的 id 不能丢失：

```python
messages.append(reply.message)
messages.append({"role": "tool", "tool_call_id": call_id,
                 "content": json.dumps(tool_result, ensure_ascii=False)})
```

最终回答的唯一格式：

```json
{"status":"answered","answer":"答案正文","citations":[{"path":"demo-note.md","start_line":1,"end_line":1,"quote":"对应完整行文本"}]}
```

## 执行顺序

### 1. 修订方案与准备环境

- [x] 核对审阅三项问题，修订原设计稿并保留来源材料。
- [x] 验证 Python 环境和手册基线；确认无现有代码仓库。
- [x] 写入包入口、忽略文件、合成样例。

### 2. 只读执行与引用校验（独立模块）

- [x] 先写失败测试：有效读取、文件变更、越界、符号链接、硬链接、非普通文件、文件过大、非法参数及 UTF-8 错误。
- [x] 先写失败测试：合法引用、缺引用、错误行号、布尔行号、错误摘录、未读取就回答、not_found / unable 的证据规则。
- [x] 运行 `PYTHONPATH=local-agent python3 -m unittest discover -s local-agent/tests -p 'test_files.py' -v`，观察预期失败，再实现最少逻辑。
- [x] 用同样命令运行 `test_answers.py`，实现并复测。

### 3. Provider 与 Runtime

- [x] 写入可控 Provider 测试替身，验证初始请求不含文件正文、第二次请求带完整 ToolCall/Result、错误也回填、换内容新运行不串数据。
- [x] 写停止保护测试：模型/工具超时、总期限、取消、迟到响应、最大轮数、上下文超限、未知/重复调用 id、过多工具请求。
- [x] 运行 `PYTHONPATH=local-agent python3 -m unittest discover -s local-agent/tests -p 'test_runtime.py' -v`，确认缺失行为，再实现 Runtime。
- [x] Provider 使用官方工具消息协议；HTTP 测试通过注入 transport 模拟响应，测试配置缺失、协议错误、截断、限流与响应过大，不访问真实网络。
- [x] 日志默认不存正文或密钥，保存内容摘要和事件；正文调试显式开启；写日志失败不得报告成功。

### 4. 可运行入口与验收

- [x] 先写 CLI / evaluation 测试，再完成入口、样例和说明。
- [x] `demo` 必须明示 simulated，使用同一 Runtime 和真实文件工具；不读取真实会议材料。
- [x] `evaluate` 创建临时合成工作区，标准答案在工具范围之外，四类各三次；自动预筛全过仍待人工核查，绝不默认人工通过。
- [x] 全量运行 `PYTHONPATH=local-agent python3 -m unittest discover -s local-agent/tests -v`。
- [x] 在 `local-agent/` 运行 `python3 -m local_agent demo`，检查完整记录和输出；再运行 `python3 -m local_agent evaluate`，密钥缺失应明确报告未执行、非零退出。
- [x] 密钥配置后才发真实请求；不把聊天中的密钥保存到代码。未配置时交付完整本地实现与尚待执行的真实验收步骤。

### 5. 独立审阅与交付

- [x] 独立代理先检查设计符合度，再检查代码质量与测试；修复关键发现后重跑相关测试。
- [x] 运行 `python3 tests/validate_handbook.py`，确认来源保持完整。
- [x] README 记录真实已通过的检查、未完成的真实模型/人工验收及可复制命令；更新本计划状态。

## 当前边界

本轮不实现页面、目录工具、流式输出、会话持久化、MCP/Skill/Cron/Memory。它们按批准的后续批次推进，不能以本轮测试通过宣称 M1 或产品验收通过。

## 本次执行证据（2026-09-08）

用户已回复确认模型服务采用 DeepSeek（沿用会议建议）；默认模型名通过 `AGENT_MODEL` 配置。密钥只存在于用户的 Terminal 进程，不保存到项目或 Codex 执行环境。

| 检查 | 实际结果 |
|---|---|
| 自动测试 | 94 项通过；以 `-W error::ResourceWarning` 运行同一套测试，退出码 0 |
| 模拟演示 | `python3 -m local_agent demo` 退出码 0，真实读取样例，模拟 Provider 两次调用 |
| 回填核对 | 初始请求没有样例独有代号；第二次请求含工具读取正文；日志以 completed 终结 |
| 无密钥验收入口 | `python3 -m local_agent evaluate` 退出码 2，`NOT_RUN / CONFIG_MISSING`，0 / 12 次已执行 |
| 真实模型连通 | 修复 Python CA 后，真实运行完成 2 次 DeepSeek 调用、`read_file` 与结果回填；系统代理导致的长请求中断已用直连规避 |
| 真实回答格式 | 首次单次调用的格式问题已通过 `response_format` 与 JSON 示例解决；第二轮整组评测又发现英文双引号未转义，现对严格 JSON 语法失败增加一次受控纠错 |
| 首次真实 12 次验收 | 已执行 12 / 12；11 次通过，`not_found` 第 2 次只输出 “I'll read the file…”、未发 ToolCall，整组按规则记为 `FAILED` |
| 第二次真实 12 次验收 | 已执行 12 / 12；11 次通过，`known` 第 1 次读取和事实均正确，但最终答案内的英文双引号未转义，整组按规则记为 `FAILED` |
| 第三次真实 12 次验收 | 12 条模型运行的状态、答案、引用和消息链均符合标准；旧评测器把“没有提及预算信息”误判为非明确缺失，规则修正后对原报告离线复算 12 / 12，人工复核为 `PASS` |
| 学习手册回归 | `python3 tests/validate_handbook.py` 输出 PASS |
| 需求符合审查 | 独立检查完成，无剩余 P1/P2 实现偏差；真实产品 Gate 仍未放行 |
| 代码质量审查 | 独立检查完成，唯一 P2 日志路径替换问题已修复并复验，无剩余 P1/P2 |

演示日志：[381ed43825b8430882ac773a2b0199e3.jsonl](../../../local-agent/runs/381ed43825b8430882ac773a2b0199e3.jsonl)。首次真实验收报告：[report.json](../../../local-agent/runs/evaluation-fa65cd7f7c934819af871ec20036e1e1/report.json)。第二次真实验收报告：[report.json](../../../local-agent/runs/evaluation-bbd1243c827b4797b3c967defc76096b/report.json)。第三次真实验收报告：[report.json](../../../local-agent/runs/evaluation-5e3ed57d7ed04ddcbb9f371217247df6/report.json) 与 [human-review.md](../../../local-agent/runs/evaluation-5e3ed57d7ed04ddcbb9f371217247df6/human-review.md)。这些是本地运行产物，已被 `local-agent/.gitignore` 排除，不应混入源代码提交。

审阅中已通过失败测试复现并修正：工具超时之后继续行动、验收次数可缩减、等待计算跨过总期限产生负数、取消后才调度的工作线程仍开始操作、取消终结时日志失败导致异常逃逸、HTTP 错误响应未关闭，以及日志或父目录被替换后写到错误位置。每项修正后均重跑相关检查。

前三轮真实整组验收依次暴露首轮未发 ToolCall、最终 JSON 引号未转义和验收器同义表达误判。前两项通过首轮动作约束和一次受控格式纠错修复；第三项通过“主题词 + 明确缺失表达”的有限规则修复。第三轮现有 12 条结果经修正规则复算并逐项人工核对后全部通过，单文件切片 Gate 已关闭。

尚待真实环境完成：

- [x] 配置可用模型凭据，验证基础连通与真实消息协议。
- [x] 同一固定配置跑完四类各三次，全部满足对应标准。
- [x] 工程师按报告逐项核对语义、引用和消息链，保存人工结论；失败则修复后重跑整组。

当前目录不是 Git 仓库，未创建提交、分支、远端或 PR。实现文件保留在 `local-agent/`，便于后续纳入版本管理。
