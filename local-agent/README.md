# Local Agent：可配置助手、资料问答与能力扩展

输入问题并选择单文件或目录模式。模型自主发起 `read_file`，或先 `list_files` 发现路径再选文件读取；程序把带原调用编号的工具结果交回下一轮模型请求，再校验回答与本次原文引用。填写输出路径后，还可由模型提出完整报告，经用户确认后新建文件，并将实际写入回执交回模型。浏览器页面与命令行共用同一 Runtime。

## 当前状态

**受管本地文档导入闭环已完成固定真实验收。** 页面可以选择文件或文件夹，将 `.md/.txt`、带文字层 PDF 和 DOCX 保存为 State Store 内的私有副本，并在新建的持久会话中按需搜索、读取和返回原始页码／段落／表格行。离线固定组、完整 Python 661 项和六组浏览器回归通过；真实 DeepSeek 固定组以 2 个 Run、8 次模型请求验证了事实、引用、工具结果回填、跨重启历史、跨 Import 隔离和成对备份恢复。原始机器报告因旧评分器要求英文标签而保留为 `FAILED`，修正后只读重放 22/22 必需检查通过，AI 语义 Gate PASS，未追加真实调用；细节与边界见[真实验收记录](trial/import-live-check.md)。

**多助手配置、Skills、MCP 已接入同一 Runtime，固定真实模型联合验收及 AI 语义复核均已通过。** 页面和 CLI 可以创建/选择助手、安装和绑定能力；会话保存固定配置版本，旧会话继续原配置。MCP 首批只接本地 stdio 的明确白名单能力；Skill 读取方法和引用，不执行脚本。当前状态和验证边界以[扩展实施计划](../docs/superpowers/plans/2026-09-14-agent-extensions.md)为准。这里的多助手指可切换的配置化助手，仍只有一个活跃 Run，不包含自动派发子 Agent。

