# Local Agent Workbench Decision and Delivery UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不修改 Runtime、Session/Run 协议、权限或存储 schema 的前提下，把现有会话工作台升级为可快速判断“任务是否接收、现在是否需要我、最终交付了什么”的任务回执、决策卡、Artifact 文件卡和安全文本预览。

**Architecture:** 保留现有三栏、`liveRunId`/`inspectedRunId` 分离和认证下载入口。在 `app.js` 内建立五个逻辑 Module：纯派生的 Run 回执和 Session 注意力投影、复用现有审批目标的决策卡视图、只接受持久 Artifact ID 的文件卡，以及带中止/代次校验的文本预览控制器。DOM 仍使用原生 HTML/CSS/JavaScript；动态正文只经 `textContent` 写入。

**Tech Stack:** Python 标准库本地服务、原生 HTML/CSS/JavaScript、Node.js `vm` 合成 DOM 检查、Playwright/Chrome 本地合成浏览器验收、`unittest` 既有后端证据。

---

## §0 当前进度与边界

2026-09-20 视觉纠偏：本计划的功能实现证据继续有效，但其“保留现有三栏与旧视觉系统”的前提已被用户实际验收否定，**不得再作为当前视觉实现依据**。[视觉重设计规格](../specs/2026-09-19-local-agent-workbench-decision-delivery-design.md)和[高保真对比稿](../mockups/2026-09-20-local-agent-workbench-comparison.html)已经用户确认，并已由[新视觉重构实施计划](2026-09-20-local-agent-workbench-visual-redesign.md)完成整体前端替换、固定离线验证和最终代码 Gate。本段只撤回旧视觉完成结论，不改写下方当时已经发生的实现和测试事实。

2026-09-20：**产品实现与固定离线 Gate PASS；首次用户交付入口 FAIL，现已纠正并完成真实启动验收**。Task 1–5 已完成 Run 回执、独立检查按钮、当前 Session `运行中/待确认`、统一决策卡、真实 Artifact 文件卡和严格 UTF-8 纯文本预览。JavaScript 语法、`browser_tools.cjs`、`browser_workbench.cjs`、`browser_sessions.cjs` 与 `git diff --check` 均退出 0；390×844、1024×768、1440×900 固定截图已核对。首次交接错误地把 `static/index.html` 作为可点击文件交给用户，用户因此通过 `file://` 打开源码；绝对 `/app.css`、`/app.js`、服务端 Session Token 和 API 均未加载，得到裸 HTML。纠正后已确认现有服务运行于 `http://127.0.0.1:8772`，首页、CSS、JS 均 HTTP 200，Chrome 实际显示完整三栏工作台。产品代码未因该交付错误修改；本批没有调用 DeepSeek。正式设计见[工作台任务回执、决策与交付 UI 设计](../specs/2026-09-19-local-agent-workbench-decision-delivery-design.md)。

当前没有未解决产品阻塞。Task 1–6 的固定断言、三种视口核对、强制 Review Gate 三层回执和真实 HTTP 启动入口核对均已完成；原交付链接错误保留为失败事实。本批按既定停止条件收口，不自动扩大到跨重启未读、后端改造、第二轮同义评审或真实模型调用。

本批包含：

- Run 回执及全部既有状态的准确映射；
- 当前客户端已知 Session 的 `运行中/待确认` 被动提示；
- live/历史决策卡的统一信息层级，且执行目标继续绑定 `liveRunId`；
- 持久 Artifact 文件卡、认证下载和 `.md/.txt` 严格 UTF-8 纯文本预览；
- 390×844、1024×768、1440×900 的交互、焦点、溢出和竞态验收。

本批不包含：

- 后端接口、SQLite schema、Run 状态、Todo/Cron/日历或并行 Run；
- 跨重启未读、全 Session 待办或每个 Session 额外请求；
- Markdown/HTML 渲染、PDF/Word/图片预览、外部 UI 依赖；
- Prompt、Provider 或真实 DeepSeek 回归。

