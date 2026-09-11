# 受限目录发现验收记录

## 当前任务入口（2026-09-11）

当前为**会话管理 Task 1–11 已实现并通过固定验收**。Runtime、工具结果、上下文、历史查询、CLI、页面、长历史摘要和进程恢复已接入持久 Session；完整 Python 381 项与三组浏览器回归通过，S1–S13 没有剩余 P0/P1 阻断。受限真实 DeepSeek 在 6 次用户提交、18 次模型请求内完成重启连续、更正、重新取证、报告发布、长历史摘要和旧调用回查；两次被程序拒绝的范围错误及后续修复均保留原始证据。设计见[会话管理模块设计 §0](../../docs/superpowers/specs/2026-09-10-session-management-design.md)，实施见[会话管理实施计划 §0](../../docs/superpowers/plans/2026-09-10-session-management.md)，S1–S13 与真实证据见[会话验收记录](session-live-check.md)。会话模块在此停止扩展；流式输出、Memory、MCP 和跨设备同步仍属于后续范围。

### 已完成的工具层基线

**工具层及结果契约复核已通过并收口**。已实现统一工具注册/校验/结果回填、模型意图与程序动作说明、网页/CLI 逐次确认，以及仅新建的 `write_file`。Loop 会核对冻结参数、程序预览、工具错误白名单和本次执行证明；读／列目录的伪成功不会进入证据，写入只有经过一次性发布边界且回执与批准内容一致才进入 `artifacts`。本地真实文件内容与批准预览一致，结果按原调用 ID 进入下一轮 context；拒绝、取消、超时、已有目标、发布竞态、重复发布及后续回答失败等边界已有证据。

工具层实施状态、验证命令和证据统一见 [工具调用层方案 §0](../../docs/superpowers/plans/2026-09-10-tool-runtime.md)及[工具层真实模型验证记录](tool-layer-live-check.md)。工具阶段完整 Python 回归 295 项、独立页面脚本、完整浏览器回归及两次独立代码 Gate 均通过。此前三个真实 run 共调用 DeepSeek 10 次：单文件完成、拒绝报告未落盘、确认报告实际创建并回填回执；结果契约修订没有新增真实 API 调用。这些证据证明工具层，不代表新增会话模块已实现或通过。
下方保留已完成目录阶段的结论和原始证据，描述的是前一阶段，不作为当前任务调度指令。

2026-09-08：用户要求优先输入、回答准确性及工具结果回填，授权进入目录发现，暂缓额外交互细化。

## 当前阶段结论（2026-09-09 范围复核）

**最小 Agent 闭环已验证，本阶段按该目标收口；受限目录发现的第三轮真实固定组也已通过。** 单文件真实会议节选三问及固定基准的证据见 [真实材料试用记录](real-file-check.md)。目录固定组自动及 AI 内容复核 12/12 PASS，36 次工具结果回填、13 处引用准确，支持“模型自主调用 → 实际执行 → 结果进入下一轮 context → 有依据地回答”的结论。

已知限制：补充网页曾将 `青禾-47` 改为 `青禾—47`。该样例仍为失败，不能宣称所有输出准确。新增保真校验代码保留，本地回归与旧结果重放已通过；**新版本真实复测已暂停／后置，原重启请求取消**。它尚未取得新的真实运行证据，不作为最小闭环阶段继续开放的理由，也不标记为真实修复验收完成。

当前没有要求用户重启或重跑终端的活动任务，也不自动开始更多保真加固或功能扩展。后续任务从用户新目标出发，按 [项目开发规则](../../AGENTS.md)限定范围和停止条件。固定样例通过不代表统计可靠性、用户收益已测量或完整 M1 已完成。

证据见 [第三轮原始复核](results/directory-evaluation-b1770c014ae64ccfb2972672e7331d87/semantic-review.md)与 [保真修复记录](results/directory-evaluation-b1770c014ae64ccfb2972672e7331d87/identifier-fidelity-fix.md)。历史记录中的 BLOCK 和当时的“下一步”保留为事实；后续调度以本节为准，不自动恢复旧待办。

## 历史验收记录

第二轮原始自动 10/12、重评自动 12/12、语义 11/12 BLOCK 的历史保留在 [第二轮真实复核](results/directory-evaluation-9871fc0a057d49c986eb5c1805030504/semantic-review.md)。

第一轮原始 8/12 及其三条回复协议失败保留在 [第一轮真实复核](results/directory-evaluation-78031bf496cf4659a3c2c34751c17e40/semantic-review.md)。没有覆盖任一原始机器报告或将评分修正冒充语义通过。

## 已有证据

