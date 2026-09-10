# Session Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不削弱现有工具验真与文件权限的前提下，让同一会话可持久保存、重启后继续、隔离查询，并在历史超过模型输入上限后仍能通过摘要和原文回查工作。

**Architecture:** `SessionService` 作为网页和 CLI 的唯一会话入口，`SessionStore` 独占 SQLite、事务、进程锁和恢复规则；每个新 Run 仍创建现有 `Runtime`，只通过 `RunJournal` 保存状态，并通过 `ContextBuilder` 获得本次模型请求。`session_history` 从同一 Store 提供有截止序号、可验真的只读历史查询；文件工具继续使用每个 Run 新建的 `FilePolicy`，历史记录不恢复权限。

**Tech Stack:** Python 3 标准库（`sqlite3`、`fcntl`、`dataclasses`、`json`、`pathlib`、`unittest`）、现有 DeepSeek Provider、`ThreadingHTTPServer`、原生 HTML/CSS/JavaScript、Node 浏览器回归脚本。

---

## §0 当前进度、范围与停止条件

- 状态：**实施计划已编排并完成结构、接口、链接与 SQLite schema 自检；会话应用代码、离线验收和真实验收均未开始。**
- 设计依据：[会话管理模块设计](../specs/2026-09-10-session-management-design.md)已通过原文对照 Gate。计划不重新讨论 SQLite、恢复为新 Run、历史查询或逐次审批等已确认取舍。
- 当前主阻塞：现有 `Runtime`、`WebRuns.jobs` 和审批都只在进程内；Trace 经过脱敏，不能恢复原会话。
- 本批交付：设计 §2 的 Session、Run、Message、ToolCall、Approval、Artifact、摘要、历史查询、网页/CLI 入口和备份。
- 明确不交付：跨设备、多用户、跨会话 Memory、向量检索、自动恢复执行、目录免重复审批、覆盖文件、后台守护、流式输出和新 Provider。
- 固定完成条件：设计 §11 的 S1–S13 全部取得对应证据；代码存在、单元测试通过或页面能打开都不能单独称为完成。
- 真实模型成本：阶段 A、B、C 的开发和故障注入全部使用测试 Provider；离线通过后最多进行 6 次用户提交、每次连同摘要最多 6 次模型请求，总上限 36 次。失败只重跑受影响场景。
- 工作区当前含已完成工具层的未提交改动。执行前只读检查 `git status --short`，保留这些变化；禁止用 reset/checkout 清理。每次提交前用 `git diff --cached --check` 和 `git diff --cached --stat` 确认暂存范围。

执行前先为已经验收的工具层和已批准的会话设计建立本地基线提交，确保后续 `runtime.py` 等共享文件的提交只含会话增量。不要推送，也不要重跑旧真实模型组：

```sh
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent status --short
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add -A
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --name-only
```

暂存清单若出现 `.env`、`runs/`、`meeting/`、`artifacts/` 或 `trial/results/`，立即取消该路径暂存并检查 `.gitignore`；不读取或输出秘密内容。清单只包含本工作区已经完成或批准的产品、测试和文档变化后，执行：

```sh
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: complete tool runtime baseline"
```

如果基线内容不能从当前阶段记录确认归属，停止在这里核对，不把未知改动混入会话任务。

## 1. 模块和文件落点

| 文件 | 动作 | 单一职责 |
|---|---|---|
| `local-agent/local_agent/session_store.py` | 新建 | SQLite schema、进程所有者锁、事务、迁移、查询和一致备份 |
| `local-agent/local_agent/sessions.py` | 新建 | Session/Run 值对象、完整提交幂等、恢复决策及网页/CLI 共用编排 |
| `local-agent/local_agent/context.py` | 新建 | 当前请求预算、近期完整轮次、摘要、历史投影和 `ContextManifest` |
| `local-agent/local_agent/session_history.py` | 新建 | 同会话、固定截止序号的 `session_history` 工具与结果证明 |
| `local-agent/local_agent/conversation.py` | 新建 | “聊这段记录”的提示和 `ConversationPolicy`，不放宽文件答案校验 |
| `local-agent/local_agent/runtime.py` | 修改 | 注入显式 Journal/Context 接口，在模型与工具状态推进前保存权威记录 |
| `local-agent/local_agent/tool_runtime.py` | 修改 | 在发布前后调用通用 Journal；保持工具注册、验真和一次发布语义 |
| `local-agent/local_agent/trace.py` | 修改 | 接受已落库 Run ID；继续提供脱敏诊断且不取代 SessionStore |
| `local-agent/local_agent/file_tools.py`、`approvals.py` | 修改 | 装配历史工具；审批决定持久记录，但许可仍只属于当前 Run/进程 |
| `local-agent/local_agent/web_runs.py`、`web.py` | 修改 | 持久 Session/Run HTTP 编排；工作区缺失时仍允许只读历史 |
| `local-agent/local_agent/__main__.py` | 修改 | `--state-dir`、`sessions` 子命令、`run --session` 和历史对话入口 |
| `local-agent/local_agent/static/index.html`、`app.js`、`app.css` | 修改 | 会话列表、历史轮次、任务类型、中断继续和归档/恢复 |
| `local-agent/tests/test_session_store.py` | 新建 | schema、权限、锁、事务、备份、损坏和迁移故障 |
| `local-agent/tests/test_sessions.py` | 新建 | CRUD、隔离、完整幂等指纹、范围、恢复为新 Run |
| `local-agent/tests/test_run_journal.py` | 新建 | 消息/ToolCall/审批/发布的保存顺序与故障窗口 |
| `local-agent/tests/test_context.py` | 新建 | 近期轮次、预算、摘要、manifest、当前工具链完整性 |
| `local-agent/tests/test_session_history.py` | 新建 | 参数/意图回查、跨会话拒绝、游标、结果证明和大小限制 |
| `local-agent/tests/test_conversation.py` | 新建 | 历史引用、not_found/unable、文件策略不被放宽 |
| `local-agent/tests/test_session_recovery.py` | 新建 | 子进程重启、强制中断、单写者、数据库故障及备份重开 |
| `local-agent/tests/session_process_fixture.py` | 新建 | 在明确暂停点留下真实子进程残留状态 |
| `local-agent/tests/test_session_cli.py` | 新建 | 会话管理、持久 run、继续和备份 CLI 合同 |
| `local-agent/tests/test_session_web.py`、`browser_sessions.cjs` | 新建 | HTTP 会话契约、迟到响应保护和真实浏览器会话流程 |
| `local-agent/tests/test_cli.py`、`test_runtime.py`、`test_tool_runtime.py`、`test_web.py` | 修改 | 新接口回归与旧一次性入口兼容 |
| `local-agent/tests/web_fixture.py` | 修改 | 提供离线 Session 页面数据；不调用云模型 |
| `local-agent/.gitignore`、`README.md`、`trial/directory-check.md` | 修改 | 本地状态排除、入口说明和阶段证据 |
| `local-agent/trial/session-live-check.md` | 新建 | S1–S13、真实模型 Usage、manifest、数据库和产物证据 |

模块依赖必须保持单向：

```text
Web / CLI -> SessionService -> SessionStore
                         \-> ContextBuilder -> SessionStore
                         \-> Runtime -> RunJournal -> SessionStore
                                      \-> ToolRuntime -> RunJournal
                                      \-> session_history -> SessionStore
```

`Runtime` 不导入 `sqlite3`；HTTP handler 不拼模型消息；文件工具不读取 Session 表。删除 `SessionStore` 后，SQLite、锁、迁移、查询和备份复杂度应一起消失，而不是散回调用者。

## 2. 分阶段执行图

