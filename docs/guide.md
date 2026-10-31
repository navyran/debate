# 使用指南

## 安装与准备

安装 Python 3.10+、Git，至少两个已认证的编码 CLI。推荐 macOS、Linux 或 WSL2。

```bash
git clone https://github.com/navyran/debate.git
cd debate
python -m venv .venv
source .venv/bin/activate
pip install -e '.[ui]'
debate-agent --help
```

已有目标仓库应位于预期分支、工作区干净，并设置有效 Git author。
没有 Git 的目标目录会初始化并创建基线，首次使用前请检查目录中是否有不应提交的文件。

## 使用本地 UI

```bash
debate-agent ui --state-root "$PWD/.runtime"
```

打开 http://127.0.0.1:8502，填写工作目录与目标，选择至少两位参与者。
建议同时填写允许修改路径和可重复执行的验证命令；可在高级设置开启独立 worktree。
启动前选择合入目标分支或保留独立结果分支。界面显示运行阶段、Agent 活动、HEAD、
验证状态、审查结果和停止原因。

UI 固定监听本机。后台 Worker 执行任务，关闭浏览器页面不等同于终止 Worker。

## 使用 CLI

```bash
debate-agent run \
  --workspace /absolute/path/to/project \
  --topic-file requirements.md \
  --participant codex \
  --participant claude \
  --worktree \
  --allow-path src \
  --allow-path tests \
  --validation-command-json '["python","-m","pytest","-q"]' \
  --state-root "$PWD/.runtime"
```

需求文件应说明预期行为、交付范围、完成条件和不应改变的约束。
验证命令使用 JSON argv，不经过 shell 拼接，且必须适用于目标项目的运行环境。
默认在原目录的临时分支执行，`--worktree` 创建独立目录。

常用参数：

| 参数 | 用途 |
| --- | --- |
| `--participant` | 可重复指定参与者，默认 Codex + Claude |
| `--first-agent` | 指定首位 Writer，必须属于参与者 |
| `--target-branch` | 指定最终目标分支，默认当前分支 |
| `--allow-path` / `--protect-path` | 允许与保护路径 |
| `--setup-command-json` | 配置环境准备命令 |
| `--validation-command-json` | 配置权威验证命令 |
| `--max-turns` | 写入回合预算，默认 8 |
| `--total-timeout-seconds` | Run 总时限，默认 7200 秒 |
| `--no-facilitator` | 关闭语义协调 |
| `--no-unrestricted-commands` | 收紧为严格命令策略 |

完整选项以 `debate-agent run --help` 为准，各 CLI 特性见 [CLI 参考](cli-reference.md)。

## Agent 与认证

Debate 复用本机 CLI 的登录状态。只检查所选参与者；OpenClaw 需要 Gateway，
Hermes 需要显式 Provider/Model，OpenCode 使用单个 `provider/model`。
CodeBuddy 的配置见 [适配器说明](codebuddy.md)。

可选 DeepSeek Key 使用环境变量 `DEEPSEEK_API_KEY`，或保存在
`~/.config/debate-agent/secrets.env`。目录权限设置为 700，文件设置为 600。
密钥按适配器需求传递，不要存入仓库或需求文件。

## 验证与完成

健康预检在开始修改前检查 CLI、认证和模型能力。startup、idle、hard 默认分别为
300、300、3600 秒；有效活动刷新 idle，心跳不会。MCP 默认为 minimal。

每轮写入后执行策略检查与验证，再将检查点交给其他参与者审查。
最终验证通过后，所有参与者需要独立确认完整目标，再确认同一个包含代码和审计的
文件树。验证没有配置时不应把“验证通过”理解为已经测试了项目行为。

最终代码与精简审计形成一个聚焦提交。合入目标时先核对分支与工作区，漂移或竞态
导致停止；选择保留结果则不改目标分支。最终提交由用户自行检查和推送。

## 状态与恢复

```bash
debate-agent status  --run-id RUN-... --state-root "$PWD/.runtime"
debate-agent resume  --run-id RUN-... --state-root "$PWD/.runtime"
debate-agent abort   --run-id RUN-... --state-root "$PWD/.runtime"
debate-agent cleanup --run-id RUN-... --state-root "$PWD/.runtime"
```

`resume` 从最新 SQLite 检查点继续，前提是当前现场可以安全归属。
对于状态不明的调用，不会盲目重放。`abort` 请求中止并保留恢复材料；
`cleanup` 只处理明确指定的非运行中 Run。

| 停止原因 | 排查方向 |
| --- | --- |
| `preflight_blocked` | CLI、认证、模型能力和仓库条件 |
| `policy_violation` | 修改范围、受保护文件或命令策略 |
| `workspace_race` | 其他进程是否修改了工作区 |
| `stalled` / `max_turns` | 需求是否过宽、问题是否在重复循环 |
| `integration_blocked` | 目标分支漂移、源工作区变化或文件树不一致 |
| `recovery_blocked` | 中断现场是否能由 journal 与当前 HEAD 唯一解释 |

失败现场与原始输出仅用于本机排障，不要作为公共 Issue 附件直接上传。
更多保护边界见 [安全说明](../SECURITY.md)。