若实现中必须修改 `web_runs.py`、`sessions.py`、数据库迁移或认证协议，立即停止并将 Gate 标为 `BLOCK`；不得把后端扩展混入本计划。

## 实施共用命令

以下命令均在 `/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent` 执行：

```zsh
NODE_BIN=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
NODE_MODULES=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules
```

文中写作 `$NODE_BIN` 和 `$NODE_MODULES` 仅为可读性；实际执行前在同一 shell 中定义这两个任务专用变量，不使用或覆盖 `$HOME`、`$CODEX_HOME` 等系统变量。

## Task 1: 建立 Run 回执投影并修正 Run 卡语义

**Files:**

- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/tests/browser_tools.cjs`
- Modify: `local-agent/tests/browser_workbench.cjs`

- [x] **Step 1: 先写状态矩阵和语义失败测试**

在 `browser_tools.cjs` 直接调用全局纯函数 `projectRunReceipt(summary, detail, context)`，为以下状态建立表驱动断言：

```js
const cases = [
  ['queued', '已接收', 'neutral'],
  ['running', '正在处理', 'info'],
  ['waiting_approval', '需要你确认', 'warning'],
  ['unable', '未完成', 'danger'],
  ['validation_failed', '校验失败', 'danger'],
  ['timed_out', '已超时', 'danger'],
  ['cancelled', '已取消', 'neutral'],
  ['interrupted', '已中断', 'warning'],
  ['failed', '失败', 'danger'],
];
```

另测两个 `completed`：有 `result.answer` 时才是 `已完成/success`；没有有效答案时必须是非成功 `未完成/danger`。详情未加载时不得猜测引用、Artifact 或步骤数量。

在 `browser_workbench.cjs` 把旧断言：

```js
assert.equal(await run04Card.getAttribute('role'), 'button');
```

改为：Run `article` 没有 `role="button"` 和 `tabindex`；每张卡有一个 `.run-inspect` 真按钮，Enter/Space 通过该按钮打开右栏。`.run-detail-retry` 仍是独立按钮，不能触发选择。

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
$NODE_BIN tests/browser_tools.cjs
NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_workbench.cjs
```

Expected: 两项均按预期失败；第一项提示 `projectRunReceipt` 不存在，第二项提示 `.run-inspect` 或新语义不存在。此失败是 TDD 基线，不是产品回归结论。

- [x] **Step 2: 实现纯 `RunReceiptProjector`**

在 `runStateLabel` 附近新增：

```js
function projectRunReceipt(summary, detail, {liveRunId, continuableRunId} = {}) {
  // return {runId, state, label, message, tone, isLive, needsAction,
  //   citationCount, artifactCount, stepCount, canContinue}
}
```

实现必须满足：

- 只从 `summary`、已加载 `detail` 和显式 context 派生，不读取 DOM、发请求或修改全局状态；
- `completed` 必须同时存在有效 `detail.result.answer` 才返回 success；
- `citationCount` 从最终答案引用取值，`artifactCount` 只数 `result.artifacts`，`stepCount` 只数真实事件；详情未知时返回 `null` 而不是零；
- 停止原因只使用 `messageFor(code)` 的既有安全映射，不猜测原因；
- `canContinue` 只有 `interrupted` 且 run ID 等于 `continuableRunId`。

- [x] **Step 3: 让 transcript 卡消费同一个投影**

在 `createRunCard(item)` 中：

- 用 `projectRunReceipt` 生成顶部 `.run-receipt`，包括文字标签、事实说明和已知数量；
- 移除 `article` 的 `role="button"`、`tabIndex`、整卡 click/keydown；
- 新增 `button.run-inspect`，明确文案为“查看本轮依据与执行记录”，点击时只调用 `openSessionRun(item.id)`；
- `markCurrentRun` 仍以 `article[data-run-id]` 标记 `aria-current`，但焦点落到 `.run-inspect`；
- `openSessionRun` 和终态焦点恢复改为聚焦 `.run-inspect`，没有按钮时不抛错；
- `replaceRunCard` 不再复制已经删除的 article role/aria-label，并继续保留滚动锚点和子按钮 listener。