- 最终模拟固定组：四类共 12 次自动检查通过，报告为 [SIMULATED_ONLY](results/directory-evaluation-0d5c0f7734cc40f98e22872b3d32e9d2/report.json)。前一份模拟报告保留，测试替身不证明真实模型选文件或回答能力。
- Runtime 脚本验证：初始请求无实际文件名或正文；目录结果只有元数据；模型选择两份文件后，下一轮请求同时包含两份完整内容和原调用 ID；引用由各文件本次快照生成。
- 安全与状态复审：已修复根目录被同名普通目录替换、失败重列后误报完整、无效参数重读后快照与范围不一致三项问题。Task 1＋2 SPEC 复审 PASS，132 项定向检查通过。
- 保真修复当时的完整 Python 回归：`python3 -W error::ResourceWarning -m unittest discover -s tests`，200 项通过，10.493 秒；本次范围复核没有重跑应用测试。
- 页面与入口：24 项 HTTP/CLI 检查通过；浏览器回归通过，含两份引用、完整/未完成范围、目录模式刷新与取消、迟到回复保护、缺密钥、5 种视口。主任务查看 [目录回答截图](../artifacts/web-directory.png) 确认布局正常；截图是模拟结果。
- 便捷启动脚本：`bash -n trial/verify-directory-and-serve.sh` 通过；独立审阅用临时假命令验证评测退出码 0/1/2 都不启动页面，仅 3 启动指定工作区及端口。没有调用真实模型。
- 目录实现及保真修复的代码审阅均为 **PASS**；第三轮固定组真实语义复核 **PASS**。旧网页失败保留，新版本真实验证已后置。独立审阅为 AI 复核，不代表用户签字。

有限保真校验只拒绝有当前原文对应的标识连接符变化，不自动改字，也不代表所有专名或推断都经过机器验证。若以后重新启动该质量项，再针对新版本取得必要的真实证据。

## 真实固定组标准

每次使用两份各小于 2 KiB 的合成资料，项目代号在一份文件、评审人和日期在另一份。文件名与关键事实不进入初始问题。更新场景重用此前已访问的文件名，更换代号，检查是否读到新内容。

| 场景 | 次数 | 必须通过 |
|---|---|---|
| 正常跨文件回答 | 3 | 自主列目录、读两份文件、回答三项事实、分别引用两份文件 |
| 同名文件更新 | 3 | 重新读取，回答新代号，无旧代号残留 |
| 缺失预算 | 3 | 完整检查发现范围，明确资料未说明预算 |
| 一份文件读取错误 | 3 | 实际错误回填下一轮，范围保持不完整，返回 unable |

全部场景检查工具结果及调用 ID 是否进入紧接着的模型请求。`completed` 只表示结构与来源检查通过；真实组自动全部通过后仍需逐条核对语义，将复核另存 Markdown，不覆盖原机器报告，不冒充用户签字。

本批不包含流式正文，因此不宣称完整 M1 完成。

## 文件变更清单

以下路径相对 `local-agent/`；文档中明确标注的上级路径位于工作区根目录。

| 范围 | 文件 |
|---|---|
| 文件工具与发现范围 | `local_agent/files.py`、新增 `local_agent/discovery.py` |
| 运行循环与引用 | `local_agent/runtime.py`、`local_agent/answers.py`、新增 `local_agent/prompts.py` |
| 页面与入口 | `local_agent/web_runs.py`、`local_agent/__main__.py`、`local_agent/demo.py`、`local_agent/static/index.html`、`local_agent/static/app.js`、`local_agent/static/app.css` |
| 固定验收与便捷启动 | `local_agent/evaluation.py`；新增 `local_agent/directory_evaluation.py`、`trial/verify-directory-and-serve.sh` |
| 两份演示资料 | 新增 `examples/discovery-workspace/plan.md`、`examples/discovery-workspace/review.md` |
| Python 回归 | `tests/test_answers.py`、`tests/test_cli.py`、`tests/test_web.py`、`tests/test_evaluation.py`；新增 `tests/test_discovery.py`、`tests/test_directory_runtime.py`、`tests/test_directory_evaluation.py` |
| 浏览器回归 | `tests/browser_web.cjs`、`tests/web_fixture.py` |
| 当前文档 | `README.md`、`trial/real-file-check.md`、本记录；工作区的 `docs/superpowers/specs/2026-09-07-local-agent-first-slice-design.md`、`docs/superpowers/plans/2026-09-08-local-agent-web.md`、新增目录发现实施计划 |

实际测试截图、合成运行日志和机器报告另外保存在 `artifacts/` 与 `trial/results/`，不作为真实模型通过的替代证据。
