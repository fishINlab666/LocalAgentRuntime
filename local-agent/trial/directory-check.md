# 受限目录发现验收记录

## 当前任务入口（2026-09-17：连续多轮会话页面）

当前为**持久 Session 的连续 transcript 已实现并完成离线验收**。中栏不再只显示一排 Run 按钮和单个选中结果，而是按旧到新同时显示多轮“用户问题 → Agent 最终回答”；刷新和重启继续读取原有持久记录，不会重新提交 Run。右栏仍按选中轮展示引用、产物与执行步骤。

执行态与检查态已经分离：轮询、审批、拒绝和取消只绑定当前 `liveRunId`，查看旧轮只改变 `inspectedRunId`。详情按 Run ID 懒加载和去重，乱序返回只更新原卡；重复重试复用同一在途请求，跨 Session 的迟到结果会被视图代次丢弃。加载更早轮次时保留当前可见卡位置。右栏展示历史审批的决定、目标、风险和完整内容，但不提供执行按钮；刷新后选中持久化的中断 Run，仍可通过独立 `continuableRunId` 新建继续运行。动态问题与回答继续使用 `textContent`，普通时间线只显示后端已校验的最终 `result.answer`。

验证证据：TDD 首次在“应有 20 张 transcript 卡、实际为 0”处按预期失败；最终 `browser_workbench.cjs` 覆盖 20/22 轮分页、旧到新顺序、乱序详情、单卡重试去重、键盘操作、中断 Run 续跑、A/B 切换、审批与取消目标、视口锚点及 390/1024/1440px；`browser_sessions.cjs` 覆盖三轮同时可见、刷新无新增 POST 和历史审批完整只读预览。`browser_web.cjs`、`browser_imports.cjs`、`browser_tools.cjs`、`browser_extensions.cjs` 均通过；完整 Python 回归 667 项通过。桌面合成页面已人工核对，并据此修复了用户卡白字白底的样式覆盖；同一审阅者对四项修复进行有界复核后给出 PASS。本轮没有调用 DeepSeek，不改变 Runtime、SQLite、模型 Context 或工具权限。

设计见[连续会话设计](../../docs/superpowers/specs/2026-09-17-local-agent-conversation-transcript-design.md)，实施与停止条件见[实施计划](../../docs/superpowers/plans/2026-09-17-local-agent-conversation-transcript.md)。本批到此停止；流式 token、跨会话 Memory、消息编辑和批量 transcript 后端接口继续后置。

## 当前任务入口（2026-09-17：Superpowers Skill + 官方 MCP 真实试用）

当前为**一个独立 Superpowers Skill 与一个外部官方 MCP 的安装、绑定和真实联合 Run 已核实**。默认持久 State Store 已安装 `receiving-code-review`，并通过受限本地 stdio 连接固定版本 `mcp-server-time==2026.8.18`，只开放 `get_current_time`；新 Agent `superpowers-time-demo` 为只读配置，未改变已有三个 Agent。

最终 Run `807bb4dc7604455f987cf9687601087b` 为非模拟 DeepSeek `completed / ANSWER_VALIDATED`。模型实际读取 Skill，调用官方 MCP，首次直接读 README 被 `PATH_NOT_DISCOVERED` 拒绝后，收到错误、列出目录并重试成功；本 Run 的 5 个工具结果都按原调用 ID 进入后续上下文。MCP 返回 `2026-09-17T20:10:30+08:00 / Thursday`，位于独立主机 `20:10:23` 至 `20:10:35` 的记录窗口内；JSONL、SQLite、引用和最终语义 9/9 项核对通过。第一次答案格式失败的 Run 原样保留，没有冒充成功。

本批达到停止条件，不再增加第二个 Skill/MCP 或更多真实样例。证据与限制见[真实验收记录](superpowers-mcp-live-check.md)，安装和实施过程见[实施计划 §0](../../docs/superpowers/plans/2026-09-16-superpowers-official-mcp-live-trial.md)。该结论只覆盖本地只读 Skill 与官方 Time MCP 的一次代表性闭环，不代表远程 MCP/OAuth、脚本型 Skill 或全部生态已经支持。

## 当前任务入口（2026-09-16：受管本地文档导入）

当前为**受管文档导入 Task 1–11 已完成实现，固定离线与真实 DeepSeek AI 语义 Gate 均 PASS**。页面可将 `.md/.txt`、带文字层 PDF 和 DOCX 保存为 State Store 内的私有副本，在兼容的目录型助手中通过 `search_documents` 定位、`read_file` 读取，并把结果按原 ToolCall ID 交回下一轮；最终引用可回到原文本行、PDF 页、DOCX 段落或表格行。扫描件／纯图片 PDF 不支持 OCR。

