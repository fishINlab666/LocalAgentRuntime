# Local Agent Managed Document Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Local Agent 会话工作台中加入受管本地文件与文件夹导入，让 `.md/.txt`、带文字层 PDF 和 DOCX 能被保存为本地副本、按需搜索读取、返回原始位置引用，并跨重启和显式备份恢复继续使用。

**Architecture:** 新的 `ImportStore` 负责上传、解析、切块、完整性清单和可恢复发布；所有执行入口通过 `ManagedWorkspaceResolver` 取得独立读根与 Artifact 写根。现有 Agent Loop、ToolCall 回填、审批和持久会话继续作为唯一运行链路，只增加受限 `search_documents`、导入事实账本和引用来源映射。HTTP 与页面只编排这一深模块，不直接实现文件权限或解析逻辑。

**Tech Stack:** Python 3.14、标准库 SQLite/HTTP/文件描述符/子进程、`pypdf==6.16.1`、`python-docx==1.2.0`、原生 HTML/CSS/JavaScript、`unittest`、Node.js、Playwright/Google Chrome。

---

## §0 当前进度与停止条件

2026-09-16：设计稿已由用户确认。Task 1 已在 commit `615b8ec` 完成，四种格式解析与受限子进程的 34 项定向测试通过；Task 2 已在 commit `4d4b587` 完成，Schema v3、旧权限冻结与维护闸门的 50 项相关测试通过；Task 3 已在 commit `af43220` 完成，安全上传、精确受管副本、目录与发布对象身份校验的 68 项相关测试通过；Task 4 已在 commit `ff6b0ab` 完成，受限解析、切块、原子发布、取消和三个崩溃窗口恢复的 137 项相关测试通过；Task 5 已在 commit `23944af` 完成，统一 Resolver、原子 Session 关联、恢复隔离及 Web／CLI／直接执行入口的 172 项相关测试通过；Task 6 已在 commit `567cd9c` 完成，只读搜索、导入事实账本、原始位置引用及持久 Session 回填的 87 项定向测试和 307 项共享链路回归通过；Task 7 已在 commit `06e4bfe` 完成，导入会话的预声明输出只写独立 `artifacts/`，一次成功、审批、禁止改名/覆盖、真实回执、未知写入恢复和受权下载均已接通。Task 8 已在 commit `417a579` 完成，SQLite、受管资料与 Artifact 作为一个清单约束的目录原子备份，恢复到不存在的 State Store 时重绑目录身份并全量验真；24 项备份/CLI 定向测试、89 项共享链路测试及最终 647 项 Python 回归通过，限定安全 Gate 通过。Task 9 已在 commit `d153368` 完成，受限 HTTP 导入、Agent 隔离、单 worker、幂等重放、失败重试及受限 Session 摘要已接通；11 项导入 HTTP 测试和 55 项 Web 回归通过，限定安全 Gate 通过。Task 10 已在 commit `6d7ddc7` 完成，文件／文件夹预检、顺序原始字节上传、取消与刷新恢复、兼容 Agent、成功后的 Session 身份核验、来源事实和响应式交互已接通；六组浏览器回归通过，三路限定终审通过。Task 11 的固定离线闭环已在 commit `3ddb60e` 完成并推送：2 个 Run、6 次脚本化模型请求贯通混合导入、搜索／读取回填、PDF/Word 原位置、删除外部原件后的重启校验、跨 Import 隔离及新 State Store 恢复后追问；127 项导入测试、661 项完整 Python 回归和六组浏览器回归通过，最终限定 Gate PASS。

当前 Codex 进程没有继承模型密钥，受限真实入口已按 `NOT_RUN / CONFIG_MISSING / 0 model calls` 保存原始报告；离线 Provider 不冒充真实模型。实现可以提交和推送，真实 DeepSeek 的 2 Run 语义验收仍是独立待验证项；取得环境变量后只运行这一组，不扩展样例。

本批完成条件：Task 1–10 的定向测试、完整 Python 回归和既有浏览器回归通过；Task 11 的固定混合资料闭环证明“导入 → 搜索 → 读取 → ToolCall 结果进入下一轮 → 原始位置引用 → 重启追问 → 隔离与恢复”。离线证据全部通过后，最多执行一个真实 DeepSeek 会话、2 个 Run、12 次模型请求；任一核心失败立即停止并保留证据。达到条件后不增加 OCR、同步、资料删除、向量检索或新格式。

## 文件职责

| 文件 | 职责 |
|---|---|
| `local-agent/local_agent/import_parsers.py` | 四种文档解析 adapter、格式边界、稳定错误码和规范化来源位置 |
| `local-agent/local_agent/import_worker.py` | 解析子进程入口、RLIMIT、IPC 上限、超时/取消与进程回收 |
| `local-agent/local_agent/imports.py` | Import 数据对象、上传 slot、切块/索引/manifest、状态机、原子发布、resolver 和来源映射 |
| `local-agent/local_agent/import_tools.py` | `search_documents` adapter、导入读取授权、catalog/正文分账和 `fact_coverage` |
| `local-agent/local_agent/state_maintenance.py` | 同进程 Run/导入活动登记与 backup 独占维护闸门 |
| `local-agent/local_agent/state_backup.py` | SQLite + imports sidecar 成对备份、总 manifest 和不存在目标目录的一次性恢复 |
| `local-agent/local_agent/session_store.py` | Schema v3、import/file 行、Session 唯一关联和恢复查询 |
| `local-agent/local_agent/sessions.py` | Session 数据对象、导入关联事务、统一 resolver、执行与 Artifact 回执访问 |
| `local-agent/local_agent/agents.py` | 工具白名单、内置兼容助手和独立读写根装配 |
| `local-agent/local_agent/file_tools.py` | 可注入导入 policy、Artifact 写根与既有 ToolRuntime 适配 |
| `local-agent/local_agent/agent_runtime.py` | 组合助手的搜索分派和最终引用映射接入 |
| `local-agent/local_agent/conversation.py` | 持久会话 policy 的搜索工具分类 |
| `local-agent/local_agent/web_runs.py` | 导入后台 job、进度/取消、Session 可见性与下载授权编排 |
| `local-agent/local_agent/web.py` | 受限元数据、二进制 slot、complete/status/cancel 和 Artifact 下载路由 |
| `local-agent/local_agent/__main__.py` | 同一 resolver 的 CLI 执行与 `restore-backup` 管理入口 |
| `local-agent/local_agent/static/index.html` | 导入入口、选择器、预检、进度、错误、隐私说明与资料详情 |
| `local-agent/local_agent/static/app.js` | 浏览器预检、逐文件上传、轮询/取消、迟到响应保护和自动进入会话 |
| `local-agent/local_agent/static/app.css` | 导入面板、进度和响应式可访问状态 |
| `local-agent/local_agent/import_evaluation.py` | 两轮合成混合资料验收、上下文／来源／重启／隔离／恢复检查及原始报告 |
| `local-agent/tests/import_fixtures.py` | 确定性生成小文本 PDF、DOCX 和恶意边界夹具 |
| `local-agent/tests/test_import_parsers.py` | 解析、位置映射、资源限制、取消与错误映射 |
| `local-agent/tests/test_import_store.py` | 上传、切块、manifest、幂等发布、取消和崩溃窗口 |
| `local-agent/tests/test_managed_workspace.py` | resolver、Web/CLI/直接执行隔离与篡改拒绝 |
| `local-agent/tests/test_import_runtime.py` | 搜索、ToolCall 回填、fact coverage、组合 policy 和旧助手快照 |
| `local-agent/tests/test_import_citations.py` | 严格 quote 校验后的 PDF/DOCX 来源位置 enrichment |
| `local-agent/tests/test_import_artifacts.py` | 独立写根、审批、回执、下载和未知发布恢复 |
| `local-agent/tests/test_state_backup.py` | 维护闸门、成对备份、摘要校验和原子恢复 |
| `local-agent/tests/test_import_web.py` | 真实 loopback HTTP 上传协议、认证、错误和幂等 |
| `local-agent/tests/browser_imports.cjs` | 文件/文件夹选择、预检、进度、取消、焦点、刷新与多视口 |
| `local-agent/tests/test_import_acceptance.py` | 固定混合资料离线闭环与重启/恢复验收 |