| 里程碑 | 任务 | 独立停止点 |
|---|---|---|
| A：保存后找得回 | 1–3 | SQLite、Session CRUD、提交幂等、Journal 与中断分类通过；无模型或页面依赖 |
| B：同一会话接着聊 | 4–9 | Runtime/工具记录、近期 Context、历史对话、CLI/HTTP 连续提交与重启继续通过 |
| C：历史变长仍可继续 | 10–11 | 摘要、长历史回查、备份、故障注入、完整回归和有限真实演示通过 |

阶段内先跑最窄测试；到停止点才扩大到受影响集。没有新改动、新失败或新风险时，不重复已经通过的固定组。

## 3. 实施任务

### Task 1：建立状态目录、单写者与 SQLite schema

**Files:**
- Create: `local-agent/local_agent/session_store.py`
- Create: `local-agent/tests/test_session_store.py`
- Modify: `local-agent/.gitignore`

- [ ] **Step 1：先写状态目录、schema 与锁的失败测试**

在 `test_session_store.py` 写入以下首组测试；测试只使用临时目录：

```python
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from local_agent.session_store import SessionStore, StoreError


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"

    def test_open_creates_private_versioned_store(self):
        store = SessionStore.open(self.state)
        self.addCleanup(store.close)
        self.assertEqual(store.user_version(), 1)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / "sessions.sqlite3").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.state / "state.lock").stat().st_mode), 0o600)
        self.assertEqual(
            {row[0] for row in store.connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'")},
            {"sessions", "runs", "messages", "tool_calls", "approvals",
             "artifacts", "summaries", "context_manifests"},
        )

    def test_second_owner_is_rejected_without_touching_database(self):
        first = SessionStore.open(self.state)
        self.addCleanup(first.close)
        with self.assertRaisesRegex(StoreError, "STATE_IN_USE"):
            SessionStore.open(self.state)

    def test_newer_schema_and_corrupt_database_stop_explicitly(self):
        self.state.mkdir(mode=0o700)
        path = self.state / "sessions.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(StoreError, "STATE_VERSION_UNSUPPORTED"):
            SessionStore.open(self.state)
        path.write_bytes(b"not a sqlite database")
        with self.assertRaisesRegex(StoreError, "STATE_CORRUPT"):
            SessionStore.open(self.state)
```

- [ ] **Step 2：运行测试，确认因模块缺失而失败**

Run: `cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent && python3 -m unittest discover -s tests -p 'test_session_store.py' -v`

Expected: FAIL，首个原因是 `ModuleNotFoundError: No module named 'local_agent.session_store'`。

- [ ] **Step 3：实现状态所有者、连接配置和完整 v1 schema**

`session_store.py` 的外部 interface 固定为：

```text
SessionStore.open(state_dir: Path, *, clock=time.time) -> SessionStore
connection() -> sqlite3.Connection
transaction() -> context manager yielding sqlite3.Connection
user_version() -> int
backup(destination: Path) -> Path
close() -> None
```

错误统一使用 `StoreError(code)`，并在对象上保留 `code`。

实现时使用 `fcntl.flock(lock_fd, LOCK_EX | LOCK_NB)` 持有 `state.lock`，连接配置严格执行：

```python
connection.execute("PRAGMA foreign_keys=ON")
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA synchronous=FULL")
connection.execute("PRAGMA busy_timeout=5000")
```

v1 migration 一次创建下列表和约束；`context_manifests` 是设计中每次请求 manifest 的规范化存储，不改变 Run 的归属：

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    workspace_device INTEGER NOT NULL,
    workspace_inode INTEGER NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','archived')),
    revision INTEGER NOT NULL DEFAULT 1,
    active_summary_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(active_summary_id, id) REFERENCES summaries(id, session_id)
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    task_type TEXT NOT NULL CHECK(task_type IN ('files','conversation')),
    question TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    output_path TEXT,
    parent_run_id TEXT,
    state TEXT NOT NULL,
    phase TEXT NOT NULL,
    stop_reason TEXT,
    result_json TEXT,
    trace_path TEXT,
    provider_json TEXT,
    config_json TEXT NOT NULL,
    system_version TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE(session_id, client_request_id),
    UNIQUE(id, session_id),
    FOREIGN KEY(session_id) REFERENCES sessions(id),
    FOREIGN KEY(parent_run_id, session_id) REFERENCES runs(id, session_id)
);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_seq INTEGER NOT NULL,
    run_seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','control')),
    source_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    validation_state TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(session_id, session_seq),
    UNIQUE(run_id, run_seq),
    UNIQUE(id, session_id, run_id),
    FOREIGN KEY(run_id, session_id) REFERENCES runs(id, session_id)
);
CREATE TABLE tool_calls (
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    assistant_message_id TEXT NOT NULL,
    name TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('requested','started','succeeded','failed','skipped','interrupted','unknown')),
    result_message_id TEXT,
    publication_intent_json TEXT,
    publication_state TEXT NOT NULL DEFAULT 'none'
        CHECK(publication_state IN ('none','intent_recorded','confirmed','unknown')),
    recovery_state TEXT NOT NULL DEFAULT 'none',
    PRIMARY KEY(run_id, call_id),
    UNIQUE(run_id, call_id, session_id),
    FOREIGN KEY(run_id, session_id) REFERENCES runs(id, session_id),
    FOREIGN KEY(assistant_message_id, session_id, run_id)
        REFERENCES messages(id, session_id, run_id),
    FOREIGN KEY(result_message_id, session_id, run_id)
        REFERENCES messages(id, session_id, run_id)
);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    argument_hash TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending'
        CHECK(decision IN ('pending','allowed','denied','expired','cancelled')),
    process_generation TEXT NOT NULL,
    created_at REAL NOT NULL,
    decided_at REAL,
    invalidated_at REAL,
    FOREIGN KEY(run_id, call_id, session_id)
        REFERENCES tool_calls(run_id, call_id, session_id)
);
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    path TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK(bytes >= 0),
    sha256 TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    recovery_state TEXT NOT NULL,
    confirmed_at REAL NOT NULL,
    UNIQUE(run_id, call_id),
    FOREIGN KEY(run_id, call_id, session_id)
        REFERENCES tool_calls(run_id, call_id, session_id)
);
CREATE TABLE summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    covered_through_seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    model_json TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(session_id, version),
    UNIQUE(id, session_id),
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE context_manifests (
    run_id TEXT NOT NULL,
    request_seq INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    input_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(run_id, request_seq),
    FOREIGN KEY(run_id, session_id) REFERENCES runs(id, session_id)
);
PRAGMA user_version=1;
```

将 `*.sqlite3`、`*.sqlite3-wal`、`*.sqlite3-shm`、`*.sqlite3.backup` 和 `state.lock` 加入 `local-agent/.gitignore`。状态默认在仓库外，但排除规则防止用户通过 `--state-dir` 误放进项目。

- [ ] **Step 4：补备份与线程连接测试并实现**

测试必须断言 `backup()` 使用 `sqlite3.Connection.backup`，并以 `uri = destination.resolve().as_uri() + "?mode=ro"`、`sqlite3.connect(uri, uri=True)` 独立打开。目的目录位于任一已绑定资料工作区时返回 `STATE_DIR_INSIDE_WORKSPACE`。`SessionStore` 为每个线程延迟创建连接；worker 在 `finally` 中调用内部 `close_thread_connection()`。网页关闭时先停止并 join worker，再由 `close()` 释放最后连接和进程锁，禁止跨线程关闭仍在使用的 SQLite connection。迁移前把旧库备份成带版本和时间的 `.backup`；备份或迁移失败时抛 `STATE_MIGRATION_FAILED`，不删除或重建旧库。

- [ ] **Step 5：运行定向测试**

Run: `cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent && python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_session_store.py' -v`

