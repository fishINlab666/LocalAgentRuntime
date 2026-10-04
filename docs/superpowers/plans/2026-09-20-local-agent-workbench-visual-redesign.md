# Local Agent Steadbot-Style Workbench Visual Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按用户已确认的高保真方向，把旧蓝灰技术三栏重构为“固定 Agent 轨 + 当前 Agent 会话 + 连续对话主舞台 + 工作状态栏”，并在 Composer 持续显示真实模型与运行状态，同时保留现有 Session、Run、审批、Artifact 与安全边界。

**Architecture:** 先以新的语义 DOM 和完整设计 Token 替换旧页面骨架与旧 CSS，再用纯投影函数把现有 `/api/config`、Agent、Session、Run 和 Artifact 数据接回新挂载点。`liveRunId` 继续只控制轮询和动作，`inspectedRunId` 继续只控制详情查看；技术证据进入覆盖式详情 Sheet，不复制 Steadbot 的 Todo、Cron 或多任务状态机。

**Tech Stack:** Python 标准库本地服务、原生 HTML/CSS/JavaScript、Node.js `vm` 合成 DOM 测试、Playwright + 本地合成 HTTP/路由测试；不新增前端依赖、外部字体、遥测或网络资源。

---

## §0 当前进度、假设与完成条件

2026-09-20：用户已确认[视觉重设计规格](../specs/2026-09-19-local-agent-workbench-decision-delivery-design.md)与[高保真对比稿](../mockups/2026-09-20-local-agent-workbench-comparison.html)。**设计 Gate、前端实施、固定离线验证与最终代码 Gate 均 PASS；用户最终体验验收待进行。** 页面已经从旧蓝灰三栏整体替换为固定 Agent 轨、当前 Agent 会话、连续对话主舞台、工作状态栏和覆盖式详情 Sheet；Composer 持续显示真实模型与运行状态。没有新增 Todo、Cron、并行 Run、多 Agent 群聊或后端协议。

实现只修改前端静态资源、浏览器测试和状态文档；`web_runs.py`、`sessions.py`、Runtime、SQLite schema、Provider 与认证协议均未修改。最终证据为项目 `.venv` 下 Python 667 项通过，`browser_tools.cjs`、`browser_workbench.cjs`、`browser_extensions.cjs`、`browser_imports.cjs`、`browser_sessions.cjs`、`browser_web.cjs`、`browser_visual_redesign.cjs` 全部通过，且 1440×900、1024×768、768×1024、375×812 四张截图已人工核对。最终有界代码 Gate 曾发现 inline Artifact、1180–1199px 抽屉隔离和 44px 触控尺寸缺口；修复并复核后结论为 PASS。本批没有调用 DeepSeek；页面真实资料与真实模型体验仍由用户最终验收。

当前要验证的假设：在不修改后端的情况下，现有 Agent、Session、Run、Approval 和 Artifact 数据足以支撑确认稿中的工作台。当前没有产品阻塞；唯一风险是前端重构时破坏已经通过的审批目标隔离、竞态保护或安全文本渲染。

完成条件：

- 1440×900 首屏明显呈现确认稿的四段桌面骨架，而不是旧页面换色。
- 桌面固定显示多个已启用 Agent；会话列表只显示当前 Agent 的 Session 和真实资料范围。
- Composer 在所有必验视口显示真实模型名与 `就绪／排队中／调用中／等待确认／不可用`。
- 右栏只投影当前 Session 的真实 Run／Artifact；详情 Sheet 保留引用、审批历史和 Trace。
- 原有审批、取消、历史查看、Artifact 认证预览、Session 代次保护和纯文本安全测试继续通过。
- 375×812、768×1024、1024×768、1440×900 无页面级横向滚动，关键操作可达。
- 最终 Review Gate 同时给出“技术证据／大白话解释／决策影响”，然后停止，不继续扩展产品范围。

## 文件结构与职责

本计划只触及以下范围：

