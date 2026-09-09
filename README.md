# LocalAgentRuntime

一个用于验证最小 Agent 闭环的本地 Python 项目：模型自主选择工具，Runtime 执行受限文件操作，将实际结果回填下一轮上下文，最后返回带原文引用的答案。

## 已有能力

- 单文件问答：模型通过 `read_file` 读取指定文本。
- 受限目录发现：模型通过 `list_files` 发现文件，再选择读取并结合多份证据回答。
- 本地网页与命令行入口，共用同一运行循环。
- 工具调用关联、引用校验、权限边界、超时、取消及运行记录。
- DeepSeek 接入，以及不需要密钥的模拟演示。

最小闭环和目录发现固定样例已有真实验证记录。专名连接符修复保留在代码中，本地检查已完成，新版本真实复测仍后置。固定样例通过不代表所有回答准确或完整 M1 已完成。详细状态见 [阶段记录](local-agent/trial/directory-check.md)。

## 快速运行

使用 Python 3.11+，产品代码仅依赖标准库。当前实现使用 POSIX 文件操作；已在 macOS 验证，Windows 不支持本版文件打开方式，Linux 尚未实测。

在仓库根目录进入应用目录，运行不调用模型 API 的演示：

```sh
cd local-agent
python3 -m local_agent demo
```

启动模拟页面：

```sh
python3 -m local_agent serve --workspace examples/workspace --demo --open
```

真实问答需要在同一终端通过环境变量提供 `DEEPSEEK_API_KEY`，程序不会自动读取 `.env`。配置后启动：

```sh
python3 -m local_agent serve --workspace examples/workspace --open
```

默认页面为 `http://127.0.0.1:8765`。真实模式会把问题和实际读取的内容发送给 DeepSeek。完整使用方式、目录模式及环境变量说明见 [应用 README](local-agent/README.md)。

## 验证与文档

在 `local-agent/` 中执行不需要 API Key 的 Python 回归：

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests
```

- [项目开发规则与范围失控复盘](AGENTS.md)
- [产品设计方案](个人本地智能助手-Agent项目设计方案.md)
- [学习手册](Local-Agent-Runtime-小白入门与开发防跑偏手册.html)
- [最小切片设计](docs/superpowers/specs/2026-09-07-local-agent-first-slice-design.md)
- [实施计划](docs/superpowers/plans/)

仓库保留代码、测试、合成示例及设计和验收摘要。原始会议文件、真实材料试用节选、运行日志、详细本地评测产物和凭据未纳入 Git；文档中指向这些材料及其他本地项目的链接只在原工作区可用。历史机器结果与 AI 复核保留各自含义，不作为用户验收签字。
