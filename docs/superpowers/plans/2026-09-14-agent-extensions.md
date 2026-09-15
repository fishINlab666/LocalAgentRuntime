# 助手配置、Skills 与 MCP 实施计划

## §0 当前进度

后续复核（2026-09-15）：下述 R1–R6 固定组通过仍有效，但不足以证明所有验收项已满足。已复现 A2 缺口：CLI 停用正在使用的 Skill 后，在途 Run 仍可能继续读取并完成，尚未修复。用户随后要求解除写入预填路径/单产物限制，该修订按[工具方案 §0](2026-09-10-tool-runtime.md)推进；当前调度入口见[阶段记录](../../../local-agent/trial/directory-check.md)。

### 原固定组交付记录

2026-09-15：A–E 已实现并核实，**第八次真实验收 R1–R6 机器检查与 AI 语义复核全部 PASS，本批交付收口**。R1 证明 Skill、MCP Tool/Resource/Prompt、本地读取、合成审批、真实写入及回执哈希闭环；R2 证明重启后更正和当前状态恢复；R3 证明摘要保存、复用及压缩后新旧日期和未完事项保留；R4 证明第二套 Skill 与独立时间 MCP；R5 证明 MCP 错误如实回填且不拿其他项目替代；R6 证明拒绝结果只回填一次、拒绝后无写入重试、无文件和无 Artifact。完整回归 464 项 PASS。固定组累计 50/60 次真实 DeepSeek 请求，剩余 10 次，停止追加调用。原始机器报告仍保留 `semantic_review/user_acceptance=pending`；后置 AI Gate 见[联合验收记录](../../../local-agent/trial/extensions-live-check.md)，不能冒充用户签收。

实现沿用现有 Python/SQLite/本地页面；扩展依赖可选，不改变既有纯内置入口。共享工作区还有手册任务，修改范围仅限本计划、设计状态、当前阶段入口以及 `local-agent/` 产品与相关测试，不提交其他任务文件。

## 执行与验证

- [x] A：新增 `local_agent/agents.py`，定义不可变配置、校验、模板渲染与装配；先写 `tests/test_agents.py`，验证两份兼容配置、第三份组合、模型/预算单源、非法配置零调用。接入 `file_tools.py/runtime.py/provider.py`，保持既有调用兼容。
- [x] B：`session_store.py/sessions.py` 保存助手 ID/配置快照并迁移旧库；CLI、`web_runs.py/web.py/static/` 共用解析入口，新增最小配置选择/管理。验证重启、旧版本、归属、撤销及旧行为；A–B 完整回归一次。
- [x] C：独立 `skills.py` 实现受管理包、元数据/依赖、绑定后受限读取与签名续页；实际文件测试证明完整内容和包越界拒绝。主任务接入 Context 与页面。
- [x] D：独立 `mcp_client.py/mcp_tools.py` 接入 stdio SDK、能力发现和三类文本入口；`tool_runtime.py` 增加受限 Schema 与来源授权。独立进程验证成功、错参、错误/迟到、超时和取消，不自动重放。
- [x] E：新增 `agent_runtime.py` 集中装配扩展及组合证据策略，增加固定联合验证；CLI/页面展示实际来源、安装状态与失败原因。C–E 完整回归及固定真实组机器检查、AI 语义复核均已通过。

定向命令在 `local-agent/` 运行：`python3 -m unittest tests.test_agents`；新增模块分别使用 `tests.test_skills`、`tests.test_mcp`、`tests.test_agent_sessions` 和 `tests.test_extensions`。依赖安装于本地 `.venv` 后，需要扩展依赖的测试改用 `.venv/bin/python`。完整回归用 `python3 -W error::ResourceWarning -m unittest discover -s tests`（扩展环境用同样参数）。新行为先有失败测试，再实现并确认通过；未改变测试输入不重跑。

A/B/C/D 的确定性验证无需真实模型 API；只在 E 收集新真实证据，首个阻断失败停止同类后续样例。密钥只检查环境中是否存在，不读取/打印或扫描其他终端。需要用户操作时合并为一次明确步骤。

并行只用于独立文件：MCP 适配及其工具契约变更、Skill 文件适配；助手/会话/页面/Context 装配由主任务维护。每个模块返回 diff 与定向验证；仅在可审查里程碑进行必要主审，不叠加同义审查。

## 验证记录

已取得配置/迁移/会话、Skill 包、MCP 真实进程、CLI/页面接口的定向证据。MCP 两种 Server 的实际互操作见[协议记录](../../../local-agent/trial/extensions-mcp-check.md)。旧会话测试与真实失败证据继续保留。