### Task 1: 建立确定性文档解析边界

**Files:**
- Modify: `local-agent/requirements-extensions.txt`
- Create: `local-agent/local_agent/import_parsers.py`
- Create: `local-agent/local_agent/import_worker.py`
- Create: `local-agent/tests/import_fixtures.py`
- Create: `local-agent/tests/test_import_parsers.py`

- [x] **Step 1: 固定解析依赖并写四种格式和位置顺序的失败测试**

先向依赖文件追加：

```text
pypdf==6.16.1
python-docx==1.2.0
```

在 `tests/import_fixtures.py` 提供 `write_text_pdf(path, pages)` 和 `write_docx(path)`。PDF helper 直接构造 Catalog、Pages、Page、Helvetica Font 与 content stream，并按实际字节偏移生成 xref；DOCX helper 使用 `Document.add_paragraph()`、`add_table()`、`add_paragraph()` 生成“段落 → 表格 → 段落”。

在 `tests/test_import_parsers.py` 写入：

```python
class ImportParserTests(unittest.TestCase):
    def test_text_and_markdown_keep_unicode_and_normalize_only_newlines(self):
        self.path('note.TXT').write_bytes('青禾-47\r\n预算：42\r'.encode())
        parsed = parse_document(self.path('note.TXT'), '资料/note.TXT', LIMITS)
        self.assertEqual([unit.text for unit in parsed.units], ['青禾-47\n', '预算：42\n'])
        self.assertEqual(parsed.units[0].location,
                         {'kind': 'text_lines', 'start': 1, 'end': 1})

    def test_pdf_maps_each_line_to_its_page(self):
        write_text_pdf(self.path('brief.pdf'), ['Project A', 'Budget 42'])
        parsed = parse_document(self.path('brief.pdf'), 'brief.pdf', LIMITS)
        self.assertEqual([unit.location for unit in parsed.units], [
            {'kind': 'pdf_page', 'page': 1}, {'kind': 'pdf_page', 'page': 2}])

    def test_docx_preserves_paragraph_table_paragraph_order(self):
        write_docx(self.path('brief.docx'))
        parsed = parse_document(self.path('brief.docx'), 'brief.docx', LIMITS)
        self.assertEqual([unit.location['kind'] for unit in parsed.units],
                         ['docx_paragraph', 'docx_table_row', 'docx_paragraph'])
```

再加入严格 UTF-8/NUL/控制字符、纯图片 PDF、加密 PDF、损坏 PDF/DOCX、部分空 PDF 页警告和不支持扩展名的表驱动断言。所有错误只比较稳定 code，不比较第三方库异常文本。

- [x] **Step 2: 安装固定依赖并确认测试因模块不存在而失败**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
.venv/bin/python -m pip install -r requirements-extensions.txt
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest tests.test_import_parsers -v
```

Expected: 安装成功；测试以 `ModuleNotFoundError: local_agent.import_parsers` 失败，而不是缺少第三方 parser。

- [x] **Step 3: 实现 parser 数据契约和四个 adapter**

在 `import_parsers.py` 定义并实际使用：

```python
SUPPORTED_EXTENSIONS = frozenset({'.md', '.txt', '.pdf', '.docx'})

@dataclass(frozen=True)
class ImportLimits:
    max_file_bytes: int = 20 * 1024 * 1024
    max_extracted_bytes: int = 512 * 1024
    max_pdf_pages: int = 500
    max_pdf_content_bytes: int = 64 * 1024 * 1024
    max_docx_entries: int = 2000
    max_docx_uncompressed_bytes: int = 100 * 1024 * 1024
    max_docx_ratio: int = 100

@dataclass(frozen=True)
class ParsedUnit:
    text: str
    location: dict

@dataclass(frozen=True)
class ParsedDocument:
    logical_path: str
    units: tuple[ParsedUnit, ...]
    warnings: tuple[dict, ...]
    stats: dict

class DocumentParseError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

def parse_document(source: Path, logical_path: str,
                   limits: ImportLimits) -> ParsedDocument:
    suffix = Path(logical_path).suffix.casefold()
    parser = {'.md': MarkdownParser, '.txt': Utf8TextParser,
              '.pdf': PdfTextParser, '.docx': DocxParser}.get(suffix)
    if parser is None:
        raise DocumentParseError('IMPORT_FORMAT_UNSUPPORTED')
    return parser().parse(source, logical_path, limits)
```

文本 adapter 只把 CRLF/CR 转为 LF；PDF adapter 逐页调用 `extract_text()` 并保留页码；DOCX adapter 使用 `Document.iter_inner_content()` 区分 `Paragraph` 与 `Table`，表格逐行输出带制表符的确定性文本。任何 parser 在构造返回值前检查提取后的 UTF-8 总字节数。

- [x] **Step 4: 增加受限子进程并验证资源错误**

`import_worker.py` 从 stdin 读取不超过 128 KiB 的 JSON 请求，只接受 `source_path/logical_path/limits`，安装 `RLIMIT_CPU=15`；非 Darwin 安装绝对 512 MiB `RLIMIT_AS`，Darwin 安装“当前 VSZ + 512 MiB”新增预算，调用 `parse_document()`，向 stdout 输出不超过 8 MiB 的严格 JSON。父进程 helper 使用：

```python
process = subprocess.Popen(
    [sys.executable, '-m', 'local_agent.import_worker'],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    env=minimal_environment(), start_new_session=True,
)
try:
    stdout, _ = process.communicate(request_bytes, timeout=20)
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    raise DocumentParseError('DOCUMENT_LIMIT_EXCEEDED')
```

父进程用临时文件承接输出并在轮询期间检查 8 MiB 上限，验证退出码和完整 schema；取消同样 `killpg` 并有界回收，极端情况下交给 daemon reaper。无法安装 RLIMIT 或 Darwin VSZ 探测失败返回 `DOCUMENT_PARSER_UNAVAILABLE`。DOCX 在加载前检查 ZIP 条目、声明解压量与单条压缩比；PDF 在提取期间累计页数、页内容流和递归可达且按对象去重的 Form XObject 解码流。为每个阈值加入“等于上限通过、超过一单位失败”的小夹具；测试注入较小的 `ImportLimits`，不在单元测试中真实分配 64 MiB/512 MiB。

- [x] **Step 5: 跑解析定向检查并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest tests.test_import_parsers -v
```

Expected: 全部 PASS，无 `ResourceWarning`。

Commit:

```zsh
git add local-agent/requirements-extensions.txt local-agent/local_agent/import_parsers.py \
  local-agent/local_agent/import_worker.py local-agent/tests/import_fixtures.py \
  local-agent/tests/test_import_parsers.py
git commit -m "Add bounded local document parsers"
```

### Task 2: 升级 Session Schema 并建立维护闸门

**Files:**
- Create: `local-agent/local_agent/state_maintenance.py`
- Modify: `local-agent/local_agent/session_store.py:18-158`
- Modify: `local-agent/local_agent/session_store.py:768-870`
- Modify: `local-agent/local_agent/agents.py:158-173`
- Modify: `local-agent/local_agent/sessions.py:87-120`
- Modify: `local-agent/tests/test_session_store.py`
- Modify: `local-agent/tests/test_agent_sessions.py`
- Create: `local-agent/tests/test_state_maintenance.py`

- [x] **Step 1: 写 v2→v3、旧快照和 gate 竞争的失败测试**

```python
def test_v3_schema_adds_import_tables_and_nullable_unique_session_link(self):
    store = self.open_store()
    self.assertEqual(store.user_version(), 3)
    names = {row[0] for row in store.connection().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    self.assertTrue({'imports', 'import_files'} <= names)
    self.assertIn('import_id', {row[1] for row in store.connection().execute(
        'PRAGMA table_info(sessions)')})

def test_legacy_migration_does_not_grant_search_documents(self):
    store = self.open_v1_store_and_migrate()
    snapshot = json.loads(store.connection().execute(
        'SELECT agent_snapshot_json FROM sessions').fetchone()[0])
    self.assertNotIn('search_documents', snapshot['tools'])

def test_maintenance_refuses_while_activity_exists_and_blocks_new_activity(self):
    gate = StateMaintenanceGate()
    lease = gate.start('run', 'run-1')
    with self.assertRaisesRegex(StateBusy, 'STATE_BUSY'):
        with gate.maintenance():
            pass
    lease.close()
    with gate.maintenance():
        with self.assertRaisesRegex(StateBusy, 'STATE_BUSY'):
            gate.start('import', 'import-1')
```

