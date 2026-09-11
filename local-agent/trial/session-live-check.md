# 会话管理固定验收记录

- 更新日期：2026-09-11
- 真实验收代码基线：`9b04514`
- 当前实现代码基线：`5381c9d`
- 离线 Gate：**PASS**
- 真实 DeepSeek 证据：**PARTIAL（5 个目标场景有成功证据；更正回答修复后未真实复跑）**

本文件是会话管理模块 S1–S13 的权威验收入口。当前代码、SQLite、模拟 Provider 和本地 fixture 的结论来自离线证据；真实 DeepSeek 结论来自三份未覆盖的机器报告。AI 内容复核不冒充用户体验签字，也不能把离线修复写成真实通过。

## 离线回归

当前实现完成搜索续页、完整返回信封分页和 S6 长会话补证后，在 `local-agent/` 运行完整 Python 回归。三组浏览器证据来自 `af3ab19`，本次没有改页面、HTTP 合同或模型提示，按止损规则复用，不重复运行。

| 检查 | 命令 | 实际结果 | 模型边界 |
|---|---|---|---|
| 完整 Python 回归 | `python3 -W error::ResourceWarning -m unittest discover -s tests` | **PASS，387 项，13.930s** | 测试替身或本地模拟 Provider；不调用 DeepSeek |
| 工具页面脚本 | `node tests/browser_tools.cjs` | **历史 PASS**：预览、允许/拒绝、重复决定、取消、刷新恢复、产物回执、过期与旧会话保护 | 合成 DOM；本次未重跑 |
| 会话浏览器流程 | 通过 `with_server.py` 启动 `tests/web_fixture.py --port 8767` 后运行 `node tests/browser_sessions.cjs` | **历史 PASS**：建会话、两轮、刷新、历史对话、A/B 切换、迟到响应、审批、归档恢复及窄屏 | loopback + `BrowserProvider`；本次未重跑 |
| 原页面浏览器回归 | 通过 `with_server.py` 启动 8767 fixture 和 8768 missing-key fixture 后运行 `node tests/browser_web.cjs` | **历史 PASS**：目录、引用、错误、取消、权限、审批、产物与五种视口 | loopback + fixture Provider；本次未重跑 |

浏览器脚本的完整可复制命令见[会话管理实施计划 Task 11](../../docs/superpowers/plans/2026-09-10-session-management.md)。

## 最终实现级 Review Gate

首次实现核对发现两项 P1：历史工具结果没有自己的可搜索、可分页视图；同一 assistant 消息包含多个 ToolCall 时，按共用消息 ID 读取会固定返回第一项。对应修复已保留稳定消息视图、调用与结果链接，并让大结果使用独立结果消息分页。

2026-09-11 的再核对发现三项交付缺口：搜索超过 8 个命中无法续页；原始正文虽小于 8 KiB，JSON 转义和策略附加字段仍可令最终信封超过 12 KiB；S6 没有覆盖“旧约定→更正→未完事项→压缩后追问”。当前实现新增绑定 action／Session／截止序号／query 的签名搜索游标，按最终 Tool Runtime 信封收缩 search/read 页面，并在固定字段本身超限时返回有界错误；固定 50 轮用例同时保留更正、旧约定、未完事项及可回查原文。

定向规格 Gate 与代码质量 Gate 均为 **PASS，无剩余 P0–P2**。定向检查为历史查询 11 项、Session Runtime 14 项、Context 8 项；完整 Python 387 项通过。本轮未重跑浏览器或 DeepSeek。

## S1–S13 证据矩阵