```text
local-agent/
├── local_agent/static/
│   ├── index.html              # 重建语义骨架；保留必要 ID 作为行为挂载点
│   ├── app.css                 # 整体替换旧视觉系统，不在旧规则尾部叠加补丁
│   └── app.js                  # 新投影函数、导航/状态栏渲染、详情 Sheet 接线
├── tests/
│   ├── browser_tools.cjs       # 纯投影、审批、Artifact 安全与竞态
│   ├── browser_workbench.cjs   # transcript、live/inspected 隔离及桌面主流程
│   ├── browser_sessions.cjs    # 持久会话、Agent 隔离和迟到响应
│   ├── browser_web.cjs         # 服务页面、缺配置和既有问答回归
│   ├── browser_imports.cjs     # 导入入口迁入新设置层后的行为
│   ├── browser_extensions.cjs  # Agent/Skill/MCP 管理入口迁移后的行为
│   └── browser_visual_redesign.cjs # 新增；结构、断点、模型状态和截图验收
└── trial/directory-check.md    # 唯一当前阶段入口

docs/superpowers/
├── specs/2026-09-19-local-agent-workbench-decision-delivery-design.md
└── plans/
    ├── 2026-09-19-local-agent-workbench-decision-delivery.md # 历史行为计划，标记被替代
    └── 2026-09-20-local-agent-workbench-visual-redesign.md   # 本计划
```

明确不修改：`web_runs.py`、`sessions.py`、Runtime、SQLite schema、认证头、Artifact 下载地址、Prompt 和 Provider。若前端无法仅凭现有响应完成某一项，立即停止并把该项标为范围阻塞，不自行补后端。

## 实施共用命令

以下命令均在 `/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent` 执行：

```zsh
TASK_NODE=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
TASK_NODE_MODULES=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules
```

实际执行前在同一 shell 中定义这两个任务专用变量；不覆盖 `$HOME`、`$CODEX_HOME` 等系统变量。本计划不自动创建提交、分支或 worktree；版本管理只在用户另行要求时进行。

## Task 1: 用失败测试冻结确认稿契约

**Files:**

- Create: `local-agent/tests/browser_visual_redesign.cjs`
- Modify: `local-agent/tests/browser_tools.cjs`
- Modify: `local-agent/tests/browser_workbench.cjs`

- [x] **Step 1: 为两个新增纯投影写表驱动测试**

在 `browser_tools.cjs` 先调用尚不存在的 `projectAgentWorkspace()` 与 `projectModelStatus()`：

```js
const workspace = context.projectAgentWorkspace(
  [fileAgent, directoryAgent, {...disabledAgent, enabled: false}],
  [fileSession, directorySession],
  'file-qa',
);
assert.deepEqual(workspace.agents.map(item => item.id), ['file-qa', 'directory-qa']);
assert.deepEqual(workspace.sessions.map(item => item.id), [fileSession.id]);

const frozen = context.projectModelStatus(
  {ready: true, provider: {provider: 'deepseek', model: 'runtime-model'}},
  directoryAgent,
  {agent_snapshot: fileAgent},
  {state: 'waiting_approval'},
);
assert.equal(frozen.model, fileAgent.model.name);
assert.equal(frozen.source, 'session_snapshot');
assert.equal(frozen.status, '等待确认');
```

状态矩阵固定为：无配置 `不可用`、`queued` 为 `排队中`、`running` 为 `调用中`、`waiting_approval` 为 `等待确认`、其余且 Provider 可用为 `就绪`。持久 Session 的 `agent_snapshot.model` 必须压过当前 Agent 与运行时 Provider 名称。

- [x] **Step 2: 新建结构与断点浏览器测试**

`browser_visual_redesign.cjs` 使用与 `browser_workbench.cjs` 相同的静态页面和合成路由，先断言以下新契约：

