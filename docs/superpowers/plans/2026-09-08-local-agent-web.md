# 单文件本地页面实施计划

**Goal:** 让用户在浏览器中对指定工作区内的单个文本提问，核对原文依据，并观察和取消执行。

**Architecture:** Python 标准库 HTTP 服务绑定 127.0.0.1；启动时冻结工作区。页面发送相对文件路径和问题；后台线程调用既有 Runtime、ReadFile、Trace。当前进程只允许一次在途运行，保存最近 20 次任务供页面刷新恢复，退出后不恢复会话。

**Tech Stack:** Python 标准库、HTML/CSS/JavaScript；unittest 和浏览器验证。无 Git 仓库，直接在现有 local-agent 子目录执行；用户已确认浏览器页面及单文件范围。

## 页面与边界

- 左侧文件路径、问题和运行/取消按钮；右侧答案、独立引用卡片和可展开的执行记录。
- 工作区显示完整路径，界面只提交相对路径；支持 UTF-8 .md/.txt，32 KiB 上限沿用 ReadFile。
- 显示 DeepSeek 是否已配置；缺密钥不回退模拟。演示仅通过显式 --demo 启动，持续标识模拟状态。
- 引用、模型回答和工具字段用 textContent 展示，不解析 HTML。最终答案校验结束前不显示为成功。
- 开始按钮旁说明文件内容会发给 DeepSeek，默认日志不存正文。
- 本地接口校验 Host、Origin 和进程随机令牌，拒绝跨站调用；令牌不会进入 URL、日志或模型消息。
- 页面刷新恢复当前任务，通信中断不误判任务停止；取消确认后等 Runtime 终结。

## 执行顺序

- [x] 新建 tests/test_web.py：先验证真实 HTTP 边界、任务提交与完成、并发拒绝、取消、缺密钥、失效任务和路径拒绝。
- [x] 运行 `PYTHONPATH=. python3 -m unittest tests.test_web -v`，确认功能缺失导致失败。
- [x] 新建 local_agent/web.py（HTTP 协议和固定资源）、local_agent/web_runs.py（线程运行与事件快照），复用现有 Runtime，不预读文件。
- [x] 新建 local_agent/static/index.html、app.css、app.js；增加 __main__.py 的 serve 命令、--workspace、--port、--demo、--open。
- [x] 运行 web 测试并修复；运行全部 unittest 和手册回归。
- [x] 启动本地服务，用浏览器验证桌面/窄屏、问答、引用、失败、取消、刷新、无密钥和 HTML 文本安全；保存截图。
- [x] 更新 README 的启动和真实体验步骤；交付可打开的页面。真实 API key 若不在执行环境中，明确需要从原 Terminal 启动真实模式，不读取用户密钥。

## 实现与审阅证据

- 最终验证：104 项 unittest/HTTP 检查通过；Python 编译与手册回归通过；浏览器自动化通过，覆盖 1440/1024/768/375 宽度和横屏；独立复审 PASS，四项问题已关闭，无剩余 P1/P2。
- 原有内核未修改；新页面通过同一 Runtime 执行。后台没有用户 Terminal 中的密钥，页面浏览器验证使用合成 Provider。
- HTTP 回归增加工作区被链接替换的拒绝检查；读取器必须仍指向服务启动时固定的规范路径，随后依靠原有逐级 O_NOFOLLOW 读取。
- 独立审查发现的旧任务延迟响应、DOM AbortError 分类及历史工具错误遮盖终止原因均补充回归。页面以任务 ID、视图代次、快照版本拒绝过期响应；请求超时后提示刷新确认，不自动重复提交。
- 浏览器测试使用现有 Node Playwright 与本机 Chrome；Python Playwright 未安装，未增加项目依赖。`tests/web_fixture.py` 仅提供测试替身和合成文本。
- 单文件页面交付之后，下一步是用户用真实小文件核对体验，再决定进入目录发现；本次未实现 list_files、流式正文或完整 M1。

## 验收

测试命令在 local-agent 下运行：`PYTHONPATH=. python3 -W error::ResourceWarning -m unittest discover -s tests -q`。
手册校验在项目根运行：`python3 tests/validate_handbook.py`。
真实启动：`python3 -m local_agent serve --workspace examples/workspace --open`。
无密钥预览：`python3 -m local_agent serve --workspace examples/workspace --demo --port 8766`。
固定单文件模型验收继续沿用既有报告；本批网页测试验证接入和呈现，不宣称新增目录发现、流式正文或真实用户收益已验证。

## 后续真实材料试用（2026-09-08）

- 使用会议 02 的 2.3 KB 连续原文节选跑三道真实网页题。首轮两道引用失败，缺失信息题通过；旧结果保持 FAILED。
- 诊断证据表明模型错用行号且改写引用。最小修复已写入 Runtime：仅模型侧正文转换为行号到原文映射，保留原始快照、路径权限和严格校验。Demo 与测试替身同步格式；无新增页面能力或工具。
- 审阅发现极多短行的编号会撑大请求，已增加完整原文回退且保留原预算检查；日志与 Provider 使用同一最终请求。修复后 108 项单元/HTTP 检查、浏览器回归和独立复审通过；等待用户重启原密钥终端的真实服务，再跑三问及语义核对。
- 材料、原始运行编号及放行条件见 `local-agent/trial/real-file-check.md`；目录发现尚未开始。
- 第二轮真实三问在最新提示词下有两题结构通过，一题仍 `INVALID_CITATION`。已进一步采用引用位置协议：模型选择行号，程序在已有快照上提取 quote；兼容的模型 quote 仍需精确匹配。未记载完成不等于尚未执行的语义边界已补入提示词。
- 新协议 112 项本地检查、浏览器回归及独立复审通过；等待再次启动服务，真实三问和旧四类基准仍待重跑。来源真实性检查不能代替答案支持关系和用户体验核对。
- 用户再次启动后，第三轮三问 3/3 程序检查通过，9 处引用一致；主任务与独立 AI 逐题复核通过，措辞体验问题另列。报告：`local-agent/trial/results/real-web-87f4e1ec-749e-4fdc-9b03-4cbf99056ec8/`。
- 新增 `local-agent/trial/run-web-baseline.py`，经同一页面 Runtime 在临时合成文件上重跑旧四类基准。12/12 自动检查及主任务逐条语义复核通过，临时目录已清理；报告：`local-agent/trial/results/baseline-web-bbf0143ea8fd44af9b9f0b0a2c7de917/`。
- 技术验证已通过此组样例；用户真实页面体验仍待反馈，AI 复核不冒充用户或工程师签字。尚未实施目录发现或宣称完整 M1 达成。

## 用户后续授权（2026-09-08）

用户要求暂不细化交互，优先验证信息输入、模型回答准确性及工具结果回填下一轮上下文，并推进目录发现。因此额外主观反馈不再阻挡下一批开发。单文件的真实验证记录保持原值；新增范围见 `2026-09-08-local-agent-directory-discovery.md`，完整 M1 的流式输出另行跟踪。
