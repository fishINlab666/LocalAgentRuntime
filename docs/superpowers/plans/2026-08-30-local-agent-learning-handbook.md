# Local Agent Runtime 新手教学手册 Implementation Plan

## §0 当前进度

2026-09-10 第 10–16 章教学重编：**已核实，成品 Gate PASS（教学内容与本地阅读功能）**。当前验证假设：以同一个具体任务逐步展开“谁做、何时做、输入输出是什么”，能补齐原章只列术语和职责的解释断点。连续实例和自检解析已补齐，原有实质信息和限定经定向复核，来源与链接及桌面/手机阅读检查通过；无剩余交付阻塞。学习效果仍待用户阅读，不以 AI 复核代替用户验收。

- 范围：原手册第 10–16 章、第 09 章阅读引导、版本说明和源文件链接；现有静态/浏览器检查；本计划。保留蓝色极简排版、原锚点、01–08 与其他正文，仅为新增教学示例补自动换行，不修改 Agent Tools 手册或应用。
- 内容：整理项目周报的连续教学案例；每章先问题后名称，展开实际步骤与数据变化，补对照、误区、自检答案和跨章跳转。新案例、字段和数字明确标为教学示意，0829 会议中的示例不得改成已确认配置或已实现功能。
- 来源：沿用原设计和 0829 两份会议；设计文件已更名为 `0-个人本地智能助手-Agent项目设计方案.md`，更新活动链接和校验路径，保留原 SHA-256。
- 验证与停止：一次定向内容复核，加静态检查与本地浏览器检查/截图；不运行应用测试或真实模型调用，不扩展后续功能验收。全部本地，新增真实 API 成本为 0。

### 成品 Review Gate · 第 10–16 章重编（2026-09-10）

**技术交接 — PASS**

- 完成：七章以同一周报任务展开，包含章节内跳转、执行步骤、具体数据/结果、常见误解、检查点及逐题解析。重点补齐保存与发送的区别、100 条历史选择、坏摘要/可续做摘要、记忆写入与取回、工具审批与实际执行、完成证据、Provider 双向适配及四种扩展接入口。
- 来源：设计与 0829 两份会议原文件 SHA-256 均与原快照一致。独立内容复核确认关键主题与限定保留；新案例、字段和数字注明教学示意，未将 0910 新工具策略混入原会议口径。
- 复核发现与处置：P2 来源措辞“向外发送数据”过宽，已对照设计 §5.9 恢复为“发送外部消息”；同时将风险清单的一致性明确为 Provider 消息与 Tool Schema，并补第 12→13 章时间回跳提示。主任务定向核对修订文本，无未处理的阻塞；不为局部文案再发起整轮审查。
- 页面：沿用原样式和交互；截图发现手机示例长行需要横向阅读，已为四块教学示例添加局部自动换行，加入相应浏览器断言并通过复测。
- 静态证据：仓库根目录运行 `python3 tests/validate_handbook.py` → `PASS: handbook validation succeeded`。验证来源 SHA-256、完整快照、离线约束、覆盖矩阵、全部锚点及本地链接、七章教学结构。定向 `git diff --check` 无问题。
- 浏览器证据：`NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_handbook.cjs` → `PASS: browser handbook checks succeeded`。直接读取本地 HTML，无服务端或模型调用；覆盖 1440/1024/768/375 宽度、七章小节跳转/答案展开、示例换行、原搜索/打印/移动目录/无脚本/减少动画及控制台检查。最终版本日期和 Cron 表达式解释为纯文案补充，静态检查再次通过。
- 已查看截图：[桌面第 10 章](../../../artifacts/handbook-deep-2026-09-10/desktop-session-story.png)、[桌面压缩对照](../../../artifacts/handbook-deep-2026-09-10/desktop-compression-example.png)、[手机压缩对照](../../../artifacts/handbook-deep-2026-09-10/mobile-compression-example.png)、[手机自检解析](../../../artifacts/handbook-deep-2026-09-10/mobile-compression-answers.png)。旧 2026-09-09 截图保留。
- 未知/边界：未验证用户是否已真正掌握；本次不是 Agent 应用功能验收，不证明目标架构已经全部实现。没有修改原始材料、其他手册或应用，没有提交或推送。