```js
for (const selector of [
  '#agent-dock', '#session-panel', '#thread-panel', '#work-rail',
  '#model-status', '#model-name', '#model-state', '#run-detail-sheet',
]) {
  assert.equal(await page.locator(selector).count(), 1, `missing ${selector}`);
}
assert.equal(await page.locator('#agent-dock [data-agent-id]').count(), 3);
assert.equal(await page.locator('#model-name').innerText(), 'deepseek-chat');
assert.equal(await page.locator('#model-state').innerText(), '就绪');
```

为 1440、1024、768、375 四个宽度加入结构断言：桌面四段常驻；1024/768 Agent 轨常驻且会话/工作栏为浮层；375 只显示单栏对话与顶栏 Agent 切换器。每个宽度都断言 `documentElement.scrollWidth <= innerWidth + 1` 且模型状态可见。

- [x] **Step 3: 运行 RED，记录首个有效失败**

```zsh
$TASK_NODE tests/browser_tools.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_visual_redesign.cjs
```

Expected: 第一条命令首先因新增投影不存在而失败；第二条命令因新语义 DOM 不存在而失败。只记录首个有效失败，不为同一缺口重复扩写测试。

## Task 2: 建立 Agent 工作区与模型状态纯投影

**Files:**

- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/tests/browser_tools.cjs`

- [x] **Step 1: 实现 `projectAgentWorkspace`**

函数不读取 DOM、不发请求，只做确定性过滤：

```js
function projectAgentWorkspace(agentItems, sessionItems, selectedId) {
  const enabled = (agentItems || []).filter(item => item.enabled !== false);
  const selected = enabled.find(item => item.id === selectedId) || enabled[0] || null;
  return {
    agents: enabled,
    selected,
    sessions: selected
      ? (sessionItems || []).filter(item => item.agent_id === selected.id)
      : [],
  };
}
```

停用 Agent 只留在管理 Sheet；日常 Agent 轨不显示。即使服务端已经按 Agent 过滤，前端仍按 `session.agent_id` 再投影，防止不同 Agent 的会话混在一起。

- [x] **Step 2: 实现 `projectModelStatus` 并保存真实 config**

增加 `appConfig`，只保存 `/api/config` 已返回的普通数据：

```js
function projectModelStatus(config, selectedAgent, session, liveRun) {
  const frozen = session?.agent_snapshot || null;
  const sourceAgent = frozen || selectedAgent || null;
  const state = liveRun?.state;
  const status = !config?.ready ? '不可用'
    : state === 'queued' ? '排队中'
    : state === 'running' ? '调用中'
    : state === 'waiting_approval' ? '等待确认'
    : '就绪';
  return {
    provider: sourceAgent?.model?.provider || config?.provider?.provider || '未配置',
    model: sourceAgent?.model?.name || '未配置模型',
    status,
    source: frozen ? 'session_snapshot' : 'agent',
    revision: session?.agent_revision || sourceAgent?.revision || null,
    error: config?.error || null,
  };
}
```

不显示上下文余量、价格或速率；它们没有当前真源。

- [x] **Step 3: 运行纯函数和现有安全行为测试**

```zsh
$TASK_NODE --check local_agent/static/app.js
$TASK_NODE tests/browser_tools.cjs
```

Expected: 新增状态矩阵、既有审批锁、Artifact 纯文本预览和竞态断言全部 PASS。

## Task 3: 从零重建语义 DOM 与视觉系统

**Files:**

- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/tests/browser_visual_redesign.cjs`

- [x] **Step 1: 替换页面骨架，不复制旧三栏**

将 `index.html` 主体重组为：

```html
<div class="app-shell">
  <nav id="agent-dock" aria-label="切换助手">
    <a class="agent-home" href="#thread-panel" aria-label="Local Agent 工作台">L</a>
    <div id="agent-list"></div>
    <button id="agent-settings-toggle" type="button">管理助手</button>
  </nav>
  <aside id="session-panel" aria-label="当前助手的会话">
    <header><h2 id="current-agent-name"></h2><button id="session-create-toggle" type="button">新建会话</button></header>
    <div id="session-list"></div>
  </aside>
  <main id="thread-panel">
    <header id="thread-header"></header>
    <div id="session-history"></div>
    <form id="question-form"></form>
  </main>
  <aside id="work-rail" aria-label="当前会话工作状态">
    <section id="work-waiting"></section>
    <section id="work-running"></section>
    <section id="work-completed"></section>
  </aside>
</div>
<aside id="run-detail-sheet" class="detail-sheet" aria-label="运行详情" hidden></aside>
```