Expected: PASS；没有 `ResourceWarning`。

- [ ] **Step 6：保存阶段提交**

```sh
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/session_store.py local-agent/tests/test_session_store.py local-agent/.gitignore
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: add durable session store"
```

若暂存区含当前任务之外的行，先取消暂存对应路径并保留工作区内容；不为满足提交步骤清理既有改动。

### Task 2：实现 Session CRUD、范围绑定和完整提交幂等

**Files:**
- Create: `local-agent/local_agent/sessions.py`
- Create: `local-agent/tests/test_sessions.py`
- Modify: `local-agent/local_agent/session_store.py`

- [ ] **Step 1：写 CRUD、隔离和完整指纹测试**

测试用固定 `client_request_id="request-1"` 连续提交：完全相同语义返回同一 `run_id`；依次只改 `question`、`task_type`、`scope`、`output_path`、`parent_run_id`、`execution_options` 时均抛 `SESSION_REQUEST_CONFLICT`。另建 A/B 两个 Session，断言 B 无法读取 A 的 Run 或 message。父 Run 变体引用测试中先创建的真实 interrupted Run，不能用不存在的 ID 绕过指纹检查。

```python
base = RunSubmission(
    client_request_id="request-1",
    question="核对代号",
    task_type="files",
    scope=SessionScope(mode="file", target_path="note.md"),
    output_path=None,
    parent_run_id=None,
    execution_options={"max_steps": 6},
)
first = service.submit(session_a.id, base)
again = service.submit(session_a.id, base)
self.assertEqual((again.run_id, again.created), (first.run_id, False))
for changed in (
    replace(base, question="核对日期"),
    replace(base, task_type="conversation"),
    replace(base, scope=SessionScope(mode="file", target_path="other.md")),
    replace(base, output_path="report.md"),
    replace(base, parent_run_id=interrupted_parent.run_id),
    replace(base, execution_options={"max_steps": 5}),
):
    with self.subTest(changed=changed):
        with self.assertRaisesRegex(SessionError, "SESSION_REQUEST_CONFLICT"):
            service.submit(session_a.id, changed)
```

- [ ] **Step 2：运行测试，确认接口尚不存在**

Run: `cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent && python3 -m unittest discover -s tests -p 'test_sessions.py' -v`

Expected: FAIL，缺少 `sessions.RunSubmission` 或 `SessionService`。

- [ ] **Step 3：定义不可变值对象与规范指纹**

在 `sessions.py` 定义：

```python
@dataclass(frozen=True)
class SessionScope:
    mode: Literal["file", "directory"]
    target_path: str | None


@dataclass(frozen=True)
class RunSubmission:
    client_request_id: str
    question: str
    task_type: Literal["files", "conversation"]
    scope: SessionScope
    output_path: str | None
    parent_run_id: str | None
    execution_options: dict

    def fingerprint(self) -> str:
        canonical = json.dumps({
            "question": self.question,
            "task_type": self.task_type,
            "scope": asdict(self.scope),
            "output_path": self.output_path,
            "parent_run_id": self.parent_run_id,
            "execution_options": self.execution_options,
        }, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    session_id: str
    created: bool
    submission: RunSubmission
```

`SessionService` 的公共 interface 固定为：

```python
create(workspace: Path, title: str, scope: SessionScope) -> SessionRecord
list(workspace: Path, *, archived: bool = False, cursor: str | None = None) -> Page
load(session_id: str) -> SessionRecord
rename(session_id: str, title: str) -> SessionRecord
archive(session_id: str) -> SessionRecord
restore(session_id: str) -> SessionRecord
submit(session_id: str, submission: RunSubmission) -> PreparedRun
continue_interrupted(session_id: str, run_id: str, client_request_id: str) -> PreparedRun
```

创建时用规范路径和 `os.stat()` 的 device/inode 固定工作区身份；`mode=file` 必须有一个受支持的相对 `target_path`，`mode=directory` 的 `target_path` 必须为空，输入范围变化通过 `create()` 建新 Session。标题去首尾空白后限制 1–120 字；列表每页 20 条并使用稳定 `(updated_at,id)` 游标。

- [ ] **Step 4：实现原子提交和跨会话归属检查**

`submit()` 在单个 `BEGIN IMMEDIATE` 事务内依次完成：验证 Session 为 active；比较 `(session_id, client_request_id)`；把包含 scope 的完整规范提交保存到 `request_json`；插入 `runs(state='queued', phase='submitted')`；分配 `session_seq/run_seq=1` 并插入用户原话；递增 Session revision。事务提交后才返回 `created=True`。重复指纹一致返回旧 Run；指纹不同抛 `SESSION_REQUEST_CONFLICT`。所有 `load_run/load_message` SQL 同时带 `session_id`，其他会话 ID 统一返回 `NOT_FOUND`。

- [ ] **Step 5：实现继续中断为新 Run**

`continue_interrupted()` 仅接受所属 Session 中 `state='interrupted'` 的旧 Run；它复制旧问题、任务类型和固定 scope，设置 `parent_run_id`，但将 `output_path` 置空、使用新的 `client_request_id`。它不复制调用 ID、预算、审批或执行许可。

