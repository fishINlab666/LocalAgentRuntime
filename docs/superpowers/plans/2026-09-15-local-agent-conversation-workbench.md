# Local Agent Conversation Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 Local Agent 长页面改成可持续使用的三栏会话工作台，同时保留工具、审批、会话、Skill、MCP 和证据链的既有行为。

**Architecture:** 后端和 HTTP interface 保持不变。`index.html` 只重排现有控件并增加展示镜像，`app.js` 仍以原有 select/form 为唯一状态来源，再把 Agent、Session 和 Run 渲染为可点击侧栏/时间线；`app.css` 提供雾蓝 × 琥珀三栏和响应式抽屉。新增的浏览器夹具直接加载真实静态文件并伪造 HTTP 返回，以验证分页、迟到响应、抽屉和可访问状态而不调用模型。

**Tech Stack:** Python 3 标准库服务端、原生 HTML/CSS/JavaScript、Node.js `assert`、Playwright/Google Chrome。

---

## §0 当前进度与停止条件

2026-09-15：设计已由用户确认并通过 Gate；实施未开始。当前分支为 `LocalAgentRuntime`，设计提交为 `07b5949`。本轮不改 Runtime、Provider、会话存储、工具、Skill 或 MCP 协议。

完成条件：任务 1–4 的定向浏览器检查和 Python 静态服务检查通过；1440、1024、390px 无页面级横向溢出且核心路径可操作；README 能让用户启动并选择一个真实模块试玩。通过后停止，不追加新后端能力或第二套视觉。

## 文件职责

| 文件 | 职责 |
|---|---|
| `local-agent/local_agent/static/index.html` | 三栏语义结构、现有控件位置、新导航与抽屉入口 |
| `local-agent/local_agent/static/app.css` | 主题 Token、桌面三栏、状态卡、移动抽屉与可访问样式 |
| `local-agent/local_agent/static/app.js` | 现有状态与新展示镜像同步、游标续页、抽屉和焦点行为 |
| `local-agent/tests/browser_workbench.cjs` | 新工作台的独立合成 HTTP 浏览器验收 |
| `local-agent/tests/browser_web.cjs` | 既有端到端行为和状态文案断言 |
| `local-agent/tests/browser_sessions.cjs` | 既有会话隔离、历史和迟到响应保护 |
| `local-agent/tests/browser_extensions.cjs` | 既有 Agent/Skill/MCP 管理与安全渲染保护 |
| `local-agent/tests/test_web.py` | 静态页面标题和安全 Header 的服务端检查 |
| `local-agent/README.md` | 启动方式、模块清单和建议试玩路径 |

### Task 1: 建立三栏工作台骨架

**Files:**
- Modify: `local-agent/tests/test_web.py`
- Create: `local-agent/tests/browser_workbench.cjs`
- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.css`

- [ ] **Step 1: 先写会失败的页面契约检查**

把 `test_page_and_static_assets_are_served_with_browser_guards` 的标题断言改成：

```python
self.assertIn('<title>Local Agent 工作台</title>', page)
for marker in ('data-region="sidebar"', 'data-region="thread"',
               'data-region="inspector"', 'id="sidebar-toggle"',
               'id="inspector-toggle"'):
    self.assertIn(marker, page)