保留 `#question`、`#start`、`#approval-*`、`#artifact-*`、`#session-select`、`#agent-select` 等既有行为 ID，但允许把原生 select 设为 `hidden` 作为状态桥。它们不能继续决定视觉布局。导入、Agent 管理、Skill/MCP、路径和输出设置进入独立 Sheet／Popover；不得默认占据左栏或右栏。

- [x] **Step 2: 整体替换 `app.css`**

删除旧 CSS 规则主体，从确认稿 Token 开始重建：

```css
:root {
  --canvas: #f1ede5;
  --paper: #fbfaf7;
  --paper-raised: #fff;
  --ink: #26221d;
  --muted: #756f67;
  --hairline: #ded7cc;
  --accent: #9a4b28;
  --accent-soft: #f2e3d8;
  --success: #3f6b50;
  --danger: #9a3f38;
}

.app-shell {
  display: grid;
  grid-template-columns: 56px 176px minmax(0, 1fr) 288px;
  min-height: 100dvh;
  background: var(--canvas);
}
```

主栏无阴影；仅 Composer、决策卡、文件卡和覆盖式 Sheet 使用一级柔和阴影。普通 Agent 回答直接排版，用户消息为暖灰气泡。去掉英文大写 Kicker 和旧技术后台的蓝灰渐变。

- [x] **Step 3: 接入断点骨架**

使用两个明确断点：

- `768–1199px`：`56px + minmax(0,1fr)`；Session 与工作栏改为覆盖浮层。
- `<768px`：单栏对话；Agent／Session 从顶栏切换；Agent dock 不占页面宽度。

所有触控按钮最小 `44px`；正文和移动端输入不小于 `16px`；尊重 `prefers-reduced-motion`。

- [x] **Step 4: 运行结构测试到首个行为缺口**

```zsh
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_visual_redesign.cjs
```

Expected: 新结构与断点断言通过；测试下一步应在 Agent／Session 或模型状态尚未接线处失败，而不是因旧 DOM 缺失报脚本异常。

## Task 4: 接回固定 Agent 轨与当前 Agent 会话栏

**Files:**

- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/tests/browser_visual_redesign.cjs`
- Modify: `local-agent/tests/browser_sessions.cjs`

- [x] **Step 1: 拆分 Agent dock 与 Session pane 渲染**

将现有 `renderAgentList()` 改为只渲染 Agent 轨：稳定首字／线性标记、完整可访问名称、`aria-pressed`、当前态描边和底色。增加当前 Agent 摘要挂载点，显示名称、策略和真实能力范围，不显示虚构的独立工作区根目录。

`renderSessionList()` 使用 `projectAgentWorkspace(...).sessions`，每项显示：标题、`SessionScope`（文件名或“目录资料”）、更新时间，以及仅来自当前客户端 live run 的 `运行中／待确认`。

- [x] **Step 2: 统一桌面、平板、手机的选择状态**

桌面点击 Agent 轨、平板浮层和手机顶栏下拉都只调用现有 `selectAgent(agentId)`；不建立第二份 Agent 状态。切换后必须清空旧 Session 视图、增加 `viewGeneration`，再加载该 Agent 的会话。

- [x] **Step 3: 保留会话行为与代次保护**

更新 `browser_sessions.cjs`：

- 创建 file Agent 的 Session 后只出现在 file Agent 下；
- 切到 directory Agent 时旧 Session 不可见；
- 切回 file Agent 后原 Session 恢复；
- A 的迟到详情不能覆盖 B；
- 刷新不伪造跨重启未读或等待标记。

- [x] **Step 4: 运行停止点 A 验证**

```zsh
$TASK_NODE --check local_agent/static/app.js
$TASK_NODE tests/browser_tools.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_visual_redesign.cjs
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 -- \
  env NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_sessions.cjs
