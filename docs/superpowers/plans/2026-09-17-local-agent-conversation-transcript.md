# Local Agent Continuous Conversation Transcript Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有会话工作台中把同一 Session 的多轮用户问题与最终回答按时间顺序持续显示，同时保证轮询、审批、取消、历史检查和分页不会串到错误的 Run。

**Architecture:** 继续复用现有 Session Run 摘要分页接口和单 Run 详情接口，不改数据库、Runtime、Context 或 Provider。中栏以 Run 为单位渲染语义化时间线；`liveRunId` 只代表当前在途执行，`inspectedRunId` 只代表右栏正在查看的历史轮次。Run 详情按需加载并以 Session、Run、视图代次为键去重，跨 Session 的迟到结果直接丢弃。

**Tech Stack:** 原生 HTML/CSS/JavaScript、Python 标准库 HTTP 服务、Node.js `assert`、Playwright/Google Chrome。

---

## §0 当前进度与停止条件

2026-09-17：设计主线 Gate 已完成。初审发现“执行中的 Run”和“正在查看的 Run”共用状态、以及打开历史轮会错误递增页面代次两项 P1；设计已修订并由同一审阅者复核为 PASS。当前进入 TDD 实施。

完成条件：同一 Session 三轮问答同时可见且刷新后仍在；历史详情乱序、Run 续页和 A/B Session 快速切换不串线；新 Run 等待审批时可查看旧轮，但审批、拒绝和取消仍只作用于新 Run；既有会话、工具、Skill/MCP 与响应式浏览器回归通过。达到这些条件后收口，不增加流式 token、跨会话 Memory、消息编辑、批量 transcript 后端接口或真实 DeepSeek 调用。

## 文件职责

| 文件 | 职责 |
|---|---|
| `local-agent/local_agent/static/index.html` | 时间线容器、空状态和现有单 Run 控件的稳定挂载点 |
| `local-agent/local_agent/static/app.js` | transcript 状态、详情缓存、执行/检查分离、分页锚点和渲染 |
| `local-agent/local_agent/static/app.css` | 纵向问答卡、状态、选中态和响应式布局 |
| `local-agent/tests/browser_workbench.cjs` | 合成接口下的多轮、乱序详情、分页、审批目标和跨 Session 竞态 |
| `local-agent/tests/browser_sessions.cjs` | 持久 Session 的真实本地夹具、刷新与多轮连续展示 |
| `local-agent/trial/directory-check.md` | 当前阶段结论和验证证据入口 |

### Task 1: 用失败测试固定连续 transcript 契约

**Files:**
- Modify: `local-agent/tests/browser_workbench.cjs`
- Modify: `local-agent/tests/browser_sessions.cjs`

- [ ] **Step 1: 改写工作台断言，要求所有轮次同时存在**

在合成夹具中断言 `#session-history article[data-run-id]` 按旧到新排列，每张卡都有用户问题；至少三张卡加载后同时显示各自的 `data-role="assistant"` 最终回答。点击旧卡只改变 `aria-current` 和右栏内容，不能删除其他卡。

- [ ] **Step 2: 固定执行态与检查态隔离**

让新提交进入 `waiting_approval`，再点击旧卡；记录审批与取消请求 URL，断言它们仍指向新 Run。旧轮只能在右栏显示历史详情，不能把旧审批变成可执行操作。

- [ ] **Step 3: 固定并发和分页行为**

分别延迟两条旧 Run 详情和第二页 Run 摘要，倒序释放；断言详情回到各自卡片、分页仍能完成。切换到另一个 Session 后再释放旧详情，断言旧内容不会进入新会话。加载更早记录时记录首个可见卡片位置，加载后位置偏差不超过 2px。

- [ ] **Step 4: 固定持久会话行为**

在 `browser_sessions.cjs` 中完成三轮后断言三个问题和回答同时存在；刷新后仍同时存在，且刷新不增加 Run POST。

- [ ] **Step 5: 运行测试并确认按预期失败**

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  node tests/browser_workbench.cjs
```

Expected: 因当前 `#session-history` 仍是按钮列表、只显示一个 `#answer-text` 而 FAIL；不能是夹具、语法或 Chrome 启动错误。

### Task 2: 实现时间线与按需详情

