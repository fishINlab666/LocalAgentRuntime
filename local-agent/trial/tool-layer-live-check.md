# 工具层真实模型验证记录

日期：2026-09-10

Provider：DeepSeek `deepseek-v4-flash`，`simulated: false`
工作区：`trial/workspace/tool-layer/`（两份合成小文件）

## 结论

本批限定的三个真实样例通过。DeepSeek 自主发出工具调用，程序实际执行并按原调用 ID 回填；拒绝不会写文件，确认后的报告与冻结预览一致，实际回执进入下一轮 context。三个 run 共调用模型 10 次，低于预定 18 次上限，因此按固定停止条件收口，不增加样例或重跑旧 12 次目录基准。

## 三个真实 run

| 场景 | run ID | 结果 | 模型调用 | 工具结果 | 核对 |
|---|---|---|---:|---:|---|
| 单文件问答 | `d966af1104e74d08bdaeb8487b28e208` | `completed / ANSWER_VALIDATED` | 2 | 1 | 模型读取 `plan.md`，回答“青禾-47”，引用第 3 行准确 |
| 目录报告并拒绝 | `353193416b604b519e1464ff749fc6df` | `unable / USER_REJECTED` | 4 | 4 | 读取两份文件；页面展示完整报告后拒绝；`denied-real-report.md` 不存在；模型只收尾一次 |
| 目录报告并确认 | `42c8b0d6bf2b4f4dac488964313689f9` | `completed / ANSWER_VALIDATED` | 4 | 4 | 读取两份文件；确认 957 字节预览；文件创建成功；最终答案引用两份原文 |

三个 trace 均显示真实 Provider，所有已完成工具结果与原调用 ID 对应，并出现在下一次模型请求中；默认 trace 未出现 `青禾-47`、`林澄` 或报告正文。

## 产物核对

- 实际文件：[confirmed-real-report.md](workspace/tool-layer/confirmed-real-report.md)
- 字节数：`957`
- SHA-256：`031d804a19f42b8a544e956d26b251a9d1016c6ad0743268361567b4a96f472c`
- 操作：`created`
- 页面确认前目标不存在；确认后页面显示相同路径、字节数和实际执行回执。
- 页面预览全文与落盘报告逐项核对一致；项目代号、评审人、日期及来源行号与 `plan.md`、`review.md` 一致。
- 拒绝目标不存在；目录内没有 `.local-agent-*.tmp` 临时残片。

本记录是固定合成样例的真实模型验证，不代表统计可靠性或真实用户收益已经测量。会话管理仍是下一批，本批不继续做界面细化、更多工具或专名加固。