```

Expected: Agent 与 Session 隔离、持久会话和响应式结构 PASS。此时只证明导航骨架，不宣称 Composer、工作栏和视觉最终完成。

## Task 5: 重建连续对话与 Codex 式 Composer 模型状态

**Files:**

- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/tests/browser_visual_redesign.cjs`
- Modify: `local-agent/tests/browser_workbench.cjs`

- [x] **Step 1: 让 transcript 成为中栏视觉主角**

保留现有 transcript 的旧到新顺序和分页，但重绘为：用户问题右侧暖灰气泡；Agent 最终回答直接排版；Run 回执为一行注脚；失败紧邻对应 Run；`运行详情` 为文字按钮。不得为普通回答再包统一白卡。

- [x] **Step 2: 重建悬浮 Composer**

Composer 最大宽度 `760px`，包含输入、资料／附件入口、发送／取消和底部模型状态。模型详情使用原生 `<details id="model-status">`，避免引入第二套弹层状态：

```html
<details id="model-status" class="model-status">
  <summary><span class="status-dot"></span><span id="model-name"></span>
    <span aria-hidden="true">·</span><span id="model-state"></span></summary>
  <div class="model-status-detail">
    <span id="model-provider"></span><span id="model-source"></span>
    <span id="model-revision"></span>
  </div>
</details>
```

当前 Session 只读显示冻结模型，不提供切换。空间不足时可隐藏 Provider 文案，但不可隐藏模型名和状态。

- [x] **Step 3: 在所有真实状态转换后刷新模型状态**

新增 `renderModelStatus()`，并从以下现有路径调用：`initialize()`、`selectAgent()`、`selectSession()`、`prepareOutput()`、`render()`、`connectionExpired()`。禁止根据按钮文案或 DOM class 反推状态。

- [x] **Step 4: 验证 transcript、模型真源和输入行为**

```zsh
$TASK_NODE tests/browser_tools.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_workbench.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_visual_redesign.cjs
```

Expected: 持久 Session 使用 `agent_snapshot.model.name`；新会话前使用当前 Agent 模型；运行中和待确认即时变化；Enter 发送、Shift+Enter 换行、取消和分页不回退。

## Task 6: 建立真实工作状态栏与覆盖式运行详情

**Files:**

- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/tests/browser_tools.cjs`
- Modify: `local-agent/tests/browser_workbench.cjs`
- Modify: `local-agent/tests/browser_visual_redesign.cjs`

- [x] **Step 1: 新增无副作用的工作栏投影**

`projectWorkRail(sessionId, liveRun, historyRuns, runDetails)` 只输出三组：

```js
{
  waiting: liveRun?.state === 'waiting_approval' ? [liveRun] : [],
  running: ['queued', 'running'].includes(liveRun?.state) ? [liveRun] : [],
  completed: newestCompletedRun ? [newestCompletedRun] : [],
}
```

每组最多保留当前实现真实支持的一个重点项；不伪装为并行任务看板。完成项可从摘要显示，Artifact 只有在 `runDetails` 中存在持久 `id` 时才显示操作。

- [x] **Step 2: 只补取最近完成 Run 的详情**

Session 历史载入后，仅对最新 completed Run 调用现有 `loadRunDetail(runId, {inspect: false})`；沿用 `activeSessionId + viewGeneration` 防迟到保护。不得为所有历史 Run 批量请求详情。

- [x] **Step 3: 将旧 Inspector 改成覆盖式详情 Sheet**

保留现有 `renderInspector(job)` 数据来源，把内容挂入 `#run-detail-sheet`：执行概览、只读审批历史、Artifact、引用和默认折叠 Trace。打开历史 Run 只更新 `inspectedRunId`；审批、拒绝、取消仍严格捕获 `liveRunId + approval_id`。关闭后焦点回到原触发按钮。