集成回归首轮：446 项中 11 项失败；3 项为并行开发中的验收脚本尚未落盘，8 项为 CLI/网页审批测试仍替换旧装配入口。旧入口替身已改为统一装配替身，保持实际审批、控制器与关闭断言；CLI 8 项、网页审批 8 项定向通过。首轮不记为整组通过；后续交付版本验证结果如下。


### 最终离线证据与剩余验收

- 一次实现主审发现追问绕过工具配置及运行参数扩张预算两项阻断，均已有失败复现后修正。追问沿用助手授权与撤销检查，运行参数只能收紧快照预算；定向 18 项通过，未再开重复审查。
- R2 历史来源与断点续跑修正后的交付回归：`PYTHONPATH=.:tests .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests`，456 项 PASS，16.422 秒。历史 read/search 的显式 `source_id` 与续跑状态复制均先由失败测试复现；额外离线入口探测确认新报告首项为 R2、R1 请求数保持 10，完整输出 0 failures / 0 errors。
- `tests/browser_extensions.cjs`：实际 Chrome 加合成 HTTP，助手选择、能力安装绑定、快照选择和来源显示通过。实际本地 HTTP 页面补测完成单文件问答、会话归属及刷新恢复；使用 DemoProvider，无真实模型调用。补测首个脚本误用不存在的 `#answer`，页面已完成问答；改为现有 `#answer-text` 后只读取已保存结果并检查刷新，没有重提任务。
- [联合离线报告](../../../local-agent/trial/results/extensions-acceptance-20260914T100334849392Z/report.json)：R1 的 Skill 主文/引用、MCP 工具/资源/模板、本地文件、多源引用、真实审批链和新建产物均通过；审批决策为合成夹具，5 次模拟模型请求、0 次真实 API。R2–R6 明确 NOT_RUN，不虚报整阶段完成。
- 固定真实命令：`bash trial/verify-extensions.command`。新建合成目录，最多 6 Run / 60 模型请求（含摘要与修复）；首个阻断停止，报告保留实际消耗及未跑项。真实长历史包含明确标注的离线扩容记录，不冒充真人对话。脚本语法检查通过。
- 最终真实报告及后置内容复核均已取得；机器 PASS 与 AI 语义 PASS 分开记录，均不冒充用户验收。E 已按本批范围完成；不据此宣称 Memory、Cron、自动子 Agent、远程 MCP/OAuth、多 Provider 或整个原始 Agent 路线达到发布质量。
- 首次真实报告如实保留：R1 使用 2 次 DeepSeek 请求后以 `TOOL_CALL_LIMIT` 停止。修复先新增失败样例，分别证明系统提示缺少自然语言限制、MCP 超限错误错误终止 Run、验收预算未继承旧报告；修正后 MCP 11 项及扩展相关 13 项通过。验收入口会自动继承最新真实报告，也接受将首次报告作为唯一参数传给 `verify-extensions.command`；不会覆盖或伪装第一次失败。
- [第二次真实报告](../../../local-agent/trial/results/extensions-acceptance-20260914T112013893932Z/report.json)如实保留：当前尝试 4 次、累计 6 次 DeepSeek 请求，8 个工具调用及产物创建成功，最终因两条不允许的来源引用以 `INVALID_CITATION` 停止。合法的本地、MCP Tool、MCP Resource 来源 ID 和行号均正确；这不是网络、参数、工具执行或结果回填失败。修正只补齐模型可见的引用来源范围，不注册方法或产物为事实来源，也不放宽校验器。
- [第三次真实报告](../../../local-agent/trial/results/extensions-acceptance-20260914T122844664341Z/report.json)如实保留：R1 机器 PASS，R2 使用 3 次请求后因历史来源后缀把字符偏移 `0` 写为行号 `1` 而失败。R2 的旧约定、更正内容、未完事项与最新 MCP 状态均已进入模型上下文，MCP 引用有效，故本次只修正历史来源 ID 的交接方式。后续显式携带该报告时跳过 R1 并从 R2 继续，累计预算不清零。
- [第四次真实报告](../../../local-agent/trial/results/extensions-acceptance-20260914T152754800945Z/report.json)如实保留：R1、R2 机器 PASS；R3 使用 5 次请求（含 1 次摘要）后以 `INVALID_ANSWER` 停止。摘要未保存的根因是模型改写了程序提供的正文或偏移，严格验真正确拒绝；后续历史搜索和 `unable` 回复是摘要缺失后的下游现象。本次以 `span_id` 选择协议消除模型抄写偏移的职责，并补齐失败 R3 的夹具复用；没有修改旧报告或把失败重评为通过。
- R3 修正后的交付回归：`.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests`，459 项 PASS，16.109 秒；`git diff --check` 通过。实际克隆第四次报告后，R1/R2 继续携带，首项为 R3，六条夹具复用前后消息行数均为 53，原报告不变。下一次真实运行只从 R3 开始。
- [第五次真实报告](../../../local-agent/trial/results/extensions-acceptance-20260915T011437973380Z/report.json)如实保留：R1、R2 机器 PASS；R3 新增 4 次请求后以 `INVALID_CITATION` 停止。摘要的 24 个 ID 全部合法，只是模型采用了协议文字可合理理解的字符串数组；最终答案五条引用中唯一错误为 7 行截断摘录写成 1–9 行。两项均属模型边界交接，不是历史、Skill 或 MCP 工具未执行。
- 第五次报告修正后的交付回归：`.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests`，460 项 PASS，16.555 秒；`git diff --check` 通过。用第五次真实请求与回复在克隆库离线重放，24/24 个签发 ID 均恢复为权威事实并成功保存摘要，原数据库哈希不变。预算器按设计区分单次 Run 的 10 次上限与固定组的 60 次累计上限，并在新报告单列本次各场景请求数。
- [第六次真实报告](../../../local-agent/trial/results/extensions-acceptance-20260915T014918532383Z/report.json)如实保留：R1、R2 机器 PASS；R3 新增 4 次请求，最终任务为 `ANSWER_VALIDATED`，历史回查、Skill 重读、最新 MCP 状态、答案与引用均通过，只因活动摘要没有保存和注入而机器 FAIL。摘要回复的 48 个选择全部合法，28 个唯一 ID 展开为权威原文后是 10,608 字节；失败发生在 6,144 字节 Store 防线，不是解析、网络、工具回填或 Context 总预算错误。
- 第六次报告修正后的交付回归：新增合法 ID 但物化超限的固定样例；物化阶段先校验全部选择并跨字段去重，核心五类必须整体保留，anchors 只用剩余字节，核心自身超限仍拒绝。原真实回复在克隆库重放后保存为 6,097 字节，下一轮 manifest 使用同一摘要 ID，原数据库哈希不变。R3 机器 Gate 现额外核对活动摘要确实保留 `2026-09-20`、`2026-09-22`、联调验证和验收记录；完整回归 462 项 PASS，`py_compile` 与 `git diff --check` 通过。
- [第七次真实报告](../../../local-agent/trial/results/extensions-acceptance-20260915T021632959956Z/report.json)如实保留：R1–R5 机器 PASS，R6 使用 6 次请求后因 `terminal_rejection_recorded/reasonable_rejection_end` 失败。实际事件顺序为写入请求、审批拒绝、唯一 `USER_REJECTED` 结果、一次模型收尾；模型没有再次请求写入，文件与 Artifact 均不存在。收尾 JSON 错写为 `answered` 并附事实引用，组合策略的无回执校验正确拒绝该答案，但 Run 因而错误落成 `validation_failed/OUTPUT_NOT_CREATED`。
- 第七次报告修正后的交付回归：拒绝结果后增加一次专用收尾指令，要求 `unable`、空引用、只说明拒绝及未创建；若模型仍返回无效答案，Runtime 保留 `unable/USER_REJECTED` 且不再调用。评分器不再把“拒绝结果未给模型”当作通过，现核对恰好一次回填、无写入重试、真实拒绝终因及未创建说明。相关失败样例先复现，7 项定向检查及完整 464 项回归 PASS，`py_compile` 与 `git diff --check` 通过。
- [第八次最终真实报告](../../../local-agent/trial/results/extensions-acceptance-20260915T030927702169Z/report.json)：沿用 R1–R5 的已通过证据，只续跑 R6；R6 使用 5 次请求后以 `unable/USER_REJECTED` 正确结束，拒绝结果恰好回填一次，之后没有工具请求，指定文件不存在且无 Artifact。整组 R1–R6 机器检查全部 PASS，本次新增 5 次、累计 50/60 次真实请求。两组独立 AI 审阅逐项核对事实、引用、摘要、工具回填、审批和磁盘结果后均 PASS；合并结论见[联合验收记录](../../../local-agent/trial/extensions-live-check.md)。用户签收仍单独保留，不修改原始报告字段。