**通俗交接 — PASS（同一范围）**

七章不再从模块名称开始背定义，而是跟着一份周报看记录怎样保存、怎样交接、怎样复用、怎样执行与核验，再解释模型切换和后续扩展。每章可以自己作答后展开原因；手机上的教学记录无需横向拖读。原设计与会议的关键意思和阶段边界保留，来源及阅读功能已核验。建议从第 10 章开始，分“10–12 / 13–14 / 15–16”三段阅读；阅读感受仍由用户判断，没有待批准的开发动作。

一致性检查：技术与通俗交接均为 PASS，范围、证据及学习效果未知的边界一致。本轮交付结束，不继续扩展测试或应用功能。

变更文件：根目录 `Local-Agent-Runtime-小白入门与开发防跑偏手册.html`；`tests/validate_handbook.py`；`tests/browser_handbook.cjs`；本计划。

以下为 2026-09-09 历史完成记录，不代表本轮已经完成。

2026-09-09 手册优化：**已核实，成品 Gate PASS**。内容修订、独立内容复核、静态检查、浏览器检查与截图查看均已完成。下方 Task 1–6 是 2026-08-30 历史建设计划，其未勾选框不代表当前待办。本次仅优化手册，不重新启动应用开发或已后置的真实模型验证。

- 目标：读者能用同一个读文件实例连接 Context、Tool、Loop 与 Runtime，并区分原始目标架构、教学示意和当前阶段。
- 当前主要阻塞：无。原版校验/执行错误回填和完成证据判断的教学缺口已补齐；当前没有自动扩展或要求用户重跑的下一步。
- 修改范围：根目录手册 HTML；现有手册静态/浏览器检查；本计划。保留章节锚点、三份源材料、覆盖矩阵和原始信息；重讲 03–06 章并补主线答案。优化搜索跳转、打印全文和手机目录的可访问性。
- 完成条件：已满足。修复下述 Gate 发现；主线 01–08 每章有答案；来源 SHA-256、覆盖、站内锚点和本地链接通过；桌面/移动浏览器检查通过并看截图；不扩展产品验收。
- 成本：全部本地文档与浏览器检查，真实模型 API 调用为 0。无新增依赖、埋点或外部网络请求。

### 成品 Review Gate · 原版（2026-09-09）

**技术交接 — BLOCK（范围：手册教学成品）**

- P1：第 05 章 `validateSchemaAndPermission`、`execute` 没有错误分支；示例可能让开发者漏掉错误结果及原调用 ID。`requiresValidation` 仅追加指令，没有解释验证通过状态如何更新。需显式写出受控执行、失败回填和证据检查。
- P2：第 06 章先抛架构名词，混用项目级 Runtime、窄义控制层与 Tool Runtime；第 10 章关系图使 Memory 看起来是所有历史进入 Context 的必经步骤。
- P2：主线自检没有解释答案；前文用户已经指出的困惑尚未进入手册。第 08 章“唯一下一步”仍要求从头验证，未区分历史路径与当前已验证阶段。
- P2：搜索隐藏章节后没有打印全文处理；跨章链接可能指向仍被筛选隐藏的章节。手机无脚本导航及抽屉焦点需核验。
- 下一步：用户已授权优化版，完成本轮只读 Gate 后进入上述局部实现；最终 Gate 待修改和检查后记录。

**通俗交接 — BLOCK（同一范围、同一阻塞）**

原版保留了大量来源信息，但流程示例和概念衔接不足，可能让读者漏掉错误回填或误以为必须先做完整记忆系统。修正文案和示意代码，补解释答案及阅读导航，再交付优化版；无需用户再次批准。此结论不推翻项目最小闭环已验证的事实，也不代表用户已验收学习效果。

一致性检查：两份交接的 Gate 和 P1 阻塞相同。当前项目状态只以 `local-agent/trial/directory-check.md` 为准。

### 成品 Review Gate · 优化版（2026-09-09）

**技术交接 — PASS（范围：教学内容和本地阅读功能）**

