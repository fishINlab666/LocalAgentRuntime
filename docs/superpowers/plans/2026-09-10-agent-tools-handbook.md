# Agent Tools 学习手册制作计划

## §0 当前进度

2026-09-10：**已核实，成品 Gate PASS**。三份源文档由主代理逐段完整阅读，独立审阅者完成来源清单与一次成品内容审查；HTML、静态检查、浏览器检查和截图查看均已完成。沿用上一份已确认的设计与 2026-09-09 优化要求，不重新请求确认。

- 验证假设：以同一个查看、读取、申请写入任务串联工具层，并保留原文及不同口径，可以形成可追溯、不误导开发范围的入门教材。
- 当前主要阻塞：无。没有本次文档的未完成必验项，不扩展应用验证。
- 完成条件：已满足。三份原文逐字符内嵌且校验值一致；各有意义主题有教学去向；自检配解析；冲突不被静默改写；桌面/手机、搜索、锚点、打印及无脚本阅读通过本地检查；一次独立成品内容 Gate 无剩余阻塞。
- 范围：新建独立 HTML、正文源稿、确定性组装脚本、静态与浏览器检查、本计划。旧手册及工作区已有改动保持不动；三份源文件只读。
- 不做：应用实现、会话或工具层重构、模型 API 调用、安装依赖、联网取材、发布、提交与推送。文档中的实施建议不是本次执行指令。
- 成本：本地文件与浏览器验证；真实模型 API 调用 0 次。

## 依据与结构

**Goal：**交付 `Agent-Tools-工具调用层学习与开发手册.html`，帮助读者理解工具请求、真实执行、用户意图与后果、错误回填、审批和本阶段验收。

**Architecture：**复用上一份 HTML 的内联样式和渐进增强导航；新正文采用具体任务—解释—误区—开发检查点—自检答案结构。三份原文经 HTML 转义完整嵌入，提供每行锚点、连续覆盖区间与 SHA-256；最终文件不依赖生成器或其他文件才能阅读。

**Tech Stack：**HTML/CSS/原生 JavaScript、Python 标准库、已安装 Playwright 与 Chrome。

源文件快照：

| 标识 | 文件 | LF 行数 / 字节 | SHA-256 |
| --- | --- | --- | --- |
| D | `0-个人本地智能助手-Agent项目设计方案.md` | 768 / 23713 | `3c1c322affc8056295a6810f4f6db6c6c0a208a9b9ea163564856e105e2f3779` |
| T | `1-工具调用层设计.md` | 206 / 8088 | `fa51e6da4803e00097c6e9ec5cd38fa6ca12b27b80acfbf864c06a9ed950872d` |
| M | `meeting/0910/会议录制：agent-meeting 0910-1.md` | 689 / 40043 | `06f2661816046ca0e6153e876ad63abbed0810ae2f23564ad84b3f682c538adb` |

项目设计的新文件名与旧手册原设计快照哈希一致。旧手册仅用于教学与阅读设计参照，不把 0829 会议混进本册的三源事实集；本册不宣称重新核验应用实现。

## 制作步骤与文件

- [x] 全文读取 D 1–768、T 1–206、M 1–689；阅读旧手册设计、最新计划 §0 与实现结构。
- [x] 独立来源审查：特别保留中风险审批、模型意图/实际后果、会话并行、状态/持久化、工具层职责等差异。
- [x] 创建 `tests/validate_tools_handbook.py`：校验源哈希、内嵌原文逐字符一致、覆盖区间连续、站内锚点、离线依赖与主线题目答案。
- [x] 创建 `docs/handbooks/agent-tools-content.html` 与 `docs/handbooks/build_agent_tools.py`，组装根目录最终 HTML。用户已接受的浅灰底、蓝色强调、系统字体与浮动导航优先于技能数据库的泛化视觉建议。
- [x] 执行 `python3 docs/handbooks/build_agent_tools.py` 和 `python3 tests/validate_tools_handbook.py`，只检查本文档。
- [x] 创建并运行 `tests/browser_tools_handbook.cjs`：375/768/1024/1440 宽度，折叠答案、来源行号深链、搜索清除与跨章恢复、移动焦点、阅读进度、打印、禁用脚本及无外部请求。截图写入 `artifacts/tools-handbook-2026-09-10/` 后实际查看。
- [x] 交付前一次独立内容 Review Gate；源哈希、浏览器检查通过且无剩余阻塞即停止，不扩展应用验收。

## 成品 Review Gate

### 技术交接 — PASS