- [x] **Step 4: 运行停止点 B 验证**

```zsh
$TASK_NODE --check local_agent/static/app.js
$TASK_NODE tests/browser_tools.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_workbench.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_visual_redesign.cjs
```

Expected: 查看旧 Run 时，右栏 live 决策仍指向当前 Run；完成项和文件不伪造；详情 Sheet 可关闭并正确归还焦点。

## Task 7: 将决策、交付和管理能力接回新挂载点

**Files:**

- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/tests/browser_tools.cjs`
- Modify: `local-agent/tests/browser_imports.cjs`
- Modify: `local-agent/tests/browser_extensions.cjs`

- [x] **Step 1: 重挂载既有决策卡**

继续复用 `renderDecisionCard()` 与审批锁，不复制新的审批模块。live 卡放在对应 Run 下并在右栏提供入口；历史审批只读。视觉使用暖白底、赤陶左标、完整预览默认折叠和唯一主行动。

- [x] **Step 2: 重挂载文件卡与全画布预览**

继续复用 `renderArtifactCard()` 和 `artifactPreviewController`。只有持久 Artifact ID 显示预览／下载；文本使用严格 UTF-8、`textContent` 写入 `<pre>`；SHA-256 放入折叠校验信息。预览最大宽 `1080px`、最大高 `calc(100dvh - 64px)`。

- [x] **Step 3: 把资料、Agent、Skill、MCP 和任务参数迁入设置层**

导入资料从 Session 栏和 Composer 均可进入同一现有导入流程；Agent/Skill/MCP 管理进入设置 Sheet；文件路径、目录模式、输出路径与运行选项进入 `任务设置`。只迁移入口和布局，不改变任何请求体、权限或保存行为。

- [x] **Step 4: 运行安全与管理回归**

```zsh
$TASK_NODE tests/browser_tools.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_imports.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_extensions.cjs
```

Expected: 导入、Agent/Skill/MCP 管理、审批互斥、Artifact 认证下载、严格 UTF-8 和脚本样文本不执行全部 PASS。

## Task 8: 完成响应式、可访问性和固定视觉核对

**Files:**

- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/tests/browser_visual_redesign.cjs`
- Modify: `local-agent/tests/browser_web.cjs`

- [x] **Step 1: 固定四个必验视口的行为**

`browser_visual_redesign.cjs` 依次验证：

| 视口 | 必须看见 | 不应常驻 |
|---|---|---|
| 1440×900 | Agent 轨、Session 栏、对话、工作栏、Composer 模型状态 | 完整 Trace、能力清单 |
| 1024×768 | Agent 轨、对话、模型状态；Session/工作栏按钮可打开浮层 | 固定 Session 栏、固定工作栏 |
| 768×1024 | 同上，触控按钮至少 44px | 页面级横向滚动 |
| 375×812 | 顶栏 Agent 切换、单栏对话、模型状态、发送/待确认动作 | 固定 Agent 轨、技术详情首屏 |

- [x] **Step 2: 核对键盘、焦点和动态播报**

断言：skip link 到问题输入；所有图标按钮有名称；`:focus-visible` 可见；Sheet 关闭后焦点归还；动态运行状态使用 `aria-live="polite"`；错误使用 `role="alert"`；`prefers-reduced-motion` 下无非必要动画。

- [x] **Step 3: 生成一次固定截图并与确认稿做结构对照**

由 `browser_visual_redesign.cjs` 生成：

```text
artifacts/workbench-redesign-1440.png
artifacts/workbench-redesign-1024.png
artifacts/workbench-redesign-768.png
artifacts/workbench-redesign-375.png
```

人工只核对结构、Token、信息密度和注意力顺序，不做逐像素比较。若发现偏差，先补一条可复现断言，再做最小修复；同一状态不重复截图。

## Task 9: 完整前端回归、状态同步与最终 Review Gate

**Files:**