本机已准备扩展环境。启用扩展时使用 `.venv/bin/python`，首次在其他环境安装：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-extensions.txt
.venv/bin/python -m local_agent serve --workspace examples/workspace --open
```

页面的“助手与能力”区域可以安装、绑定 Skill/MCP 并创建新会话；详细字段、合成 MCP 示例和操作顺序见[助手与能力说明](examples/agents/README.md)。旧会话不会自动取得后来新增的能力。停用助手或能力会阻止新调用并取消受影响的运行。密钥仍只从环境变量读取。

无模型的管理命令：

```sh
.venv/bin/python -m local_agent agents list
.venv/bin/python -m local_agent capabilities install-skill examples/skills/project-brief
```

固定联合验收入口是 `scripts/evaluate_extensions.py`；默认只运行一例明确标记的离线演练，`--real` 才发起最多 6 个 Run、60 次模型请求（包括摘要和修复）。可直接运行 `bash trial/verify-extensions.command`，脚本在缺密钥时隐藏输入，然后开始固定真实组。脚本只使用新建的合成材料，并单独标记合成审批、机器检查和待内容复核；不能将离线演练当成真实模型或用户验收。

以下保留已交付的文件/工具/会话基线及其历史证据。

**会话管理 Task 1–12 已实现，当前离线 Gate 通过。** Runtime、CLI 和页面共用持久 Session：同一会话可跨重启继续，历史搜索可安全续页，长正文按最终 12 KiB 结果信封分页，超过 128 KiB 的固定历史在压缩后仍保留新旧约定、未完事项和原文回查能力。完整 Python 387 项通过；三组既有浏览器回归继续有效。此前受限 DeepSeek 的 6 次提交为五个目标场景留下成功证据；更正回答因引用范围错误失败，修复后只有离线证据，尚未真实复跑，不能称为真实全量验收通过。证据见[会话验收记录](trial/session-live-check.md)，目标与实施见[会话模块设计](../docs/superpowers/specs/2026-09-10-session-management-design.md)和[实施计划](../docs/superpowers/plans/2026-09-10-session-management.md)。

**工具层及结果契约复核已通过，本批收口。** 默认仍是只读问答；填写输出路径才启用逐次确认的新建文件能力。Loop 会校验冻结参数、用户所见预览、工具错误及本次真实执行证明，写入成功还必须经过一次性发布并取得与批准内容一致的回执；结果始终按原调用 ID 进入下一轮。此前真实单文件、拒绝报告、确认报告三个 run 共使用 10 次 DeepSeek 请求，确认后实际创建的 957 字节报告与预览一致；本次加固没有新增真实 API 调用。证据见[真实验证记录](trial/tool-layer-live-check.md)，当前状态见[阶段验收记录](trial/directory-check.md)，具体实施见[工具层方案](../docs/superpowers/plans/2026-09-10-tool-runtime.md)。后续开发遵循根目录[项目规则](../AGENTS.md)。
- 运行环境：macOS、Python 3.14.6；内置文件能力使用 Python 3.11+ 标准库，扩展依赖见 `requirements-extensions.txt`。Python 3.11 / Linux 尚未实测，Windows 不支持本版文件打开方式。
- 原内置范围：一个 DeepSeek Provider、`list_files`、`read_file` 及按任务注册的 `write_file`、一次任务的完整消息链、状态、取消/超时和本地日志；本阶段在相同链路增加 Skill/MCP 适配。
- 单文件模式只读用户指定的一份 UTF-8 `.md` / `.txt`；目录模式允许模型发现和选择最多 4 份文件，每份上限仍为 32 KiB。写工具只允许新建用户指定的一个 `.md` / `.txt`，最多 32 KiB，不覆盖、不自动更名或创建父目录；没有 Shell。
- 两份会议与产品设计稿用于确定需求；会议 02 的小节选已用于真实模型验证。原始会议 01 超过大小上限，三份长文的综合试用在后续阶段。

工具阶段已按结果回填与确认后的实际产物收口；会话管理已完成代码和离线固定场景，并保留受限真实连续演示的证据边界。流式正文尚未实现，不能将当前版本称为完整 M1。前一阶段已通过的真实基准没有因本次开发而重复执行。

## 使用本地页面

单文件页面及引用修复的原有 112 项单元/HTTP 测试、浏览器回归与独立代码复审通过。真实会议节选前两轮暴露引用问题，现采用“模型选引用位置、程序从本次读取快照提取原文”：第三轮三问 3/3 通过，9 处引用与原文一致；旧四类基准重新执行 12/12 通过，均另附 AI 内容复核。不能把技术通过写成已测量的用户收益。失败记录、措辞问题及证据见 [真实材料试用记录](trial/real-file-check.md)。

在之前已配置 `DEEPSEEK_API_KEY` 的终端启动：

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -m local_agent serve --workspace examples/workspace --open
```

默认打开 `http://127.0.0.1:8765`。终端保持运行；`Ctrl+C` 关闭服务并取消在途任务。端口被占用时加 `--port 8769`。页面显示“DeepSeek 已配置”表示已加载凭据，是否连通由实际问答验证；缺密钥时页面可打开，但不能开始问答，不会自动切到模拟模式。

### 导入文件或文件夹

1. 启动最新服务后，在左栏点击“导入本地资料”。旧服务进程不会自动取得新入口，升级代码后需停止并重新启动一次。
2. 选择多份文件或一个文件夹。页面支持 `.md`、`.txt`、`.pdf`、`.docx`；其他格式会列为忽略项，不会上传。
3. 填写会话名称并选择兼容的目录型助手，核对文件数和大小后点击“开始导入”。解析完成后页面自动进入新会话，输入问题即可使用。

PDF 必须含可提取的文字层；扫描件或纯图片 PDF 当前会明确失败，不做 OCR。DOCX 会按段落和表格行提取，PDF 会保留原页码；回答右侧的来源信息显示原逻辑文件名及页／段／表格行。导入时浏览器顺序发送所选文件的原始字节，本机解析后生成只读正文块。初始模型请求不预装正文；`search_documents` 会把受限的命中摘录发给 Provider，`read_file` 会把模型选中的提取正文发给 Provider。