- 原版 P1 已修复：Loop 示意明确模型错误与工具错误的不同分支、按原调用 ID 回填、取消后的停止、验证通过/继续/无法完成及预算约束。示意函数明确不是仓库 API，也不是应用实现已通过验证的证据。
- P2 已修复：03–06 补具体例子、短期连续性和 100 条历史的选择解释；06 重讲三种 Runtime 范围、两种 Context、状态、UI 双向职责与统一扩展；10 修正 Memory 必经层误导；主线八章补折叠解析；首页、08 与附录未决项分开历史和当前。
- 来源复核：两份会议的主要实质主题均有对应章节；原 Phase 1 的 CLI 表述恢复，`grape` 标记疑似 grep、未核实，不再推成图检索。源文件 SHA-256 与原快照一致；覆盖矩阵保留，不声称逐字替代原文。
- 页面修复：跨章跳转会恢复被搜索隐藏的目标；打印保留全文并恢复答案折叠状态；手机目录限制焦点并支持退出；禁用脚本仍可通过目录阅读。
- 验证：在仓库根目录运行 `python3 tests/validate_handbook.py` → `PASS: handbook validation succeeded`；`git diff --check` 无问题。使用已安装 Node / Playwright 运行 `tests/browser_handbook.cjs` → `PASS: browser handbook checks succeeded`。浏览器直接打开本地 HTML，没有静态服务或真实模型调用。
- 浏览器范围：1440、1024、768、375 宽度；主线题目与解析逐项对应、折叠展开、06 小节导航、搜索恢复、打印、移动目录键盘循环、无脚本手机导航、减少动画及控制台错误检查。根目录现有测试已经记录具体断言。
- 截图已查看：[桌面首页](../../../artifacts/handbook-2026-09-09/desktop-cover.png)、[桌面第 06 章](../../../artifacts/handbook-2026-09-09/desktop-runtime-story.png)、[手机实例](../../../artifacts/handbook-2026-09-09/mobile-runtime-story.png)、[手机答案](../../../artifacts/handbook-2026-09-09/mobile-runtime-answer.png)。截图和补充截图位于该目录，不是应用真实模型效果证据。
- 独立内容复核：前述八类内容问题已处理，无剩余内容阻塞。当前未证明的项目：用户学习效果、浏览器之外的阅读器兼容性；本次没有重新验收 Agent 应用。

**通俗交接 — PASS（同一范围，无剩余阻塞）**

优化版先让读者经历一次读文件，再介绍各部分名字；容易混淆的概念有对照，自检题可以展开答案。原始会议与设计含义保留，旧阶段的“下一步”不会引导用户重做已完成工作。桌面和手机的阅读、跳转、搜索及打印已检查；是否显著降低理解难度仍需实际阅读反馈，不冒充用户验收。

交付为原路径的更新版，旧版可由 Git 历史回看；未提交或推送。本次修改四个文件：手册 HTML、手册静态检查、手册浏览器检查、本计划。一致性检查：技术和通俗版本均为 PASS，范围与剩余未知相同，无需再次批准。

---

## 历史实施计划（2026-08-30，保留为建设记录）

**Goal:** 基于三份只读源文件，生成一份内容可追溯、离线可用、具有浮动侧导航的 Agent 新手教学手册 HTML。

**Architecture:** 最终交付是单个渐进增强 HTML：语义化正文和锚点不依赖 JavaScript；内联 CSS 提供极简工作手册样式与响应式布局；内联 JavaScript 提供搜索、Scroll Spy、阅读进度、移动目录和返回顶部。Python 标准库验证脚本检查源文件版本、章节覆盖、锚点完整性、离线约束和结构要求。

**Tech Stack:** HTML5、CSS3、Vanilla JavaScript、Python 3 标准库、Playwright 浏览器检查

---

## 文件结构

- Create: `Local-Agent-Runtime-小白入门与开发防跑偏手册.html` — 最终离线教学手册，包含全部内容、样式和交互。
- Create: `tests/validate_handbook.py` — 使用 Python 标准库执行静态结构、来源版本、覆盖键、锚点和离线依赖验证。
- Existing read-only: `个人本地智能助手-Agent项目设计方案.md`
- Existing read-only: `meeting/0829/会议录制：agent-meeting01.md`
- Existing read-only: `meeting/0829/会议录制：agent-meeting02.md`
- Existing spec: `docs/superpowers/specs/2026-08-30-local-agent-learning-handbook-design.md`