固定离线验收用一批 MD、TXT、两页 PDF、含表格 DOCX 和被忽略 PNG，完成 2 个持久 Run、6 次脚本化模型请求。证据覆盖：初始请求无正文；搜索结果及 PDF/Word 读取结果进入紧接的模型上下文；引用映射为 PDF 第 2 页和 DOCX 表 1 行 1；外部原件删除并重启后仍能搜索、读取和校验引用；另一 Import 的秘密搜索为空且路径读取被拒绝；SQLite 与 imports sidecar 恢复到新 State Store 后，同一 Session 保留历史并再次读取 TXT 原文。异常后的必需证据不完整时 Gate 必定失败，不能用已有检查误报通过。

验证结果：导入定向组 127 项通过；完整 Python 回归 661 项通过且无 ResourceWarning；JavaScript 语法和六组浏览器回归通过；权限／上下文与重启／恢复两路限定代码 Gate 均 PASS。真实固定组完成 1 个 Session、2 个 Run、8 次 DeepSeek 调用，预算 `42`、负责人 `Mei`、`TXT-READY` 及 PDF 第 2 页、DOCX 表 1 行 1、TXT 第 1 行引用均核对正确。

原始真实报告因旧评分器要求第二轮重复英文标签而保留为 `FAILED`；修正后的标签—值绑定规则对同一报告只读重放，22/22 必需检查通过，AI 语义复核 PASS，没有再次调用模型。首轮额外读取触发的 `FILE_COUNT_LIMIT` 被 Runtime 正确拒绝和回填；两轮“未出现冲突”的措辞超出未完整读取的范围，作为 P2 表述限制保留，不影响三个目标事实和引用通过。完整证据见[真实验收记录](import-live-check.md)。

本批达到停止条件：不增加 OCR、自动同步、资料删除、向量检索、新格式、额外 UI 打磨或更多真实样例。实际玩法、保存位置及备份恢复命令见[README](../README.md)，实现与验收口径见[导入实施计划 §0](../../docs/superpowers/plans/2026-09-15-local-document-import.md)。

## 当前任务入口（2026-09-15：会话工作台）

当前为**三栏会话工作台的首次试玩阻断已修复，最终回归 PASS**。用户实际打开页面后发现发送按钮在常用桌面高度下落到视口之外，Enter 也不能发送；这属于主输入闭环阻断。当前修复让输入框和发送操作保持可见，支持 Enter 发送与 Shift+Enter 换行；设置和新建会话表单默认收起，缺少单文件范围时给出可见提示并聚焦文件字段。页面仍将配置化助手与持久会话放在左栏，连续任务、审批与回答放在中栏，将依据、产物、执行记录和能力管理放在右栏；平板与手机使用不可聚焦的响应式抽屉。会话和 Run 支持签名游标续页，切换助手、会话或历史 Run 时会丢弃旧请求，避免内容或加载状态串线。设计见[工作台设计](../../docs/superpowers/specs/2026-09-15-local-agent-conversation-workbench-design.md)，实施与验证见[工作台实施计划 §0](../../docs/superpowers/plans/2026-09-15-local-agent-conversation-workbench.md)。

Review 同时发现分支内 `write_file` 曾偏离用户约束，提交 `90e5bb4` 已恢复为：用户提交任务前声明唯一输出路径，未声明时不注册写工具；模型不能改路径，每个 Run 最多成功新建一次。最终证据为 Python 465 项、五组浏览器回归及隔离页面 Enter 端到端均 PASS；四种视口的输入框和发送按钮都在首屏内。本轮没有调用 DeepSeek；原有真实 Agent/会话/Skill/MCP 证据继续有效。本阶段按工作台范围停止，不追加新后端能力或第二套视觉。

## 当前任务入口（2026-09-14）

当前为**多助手配置、MCP、Skills、会话与工具层的本批联合闭环已完成**。方案见[扩展设计 §0](../../docs/superpowers/specs/2026-09-14-agent-extensions-design.md)，实施与历史失败证据见[实施计划 §0](../../docs/superpowers/plans/2026-09-14-agent-extensions.md)。[第八次最终真实报告](results/extensions-acceptance-20260915T030927702169Z/report.json)的 R1–R6 机器检查全部 PASS；后置[联合验收记录](extensions-live-check.md)逐项核对事实、引用、摘要、工具结果回填、审批、落盘与错误收口后，AI 语义 Gate PASS。完整回归 464 项通过；固定组累计使用 50/60 次真实 DeepSeek 请求，剩余 10 次，已按停止条件停止追加调用。原机器报告的用户验收仍为 pending；合成审批 `human_approval=false`，不冒充用户签字。当前结论只覆盖本批配置化助手与联合能力，不包含 Memory、Cron、自动子 Agent、远程 MCP/OAuth、多 Provider 或全路线发布质量。