- [x] **Step 2: 运行并确认 schema 仍为 v2 且 gate 不存在**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_session_store tests.test_agent_sessions tests.test_state_maintenance -v
```

Expected: 新断言因版本为 2、缺少 import 表或模块而失败；既有测试仍能启动。

- [x] **Step 3: 实现 schema v3 与冻结旧内置助手**

`_migrate_v3()` 在一个事务中执行：

```sql
CREATE TABLE imports (
  id TEXT PRIMARY KEY,
  request_json TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('uploading','finalizing','ready','failed','cancelled','unavailable')),
  job_id TEXT,
  manifest_sha256 TEXT,
  workspace_device INTEGER,
  workspace_inode INTEGER,
  artifact_device INTEGER,
  artifact_inode INTEGER,
  error_code TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE import_files (
  import_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  slot_id TEXT NOT NULL,
  logical_path TEXT NOT NULL,
  extension TEXT NOT NULL,
  declared_bytes INTEGER NOT NULL CHECK(declared_bytes >= 0),
  sha256 TEXT,
  upload_state TEXT NOT NULL CHECK(upload_state IN ('pending','stored')),
  parser_json TEXT,
  PRIMARY KEY(import_id, source_id),
  UNIQUE(import_id, slot_id),
  UNIQUE(import_id, logical_path),
  FOREIGN KEY(import_id) REFERENCES imports(id) ON DELETE CASCADE
);
ALTER TABLE sessions ADD COLUMN import_id TEXT REFERENCES imports(id);
CREATE UNIQUE INDEX sessions_import_id_unique
  ON sessions(import_id) WHERE import_id IS NOT NULL;
PRAGMA user_version=3;
```

初始化按当前版本逐级调用 v1、v2、v3。`builtin_agent(mode, *, legacy=False)` 在 `legacy=True` 时永远返回升级前工具集合；`_migrate_v2()` 必须调用 legacy 版本，避免未来迁移静默扩权。`SessionRecord` 最后增加 `import_id: str | None = None`，同步所有 SELECT 的列顺序并保持旧测试构造方式兼容。

- [x] **Step 4: 实现 `StateMaintenanceGate`**

```python
class StateBusy(RuntimeError):
    def __init__(self):
        super().__init__('STATE_BUSY')
        self.code = 'STATE_BUSY'

class StateMaintenanceGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = set()
        self._maintenance = False

    def start(self, kind: str, identity: str) -> ActivityLease:
        key = (kind, identity)
        with self._lock:
            if self._maintenance or key in self._active:
                raise StateBusy()
            self._active.add(key)
        return ActivityLease(self, key)

    @contextmanager
    def maintenance(self):
        with self._lock:
            if self._maintenance or self._active:
                raise StateBusy()
            self._maintenance = True
        try:
            yield
        finally:
            with self._lock:
                self._maintenance = False
```

`ActivityLease.close()` 在锁内幂等移除 key。每个 `SessionStore` 构造一个 `maintenance_gate`；文件锁继续只处理跨进程所有权。

- [x] **Step 5: 跑迁移和 gate 检查并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_session_store tests.test_agent_sessions tests.test_state_maintenance -v
```

Expected: 全部 PASS；v0/v1/v2 迁移数据保留，旧 Session 快照不含搜索工具。

Commit:

```zsh
git add local-agent/local_agent/state_maintenance.py local-agent/local_agent/session_store.py \
  local-agent/local_agent/agents.py local-agent/local_agent/sessions.py \
  local-agent/tests/test_session_store.py local-agent/tests/test_agent_sessions.py \
  local-agent/tests/test_state_maintenance.py
git commit -m "Add import state schema and maintenance gate"
```

### Task 3: 实现安全上传与受管副本

**Files:**
- Create: `local-agent/local_agent/imports.py`
- Create: `local-agent/tests/test_import_store.py`

- [x] **Step 1: 写 begin/add_file 的失败测试**

```python
def test_begin_allocates_random_slots_and_add_file_saves_exact_private_copy(self):
    batch = self.imports.begin({
        'kind': 'folder', 'name': '项目资料', 'agent_id': 'directory-qa',
        'files': [{'logical_path': '项目/note.MD', 'bytes': 12}],
        'ignored': [{'logical_path': '项目/image.png', 'bytes': 3}],
    })
    slot = batch.files[0]
    receipt = self.imports.add_file(batch.id, slot.slot_id, io.BytesIO(b'hello world\n'), 12)
    self.assertEqual(receipt.sha256, hashlib.sha256(b'hello world\n').hexdigest())
    original = self.state / 'imports' / '.staging' / batch.id / 'originals' / f'{slot.source_id}.bin'
    self.assertEqual(original.read_bytes(), b'hello world\n')
    self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o600)
```

加入以下失败矩阵：绝对路径、`..`、空段、反斜杠、控制字符、重复路径、扩展名大小写、501 个目录项、51 个支持文件、单文件 20 MiB+1、批量 100 MiB+1、错误 `Content-Length`、短 body、长 body、重复 slot、未知 import/slot。失败不得留下 `stored` 行或可见正式目录。

- [x] **Step 2: 运行并确认 ImportStore 尚不存在**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest tests.test_import_store -v
```

Expected: `ImportError` 或缺少 `ImportStore` 导致 FAIL。

- [x] **Step 3: 实现请求类型、路径校验和上传 slot**

`imports.py` 对外固定：

```python
@dataclass(frozen=True)
class ImportBatch:
    id: str
    status: str
    files: tuple['ImportFileSlot', ...]

@dataclass(frozen=True)
class FileReceipt:
    import_id: str
    source_id: str
    bytes: int
    sha256: str
```

公开方法签名固定为 `begin(request) -> ImportBatch`、`add_file(import_id, slot_id, stream, content_length) -> FileReceipt`、`start_finalize(import_id) -> ImportJob`、`snapshot(import_id) -> ImportSnapshot`、`cancel(import_id) -> ImportSnapshot` 和 `open(import_id) -> ImportedWorkspace`；每个返回类型都用冻结 dataclass，并在本任务定义完整字段。

请求 JSON canonical 化后计算 SHA-256；import/source/slot/job ID 均取 `uuid.uuid4().hex`。路径只接受非空相对 POSIX 段；扩展名用 `casefold()`。上传只按声明长度从 stream 分块读取，边写边 SHA-256；额外读取 1 byte 防止长 body，成功后 `fsync` 并以 `O_EXCL|O_NOFOLLOW` 发布该 source 临时文件，再事务更新 `stored`。`begin/add_file/cancel` 各自在数据库操作期间取得 gate activity；SQLite 中的 `uploading/finalizing` 状态让 backup 在请求间隙仍能识别未完成导入。

- [x] **Step 4: 通过上传检查并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest tests.test_import_store -v
```

Expected: 上传、路径、大小、权限和重复请求全部 PASS。

Commit:

```zsh
git add local-agent/local_agent/imports.py local-agent/tests/test_import_store.py
git commit -m "Store validated local import copies"
```

### Task 4: 完成解析、切块、发布与崩溃恢复

**Files:**
- Modify: `local-agent/local_agent/imports.py`
- Modify: `local-agent/local_agent/session_store.py:1225-1327`
- Modify: `local-agent/tests/test_import_store.py`
- Modify: `local-agent/tests/test_session_recovery.py`

- [x] **Step 1: 写规范化产物和每个崩溃窗口的失败测试**

创建两份文本各跨越 12 KiB、两页 PDF 和段落/表格 DOCX，断言：

```python
job = self.imports.start_finalize(batch.id)
published = job.run()
self.assertEqual(published.status, 'published_unlinked')
self.assertTrue(all(path.stat().st_size <= 12 * 1024 for path in published.chunk_paths))
self.assertLessEqual((published.workspace / 'index.md').stat().st_size, 16 * 1024)
self.assertLessEqual(len(published.chunk_paths), 256)
self.assertEqual(self.hash_manifest_files(published.root), published.manifest_hashes)
```