当时目录尚未纳入 Git，使用测试结果和 SHA-256 快照作为检查点。工作区现已纳入 Git；本次文档优化不自动提交或推送。

### Task 1: 建立可执行的内容与页面验收器

**Files:**
- Create: `tests/validate_handbook.py`
- Test: `tests/validate_handbook.py`

- [ ] **Step 1: 写入会失败的初始验证器**

验证器使用 `hashlib`、`html.parser.HTMLParser` 和 `pathlib.Path`，至少定义以下常量：

```python
TARGET = ROOT / "Local-Agent-Runtime-小白入门与开发防跑偏手册.html"
SOURCES = {
    ROOT / "个人本地智能助手-Agent项目设计方案.md": "3c1c322affc8056295a6810f4f6db6c6c0a208a9b9ea163564856e105e2f3779",
    ROOT / "meeting/0829/会议录制：agent-meeting01.md": "6ba6fb17d6dc83ae5335c6af210b4b68df63154c1fe8c424add755bad2d8d670",
    ROOT / "meeting/0829/会议录制：agent-meeting02.md": "f33607087582a429eace0e19b919aa83a7911befc398b7e33b0b295608fef4a5",
}
REQUIRED_COVERAGE = {
    *(f"D-{i:02d}" for i in range(1, 11)),
    *(f"M1-{i:02d}" for i in range(1, 8)),
    *(f"M2-{i:02d}" for i in range(1, 7)),
}
```

验证器必须检查：目标存在；源 SHA-256 未变化；无重复 `id`；所有 `href="#..."` 有目标；覆盖键齐全；包含搜索、目录、进度、移动目录、返回顶部、来源标签、打印样式和 `prefers-reduced-motion`；不存在外部脚本、样式表、字体或图片 URL；不存在 `TODO`、`TBD`、`FIXME`。

- [ ] **Step 2: 运行验证器并确认红灯**

Run: `python3 tests/validate_handbook.py`

Expected: FAIL，明确提示目标 HTML 尚不存在。

- [ ] **Step 3: 检查测试自身可执行性**

Run: `python3 -m py_compile tests/validate_handbook.py`

Expected: 无输出且退出码为 0。

### Task 2: 构建语义化 HTML 骨架和 60–90 分钟主线

**Files:**
- Create: `Local-Agent-Runtime-小白入门与开发防跑偏手册.html`
- Test: `tests/validate_handbook.py`

- [ ] **Step 1: 写入单页语义骨架**

骨架必须包含：

```html
<a class="skip-link" href="#main-content">跳到正文</a>
<header class="mobile-header">...</header>
<aside id="sidebar" aria-label="章节导航">...</aside>
<main id="main-content" tabindex="-1">...</main>
<button id="back-to-top" aria-label="返回顶部">...</button>
<div id="reading-progress" aria-hidden="true"></div>
```

使用稳定章节 ID：`start`、`project`、`model-to-agent`、`context`、`tool`、`agent-loop`、`runtime`、`first-slice`、`guardrails`、`deep-dives`、`appendices`。

- [ ] **Step 2: 写入主线九章完整教学内容**

每章至少包含：章节问题、结论、通俗解释、准确术语、当前项目映射、常见误区、开发检查点、自检问题和来源依据。按设计说明第 5.1 节逐章写入，不省略会议中的模型局限、产品形态、用户不偏好 CLI、工具回填、Runtime 分层和最小闭环讨论。

- [ ] **Step 3: 写入三张核心图和 Agent Loop 伪代码**

图示使用可访问的 HTML/CSS 结构：

```text
Model → decision → final response
                 ↘ tool call → execute → observation ┐
                            ↑__________________________┘
```

伪代码必须体现最大轮数、取消、工具执行结果回填和停止条件。

- [ ] **Step 4: 运行静态验证观察剩余失败项**

Run: `python3 tests/validate_handbook.py`

Expected: HTML 存在、基础锚点通过；深挖、覆盖键、样式和交互相关检查仍失败。

### Task 3: 写入深挖章节、术语表和完整证据覆盖

**Files:**
- Modify: `Local-Agent-Runtime-小白入门与开发防跑偏手册.html`
- Test: `tests/validate_handbook.py`

- [ ] **Step 1: 写入深挖第 10–19 章**

