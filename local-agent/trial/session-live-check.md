# 会话管理固定验收记录

- 更新日期：2026-09-11
- 真实验收代码基线：`9b04514`
- 最终实现代码基线：`af3ab19`
- 离线 Gate：**PASS**
- 真实 DeepSeek：**PASS（自动检查与 AI 语义复核均通过）**

本文件是会话管理模块 S1–S13 的权威验收入口。离线证据证明本地代码、SQLite、模拟 Provider 和本地浏览器 fixture 的行为；真实 DeepSeek 证据来自三份未覆盖的机器报告及独立语义复核。AI 内容复核不冒充用户体验签字。

## 离线回归

离线命令均在 `local-agent/` 下执行。引用修复和摘要 v2 落地后重新运行完整 Python；会话浏览器流程也在同一产品代码上重跑。工具页面及原页面脚本未受此次会话策略修复影响，按工作区止损规则复用已通过证据。

| 检查 | 命令 | 实际结果 | 模型边界 |
|---|---|---|---|
| 完整 Python 回归 | `python3 -W error::ResourceWarning -m unittest discover -s tests` | **PASS，381 项，13.399s** | 测试替身或本地模拟 Provider；不调用 DeepSeek |
| 工具页面脚本 | `node tests/browser_tools.cjs` | **PASS**：预览、允许/拒绝、重复决定、取消、刷新恢复、产物回执、过期与旧会话保护 | 合成 DOM；不调用模型 |
| 会话浏览器流程 | 通过 `with_server.py` 启动 `tests/web_fixture.py --port 8767` 后运行 `node tests/browser_sessions.cjs` | **PASS**：建会话、两轮、刷新、历史对话、A/B 切换、迟到响应、审批、归档恢复及窄屏 | loopback + `BrowserProvider`；不调用 DeepSeek |
| 原页面浏览器回归 | 通过 `with_server.py` 启动 8767 fixture 和 8768 missing-key fixture 后运行 `node tests/browser_web.cjs` | **PASS**：目录、引用、错误、取消、权限、审批、产物与五种视口 | loopback + fixture Provider；不调用 DeepSeek |

浏览器脚本使用的 Node/Playwright 运行时为工作区既有依赖；完整可复制命令见[会话管理实施计划 Task 11](../../docs/superpowers/plans/2026-09-10-session-management.md)。

## 最终实现级 Review Gate

首次实现核对发现两项 P1：历史工具结果没有自己的可搜索、可分页视图；同一 assistant 消息包含多个 ToolCall 时，按共用消息 ID 读取会固定返回第一项。两项均违反设计 §8.3 的稳定 `text_view` 与调用/结果双向回查要求。

修复后，每条真实消息只对应一个稳定视图；多个 ToolCall 在同一调用消息中按原顺序投影，并保留各自的 call/result 链接；已验证工具结果使用自己的消息 ID 搜索和分页，大结果不再挤入调用页。新增两个针对原故障的回归测试，随后历史查询 7 项、Context 7 项、Runtime 13 项、完整 Python 381 项及会话浏览器回归均通过。定向复核结论为 **PASS，无剩余 P0/P1**。该修复不改变模型提示、权限、真实验收场景或 Provider，因此没有重复 DeepSeek 调用。

## S1–S13 证据矩阵