`transcriptStatus` 若与投影重复，收缩为投影内部 helper；不要保留两套状态文案。

- [x] **Step 4: 加入最小样式**

在 `app.css` 增加 `.run-receipt`、`.run-receipt-label`、`.run-receipt-message`、`.run-receipt-counts` 和 `.run-inspect`。要求：状态同时有文字和色调、历史 Run 无动画、按钮 44px、窄屏不横向溢出。不要改变现有三栏宽度和断点。

- [x] **Step 5: 运行 Task 1 定向检查**

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
$NODE_BIN --check local_agent/static/app.js
$NODE_BIN tests/browser_tools.cjs
NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_workbench.cjs
```

Expected: 10 个状态、无有效答案的 completed、独立选择按钮、重试不误选和既有乱序详情断言全部 PASS。

## Task 2: 增加当前 Session 注意力投影

**Files:**

- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/tests/browser_workbench.cjs`
- Modify: `local-agent/tests/browser_sessions.cjs`

- [x] **Step 1: 先写标记生命周期失败测试**

在 `browser_workbench.cjs` 的 `approval-run` 路径断言：

1. POST 返回 `running` 后，当前 `session-01` 行出现 `.session-attention` 和 `运行中`；
2. 轮询进入 `waiting_approval` 后，同一标记变成 `待确认`；
3. 查看旧 Run 时标记仍属于 `session-01`；
4. 批准或取消到终态后标记消失；
5. 页面刷新后，仅凭 Session 列表不得恢复标记。

在 `browser_sessions.cjs` 追加“刷新持久历史后没有 `.session-attention`”断言，证明第一阶段没有伪造跨重启未读。

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_workbench.cjs
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 -- \
  env NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_sessions.cjs
```

Expected: `.session-attention` 不存在而失败；刷新无标记的负断言可以先通过。

- [x] **Step 2: 实现纯 `SessionAttentionProjector`**

新增：

```js
function projectSessionAttention(sessionId, liveRun) {
  // null | {label: '运行中' | '待确认', tone: 'info' | 'warning'}
}
```

规则固定为：相同 `session_id` 的 `queued/running` 返回 `运行中`，`waiting_approval` 返回 `待确认`，其余状态或无 live run 返回 `null`。

- [x] **Step 3: 维护最小 live 投影状态**

增加 `let liveRunSnapshot = null`，只保存 `{id, session_id, state}`：

- `prepareOutput(job)` 设置初始 snapshot 并重绘 Session 列表；
- `render(job)` 在轮询接收新状态后更新 snapshot；终态先清空再重绘；
- `clearRunView`、切换 Agent/Session 和连接失效时清空；
- 不写 `localStorage`，不合并进 `visibleSessions`，不从历史摘要恢复。

在 `renderSessionList()` 中只将投影结果追加为文字 `<span class="session-attention">`。现有 `busy` 期间禁用 Agent/Session 导航的规则不变。

- [x] **Step 4: 运行 Task 2 定向检查**

重复 Step 1 两条命令。Expected: 生命周期、旧 Run 检查隔离、终态移除、刷新不伪造和跨 Session 迟到保护全部 PASS。

## Task 3: 把 live 与历史审批统一为决策卡

**Files:**

- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/tests/browser_tools.cjs`
- Modify: `local-agent/tests/browser_workbench.cjs`
- Modify: `local-agent/tests/browser_sessions.cjs`

- [x] **Step 1: 先写字段、模式和目标隔离失败测试**

保留现有审批锁和目标断言，并新增：