Task 4 的故障注入点固定为 `after_parse_fsync`、`after_publish_rename`、`after_parent_fsync`。每个点重开 State Store 后断言：同一 complete 返回同一 job；完整正式目录可重建同一个 `PublishedImport`；不创建 Session；孤儿正式目录才清理。`before_link_transaction`、`after_link_commit`、同一 import 不会出现两个 Session，以及 `ready` 摘要错误转为 `unavailable` 依赖 Task 5 的关联事务与 resolver，在 Task 5 验证。

- [x] **Step 2: 运行并确认 finalize/恢复断言失败**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_import_store tests.test_session_recovery -v
```

Expected: 新 finalize、manifest 或恢复断言 FAIL。

- [x] **Step 3: 实现切块、索引、位置表和原子发布**

将 parser 的有序 unit 转成 LF 行；按“来源单元 → 段落 → 行 → UTF-8 字符边界”切块，每块最大 12 KiB。生成：

```json
{
  "documents/<source-id>/chunk-0001.md": {
    "1": [{"kind":"pdf_page","page":1}],
    "2": [{"kind":"pdf_page","page":2}]
  }
}
```

`index.md` 只列逻辑路径、格式、块号范围和原始位置范围。manifest 记录 originals、index、每块和 locations 的 SHA-256、parser 版本、统计、警告及目录身份。全批解析成功后依次 `fsync` 文件、目录，rename staging 到 `imports/<id>`，再 `fsync(imports/)`；任一已接纳文件失败则整批失败且不创建 Session。

- [x] **Step 4: 接入状态恢复与批次锁**

`start_finalize()` 用数据库 CAS 从 `uploading` 进入 `finalizing`，重复调用返回同一 job。`ImportJob.run()` 返回冻结的 `PublishedImport`；Task 4 完成后 SQLite 仍为 `finalizing`，而“正式目录完整、staging 已消失、尚无 Session”推导为文件系统阶段 `published_unlinked`，不增加新的 SQLite 状态。finalize worker 持有 gate activity 到发布成功、失败或取消；批次锁仲裁 cancel/finalize；取消终止 parser 子进程并删除 staging。恢复逻辑按 SQLite 状态和目录事实重建同一个 `PublishedImport`，绝不创建 Session。worker 的 `finally` 释放 activity 并调用 `store.close_thread_connection()`。

- [x] **Step 5: 跑发布恢复检查并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_import_store tests.test_session_recovery -v
```

Expected: 全部 PASS；没有残留 parser 进程或 staging 目录。

Commit:

```zsh
git add local-agent/local_agent/imports.py local-agent/local_agent/session_store.py \
  local-agent/tests/test_import_store.py local-agent/tests/test_session_recovery.py
git commit -m "Publish recoverable imported workspaces"
```

### Task 5: 用统一 Resolver 接通持久 Session

**Files:**
- Modify: `local-agent/local_agent/imports.py`
- Modify: `local-agent/local_agent/sessions.py:291-470`
- Modify: `local-agent/local_agent/sessions.py:804-1195`
- Modify: `local-agent/local_agent/web_runs.py:69-170`
- Modify: `local-agent/local_agent/web_runs.py:425-579`
- Modify: `local-agent/local_agent/__main__.py:42-193`
- Create: `local-agent/tests/test_managed_workspace.py`
- Modify: `local-agent/tests/test_session_web.py`
- Modify: `local-agent/tests/test_session_runtime.py`
- Modify: `local-agent/tests/test_session_cli.py`

- [x] **Step 1: 写 Web、CLI 和直接执行共享授权的失败测试**

```python
def test_import_session_ignores_forged_database_workspace_path(self):
    session = self.ready_import_session()
    self.store.connection().execute(
        'UPDATE sessions SET workspace_path=? WHERE id=?',
        (str(self.secret_directory), session.id))
    resolved = self.resolver.resolve(self.service.load(session.id))
    self.assertEqual(resolved.read_root, self.import_root / session.import_id / 'workspace')
    self.assertNotEqual(resolved.read_root, self.secret_directory)

def test_tampered_import_stops_direct_execute_before_provider(self):
    prepared = self.prepare_import_run()
    self.tamper_chunk(prepared.session_id)
    result = self.service.execute(prepared, FailIfCalledProvider(), self.trace())
    self.assertEqual(result['state'], 'failed')
    self.assertEqual(result['stop_reason'], 'IMPORT_INTEGRITY_ERROR')
    self.assertEqual(result['model_calls'], 0)
```

再覆盖 `before_link_transaction`、`after_link_commit` 两个关联故障点及重复恢复，断言同一 import 最多产生一个 Session；同时覆盖其他 import ID、目录 inode 替换、symlink、`ready` 摘要错误转为 `unavailable`、Web 列表过滤、CLI continue、selected Session 重启和无效导入会话“历史可读、新 Run 拒绝”。普通 workspace 丢失时既有历史会话行为保持；文件 Run 的执行前失败沿用现有持久化结果契约，不抛出未记录的异常。

- [x] **Step 2: 运行并确认现有代码仍信任 `workspace_path`**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=.:tests .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_managed_workspace tests.test_session_runtime tests.test_session_web \
  tests.test_session_cli -v
```

Expected: 伪造路径或导入 Session 断言 FAIL。

- [x] **Step 3: 实现 resolver 与事务内 Session 关联**

```python
@dataclass(frozen=True)
class ResolvedWorkspace:
    read_root: Path
    read_identity: tuple[int, int]
    write_root: Path
    write_identity: tuple[int, int]
    source_mapper: 'ImportedSourceMapper | None'

class ManagedWorkspaceResolver:
    def resolve(self, session: SessionRecord) -> ResolvedWorkspace:
        if session.import_id is None:
            return self._ordinary(session)
        return self._imported(session.import_id)
```

导入分支只从固定 `ImportStore.root` fd 与 32 位十六进制 ID 派生子目录，逐级 `O_DIRECTORY|O_NOFOLLOW` 打开，校验 DB identity、manifest 摘要与相关文件摘要。`SessionService.attach_import(published, request)` 只接收 Task 4 生成或恢复的 `PublishedImport`，在一个 SQLite 事务中写 parser file 记录、插入带冻结助手快照的 Session、设置唯一 `import_id`、记录两个根 identity 并把 import 置 `ready`。事务前后故障恢复重放同一关联操作，依靠 `sessions.import_id` 唯一约束返回同一个 Session，不能重新解析或产生第二个 Session。

- [x] **Step 4: 替换所有执行入口的路径来源**

`SessionService.submit()` 在短期 gate activity 内完成 resolve 和入队事务；`execute()` 取得覆盖整次 Runtime 的 activity，再次 resolve，最后在 `finally` 释放。本任务保持现有 `assemble(..., workspace: Path, ...)` 接口，只传 `resolved.read_root`；完整 `ResolvedWorkspace` 保留独立写根，Task 7 再接入 assembly。Task 5 期间导入 Session 的 `output_path` 必须在执行前拒绝，不能临时写入只读资料根。`WebRuns._visible/list_sessions/_require_session` 合并普通绑定 Session 与 resolver 有效的导入 Session。CLI `_open_service/_persistent_run` 构造同一个 ImportStore/resolver；Trace 使用受信 read root。任何入口都不能从导入 Session 的 `workspace_path` 授权。

- [x] **Step 5: 跑三入口检查并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=.:tests .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_managed_workspace tests.test_session_runtime tests.test_session_web \
  tests.test_session_cli -v
```

Expected: 全部 PASS；Provider 在完整性失败场景调用数为 0。

Commit:

```zsh
git add local-agent/local_agent/imports.py local-agent/local_agent/sessions.py \
  local-agent/local_agent/web_runs.py local-agent/local_agent/__main__.py \
  local-agent/tests/test_managed_workspace.py local-agent/tests/test_session_runtime.py \
  local-agent/tests/test_session_web.py local-agent/tests/test_session_cli.py
git commit -m "Resolve managed session workspaces consistently"
```