默认 State Store 位于 `~/.local/share/local-agent/`。每批导入保存在 `imports/<import-id>/` 下，包含私有原件副本、只读提取工作区、来源位置表和独立 `artifacts/`；页面和模型不会获得这些绝对路径。原始外部文件移动或删除后，受管会话仍可跨服务重启继续。需要长期保留时，应同时备份 SQLite 与 imports sidecar：

```zsh
python3 -m local_agent sessions backup "/Users/you/Backups/LocalAgent"
python3 -m local_agent sessions restore-backup \
  "/Users/you/Backups/LocalAgent/sessions.TIMESTAMP.sqlite3" \
  "/Users/you/Backups/LocalAgent/sessions.TIMESTAMP.sqlite3.imports" \
  "/Users/you/LocalAgent-restored"
```

恢复目标必须是尚不存在的目录。恢复后以 `--state-dir "/Users/you/LocalAgent-restored"` 启动页面；Session、Import 和助手快照保持不变，文件身份会绑定到新目录。若任务预先声明输出路径并获批准，生成文件位于该 Import 的 `artifacts/`，可从页面右栏下载。当前没有自动同步、自动删除、向量检索或跨设备恢复；State Store 和备份都由用户本地保管。

### 现在可以怎么玩

| 模块 | 页面中的入口 | 适合尝试的任务 |
|---|---|---|
| `file-qa` | 左侧“单文件问答” | 从工作区的一份 `.md/.txt` 中找事实并核对逐字引用 |
| `directory-qa` | 左侧“目录问答”或“导入本地资料” | 发现工作区文件，或搜索导入的 MD/TXT/PDF/DOCX 并综合资料 |
| `project-brief` | 左侧“项目简报” | 组合本地资料、已绑定 Skill/MCP 和受控写入 |
| 持久会话 | 左侧新建、切换、改名、归档和恢复 | 重启服务后继续同一资料范围与历史 |
| 文件工具 | 模型自主调用；中间显示状态 | `read_file`、`list_files`、`write_file`、`session_history` |
| Skill / MCP | 右侧“管理助手、Skills 与 MCP” | 导入文本 Skill；连接受限本地 stdio Tool/Resource/Prompt |
| 证据与控制 | 中间回答，右侧依据/产物/执行记录 | 取消、恢复中断、写入审批、核对 SHA-256 和引用 |

三个入门玩法：

1. 选择“单文件问答”，使用 `demo-note.md`，问“项目代号、评审人和演示日期分别是什么？引用原文。”
2. 以 `examples/discovery-workspace` 启动服务，选择“目录问答”，让 Agent 综合项目资料并检查右栏的发现/读取范围。
3. 选择“项目简报”，先在“本轮设置”声明唯一输出路径 `brief.md`，再要求生成简报；核对完整预览后批准或拒绝。

左栏负责选择助手和持久会话，中间是连续任务、回答与审批，右栏是工作区、引用、产物、执行记录和能力管理。输入任务后按 `Enter` 发送，按 `Shift+Enter` 换行；发送按钮始终位于输入框下方。手机端用左上角打开助手/会话，用右上角打开本轮工作；“本轮设置”和“新建会话”表单默认收起。临时单文件任务尚未选择文件时，页面会展开设置并把焦点移到文件输入框。

当前边界：只接 DeepSeek，同一时刻只有一个活跃 Run；直接工作区读写仍限 `.md/.txt`，受管导入额外接受带文字层 PDF 与 DOCX。写文件须由用户在提交前声明唯一目标且每个任务最多成功写一次；MCP 首批仅本地 stdio 白名单，Skill 脚本不执行，也没有自动子 Agent、Cron、Memory、远程 MCP/OAuth 或流式输出。页面按游标加载更多会话和 Run；这些界面能力不会扩大工作区、助手或会话权限。

操作顺序：

