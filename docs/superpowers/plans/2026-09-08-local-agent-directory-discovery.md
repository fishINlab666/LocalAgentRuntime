# 受限目录发现与多文件上下文实施计划

## §0 当前进度

2026-09-09：**目录发现实现和第三轮真实固定组验证已核实；最小闭环阶段收口。** 后续专名连接符修复保留，新版本真实复测已暂停／后置，原重启请求取消。当前以 [阶段验收记录](../../../local-agent/trial/directory-check.md)和根目录 [项目规则](../../../AGENTS.md)为准。下方实施日志描述当时的过程，不构成继续重跑、复审或扩展的任务。

## 原定范围与实现

**Goal:** 用户只给问题，模型在已指定工作区自主 list_files → read_file → 结合本次读取内容回答，验证目录结果和每次读取都准确回填下一轮 context。

**Architecture:** 沿用同一 Runtime、Provider、Trace 和页面。单文件模式保持兼容，目录模式按本轮发现结果授予读取权限，并记录已列目录、已发现文件与已读文件；引用只从本次成功读取快照提取。元数据检查、停止预算和权限由程序执行，选文件与工具调用由模型决定。

**Tech Stack:** Python 3 标准库、现有 DeepSeek Tool Calling 接口、现有 HTML/JS 页面、unittest 与现有 Playwright 浏览器测试。当前目录不是 Git 仓库，直接延续现有工作目录，不创建假提交或新项目。

## 已确认的范围

2026-09-08 用户明确要求减少交互修改，优先输入和回答准确性、工具结果回填下一轮 context，并推进下一步。因此不再以额外的交互优化或主观试用反馈阻挡本批实现。

- `list_files({"path":"."})` 开始。只列一层；返回相对路径、file/directory 类型和文件字节数，无正文。子目录需要模型再次显式调用。
- 只支持可见 `.md` / `.txt` 普通文件及普通子目录。排除符号链接、硬链接、隐藏项、特殊文件与不安全名称。目录扫描最多 200 项，超限明确报错，不把部分列表冒充完整。
- 只能读本轮发现的文件、列本轮发现的子目录，默认最多成功读取 4 个不同文件；单文件原有 32 KiB、6 次模型请求、64 KiB 请求等限制保留。
- 状态只在主循环收到未超时且未取消的工具结果后更新，迟到 worker 不能授予下一轮权限。
- 目录 answered 至少引用一个本次成功读取文件。not_found 仅在本次发现范围全部列出和读完后成立（空的受支持范围也可成立）；未读完返回 INCOMPLETE_SEARCH。unable 要有实际工具错误且范围未完成。
- 这批交付目录发现和跨文件证据问答；完整 M1 的流式展示单独跟踪，不把它混入本次准确性验证。

## 共享接口

```python
# local_agent/discovery.py；每次 run 新建一个对象。
class DirectoryTools:
    workspace: Path
    schemas: list[dict]  # LIST_FILES_SCHEMA + READ_FILE_SCHEMA
    def execute(self, name: str, arguments: dict) -> dict: ...
    def record(self, name: str, arguments: dict | None, result: dict) -> None: ...
    def coverage(self) -> dict: ...
```

coverage 字段固定为 `attempted`, `had_error`, `listed_directories`, `unlisted_directories`, `discovered_files`, `read_files`, `unread_files`, `complete`；路径列表排序，初始 unlisted_directories 为 `["."]`。`execute` 不改变账本，`record` 由 Runtime 在 bounded_call 成功返回后调用；工具错误也记录，但不授予路径权限。

```json
{"ok":true,"path":".","entries":[{"path":"plan.md","type":"file","bytes":100},{"path":"notes","type":"directory"}],"complete":true}
```

Web 新输入为 `{"mode":"directory","question":"问题"}`，旧 `{"file":"note.md","question":"问题"}` 保持兼容。Runtime 接收 DirectoryTools 时使用 `run(question, None, cancel)`；最终结果增加 scope，不改变 answer/citations 格式。CLI 的 `run --discover` 与 `--file` 二选一。页面仅增加目录模式开关、必要说明与现有进度映射。

## Task 1：文件系统与本轮发现账本

Files: `local-agent/local_agent/files.py`, new `local-agent/local_agent/discovery.py`, new `local-agent/tests/test_discovery.py`。

- [x] 写测试并先看到失败：根目录和嵌套目录列表、不能预读正文、只读已发现文件、未知字段/越界/链接/硬链接/隐藏/特殊文件拒绝、200 项扫描上限、4 文件上限、根路径被替换、迟到执行不更改账本、覆盖范围完整性。
- [x] 用现有 O_NOFOLLOW/openat 逐层遍历实现 ListFiles；提取共享目录打开函数供 ReadFile 复用。DirectoryTools 按上述固定接口实现 execute/record/coverage。
- [x] 运行 `python3 -m unittest discover -s tests -p 'test_discovery.py' -v` 及 `test_files.py`，原安全测试必须保持通过。

