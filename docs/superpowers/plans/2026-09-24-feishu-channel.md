# 飞书首批接入实施计划

> For agentic workers: 使用 subagent-driven-development 的独立分工；按项目规则在里程碑统一核对规格与质量，不重复同一风险的审查。

**Goal:** 同机飞书文字私聊复用现有 Agent，支持本机审批、真实产物、可恢复收件和独立通知。
**Architecture:** 单一 WebRuns；ChannelService 负责已授权入站、持久事件与 outbox；官方 SDK 仅传输。沿用 SessionStore 与原 Runtime。
**Tech Stack:** Python、SQLite、现有原生 JS、固定发行版 lark-oapi 可选依赖。

## §0 当前进度

2026-10-03：飞书应用已发布，本人私聊绑定、权限、长连接和正式 8780 服务均已生效。真实只读任务 `e408065a4fd84f748952ccb5591371d8` 完成：平台事件、唯一 Run、真实 DeepSeek 3 次调用、4 次成功工具结果回填、`ANSWER_VALIDATED` 及三类飞书通知回执均已在同一 State Store 对应；人工核对答案与两份虚构资料一致。真实写入任务 `0d1484da4c154c889d16826b34bcab37` 也已完成：本机批准冻结目标后，5 次工具调用全部成功回填，批准预览、实际文件与 Artifact 的 3623 bytes 内容及 SHA-256 完全一致；终态通知首次 unknown，只补发一次后被飞书接受并在客户端可见。第一次审批过期 Run 保持无文件、无产物的失败事实。

取消验收 Run `adddf978697944948061448af496631e` 随后在 `write_file` 等待审批时由本人飞书命令显式取消，最终为 `cancelled / CANCELLED`；无 Artifact、无目标文件、无取消后模型调用或新 Run，飞书终态通知一次接受并在客户端可见。持久 Run 快照已清除可操作审批，SQLite 保留的 pending 审批行仅作历史审计。

当前状态：代码、固定离线 Gate、真实只读、审批写入和待审批取消均 PASS；飞书首批真实验收完成。设计 F1–F12、正式入口和受限真实飞书/DeepSeek 验收的既定停止条件已经满足，本批在此收口，不再追加真实 Run、完整回归或同义审查。完整回归历史证据为 714 项 / 37.235 秒；证据导出脚本随后经渠道定向组 16 项验证。附件、撤回自动取消、手机访问、群聊和自由发送正文仍按范围后置。

首次配置补正：旧说明要求先填私聊 ID 才建连，无法可靠指导无历史事件的新应用。已给准备脚本增加 `--listen`：仅 App ID + 环境变量 Secret 建连，匹配本机随机码的同租户本人私聊后展示身份，须本机确认才保存。它不创建 Run、不发消息，5 分钟超时；默认手工离线方式保留。新增 `test_feishu_prepare.py` 五项检查通过（含拒绝身份/错误码、关闭监听、本机拒绝无落盘、确认后 0600 配置）；不是飞书真实验收。

## 1. SDK 预检与固定传输

Files: 新增 `local-agent/requirements-feishu.txt`、`local-agent/local_agent/channels/feishu.py`、`local-agent/tests/test_feishu_transport.py`。

- [x] 核对发行 wheel 的 ws 回调、ACK、线程退出、请求超时和日志；版本写入 requirements。
- [x] 先验证传输失败不被当成成功，再实现有限超时的 `send(chat_id, text, uuid)`；结果仅为 accepted/retry/unknown/failed，保留平台 message_id。
- [x] 收件经可停止的 SDK 隔离执行器送回主服务，回调只等待持久化 ACK；退出时不遗留 SDK 子进程。
- [x] `python3 -m unittest discover -s tests -p 'test_feishu_transport.py'`：无凭据测试合成 SDK，明确不属于真实飞书。

## 2. 渠道台账与迁移

Files: 新增 `local-agent/local_agent/channels/{__init__,store}.py`；修改 `session_store.py`；新增 `tests/test_channel_store.py`，更新 schema 版本断言。

- [x] 先写并运行重复消息、同文不同消息、冲突事件、通知状态、迁移备份测试。
- [x] 增量 schema 4，新增 bindings/events/outbox，保留旧会话和备份流程。
- [x] 冻结主体、Session、revision、规范化请求和稳定 request ID；原消息重投不创建新 Run。
- [x] 通知 claim/结果持久化，sending 恢复为 unknown；修改绑定使旧待发失效。
- [x] `python3 -m unittest discover -s tests -p 'test_channel_store.py'`；随后受影响 store/recovery 测试。

契约示例（测试断言）：
```python
first, created = channels.receive(binding, envelope, request)
again, repeated = channels.receive(binding, envelope, request)
assert created and not repeated and first['id'] == again['id']
```

## 3. 共享执行及启动

Files: 新增 `channels/service.py`、`channels/config.py`、`tests/test_feishu_channel.py`；修改 `web_runs.py`、`web.py`、`__main__.py`。

- [x] 先写身份拒绝、busy 固定拒绝、显式报告路径、取消/状态、通知失败不重做的集成测试。
- [x] `serve --feishu-config <json>` 读取非密钥绑定；环境只读 `FEISHU_APP_SECRET`，模型配置沿用既有 Provider。
- [x] 一个 ChannelService 使用同一个 WebRuns；先查确定性 request ID，再提交 Run；事务窗口恢复只对账、不重做。
- [x] 工作者分开接收/派发/通知；权限来自绑定，通知仅固定状态和本机深链接。
- [x] 提供经过本机认证的渠道状态和明确补发入口；补发只涉及通知。
- [x] `python3 -m unittest discover -s tests -p 'test_feishu_channel.py'`；运行受影响 CLI/Web 组。

## 4. 工作台接管外部任务

Files: 修改 `static/app.js`；新增 `tests/browser_channel.cjs`，必要时扩展已有 fixture。

- [x] 浏览器失败样例：通过 hash 定位 session/run；刷新仍能看到待审批并取消，不重复 POST。
- [x] 用既有认证获取目标信息，验证 agent/session/run 关系；失效链接明确提示。
- [x] 载入/切换会话时恢复 live Run；有界轮询发现外部 Run，继续保留 inspected/live 区分。
- [x] `node --check local_agent/static/app.js`，运行渠道与会话相关浏览器组。

## 5. 固定验收与交付

Files: 更新 `README.md`、`trial/directory-check.md`；新增 `trial/feishu-check.md`、`examples/feishu-workspace/` 两份明确虚构资料、配置示例及真实记录导出工具。

- [x] 执行 F1–F12 受影响测试，核对真实工具执行/结果回填/审批和产物证据；离线替身单独标识。
- [x] 一次完整 Python 回归 `python3 -W error::ResourceWarning -m unittest discover -s tests`；无新改动不重跑。
- [x] 正式 CLI 启动，真实浏览器核对首屏与链接定位；不把 fixture 当正式入口验证。
- [x] SDK 能安装但缺 App Secret 时给出明确错误，不能显示“飞书已连接”。
- [x] 真实配置下首条写入任务已完成，平台 ID、Run/trace/SQLite、审批、文件与 Artifact 哈希均已记录；第一次尝试因审批过期停止且无文件，第二次批准后成功。
- [x] 使用不同输出名完成一次待审批显式取消，并记录无文件、无 Artifact、无重复执行。原“两条写任务最多 2 个 Run”上限被首次审批过期占用；取消验收按计划例外新增 1 个 Run，完成后已停止。

首批不做：附件自动接收、撤回自动取消、手机访问、群聊、自由发送正文、另外三个业务模板、Runtime 重构。