| 场景 | 离线状态 | 主要证据 | 已证明的行为 |
|---|---|---|---|
| S1 | PASS | [`SessionRuntimeTests.test_session_service_runs_file_then_conversation_and_survives_reopen`](../tests/test_session_runtime.py)、[`SessionRuntimeTests.test_runtime_persists_each_request_reply_and_tool_result_in_order`](../tests/test_session_runtime.py)、[`browser_sessions.cjs`](../tests/browser_sessions.cjs) | 同一 Session 完成两轮后关闭并重开 Store；第三轮实际请求含前轮原文；用户、助手和工具消息及原调用 ID 顺序保留。 |
| S2 | PASS | [`SessionServiceTests.test_run_and_message_reads_are_scoped_to_session`](../tests/test_sessions.py)、[`SessionHistoryTests.test_cutoff_and_session_boundary_hide_later_or_foreign_messages`](../tests/test_session_history.py)、[`SessionHistoryTests.test_read_cursor_is_bound_to_session_cutoff_message_and_offset`](../tests/test_session_history.py)、[`ContextBuilderTests.test_fifty_runs_create_a_bounded_traceable_summary_and_keep_raw_history`](../tests/test_context.py)、[`SessionWebTests.test_cross_session_run_and_controls_are_all_not_found`](../tests/test_session_web.py)、[`browser_sessions.cjs`](../tests/browser_sessions.cjs) | A/B 的 Run、消息、摘要、历史游标和控制接口隔离；跨会话或篡改游标不可读；切换后迟到响应不覆盖当前会话。 |
| S3 | PASS | [`SessionServiceTests.test_submit_is_atomic_idempotent_and_uses_complete_fingerprint`](../tests/test_sessions.py)、[`SessionRuntimeTests.test_idempotent_submit_never_executes_provider_twice`](../tests/test_session_runtime.py)、[`SessionWebTests.test_run_is_durable_idempotent_and_conflicting_replay_is_409`](../tests/test_session_web.py)、[`SessionServiceTests.test_continue_interrupted_creates_new_clean_run_and_is_idempotent`](../tests/test_sessions.py) | 同一完整提交只产生一份用户消息和一个 Run，重复不会再次执行 Provider；问题、任务类型、scope、输出目标、父 Run 或执行选项变化都会冲突。 |
| S4 | PASS | [`ContextBuilderTests.test_recent_context_contains_original_correction_with_ids`](../tests/test_context.py)、[`ConversationPolicyTests.test_current_user_message_id_is_visible_and_can_be_referenced`](../tests/test_conversation.py)、[`SessionRuntimeTests.test_conversation_repairs_one_out_of_range_reference`](../tests/test_session_runtime.py)、[`browser_sessions.cjs`](../tests/browser_sessions.cjs) | 原约定与后续更正进入 Context；当前更正和历史消息均可引用；错误范围被拒绝且最多修复一次。真实报告在重启后采用更正标题，原始引用偏移失败另行保留。 |
| S5 | PASS | [`SessionRuntimeTests.test_each_file_run_re_reads_changed_source`](../tests/test_session_runtime.py)、[`ContextBuilderTests.test_history_tool_chain_is_data_and_current_chain_stays_native`](../tests/test_context.py)、[`SessionRuntimeTests.test_continue_creates_a_new_run_without_inheriting_output`](../tests/test_session_runtime.py) | 新资料 Run 重新读取并得到修改后的 B；同一会话仍可引用第一次回答中的 A；历史调用只作为数据投影，旧输出目标和执行许可不继承。 |
| S6 | PASS | [`ContextBuilderTests.test_fifty_runs_create_a_bounded_traceable_summary_and_keep_raw_history`](../tests/test_context.py)、[`SessionHistoryTests.test_search_and_read_preserve_original_call_arguments_and_result_link`](../tests/test_session_history.py)、[`SessionHistoryTests.test_read_cursor_is_bound_to_session_cutoff_message_and_offset`](../tests/test_session_history.py)、[`ContextBuilderTests.test_current_native_tool_chain_is_never_split_when_history_is_removed`](../tests/test_context.py) | 50 轮原始历史超过 128 KiB，模型请求不超过 64 KiB、摘要不超过 6 KiB；原始历史保留。只存在旧 ToolCall 参数中的路径退出近期 Context 后仍能按 `source_run_id/call_id/result_message_id` 回查，且没有执行旧工具；当前原生工具链不拆分。 |
| S7 | PASS | [`ContextBuilderTests.test_invalid_summary_variants_leave_old_summary_and_raw_messages_unchanged`](../tests/test_context.py)、[`SessionRuntimeTests.test_failed_summary_is_attempted_once_then_history_tool_can_recover_detail`](../tests/test_session_runtime.py)、[`ContextBuilderTests.test_current_chain_over_limit_fails_without_truncation`](../tests/test_context.py) | 摘要超限、非法 JSON、错误消息 ID、摘录不符或异常时不覆盖旧摘要和原文；每个 Run 最多尝试一次摘要，然后有限降级或明确 `CONTEXT_LIMIT`。 |
| S8 | PASS | [`SessionRecoveryTests.test_sigkill_submission_model_and_read_stages_recover_without_replay`](../tests/test_session_recovery.py)、[`SessionServiceTests.test_continue_interrupted_creates_new_clean_run_and_is_idempotent`](../tests/test_sessions.py) | 在输入提交后、模型处理中、读取开始后三个真实子进程暂停点执行 `SIGKILL`；旧 Run 归类为 interrupted，重开不自动调用 Provider/工具，继续会创建新 Run。 |
| S9 | PASS | [`SessionRecoveryTests.test_sigkill_invalidates_old_approvals_and_new_run_requires_new_preview`](../tests/test_session_recovery.py)、[`ApprovalJournalTests.test_required_and_decision_are_durable_before_they_become_visible`](../tests/test_session_runtime.py)、[`browser_sessions.cjs`](../tests/browser_sessions.cjs) | 审批等待或已允许未发布时强杀后，旧审批过期且仅可查看；旧 ID 不能执行；新 Run 使用新 run/call、重新校验参数并产生新预览。 |
| S10 | PASS | [`SessionRecoveryTests.test_sigkill_classifies_publication_windows_without_inventing_receipts`](../tests/test_session_recovery.py)、[`SessionRecoveryTests.test_recovery_classifies_inflight_runs_and_preserves_receipt`](../tests/test_session_recovery.py)、[`SessionRecoveryTests.test_unknown_target_is_locked_across_sessions_and_inspection_is_read_only`](../tests/test_session_recovery.py)、[`SessionRuntimeTests.test_publication_intent_store_failure_prevents_file_creation`](../tests/test_session_runtime.py)、[`SessionRuntimeTests.test_receipt_store_failure_reports_unknown_and_keeps_actual_receipt`](../tests/test_session_runtime.py) | 发布意图前、意图后至回执前、回执后三窗口分别归类为未执行、未知、已确认；未知目标不自动重写、不伪造成功，已确认 Artifact 保留。 |
| S11 | PASS | [`SessionRecoveryTests.test_sqlite_full_and_read_only_writes_fail_atomically`](../tests/test_session_recovery.py)、[`SessionStoreTests.test_newer_schema_and_corrupt_database_stop_explicitly`](../tests/test_session_store.py)、[`SessionRecoveryTests.test_second_owner_is_rejected_by_store_cli_and_web_without_corruption`](../tests/test_session_recovery.py)、[`SessionRuntimeTests.test_journal_failure_stops_before_provider_call`](../tests/test_session_runtime.py)、[`SessionStoreTests.test_database_creation_error_is_mapped_and_releases_lock`](../tests/test_session_store.py) | SQLite 满、只读、损坏、版本不兼容、创建失败和跨进程双所有者均明确失败；事务不留下半份 Session/Run/Message，不启动后续 Provider/副作用，库完整性保持。 |
| S12 | PASS | [`SessionWebTests.test_completed_history_survives_more_than_twenty_runs`](../tests/test_session_web.py)、[`SessionServiceTests.test_create_load_rename_archive_and_restore`](../tests/test_sessions.py)、[`SessionRecoveryTests.test_live_wal_backup_contains_relations_without_copying_artifact`](../tests/test_session_recovery.py)、[`SessionStoreTests.test_wal_backup_uses_sqlite_backup_and_is_readable`](../tests/test_session_store.py) | 第 21 个 Run 不淘汰；归档/恢复有效；WAL 状态下一致备份可读且关系完整；原 Artifact 不移动。 |
| S13 | PASS | 完整 Python 381 项；[`browser_tools.cjs`](../tests/browser_tools.cjs)、[`browser_sessions.cjs`](../tests/browser_sessions.cjs)、[`browser_web.cjs`](../tests/browser_web.cjs)；三份真实机器报告及最终语义复核 | 原工具验真、权限、拒绝、超时、取消、原 ID 回填和一次 publish 未回退；真实连续演示覆盖重启、更正、重新取证、报告发布、长历史摘要和旧调用回查。 |