### Task 6: 加入受限搜索、事实账本与原始位置引用

**Files:**
- Create: `local-agent/local_agent/import_tools.py`
- Modify: `local-agent/local_agent/agents.py:19-199`
- Modify: `local-agent/local_agent/file_tools.py:151-286`
- Modify: `local-agent/local_agent/answers.py:34-101`
- Modify: `local-agent/local_agent/agent_runtime.py:173-330`
- Modify: `local-agent/local_agent/conversation.py:211-230`
- Modify: `local-agent/local_agent/sessions.py:118-151,1280-1360`
- Create: `local-agent/tests/test_import_runtime.py`
- Create: `local-agent/tests/test_import_citations.py`
- Modify: `local-agent/tests/test_agents.py`
- Modify: `local-agent/tests/test_agent_sessions.py`
- Modify: `local-agent/tests/test_managed_workspace.py`

- [x] **Step 1: 写搜索、coverage、旧快照和组合助手失败测试**

```python
def test_search_result_is_returned_to_next_model_call_but_not_fact_coverage(self):
    result = self.run_scripted([
        tool_call('s1', 'search_documents', {'query': '预算'}),
        tool_call('r1', 'read_file', {'path': self.budget_chunk}),
        answer(citation(self.budget_chunk, 1, 1)),
    ])
    self.assertEqual(result['answer']['citations'][0]['source']['locations'],
                     [{'kind': 'pdf_page', 'page': 2}])
    self.assertEqual(self.provider.requests[1]['messages'][-1]['tool_call_id'], 's1')

    policy = self.policy(two_chunks=True)
    policy.accept('search_documents', {'query': '预算'}, self.search_result())
    self.assertEqual(policy.fact_coverage()['read_files'], [])

def test_catalog_is_not_citable_and_does_not_block_complete_not_found(self):
    policy = self.policy(two_chunks=True)
    policy.accept('read_file', {'path': 'index.md'}, self.read('index.md'))
    for path in policy.fact_coverage()['discovered_files']:
        policy.accept('read_file', {'path': path}, self.read(path))
    self.assertTrue(policy.fact_coverage()['complete'])
    with self.assertRaisesRegex(AnswerError, 'INVALID_CITATION'):
        policy.validate(answer_json(path='index.md'))
```

覆盖 query 256 个 UTF-8 bytes、多字节边界、20 hits、384-byte excerpt、最终 wire result 12 KiB、1 秒、稳定排序、跨 import 拒绝、运行中 manifest／locations／chunk 替换拒绝、尚有正文未读时 `not_found` 被拒绝、全部正文已读时允许。分别走 `directory-qa`、`project-brief` 和持久 Session；确保组合 `ExtensionPolicy.validate()` 不绕过来源映射，普通工作区、`file-qa` 和旧冻结快照都不获得搜索。

另用真实 `Runtime` 请求构造链覆盖“搜索 + 两个 12 KiB 正文块”，请求包含系统提示、工具 schema、scope、历史和需要 JSON 转义的正文。测试最终 provider payload 的 UTF-8 大小，而不是只累加正文长度；不得靠提高 64 KiB 上限、丢失正文或把 `CONTEXT_LIMIT` 当作预算通过。

- [x] **Step 2: 运行并确认工具白名单和 policy 尚不支持搜索**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=.:tests .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_agents tests.test_agent_sessions tests.test_import_runtime \
  tests.test_import_citations tests.test_managed_workspace tests.test_answers \
  tests.test_extensions -v
```

Expected: `search_documents` 被白名单拒绝或 adapter 不存在，新增测试 FAIL。

- [x] **Step 3: 实现搜索 adapter 与 `ImportedDocumentPolicy`**

`SEARCH_DOCUMENTS_SCHEMA` 只接受 `query`；adapter 校验 manifest 和涉及的 chunk SHA-256，按 casefold 子串搜索，返回：

```json
{
  "ok": true,
  "query": "预算",
  "evidence_role": "catalog",
  "matches": [
    {"path":"documents/abc/chunk-0002.md","start_line":3,"end_line":3,
     "excerpt":"预算：42","locations":[{"kind":"pdf_page","page":2}]}
  ]
}
```

`ImportedDocumentPolicy` 从 resolver 已校验的 manifest 初始化全部正文 chunk，并直接持有 manifest 事实白名单、预期摘要与当前 import 身份；它负责 `read_file` 的授权和证明，不借用 `DirectoryTools.record()` 的目录发现状态。`index.md` 读取快照和 `search_documents` 结果都标记 `evidence_role: catalog` 并单独记录，正文在同一次受身份保护的读取中通过预期 SHA-256 后才进入 snapshots/read_files。`fact_coverage()` 返回 `attempted/had_error/discovered_files/read_files/unread_files/complete`，其中 catalog 永远不在三个事实集合里。`validate_answer()` 增加明确的 imported completeness 分支，不伪造普通目录的 `listed_directories=['.']`，也不放宽普通目录规则。

搜索 adapter 用紧凑 `result_fields()`，最终 wire JSON 逐条接纳 hit，不能把完整 chunk 路径集合复制进每个工具结果；完整 `fact_coverage` 只保留在 Run 最终 scope。导入 policy 的模型请求投影可在模型已经据搜索结果选择正文后，将旧搜索结果压缩为带原 ToolCall ID 的 catalog 标记，并用无损、可辨行边界的正文表示减少嵌套 JSON 转义；Runtime 仍对最终请求执行 65,536-byte 硬上限。

- [x] **Step 4: 接入 Agent 与组合 policy，最后再 enrich**

将 `search_documents` 加入白名单；当前内置 `directory-qa/project-brief` 声明它，`file-qa` 不声明。导入面板以后只使用兼容助手；旧冻结快照保持原工具。`ManagedWorkspaceResolver` 从已校验 manifest／locations 构造只读 mapper，`SessionService.execute()` 把它传入 `assemble()`；普通 workspace 保持 `source_mapper=None`。`build_file_engine()`／`adapt_tools()` 使用向后兼容的可选 policy 与 adapter 注入点，只在 import 有效且冻结助手声明工具时注册搜索。`ExtensionPolicy.before/accept()` 和 `SessionTaskPolicy.FILE_TOOLS` 纳入搜索，但搜索只交给 base 导入 policy。

`ImportedSourceMapper.enrich(validated_answer, snapshots)` 只在顶层严格引用校验成功后运行；模型提供 `source` 仍因多余字段被拒绝。mapper 把引用行映射成有序、相邻合并的 `source.locations`；缺行、hash 变化或 catalog 路径返回 `INVALID_CITATION`。组合助手先完成 MCP/Skill/历史与本地引用校验，再只对本地 chunk 引用 enrich。

- [x] **Step 5: 跑工具与引用检查并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=.:tests .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_agents tests.test_agent_sessions tests.test_import_runtime \
  tests.test_import_citations tests.test_managed_workspace tests.test_answers \
  tests.test_extensions -v
```

Expected: 全部 PASS；搜索 envelope 与事实 scope 分开受限，构造后的“搜索 + 两个 12 KiB 块”在含转义、schema 和持久上下文时仍不超过 64 KiB，正文与 ToolCall ID 无损保留。

Commit:

```zsh
git add local-agent/local_agent/import_tools.py local-agent/local_agent/agents.py \
  local-agent/local_agent/file_tools.py local-agent/local_agent/answers.py \
  local-agent/local_agent/agent_runtime.py local-agent/local_agent/conversation.py \
  local-agent/local_agent/sessions.py \
  local-agent/tests/test_import_runtime.py local-agent/tests/test_import_citations.py \
  local-agent/tests/test_agents.py local-agent/tests/test_agent_sessions.py \
  local-agent/tests/test_managed_workspace.py
git commit -m "Search imported documents with mapped citations"
```

### Task 7: 分离 Artifact 写根并提供受权下载