- [ ] **Step 6：运行定向测试并提交**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_session_store.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_sessions.py' -v
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/session_store.py local-agent/local_agent/sessions.py local-agent/tests/test_session_store.py local-agent/tests/test_sessions.py
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: add session lifecycle and idempotent submissions"
```

Expected: 两个测试模块全部 PASS。

### Task 3：实现 RunJournal、恢复分类和发布窗口

**Files:**
- Modify: `local-agent/local_agent/session_store.py`
- Modify: `local-agent/local_agent/sessions.py`
- Create: `local-agent/tests/test_run_journal.py`
- Create: `local-agent/tests/test_session_recovery.py`

- [ ] **Step 1：写消息顺序、调用归属和恢复失败测试**

构造一个 Run，依次记录用户消息、合法 assistant ToolCall、工具失败、最终回答。断言角色顺序、`session_seq/run_seq` 单调、`tool_calls.result_message_id` 指向原 ID 的 tool message。再构造 `model_in_flight`、`tool_started`、`approval_allowed`、`publication_intent_committed`、`publication_receipt_committed` 五种未结束状态，关闭并重开 Store 后断言：前三种旧 Run 为 `interrupted`，intent 无回执为 `WRITE_OUTCOME_UNKNOWN`，已有回执保留 Artifact。

```python
journal.record_model_reply({"role": "assistant", "content": None, "tool_calls": [{
    "id": "call-1", "type": "function", "function": {
        "name": "read_file",
        "arguments": '{"path":"missing-73.md","intent":"核对旧参数"}',
    },
}]}, usage=None, validation="tool_calls_valid")
journal.record_tool_result("call-1", {
    "ok": False,
    "error": {"code": "PATH_NOT_DISCOVERED", "message": "not discovered", "owner": "model"},
})
history = store.load_run_messages(session.id, prepared.run_id)
self.assertEqual([item.role for item in history], ["user", "assistant", "tool"])
self.assertEqual(history[-1].payload["tool_call_id"], "call-1")
```

- [ ] **Step 2：运行失败测试**

Run these commands from `/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent`:

```sh
python3 -m unittest discover -s tests -p 'test_run_journal.py' -v
python3 -m unittest discover -s tests -p 'test_session_recovery.py' -v
```

Expected: FAIL，`PreparedRun` 尚不能创建 Journal。

- [ ] **Step 3：实现窄 RunJournal interface**

`PreparedRun` 增加 `journal`。`RunJournal` 只暴露以下有顺序语义的方法：

```python
record_model_request(request: dict, manifest: dict) -> None
record_model_reply(message: dict, usage: dict | None, validation: str) -> str
record_tool_started(call_id: str) -> None
record_tool_result(call_id: str, result: dict) -> str
record_approval_required(call_id: str, approval: dict) -> None
record_approval_decision(approval_id: str, decision: str) -> None
record_publication_intent(call_id: str, intent: dict) -> None
record_publication_receipt(call_id: str, receipt: dict) -> None
finish_run(result: dict) -> None
```

每个方法开启自己的短事务并验证前态；模型调用、审批等待和文件 I/O 期间不得持事务。`record_model_reply()` 必须保存原始合法或非法 assistant payload；只有协议合法时，才在同一事务从该 payload 建 ToolCall 索引。`tool_calls` 只保存消息引用、名称和阶段，禁止复制 `arguments`。无对应 ToolCall 的结果、重复 terminal 结果和跨 Run call ID 均抛 `JOURNAL_ORDER_ERROR`。

- [ ] **Step 4：实现启动恢复和发布结果分类**

`SessionStore.recover_interrupted(process_generation)` 在取得所有者锁后运行一次。对 `queued/running/model_in_flight/tool_started/waiting_approval` 设 `interrupted`；审批许可全部标记失效。`publication_intent_json` 已提交而无 Artifact 时设 `WRITE_OUTCOME_UNKNOWN`，并在同一 workspace 的所有 Session 中锁定该目标路径；有 Artifact 回执时保留 confirmed。恢复只更新记录，不启动 Provider、工具或审批线程。

只读核对未知目标可返回 `missing/present_same_hash/present_different_hash`，但三者都不能自动升级为“当时已创建”。用户明确选择保留或改用新输出目标后才解除目标锁；首版不自动删除、覆盖或改名。

- [ ] **Step 5：实现一致在线备份测试**

在一个连接保持 WAL 活跃时调用 `store.backup(destination)`，用标准库 `sqlite3` 的 URI `mode=ro` 打开备份，断言 Run、messages、ToolCall、Artifact 数量和关联一致；原 Artifact 文件不出现在备份目录。备份检查不取得生产状态目录的所有者锁，也不触发迁移或恢复。

- [ ] **Step 6：运行里程碑 A 验证并提交**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_session*.py' -v
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/session_store.py local-agent/local_agent/sessions.py \
  local-agent/tests/test_run_journal.py local-agent/tests/test_session_recovery.py
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: persist run journal and recovery state"
```

Expected: 里程碑 A 全部 PASS。此时停止检查数据库内容和中断分类；不接页面，不调用模型。

### Task 4：给 Runtime 和 ToolRuntime 接入显式 Journal seam

**Files:**
- Modify: `local-agent/local_agent/runtime.py`
- Modify: `local-agent/local_agent/tool_runtime.py`
- Modify: `local-agent/local_agent/approvals.py`
- Modify: `local-agent/local_agent/trace.py`
- Modify: `local-agent/tests/test_runtime.py`
- Modify: `local-agent/tests/test_tool_runtime.py`
- Modify: `local-agent/tests/test_approvals.py`
- Create: `local-agent/tests/test_session_runtime.py`

- [ ] **Step 1：写当前 Run 全链保存和旧入口兼容测试**

测试使用现有 `ScriptedProvider` 和临时 SessionStore。断言第一次模型请求保存 manifest；合法 assistant ToolCall 在执行前落库；工具结果按原 ID 落库后才进入第二次请求；最终回答先落库再由 `run()` 返回。原 `Runtime(provider, tool, trace).run(question, target_path)` 仍可通过显式 `NullRunJournal` 路径运行原测试。

- [ ] **Step 2：写发布数据库故障测试**

在 `record_publication_intent()` 抛错时断言目标文件不存在；在实际文件已发布、`record_publication_receipt()` 抛错时断言 Runtime 返回 `WRITE_OUTCOME_UNKNOWN`，当前进程保留实物回执用于展示，不能返回 `completed` 或重新 publish。

- [ ] **Step 3：抽取统一记录器并保持 Trace 次要**

在 `runtime.py` 增加：

```python
class NullRunJournal:
    session_id = None
    run_id = None
    def record_model_request(self, request, manifest): return None
    def record_model_reply(self, message, usage=None, validation="raw"): return None
    def record_tool_started(self, call_id): return None
    def record_tool_result(self, call_id, result): return None
    def record_approval_required(self, call_id, approval): return None
    def record_approval_decision(self, approval_id, decision): return None
    def record_publication_intent(self, call_id, intent): return None
    def record_publication_receipt(self, call_id, receipt): return None
    def finish_run(self, result): return None
```

`Runtime.__init__` 增加 keyword-only `journal=None` 和 `request_builder=None`；未传时显式构造 `NullRunJournal` 和原策略 request builder。持久 Session 必须传真实 Journal，Store 错误直接停止，不能换成 Null。

`Trace.__init__` 同时增加可选 `run_id`；持久 Run 必须把已经落库的 Run ID 传入 Trace，避免数据库和诊断日志出现两套 ID。Trace 仍脱敏且位于资料目录外；Trace 写失败不修改数据库中已经确认的会话事实。

保存顺序固定为：request＋manifest → 调用 Provider → 原 assistant reply＋校验状态和合法 ToolCall 索引（同一事务，初始为 requested）→ ToolCall started → 实际 result＋tool message → 下一次 request；终态先 `journal.finish_run(result)`，再尽力写 Trace 并返回。Trace 写失败记录 `TRACE_ERROR` 诊断，但不得回滚已保存 Session 或抹去已确认 Artifact。

- [ ] **Step 4：在 ToolRuntime 发布 seam 记录 intent 和 receipt**

为现有 `ToolRuntime.invoke` 增加 keyword-only `journal=None`，并使用传入的 Journal。在取得发布锁且调用 `publish` 前提交冻结的 target、内容哈希、run/call/approval；提交失败不得 publish。发布并验真后立即提交 receipt，再允许 `policy.accept()` 和最终 Artifact 展示。若 receipt 提交失败，返回 `WRITE_OUTCOME_UNKNOWN` 并停止后续调用；一次许可仍最多进入一次 publish。

- [ ] **Step 5：让审批事件保存决定而不恢复许可**

`ApprovalBroker` 的 publish 回调继续提供页面事件。`request()` 必须在 required 记录落库后才能展示并等待；`decide()` 必须在 decision 落库后才能把内存状态改成 allowed。记录包含冻结 preview、规范参数摘要、decision、run/call 和 process generation；Store 中的 allowed 记录不能被 `ApprovalBroker` 加载成新 permit。

