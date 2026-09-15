# 助手配置、Skills 与 MCP 联合验收记录

2026-09-15，本批固定真实组 **R1–R6 机器检查全部 PASS，AI 语义 Gate PASS**。完整 Python 回归 464 项通过；真实 DeepSeek 请求累计 50/60 次，剩余 10 次，已按停止条件停止追加调用。

## 已证明的联合闭环

| 场景 | 结论 | 已核实行为 |
| --- | --- | --- |
| R1 首次联合任务 | PASS | 配置化助手读取 Skill 主文与引用、本地文件、MCP Tool/Resource/Prompt；引用逐字匹配；合成批准后创建文件，审批预览、回执与磁盘文件哈希一致。 |
| R2 重启与更正 | PASS | 同一会话重启后保留旧约定与更正，重新读取当前文件和 MCP 状态，不重复写入。 |
| R3 压缩后追问 | PASS | 摘要实际保存并进入后续 Context，保留旧日期 `2026-09-20`、新日期 `2026-09-22`、联调验证与验收记录；历史搜索和当前能力均可继续使用。 |
| R4 第二套能力 | PASS | 另一助手读取第二份 Skill，调用独立 `official-time` MCP；时区参数、返回星期及 MCP 引用均正确。 |
| R5 MCP 错误 | PASS | `MCP_TOOL_ERROR` 按原调用进入下一轮；助手以 `unable` 收口，不拿其他项目替代，不虚报成功。 |
| R6 写入被拒绝 | PASS | `USER_REJECTED` 恰好回填一次，拒绝后不再调用写工具；最终 `unable/USER_REJECTED`，文件不存在且无 Artifact。 |

R1 的 `brief-approved.md` 为 726 bytes；写入参数、审批预览、Artifact 回执和磁盘文件 SHA256 均为 `8a7bcde853d9aca912019551235d8bc201c1e9f68575869174f6c42f5ddb37c6`。R3 活动摘要 ID 为 `793149481d24410c9e107be021f11108`，已在同一 Run 后续请求中复用。R6 的 `brief-denied.md` 未落盘。

## 证据与边界

原始报告在本机 `trial/results/extensions-acceptance-20260915T030927702169Z/`，没有覆盖此前七次失败或部分通过报告。最终报告仍保留 `semantic_review=pending`、`user_acceptance=pending` 和 `release_ready=false`；后置 AI 复核另存为该目录的 `semantic-review.md`，不改写机器原件，也不冒充用户签收。

模型为真实 DeepSeek，MCP 为实际 stdio 进程；项目数据、时间服务和审批决定来自固定合成夹具。审批记录明确为 `human_approval=false`，只证明允许/拒绝、落盘和回执协议。

一个非阻断观察：R5 顶层 `stop_reason=FILE_UNAVAILABLE` 比工具层的 `MCP_TOOL_ERROR` 更宽泛，但精确错误完整保留在 trace、工具记录和答案中。

本记录支持“配置化多助手、Skills、受限 stdio MCP、持久会话和工具层在同一 Runtime 中形成联合闭环”。这里的多助手是可选择的多个配置对象，不是自动派发、并行协作或子 Agent 编排。本批也不覆盖 Memory、Cron、远程 MCP/OAuth、多 Provider、任意第三方 Server 兼容性或生产发布质量。
