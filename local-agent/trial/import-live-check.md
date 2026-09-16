# 受管本地文档导入真实模型验收记录

## 结论

2026-09-16，固定合成资料在 DeepSeek `deepseek-v4-flash` 上完成 1 个持久 Session、2 个 Run、8 次模型请求。两个 Run 均为 `completed / ANSWER_VALIDATED`；AI 语义 Gate **PASS**。这证明固定场景中的受管导入、搜索、读取、ToolCall 结果回填、原始位置引用、重启追问、Import 隔离及备份恢复闭环，不代表用户签字、任意真实资料质量或完整发布质量。

原始机器报告保留为 `FAILED`，SHA-256 为 `623ecf8f0d304573add0131d0c5fee0a4b66670bd6bef6321de36ba499ce581c`。它唯一失败的是旧 `follow_up_facts`：规则要求追问逐字包含英文标签 `Budget 42`、`Owner Mei`，而真实回答正确表述为“预算 42、负责人 Mei”。评分器现要求 `Budget/预算` 与 `42`、`Owner/负责人` 与 `Mei` 分别绑定，并精确匹配完整标识 `TXT-READY`；值错位、`42.5`、`TXT-READY-old` 与 `TXT-READY.v2` 均不能通过。对原报告只读重放后，22 个必需检查全部为真，原报告没有覆盖，也没有再次调用模型。

本机原始证据位于忽略目录：

- `trial/results/import-evaluation-bdfaa2a6a750463f9b09106be9099c95/report.json`
- `trial/results/import-evaluation-bdfaa2a6a750463f9b09106be9099c95/corrected-score-replay.json`
- `trial/results/import-evaluation-bdfaa2a6a750463f9b09106be9099c95/semantic-review.md`

## 两轮核对

| Run | 自动与语义结果 | 关键证据 |
|---|---|---|
| 首问 | PASS | 正确回答预算 `42` 和负责人 `Mei`；程序引用映射分别为 `资料/budget.pdf` 第 2 页与 `资料/owner.docx` 表 1 行 1；搜索、读取结果按原调用 ID 进入后续模型请求。 |
| 重启恢复后的追问 | PASS | 历史投影包含上一轮结论；模型搜索、读取 `资料/checkpoint.txt`，回答保留“预算 42、负责人 Mei”和 `TXT-READY`，引用对应文本第 1 行。 |

首问取得所需 PDF 与 DOCX 证据后，又尝试读取本题不需要的 checkpoint 文件；Runtime 以 `FILE_COUNT_LIMIT` 拒绝并把错误回填。最终答案只引用成功读取的两份相关文件，因此不阻塞本次事实验收。两轮回答都补充了“未出现冲突”，但各轮读取范围并不完整，这句话超出了已读范围，记录为 **P2 表述限制**；它不改变三个所问事实及引用均正确的结论，后续不为此扩大本批或追加真实调用。

## 结构、安全与恢复

- 初始请求没有预装文档正文；搜索命中与读取正文只在工具调用后进入 Provider。
- 另一 Import 的秘密值未进入 Provider，请求与路径权限保持隔离。
- 外部原件删除后仍能从受管副本读取；备份恢复到新 State Store 后保留 Session、Import 与 Agent 逻辑身份，并重新绑定文件身份。
- PDF 页码、DOCX 表格行、TXT 行号均来自受管副本的来源映射。
- 8 次真实调用未超过固定 12 次上限；本次评分修正只重放已有结果。

## 边界

当前不包含 OCR、自动同步、资料删除、向量检索、新格式或跨设备自动恢复。页面真实使用体验仍由用户以自己的资料试玩判断，固定合成样例通过不等于统计可靠性。