**Files:**
- Modify: `local-agent/local_agent/agents.py:176-199`
- Modify: `local-agent/local_agent/file_tools.py:262-286`
- Modify: `local-agent/local_agent/agent_runtime.py:365-412`
- Modify: `local-agent/local_agent/imports.py`
- Modify: `local-agent/local_agent/sessions.py:672-788`
- Modify: `local-agent/local_agent/session_store.py:1328-1370`
- Modify: `local-agent/local_agent/web_runs.py`
- Modify: `local-agent/local_agent/web.py`
- Modify: `local-agent/local_agent/static/app.js:478-493`
- Create: `local-agent/tests/test_import_artifacts.py`
- Modify: `local-agent/tests/test_managed_workspace.py`
- Modify: `local-agent/tests/test_session_recovery.py`
- Modify: `local-agent/tests/test_session_web.py`
- Modify: `local-agent/tests/browser_tools.cjs`

- [x] **Step 1: 写独立写根、回执与下载授权的失败测试**

```python
def test_approved_import_write_creates_only_one_declared_artifact(self):
    result = self.run_import(output_path='report.md', approve=True)
    artifact = self.import_root / self.import_id / 'artifacts' / 'report.md'
    self.assertTrue(artifact.is_file())
    self.assertFalse((self.import_root / self.import_id / 'workspace' / 'report.md').exists())
    self.assertEqual(result['artifacts'][0]['sha256'], sha256(artifact.read_bytes()).hexdigest())

def test_download_rejects_cross_session_and_changed_artifact(self):
    artifact = self.create_approved_artifact()
    self.assertEqual(self.http_download(other_session_id, artifact.id).status, 404)
    artifact.path.write_text('changed')
    self.assertEqual(self.http_download(artifact.session_id, artifact.id).json()['error'],
                     'IMPORT_INTEGRITY_ERROR')
```

继续覆盖：未声明不注册 write、模型改名拒绝、拒绝后无文件、一次成功、不覆盖、symlink、未知发布只检查 `artifacts/`、缺 token/错误 Origin/Agent/Run/Artifact 所属关系拒绝。

- [x] **Step 2: 运行并确认当前 `WriteFile` 仍写入读取根**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=.:tests .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_import_artifacts tests.test_session_web tests.test_write_file -v
```

Expected: 独立根或下载路由断言 FAIL；既有写入测试保持可运行。

- [x] **Step 3: 贯通独立 `write_root`**

将装配接口改为：

```python
def build_file_engine(agent, read_root, target_path, output_path=None,
                      *, write_root=None, read_identity=None,
                      write_identity=None, source_mapper=None):
    # 导入会话由统一 resolver 同时提供独立读写根和两个目录 identity。
    ...

def adapt_tools(tool, output_path=None, *, agent=None,
                write_root=None, write_identity=None, policy=None):
    root = tool.workspace if write_root is None else write_root
    identity = tool.workspace_identity if write_identity is None else write_identity
    if output_path is not None:
        adapters.append(WriteFile(root, output_path, identity))
```

`SessionService.execute()` 只传 resolver 的根与 identity。`inspect_unknown_publication()` 改接已解析的 write root，不再查询并信任 `workspace_path`。普通 Session 的默认行为不变。

- [x] **Step 4: 实现 Artifact 查询和二进制下载**

`SessionService.open_artifact(session_id, run_id, artifact_id, agent_id)` 先验证数据库归属，再 resolve write root，以安全目录 fd 打开回执 path，核对普通文件、identity、bytes 和 SHA-256；在同一次读取中冻结最多 32 KiB 的已验真字节并关闭原文件 fd，再返回字节、长度与下载名。`Handler` 使用独立 `respond_file()` 只发送该冻结快照，并写入 `Content-Type: application/octet-stream`、经过 CR/LF 拒绝和 RFC 5987 编码的 `Content-Disposition: attachment`、`Content-Length` 与既有安全 headers；路由不接受 path 参数。

Artifact 卡片使用按钮调用带 `X-Session-Token` 与当前 `X-Agent-ID` 的 fetch，成功后创建短生命周期 Blob URL 并触发浏览器下载，随后 revoke；不使用无法附加认证 Header 的裸链接。引用渲染同时识别导入来源的 `kind/name/logical_path/locations`，把 PDF 页、文本行、Word 段落和表格行作为只读定位信息展示，所有动态字段继续只写 `textContent`。

- [x] **Step 5: 跑 Artifact 回归并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=.:tests .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_import_artifacts tests.test_session_web tests.test_write_file -v
/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  --check local_agent/static/app.js
/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  tests/browser_tools.cjs
```

Expected: Python 检查与 JavaScript 语法检查全部 PASS；普通 Session 写入和审批回归无变化。

Actual: 最终实现运行 632 项完整 Python 回归并通过；`app.js`、`browser_tools.cjs` 语法检查与既有浏览器离线交互脚本通过。下载竞态回归证明原文件在验真后被等长改写时，HTTP 仍只返回已验真的冻结内容。

Commit:

```zsh
git add local-agent/local_agent/agents.py local-agent/local_agent/file_tools.py \
  local-agent/local_agent/agent_runtime.py local-agent/local_agent/imports.py \
  local-agent/local_agent/sessions.py \
  local-agent/local_agent/session_store.py local-agent/local_agent/web_runs.py \
  local-agent/local_agent/web.py local-agent/local_agent/static/app.js \
  local-agent/tests/test_import_artifacts.py local-agent/tests/test_managed_workspace.py \
  local-agent/tests/test_session_recovery.py local-agent/tests/test_session_web.py \
  local-agent/tests/browser_tools.cjs
git commit -m "Separate and download imported artifacts"
```

### Task 8: 实现一致备份与原子恢复

**Files:**
- Create: `local-agent/local_agent/state_backup.py`
- Modify: `local-agent/local_agent/session_store.py:964-994`
- Modify: `local-agent/local_agent/sessions.py:1197-1207`
- Modify: `local-agent/local_agent/__main__.py:120-153`
- Modify: `local-agent/local_agent/__main__.py:251-283`
- Create: `local-agent/tests/test_state_backup.py`
- Modify: `local-agent/tests/test_session_cli.py`

- [x] **Step 1: 写成对备份、竞争和恢复失败测试**

```python
def test_bundle_backup_and_restore_rebind_import_identity(self):
    session = self.ready_import_with_artifact()
    bundle = self.backup.backup_bundle(self.backups)
    self.assertTrue(bundle.database.is_file())
    self.assertTrue(bundle.sidecar.is_dir())
    self.assertTrue(bundle.manifest.is_file())
    destination = self.root / 'restored-state'
    StateBackup.restore_bundle(bundle.database, bundle.sidecar, destination)
    restored = self.open_service(destination)
    self.assertEqual(restored.load(session.id).import_id, session.import_id)
    self.assertNotEqual(restored.resolve(session.id).read_identity,
                        self.original_read_identity)

def test_backup_refuses_run_and_artifact_races(self):
    lease = self.store.maintenance_gate.start('run', 'run-1')
    with self.assertRaisesRegex(StoreError, 'STATE_BUSY'):
        self.service.backup(self.backups)
    lease.close()
```

加入：目标已存在、sidecar 缺失、DB/hash/file 篡改、active import/approval、backup 持锁时新 Run 拒绝、恢复在 rename 前失败无 destination、rename 后父目录 fsync、恢复后 resolver 全量验证。

- [x] **Step 2: 运行并确认当前 backup 只有 SQLite**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_state_backup tests.test_session_cli -v
```

Expected: sidecar、maintenance 或 restore 断言 FAIL。

- [x] **Step 3: 实现 `StateBackup` bundle**

`backup_bundle()` 先取得 `maintenance_gate.maintenance()`，再查询 SQLite；存在未结束 Run、`uploading/finalizing` import 或 pending/allowed 审批时返回 `STATE_BUSY`。确认静止后调用现有安全 SQLite backup，并从快照库查询 `ready` import ID，将对应 originals/workspace/artifacts/manifest/locations 复制到 `<db-name>.imports/` 临时目录。总 manifest canonical JSON 至少为：

```json
{"schema_version":1,"database":{"name":"sessions.sqlite3","sha256":"0000000000000000000000000000000000000000000000000000000000000000"},
 "imports":[{"id":"0123456789abcdef0123456789abcdef","manifest_sha256":"1111111111111111111111111111111111111111111111111111111111111111","files":[
   {"path":"workspace/index.md","bytes":123,"sha256":"2222222222222222222222222222222222222222222222222222222222222222"}]}]}