逐章覆盖设计说明第 5.2 节：Session/Context/Memory、上下文压缩、长期记忆与知识库、Tool Runtime 与权限、完成条件与评测、Provider、MCP/Skill/RAG/Cron、产品形态、Phase 0–7 与 M1–M5、协作和学习方式。

- [ ] **Step 2: 写入规范术语表**

至少包含 Model、Provider、Agent、AgentRun、Agent Loop、Runtime、Harness、Context、Session、Memory、Tool、ToolCall、Tool Runtime、Skill、MCP、Trigger、Artifact、Event/Trace，并保持与设计说明第 6 节定义一致。

- [ ] **Step 3: 写入转写纠错表**

表格列为“原转写、可能术语、判断依据、置信度”。至少覆盖 `look`、`refile/redfile`、`tell list/tour list`、`方心靠/通信靠`、`chrome`、`reg`、`CRI`、`绘画/货号`。无法可靠判断的词必须标记为低置信度，不强行定论。

- [ ] **Step 4: 写入设计文档覆盖矩阵**

使用 `data-coverage="D-01"` 至 `data-coverage="D-10"` 映射设计文档十大主题，并列出对应 HTML 章节。

- [ ] **Step 5: 写入会议 01 覆盖矩阵**

使用七个时间段键：

```text
M1-01 00:01–00:10 项目目标、产品形态、企业交付、事件轨迹
M1-02 00:11–00:17 模型本质与三类局限
M1-03 00:18–00:28 Context、API、构建层/应用层、工具定义
M1-04 00:29–00:37 Tool Calling 与 Agent Loop
M1-05 00:37–00:47 完成判断、验证注入、RAG 与产品优化
M1-06 00:47–00:55 会话、上下文上限、Tool Runtime、权限和模式
M1-07 00:55–结束 会话隔离与数据库管理
```

- [ ] **Step 6: 写入会议 02 覆盖矩阵**

使用六个时间段键：

```text
M2-01 00:00–00:07 短期记忆、上下文压缩、任务状态和评测基准
M2-02 00:07–00:11 长期记忆、memory.md、记忆搜索与知识库
M2-03 00:11–00:13 Runtime 定义及核心与外围扩展
M2-04 00:13–00:16 Cron 调度与任务注入
M2-05 00:17–00:20 API、read_file、上下文历史、工具集与 Loop 顺序
M2-06 00:20–结束 克制范围、GitHub 同步和学习方式
```

- [ ] **Step 7: 运行覆盖验证**

Run: `python3 tests/validate_handbook.py`

Expected: 来源版本、覆盖键、章节、锚点和离线内容检查通过；样式和交互项可能仍失败。

### Task 4: 实现极简产品工作手册视觉系统

**Files:**
- Modify: `Local-Agent-Runtime-小白入门与开发防跑偏手册.html`
- Test: `tests/validate_handbook.py`

- [ ] **Step 1: 定义内联设计令牌**

```css
:root {
  --bg: #f6f7f9;
  --surface: #ffffff;
  --surface-subtle: #eef2f6;
  --text: #172033;
  --text-muted: #536174;
  --accent: #2563eb;
  --border: #dce3ea;
  --focus: #1d4ed8;
  --sidebar-width: 18rem;
  --content-width: 48rem;
}
```

字体使用系统栈，正文最小 16px、行高 1.7、桌面正文宽度约 65–75 个字符。

- [ ] **Step 2: 实现桌面固定侧栏和正文布局**

使用 `position: fixed` 侧栏、为正文预留对应边距、单一主滚动区和明确当前章节状态。不得出现侧栏覆盖正文或嵌套滚动正文。

- [ ] **Step 3: 实现移动端目录**

在 900px 以下将侧栏改为抽屉；遮罩、关闭按钮和目录链接均可操作；正文无水平溢出；触控区域至少 44×44px。

- [ ] **Step 4: 实现内容组件和表格/代码样式**

包括结论框、来源标签、教学解释、误区警告、检查点、自检题、流程图、代码块、可折叠深挖和局部横向滚动表格。

- [ ] **Step 5: 实现可访问性与打印样式**