### 会话模块基线（2026-09-11）

当前为**会话管理 Task 1–12 已实现，离线 Gate 通过，真实证据边界已修正**。Runtime、工具结果、上下文、历史查询、CLI、页面、长历史摘要和进程恢复已接入持久 Session；搜索续页、完整信封分页及压缩后保留更正/未完事项也已补齐。完整 Python 387 项通过，三组既有浏览器回归继续有效。受限 DeepSeek 的 6 次提交证明了五个目标场景；更正回答原运行以 `INVALID_REFERENCE` 失败，修复后没有真实复跑，因此不再写成“真实全量通过”。设计见[会话管理模块设计 §0](../../docs/superpowers/specs/2026-09-10-session-management-design.md)，实施见[会话管理实施计划 §0](../../docs/superpowers/plans/2026-09-10-session-management.md)，S1–S13 与分层证据见[会话验收记录](session-live-check.md)。本轮在此停止扩展；流式输出、Memory、MCP 和跨设备同步仍属于后续范围。

### 已完成的工具层基线

**工具层及结果契约复核已通过并收口**。已实现统一工具注册/校验/结果回填、模型意图与程序动作说明、网页/CLI 逐次确认，以及仅新建的 `write_file`。Loop 会核对冻结参数、程序预览、工具错误白名单和本次执行证明；读／列目录的伪成功不会进入证据，写入只有经过一次性发布边界且回执与批准内容一致才进入 `artifacts`。本地真实文件内容与批准预览一致，结果按原调用 ID 进入下一轮 context；拒绝、取消、超时、已有目标、发布竞态、重复发布及后续回答失败等边界已有证据。

工具层实施状态、验证命令和证据统一见 [工具调用层方案 §0](../../docs/superpowers/plans/2026-09-10-tool-runtime.md)及[工具层真实模型验证记录](tool-layer-live-check.md)。工具阶段完整 Python 回归 295 项、独立页面脚本、完整浏览器回归及两次独立代码 Gate 均通过。此前三个真实 run 共调用 DeepSeek 10 次：单文件完成、拒绝报告未落盘、确认报告实际创建并回填回执；结果契约修订没有新增真实 API 调用。这些证据证明工具层，不代表新增会话模块已实现或通过。
下方保留已完成目录阶段的结论和原始证据，描述的是前一阶段，不作为当前任务调度指令。

2026-09-08：用户要求优先输入、回答准确性及工具结果回填，授权进入目录发现，暂缓额外交互细化。

## 当前阶段结论（2026-09-09 范围复核）

**最小 Agent 闭环已验证，本阶段按该目标收口；受限目录发现的第三轮真实固定组也已通过。** 单文件真实会议节选三问及固定基准的证据见 [真实材料试用记录](real-file-check.md)。目录固定组自动及 AI 内容复核 12/12 PASS，36 次工具结果回填、13 处引用准确，支持“模型自主调用 → 实际执行 → 结果进入下一轮 context → 有依据地回答”的结论。

已知限制：补充网页曾将 `青禾-47` 改为 `青禾—47`。该样例仍为失败，不能宣称所有输出准确。新增保真校验代码保留，本地回归与旧结果重放已通过；**新版本真实复测已暂停／后置，原重启请求取消**。它尚未取得新的真实运行证据，不作为最小闭环阶段继续开放的理由，也不标记为真实修复验收完成。

当前没有要求用户重启或重跑终端的活动任务，也不自动开始更多保真加固或功能扩展。后续任务从用户新目标出发，按 [项目开发规则](../../AGENTS.md)限定范围和停止条件。固定样例通过不代表统计可靠性、用户收益已测量或完整 M1 已完成。

证据见 [第三轮原始复核](results/directory-evaluation-b1770c014ae64ccfb2972672e7331d87/semantic-review.md)与 [保真修复记录](results/directory-evaluation-b1770c014ae64ccfb2972672e7331d87/identifier-fidelity-fix.md)。历史记录中的 BLOCK 和当时的“下一步”保留为事实；后续调度以本节为准，不自动恢复旧待办。

## 历史验收记录

第二轮原始自动 10/12、重评自动 12/12、语义 11/12 BLOCK 的历史保留在 [第二轮真实复核](results/directory-evaluation-9871fc0a057d49c986eb5c1805030504/semantic-review.md)。

第一轮原始 8/12 及其三条回复协议失败保留在 [第一轮真实复核](results/directory-evaluation-78031bf496cf4659a3c2c34751c17e40/semantic-review.md)。没有覆盖任一原始机器报告或将评分修正冒充语义通过。

