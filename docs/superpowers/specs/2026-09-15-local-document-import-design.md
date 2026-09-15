# Local Agent 本地文档与文件夹导入设计

## §0 当前结论

2026-09-15：用户确认在现有会话工作台中增加“导入资料”，第一版支持 `.md`、`.txt`、带文字层的 `.pdf` 和 `.docx`，并允许选择整个本地文件夹。每次导入保存一份本地副本，创建独立受管资料空间并自动建立持久会话。扫描版或纯图片 PDF 不做 OCR；解析失败时不调用模型，也不创建残缺会话。

本设计只处理“明确选择本地资料 → 保存副本 → 本地解析 → 固定会话范围 → Agent 按需检索和读取 → 返回可核对引用”的闭环。它不增加云端文件存储、自动同步、OCR、拖拽、删除资料或任意磁盘浏览，也不改变用户预先声明唯一输出目标及每个 Run 最多成功写一次的 `write_file` 契约。

当前状态：设计 Review Gate 已通过，P0/P1 阻断项为 0；产品代码尚未实施，等待用户审阅本成文方案后进入实施计划。

## 1. 目标与完成条件

用户应能在当前页面完成以下过程，不再手填文件相对路径或为每个文件夹重启服务：

1. 点击“导入资料”，选择一个或多个文件，或者选择一个文件夹。
2. 在模型调用前看见支持文件、忽略文件、数量和大小，并主动开始导入。
3. 系统将原文件复制到本机受管目录，在本机提取文本并建立定位信息。
4. 全批成功后创建持久会话，并把该次导入的独立资料空间固定为会话范围。
5. Agent 通过索引、受限搜索和 `read_file` 选择实际资料；工具结果按原调用 ID 回填下一轮。
6. 最终引用展示原文件名以及文本行、PDF 页码或 Word 段落／表格位置。
7. 服务重启或原文件被移动后，同一会话仍可继续使用冻结副本。

完成条件是四种格式、混合嵌套文件夹、跨重启、引用映射和失败隔离全部通过固定检查，并完成一个受限真实 DeepSeek 场景。达到停止条件后不继续增加 OCR、同步或资料管理功能。

## 2. 用户交互

### 2.1 入口和预检

左栏“会话”区域增加主按钮“导入资料”。原“新建会话”继续用于启动工作区中的手动文件路径和目录会话，作为次级入口保留。

导入面板提供：