| 场景 | 离线状态 | 主要证据 | 已证明的行为 |
|---|---|---|---|
| S1 | PASS | [`SessionRuntimeTests.test_session_service_runs_file_then_conversation_and_survives_reopen`](../tests/test_session_runtime.py)、[`SessionRuntimeTests.test_runtime_persists_each_request_reply_and_tool_result_in_order`](../tests/test_session_runtime.py)、[`browser_sessions.cjs`](../tests/browser_sessions.cjs) | 同一 Session 完成两轮后关闭并重开 Store；第三轮实际请求含前轮原文；用户、助手和工具消息及原调用 ID 顺序保留。 |
| S2 | PASS | [`SessionServiceTests.test_run_and_message_reads_are_scoped_to_session`](../tests/test_sessions.py)、[`SessionHistoryTests.test_cutoff_and_session_boundary_hide_later_or_foreign_messages`](../tests/test_session_history.py)、[`SessionHistoryTests.test_search_cursor_rejects_changed_query_cutoff_session_tampering_and_read`](../tests/test_session_history.py)、[`SessionWebTests.test_cross_session_run_and_controls_are_all_not_found`](../tests/test_session_web.py)、[`browser_sessions.cjs`](../tests/browser_sessions.cjs) | A/B 的 Run、消息、摘要、历史游标和控制接口隔离；搜索／读取游标跨 action、查询、会话、截止点复用或篡改均被拒绝；迟到响应不覆盖当前会话。 |
| S3 | PASS | [`SessionServiceTests.test_submit_is_atomic_idempotent_and_uses_complete_fingerprint`](../tests/test_sessions.py)、[`SessionRuntimeTests.test_idempotent_submit_never_executes_provider_twice`](../tests/test_session_runtime.py)、[`SessionWebTests.test_run_is_durable_idempotent_and_conflicting_replay_is_409`](../tests/test_session_web.py)、[`SessionServiceTests.test_continue_interrupted_creates_new_clean_run_and_is_idempotent`](../tests/test_sessions.py) | 同一完整提交只产生一份用户消息和一个 Run，重复不会再次执行 Provider；执行语义不同明确冲突。 |
| S4 | PASS | [`ContextBuilderTests.test_recent_context_contains_original_correction_with_ids`](../tests/test_context.py)、[`ConversationPolicyTests.test_current_user_message_id_is_visible_and_can_be_referenced`](../tests/test_conversation.py)、[`SessionRuntimeTests.test_conversation_repairs_one_out_of_range_reference`](../tests/test_session_runtime.py)、[`browser_sessions.cjs`](../tests/browser_sessions.cjs) | 原约定与后续更正进入 Context；当前更正和历史消息均可引用；错误范围被拒绝且最多修复一次。真实运行证明更正已持久化并被后续报告采用，但更正回答本身在修复后没有真实复跑。 |
| S5 | PASS | [`SessionRuntimeTests.test_each_file_run_re_reads_changed_source`](../tests/test_session_runtime.py)、[`ContextBuilderTests.test_history_tool_chain_is_data_and_current_chain_stays_native`](../tests/test_context.py)、[`SessionRuntimeTests.test_continue_creates_a_new_run_without_inheriting_output`](../tests/test_session_runtime.py) | 新资料 Run 重新读取并得到修改后的 B；同一会话仍可引用第一次回答中的 A；历史调用只作为数据投影，旧输出目标和执行许可不继承。 |
| S6 | PASS | [`ContextBuilderTests.test_fifty_runs_create_a_bounded_traceable_summary_and_keep_raw_history`](../tests/test_context.py)、[`ContextBuilderTests.test_summary_keeps_correction_pending_item_and_original_history_for_follow_up`](../tests/test_context.py)、[`SessionHistoryTests.test_search_pages_all_matches_in_stable_order_without_duplicates`](../tests/test_session_history.py)、[`SessionHistoryTests.test_read_pages_by_actual_wire_size_and_preserves_unicode_exactly`](../tests/test_session_history.py)、[`SessionHistoryTests.test_search_and_read_preserve_original_call_arguments_and_result_link`](../tests/test_session_history.py) | 50 轮原始历史超过 128 KiB，输入与摘要有界；压缩后的新旧约定顺序、未完事项和原文 ID 均保留。搜索 17 个命中可按 8＋8＋1 续页，转义密集正文逐页拼回一致，旧 ToolCall 参数、意图和结果关联可查且不会重放。 |
| S7 | PASS | [`ContextBuilderTests.test_invalid_summary_variants_leave_old_summary_and_raw_messages_unchanged`](../tests/test_context.py)、[`SessionRuntimeTests.test_failed_summary_is_attempted_once_then_history_tool_can_recover_detail`](../tests/test_session_runtime.py)、[`ContextBuilderTests.test_current_chain_over_limit_fails_without_truncation`](../tests/test_context.py) | 摘要超限、非法 JSON、错误消息 ID、摘录不符或异常时不覆盖旧摘要和原文；每个 Run 最多尝试一次摘要，然后有限降级或明确 `CONTEXT_LIMIT`。 |
| S8 | PASS | [`SessionRecoveryTests.test_sigkill_submission_model_and_read_stages_recover_without_replay`](../tests/test_session_recovery.py)、[`SessionServiceTests.test_continue_interrupted_creates_new_clean_run_and_is_idempotent`](../tests/test_sessions.py) | 在三个真实子进程暂停点执行 `SIGKILL`；旧 Run 归类为 interrupted，重开不自动调用 Provider/工具，继续创建新 Run。 |
| S9 | PASS | [`SessionRecoveryTests.test_sigkill_invalidates_old_approvals_and_new_run_requires_new_preview`](../tests/test_session_recovery.py)、[`ApprovalJournalTests.test_required_and_decision_are_durable_before_they_become_visible`](../tests/test_session_runtime.py)、[`browser_sessions.cjs`](../tests/browser_sessions.cjs) | 旧审批过期且只可查看；旧 ID 不能执行；新 Run 重新校验参数并产生新预览。 |
| S10 | PASS | [`SessionRecoveryTests.test_sigkill_classifies_publication_windows_without_inventing_receipts`](../tests/test_session_recovery.py)、[`SessionRecoveryTests.test_recovery_classifies_inflight_runs_and_preserves_receipt`](../tests/test_session_recovery.py)、[`SessionRecoveryTests.test_unknown_target_is_locked_across_sessions_and_inspection_is_read_only`](../tests/test_session_recovery.py)、[`SessionRuntimeTests.test_publication_intent_store_failure_prevents_file_creation`](../tests/test_session_runtime.py)、[`SessionRuntimeTests.test_receipt_store_failure_reports_unknown_and_keeps_actual_receipt`](../tests/test_session_runtime.py) | 发布三窗口分别归类为未执行、未知、已确认；未知目标不自动重写、不伪造成功，已确认 Artifact 保留。 |
| S11 | PASS | [`SessionRecoveryTests.test_sqlite_full_and_read_only_writes_fail_atomically`](../tests/test_session_recovery.py)、[`SessionStoreTests.test_newer_schema_and_corrupt_database_stop_explicitly`](../tests/test_session_store.py)、[`SessionRecoveryTests.test_second_owner_is_rejected_by_store_cli_and_web_without_corruption`](../tests/test_session_recovery.py)、[`SessionRuntimeTests.test_journal_failure_stops_before_provider_call`](../tests/test_session_runtime.py) | SQLite 满、只读、损坏、版本不兼容、创建失败和双所有者均明确失败；事务不留下半份记录，不启动后续副作用。 |
| S12 | PASS | [`SessionWebTests.test_completed_history_survives_more_than_twenty_runs`](../tests/test_session_web.py)、[`SessionServiceTests.test_create_load_rename_archive_and_restore`](../tests/test_sessions.py)、[`SessionRecoveryTests.test_live_wal_backup_contains_relations_without_copying_artifact`](../tests/test_session_recovery.py)、[`SessionStoreTests.test_wal_backup_uses_sqlite_backup_and_is_readable`](../tests/test_session_store.py) | 第 21 个 Run 不淘汰；归档/恢复有效；WAL 状态下一致备份可读且关系完整；原 Artifact 不移动。 |
| S13 | PASS | 当前完整 Python 387 项；三组历史浏览器证据；三份真实机器报告及语义复核 | 当前代码没有回退原工具验真、权限、拒绝、超时、取消、原 ID 回填和一次 publish。真实证据覆盖五个目标场景；更正回答修复后的精确真实复跑仍未执行。 |