## 已有证据

- 最终模拟固定组：四类共 12 次自动检查通过，报告为 [SIMULATED_ONLY](results/directory-evaluation-0d5c0f7734cc40f98e22872b3d32e9d2/report.json)。前一份模拟报告保留，测试替身不证明真实模型选文件或回答能力。
- Runtime 脚本验证：初始请求无实际文件名或正文；目录结果只有元数据；模型选择两份文件后，下一轮请求同时包含两份完整内容和原调用 ID；引用由各文件本次快照生成。
- 安全与状态复审：已修复根目录被同名普通目录替换、失败重列后误报完整、无效参数重读后快照与范围不一致三项问题。Task 1＋2 SPEC 复审 PASS，132 项定向检查通过。
- 保真修复当时的完整 Python 回归：`python3 -W error::ResourceWarning -m unittest discover -s tests`，200 项通过，10.493 秒；本次范围复核没有重跑应用测试。
- 页面与入口：24 项 HTTP/CLI 检查通过；浏览器回归通过，含两份引用、完整/未完成范围、目录模式刷新与取消、迟到回复保护、缺密钥、5 种视口。主任务查看 [目录回答截图](../artifacts/web-directory.png) 确认布局正常；截图是模拟结果。
- 便捷启动脚本：`bash -n trial/verify-directory-and-serve.sh` 通过；独立审阅用临时假命令验证评测退出码 0/1/2 都不启动页面，仅 3 启动指定工作区及端口。没有调用真实模型。
- 目录实现及保真修复的代码审阅均为 **PASS**；第三轮固定组真实语义复核 **PASS**。旧网页失败保留，新版本真实验证已后置。独立审阅为 AI 复核，不代表用户签字。

有限保真校验只拒绝有当前原文对应的标识连接符变化，不自动改字，也不代表所有专名或推断都经过机器验证。若以后重新启动该质量项，再针对新版本取得必要的真实证据。

## 真实固定组标准

每次使用两份各小于 2 KiB 的合成资料，项目代号在一份文件、评审人和日期在另一份。文件名与关键事实不进入初始问题。更新场景重用此前已访问的文件名，更换代号，检查是否读到新内容。

| 场景 | 次数 | 必须通过 |
|---|---|---|
| 正常跨文件回答 | 3 | 自主列目录、读两份文件、回答三项事实、分别引用两份文件 |
| 同名文件更新 | 3 | 重新读取，回答新代号，无旧代号残留 |
| 缺失预算 | 3 | 完整检查发现范围，明确资料未说明预算 |
| 一份文件读取错误 | 3 | 实际错误回填下一轮，范围保持不完整，返回 unable |

全部场景检查工具结果及调用 ID 是否进入紧接着的模型请求。`completed` 只表示结构与来源检查通过；真实组自动全部通过后仍需逐条核对语义，将复核另存 Markdown，不覆盖原机器报告，不冒充用户签字。

本批不包含流式正文，因此不宣称完整 M1 完成。

## 文件变更清单

以下路径相对 `local-agent/`；文档中明确标注的上级路径位于工作区根目录。

| 范围 | 文件 |
|---|---|
| 文件工具与发现范围 | `local_agent/files.py`、新增 `local_agent/discovery.py` |
| 运行循环与引用 | `local_agent/runtime.py`、`local_agent/answers.py`、新增 `local_agent/prompts.py` |
| 页面与入口 | `local_agent/web_runs.py`、`local_agent/__main__.py`、`local_agent/demo.py`、`local_agent/static/index.html`、`local_agent/static/app.js`、`local_agent/static/app.css` |
| 固定验收与便捷启动 | `local_agent/evaluation.py`；新增 `local_agent/directory_evaluation.py`、`trial/verify-directory-and-serve.sh` |
| 两份演示资料 | 新增 `examples/discovery-workspace/plan.md`、`examples/discovery-workspace/review.md` |
| Python 回归 | `tests/test_answers.py`、`tests/test_cli.py`、`tests/test_web.py`、`tests/test_evaluation.py`；新增 `tests/test_discovery.py`、`tests/test_directory_runtime.py`、`tests/test_directory_evaluation.py` |
| 浏览器回归 | `tests/browser_web.cjs`、`tests/web_fixture.py` |
| 当前文档 | `README.md`、`trial/real-file-check.md`、本记录；工作区的 `docs/superpowers/specs/2026-09-07-local-agent-first-slice-design.md`、`docs/superpowers/plans/2026-09-08-local-agent-web.md`、新增目录发现实施计划 |

实际测试截图、合成运行日志和机器报告另外保存在 `artifacts/` 与 `trial/results/`，不作为真实模型通过的替代证据。