**Files:**
- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`

- [ ] **Step 1: 建立 transcript 状态模型**

将 `activeId` 拆为：

```javascript
let liveRunId = null, liveRunRevision = -1;
let inspectedRunId = null, inspectionToken = 0;
const runDetails = new Map(), detailRequests = new Map();
```

`viewGeneration` 只在切换 Session、切换 Agent或清空持久会话视图时递增。同一 Session 内打开卡片、加载详情和提交新 Run 不递增。

- [ ] **Step 2: 渲染纵向时间线骨架**

摘要按时间从旧到新生成 `article[data-run-id]`。每张卡始终显示用户问题、时间和文字状态；终态详情只从已验证的 `result.answer.answer` 渲染助手正文。失败、取消、中断和待确认显示明确文字状态，动态内容全部通过 `textContent` 写入。

- [ ] **Step 3: 按需加载详情并更新原卡**

实现以 `sessionId + runId + viewGeneration` 为键的 `loadRunDetail`。最新 Run 自动加载；卡片进入视口或被选择时加载；相同请求复用 Promise。迟到详情只在 Session 和视图代次仍匹配时进入 `runDetails`，然后更新对应卡和当前右栏。

- [ ] **Step 4: 保留现有单 Run 控件**

现有审批、进度、失败和回答 DOM 继续服务 `liveRunId`，并挂入当前执行卡；历史卡使用轻量摘要。右栏引用、产物和执行记录由 `inspectedRunId` 的详情渲染。检查历史轮不得更改执行控件的目标。

- [ ] **Step 5: 实现顶部续页锚点**

加载前记录当前首个可见卡的 `getBoundingClientRect().top`，合并旧摘要并重渲染后用差值调整 `scrollTop`。Run ID 去重；续页请求继续受 Session 和 `viewGeneration` 约束。

- [ ] **Step 6: 完成 CSS 与可访问状态**

把横向 pill 列表改为纵向 `article` 时间线；用户/Agent 标签可读，当前运行、失败和选中态同时使用文字或图标，不只依赖颜色。390、1024、1440px 下输入区可见、时间线内部滚动且页面无横向溢出。

- [ ] **Step 7: 运行 Task 1 两个浏览器测试至通过**

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node tests/browser_workbench.cjs
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node tests/browser_sessions.cjs
```

### Task 3: 关闭执行控制和竞态回归

**Files:**
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/tests/browser_workbench.cjs`
- Modify: `local-agent/tests/browser_sessions.cjs`

- [ ] **Step 1: 让轮询只跟随 `liveRunId`**

轮询、取消、审批和中断继续均捕获 `liveRunId + activeSessionId + viewGeneration`；Run 终态后更新相应卡、清空 live 状态，再刷新第一页摘要。查看旧轮只改变 `inspectedRunId/inspectionToken`。

- [ ] **Step 2: 让检查器只跟随选择代次**

点击卡片时增加 `inspectionToken`。右栏只接受仍匹配的选择结果；详情缓存仍可供其他卡片渲染，不因选择变化作废。

- [ ] **Step 3: 跑竞态与审批断言**

Run Task 2 Step 7。Expected: 乱序详情、分页、跨 Session、审批/取消目标和历史只读断言全部 PASS。

### Task 4: 回归、记录和交付

**Files:**
- Modify: `local-agent/trial/directory-check.md`
- Modify only if wording is stale: `local-agent/README.md`

- [ ] **Step 1: 运行静态和受影响回归**

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
node --check local_agent/static/app.js
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node tests/browser_workbench.cjs
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node tests/browser_sessions.cjs
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node tests/browser_web.cjs
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node tests/browser_tools.cjs
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules node tests/browser_extensions.cjs
python3 -W error::ResourceWarning -m unittest discover -s tests
git diff --check
```

- [ ] **Step 2: 做一次页面级核对**

在本地合成夹具或现有 8772 服务刷新页面，检查三轮可见、历史检查、当前审批、加载更早和 390/1024/1440px 布局。只使用本地数据，不调用 DeepSeek。

- [ ] **Step 3: 更新唯一当前状态入口**

在 `trial/directory-check.md` 顶部记录：实现范围、关键证据、仍后置事项和本轮没有真实模型调用。README 仅在现有玩法说明与实际页面不一致时修改。

- [ ] **Step 4: 提交并推送当前分支**

只暂存本计划列出的文件与已经确认的设计文档；不纳入工作区既有未跟踪文件。提交前再次确认 `git status --short` 和 `git diff --cached --check`，然后推送 `LocalAgentRuntime`。