- live 卡有 `data-decision-mode="live"`，展示 action/path/bytes/create/source/risk/intent/content/countdown；
- historical 卡有 `data-decision-mode="historical"`、明确“仅供查看”，且没有 allow/deny 按钮；
- 查看旧 Run 后允许、拒绝、取消仍请求 `liveRunId + approval_id`；
- 超时、Session 失效、提交中和取消中都保持两按钮禁用；
- 所有正文仍由 `textContent` 显示，测试中的 `<script>` 不执行。

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
$NODE_BIN tests/browser_tools.cjs
NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_workbench.cjs
```

Expected: 新 `data-decision-mode` 和统一文案断言失败；既有目标/锁断言仍应通过。

- [x] **Step 2: 提取 `DecisionCardView`，不重写审批状态机**

新增 `renderDecisionCard(approval, mode, handlers)`，内部复用现有稳定 DOM 挂载点并返回对应根元素。`mode` 只能是 `live` 或 `historical`：

- live 使用现有 `#approval`、`#approval-allow`、`#approval-deny`，handler 仍由当前 `liveRunId` 注入；
- historical 使用 `#approval-history`，只渲染同样的事实字段和完整预览，不创建动作按钮；
- `renderApproval(job)` 决定 live 数据源，`renderApprovalHistory(job)` 决定 inspected 数据源；两者不得互相赋值；
- `updateApprovalControls` 继续作为倒计时和锁定实现，不复制第二套 timer；
- 继续保留既有 ID，以减少无关测试和可访问关联变化。

- [x] **Step 3: 调整决策层级与可访问文案**

在 HTML/CSS 中把两类卡统一为 `.decision-card`：动作摘要为标题下第一事实，目标/大小/新建不覆盖/来源/风险为详情，模型意图明确标为辅助信息，完整内容默认可展开。live 卡的 warning 标签和操作按钮保持强视觉；historical 卡降为只读色调。

- [x] **Step 4: 运行 Task 3 定向检查**

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
$NODE_BIN --check local_agent/static/app.js
$NODE_BIN tests/browser_tools.cjs
NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_workbench.cjs
```

Expected: live/historical 字段、按钮锁、完整正文、安全文本、焦点和目标隔离全部 PASS。

## Task 4: 实现真实 Artifact 文件卡与安全文本预览

**Files:**

- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/tests/browser_tools.cjs`
- Modify: `local-agent/tests/browser_workbench.cjs`

- [x] **Step 1: 先写 Artifact 真伪和预览失败测试**

扩展 `browser_tools.cjs` 的 synthetic DOM，使 `Element` 支持测试所需的 `showModal()`、`close()`、`open`、`contains()` 与焦点记录；不要为生产代码增加测试专用分支。

新增断言：

- 没有 `artifact.id` 的临时 receipt 无预览/下载动作；
- 有持久 ID 的 `.md/.txt` 同时显示预览和下载，未知扩展名只显示下载；
- 两个动作都请求精确的 `/api/sessions/{session}/runs/{run}/artifacts/{id}/download`，带 `X-Session-Token` 和 `X-Agent-ID`；
- 预览 `<script>window.injected=1</script>` 后只出现在 `<pre>` 文本里，`window.injected` 仍未定义；
- 非法 UTF-8 触发“不是有效 UTF-8”，不显示替换字符；
- Artifact 已创建而 Run 后续失败/取消时卡仍保留；
- 切换 Session、切换 Agent、关闭 dialog 或 `viewGeneration` 改变后，迟到响应不得写入预览。

在 `browser_workbench.cjs` 的合成路由增加一个真实 Artifact 下载响应，覆盖原生 dialog 的打开、关闭、焦点返回、390px 无溢出和内容不执行。

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
$NODE_BIN tests/browser_tools.cjs
NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_workbench.cjs
```

Expected: 预览 dialog、严格解码和中止代次断言失败；既有认证下载断言仍通过。

- [x] **Step 2: 添加稳定的原生 dialog 挂载点**

在 `index.html` 的 import dialog 前新增 `#artifact-preview-dialog`，包含：

- `#artifact-preview-title` 文件名；
- `#artifact-preview-meta` 相对路径、字节数和校验状态；
- `#artifact-preview-status` 的 `role="status"`；
- `#artifact-preview-error` 的 `role="alert"`；
- `pre#artifact-preview-content`；
- `#artifact-preview-download` 与 `#artifact-preview-close`。