核心验证示例：

```python
tools = DirectoryTools(workspace)
assert tools.execute('read_file', {'path': 'plan.md'})['error']['code'] == 'PATH_NOT_DISCOVERED'
result = tools.execute('list_files', {'path': '.'})
assert tools.coverage()['discovered_files'] == []
tools.record('list_files', {'path': '.'}, result)
assert tools.execute('read_file', {'path': 'plan.md'})['ok'] is True
```

## Task 2：同一 Loop 与多文件引用

Files: `local_agent/runtime.py`, `local_agent/answers.py`, new `local_agent/prompts.py`, `tests/test_runtime.py`, `tests/test_answers.py`。

- [x] 写脚本 Provider 测试：初始消息没有文件名和正文；模型选择 list_files，后续请求带相同 ToolCall ID 的目录结果；读取两个文件后下一轮同时含两份完整内容；多文件引用取自各自快照。
- [x] 为 model_request 增加所选工具 schema；Runtime 用同一主循环调度两个工具，接收结果后 record，再把 result 与 scope 回填。
- [x] 验证 answered/完整 not_found/错误 unable；目录只列未读不能 answered，不完整检索不能 not_found，未读文件不能被引用，未知工具/取消/超时仍受原预算限制。
- [x] 运行 `python3 -m unittest discover -s tests -p 'test_runtime.py' -v` 和 `test_answers.py`，随后完整 unittest。

预期回填角色顺序允许同批读取：`system, user, assistant(list), tool(list), assistant(read A/read B), tool(A), tool(B)`。每个 tool_call_id 必须与对应 assistant ToolCall 一致；程序不能预先把答案塞入初始 context。

## Task 3：最小入口接入

Files: `web_runs.py`, `__main__.py`, `demo.py`, `static/index.html`, `static/app.js`, `static/app.css`（仅开关样式如需）；`tests/test_web.py`, `tests/test_cli.py`, `tests/browser_web.cjs`；new `examples/discovery-workspace/plan.md`, `examples/discovery-workspace/review.md`。

- [x] 先写 Web/CLI 测试：旧单文件 body 不变；新 mode 仅接受 directory；不能混入任意 workspace/file 字段；缺 Key 不降级；工作区路径替换检查继续覆盖。
- [x] WebRuns 每次目录任务创建 DirectoryTools；job.file 为 null，job.mode 为 directory；页面提交目录 body，刷新恢复模式、显示 scope 与 list_files 进度。只加一个模式开关，不重做布局。
- [x] DemoProvider 明确支持目录模拟：列根目录、读演示的两份文件、回显合并内容并返回引用位置。CLI demo --discover 使用专用两个小文件，run --discover 使用用户提供工作区。
- [x] 运行 `test_web.py`, `test_cli.py` 和现有浏览器脚本；保留跨任务迟到响应、取消、HTML 文本呈现、缺密钥和小屏回归。

## Task 4：固定跨文件验证与交付

Files: new `local_agent/directory_evaluation.py`, new `tests/test_directory_evaluation.py`, `README.md`, current plan/spec/trial record。

- [x] 创建两份各小于 2 KiB 的合成材料，随机文件名/独有值不进入初始提示。题目必须同时需要两份材料的事实；正常、换内容、缺失信息、工具读取错误四类各 3 次。
- [x] 报告检查目录→读取→下一轮回填的关联、两份文件引用及事实；保存失败，任一失败整组不通过。模拟只记录 SIMULATED_ONLY，真实自动通过仍待语义复核。
- [x] 用同一真实服务或原 Key 终端执行真实组，逐项复核。第三轮固定组自动及内容复核均为 12/12，证据另存；补充网页失败单独跟踪，不宣称所有页面答案准确。
- [x] 实现补充网页样例的标识连接符保真检查；200 项回归、旧真实结果离线重放与独立复审通过，原失败证据保留。细节见 `2026-09-09-identifier-fidelity.md`。
- [ ] **已后置，非当前待办：**新版本真实页面标识保真验证尚未执行；原重启请求已取消，不以离线/模拟检查代替真实新运行。
- [x] 更新当前完成范围与证据，明确本批未完成流式展示等完整 M1 余项。

## 验证命令

基线和最终完整回归：`python3 -m unittest discover -s tests -v`。

浏览器：沿用 `tests/browser_web.cjs`，通过 `with_server.py` 启动 8767/8768 合成服务，使用已安装的 Chrome 与工作区 Node/Playwright。工具、安全与消息回填为主要验收点，视觉只检查新开关不破坏原操作。

## 历史实施记录（不作为当前待办）