1. 在左栏选助手，创建持久会话并固定单文件或目录范围；也可选择临时任务。
2. 在中间输入问题。需要指定 Skill、MCP Prompt 或输出路径时展开“本轮设置”；留空输出路径时只问答。
3. 回答生成后，在中间阅读结论，在右栏核对引用、目录覆盖范围和执行步骤。
4. 如需文件，提交前填写一个新的相对目标。模型只能为该路径提出完整内容，且只有收到真实 Artifact 回执后页面才显示“已创建”。
5. 切换历史 Run 可查看旧回答和只读审批；刷新不会重复提交。持久会话可跨服务重启继续，临时任务不提供这一保证。

工作区由 `--workspace` 指定，普通会话不能扩大它。要直接使用另一个文件夹，可重新启动并更换该参数；也可通过“导入本地资料”只复制明确选择的文件并创建独立受管会话。日志目录必须在普通可读工作区外，必要时用 `--log-dir` 指定。单文件模式只读取提交的文件；目录模式允许模型发现并读取本轮需要的文件。各模式都不提前把正文放进初始请求；真实模式会将问题、目录元数据和实际读取的正文发送给 DeepSeek。

只看页面和本地执行效果，无需密钥：

```zsh
python3 -m local_agent serve --workspace examples/workspace --demo --port 8766 --open
```

`--demo` 用于原只读问答演示，输出路径请留空；始终显示模拟标识，直接回显读到的文本，不理解问题，不能代替真实问答验收。页面只监听 127.0.0.1，检查 Host、Origin 和随机进程令牌，不提供公网或局域网服务。默认日志不保存正文；问答和结果暂存在服务内存供页面查看。

页面测试：`PYTHONPATH=. python3 -W error::ResourceWarning -m unittest tests.test_web -v`。浏览器测试使用本地合成 Provider，覆盖问答、引用、取消、刷新、跨任务迟到响应、提交超时、审批、会话隔离、Agent/Skill/MCP 管理、游标续页、移动抽屉和窄屏；`browser_workbench.cjs` 专门检查三栏工作台。截图保存在 `artifacts/`。页面真实使用体验仍需用户以自己的资料核对，原内核 12 条真实评测不重复计为网页实测。

本机完整浏览器回归命令（Node Playwright 来自已安装运行环境；不属于产品依赖）：

```zsh
python3 /Users/wujingyu/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8767' --port 8767 \
  --server 'PYTHONPATH=. python3 tests/web_fixture.py --port 8768 --missing' --port 8768 \
  -- env NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
  /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_web.cjs
```

## 先运行不需要密钥的演示