```

所有文件和目录 `fsync` 后无覆盖 rename；任一失败清理本次新建的 DB/sidecar/manifest。`SessionService.backup()` 返回三个路径与 Session/Run/import 数量。

- [x] **Step 4: 实现不存在目标目录的一次性 restore**

`restore_bundle()` 拒绝已存在 destination；在同一父目录创建随机 staging，复制并校验整个 bundle，打开离线 DB 更新导入 Session 的派生展示路径和新设备/inode，在临时 Store 上运行 resolver 全量验证，关闭所有 fd/连接后将 staging 一次 rename 为 destination 并 `fsync` 父目录。CLI 新增 `sessions restore-backup DATABASE SIDECAR DESTINATION`，且在 `_sessions_command()` 调用 `_open_service()` 之前单独分派，避免预先创建 destination；保留现有 `sessions restore SESSION_ID` 的解除归档语义。

- [x] **Step 5: 跑备份恢复检查并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_state_backup tests.test_session_cli -v
```

Expected: 全部 PASS；原有 WAL backup 测试继续通过。

Commit:

```zsh
git add local-agent/local_agent/state_backup.py local-agent/local_agent/session_store.py \
  local-agent/local_agent/sessions.py local-agent/local_agent/__main__.py \
  local-agent/tests/test_state_backup.py local-agent/tests/test_session_cli.py
git commit -m "Back up and restore managed imports"
```

### Task 9: 接入受限 HTTP 导入协议

**Files:**
- Modify: `local-agent/local_agent/web.py:33-197`
- Modify: `local-agent/local_agent/web_runs.py:69-116`
- Modify: `local-agent/local_agent/web_runs.py:678-697`
- Create: `local-agent/tests/test_import_web.py`

- [x] **Step 1: 写真实 loopback 上传、轮询、取消与认证失败测试**

使用 `http.client` 直接发送 JSON 和 bytes：

```python
status, begin = self.request('POST', '/api/imports', metadata)
self.assertEqual(status, 201)
slot = begin['files'][0]['slot_id']
self.assertEqual(self.request_bytes(
    'POST', f"/api/imports/{begin['id']}/files/{slot}", b'项目：青禾-47\n',
    content_type='application/octet-stream')[0], 200)
self.assertEqual(self.request('POST', f"/api/imports/{begin['id']}/complete", {})[0], 202)
ready = self.wait_import(begin['id'])
self.assertEqual(ready['status'], 'ready')
self.assertTrue(ready['session_id'])
```

覆盖：元数据 128 KiB/128 KiB+1，其他 JSON 仍 16 KiB，缺/错 `Content-Length`，chunked、错误 Content-Type、Host/Origin/token/Agent、重复 slot/complete、另一 Agent 访问、解析失败模型调用 0、并行第二个 finalize 409、取消后 staging 删除、server close 回收 worker。

- [x] **Step 2: 运行并确认路由不存在**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest tests.test_import_web -v
```

Expected: `/api/imports` 返回 404 或路由断言 FAIL。

- [x] **Step 3: 在 JSON 通用分支之前实现导入路由解析**

`Handler.dispatch()` 先识别导入路径：begin 只读最大 128 KiB JSON；slot 只接受 `application/octet-stream`、单一十进制 `Content-Length` 且拒绝 `Transfer-Encoding`；complete/cancel 使用既有小 JSON；snapshot 用 GET。所有路径先经过现有 `authorize(api=True)`，再固定核对 batch 的 Agent ID。异常只返回设计稿稳定 code，不返回路径或堆栈。

`WebRuns.config()` 增加稳定的 `imports` 能力对象，列出四种格式的 parser availability 和设计中的数量/大小上限；begin 在读取正文前再次拒绝当前不可用的 PDF/DOCX parser。Session 详情在 `import_id` 有效时附加受限 import 摘要，包括逻辑路径、格式、bytes/hash、页/段/表格统计、警告和块数，不返回 originals 路径、State Store 绝对路径或位置表全文。

- [x] **Step 4: 在 `WebRuns` 编排单个 finalize worker**

新增 `import_jobs`，以及 `begin_import(data)`、`upload_import_file(import_id, slot_id, stream, content_length)`、`complete_import(import_id)`、`import_snapshot(import_id)`、`cancel_import(import_id)` 五个明确方法；分别只调用同名 ImportStore 操作并转换 Web 错误，不复制校验逻辑。

complete worker 依次运行 publish、`SessionService.attach_import()`，再发布 ready snapshot；用一把锁保证同服务最多一个活跃解析。`close()` 取消并 join worker、关闭线程连接。HTTP 状态固定为 201/200/202/409/413/415/422/503，并由测试逐项锁定。

- [x] **Step 5: 跑 HTTP 与既有 Web 回归并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest \
  tests.test_import_web tests.test_web tests.test_session_web tests.test_agent_web -v
```

Expected: 全部 PASS；旧路由大小与认证规则未放宽。

Commit:

```zsh
git add local-agent/local_agent/web.py local-agent/local_agent/web_runs.py \
  local-agent/tests/test_import_web.py
git commit -m "Expose managed document import endpoints"
```

Actual: commit `d153368`。11 项 `test_import_web` 与包含审批入口的 55 项 Web 回归通过；解析失败不创建 Session、模型调用为 0，关联失败和 worker 启动失败均有可停止且可重试的 HTTP 状态。限定安全 Gate PASS。

### Task 10: 完成页面导入交互

**Files:**
- Modify: `local-agent/local_agent/static/index.html:33-65`
- Modify: `local-agent/local_agent/static/app.js`
- Modify: `local-agent/local_agent/static/app.css`
- Modify: `local-agent/tests/web_fixture.py`
- Create: `local-agent/tests/browser_imports.cjs`
- Modify: `local-agent/tests/browser_workbench.cjs`

- [x] **Step 1: 写浏览器预检、上传、取消、焦点和迟到响应失败断言**

`browser_imports.cjs` 加载真实静态文件并拦截 HTTP；使用 `setInputFiles()` 选择 MD/TXT/PDF/DOCX 和 PNG，断言：

```javascript
await page.locator('#import-trigger').click();
await page.locator('#import-files').setInputFiles(files);
assert.equal(await page.locator('#import-supported-count').textContent(), '4');
assert.equal(await page.locator('#import-ignored-count').textContent(), '1');
await page.locator('#import-start').click();
assert.deepEqual(uploaded.map(item => item.contentType),
  Array(4).fill('application/octet-stream'));
await expectText(page, '#import-status', '本地副本已保存 · 4 份资料');
assert.equal(await page.locator('#question').evaluate(node => node === document.activeElement), true);
```

再覆盖文件夹的 `webkitRelativePath`、关闭后焦点归还、取消 AbortController + 服务端 cancel、刷新不重复 complete、上一导入迟到轮询丢弃、恶意文件名用 textContent、兼容助手过滤、parser unavailable、导入引用位置展示，以及下载按钮确实用认证 fetch 获取 Blob；检查 320/375/768/1024/1440px 无横向滚动和隐藏面板不可 Tab。

- [x] **Step 2: 运行并确认页面节点不存在**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
env NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  tests/browser_imports.cjs
```

Expected: `#import-trigger` 或导入面板断言 FAIL。

- [x] **Step 3: 增加导入面板语义结构**

左栏会话区加入“导入资料”按钮；dialog 内包含独立 `#import-files[multiple][accept]` 与 `#import-directory[webkitdirectory][multiple]`、会话名称、兼容助手、预检表、忽略列表、隐私说明、开始/取消按钮和 `aria-live` 进度。不要把文件名或 parser 错误写入 `innerHTML`。

- [x] **Step 4: 实现浏览器状态机和二进制上传**

```javascript
let importGeneration = 0;
let activeImport = null;

async function uploadSlot(importId, slot, file, signal) {
  return apiBinary(`/api/imports/${importId}/files/${slot}`, file, signal);
}

async function pollImport(importId, generation) {
  while (generation === importGeneration) {
    const snapshot = await api(`/api/imports/${importId}`);
    if (generation !== importGeneration) return;
    renderImport(snapshot);
    if (['ready', 'failed', 'cancelled'].includes(snapshot.status)) return snapshot;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
}
```

