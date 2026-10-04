# 飞书首批接入验收记录

## 当前结论（2026-10-03）

**代码、固定离线验证及真实飞书首批验收均 PASS。** 飞书应用已发布并完成本人私聊绑定；本人私聊只读问答、本机审批写入、真实 Artifact、状态回传，以及待审批显式取消且不落盘/不重做均有原始证据。本批按既定范围收口。

本批范围：本人私聊文字、固定资料会话、现有工具与审批、状态回传、显式取消；附件、撤回自动取消、手机访问后置。

首次配置历史：旧说明的首装 ID 循环已补正为随机码私聊监听 + 本机确认；`test_feishu_prepare.py` 5 项 / 0.265 秒通过，覆盖不匹配/群聊/错误主体拒绝及确认后私有落盘。之后已完成真实应用发布、消息权限、`im.message.receive_v1` 长连接订阅、本人绑定和正式服务启动。

## 真实只读闭环（2026-10-03）

飞书消息 `om_x100b64c93dc770a0c2b0e04f8d90f08` 对应事件 `6b9bf3775129863513955777c4d9a66568d27c482edafa9243694186151ac887` 和唯一 Run `e408065a4fd84f748952ccb5591371d8`。Run 使用真实 `deepseek-v4-flash`，`simulated=false`、`proxy_mode=direct`，3 次模型请求后以 `completed / ANSWER_VALIDATED` 结束。

模型先列根目录，再列 `output` 并读取 `01-weekly.md`、`02-actions.md`；4 个工具调用全部 `succeeded`，每项都有 `result_message_id`，证据导出均标记 `returned_to_model=true`。最终答案正确区分广州和深圳的营业供给、自然/商业流量、进店下单和首屏套餐，明确费率、资源投放和审批记录未提供，并将 49 元套餐标为过期纪要；两处引用分别覆盖周报第 4–14 行和行动纪要第 6–12 行，人工对照一致。任务未请求写入，登记产物为 0。

飞书 outbox 中 `received`、`running`、`terminal` 三类通知均为 `accepted`、`attempts=1`，客户端实际显示“已收到”“执行中”和带同一 Run ID 的“已完成”本机链接。`accepted` 只证明平台接收，不证明用户已读。原始依据保存在 State Store、`channel-events.jsonl` 及 Run trace；`.venv/bin/python trial/inspect-feishu.py` 可只读重放关联证据。

## 真实审批写入闭环（2026-10-03）

第一次写入 Run `749b32a9515a429e9622b53fb2eb5db4` 在等待本机审批后以 `APPROVAL_EXPIRED` 停止，目标文件不存在、登记产物为 0；该失败原样保留。用户重新发送同一任务后，平台事件 `31a8364da87ffd82adcccbdf55474776c3b4a5fd679cd89e8afb85c2b4fad359` 创建唯一 Run `0d1484da4c154c889d16826b34bcab37`。Run 使用真实 `deepseek-v4-flash`，`simulated=false`、`proxy_mode=direct`，4 次模型请求后以 `completed / ANSWER_VALIDATED` 结束。

模型执行 2 次 `list_files`、2 次 `read_file` 和 1 次 `write_file`，全部 `succeeded` 且各有 `result_message_id`，写入回执进入下一轮模型上下文。审批 `64564c45f88445fbad22eea757b7c4b8` 明确允许冻结目标 `output/gzsz-brief-1003.md`；批准预览、Artifact `0b575f9d805f433a8cca06692cf8ecf4` 与实际文件均为 3623 bytes，SHA-256 均为 `01e19ccd4f3e8d97639855d77cda60f13a6ea32ffd1b06929d465a67e9df59d8`，实际权限为 `0600`，Artifact 恢复状态为 `confirmed`。这证明模型没有改名、一次审批只发布了批准内容、写入结果确实回到上下文。

该 Run 的终态通知首次发送为 `unknown / DELIVERY_UNKNOWN`。使用现有“仅补发通知”接口对 outbox `c6b163b5dd6b46f8b79182733ff0e3f4` 有界重试一次后变为 `accepted`、`attempts=2`，长连接保持 `connected`；飞书客户端实际显示同一 Run ID 的“任务状态：已完成；已登记产物 1 份”。本次补发只处理 outbox，没有新建 Run、重新调用模型或再次写文件。

## 真实待审批取消闭环（2026-10-03）

飞书报告任务创建 Run `adddf978697944948061448af496631e`，真实 DeepSeek 执行 4 次模型请求，完成目录列举和资料读取后请求唯一目标 `output/gzsz-cancel-1003.md` 的 `write_file`，随后停在 `waiting_approval`。本人从同一飞书私聊发送 `/取消 adddf978697944948061448af496631e`；渠道事件解析为 `action=cancel` 并被处理，Run 最终为 `cancelled / CANCELLED`。

取消后 `write_file` 为 `failed` 且有结果消息，未执行发布；Artifact 列表为空，目标文件在稳定性复查后仍不存在，模型请求保持 4 次，没有取消后再次调用或新建 Run。持久 Run 快照返回 `pending_approval=null`，工作台不再提供审批动作；SQLite 的 approvals 表仍保留原 `pending` 行作为审计历史，不代表审批仍可执行。`received`、`running`、`waiting_approval`、`terminal` 四类通知均一次被飞书接受，客户端实际显示同一 Run ID 的“已请求取消”和“任务状态：已取消；已登记产物 0 份”。

## 已取得证据