使用 `aria-labelledby` 和 `aria-describedby`；关闭按钮在标题区，下载按钮在底部。不要新增外部库。

- [x] **Step 3: 提取共享认证读取函数**

把 `downloadArtifact` 的 fetch 部分提取为：

```js
async function fetchArtifactBlob({sessionId, runId, artifactId, agentId, signal}) {
  // authenticated GET; map server errors with messageFor; return Blob
}
```

`downloadArtifact` 继续创建/revoke Object URL；预览复用同一函数。调用前必须具备 `activeSessionId`、Agent ID 和持久 `artifact.id`。

- [x] **Step 4: 实现 `ArtifactPreviewController`**

控制器公开 `open({sessionId, runId, artifact, agentId, viewGeneration, trigger})` 和 `close()`：

- 每次 open 先 abort 上一个 controller，记录 session/run/artifact/agent/viewGeneration；
- 使用 8 秒 timeout 和 `AbortController`；
- blob 转 `ArrayBuffer` 后用 `new TextDecoder('utf-8', {fatal: true})`；
- 只通过 `textContent` 写入 `<pre>`；先清空旧正文再显示 loading；
- 写入前重新比较 active Session、Agent、viewGeneration 和 controller identity；
- close、Session/Agent 切换、`clearRunView` 时 abort 并清空正文；
- dialog 关闭后把焦点还给仍在 DOM 中的 trigger；
- `SESSION_EXPIRED/RUN_NOT_FOUND` 调用既有连接失效处理；完整性错误沿用服务端错误文案并保留文件卡。

- [x] **Step 5: 让 `ArtifactCardView` 只消费真实 Artifact**

将 `renderArtifacts(job)` 内的单卡构造提取为 `renderArtifactCard(runId, artifact, handlers)`：

- 始终显示文件名、相对路径、字节数、已新建和可折叠 SHA-256；
- 只有持久 ID 才显示下载；只有 ID 且扩展名为 `.md/.txt` 才显示预览；
- 不解析回答中的路径，不因批准决定提前生成卡；
- Run 非成功但 Artifact 已成立时保留卡，并沿用“文件已生成，后续步骤未完成/取消”的说明。

- [x] **Step 6: 完成 dialog 与文件卡样式并复跑**

复用 `.import-dialog` 的遮罩、尺寸和移动端安全区规则，新增 `.artifact-card`、`.artifact-actions`、`.artifact-preview-dialog`、`.artifact-preview-content`。文本可横向滚动但页面级不得横向溢出；两个动作都保持 44px。

重复 Step 1 两条命令。Expected: 真伪、认证头、严格 UTF-8、纯文本、abort/代次、焦点和移动端断言全部 PASS。

## Task 5: 完成三视口集成与有界回归

**Files:**

- Modify: `local-agent/tests/browser_workbench.cjs`
- Modify only if a real persistent regression requires it: `local-agent/tests/browser_sessions.cjs`
- Modify only for defects exposed by these tests: `local-agent/local_agent/static/index.html`
- Modify only for defects exposed by these tests: `local-agent/local_agent/static/app.js`
- Modify only for defects exposed by these tests: `local-agent/local_agent/static/app.css`

- [x] **Step 1: 固定完整用户路径**

在 `browser_workbench.cjs` 用现有本地合成路由覆盖一条连续路径：

1. 发送任务，Session 显示运行中；
2. 进入待确认，决策卡聚焦且 Session 显示待确认；
3. 查看旧 Run，live 决策仍可正确批准；
4. 终态回执显示有效答案和真实 Artifact 数量，Session 标记消失；
5. 打开纯文本预览，HTML 仅显示为文本；
6. 关闭后焦点返回预览按钮；
7. 下载仍使用认证请求；
8. 历史审批只读；
9. 390、1024、1440px 无页面级横向溢出，抽屉 inert 和 44px 控件不回退。