提供可见焦点、跳转目标偏移、`prefers-reduced-motion`、高对比度文本和 `@media print`；打印时隐藏侧栏、搜索、进度、移动导航和返回顶部，并展开深挖内容。

### Task 5: 实现低摩擦阅读交互

**Files:**
- Modify: `Local-Agent-Runtime-小白入门与开发防跑偏手册.html`
- Test: `tests/validate_handbook.py`

- [ ] **Step 1: 实现移动目录开关**

按钮维护 `aria-expanded`，打开时聚焦关闭按钮，关闭后将焦点还给触发按钮；Escape 可以关闭。

- [ ] **Step 2: 实现 Scroll Spy**

使用 `IntersectionObserver` 更新 `aria-current="location"` 和活动样式；不支持时保留普通锚点行为。

- [ ] **Step 3: 实现阅读进度和返回顶部**

滚动时计算 `scrollTop / (scrollHeight - clientHeight)`；使用 `requestAnimationFrame` 节流；在减少动画模式下使用即时返回顶部。

- [ ] **Step 4: 实现本地全文搜索**

搜索标题、正文、术语和来源标签；显示结果数量；无结果时显示恢复路径；清空时恢复全部章节。搜索功能不能删除 DOM 中的证据内容。

- [ ] **Step 5: 运行完整静态验证**

Run: `python3 tests/validate_handbook.py`

Expected: `PASS: handbook validation succeeded`。

### Task 6: 浏览器与打印质量验证

**Files:**
- Modify if required: `Local-Agent-Runtime-小白入门与开发防跑偏手册.html`
- Test: browser screenshots and console

- [ ] **Step 1: 启动本地静态服务器**

Run: `python3 -m http.server 8765 --bind 127.0.0.1`

Expected: 浏览器可访问 `http://127.0.0.1:8765/Local-Agent-Runtime-%E5%B0%8F%E7%99%BD%E5%85%A5%E9%97%A8%E4%B8%8E%E5%BC%80%E5%8F%91%E9%98%B2%E8%B7%91%E5%81%8F%E6%89%8B%E5%86%8C.html`。

- [ ] **Step 2: 检查桌面布局与交互**

在 1440×1000 和 1024×900 检查侧栏、搜索、锚点、活动章节、折叠、自检题和返回顶部；控制台必须无错误。

- [ ] **Step 3: 检查移动布局与键盘操作**

在 768×1024 和 375×812 检查目录抽屉、正文溢出、表格局部滚动、触控目标、Escape、Tab 顺序和焦点恢复。

- [ ] **Step 4: 检查打印预览与减少动画**

确认打印时交互界面隐藏、正文与来源保留；模拟 `prefers-reduced-motion: reduce` 后无非必要平滑动画。

- [ ] **Step 5: 根据截图修复视觉问题并重新验证**

每次修改后运行：

Run: `python3 tests/validate_handbook.py`

Expected: `PASS: handbook validation succeeded`，浏览器控制台继续无错误。

### Task 7: 最终来源与交付核验

**Files:**
- Verify: `Local-Agent-Runtime-小白入门与开发防跑偏手册.html`
- Verify: three read-only sources

- [ ] **Step 1: 重新计算源文件校验值**

Run: `shasum -a 256 '个人本地智能助手-Agent项目设计方案.md' 'meeting/0829/会议录制：agent-meeting01.md' 'meeting/0829/会议录制：agent-meeting02.md'`

Expected: 与 Task 1 中的三个 SHA-256 完全一致。

- [ ] **Step 2: 扫描占位符和外部依赖**

Run: `rg -n 'TODO|TBD|FIXME|https?://|fonts.googleapis|cdn\.' 'Local-Agent-Runtime-小白入门与开发防跑偏手册.html'`

Expected: 无输出；源文件相对链接不计为外部依赖。

- [ ] **Step 3: 运行最终自动验证**

Run: `python3 tests/validate_handbook.py`

Expected: `PASS: handbook validation succeeded`。

- [ ] **Step 4: 记录最终文件信息**

Run: `wc -l -w -c 'Local-Agent-Runtime-小白入门与开发防跑偏手册.html' && shasum -a 256 'Local-Agent-Runtime-小白入门与开发防跑偏手册.html'`

Expected: 输出最终行数、词数、字节数和 SHA-256，作为交付快照。