- Modify: `docs/superpowers/plans/2026-09-20-local-agent-workbench-visual-redesign.md`
- Modify: `docs/superpowers/specs/2026-09-19-local-agent-workbench-decision-delivery-design.md`
- Modify: `docs/superpowers/plans/2026-09-19-local-agent-workbench-decision-delivery.md`
- Modify: `local-agent/trial/directory-check.md`
- Modify: `local-agent/README.md`

- [x] **Step 1: 运行受影响的完整前端证据链**

```zsh
$TASK_NODE --check local_agent/static/app.js
$TASK_NODE tests/browser_tools.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_workbench.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_visual_redesign.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_imports.cjs
NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_extensions.cjs
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8768 --missing' --port 8768 -- \
  env NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_web.cjs
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 -- \
  env NODE_PATH=$TASK_NODE_MODULES $TASK_NODE tests/browser_sessions.cjs
```

Expected: 全部 PASS、无 `pageerror`、无模型网络调用。因为没有后端改动，不重复完整 Python 回归；若浏览器测试暴露服务契约疑点，只运行对应的最小 `unittest`，不得直接扩大为全套。

- [x] **Step 2: 核对静态安全、文档一致性和工作区边界**

在仓库根目录执行：

```zsh
git diff --check
rg -n "等待用户确[认]|设计待确[认]|BLOCK（等待用户确[认]）" \
  docs/superpowers/specs/2026-09-19-local-agent-workbench-decision-delivery-design.md \
  docs/superpowers/plans/2026-09-20-local-agent-workbench-visual-redesign.md \
  local-agent/trial/directory-check.md
rg -n "\bTO[D]O\b|\bTB[D]\b|待[补]" \
  docs/superpowers/specs/2026-09-19-local-agent-workbench-decision-delivery-design.md \
  docs/superpowers/plans/2026-09-20-local-agent-workbench-visual-redesign.md
git status --short
```

Expected: `git diff --check` 无输出；没有陈旧待确认或占位符；只解释本计划触及的文件，不覆盖或暂存用户既有改动。

- [x] **Step 3: 更新权威状态和用户启动说明**

只按真实证据更新本计划 §0、规格 §0/§13、旧计划替代说明、`directory-check.md`。`README.md` 只更新受新布局影响的入口名称和正确 HTTP 打开方式；不得再次把 `static/index.html` 文件链接当应用入口。

- [x] **Step 4: 执行三层最终 Review Gate**

逐项记录以下五项：视觉骨架、Agent/Session 隔离、模型状态真源、live/inspected 动作隔离、Artifact/安全与响应式。每项必须依次写明判定项名称、实际运行的命令／断言／截图或错误、用户实际会看到什么及证据边界、是否可交付及剩余限制、严重级别（P0／P1／P2／P3／无）。

缺少任一层时 Gate 不完整，不得标 PASS。

- [x] **Step 5: 在固定停止条件收口**

全部固定检查通过、四张视口截图与确认稿结构一致后，标记“代码完成／离线验证完成／真实模型未调用／用户最终验收待进行”，交付启动命令实际回显的 loopback HTTP 地址；当前开发入口为 `http://127.0.0.1:8772/`，交付前必须重新核对。不要继续追加日历、Todo、多 Agent 协同、第二轮同义审查或真实 DeepSeek 回归。

## 实施停止点

- **A — 导航骨架：** Task 1–4 完成。只确认新骨架与 Agent/Session 隔离，不宣称整体完成。
- **B — 核心工作台：** Task 5–7 完成。确认 Composer、模型状态、工作栏、详情、决策和交付接线。
- **C — 最终验收：** Task 8–9 完成。固定视口、全前端回归、三层 Gate 与 HTTP 入口齐全后停止。

任务共享同一组 `index.html/app.css/app.js`，必须顺序执行；不适合拆给多个并行代理。若用户以后明确授权多代理，也只能把截图核对或独立测试审阅拆出去，不能让多个代理同时改这三份产品文件。
