# Superpowers Skill + 官方 Time MCP 真实验收记录

## 结论

2026-09-17，本批真实 Gate **PASS**。默认持久 State Store 中的独立 Agent `superpowers-time-demo` 已在同一个非模拟 DeepSeek Run 内实际读取 `receiving-code-review` Skill、调用官方 Time MCP、读取项目 README，并把每个工具结果按原调用 ID 交回后续模型上下文。最终答案通过 Runtime 的来源与引用校验。

该结论证明当前安装、绑定和一次代表性任务的闭环有效。它只覆盖一个只读文本 Skill、官方 `mcp-server-time==2026.8.18` 的本地 stdio `get_current_time`，不证明所有 Skill/MCP、远程 MCP/OAuth、脚本型 Skill 或统计稳定性。

## 固定对象

- Skill：`receiving-code-review`，版本 `ef6a398e358bf5c144b9499f8f9ba67289965248610170997263a586d04520b7`，`SKILL.md` 为 6314 bytes。
- MCP：`official-time`，固定包 `mcp-server-time==2026.8.18`，配置版本 `3c20564bd305c972b14810e14e85c56b159a104987bcdafc5319f3ea74b3af36`，只启用 `get_current_time`。
- Agent：`superpowers-time-demo`，修订 `75aac7433b5527d60823d7fa46f4d1510a507c8f4e4c4a091b877b371419b53e`，只读且拒绝写入。
- 成功 Session：`2b5ec039016d409e90ec81e367500dc3`。
- 成功 Run：`807bb4dc7604455f987cf9687601087b`；`completed / ANSWER_VALIDATED`；4 次真实模型请求；`deepseek-v4-flash`，`simulated=false`，直连。

## 链路证据

JSONL 的关键顺序为：

1. seq 1 记录非模拟 DeepSeek `run.started`。
2. seq 3 模型同时提出 `read_skill`、`read_file` 和官方时间 MCP 调用。Skill 与 MCP 均真实成功；README 因模型尚未先列目录，被 Runtime 以 `PATH_NOT_DISCOVERED` 拒绝。
3. seq 19 的下一次模型请求包含上述三个原 ToolCall ID 及对应工具消息。模型收到真实错误后，在 seq 20 提出 `list_files`。
4. 目录列举结果在 seq 26 按原 ID 回填；模型随后重新提出 `read_file`，seq 32 成功读取 README 的 285 行内容。
5. seq 33 的最终模型请求包含本 Run 全部 5 个 ToolCall ID 与 5 个同 ID 工具结果；seq 35 为 `completed / ANSWER_VALIDATED`。

这次先失败再修正不是合成流程：SQLite 的 `tool_calls` 保留 5 条实际记录，其中首次 `read_file` 为 `failed`，`read_skill`、MCP、`list_files` 和第二次 `read_file` 为 `succeeded`；每条都有独立的 assistant/result message 关联。最终模型消息的持久状态为 `answer_valid`。

## MCP 与回答核对

MCP 返回：

- `datetime=2026-09-17T20:10:30+08:00`
- `day_of_week=Thursday`
- `is_dst=false`

独立主机记录窗口为 `20:10:23+08:00` 至 `20:10:35+08:00`，MCP 时间位于窗口内。MCP 回执包含本次 `server_id=official-time`、Run ID、Call ID、`request_id=2`、`method=tools/call` 和结果 SHA256，结果标记为 `received_from_mcp_server`。

最终回答引用 README 第 9、84、95、285 行，正确判断文档只宣称本地 stdio 白名单 MCP，远程 MCP/OAuth 仍在范围外，因此拒绝了错误评审意见；同时正确复述 `Thursday` 与 UTC `+08:00`。5 条引用均由 Runtime 补入本轮实际来源原文。

## 保留的失败证据

第一次真实 Run `a6f62acb80d0401bbbe93d42cb91dfba` 保持为 `validation_failed / INVALID_ANSWER`。其中 Skill、README、MCP 和上下文回填均成功，但 DeepSeek 两次把真实换行直接放入 JSON 字符串，严格解析失败。随后只做一次受限重试：任务事实与能力不变，只要求最终 `answer` 使用无换行短字符串。没有修改 Runtime 来掩盖失败，也没有把失败 Run 改写成通过。

## 本地原始证据

- 成功结果：`trial/results/superpowers-mcp-live-retry-20260917T143132/`（Git 忽略）。
- 成功 JSONL：`~/.local/share/local-agent/live-logs/807bb4dc7604455f987cf9687601087b.jsonl`。
- 持久记录：`~/.local/share/local-agent/sessions.sqlite3` 中同 Run 的 `runs`、`tool_calls`、`context_manifests` 与 `messages`。
- 最终只读 Gate：真实 Provider、JSONL 终态、Skill、README、官方 MCP、主机时间、原 ID 回填、引用答案和 SQLite 共 9/9 项 PASS。

密钥只在终端通过隐藏输入进入环境变量，没有写入聊天、配置、结果文件或本记录。该验收是 AI/工程证据核对，不冒充用户对产品体验的签字。