范围仅为三源教学内容与本地 HTML 阅读功能，不代表 Agent 应用质量、实现状态或用户学习效果验收。

- 来源：主代理读完 D 1–768、T 1–206、M 1–689，独立审阅者另行全文阅读。交付前 shasum 三源结果与上表一致。内嵌文本经 HTML 解码与源 UTF-8 逐字符一致，1,663 行均有锚点与连续覆盖区间。原文行末空格用等价 HTML 实体编码，避免源码空白警告，不删除原文空白。
- 内容：10 个主线、4 个选读章节，主线自检与解析逐项对应；全项目章节、工具设计 §0–11 与完整会议时间线均有去向。事实来源限三份新材料，旧手册仅提供教学和样式参考。
- 独立内容 Gate：PASS，无需阻塞交付的 P1/P2 内容遗漏或原意偏差。中风险三口径、模型意图/程序后果、会话并行回应、Tool Runtime 完整职责、状态隔离与全局预算、错误恢复、新建/覆盖、进程内审批与重启恢复、原 200 测试基线、Provider/MCP 假设均保留并说明边界。
- 静态验证：`python3 tests/validate_tools_handbook.py` → `PASS: tools handbook — exact sources, complete line coverage, anchors, offline structure, 10 lesson answer pairs`。
- 浏览器验证：下列命令 → `PASS: tools workbook browser checks — four widths, source deep links, 10 answer pairs, search, print, keyboard, no JS, offline`。本地 Chrome 离线上下文直接打开 HTML，无静态服务、外部页面请求或真实模型调用。

```sh
NODE_PATH=/Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules /Users/wujingyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser_tools_handbook.cjs
```

- 页面范围：1440/1024/768/375，手机横屏与放大文字；固定侧栏不遮正文、无整页横向溢出；10 组答案开合；原文直接深链与搜索隐藏后的恢复；无结果清除；当前章节、进度、返回顶部；手机焦点循环/Escape/恢复；打印展开全文并恢复折叠；禁用脚本后导航、答案、原文可用；控制台无错误。
- 首轮问题及最小修订：无脚本状态下，长距离平滑锚点滚动与紧接的答案定位冲突，点击等待超时。对比同一操作的 smooth/auto 后定位；仅为 no-script 回退加即时滚动。失败断言不变，重跑通过。不修改应用或旧手册。
- 格式：`git diff --check` 无问题；新 HTML 另经 `git diff --no-index --check /dev/null Agent-Tools-工具调用层学习与开发手册.html` 检查，无空白警告。
- 已查看截图：[桌面首页](../../../artifacts/tools-handbook-2026-09-10/desktop-cover.png)、[桌面审批示意](../../../artifacts/tools-handbook-2026-09-10/desktop-approval.png)、[手机第 06 章](../../../artifacts/tools-handbook-2026-09-10/mobile-gates.png)、[手机审批示意](../../../artifacts/tools-handbook-2026-09-10/mobile-approval.png)。
- 剩余未知：真实读者学习效果、其他浏览器/阅读器、所有打印设备的分页兼容性；不声称原文中的应用功能已实现。

### 通俗交接 — PASS

可交付阅读。手册先用一次文件任务串起概念，再给名字；答案可展开，原文可按行号查回，不同意见没有合并成假定结论。三份源文件没改，桌面和手机阅读、搜索、打印与无脚本备用阅读已检查。

小白翻译技能用于把术语拆成可观察步骤、区分意图与实际后果；UI/UX 检查沿用既定极简样式；Review Gate 保留不同口径、当时陈述与实现证据的区别。实际学习效果由用户阅读反馈判断，不再请求批准交付。

一致性检查：两份交接均为 PASS，无剩余内容/阅读功能阻塞，未知与应用范围限制一致。信息告知即可；下一步是用户阅读或反馈，不自动继续应用开发。

## 本次新增文件与交付

1. `Agent-Tools-工具调用层学习与开发手册.html` — 最终独立离线文档，349,806 字节。
2. `docs/handbooks/agent-tools-content.html` — 教学正文源稿。
3. `docs/handbooks/build_agent_tools.py` — 复用样式、内嵌原文及覆盖索引的生成器。
4. `tests/validate_tools_handbook.py` — 来源与静态结构检查。
5. `tests/browser_tools_handbook.cjs` — 本地阅读功能检查与截图。
6. 本计划 — 来源、范围、进度与 Gate 证据。

未提交或推送。工作区同时存在其他应用改动，本次未修改、回退或将其纳入本册验收。