页面先本地重算格式、逻辑路径、数量和大小，再提交 metadata；逐 slot 上传并显示数量；complete 只发送一次。成功后刷新 Session 列表、选择返回的 Session ID、关闭面板、聚焦 Composer，并在右栏展示来源路径、hash、页/段统计、警告和块数。取消同时 abort 当前 fetch 并调用服务端 cancel。

- [x] **Step 5: 跑新旧浏览器回归并提交**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
node_bin=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
node_path=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules
for suite in browser_imports.cjs browser_workbench.cjs browser_extensions.cjs browser_tools.cjs; do
  env NODE_PATH="$node_path" "$node_bin" "tests/$suite"
done
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. .venv/bin/python tests/web_fixture.py --port 8767' --port 8767 -- \
  env NODE_PATH="$node_path" "$node_bin" tests/browser_sessions.cjs
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. .venv/bin/python tests/web_fixture.py --port 8767' --port 8767 \
  --server 'PYTHONPATH=. .venv/bin/python tests/web_fixture.py --port 8768 --missing' --port 8768 -- \
  env NODE_PATH="$node_path" "$node_bin" tests/browser_web.cjs
```

Expected: 六组均 PASS。

Commit:

```zsh
git add local-agent/local_agent/static/index.html local-agent/local_agent/static/app.js \
  local-agent/local_agent/static/app.css local-agent/tests/web_fixture.py \
  local-agent/tests/browser_imports.cjs local-agent/tests/browser_workbench.cjs
git commit -m "Add document import to the workbench"
```

### Task 11: 固定闭环、文档与最终 Gate

**Files:**
- Create: `local-agent/tests/test_import_acceptance.py`
- Create: `local-agent/local_agent/import_evaluation.py`
- Modify: `local-agent/local_agent/__main__.py`
- Modify: `local-agent/README.md`
- Modify: `local-agent/trial/directory-check.md`
- Modify: `local-agent/tests/test_session_recovery.py`
- Modify: `docs/superpowers/plans/2026-09-15-local-document-import.md`

- [x] **Step 1: 写混合资料离线闭环失败测试**

固定资料包含 MD、TXT、两页文字 PDF、段落/表格 DOCX 和被忽略 PNG。`ScriptedProvider` 必须依次发出 search、read PDF、read DOCX，再回答。断言初始请求没有正文；两个 tool result 按原 ID 进入紧接的下一轮；引用页码与 Word 位置正确；原始文件移走并重启仍可追问；另一 import 无法读取；成对备份恢复到新 State Store 后再次追问成功。

```python
self.assertNotIn('Budget 42', first_request_json)
self.assertEqual(request_after_search['messages'][-1]['tool_call_id'], 'search-1')
self.assertEqual(result['answer']['citations'][0]['source']['locations'],
                 [{'kind': 'pdf_page', 'page': 2}])
self.assertEqual(result['answer']['citations'][1]['source']['locations'],
                 [{'kind': 'docx_table_row', 'table': 1, 'row': 1}])
```

- [x] **Step 2: 运行导入定向组和完整 Python 回归**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest discover \
  -s tests -p 'test_import*.py' -v
PYTHONPATH=. .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests
```

Expected: 导入定向组全部 PASS；完整回归 0 failure/0 error/0 ResourceWarning。

Actual: 导入定向组 127 项 PASS；完整回归 661 项 PASS，0 failure/0 error/0 ResourceWarning。首次完整运行发现既有 FIFO 检查线程未关闭自己的 SQLite thread-local connection，仅在该测试线程的 `finally` 调用现有清理接口；相关 53 项及完整组重跑均无警告，产品运行逻辑未改。

- [x] **Step 3: 跑静态语法和全部浏览器回归**

Run:

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node \
  --check local_agent/static/app.js
node_bin=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node
node_path=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules
for suite in browser_imports.cjs browser_workbench.cjs browser_extensions.cjs browser_tools.cjs; do
  env NODE_PATH="$node_path" "$node_bin" "tests/$suite"
done
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. .venv/bin/python tests/web_fixture.py --port 8767' --port 8767 -- \
  env NODE_PATH="$node_path" "$node_bin" tests/browser_sessions.cjs
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. .venv/bin/python tests/web_fixture.py --port 8767' --port 8767 \
  --server 'PYTHONPATH=. .venv/bin/python tests/web_fixture.py --port 8768 --missing' --port 8768 -- \
  env NODE_PATH="$node_path" "$node_bin" tests/browser_web.cjs
```

Expected: 语法检查与六组浏览器测试全部 PASS。

Actual: JavaScript 语法与 `browser_imports`、`browser_workbench`、`browser_extensions`、`browser_tools`、`browser_sessions`、`browser_web` 六组全部 PASS。

- [x] **Step 4: 更新玩法、限制和当前阶段证据**

README 写明页面“导入资料”玩法、四种格式、纯图片 PDF 不支持、受管副本位置、初始请求无正文而搜索会发送受限命中摘录、读取会发送选中正文、Artifact 下载、备份恢复命令及本地长期保存提示。`directory-check.md` 顶部新增当前阶段入口，只陈述已经取得的离线/真实证据和剩余限制；不覆盖历史失败。

- [x] **Step 5: 提交离线闭环并执行代码 Review Gate**

Commit:

```zsh
git add local-agent/tests/test_import_acceptance.py local-agent/local_agent/import_evaluation.py \
  local-agent/local_agent/__main__.py local-agent/README.md local-agent/trial/directory-check.md \
  docs/superpowers/plans/2026-09-15-local-document-import.md
git commit -m "Complete managed document import loop"
```

Review 只检查设计验收项、权限绕过、虚假执行、来源映射、资源上限和备份一致性。P0/P1 修完并重跑受影响检查；一般 UI 建议记录后置，不扩大本批。

Actual: 权限／上下文审查 PASS。重启／恢复审查先发现“reopen 后只 resolve”及异常后必需检查可能不完整两项阻塞；已增加 reopen 后真实 search/read/引用 probe，并要求无 error、必需检查键精确完整且全真，故障注入测试通过；复审 PASS。

Commit: `3ddb60e`（`Complete managed document import loop`），已推送至 `origin/LocalAgentRuntime`。

- [ ] **Step 6: 在所有离线证据通过后运行一次受限 DeepSeek 验收**

只在调用进程已经通过环境变量取得 `DEEPSEEK_API_KEY` 时运行：

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent"
PYTHONPATH=. .venv/bin/python -m local_agent evaluate-imports
```

验收只用合成的小混合资料，首问跨 PDF/Word 找两项事实，次问引用上一轮结论并核对文本资料。上限为 2 个 Run、12 次模型请求；输出原始机器报告和待语义复核状态。缺密钥时记录 `NOT_RUN`，不得把离线 Provider 冒充真实模型通过，也不得索取或记录密钥。

Actual: 当前进程无密钥，命令返回 `NOT_RUN / CONFIG_MISSING`，模型调用 0；原始报告位于本地忽略目录 `trial/results/import-evaluation-1e84050dd0624e17a443fe949f4322ca/report.json`。真实 2 Run 尚未执行。

- [ ] **Step 7: 核对真实结果、更新 §0 并推送**

逐条核对回答事实、引用位置、搜索/读取 ToolCall ID、上下文回填、跨轮历史和任何 Artifact。将 AI 语义复核另存于评测目录，不覆盖原报告，不冒充用户签字。更新本计划 §0 与阶段记录后运行：

```zsh
cd "/Users/wujingyu/Desktop/AI/projects/dev-agent"
git diff --check
git status --short
git push origin LocalAgentRuntime
git rev-parse HEAD
git ls-remote origin refs/heads/LocalAgentRuntime
```

Expected: 只有本批已审阅文件进入提交；本地与远端 commit 相同。达到停止条件即收口，不追加更多真实样例。

Current: 实现 commit `3ddb60e` 已推送。由于 Step 6 为 `NOT_RUN`，本步骤中的真实结果语义复核与最终状态提交仍待真实 2 Run 完成；不影响已验证实现的远端保存。