在终端复制以下命令：

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent
python3 -m local_agent demo
```

演示使用测试替身决定“请求读取 → 根据工具返回回答”，但文件工具和 Runtime 真实执行。输出会明确显示 `provider.simulated: true`、`state: completed`、原文引用和 `trace_path`。它验证本地链路，不验证模型能力。

## 目录发现与跨文件问答

页面启用“目录发现”后只需要填写问题。CLI 对应 `run --discover`，与 `--file` 二选一。模拟演示用 `python3 -m local_agent demo --discover`。

目录模式从 `list_files({"path":".","intent":"发现相关资料"})` 开始，只列一层，不预读正文。只返回可见 `.md` / `.txt` 普通文件与普通子目录的相对路径和文件大小；模型必须再次调用才能列子目录。隐藏项、符号链接、硬链接及特殊文件不进入可读范围。一次扫描超过 200 项（包含被排除项）会明确失败，不返回冒充完整的部分列表。

每次提问只允许读取本轮列出的文件，最多成功读取 4 个不同路径；重新读取失败不会释放这个额度。工具结果中附带 `scope`：已列目录、已发现文件、已读文件、未读文件与未列目录。成功结果和错误结果都按原 ToolCall ID 回填，后续模型请求保留之前所有工具结果；引用只从当前有效读取快照提取。工作区根目录身份被替换会拒绝读取。

`answered` 至少需要成功读取和一项有效引用，但程序不能自动保证答案全部推断成立。`not_found` 必须完整列出并读完本次发现的受支持范围；空范围也可返回未找到。`unable` 必须有实际工具错误且范围不完整。失败重读或失败重列会撤销相应完成状态，防止用旧证据假称已检查完整。

真实固定验收命令：`python3 -m local_agent evaluate-directory`。正常跨文件问答、同名文件更新、预算缺失、读取错误四类各 3 次，两份材料各小于 2 KiB。标准答案不进入初始提示；报告另外检查首次列根目录、两份文件读取、工具结果进入紧接着的模型请求，以及联合回答分别引用两份资料。真实自动通过为 `PENDING_SEMANTIC_REVIEW`，须另附内容复核；模拟只能得到 `SIMULATED_ONLY`。报告保留全部失败，输出目录为 `runs/directory-evaluation-*/`，完整合成数据日志用于核对上下文。

也可以在原来已配置密钥的终端，先按 `Ctrl+C` 停掉旧页面，再整行执行：

```zsh
bash "/Users/wujingyu/Desktop/AI/projects/dev-agent/local-agent/trial/verify-directory-and-serve.sh"
```

该脚本自动切换工作目录，执行 12 次真实验收并将报告保存到 `trial/results/`。自动检查全部通过后启动 8765 页面，工作区切换为两份合成资料 `examples/discovery-workspace`，语义复核仍需完成。任一失败会停止并保留报告，不继续启动页面。

## 接入真实模型

用户已确认第一版沿用会议建议，接入 DeepSeek。官方接口地址固定为 `https://api.deepseek.com/chat/completions`。采用可替换的默认模型名 `deepseek-v4-flash`；这不代表已验证该模型在本任务上的表现。

| 环境变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | 必填，供应商密钥；优先于备用变量 |
| `AGENT_API_KEY` | 可选的同用途备用变量，仍访问 DeepSeek |
| `AGENT_MODEL` | 可选，默认为 `deepseek-v4-flash` |
| `AGENT_USE_SYSTEM_PROXY` | 可选，`1` 表示使用系统代理；默认 `0` 直连，避免代理切断较长的模型请求 |

程序只读取进程环境，不自动加载 `.env`。不要把密钥发到聊天或写进代码。在本机 zsh 中可用隐藏输入设置，输入值不会显示在终端或成为命令历史：

```zsh
read -rs 'DEEPSEEK_API_KEY?输入 DeepSeek API Key：'
export DEEPSEEK_API_KEY
export AGENT_MODEL=deepseek-v4-flash
```

随后先用合成样例运行真实问答：

```zsh
python3 -m local_agent run \
  --workspace examples/workspace \
  --file demo-note.md \
  --question '项目代号、评审人和演示日期分别是什么？引用原文。'
```

`run` 会把问题和读取到的文件正文发送给云端模型。`--file` 是相对 `--workspace` 的路径；日志目录必须位于这个可读工作区之外。首次使用请保留合成样例，真实会议材料另准备脱敏副本。

DeepSeek 请求默认直连。只有网络环境明确要求代理时，才设置 `AGENT_USE_SYSTEM_PROXY=1`；日志里的 `provider.proxy_mode` 会显示实际模式。

需要生成报告时，在真实终端运行（确认提示需要交互终端，管道输入不会自动批准）：

```zsh
python3 -m local_agent run --workspace examples/discovery-workspace --discover --output-file report.md --question '请核对项目代号、评审人与演示日期，引用原文并生成报告。'
```

程序展示完整内容后，输入 `y` 确认或 `n` 拒绝。确认只适用于当前工具调用；每次最多新建一个产物，路径必须与用户指定值一致。已有目标或缺少父目录会停止，由用户处理。网页和 CLI 的最终 `artifacts` 记录实际路径、字节数及 SHA-256；即使后续模型回答失败，已创建文件及回执也会保留。产物不作为本次输入证据。