- [ ] **Step 6：先跑定向，再跑共享链路**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*runtime.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_approvals.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*workflow.py' -v
```

Expected: 新持久链和原工具合同全部 PASS；没有真实模型调用。

- [ ] **Step 7：提交 Runtime seam**

```sh
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/runtime.py local-agent/local_agent/tool_runtime.py \
  local-agent/local_agent/approvals.py local-agent/local_agent/trace.py local-agent/tests/test_runtime.py \
  local-agent/tests/test_tool_runtime.py local-agent/tests/test_approvals.py \
  local-agent/tests/test_session_runtime.py
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: journal runtime and tool execution"
```

### Task 5：实现近期 Context、ConversationPolicy 和当前文件重新取证

**Files:**
- Create: `local-agent/local_agent/context.py`
- Create: `local-agent/local_agent/conversation.py`
- Modify: `local-agent/local_agent/file_tools.py`
- Modify: `local-agent/local_agent/answers.py`
- Modify: `local-agent/local_agent/prompts.py`
- Create: `local-agent/tests/test_context.py`
- Create: `local-agent/tests/test_conversation.py`

- [ ] **Step 1：写重启连续、更正优先和文件新证据测试**

建立两轮已结束历史：用户先说“报告用表格”，后更正“不要表格，改成三段文字”。第三轮 Conversation Context 必须含两条有角色和 ID 的原文，回答引用更正消息。文件模式第一轮读取 A，修改文件为 B 后新 Run 必须重新调用 `read_file`；旧 A 只能作为“当时记录”，不能进入当前 `FilePolicy.snapshots` 或当前文件 citation。

- [ ] **Step 2：写 Context 预算和完整工具链测试**

输入 5 个已结束 Run，断言默认最多选择最近 4 个完整 Run；历史 ToolCall 及其结果以一条有关联 ID 的结构化文本记录出现，不生成会被 Provider 当作新调用的原生 `tool_calls`，也不生成孤立 `role=tool`。当前用户输入、当前 ToolCall 和当前完整结果在缩减历史时不得拆开；仅当前链已超过 64 KiB 时返回 `CONTEXT_LIMIT`。

- [ ] **Step 3：实现 ContextBuilder 深模块**

固定 interface：

```text
@dataclass(frozen=True)
class BuiltRequest:
    request: dict
    manifest: dict

ContextBuilder(store, session_id: str, run_id: str)
build(current_request: dict, max_input_bytes: int, *, summarize: callable | None) -> BuiltRequest
```

每轮先由当前 `FilePolicy.model_request(messages, limit, schemas)` 完成本轮工具正文的行号视图，再把生成的 current request 交给 `build()`。`build()` 按当前 system/schema → 结构化摘要/恢复状态 → 最近完整历史 Run → 当前用户及当前工具链排序。历史 system、摘要和旧工具文本只能作为带 `source_kind/session_seq/message_id` 的文本数据投影，不能成为新 system 指令；历史 ToolCall/结果带 `historical=True`、`source_run_id/call_id/status`，不生成原生 `tool_calls` 或孤立 `role=tool`。只有当前 Provider 新回复中的调用交给 ToolRuntime。

最终 JSON 序列化后计字节，manifest 保存 Session/Run、规则/schema 版本、选中消息 ID、当前范围、遗漏历史范围、实际字节数和 SHA-256。阶段 B 的 `ContextBuilder` 只读 Store 并返回数据；阶段 C 唯一允许的写入是通过 Store 原子切换校验成功的摘要版本。

- [ ] **Step 4：实现 ConversationPolicy 而不修改文件答案规则**

`conversation.py` 定义输出：

```json
{"status":"answered","answer":"上次要求改成三段文字。","references":[{"message_id":"message-2","start":0,"end":12}]}
```

`answered` 至少一条可见历史引用；`not_found` 要求本 Run 有真实历史查询；`unable` 要求查询或恢复错误。引用按 Unicode 码点范围由程序从当前可见原文生成。旧 assistant 回答只能证明“当时回答过”，不能证明外部事实。`validate_answer()` 的文件 citation 和本轮读取门槛保持原样。

同时把 Runtime 中硬编码的 `JSON_REPAIR/IDENTIFIER_REPAIR` 选择移入 policy interface：文件策略返回现有 citation/标识修复提示；ConversationPolicy 返回 references 格式修复提示。Runtime 只根据 `error.code` 调用 `policy.repair_prompt(code)`，不能把文件答案格式发给历史对话。

- [ ] **Step 5：装配 Session 文件策略**

在 `file_tools.py` 加 `SessionTaskPolicy`，按已注册工具域委托：`list_files/read_file/write_file` 才能更新 `FilePolicy`；`session_history` 结果不进入文件 scope、快照或 Artifact。每个文件 Run 新建 FilePolicy；历史 SHA/正文不能调用 `record()` 重新授权。Conversation 模式不注册文件工具或 writer。

- [ ] **Step 6：运行定向测试并提交**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_context.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_conversation.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_answers.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_*runtime.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_report_workflow.py' -v
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/context.py local-agent/local_agent/conversation.py \
  local-agent/local_agent/file_tools.py local-agent/local_agent/answers.py \
  local-agent/local_agent/prompts.py local-agent/tests/test_context.py \
  local-agent/tests/test_conversation.py
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: build bounded session context"
```

Expected: 近期上下文、历史对话和当前文件重新取证均 PASS。

### Task 6：实现可验真的 `session_history` 历史工具

**Files:**
- Create: `local-agent/local_agent/session_history.py`
- Modify: `local-agent/local_agent/session_store.py`
- Modify: `local-agent/local_agent/tool_runtime.py`
- Modify: `local-agent/local_agent/file_tools.py`
- Create: `local-agent/tests/test_session_history.py`

- [ ] **Step 1：写旧调用参数回查和跨会话拒绝测试**

在旧 Run 保存一个合法 ToolCall，路径标记只存在原 `function.arguments` 中，用户消息、最终回答和错误结果均不含该标记。让该 Run 退出近期 Context 后搜索标记，必须返回调用消息；read 必须返回原 `call_id/name/arguments/intent`、`source_run_id` 和对应错误消息引用。

```python
tool = SessionHistoryTool(store, session_a.id, before_seq=cutoff)
found = tool.execute({"action": "search", "query": "missing-73.md", "intent": "回查失败参数"})
self.assertTrue(found["ok"])
self.assertEqual(found["hits"][0]["source_kind"], "tool_call")
page = tool.execute({
    "action": "read",
    "message_id": found["hits"][0]["message_id"],
    "intent": "读取原调用",
})
self.assertEqual(page["source_run_id"], old_run_id)
self.assertEqual(page["call_id"], "call-1")
self.assertIn('"path":"missing-73.md"', page["text"])
self.assertEqual(page["result_message_id"], failed_result_message_id)
```

另用 session B 的 tool 查询 A 的 message ID、cursor 和 call ID，三者都返回同一个 `NOT_FOUND` 信封，不泄露是否存在。

- [ ] **Step 2：写游标、Unicode、截止序号和证明测试**

search 最多 8 条；read 原文页最多 8 KiB，信封最多 12 KiB；首次 read 省略 cursor，后续只能使用服务器返回、绑定 `session_id/before_seq/message_id/offset` 的不透明 cursor。文本范围按 Unicode 码点 `[start,end)`，不能切断代理项。构造 tool 后再追加同会话消息，新消息因 `before_seq` 不可见。伪造 `{ok: true}` 或改写搜索结果时，`verify_success()` 必须拒绝。

- [ ] **Step 3：扩展扁平 schema 的 enum 校验**

当前 `valid_arguments()` 只检查字符串。加入以下通用规则，确保 `action` 的 `enum` 真正生效；search/read 的字段组合仍由工具自身验证：

```python
allowed = rule.get("enum")
if allowed is not None and value not in allowed:
    return False
```

`session_history` 的模型 schema 只暴露 `action/query/message_id/cursor/intent`；`session_id`、`before_seq`、最大结果数和字节限制只由构造器注入，不能让模型填写。

- [ ] **Step 4：实现 Store 查询和稳定 `text_view`**

search 只查询同 Session、`session_seq < before_seq` 的业务消息，使用参数绑定和稳定 `(session_seq,message_id)` 游标。开放用户原话、已校验最终回答、协议合法的 assistant ToolCall 及已验真的工具结果；非法模型回复、system、凭据和程序私有记录不开放。

