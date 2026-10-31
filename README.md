<div align="center">

# Debate

### 多个编码 Agent，一份共同验证的交付。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

让你已经在使用的 Codex、Claude Code 等 CLI 轮流实现、交叉审查，
直到它们对同一份代码的完成情况达成一致。

[快速开始](#快速开始) · [架构](#架构) · [工作流程](#工作流程) · [使用指南](docs/guide.md)

</div>

## 为什么做 Debate

AI 写完了代码，不代表任务已经完成。

你可能需要再开一个 Agent 检查边界条件，把意见复制回去，提醒它读取最新代码，重新跑测试，再判断这次是否真的修好了。任务一复杂，开发者就成了多个终端之间的调度员。

**Debate 把这段往返变成可执行的协作流程。** 你提供目标、允许修改的范围和验证命令；不同 Agent 基于真实 Git 变更接力工作。每轮有记录，中断可以恢复，最终结果可以审查。

| 你关心的问题 | Debate 的做法 |
| --- | --- |
| 它是不是遗漏了需求？ | 将目标整理为完成条件，并让每位参与者独立判断 |
| 审查的是不是最新代码？ | 交接绑定当前 Git HEAD，要求读取实际变更 |
| 两个 Agent 会不会互相覆盖？ | 同一时刻只允许一位 Writer 修改代码 |
| “修好了”有什么依据？ | 执行你配置的验证命令，记录结果 |
| 中间断了怎么办？ | 保存检查点与恢复状态，明确停止原因 |
| 最后要合并哪些提交？ | 将代码与精简审计整理为一个聚焦提交 |

适用于有明确完成条件的功能开发、Bug 修复和重构。它增加独立检查的机会，但不保证消除所有缺陷；重要变更仍需要你的审查。

## 架构

![Debate 系统架构：确定性 Supervisor 编排独立 CLI Agent，Git 保存代码变化，SQLite 保存恢复状态。](docs/diagrams/architecture.svg)

**Agent 负责实现和判断，Supervisor 负责协作规则。**

Debate 使用 LangGraph 管理运行流程。每位 Agent 保留自己的原生 Session，共享同一个 Git 工作空间；Supervisor 控制写入租约、验证、检查点和最终集成。

可选的 DeepSeek 协调器建议下一位 Writer 和下一轮任务，不直接修改代码，也不能替参与者宣布完成。Git 和 SQLite 分别保存版本证据与恢复状态。

## 工作流程

![Debate 工作流程：准备目标，轮流实现与审查，最终验证，全员独立确认，交付聚焦提交。](docs/diagrams/run.svg)

1. **定义任务。** 指定目标仓库、参与者、修改范围与验证命令。
2. **接力改进。** Writer 实现，其他 Agent 读取变更并审查；存在未解决问题时继续下一轮。
3. **确认结果。** 最终验证通过，每位 Agent 独立判断目标完成，再确认代码与审计组成的同一份文件树。
4. **交付。** 生成一个聚焦提交，合入目标分支，或保留结果分支供你检查。

超时、停滞、验证失败或目标分支冲突会明确停止，不会被当成成功。Debate 自身不自动推送、发布或部署。

## 快速开始

需要 Python 3.10+、Git，以及至少两个已安装并登录的编码 CLI。推荐 macOS、Linux 或 WSL2。

```bash
git clone https://github.com/navyran/debate.git
cd debate
python -m venv .venv
source .venv/bin/activate
pip install -e '.[ui]'
debate-agent ui --state-root "$PWD/.runtime"
```

打开 **http://127.0.0.1:8502**，填写目标并选择参与者。界面展示当前阶段、Agent 活动、提交接力和验证结果。

也可以直接从命令行开始。下面以已有测试的 Python 仓库为例，请按实际项目替换路径和命令：

```bash
debate-agent run \
  --workspace /absolute/path/to/your-project \
  --topic "修复重复请求造成的重复写入，并补充回归测试" \
  --participant codex \
  --participant claude \
  --worktree \
  --allow-path src \
  --allow-path tests \
  --validation-command-json '["python","-m","pytest","-q"]' \
  --state-root "$PWD/.runtime"
```

`--worktree` 使用独立工作树，目标仓库应处于预期分支且工作区干净。默认参与者为 Codex + Claude Code；只会检查你选择的 CLI。

## 选择你的 Agent

| Agent | 配置要点 |
| --- | --- |
| Codex CLI | 复用原生 Session，使用 workspace-write sandbox |
| Claude Code CLI | 本机已认证 CLI，支持结构化流和会话续接 |
| Qoder CLI | headless 调用和固定 Session |
| OpenCode CLI | 指定 `provider/model`，可用 `--opencode-variant` 选择 Variant |
| OpenClaw | 通过 Gateway 连接原生 Agent/Profile |
| Hermes（实验性） | 显式指定 Provider 和 Model |
| CodeBuddy（实验性） | 可用 `--codebuddy-native-profile`、`--codebuddy-model` 覆盖配置 |

每次至少选择两位。完整参数见 [CLI 参考](docs/cli-reference.md)。

### 可选的 DeepSeek 协调

使用已有编码 CLI 不要求额外配置 DeepSeek Key。需要启用语义协调时，可通过环境变量提供：

```bash
export DEEPSEEK_API_KEY='your-key'
```

也可保存在 `~/.config/debate-agent/secrets.env`，将文件权限设置为 `600`。未配置时协调器使用确定性回退；`--no-facilitator` 可显式关闭。

## 观察、恢复与交付

```bash
debate-agent status --run-id RUN-... --state-root "$PWD/.runtime"
debate-agent resume --run-id RUN-... --state-root "$PWD/.runtime"
debate-agent abort  --run-id RUN-... --state-root "$PWD/.runtime"
```

UI 提供各 Agent 的实时输出、运行状态与历史记录。调用按启动等待、活动空闲和总时长分别限时，避免一直等待无进展的进程。完成后的精简审计随代码提交，包含目标、变更摘要与验证报告；详细运行数据留在本地。

## 安全使用

只在可信项目中运行。Agent 会使用本机 CLI 的权限和认证，默认可信模式允许广泛命令及联网；worktree 不隔离凭据或外部服务。可用 `--no-unrestricted-commands` 收紧命令范围，需要更强隔离时使用容器或操作系统沙箱。

不要上传 `.runtime`、环境文件、私钥或原始会话。漏洞反馈与发布注意事项见 [SECURITY.md](SECURITY.md)。

## 文档与贡献

- [使用指南](docs/guide.md)：安装、配置、运行与恢复。
- [CLI 参考](docs/cli-reference.md)：适配器参数和权限差异。
- [CodeBuddy](docs/codebuddy.md)：实验性适配器配置。
- [测试指南](docs/testing.md)：开发验证与可选真实 CLI 测试。

```bash
pip install -e '.[dev,ui]'
python -m pytest -q
ruff check src tests
mypy src
python -m build
```

欢迎通过 Issue 提供脱敏复现，或提交带测试的 Pull Request。真实 CLI 测试可能产生模型调用费用。

## License

[MIT](LICENSE) © 2026 navyran