## 真实 DeepSeek 证据边界

状态：**PARTIAL**。三次受限运行累计 6 次用户提交、18 次模型请求；Usage 记录 18/18，总计 prompt 138,924、completion 4,149、合计 143,073 tokens。所有请求不超过 64 KiB，单次提交不超过 6 次模型请求，总量未超过 6/36 上限。

- [`session-live-6bcef73c3698419dbdad28d637012b99/report.json`](results/session-live-6bcef73c3698419dbdad28d637012b99/report.json)：初始资料通过；当前更正因错误引用范围以 `INVALID_REFERENCE` 停止。
- [`session-live-7e76dd89476e451f8b272d4fbe930221/report.json`](results/session-live-7e76dd89476e451f8b272d4fbe930221/report.json)：确认更正已持久化；重启重新取证、会话标题继承、报告发布和早期要求回查通过；v1 摘要未保存。
- [`session-live-94fdc1ae545946b69fc018f52aa763e5/report.json`](results/session-live-94fdc1ae545946b69fc018f52aa763e5/report.json)：v2 摘要及旧 ToolCall 参数、intent、错误和 Run/Call/Result 关联通过；机器 Gate 为 `PENDING_SEMANTIC_REVIEW`。

最终目录的 [`semantic-review.md`](results/session-live-94fdc1ae545946b69fc018f52aa763e5/semantic-review.md)支持初始资料、重启重新取证、报告发布、早期要求回查以及 v2 摘要/旧调用回查五个目标场景。其“六个目标场景均通过”结论把更正回答的离线修复当成了真实成功，本记录在证据复核后予以更正。确认后的 `project-review.md` 为 346 字节，SHA-256 为 `957311e485b90b95618daa72a7fa766a356395e410dbfadab45a9631172f29df`，与磁盘内容一致；数据库 `PRAGMA integrity_check` 为 `ok`。

Task 12 没有调用 DeepSeek。搜索续页、完整信封分页和“旧约定→更正→未完事项→压缩后追问”现有当前代码的固定离线证据；它们不冒充新版本真实模型证据。若以后要求“真实 DeepSeek 全量验收通过”，只需针对更正回答做一次受限复跑，不重跑其他五个已证明场景。

已知 P2：真实 v2 摘要曾把一条测试占位记录归入 `goals`，但内容可追溯，最早交付要求已保存在 `constraints`，摘要也不作为答案证据或权限。该质量项不扩展本轮范围。