- [x] 112 项原始基线通过。
- [x] 文件工具完成并复审。
- [x] Runtime/引用完成并复审。
- [x] 入口与浏览器回归完成。
- [x] 真实跨文件固定组完成并逐项复核（第三轮 12/12）；补充网页专名准确性仍未通过。

- Task 1＋2 需求复审与代码质量复审 PASS；132 项定向检查通过。修复并补回归：根目录普通替换、失败重新列目录撤销完整性、无效参数重读的范围与快照一致撤销。
- 跨文件固定组测试替身 12 次预筛通过，gate=SIMULATED_ONLY；真实模型组未执行。

- 最终集成：182 项 Python 回归通过（ResourceWarning 视为错误），24 项入口测试另行独立通过；浏览器完成目录问答、双引用、范围、刷新、取消、迟到回复、缺密钥和 5 种视口。
- Task 3、Task 4 规格审阅与最终代码质量/集成审阅均 PASS。便捷启动脚本语法及 0/1/2 不启动、仅 3 启动的退出码分支已验证。
- 下一项真实验收仍未执行：原密钥终端停止旧服务后，运行 local-agent/trial/verify-directory-and-serve.sh；脚本先跑 12 次，预筛全过再启动 8765 两文件页面。报告随后另行语义复核。

## 2026-09-09 第一轮真实验收与修正

- 第一轮已实际执行且逐条复核，原报告 `local-agent/trial/results/directory-evaluation-78031bf496cf4659a3c2c34751c17e40/report.json` 保持 FAILED / 8/12。12/12 目录发现与上下文回填正确，41 个工具返回均完整传递，15 项落地引用准确。
- 一项预筛误判：“未记载任何预算”被旧紧邻词组识别器漏掉。修正仅允许可选“任何”，正反例先红后绿；不覆盖旧报告。
- 三项实际协议失败：一项 JSON 引号错误在一次修复后仍无效，两项 unable 附带部分有效引用。补充通用输出格式规则与多状态示例、确定性错误不重复读取，保持原校验和运行预算；失败原文重放仍被拒绝。
- 小修涉及 `evaluation.py`、`prompts.py`、`runtime.py` 的提示常量与 `tests/test_evaluation.py`；未改 Provider、工具或页面。182 项完整回归及最终 42 项 Runtime 回归通过。独立小修审阅 PASS，真实组仍 BLOCK。
- 下一步原终端再次运行相同便捷脚本，重新执行完整 12 次，并核对新报告。最新状态以 `local-agent/trial/directory-check.md` 及本组 `semantic-review.md` 为准。

## 2026-09-09 第二轮真实验收与修正

- 原报告 `local-agent/trial/results/directory-evaluation-9871fc0a057d49c986eb5c1805030504/report.json` 保持 FAILED / 10/12。等价日期及“未说明所问的预算”两项预筛误判已通过有限匹配修正，另存离线重放为自动 12/12，未追加模型调用或改写原报告。
- 独立内容复核 11/12，整组 BLOCK：第 7 条以“未发现的受支持文件”为主语断言其不含预算，超出本次已读证据；完整覆盖不能支持未知文件正文。12 处引用与 40 次工具结果回填准确，所有结构和来源验证通过。
- 已修改 `evaluation.py`、`directory_evaluation.py`、两个相应测试文件及 `prompts.py` 的目录范围提示。186 项完整回归通过；未更改 Runtime、Provider、工具或页面。新范围提示的真实效果仍待验证。
- Task 4 真实验收通过项继续未勾选。原密钥终端运行相同便捷脚本，取得新完整组后逐条语义复核；不能用离线评分改善替代语义失败的修复证据。

## 2026-09-09 第三轮真实验收与页面补充检查

- 固定组 `directory-evaluation-b1770c014ae64ccfb2972672e7331d87` 自动及 AI 内容复核 12/12 PASS；主任务离线重放、独立任务逐条审查确认 36 次工具回填、13 处引用、12 份初始上下文正确。原机器报告保留 PENDING_SEMANTIC_REVIEW，复核另存，不伪造用户签字。
- 三次预算缺失回答限定实际已读资料，未重现第二轮范围越界。一次 read_error 的 JSON 修复成功，最终状态、空引用和失败范围正确。
- 8765 已实际启动并通过浏览器提交跨文件问题。补充样例 `1f300f159bf34c0dba9e07d5e5006723` 的后端回答将专名中的 ASCII 连字符改成长横线；引用正确、页面原样呈现。独立审阅将此专名保真问题列为 P1 / BLOCK；不改变固定组自身 PASS。
- 本轮只保存 `replay-audit.json`、`web-smoke.json`、`semantic-review.md` 和同步当前文档，没有改产品代码或提示。下一步先定位并修复回答内的专名保真机制，以保存的失败样例回归；服务保持运行，不让用户重复跑尚未修复的测试。
