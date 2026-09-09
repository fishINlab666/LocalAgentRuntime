# Local Agent Runtime 新手教学手册 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

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

当前目录不是 Git 仓库，因此本计划不执行 `git add` 或 `git commit`。每个任务完成后以测试结果和 SHA-256 快照作为检查点。

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