- [x] **Step 2: 运行静态与三组受影响回归**

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
$NODE_BIN --check local_agent/static/app.js
$NODE_BIN tests/browser_tools.cjs
NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_workbench.cjs
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 -- \
  env NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_sessions.cjs
```

Expected: 全部 PASS，无 `pageerror`。若只修改了前端，不重复完整 Python 回归；既有 `test_session_web.py` 的 Artifact 权限和完整性证据继续有效。

- [x] **Step 3: 只在共享链路出现风险时扩大浏览器回归**

如果 Task 5 Step 2 暴露初始化、抽屉、导入或能力管理共享链路风险，再运行：

```zsh
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8768 --missing' --port 8768 -- \
  env NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_web.cjs
NODE_PATH=$NODE_MODULES $NODE_BIN tests/browser_extensions.cjs
```

若没有上述风险，不运行本步骤，并在最终技术交接中写明“未触发扩大条件”，不能把未运行写成 PASS。

执行结果：受影响回归未暴露初始化、抽屉、导入或能力管理共享链路风险，因此扩大条件未触发；本步骤按“已完成条件判断、未运行扩大命令”记录，不将未运行项写成 PASS。

- [x] **Step 4: 做一次固定视口人工核对**

使用与 `browser_workbench.cjs` 相同的合成数据查看 1440×900、1024×768、390×844：回执信息层级、live 决策卡、历史审批、文件卡、预览 dialog、焦点可见和页面级溢出。发现问题时先补可复现断言，再做最小修复；同一状态不重复截图。

## Task 6: 更新权威状态并执行强制 Review Gate

**Files:**

- Modify: `docs/superpowers/plans/2026-09-19-local-agent-workbench-decision-delivery.md`
- Modify: `docs/superpowers/specs/2026-09-19-local-agent-workbench-decision-delivery-design.md`
- Modify: `local-agent/trial/directory-check.md`
- Modify only if actual user instructions changed: `local-agent/README.md`

- [x] **Step 1: 核对范围和文档一致性**

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent
git diff --check
rg -n "等待用户审阅|用户确认规格后" \
  docs/superpowers/specs/2026-09-19-local-agent-workbench-decision-delivery-design.md \
  local-agent/trial/directory-check.md
rg -n "\\bTO[D]O\\b|\\bTB[D]\\b|待[补]" \
  docs/superpowers/specs/2026-09-19-local-agent-workbench-decision-delivery-design.md \
  docs/superpowers/plans/2026-09-19-local-agent-workbench-decision-delivery.md \
  local-agent/trial/directory-check.md
git status --short
```

Expected: `git diff --check` 无输出；没有陈旧待确认文案或占位符。`git status` 中只解释本计划涉及文件，不改动或暂存既有无关文件。

- [x] **Step 2: 更新 §0 和唯一当前状态入口**

只按实际证据更新本计划 §0 与 `directory-check.md`：分别记录代码是否完成、离线浏览器是否通过、是否运行扩大回归、是否调用真实模型。README 只有在用户玩法说明确实与新 UI 不一致时才修改。

- [x] **Step 3: 按三层格式逐项执行最终 Gate**

每个可能改变 Gate 或下一步的判定项必须输出：

```text
判定项：<状态映射 / live目标隔离 / Artifact真伪与安全 / 竞态 / 响应式>
技术证据：<精确命令、断言、文件或错误>
大白话解释：<它证明什么、没有证明什么>
决策影响：<产品/用户后果、下一动作、责任人>
严重级别：P0 / P1 / P2 / P3
```

缺少“技术证据／大白话解释／决策影响”任一项时，Gate 不完整，不得标 PASS。例行读文件、搜索和不改变判断的重复成功不逐条扩写。

最终 Gate 记录（2026-09-20）：