```

新增 `browser_workbench.cjs`，先只实现真实静态文件路由与骨架断言：

```javascript
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  await page.route('http://workbench.test/**', async route => {
    const url = new URL(route.request().url());
    if (['/', '/app.js', '/app.css'].includes(url.pathname)) {
      const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      return route.fulfill({status: 200,
        contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
        body: fs.readFileSync('local_agent/static/' + file, 'utf8')});
    }
    return route.fulfill({status: 200, contentType: 'application/json', body: '{}'});
  });
  try {
    await page.goto('http://workbench.test/', {waitUntil: 'domcontentloaded'});
    for (const region of ['sidebar', 'thread', 'inspector']) {
      assert.equal(await page.locator(`[data-region="${region}"]`).count(), 1);
    }
    assert.equal(await page.locator('.workbench-shell').count(), 1);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
```

- [ ] **Step 2: 运行检查并确认因新结构尚不存在而失败**

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
PYTHONPATH=. python3 -m unittest tests.test_web.WebTests.test_page_and_static_assets_are_served_with_browser_guards -v
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_workbench.cjs
```

Expected: 标题或 `data-region`/`.workbench-shell` 断言 FAIL，而不是语法或环境错误。

- [ ] **Step 3: 重排 HTML，但保留原接口节点**

将页面整理为以下稳定骨架；把原有 Agent/Session 控件放入左栏，`session-history`、输入表单和回答/审批放入中栏，引用/产物/Trace 放入右栏：

```html
<div class="workbench-shell">
  <aside class="sidebar" data-region="sidebar" id="sidebar-panel">…</aside>
  <main class="thread" data-region="thread" id="thread-panel">…</main>
  <aside class="inspector" data-region="inspector" id="inspector-panel">…</aside>
</div>
<button type="button" id="workspace-backdrop" class="workspace-backdrop" aria-label="关闭面板"></button>
```

增加 `sidebar-toggle/sidebar-close/inspector-toggle/inspector-close`，保留原 81 个脚本 ID、表单字段属性、首个临时 Session option、`details#capability-management`、`details#trace` 及所有现有 `data-*` 属性。模型和工具内容节点不写静态 HTML。

- [ ] **Step 4: 实现主题与桌面布局**

在 `app.css` 定义并使用：

```css
:root{
  --canvas:#eef3f8;--panel:#fff;--panel-soft:#f5f8fc;--ink:#172234;
  --muted:#657389;--line:#d7e1ec;--brand:#3f6f99;--brand-strong:#2f587d;
  --accent:#d59642;--accent-soft:#fff3df;--success:#39745b;
  --danger:#a54b4b;--warning:#8a641f
}
.workbench-shell{display:grid;grid-template-columns:272px minmax(480px,1fr) 344px;
  min-height:100dvh;max-height:100dvh;overflow:hidden;background:var(--canvas)}
.sidebar,.thread,.inspector{min-width:0;min-height:0;background:var(--panel)}
.sidebar,.inspector{overflow-y:auto}.thread{display:flex;flex-direction:column;overflow:hidden}
```

表单、会话项、审批、回答、引用、产物和事件继续使用原 class 或兼容新 class；任何可点击控件最小高度 44px，焦点使用蓝色外轮廓。

- [ ] **Step 5: 运行 Task 1 检查至通过并提交**

Run: 重复 Step 2 两条命令，并执行 `git diff --check`。

Expected: 两项 PASS；没有缺失 DOM ID 或页面级横向溢出。

Commit:

```zsh
git add local-agent/local_agent/static/index.html local-agent/local_agent/static/app.css \
  local-agent/tests/test_web.py local-agent/tests/browser_workbench.cjs
git commit -m "Build Local Agent workbench shell"
```

### Task 2: 把 Agent、会话与 Run 映射成可玩的导航

**Files:**
- Modify: `local-agent/tests/browser_workbench.cjs`
- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`

- [ ] **Step 1: 扩展合成路由，先写导航与续页失败断言**

在 `browser_workbench.cjs` 返回三个 Agent、22 个 Session、22 个 Run；首批返回 20 项和签名样例游标，带 `cursor` 时返回剩余 2 项。加入：

```javascript
assert.equal(await page.locator('#agent-list [data-agent-id]').count(), 3);
assert.equal(await page.locator('#session-list [data-session-id]').count(), 20);
await page.locator('#session-load-more').click();
assert.equal(await page.locator('#session-list [data-session-id]').count(), 22);
assert(requests.some(item => item.path === '/api/sessions' && item.cursor === 'sessions-2'));
assert.equal(await page.locator('#session-history button').count(), 20);
await page.locator('#run-load-more').click();
assert.equal(await page.locator('#session-history button').count(), 22);
assert(requests.some(item => item.path.endsWith('/runs') && item.cursor === 'runs-2'));
```

- [ ] **Step 2: 运行并确认因展示镜像和加载按钮尚不存在而失败**

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_workbench.cjs
```

Expected: `#agent-list`、`#session-list` 或加载按钮断言 FAIL。

- [ ] **Step 3: 增加展示节点和唯一状态源**

在 HTML 增加：

```html
<div id="agent-list" class="agent-list" aria-label="助手列表"></div>
<div id="session-list" class="session-list" aria-label="会话列表"></div>
<button type="button" id="session-load-more" class="quiet" hidden>加载更多会话</button>
<button type="button" id="run-load-more" class="quiet" hidden>加载更早记录</button>
<div id="active-capabilities" class="active-capabilities" aria-live="polite"></div>
```

原 `#agent-select`、`#session-select` 继续作为实际表单状态；新按钮只调用 `selectOption` 等价路径或现有 `chooseAgent/selectSession`，不另存业务状态。

- [ ] **Step 4: 实现可复用渲染与游标续页**

在 `app.js` 增加并使用以下 interface：

```javascript
let visibleSessions = [], sessionCursors = {active: null, archived: null};
let runHistoryCursor = null;

function renderAgentList() {}
function renderSessionList() {}
function renderActiveCapabilities() {}
async function loadMoreSessions() {}
async function loadMoreRuns() {}
function markCurrentRun(runId) {}
```

`loadSessions` 首次并行读取 active/archived，保存各自 `next_cursor`；续页只请求非空游标并去重 ID。`selectSession` 保存本会话 `next_cursor`；Run 续页追加而不替换。所有请求捕获当前 `sessionListGeneration/viewGeneration` 和 Agent/Session ID，晚到结果直接丢弃。动态文本仍只赋给 `textContent`。

继续复用现有 `api()`；每个请求仍由它附加 `X-Session-Token` 与当前 `X-Agent-ID`。新渲染函数不得直接 `fetch` 或创建绕过这两个 Header 的请求路径。

- [ ] **Step 5: 通过新检查和既有会话/扩展回归**

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_workbench.cjs
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 -- \
  env NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_sessions.cjs
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_extensions.cjs
```

Expected: 三项 PASS；切换助手/会话、迟到响应保护、Skill/MCP 管理和 XSS 断言不回退。

Commit:

```zsh
git add local-agent/local_agent/static/index.html local-agent/local_agent/static/app.js \
  local-agent/local_agent/static/app.css local-agent/tests/browser_workbench.cjs
git commit -m "Add workbench conversation navigation"
```

### Task 3: 完成 Composer、检查栏和移动抽屉

**Files:**
- Modify: `local-agent/tests/browser_workbench.cjs`
- Modify: `local-agent/tests/browser_web.cjs`
- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`

- [ ] **Step 1: 先增加抽屉、状态与焦点失败断言**

加入：

```javascript
await page.setViewportSize({width: 390, height: 844});
await page.locator('#sidebar-toggle').click();
assert.equal(await page.locator('body').getAttribute('data-drawer'), 'sidebar');
await page.keyboard.press('Escape');
assert.equal(await page.locator('body').getAttribute('data-drawer'), null);
assert.equal(await page.locator('#sidebar-toggle').getAttribute('aria-expanded'), 'false');
await page.locator('#inspector-toggle').click();
assert.equal(await page.locator('body').getAttribute('data-drawer'), 'inspector');
assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
```

在一次等待审批的合成 Run 中断言 `#thread-panel[aria-busy="true"]`、审批标题获得焦点、批准按钮至少 44px；完成后断言当前历史按钮具有 `aria-current="true"`。

- [ ] **Step 2: 运行并确认缺少抽屉状态与焦点行为而失败**

Run: Task 2 的 `browser_workbench.cjs` 命令。

Expected: `data-drawer`、`aria-expanded`、`aria-busy` 或焦点断言 FAIL。

- [ ] **Step 3: 实现抽屉和焦点的最小行为**

在 `app.js` 增加：

```javascript
function setDrawer(name = null) {
  if (name) document.body.dataset.drawer = name;
  else delete document.body.dataset.drawer;
  $('sidebar-toggle').setAttribute('aria-expanded', String(name === 'sidebar'));
  $('inspector-toggle').setAttribute('aria-expanded', String(name === 'inspector'));
}
```

连接四个开关、背景和 `Escape`。在新审批首次出现时聚焦 `approval-title[tabindex="-1"]`；运行中设置 `#thread-panel[aria-busy="true"]`，终止时移除。历史按钮用 `aria-current` 标识当前 Run。不要在用户仍编辑问题时抢焦点。

- [ ] **Step 4: 实现响应式样式和状态层级**

```css
@media (max-width:1179px){.workbench-shell{grid-template-columns:244px minmax(0,1fr)}
  .inspector{position:fixed;inset:0 0 0 auto;width:min(380px,92vw);transform:translateX(100%)}}
@media (max-width:767px){.workbench-shell{display:block}.sidebar{position:fixed;inset:0 auto 0 0;
  width:min(328px,92vw);transform:translateX(-100%)}.thread{min-height:100dvh}}
body[data-drawer="sidebar"] .sidebar,body[data-drawer="inspector"] .inspector{transform:translateX(0)}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
```

中栏 Composer 固定在底部安全区，设置项可折叠；右栏将本轮、依据、产物、能力分组。统一缺失文件状态文案为“无法读取”，同步 `browser_web.cjs` 与实现。

- [ ] **Step 5: 运行完整本地浏览器回归并提交**

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8768 --missing' --port 8768 -- \
  env NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_web.cjs
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_workbench.cjs
```

Expected: 两项 PASS；1440、1024、768、390/375px 无横向溢出；审批、取消、刷新和错误状态正常。

Commit:

```zsh
git add local-agent/local_agent/static/index.html local-agent/local_agent/static/app.js \
  local-agent/local_agent/static/app.css local-agent/tests/browser_workbench.cjs \
  local-agent/tests/browser_web.cjs
git commit -m "Finish responsive Agent workbench"
```

### Task 4: 写明玩法并完成交付验证

**Files:**
- Modify: `local-agent/README.md`
- Modify: `docs/superpowers/plans/2026-09-15-local-agent-conversation-workbench.md`

- [ ] **Step 1: 更新 README 的启动和模块清单**

在页面启动章节增加“现在可以怎么玩”，准确列出 `file-qa`、`directory-qa`、`project-brief`、持久会话、审批写文件、Skill、stdio MCP、引用/Trace，以及 DeepSeek/单活跃 Run/本地文本/无自动子 Agent 等限制。沿用：

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -m local_agent serve --workspace examples/discovery-workspace --port 8765 --open
```

密钥只从环境变量读取，不写入页面说明、源码或日志。

- [ ] **Step 2: 跑静态与 Python 回归**

Run:

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --check local_agent/static/app.js
PYTHONPATH=. python3 -W error::ResourceWarning -m unittest tests.test_web tests.test_session_web tests.test_agent_web -v
```

Expected: JavaScript 语法通过；Python 定向测试 PASS，无 ResourceWarning。

- [ ] **Step 3: 跑三组现有浏览器回归和新工作台检查**

Run: 依次执行 `browser_web.cjs`、`browser_sessions.cjs`、`browser_extensions.cjs`、`browser_workbench.cjs`。前三项沿用各自现有本地夹具；全部为合成数据，不调用 DeepSeek。

Expected: 四项 PASS，控制台无 `pageerror`。

- [ ] **Step 4: 做最终视觉检查**

用 Playwright 在 1440×900、1024×900、390×844 截图并核对：三栏/抽屉层级、Composer、审批、空状态、引用、产物、44px 控件、焦点和页面级溢出。发现可见缺陷时先写一个能复现的浏览器断言，再做最小修复。

- [ ] **Step 5: 更新 §0、检查提交范围并提交**

将本计划 §0 更新为实际完成状态和验证证据。只暂存本计划、README 和本轮 UI/测试文件；排除 `.superpowers/` 及其他既有未跟踪文件。

```zsh
git diff --check
git status --short
git add local-agent/README.md docs/superpowers/plans/2026-09-15-local-agent-conversation-workbench.md
git commit -m "Document Local Agent workbench usage"
```

Expected: 提交中没有密钥、运行日志、截图生成目录或无关手册文件。