| 项目 | 结果与证据 | 实际说明 |
|---|---|---|
| 官方 SDK | `lark-oapi==1.7.3` 已安装；11 项传输测试通过 | 真实 SDK 构建器与 ACK 200/500 已在禁止网络的测试中核对；不代表真实连接 |
| 渠道台账 | `test_channel_store.py` 20 项通过 | 重投与新消息分开，变更绑定失效，通知独立记录，迁移与私有备份有效 |
| 共享执行 | `test_feishu_channel.py` 16 项通过 | 实际 Runtime/SQLite/文件工具执行；脚本模型、合成事件及通知替身；无真实平台数据 |
| 广深随附样例 | 两份样例复制到临时目录，经 ChannelService 和 ReportProvider 运行；`completed / ANSWER_VALIDATED`，4 次工具调用，`output/gzsz-brief.md` 实际存在 | 固定样例在现有工具预算内可走通；模型、飞书发送及批准均为测试替身，不证明真实语义质量 |
| 完整回归 | `.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests`：714 项，37.235 秒，PASS | 原会话/工具/导入等回归通过；原始输出 `/tmp/local-agent-feishu-unittest.log` |
| 后续只读证据导出 | 同一渠道组 16 项，0.602 秒，PASS | 新增 inspect 脚本后仅重跑受影响集；未重复完整回归 |
| UI 自动验证 | `browser_channel.cjs`、`browser_workbench.cjs`、HTTP fixture 上 `browser_sessions.cjs` 均 PASS；JS 语法 PASS | 合成页面/服务证据：定位、刷新审批、历史/live 隔离、外部任务发现、5/15 秒降频、仅补发 |
| 正式启动入口 | `.venv/bin/python -m local_agent serve --workspace examples/feishu-workspace --state-dir /tmp/local-agent-feishu-browser-state --log-dir /tmp/local-agent-feishu-browser-runs --port 0 --demo` | 正式 CLI，独立状态库；不是直接打开静态 HTML |
| 真实浏览器检查 | `http://127.0.0.1:56651/`；创建目录会话、发送问题、得到两份来源、刷新历史、hash 定位后刷新，控制台 error 为空 | 真实浏览器/真实文件读取，页面明确标识模拟模型；临时服务检查后关闭 |

正式页面样例 Session `1c15b22e688642bf8c777111cb784929`，Run `fa586467a30a45fb9c88e89223e16f32`；记录在上述独立状态库。它们是浏览器演练，不是飞书消息。

固定 F1–F12 覆盖：F1 工具实际读取且结果按调用 ID 进入下一轮、审批后文件哈希；F2/F3 并发/重投去重与同文新消息；F4 绑定身份与改绑；F5/F6 浏览器定位/审批目标；F7 未派发中断、Run 已落库但关联未写回、sending 恢复 unknown；F8 通知失败不重做；F9 取消/拒绝/审批到期；F10 明确拒绝附件/未知命令、撤回不自动取消；F11 原认证、产物核验与内容显示的受影响回归；F12 子进程退出、schema 迁移与恢复。模型超时沿用原 Runtime 的完整回归，未声称在真实飞书触发。

## 代码 Gate

主审未发现已证实 P0/P1。一项 P2：对已结束任务的取消错误回复“已请求取消”；已改为返回实际终态，增加定向断言并通过。未追加同义复审。

**技术交接：本批代码/离线 Gate 与受限真实交付均 PASS。** 单管理器复用、消息幂等、独立 outbox、持久化 ACK 和网页接管已实现。真实应用权限、长连接、收件、Runtime 派发、工具回填、审批、Artifact 哈希、显式取消、不落盘/不重做、终态同步和通知补发均已验证。本次没有强制断网重连，也未把本机链接点击行为算作已验证。

**白话简报：你的飞书账号现在可以发起真实资料任务、收到进度、在本机批准后拿到文件，也可以在审批前从飞书取消。** 批准任务写出的文件与审批预览完全一致；取消任务没有生成第二份文件，也没有偷偷重跑。首批闭环已经成立。虚构资料演练仍不能写成阿里/朴朴历史提效成果。

## 真实验收：固定两条任务

配置见 [README 飞书接入](../README.md#飞书本人私聊接入)。正常报告及取消各一次，最多 2 个模型 Run、20 次请求（包括失败），不自动追加。

1. [x] 飞书发送 `/报告 output/gzsz-brief-1003.md`，要求按四层核对 `01-weekly.md`、`02-actions.md`，注明来源、两城差异、过期纪要与缺失项；工作台核对完整预览后手动批准。产物、哈希、回填与飞书终态均已核实。
2. [x] 使用不同输出名发送第二个报告任务，在待审批时从飞书显式取消；Run 为 `cancelled / CANCELLED`，无第二份文件、无 Artifact、无取消后重做。
3. [x] 在工作台和实际文件系统核对第一份产物、引用和内容；原始 trace、State Store、`channel-events.jsonl`、SQLite 与平台消息/Run/通知已关联。
4. [x] 使用 `trial/inspect-feishu.py` 只读导出记录，并人工核对工具回填、真实 Provider、审批与产物哈希；未覆盖历史失败证据。

需逐项保留：输入平台 message_id、Run ID、原始 trace 路径、真实 Provider 标识、工具结果进入模型的证据、审批决定、产物实际内容/哈希、通知 API 回执。accepted 仅证明平台接收，不证明用户已读。通知超时为 unknown，明确补发可能重复通知，但不能重复任务。