| 判定项 | 技术证据 | 大白话解释 | 决策影响 | 严重级别 |
|---|---|---|---|---|
| 状态映射 | `projectRunReceipt` 对 10 类状态及“completed 但无有效答案”有表驱动断言；`browser_tools.cjs` PASS | 页面只把确有最终答案的 Run 说成“已完成”，不会用绿色成功掩盖空结果 | 可以用回执判断本轮是否真的交付；无需补后端状态 | 无 |
| live 目标隔离 | `decideApproval` 固定捕获 `liveRunId`、Session 与 `viewGeneration`；浏览器断言在查看旧 Run 时 POST 仍命中 `approval-run` | 右栏翻看旧记录不会把“批准”误发给旧任务或别的任务 | 审批主路径可交付；不需要用户额外核对目标 | 无 |
| Artifact 真伪与安全 | 只有持久 `artifact.id` 才显示动作；预览复用认证下载端点、严格 UTF-8 解码并经 `textContent` 写入；脚本形文本未执行；相关合成 DOM 与浏览器断言 PASS | 只有实际落盘并由服务端校验的文件才可预览/下载，文件里的 HTML 或脚本只会当文字显示 | `.md/.txt` 预览可用；二进制和未知格式继续只下载，不误猜格式 | 无 |
| 竞态 | 预览、轮询和审批均核对 Run、Session、Agent 与视图代次；迟到预览正文断言 PASS | 切换 Session、Run 或页面状态后，旧请求不会把内容塞进当前视图 | 不需要增加串行锁或新接口；保持现有异步结构 | 无 |
| 响应式与可访问操作 | 390×844、1024×768、1440×900 的 dialog 几何、页面横向溢出、44px 操作区和关闭后焦点恢复断言 PASS，截图人工核对 | 手机、平板和桌面都能打开、阅读、关闭预览，不会被挤出屏幕或丢失键盘焦点 | 三种目标视口可交付；无需扩大视觉重构 | 无 |

Gate 结论：**PASS**。该结论只覆盖本计划的前端投影和固定离线验收，不声称后端、真实 DeepSeek 或未运行的扩大浏览器组已重新通过。

### 用户交付入口复盘（2026-09-20）

- **技术证据：**用户截图地址为本机 `static/index.html` 文件；源码依赖 `/app.css`、`/app.js`、服务端注入 Session Token 和本地 API。真实服务进程占用默认状态库并监听 8772；直连 `/`、`/app.css`、`/app.js` 均返回 HTTP 200，Chrome 在 `127.0.0.1:8772` 显示完整三栏工作台。
- **大白话解释：**实现代码没有以“直接双击 HTML”的方式工作。我把建筑图纸链接当成了正门，用户看到的当然只是没有装修和水电的页面；正确入口是正在运行的本地服务地址。
- **决策影响：**产品实现无需为这次问题返工；交付流程必须修正。以后 UI 只有在正式启动命令和真实浏览器地址验证后才能宣称可交付，源码 HTML 不再作为应用入口链接。
- **责任与严重级别：**责任在助手的验收与交付判断；首次用户交付为 P1，已通过纠正入口解除。原固定离线 Gate 仍作为实现证据，不冒充此前用户验收通过。

- [x] **Step 4: 生成两份一致的最终交接**

最终输出必须明确分为：

1. **技术交接**：`PASS / CONDITIONAL / BLOCK`、范围、命令、P0–P3、约束、未知和下一动作；
2. **白话简报**：同一 Gate、同一阻塞和严重级别，解释用户能做什么、风险是什么、是否需要用户决定；
3. **一致性检查**：逐项确认两份交接的 Gate、阻塞和推荐没有冲突。

P0/P1 必须 BLOCK；有界 P2 才能 CONDITIONAL；P3 不能单独阻塞。若全部固定路径通过且无 P0/P1，标 PASS 并按停止条件结束，不自动增加第二轮同义评审或真实 DeepSeek 调用。

## 执行方式约束

本计划可在当前任务内逐项执行。若使用多代理或独立实施任务，必须由用户明确提出；没有该授权时按顺序在当前任务内执行，不自动派发。实施前无需再次决定第一阶段 Session 标记范围，用户已经确认。