## 真实 DeepSeek 验收

状态：**PASS（自动检查通过，AI 语义复核通过）**。

验收入口为 [`run-session-live-check.py`](run-session-live-check.py)。第一次运行在当前更正引用处发现 DeepSeek 将 68 字符原文的 `end` 写成 73，程序正确拒绝；第二次续跑在真实摘要处发现同类中文范围偏移及非法工具链锚点。两项缺口分别以一次有界引用修复、程序预计算的合法摘要 `reference_spans` 修复，并只续跑受影响的剩余场景。三份原始报告均保留：

- [`session-live-6bcef73c3698419dbdad28d637012b99/report.json`](results/session-live-6bcef73c3698419dbdad28d637012b99/report.json)：初始资料通过；当前更正因错误范围停止。
- [`session-live-7e76dd89476e451f8b272d4fbe930221/report.json`](results/session-live-7e76dd89476e451f8b272d4fbe930221/report.json)：重启重新取证、会话标题继承、报告发布和早期要求回查通过；v1 摘要未保存。
- [`session-live-94fdc1ae545946b69fc018f52aa763e5/report.json`](results/session-live-94fdc1ae545946b69fc018f52aa763e5/report.json)：v2 摘要、旧 ToolCall 参数/intent/错误与 Run/Call/Result 关联全部通过；机器 Gate 为 `PENDING_SEMANTIC_REVIEW`，没有自动冒充人工结论。

累计实际用量：6 次用户提交、18 次模型请求；Usage 记录 18/18，总计 prompt 138,924、completion 4,149、合计 143,073 tokens。所有请求不超过 64 KiB，单次提交不超过 6 次模型请求，总量未超过 6/36 上限。

语义复核见最终结果目录的 [`semantic-review.md`](results/session-live-94fdc1ae545946b69fc018f52aa763e5/semantic-review.md)。六个目标场景均通过；确认后的 `project-review.md` 为 346 字节，SHA-256 为 `957311e485b90b95618daa72a7fa766a356395e410dbfadab45a9631172f29df`，与磁盘内容一致。活动摘要版本为 `session-summary-v2`，数据库 `PRAGMA integrity_check` 为 `ok`。

已知 P2：真实摘要把一条测试占位记录归入 `goals`，但该内容是可追溯原文，最早交付要求已正确保存在 `constraints`，且摘要不作为答案证据或权限。本批按固定停止条件收口，不扩展为摘要文案调优或更多模型统计。