ToolCall 的 `text_view` 每次从原 assistant message payload 生成固定键序 JSON：`call_id/name/arguments/intent`。`arguments` 和 `intent` 不从 `tool_calls` 索引、错误结果或摘要反推。通过 `(session_id,run_id,call_id)` 关联结果；没有结果时返回空结果引用和 Store 已记录的中断/未知状态，不生成伪 tool message。

- [ ] **Step 5：实现工具结果证明并装配策略**

`SessionHistoryTool` 使用 `ToolSpec(name="session_history", risk="low", timeout_seconds=5, max_result_bytes=12288)`。`execute()` 保存 Store 生成的不可变结果证明；`verify_success(arguments,data)` 只接受同一次参数和结果的证明。FilePolicy 只为 `{list_files,read_file,write_file}` 更新文件状态；history 成功或失败均不改变文件 scope、snapshot、`had_error` 或 Artifact。

- [ ] **Step 6：运行定向测试并提交**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_session_history.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_context.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_conversation.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_tool_runtime.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_report_workflow.py' -v
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/session_history.py local-agent/local_agent/session_store.py \
  local-agent/local_agent/tool_runtime.py local-agent/local_agent/file_tools.py \
  local-agent/tests/test_session_history.py
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: add bounded session history tool"
```

Expected: 参数/意图、结果双向关联、隔离、游标和结果证明均 PASS。

### Task 7：用 SessionService 编排一轮持久执行与继续

**Files:**
- Modify: `local-agent/local_agent/sessions.py`
- Modify: `local-agent/local_agent/runtime.py`
- Modify: `local-agent/tests/test_session_runtime.py`
- Modify: `local-agent/tests/test_session_recovery.py`

- [ ] **Step 1：写两轮连续和工作区失效测试**

第一轮保存用户约定，第二轮使用新 Runtime 和同一 Session，断言 Provider 实际收到第一轮原话。关闭 Store 并重开后第三轮仍收到历史。把工作区移走后：conversation Run 仍可查询历史；file/directory Run 返回 `WORKSPACE_UNAVAILABLE`，Provider 调用数为 0。

- [ ] **Step 2：写 continue 创建新 Run 测试**

中断旧 Run 后调用 `continue_interrupted`。断言新 `run_id`、新调用 ID 空集合、新 6 次模型预算、新审批许可为空、`parent_run_id` 指向旧 Run；Context 以程序恢复说明呈现旧问题和进度，不伪造 tool role。未显式给新 `output_path` 时 writer 不注册。

- [ ] **Step 3：实现共用执行入口**

在 `SessionService` 增加：

```text
execute(prepared_run, provider, trace, *, approvals, control, config) -> dict
```

该方法根据 `task_type` 新建本 Run 的 FilePolicy/ConversationPolicy、`SessionHistoryTool(before_seq=current_user_seq)`、ContextBuilder 和 Runtime。文件模式每次重新核对规范路径及 device/inode；conversation 模式不实例化 DirectoryTools、ReadFile 或 writer。任何 Store 写失败都返回明确状态并停止后续 Provider/工具调用。

若 `prepared_run.created=False`，`execute()` 只返回已保存的 Run 快照，不构造 Provider/Runtime、不加入 `WebRuns.jobs`，也不增加模型或工具调用；这把 Task 2 的幂等约束落实到实际调度边界。

沿用 Task 4 的 Trace Run ID；SessionService 不生成第二个诊断 ID。Trace 失败不修改数据库中已确认的会话事实。

- [ ] **Step 4：实现继续恢复说明和目标锁检查**

继续 Run 的 Context 包含旧 Run ID、原问题、已保存进度和中断分类；旧 ToolCall 仅为历史数据。若旧发布为未知，新提交同一 workspace/target 在执行前返回 `WRITE_OUTCOME_UNKNOWN`；用户选择新输出路径后按普通逐次审批运行。

- [ ] **Step 5：运行里程碑 B 核心测试并提交**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_session*.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_context.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_conversation.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_runtime.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_tool_runtime.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_report_workflow.py' -v
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/sessions.py local-agent/local_agent/runtime.py \
  local-agent/tests/test_session_runtime.py \
  local-agent/tests/test_session_recovery.py
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: execute and continue durable sessions"
```

Expected: S1 的持久/连续核心、S4、S5，以及 S8、S9、S10 的底层行为通过；页面尚未接入。

### Task 8：接入 CLI 会话命令和一致备份

**Files:**
- Modify: `local-agent/local_agent/__main__.py`
- Create: `local-agent/tests/test_session_cli.py`
- Modify: `local-agent/tests/test_cli.py`

- [ ] **Step 1：写 CLI 解析和无 Provider 管理测试**

测试以下命令均使用临时 `--state-dir`：

```text
sessions create --workspace PATH --title TITLE --file note.md
sessions list --workspace PATH
sessions show SESSION_ID
sessions rename SESSION_ID --title TITLE
sessions archive SESSION_ID
sessions restore SESSION_ID
sessions backup DESTINATION
run --session SESSION_ID --question QUESTION
run --session SESSION_ID --conversation --question QUESTION
sessions continue SESSION_ID --run INTERRUPTED_RUN_ID
```

`list/show/rename/archive/restore/backup` 不构造 Provider；无 API key 也可成功。旧 `demo` 和不带 `--session` 的 `run --workspace` 保持一次性、无持久化语义。

- [ ] **Step 2：写占用、范围和输出路径测试**

网页进程持有 state owner 时，第二个 CLI 写命令返回非零和 `STATE_IN_USE`；不创建第二份库。`run --session` 从 Session 读取固定 input scope，不接受新的 workspace/file/discover 来扩大范围。`--output-file` 每 Run 显式提供且不从上一轮继承；`--conversation` 与文件模式参数互斥。

- [ ] **Step 3：实现 argparse 层和结构化退出结果**

给 `serve`、持久 `run`、`sessions` 命令增加 `--state-dir`，默认 `Path.home()/".local/share/local-agent"`。创建 nested `sessions` subparser；所有入口只调用 SessionService，不直接执行 SQL。错误码映射固定为：输入 2、未完成 Run 1、成功 0；输出 JSON 包含 `session_id/run_id/state/error`，不输出数据库正文、密钥或进程令牌。

- [ ] **Step 4：实现 CLI 备份和中断继续**

`sessions backup DESTINATION` 调用 SQLite backup interface，拒绝目的地位于会话绑定资料根内；输出数据库备份路径及包含的 Session/Run 数，不复制 Artifact。`sessions continue` 仅创建和执行新 Run；不会恢复旧审批或自动沿用输出路径。

- [ ] **Step 5：运行测试并提交**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_session_cli.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_cli.py' -v
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/__main__.py local-agent/tests/test_session_cli.py \
  local-agent/tests/test_cli.py
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: expose session management in cli"
```

Expected: 持久和旧一次性 CLI 均 PASS；管理命令模型调用数为 0。

### Task 9：接入 HTTP、会话页面和缺失工作区降级

**Files:**
- Modify: `local-agent/local_agent/web_runs.py`
- Modify: `local-agent/local_agent/web.py`
- Modify: `local-agent/local_agent/__main__.py`
- Modify: `local-agent/local_agent/static/index.html`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`
- Create: `local-agent/tests/test_session_web.py`
- Modify: `local-agent/tests/test_web.py`
- Modify: `local-agent/tests/test_web_approvals.py`
- Modify: `local-agent/tests/web_fixture.py`
- Create: `local-agent/tests/browser_sessions.cjs`

- [ ] **Step 1：写 HTTP 路由、隔离和迟到响应失败测试**

固定最小路由：