- “选择文件”：允许多选 `.md`、`.txt`、`.pdf`、`.docx`。
- “选择文件夹”：使用 `<input type="file" webkitdirectory multiple>` 取得文件及 `webkitRelativePath`，保留用户选择目录下的逻辑层级。浏览器只暴露用户明确选择的文件内容，不提供任意磁盘读取。[MDN 的目录选择说明](https://developer.mozilla.org/en-US/docs/Web/API/HTMLInputElement/webkitdirectory)。
- 会话名称：默认取单文件名或文件夹名，用户可修改。
- 助手：只列出声明 `search_documents` 且采用目录策略或组合策略的已启用助手；默认使用本批升级后的“目录问答”。所有导入会话都采用目录范围，即使只导入一份资料，以保持长文切块行为一致。若用户没有兼容的自定义助手，内置“目录问答”仍作为确定性可用的保底项。

选择完成后先显示预检摘要：支持文件数量、被忽略的其他格式、原始总大小和相对路径列表。页面侧校验只用于即时反馈，服务端必须重新校验。用户点击“开始导入”后才上传和保存副本。

### 2.2 进度、成功与失败

进度按“正在复制、正在解析、正在建立索引、正在创建会话”展示，同时显示已处理文件数。上传或解析期间提供“取消导入”；取消后服务端删除本次临时目录。

成功后页面自动选择新会话，将焦点移到任务输入框，并显示“本地副本已保存 · N 份资料”。右栏资料区展示：

- 原始逻辑路径与格式；
- 原文件字节数和 SHA-256；
- PDF 页数或 Word 段落／表格数量；
- 提取警告、导入时间和解析器版本；
- 规范化块数量及当前会话的受管范围。

文件夹中的不支持格式在预检时列出并忽略。任一已经接纳的支持文件若上传不完整、损坏、加密、超限或无法提取，则整批失败，不创建 Session。错误必须指出具体文件和原因，不把部分成功显示成可用会话。

## 3. 模块与 seam

新增 `local_agent/imports.py`，把文件校验、受管存储、解析、索引、发布和恢复集中在一个深模块。外部主要 interface 为：

```python
ImportStore.begin(request) -> ImportBatch
ImportStore.add_file(import_id, slot_id, stream, content_length) -> FileReceipt
ImportStore.start_finalize(import_id) -> ImportJob
ImportStore.snapshot(import_id) -> ImportSnapshot
ImportStore.cancel(import_id) -> ImportSnapshot
ImportStore.open(import_id) -> ImportedWorkspace
```

`Handler` 只负责认证、有限请求解析和响应；`WebRuns` 只负责编排导入任务与 Session 创建；解析器、路径规则和原子文件操作不散落在 HTTP 或页面代码中。

所有 Session 执行入口共享一个受管目录解析 seam：

```python
ManagedWorkspaceResolver.resolve(session) -> ResolvedWorkspace
```

`ResolvedWorkspace` 明确给出 `read_root/read_identity`、`write_root/write_identity` 和可选的 `source_mapper`。普通 Session 返回原启动 workspace，且读写根相同；导入 Session 只依据 `session.import_id` 从已经固定的 `ImportStore.root` 目录描述符推导 `workspace/` 与 `artifacts/`，数据库中的绝对 `workspace_path` 只作旧记录展示，不能作为授权来源。Session 的创建、加载、列表、执行、CLI、Web、恢复和 Artifact 下载都必须经过同一个 resolver。

resolver 逐级使用目录描述符、随机 import ID、`O_NOFOLLOW`（平台支持时）及设备／inode 身份检查，拒绝符号链接和目录替换；不能只依赖 `Path.resolve()`。导入资料失效时，历史消息仍可查看，但任何新 Run 必须在模型调用前停止。

文档解析 seam 为：

```python
DocumentParser.parse(source, limits) -> ParsedDocument
```

提供四个 adapter：`Utf8TextParser`、`MarkdownParser`、`PdfTextParser`、`DocxParser`。解析结果包含规范化文本块、每行到原始位置的映射、文档统计和非阻断警告。后续 OCR 若进入新阶段，只能增加新的 adapter，不能改 Runtime 或放宽现有文件权限。

为长文定位增加只读 `search_documents` 工具，仅在受管导入会话中注册。interface 为：

```json
{"query":"预算"}
```

它按 Unicode casefold 后的确定性子串匹配返回候选：规范化块路径、行号范围、原始文件位置和短摘录。查询最多 256 个 UTF-8 字节，最多返回 20 条、每条摘录最多 384 个 UTF-8 字节、整个结果最多 12 KiB，执行上限 1 秒；结果按逻辑路径、块序号和行号稳定排序。搜索结果用于选择待读块，不能直接作为最终事实引用；模型仍须调用 `read_file`，最终引用由本轮实际快照生成。现有普通工作区和旧 Session 不自动获得该工具。

## 4. 受管资料空间与持久化

每批导入存于当前 `state_dir` 下；实际目录名使用不可预测的 128-bit ID，不使用用户文件名：

```text
state_dir/imports/<import-id>/
├── originals/
│   ├── <source-id>.bin
│   └── ...
├── workspace/
│   ├── index.md
│   └── documents/<source-id>/chunk-0001.md
├── artifacts/
├── manifest.json
└── locations.json
```

- `originals/` 保存用户选择的原始字节副本，不属于模型可读 workspace。
- `workspace/` 只保存确定性提取后的 UTF-8 文本、索引和文本块，是本 Session 的读取根。
- `artifacts/` 是导入会话的受控输出根，不进入 `list_files` 或事实引用范围。
- `manifest.json` 保存逻辑路径、原始格式、解析器版本、块清单、警告、目录身份，以及每个 original、规范化块、`index.md` 和 `locations.json` 的 SHA-256。
- `locations.json` 将规范化文件行号映射到原始文本行、PDF 页码或 Word 段落／表格位置，不交给模型修改。

目录权限为 `0700`，文件权限为 `0600`。导入先写入同一父目录下的临时批次；全部文件、清单和位置表写入并 `fsync` 后，以原子 rename 发布，再 `fsync(imports/)`。每次 `open` 校验 manifest 与位置表；搜索和读取还要校验涉及的规范化块摘要。摘要或目录身份不符时将 import 标记为不可用，不把内容交给模型。

Session Store 增加 `imports`、`import_files` 记录及 `sessions.import_id` 可空关联；`sessions.import_id` 为唯一外键，保证一批导入最多创建一个 Session。旧 Session 的 `import_id` 为 `NULL`，行为不变。SQLite 是 Session 关联和 import 状态的唯一权威来源。导入 Session 的授权必须同时满足：

1. `import_id` 存在且状态为 `ready`；
2. 路径位于本进程 `ImportStore.root` 的直接受管后代；
3. manifest 摘要、workspace 设备／inode 与数据库一致；
4. Session 保存的 `import_id` 与 workspace 一致。

Web 层列出 Session 时可合并启动工作区与当前 State Store 的有效导入 Session，但不能因为数据库中存在任意绝对路径就将它加入授权集合。CLI 和 `SessionService.execute()` 使用相同 resolver，不能绕过这一限制。原有基于启动工作区的临时 Run 保持不变。

导入提交采用以下可恢复状态机：

```text
SQLite:    uploading -> finalizing ----------------------> ready
                          |                                |
                          +-> cancelled / failed           +-> unavailable（完整性失效）
filesystem: staging -> parsed -> published_unlinked -> linked
```

1. `begin` 在 SQLite 写入 `uploading`、随机 slot 和请求指纹；`complete` 以同一指纹幂等地将它改为 `finalizing`。
2. worker 在 staging 完成解析、摘要与 `fsync`，将 immutable manifest 标为 `parsed`；随后 rename 为正式 import 目录并 `fsync(imports/)`，成为 `published_unlinked`。
3. 一个 SQLite 事务写入 import/file 记录、创建 Session、关联唯一 `import_id` 并置 `ready`。提交后文件状态视为 `linked`，无需再改写 immutable manifest。
4. 重复 `complete` 在 `finalizing` 时返回原 job，在 `ready` 时返回同一 Session ID；请求指纹不同则返回冲突。

启动恢复按 SQLite 状态与文件事实联合判断：只有 staging 且尚未发布时安全清理或重试；`finalizing` 且已有完整正式目录时继续执行同一个关联事务；有正式目录但没有任何数据库记录的孤儿批次才清理；数据库已是 `ready` 而目录缺失或摘要不符时标记 `unavailable`，不删除历史或显示成功。取消与 finalize 共用批次锁；一旦 `ready`，上传取消接口不再改变资料。

现有 `sessions backup` 必须在同一批实现中识别导入资料：SQLite 目标旁生成 `<backup-name>.imports/` 受管副本和总 manifest。新增进程内 `StateMaintenanceGate`：Run submit、import begin/finalize/cancel、审批执行与 Artifact 发布共用活动登记；backup 非阻塞地取得独占维护权，取得后若仍有活跃项则返回 `STATE_BUSY`，否则一直持有到数据库、sidecar 和总 manifest 全部落盘。State Store 的进程所有者锁仍负责排除其他进程，不能替代这个进程内 gate。

备份先取得 SQLite 快照，再只按该快照中的 `ready` import ID 复制 originals、workspace、locations、manifest 和 artifacts，最后生成覆盖数据库及每个文件摘要的总 manifest。命令结果列出数据库与 sidecar 两个路径；缺少任一部分不得声称备份完整。

第一版同时提供独立显式恢复 seam：`StateBackup.restore_bundle(database_backup, import_sidecar, destination)`。`destination` 必须不存在，且不能有已打开的 State Store。恢复先校验总 manifest，在 destination 的同一父目录构造完整临时 State Store，安装 imports、重建本机目录身份及派生路径、更新离线数据库副本，并打开临时库执行 resolver 全量校验；全部通过后一次 rename 为 destination 并 `fsync` 父目录。失败时保留原备份、清理临时目录且不暴露目标目录。自动合并到已有 State Store、增量恢复与覆盖恢复后置。

## 5. 本地上传协议

继续使用 loopback HTTP、同源检查、随机 `X-Session-Token` 和 `X-Agent-ID`。不使用浏览器提供的绝对路径，也不把本地路径传给模型。

协议分为四步：

1. `POST /api/imports`：JSON 提交批次类型、会话名称、助手 ID、支持文件的逻辑路径／大小及忽略列表；返回 `import_id` 和每个文件的随机 `slot_id`。该路由单独允许至多 128 KiB 元数据；其他既有 JSON 路由继续保持 16 KiB 上限。
2. `POST /api/imports/{id}/files/{slot}`：`application/octet-stream`，一次发送一个文件；必须提供与 slot 声明值相同且不超限的 `Content-Length`，不接受 chunked body。服务端只按 slot 读取精确字节数，流式写入并计算 SHA-256。
3. `POST /api/imports/{id}/complete`：锁定清单并启动后台解析，立即返回 `202`。
4. `GET /api/imports/{id}` 轮询进度；完成时返回新 Session ID。`POST /api/imports/{id}/cancel` 终止上传或解析并清理 staging。

每个 slot 只接收一次。重复 complete 按 §4 状态机返回同一 job 或同一 Session；清单或正文不同的重试返回冲突，避免一次用户动作产生多个会话。导入任务和模型 Run 分开计数；同一服务最多一个活跃导入解析任务，避免多个 PDF 同时占满本机资源。

## 6. 格式处理与规范化

### 6.1 Markdown 与文本

- 只接受严格 UTF-8；拒绝 NUL 和不允许的控制字符。
- 四种扩展名匹配不区分大小写，但规范化文件名由系统生成，原逻辑路径保持用户看到的大小写。
- `originals/` 保存原始字节；规范化文本不做 Unicode 标点或兼容字符替换。
- 仅将换行表示统一为 `LF` 供行号引用，manifest 记录原始换行和摘要。

### 6.2 带文字层的 PDF

- 使用固定版本 `pypdf` 按页提取文字；PDF 中的图片、附件、JavaScript 和表单不执行。
- 每页保留页码映射。全文件没有可用文字时返回 `PDF_TEXT_NOT_FOUND`；部分页面无文字时导入成功，但 manifest 和页面显示具体空白页警告。
- 加密或需要密码的 PDF 第一版返回 `PDF_ENCRYPTED`。
- PDF 没有可靠的语义段落结构，提取顺序可能与复杂多栏排版不同；页面明确称为“提取文本”，不宣称等同视觉版式。`pypdf` 官方也说明图片型 PDF 不能由文本提取器读取，并提示异常内容流可能消耗大量内存：[文本提取说明](https://pypdf.readthedocs.io/en/5.7.0/user/extract-text.html)。

### 6.3 Word `.docx`

- 使用固定版本 `python-docx`，按文档顺序提取正文段落和表格；表格转为带单元格分隔的确定性文本。
- 位置显示为“段落 N”或“表格 N，第 R 行”。DOCX 本身不提供稳定的最终页码，因此不伪造页码。
- 图片文字、文本框、批注、脚注及页眉页脚第一版不提取，并在导入结果中列为格式边界。
- 不接受 `.doc`、`.docm` 或加密 Office 文件。`python-docx` 提供 Word 2007+ `.docx` 的段落和表格访问；复杂合并表格会被近似为单元格矩阵：[项目说明](https://pypi.org/project/python-docx/)、[表格说明](https://python-docx.readthedocs.io/en/latest/user/tables.html)。

实现将 `pypdf==6.16.1` 与 `python-docx==1.2.0` 作为文档导入依赖加入扩展环境，并在项目当前 Python 3.14 环境安装验证。没有安装解析依赖时，`.md/.txt` 仍可导入；PDF/Word 在选择前显示“解析器未安装”，不得上传后才静默失败。

## 7. 切块、索引和引用

单块上限为 12 KiB UTF-8，给现有 32 KiB `read_file` 信封和 64 KiB 总输入预算留出搜索结果、系统规则及至少两个相关块的余量。切分优先顺序为：PDF 页／Word 块 → 段落 → 行 → UTF-8 字符边界。任何切分都必须保留逐行位置映射；一行跨来源单元时映射拆成多个连续区段。

`index.md` 上限 16 KiB，只包含导入清单、原始逻辑路径、格式、每份来源的块号范围和位置范围；不复制每块开头或完整长文。它和 `search_documents` 结果都带 `evidence_role=catalog`，只用于定位，不是事实证据。`search_documents` 对规范化块进行 §3 所述的确定性本地搜索，不调用模型、不访问网络。

每次 Run 仍只允许实际读取助手预算内的文件。`ImportedDocumentPolicy` 将搜索调用单独记账；搜索结果进入下一轮 ToolCall context，但不进入事实 `snapshots`、`read_files` 或目录 coverage，也不能满足 `answered` 的引用要求。读取 `index.md` 只产生 catalog snapshot，答案校验器拒绝将它作为事实引用。

`ImportedDocumentPolicy.fact_coverage()` 是导入会话完整性的唯一输入：`index.md` 和其他 `evidence_role=catalog` 文件不进入 `discovered_files` 或 `unread_files`，catalog 读取单独记账；只有 manifest 中通过摘要校验的正文 chunk 才进入事实发现、已读和 snapshot。`validate_answer()` 使用该 `fact_coverage` 判断完整性。全部正文 chunk 已读时允许 `not_found`；仍有正文 chunk 未读时沿用预算规则拒绝把无匹配宣称为完整结论，返回 `unable` 并说明范围限制。

最终引用继续包含稳定的规范化 `path/start_line/end_line/quote`，程序另附只读展示字段：

```json
{
  "source": {
    "kind": "imported_document",
    "name": "合同.pdf",
    "logical_path": "项目资料/合同.pdf",
    "locations": [
      {"kind": "pdf_page", "page": 3},
      {"kind": "pdf_page", "page": 4}
    ]
  }
}
```

模型仍只能返回严格的 `path/start_line/end_line[/quote]`；若模型自行加入 `source`，沿用现有严格字段校验并拒绝。`quote` 必须逐字等于本轮实际 chunk 读取快照对应行。`validate_answer()` 先完成现有证据校验，随后 `ImportedSourceMapper.enrich(validated_answer, snapshots)` 才依据只读 `locations.json` 生成展示用 `source.locations`，并在最终结果持久化前附加。

`locations` 按引用文本顺序列出覆盖到的原始区段，合并相邻同类位置；文本使用原始行范围，PDF 使用页码范围，Word 使用段落或“表格 N，第 R 行”的范围。引用跨多个页或多个 Word 单元时保留多个区段；任何行无法完整映射、映射摘要失效或引用来自 catalog 时直接返回 `INVALID_CITATION`。`source` 只改善用户定位，不能绕过引用校验。PDF/Word 的“逐字”指解析器冻结后的提取文本；UI 必须保留这一说明，不能称为原始版面逐字还原。

## 8. Agent、会话与写入行为

导入 Session 固定：import ID、受管 workspace 身份、助手 ID 和助手修订。导入完成后更换当前助手不会改变旧会话。所有导入会话使用目录或组合策略；不允许文件策略意外接收多块范围。

本批将 `search_documents` 加入 `AgentDefinition.TOOLS` 白名单，并发布声明该工具的新修订内置“目录问答”与“项目简报”；“单文件问答”不增加该工具。旧 Session 继续使用冻结的旧助手快照，不被静默升级，也不能转为导入 Session。导入面板只允许选择声明该工具的兼容助手，因此新导入会话一定具备搜索能力。

`search_documents` 只在 `import_id` 有效且助手声明允许该只读工具时注册。新增独立 `SearchDocumentsAdapter` 和 `ImportedDocumentPolicy`，不把搜索结果交给现有 `DirectoryTools.record()`。工具只能搜索 resolver 给出的当前 Session 规范化 workspace，不能接受 import ID、绝对路径或其他 Session ID 作为模型参数。

文件工具组装接口显式拆分读写根：

```python
build_file_engine(agent, read_root, target_path, output_path=None, write_root=None)
adapt_tools(tool, policy, approvals, output_path=None, write_root=None, write_identity=None)
```

`write_root` 省略时与 `read_root` 相同，保持普通 Session 行为；导入 Session 必须由 resolver 显式传入 sibling `artifacts/` 及其身份。现有 `write_file` 规则保持：用户提交 Run 前声明一个目标；模型不能改名；每个 Run 最多成功新建一次；完整预览经过用户批准后才执行；不覆盖。Artifact 发布、恢复和回执核验都通过 resolver 与目录描述符完成，不能靠字符串拼路径或跟随符号链接。

页面通过 `GET /api/sessions/{session_id}/runs/{run_id}/artifacts/{artifact_id}/download` 下载已批准并持久化的产物。路由校验同源 token、Agent、Session、Run、Artifact 回执、写入根身份及 SHA-256，以 `attachment` 流式返回；它不接受任意路径。模型不能调用下载接口读取文件，也不能把 Artifact 当成本轮事实引用。

## 9. 限制、资源与安全

第一版固定限制：

| 项目 | 上限 |
|---|---:|
| 用户选择的全部目录项 | 500 |
| 支持文件数 | 50 |
| 单个原文件 | 20 MiB |
| 单批原文件总量 | 100 MiB |
| 单文件提取文本 | 512 KiB |
| 单批提取文本 | 3 MiB |
| 单批规范化块 | 256 |
| 单层目录项 | 200（沿用现有值） |
| 单规范化块 | 12 KiB |
| `index.md` | 16 KiB |
| 单次搜索结果 | 12 KiB |
| 单个 PDF 页数 | 500 |
| 单个 PDF 解码内容流累计 | 64 MiB |
| 单个 DOCX ZIP 条目 | 2,000 |
| 单个 DOCX 声明解压总量 | 100 MiB |
| 单个 DOCX 条目压缩比 | 100:1 |
| 解析子进程 CPU 时间 | 15 秒 |
| 解析子进程地址空间 | 512 MiB |
| 解析子进程 IPC 输出 | 8 MiB |
| 单文件解析墙钟时间 | 20 秒 |

服务端拒绝绝对路径、`..`、空路径段、反斜杠混淆、控制字符、重复逻辑路径和大小不符。目录选择超过 500 个总项目时在读取正文前拒绝，防止大量不支持文件占用页面与元数据预算。原文件用随机 source ID 保存，不按用户路径在磁盘重建，因此目录穿越和同名覆盖不会到达文件系统。

PDF/Word 在独立子进程中解析。子进程代码不发起网络请求、不解析外部关系、不执行附件／宏／JavaScript，也不启动其他进程；第一版不把这描述成 OS 级断网沙箱。父进程在启动解析前检查 DOCX ZIP 条目数、声明解压总量和单条压缩比，并在 PDF 提取期间累计页数与解码内容流；子进程使用系统可用的 `RLIMIT_CPU`/`RLIMIT_AS`，父进程始终执行墙钟和 IPC 输出上限。任何限制无法在当前平台安装时，对应 PDF/DOCX parser 标记为不可用，而不是降级为无上限运行。

超过任一阈值时父进程终止整个解析子进程组、限时等待并回收，删除该批解析临时输出，统一返回 `DOCUMENT_LIMIT_EXCEEDED`；异常退出返回 `DOCUMENT_CORRUPT`，解析器缺失或资源限制无法安装返回 `DOCUMENT_PARSER_UNAVAILABLE`。子进程崩溃或超限只使本批失败，不影响 HTTP 服务和已有 Session。

导入资料会长期保存在本机且可能包含敏感内容。页面在第一次导入前明确说明：原文件副本、提取文本和会话记录位于本机 State Store；当前版本没有应用层加密或自动过期。Provider 只收到 Agent 实际读取的提取文本，不收到未读取原文件、原始二进制或本机绝对路径。

## 10. 错误契约

用户可见错误使用稳定 code，不返回解析器堆栈或本机绝对路径：

| code | 页面说明 |
|---|---|
| `IMPORT_FORMAT_UNSUPPORTED` | 该格式不在本版支持范围 |
| `IMPORT_TOO_MANY_FILES` | 支持文件数量超过 50 |
| `IMPORT_FILE_TOO_LARGE` | 单个文件超过 20 MiB |
| `IMPORT_BATCH_TOO_LARGE` | 本批原文件超过 100 MiB |
| `IMPORT_PATH_INVALID` | 文件夹中的相对路径不安全或重复 |
| `IMPORT_UPLOAD_INCOMPLETE` | 文件上传不完整，请重新导入 |
| `TEXT_ENCODING_INVALID` | Markdown 或文本不是有效 UTF-8 |
| `PDF_TEXT_NOT_FOUND` | PDF 没有可提取文字；本版不做 OCR |
| `PDF_ENCRYPTED` | PDF 需要密码，本版不支持 |
| `DOCUMENT_CORRUPT` | PDF 或 Word 文件损坏或结构无效 |
| `DOCUMENT_LIMIT_EXCEEDED` | 解压、页数、提取文本或资源超过限制 |
| `DOCUMENT_PARSER_UNAVAILABLE` | 本机未安装对应解析器 |
| `IMPORT_CANCELLED` | 用户已取消，本批未创建会话 |
| `IMPORT_STORE_ERROR` | 本地副本未能安全保存 |
| `IMPORT_INTEGRITY_ERROR` | 本地副本或来源映射完整性校验失败 |
| `IMPORT_UNAVAILABLE` | 该资料会话的受管目录当前不可用 |
| `STATE_BUSY` | 当前有导入、运行或审批，暂不能生成一致备份 |

错误发生在最终发布前时，Session 不存在，模型调用次数必须为 0。文件已经发布但 Session 关联事务失败时保留 `finalizing` 记录，由同一 complete 重试或启动恢复继续；不能直接删除可能已被事务关联的资料。进程在任一崩溃窗口退出时严格按 §4 状态机恢复，只有 SQLite `ready` 且 resolver 完整性校验通过才向页面显示成功。

## 11. 验收与停止条件

### 11.1 模块检查

- 四个 parser adapter 使用真实小文件，验证文本、表格、页码／段落映射和确定性 SHA-256。
- 切块不超过 12 KiB，索引不超过 16 KiB，不拆坏 UTF-8，行号到全部来源位置可逆；搜索加两个最大块仍不使构造后的模型请求超过现有 64 KiB 输入预算。
- 导入发布前失败、取消、重复 slot、重复 complete，以及 `uploading/finalizing/published/ready` 各崩溃窗口恢复都不会产生重复 Session、误删已关联资料或留下残缺授权。
- 含导入 Session 的手动备份同时生成数据库和 import sidecar；备份与新 Run、导入 finalize、审批和 Artifact 发布的竞争由 `StateMaintenanceGate` 确定性阻断。恢复到不存在的目标目录后重建目录身份，跨重启可执行同一会话；缺失或篡改任一备份部分，以及恢复各崩溃窗口都不暴露半恢复 State Store。
- 扫描 PDF、加密 PDF、损坏 PDF/DOCX、非法 UTF-8、路径穿越、同名，以及 PDF 页数／内容流、DOCX ZIP 条目／解压量／压缩比、CPU、地址空间、IPC 和墙钟边界夹具返回正确 code，模型调用为 0。
- `search_documents` 只能搜索当前 import，固定排序且不超过结果预算；结果按原调用 ID 回填，但不进入 read snapshot 或 coverage。旧助手快照不获得该工具，不兼容助手不会出现在导入面板。
- 读取并引用 `index.md` 被拒绝，搜索结果不能直接成为引用；只有实际读取且完整性通过的 chunk 可引用。
- `fact_coverage` 排除 catalog：全部正文块已读时允许 `not_found`，仍有任一正文块未读时拒绝完整范围结论。
- manifest、chunk 或 `locations.json` 被改动时拒绝继续；DOCX“段落 → 表格 → 段落”和跨 PDF 页引用均生成有序 `source.locations`，无法完整映射时拒绝答案。
- 导入 Session 在 Web、CLI 和直接 `SessionService.execute()` 中都只能访问自己的 workspace；伪造数据库路径、其他 import ID、旧 inode、symlink 和跨 Agent Session 均在模型调用前拒绝。
- 导入 Session 的批准写入只进入 `artifacts/`，仍遵守预声明、一次成功、不覆盖和真实回执；拒绝后没有文件。下载只接受持久化 Artifact ID，路径伪造、摘要不符或跨 Session 请求均拒绝。

### 11.2 页面检查

- 文件和文件夹选择、预检、忽略列表、逐文件进度、取消、失败和自动进入会话均可通过键盘完成。
- 助手列表只显示兼容项，并始终提供升级后的内置“目录问答”；选择不兼容或已撤销助手时服务端也会拒绝。
- 导入面板关闭后焦点回到触发按钮；成功后焦点进入 Composer；运行时不让 Tab 进入隐藏面板。
- 320、375、768、1024、1440px 下导入入口、进度、错误和 Composer 均可见，无页面级横向滚动。
- 文件名和解析内容继续使用 `textContent`，恶意 HTML 文件名、正文或解析错误不能执行脚本。
- 页面刷新不重复上传、complete、Session 创建或审批。

### 11.3 持久与真实闭环

固定混合资料夹包含一份 `.md`、一份 `.txt`、一份两页文字 PDF、一份含段落和简单表格的 DOCX，以及一份被忽略的图片。离线 Provider 必须证明：

1. 导入后创建一个固定 import Session；
2. 初始模型请求没有偷放全部正文；
3. 模型先搜索／发现，再读取 PDF 和 Word 的相关块；
4. 工具结果按原调用 ID 进入下一轮；
5. 回答分别引用 PDF 页码与 Word 段落／表格位置；
6. 移走原始测试文件并重启服务后，同一 Session 仍能追问；
7. 另一个 import Session 不能读取本批资料；
8. 从成对备份显式恢复到新的空 State Store 后，资料、会话和引用仍可使用。

离线固定组与完整回归通过后，仅运行一个受限 DeepSeek 会话：首问跨 PDF/Word 找两项事实，次问引用上一轮结论并核对文本资料。最多 2 个 Run、12 次模型请求；任一核心链路失败即停止并保留证据，不自动扩展样例。AI 内容复核与用户试玩分别记录，不把机器通过当成用户验收。

## 12. 实现范围与后置项

预计修改：

- 新增 `local_agent/imports.py`、统一 `ManagedWorkspaceResolver`、解析子进程入口和导入测试夹具；
- 扩展 `session_store.py`、`sessions.py`、`web_runs.py`、`web.py`，实现状态机、成对备份和显式空目录恢复；
- 扩展 Agent 工具白名单与内置目录／组合助手修订；为受管 workspace 增加 `search_documents`、独立策略、来源映射和独立 Artifact 根；
- 修改 `static/index.html`、`static/app.js`、`static/app.css`；
- 更新扩展依赖、README、阶段状态与浏览器测试。

本批明确后置：OCR、`.doc`、`.docm`、Excel、PPT、图片理解、音视频、拖拽、原文件自动同步、版本替换、资料删除、云端上传、远程 URL 导入、后台批量索引、向量数据库、语义检索、合并恢复、增量恢复和覆盖恢复。以后新增格式或 OCR 必须沿用 `DocumentParser` seam 与同一授权模型，不把文件解析分支写进 Agent Loop。
