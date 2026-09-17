# Superpowers Skill + Official MCP Live Trial Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在用户默认的持久 State Store 中安装一个可独立运行的 Superpowers Skill 与官方 Time MCP，并用新建 Agent 的真实 DeepSeek Run 证明两类结果都进入下一轮上下文和后台日志。

**Architecture:** 保留三个内置 Agent 不变，新建 `superpowers-time-demo`。Skill 固定复制 `receiving-code-review` 当前版本；MCP 固定使用已安装的 `mcp-server-time==2026.8.18`，只允许只读工具 `get_current_time`。安装、绑定和协议探测只是准备；最终 Gate 读取 SQLite 与 JSONL，核对 ToolCall、MCP receipt、上下文回填、真实 Provider 和语义回答。

**Tech Stack:** Local Agent CLI、SQLite、JSONL trace、Superpowers `receiving-code-review`、官方 MCP Python SDK 1.30.0、`mcp-server-time==2026.8.18`、DeepSeek。

---

## §0 当前进度与停止条件

**状态：已核实（2026-09-17）。** 方案 A 已获用户确认。之前打开的 8771 页面是临时 `--demo` 环境，不能作为本次证据。官方 Time Server 曾在历史 R4 中通过，本次另行产生了持久 Agent、Session、Run 和 trace，没有用旧报告替代完成证明。

`receiving-code-review` 已以版本 `ef6a398...20b7` 安装；官方 Time MCP 已以固定包 `mcp-server-time==2026.8.18`、配置版本 `3c20564...af36` 安装，只启用 `get_current_time`；独立 Agent `superpowers-time-demo` 修订 `75aac743...b53e` 已绑定两项能力。第一次把 `.venv/bin/python` 作为 command 的配置在安装器规范化后变成系统解释器，协议探测真实失败为 `MCP_DISCONNECTED`；改用绝对 `uvx` 与固定包版本后 probe 成功。直接适配器调用返回 `2026-09-16T17:09:57+08:00 / Wednesday`，MCP receipt 验真通过，与主机时间差 `0.578282` 秒。

最终真实 Run `807bb4dc7604455f987cf9687601087b` 为 `completed / ANSWER_VALIDATED`，Provider 是非模拟 `deepseek-v4-flash`。它实际读取 Skill，调用官方 Time MCP，先因目录权限得到 `PATH_NOT_DISCOVERED`，收到错误后执行 `list_files` 并成功读取 README；5 个调用的结果都以原调用 ID 进入后续模型请求。MCP 返回 `2026-09-17T20:10:30+08:00 / Thursday`，位于主机 `20:10:23` 至 `20:10:35` 的记录窗口内；SQLite 最终模型消息为 `answer_valid`。第一次真实 Run `a6f62acb80d0401bbbe93d42cb91dfba` 因答案字符串含未转义换行而保留为 `validation_failed / INVALID_ANSWER`，没有改写成成功。完整核对见[真实验收记录](../../../local-agent/trial/superpowers-mcp-live-check.md)。

停止条件已经满足：同一成功 Run 中实际完成 `read_skill(SKILL.md)`、README 读取和 `official-time/get_current_time(Asia/Shanghai)`；工具结果以原调用 ID 回填；MCP 回执包含本次 server/run/call/request/sha256；Provider 为非模拟 DeepSeek；返回时间落在独立主机时间窗口内；回答基于 README 拒绝了错误评审意见。本批不追加第二种 MCP 或扩展权限。

## Task 1：安装和绑定独立能力

- [x] 将 `/Users/wujingyu/.codex/skills/receiving-code-review` 安装到默认 State Store，记录返回的固定版本和包哈希。
- [x] 生成只允许 `get_current_time` 的 `official-time` 配置；command 使用绝对 `uvx`，固定 `mcp-server-time==2026.8.18`，cwd 固定为 `local-agent/`，不传环境变量。
- [x] 导入 `superpowers-time-demo` Agent；工具只允许 `read_file`、`list_files`、`session_history`，不允许写入。
- [x] 将 Skill 与 MCP 的固定版本绑定到该 Agent；重复列出配置并成功 probe。

## Task 2：无模型协议探测

- [x] 运行 `capabilities probe official-time`，确认 `get_current_time` 启用，`convert_time` 被发现但禁用。
- [x] 直接调用适配器一次，记录 MCP receipt 与返回时间；与主机上海时间差 `0.578282` 秒。
- [x] 新建持久 Session；已有 Session 不复用，因为能力绑定随 Session 快照冻结。

## Task 3：真实模型闭环

- [x] 在已持有 `DEEPSEEK_API_KEY` 的终端执行 `--debug-content` Run：显式选择 `receiving-code-review`，核对 README 中 Remote MCP/OAuth 的错误评审意见，并调用官方 Time MCP 给出复核时间。首轮答案格式失败后只做一次受限格式重试。
- [x] 不在聊天、配置或 trace 中写入密钥；本次公开 Skill、公开 README 与当前时间允许保存全文 trace。

## Task 4：后台证据与 Gate

- [x] 核对 JSONL 事件顺序：真实 `run.started`、`read_skill` 与 MCP tool request/completion、带同一 ToolCall ID 的后续 `model.requested`、`run.ended`。
- [x] 核对 SQLite：成功调用为 `succeeded`，首次越权顺序读取如实为 `failed`；MCP payload 含真实时间和 receipt；后续 context manifest 含相同调用 ID；最终结果为 `ANSWER_VALIDATED` 且 `provider.simulated=false`。
- [x] 人工核对回答确实先查证再拒绝错误意见，并正确复述 MCP 时间；结论写入 `local-agent/trial/superpowers-mcp-live-check.md`，不覆盖原始 trace，不冒充用户签字。
- [x] 更新本计划 §0；Gate PASS 后停止，不扩展第二个 Skill/MCP。