```text
GET/POST /api/sessions
GET /api/sessions/{session_id}
POST /api/sessions/{session_id}/rename
POST /api/sessions/{session_id}/archive
POST /api/sessions/{session_id}/restore
GET/POST /api/sessions/{session_id}/runs
GET /api/sessions/{session_id}/runs/{run_id}
POST /api/sessions/{session_id}/runs/{run_id}/cancel
POST /api/sessions/{session_id}/runs/{run_id}/approvals/{approval_id}
POST /api/sessions/{session_id}/continue
```

创建 Session body 只含 title 和固定 scope；网页不能传 workspace。Run body 只含 `client_request_id/task_type/question/output_file`。跨 Session 的 run、approval、message、cursor 均统一 404。重复 client ID 同语义返回原 Run，不同语义 409。Host、Origin、`X-Session-Token` 和 body 上限检查覆盖所有新路由。

创建 body 的合同固定为 `{"title":"资料整理","scope":{"mode":"file","file":"note.md"}}` 或 `{"title":"目录核对","scope":{"mode":"directory"}}`；workspace 由服务启动时的规范路径绑定。Run 的 `task_type` 只允许 `files` 或 `conversation`，scope 不允许随 Run 重传。

- [ ] **Step 2：重构 WebRuns 只管理在途控制**

历史列表、已结束 Run 和分页全部从 SessionService/Store 读取；`WebRuns.jobs` 只保留当前进程的 cancel/control/thread，以 `(session_id,run_id)` 为键。删除“超过 20 个 job 就淘汰业务历史”的含义，页面每页 20 条只是 DB keyset 分页。全服务仍一次只执行一个 Run，但查看或切换其他 Session 不取消后台 Run。

- [ ] **Step 3：解除启动时对 DirectoryTools 的依赖**

`WebRuns.__init__` 只接收 SessionService、当前工作区选择和 provider factory，不创建 DirectoryTools。`serve --workspace PATH` 只展示绑定该规范路径的 Session；另支持 `serve --session SESSION_ID` 从 Store 选择已存在 Session。工作区缺失时页面和历史 GET 仍可打开，conversation Run 可用；文件 Run 返回 `WORKSPACE_UNAVAILABLE` 且不调用 Provider。缺 API key 时历史仍可看，新 Run 显示模型未配置。

- [ ] **Step 4：先写离线页面脚本断言，再修改页面**

页面只增加：会话列表、新建/选择/改名/归档恢复；当前 Session 固定 scope；按 Run 展示原问题、回答、工具调用、产物和历史 reference；“核对资料/聊这段记录”选择；中断卡片和继续按钮；旧审批预览只读。首次显示“对话和已读取内容保存在本机”。

JavaScript 用 `sessionId + runId + viewGeneration` 判断响应是否仍属于当前视图；切换 Session 后丢弃迟到的 poll/approval/cancel 响应。所有历史摘录使用服务器给出的 `textContent`，不在浏览器重新计算 Unicode offset。新文件仍逐次预览批准，不展示“记住目录”。

- [ ] **Step 5：实现页面和 HTTP，运行定向测试**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_session_web.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_web.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_web_approvals.py' -v
/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  tests/browser_sessions.cjs
```

Expected: 新 HTTP 合同、旧工具页面状态和离线 Session 页面脚本全部 PASS。

- [ ] **Step 6：运行真实浏览器回归**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8768 --missing' --port 8768 \
  -- env NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  tests/browser_web.cjs
```

Expected: 建会话、连续两轮、切换 A/B、迟到响应、审批、归档恢复、中断提示和窄屏布局全部 PASS；fixture 不调用云模型。

- [ ] **Step 7：提交页面切片**

```sh
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/web_runs.py local-agent/local_agent/web.py \
  local-agent/local_agent/__main__.py local-agent/local_agent/static/index.html \
  local-agent/local_agent/static/app.js local-agent/local_agent/static/app.css \
  local-agent/tests/test_session_web.py local-agent/tests/test_web.py \
  local-agent/tests/test_web_approvals.py local-agent/tests/web_fixture.py \
  local-agent/tests/browser_sessions.cjs
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: add persistent session web interface"
```

此处达到里程碑 B：短会话跨重启可继续；长历史压缩尚未实现，不能宣称完整会话模块完成。

### Task 10：实现摘要、长历史预算和失败降级

**Files:**
- Modify: `local-agent/local_agent/context.py`
- Modify: `local-agent/local_agent/runtime.py`
- Modify: `local-agent/local_agent/session_store.py`
- Modify: `local-agent/tests/test_context.py`
- Modify: `local-agent/tests/test_session_runtime.py`

- [ ] **Step 1：写 50 轮、摘要预算和失败降级测试**

人工插入 50 个完整 Run，总历史超过 128 KiB。触发新 Run 后断言最终每次 request ≤64 KiB；摘要输入自身 ≤64 KiB、输出 ≤6 KiB；覆盖点只跨完整 terminal Run；最近最多 4 轮按实际字节缩减。摘要保留目标、更正、未完成事项和可核对 message ID/range，早期原文仍由 history tool 找回。

再让摘要 Provider 返回超限、非法 JSON、错误 message ID、摘录不匹配和异常；旧摘要和原消息均保持，当前 Run 最多尝试一次摘要，然后用旧摘要＋近期轮次＋遗漏范围继续或明确 `CONTEXT_LIMIT`，不能循环摘要。

- [ ] **Step 2：统一摘要和正常模型调用预算**

把 Runtime 的 Provider 调用整理为一个内部 `_complete(request, purpose)` 路径；`purpose` 只取 `summary` 或 `task`。ContextBuilder 的 `summarize` callback 必须走该路径，因此摘要计入同一 Run 的最多 6 次模型调用、同一 `RunControl` 活动时限、Usage 和 Journal。摘要请求 tools 固定为空；每个用户 Run 最多一次。

- [ ] **Step 3：在文件消息投影之后构建最终 Context**

每轮先由 `FilePolicy.model_request()` 完成本轮工具正文的行号视图和当前证据投影，再把该 current request 交 `ContextBuilder.build(current_request, max_input_bytes, summarize=callback)` 合并历史。每次工具返回后重新计算；超限时依次移除最老完整历史投影、派生摘要和更老近期轮次。当前 user、当前 assistant ToolCall 和对应 tool result 不可拆分或静默截断。

历史 ToolCall 使用带 `source_run_id/call_id/status` 的 assistant 文本数据投影，不使用可被 Provider 当作待执行请求的原生 `tool_calls`；历史 tool result 也不制造孤立 `role=tool`。当前 Run 的原生 assistant ToolCall/tool 链保持原协议。

- [ ] **Step 4：实现可追溯结构化摘要**

摘要 schema 固定为 `goals/constraints/decisions/completed/pending/anchors`；每个事实带来源 message ID 和 Unicode range。程序验证消息属于本 Session、范围逐字匹配、覆盖序号单调后，才在事务内保存新 summary 并切换 `active_summary_id`。摘要只作导航和 Context 数据，不能成为 Conversation reference、文件 evidence 或权限。

摘要模型的原始请求和回复以 `source_kind=summary` 留在 Journal 供审计，但普通历史投影和 `session_history` 默认排除它们；只有已验证的结构化摘要进入 Context，避免把摘要过程当成用户对话重复注入。