按 `Ctrl+C` 请求取消。取消或任一超时后，主循环不会启动下一步，迟到读取不能更新证据；未发布写入不能在随后绕过取消或截止时间。最终发布与取消由同一锁仲裁，已发布文件不会自动删除，真实回执保留；已发出的 HTTP 请求由后台线程与网络超时收尾，不能保证云端计算或计费立即中止。首版不自动重试请求。

适配实现使用官方的 [Tool Calls 消息协议](https://api-docs.deepseek.com/guides/tool_calls/)，显式设置 [非思考模式](https://api-docs.deepseek.com/guides/thinking_mode/) 与非流式输出；模型名称取自核对时的 [DeepSeek 官方文档](https://api-docs.deepseek.com/)。这里只保存外显请求和结果，不依赖隐含思维链。

## 固定验收：四类场景，每类三次

配置密钥后执行：

```zsh
python3 -m local_agent evaluate
```

评测创建临时的合成文件，每次使用新的 Runtime 和独立 Trace。随机项目代号等标准答案保存在评测逻辑及报告中，不放进模型的初始提示词或工具说明。临时输入结束后清理，完整合成数据 Trace 与报告保留在 `runs/evaluation-*/`。

| 场景 | 每次必须满足的标准 |
|---|---|
| `known`：已知事实 | 自主读文件，回答包含项目代号、评审人和日期，引用有效 |
| `not_found`：预算未记载 | 读完整文件，返回 `not_found`，明确未说明，人工确认没有编造 |
| `missing_file`：文件不存在 | 真正尝试读取，收到 `FILE_NOT_FOUND` 并回填，最终 `unable` |
| `changed`：更换代号 | 新运行重新读取，回答包含新代号等关键事实，引用有效 |

输出 `report_path` 指向报告，每个 trial 列出场景、标准答案、自动检查、结果及对应日志。自动检查只预筛格式、关键事实和部分固定措辞，人工还要核对答案是否忠于原文、引用能否支撑结论、是否夹带错误事实。

| 报告 gate | 含义 |
|---|---|
| `NOT_RUN` | 无可用配置，真实测试未执行 |
| `FAILED` | 任一场景未达标，或被取消导致不足 12 次；整组不放行 |
| `SIMULATED_ONLY` | 测试替身通过预筛，不能作为真实模型证据 |
| `PENDING_HUMAN_REVIEW` | 真实模型 12 次自动预筛通过，仍待逐项人工核对 |

程序不会自动生成“最终通过”。工程师应新建同一评测目录下的 `human-review.md`，记录评审人、时间、报告路径、12 条 trial 的语义核对与结论；不要覆盖原报告。任一必过项失败不能宣称该组通过，应保留失败并按本批验收目标安排修复与验证。仅评分器修正可重放旧结果；新模型行为需要真实运行证据，正式宣称新版本整组通过时需完整执行该组。不要因文档或评分修正机械重跑。12 次全过只表示固定样例通过，不能宣称统计可靠性。

## 结果与运行限制

最终答案严格使用这个结构，引用必须与本次读取的完整行逐字相同：

```json
{"status":"answered","answer":"项目代号是青禾-47。","citations":[{"path":"demo-note.md","start_line":1,"end_line":1,"quote":"项目代号：青禾-47"}]}
```

`answered` 要求成功读取及至少一项引用。单文件 `not_found` 要求目标成功读取，目录模式则要求本次发现范围完整检查；允许空引用。`unable` 在只读任务中要求实际工具错误且证据不完整；在报告任务中也可说明实际写入失败或被拒绝，引用为空。JSON 外附加文字、未知字段、类型错误、引用不匹配都会被拒绝。结构校验不自动判断答案语义。

模型只需返回引用的 `path`、`start_line`、`end_line`，程序校验后从本次读取快照提取完整原文作为 `quote`，因此页面和 CLI 的上述结果格式不变。模型如果自行提供 `quote`，仍须与原文完全一致；错误摘录不会被自动替换成通过。合法行号和真实原文不等于答案语义正确，仍需核对引用是否支持结论。

| 运行 state | 含义 |
|---|---|
| `completed` | `answered` / `not_found` 通过结构与引用检查，仍需判断语义 |
| `unable` | 读取/写入未完成或用户拒绝；没有伪造成功 |
| `validation_failed` | 回答格式或证据不合格 |
| `cancelled` / `timed_out` / `max_steps` | 取消、超时或轮数限制导致停止 |
| `failed` | 配置之外的请求、协议、上下文或日志错误，详见 `stop_reason` |

`run` / `demo` 正常结构完成退出码为 0，任务未完成为 1，配置错误为 2。`evaluate` 未执行为 2、任一项失败为 1、待人工核对为 3；故自动预筛全过也不返回“验收通过”的退出码。

默认限制：最多 6 次模型调用；单轮最多执行 4 个工具请求，其余各自返回 `TOOL_CALL_LIMIT`；活动执行 120 秒、模型调用 45 秒、工具执行 5 秒；人工确认累计另限 300 秒，不占活动执行时间。每次请求的序列化输入最多 64 KiB，模型输出最多 2,048 tokens，HTTP 响应最多 2 MiB。输入字节上限是保守工程预算，不是精确 token 计数，真实模型容量仍需实测。

读取工具只允许指定或本轮发现的文件；拒绝绝对路径、`..`、隐藏路径、符号链接、硬链接、目录、FIFO、二进制、非法 UTF-8、超限文件及读取中变化的文件。文件打开逐级检查目录并核对固定根目录身份，避免只检查路径字符串。文件内容不能扩大工具权限。

日志默认位于 `runs/`，每次一个 JSONL，包含调用编号、角色、阶段、耗时、错误、用量（供应商未返回则为空）和内容摘要。真实 `run` 默认不存问题、正文、回答、模型 intent 或确认预览全文；显式加 `--debug-content` 才保存全文。演示与合成评测开启正文日志，方便核查回填。不要在真实材料上无意开启正文调试。

## 工程核对

从项目根目录运行：

```zsh
cd /Users/wujingyu/Desktop/AI/projects/dev-agent
PYTHONPATH=local-agent python3 -m unittest discover -s local-agent/tests -v
```

| 文件 | 职责 |
|---|---|
| `local_agent/runtime.py` | 消息循环、调用关联、停止与终态 |
| `local_agent/tool_runtime.py`、`file_tools.py` | 注册、统一参数/结果、执行决定及文件证据策略 |
| `local_agent/approvals.py`、`console_approval.py` | 本轮确认、等待预算、取消与终端交互 |
| `local_agent/write_file.py` | 确认后的仅新建写入、原子发布及回执 |
| `local_agent/provider.py` | DeepSeek HTTP 协议、配置、响应与错误转换 |
| `local_agent/files.py` | 文件权限边界及完整读取 |
| `local_agent/discovery.py` | 每次任务的目录发现、读取授权与覆盖范围 |
| `local_agent/answers.py` | 固定 JSON 与引用检查 |
| `local_agent/prompts.py` | 两种模式的任务规则与共同引用格式 |
| `local_agent/trace.py` | 每次运行的本地事件记录 |
| `local_agent/evaluation.py` | 四类固定场景与未放行的评测报告 |
| `local_agent/directory_evaluation.py` | 两文件固定验收与上下文回填核对 |
| `local_agent/import_evaluation.py` | 两轮混合文档导入、重启、隔离和备份恢复固定验收 |
| `local_agent/demo.py`、`__main__.py` | 模拟替身与运行入口 |
| `tests/` | 文件、引用、协议、Runtime、入口与验收规则测试 |

已实现的单文件、页面及目录能力见 [目录发现计划](../docs/superpowers/plans/2026-09-08-local-agent-directory-discovery.md)，当前阶段结论统一见 [阶段验收记录](trial/directory-check.md)。持久会话、配置化助手、Skill 与本地 stdio MCP 已接入 Runtime、CLI 和页面；流式输出、长会议分段、Cron、Memory、自动子 Agent、远程 MCP/OAuth 和多 Provider 仍在本批范围外。