- [ ] **Step 5：运行长会话测试并提交**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_context.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_session_history.py' -v
python3 -W error::ResourceWarning -m unittest discover -s tests -p 'test_session_runtime.py' -v
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/local_agent/context.py local-agent/local_agent/runtime.py \
  local-agent/local_agent/session_store.py local-agent/tests/test_context.py \
  local-agent/tests/test_session_runtime.py
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "feat: keep long sessions within context budget"
```

Expected: S6、S7 通过；测试 Provider 的请求计数证明摘要没有突破总预算。

### Task 11：完成故障注入、全回归、有限真实演示和状态收口

**Files:**
- Create: `local-agent/tests/session_process_fixture.py`
- Modify: `local-agent/tests/test_session_recovery.py`
- Modify: `local-agent/tests/browser_web.cjs`
- Modify: `local-agent/tests/browser_tools.cjs`
- Modify: `local-agent/README.md`
- Modify: `local-agent/trial/directory-check.md`
- Create: `local-agent/trial/session-live-check.md`

- [ ] **Step 1：用真实子进程覆盖强制中断和单写者**

fixture 接收临时 state/workspace 和暂停点 `after_submit/model_in_flight/read_started/approval_waiting/approval_allowed/publication_intent/publication_receipt`；到点后向父进程写入“ready”再等待。测试进程用 `os.kill(pid, signal.SIGKILL)`，随后重开 Store：S8 断言输入、模型和读取阶段分类正确且继续会创建新 Run；S9 断言旧审批失效、新建仍须重新预览批准；S10 断言发布前、意图后和回执后的状态分别为未执行、未知、已确认。所有重启场景的 Provider/工具自动调用数必须为 0。第二进程持锁时，CLI/网页第二所有者明确失败且库未损坏。

- [ ] **Step 2：完成 S1–S13 追踪矩阵**

在 `session-live-check.md` 建立固定表，每个场景只引用实际命令、测试名、数据库核对或 trace/manifest 路径：

| 场景 | 主要证据 |
|---|---|
| S1 | 两轮＋关闭重启＋第三轮实际 Context |
| S2 | A/B Store、Context、HTTP ID/cursor 隔离及迟到响应 |
| S3 | 完整 request JSON＋fingerprint 幂等冲突 |
| S4 | 用户更正优先、历史回答引用用户消息 |
| S5 | 当前文件重新读取、历史文件值仍可回顾且不恢复权限 |
| S6 | 50 轮、摘要预算、旧 ToolCall 参数/意图/结果回查 |
| S7 | 摘要失败、非法结构/引用和超限时有限降级 |
| S8 | 输入、模型、读取三个强制中断点及新 Run 继续 |
| S9 | 审批等待/已允许未发布两个中断点及许可失效 |
| S10 | 发布前、意图后、回执后三个窗口的恢复分类 |
| S11 | 满盘/只读/损坏/版本/双所有者故障注入 |
| S12 | 第 21 个 Run、归档恢复、一致备份、Artifact 不搬移 |
| S13 | 原工具合同、网页与真实连续演示 |

缺少证据的格子保持 NOT_RUN/FAILED，不能用工具层旧 295 项替代会话验收。

- [ ] **Step 3：运行完整离线回归**

```sh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -W error::ResourceWarning -m unittest discover -s tests
/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  tests/browser_tools.cjs
/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  tests/browser_sessions.cjs
```

Expected: 所有 Python、工具页面和会话页面离线检查 PASS。记录实际测试数量和耗时，不预填数字。

- [ ] **Step 4：运行一次真实浏览器回归**

沿 Task 9 的 `with_server.py` 命令运行 `tests/browser_web.cjs`。Expected: PASS；网络请求仅 loopback，fixture Provider 为模拟，不使用 DeepSeek。

- [ ] **Step 5：执行有限真实模型验收**

在已通过离线回归的同一代码版本上执行两段：

1. 创建 Session，完成资料任务与约定；第二轮修改要求；关闭并重启；第三轮“按上次要求继续”，重新读取已变化文件；第四轮新建报告并逐次确认。
2. 用离线 fixture 预置超过 128 KiB 的旧历史；真实提交一次触发摘要，再提交一次查询早期要求和旧失败调用参数。

每段保存实际 `session_id/run_id`、Usage、Context manifest、回答引用、数据库状态和 Artifact 哈希。第一段最多 4 次用户提交、第二段最多 2 次；每次连摘要最多 6 次模型请求，总硬上限 36，不要求用满。创建、查看、切换、重启、批准、归档和备份本身不得调用模型。失败只修并重跑受影响场景，不重跑旧目录 12 例。

- [ ] **Step 6：做实现级 Review Gate**

对实际 diff、S1–S13 证据和真实结果做一次 Review Gate。P0/P1 必须修复并定向重验；P2 仅在有界条件下处理；固定样例通过后停止，不开展额外 UI 打磨、更多模型统计或目录免审批。

- [ ] **Step 7：更新权威状态并提交**

只有 S1–S13 全部有证据时，才将 README、`trial/directory-check.md` 和本计划 §0 改为“会话模块已实现并通过”；否则写明具体 NOT_RUN/FAILED 和下一阻塞。最终提交：

```sh
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent add local-agent/README.md local-agent/trial/directory-check.md \
  local-agent/trial/session-live-check.md docs/superpowers/plans/2026-09-10-session-management.md \
  local-agent/tests/session_process_fixture.py local-agent/tests/test_session_recovery.py \
  local-agent/tests/browser_web.cjs local-agent/tests/browser_tools.cjs
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent diff --cached --check
git -C /Users/wujingyu/Desktop/AI/projects/dev-agent commit -m "test: verify durable session workflow"
```

## 4. 验收覆盖与执行止损

| 设计要求 | 实施任务 |
|---|---|
| SQLite、本地权限、单写者、迁移与备份 | 1、3、8、11 |
| Session CRUD、隔离、21 条不淘汰、归档恢复 | 2、9、11 |
| 完整提交幂等 | 2、9 |
| Journal 顺序、审批、发布三窗口 | 3、4、7、11 |
| 重启中断和继续为新 Run | 3、7、8、9、11 |
| 当前文件重新取证、历史不恢复权限 | 5、7 |
| ConversationPolicy 与历史引用 | 5、6、7 |
| 旧 ToolCall 参数/意图/结果回查 | 3、6、10 |
| 64 KiB、有界摘要、失败降级、manifest | 5、10 |
| 页面、CLI、缺失工作区历史可看 | 8、9 |
| 原工具合同和有限真实模型演示 | 4、6、11 |

每次增加测试或修复前，先指出它对应 S1–S13 的哪一项以及会改变什么判断。连续两轮只在扩充边界或评分器而没有推进当前里程碑时，停止并回到本表。阶段 A/B/C 到达各自停止点后才更新状态；不把部分完成写成完整会话模块通过。

## 5. 计划自检

- Spec coverage：设计 §1–§12 均映射到 Tasks 1–11；目录免重复审批明确保持后置。
- Interface consistency：SessionService、SessionStore、RunJournal、ContextBuilder 和 session_history 的调用方向单向；网页/CLI 不直接写表，Runtime 不导入 SQLite。
- Authority consistency：每个文件 Run 新建权限账本；旧批准、历史消息、摘要和 ToolCall 都不能重新授权或进入执行队列。
- Failure consistency：数据库失败先于 Provider/副作用时停止；发布后 receipt 保存失败保留当前进程实物事实，重启后按 unknown，不伪造成功或失败。
- Cost consistency：离线开发 0 次真实 API；最终真实提交最多 6 次、模型请求最多 36 次。
- Placeholder scan：计划没有占位接口或未分配的验收项；执行时以实际测试输出填写数量和证据。

计划完成后的推荐执行方式是 `subagent-driven-development`：一次只实施一个任务，每个里程碑由主任务核对证据和范围。若在当前会话内连续执行，则使用 `executing-plans`，同样在 Task 3、9、11 三个停止点汇报，不跨过失败的里程碑。
