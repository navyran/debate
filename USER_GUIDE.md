# 辩论 Agent 使用与初始化手册

本文面向第一次接触 `debate-agent` 的使用者。你可以从一台全新环境开始，完成
Codex CLI、Claude Code CLI、Qoder CLI、OpenCode CLI、OpenClaw、Hermes、CodeBuddy、
DeepSeek Facilitator 和本地 Web UI 的
初始化，并让每次选中的多个 CLI Agent 在同一个 Git 工作空间内串行修改、互相审查
和共同确认结果。

## 1. 它解决什么问题

`debate-agent` 使用 LangGraph 编排可选择的独立编码 Agent：

- Codex CLI 负责实现、审查或补充修改；
- Claude Code CLI 负责实现、审查或补充修改；
- Qoder CLI 负责实现、审查或补充修改，默认以 `qodercli --yolo` 启动；
- OpenCode CLI 负责实现、审查或补充修改；只在被选择后预检和启动，默认不参与；
- OpenClaw 通过本机 Gateway 和显式 Session Key 参与，默认不参与；
- Hermes 通过显式 Provider/Model 参与，首版标记为实验性，默认不参与；
- CodeBuddy 通过 headless JSON/Schema 协议参与，标记为实验性，默认不参与；
- Supervisor 控制唯一写租约、Git 检查点、验证、恢复和最终交付；
- 可选的 DeepSeek Facilitator 只提供下一步语义建议，不替所有已选 Agent 判断业务方案；
- 只有所有选中的 Agent 都认为目标已经完成，并且最终验证与文件树确认都通过，
  Run 才能结束为 `completed`。

各 CLI 都保留同一次 Run 内的原生 Session 记忆。每次写入后，Supervisor 会生成
Git 检查点，并向其他参与者扇出结构化交接摘要；任一 Agent 再次行动前必须读取当前
HEAD、此前提交和工作区变化，不能只依赖自己的历史对话。

### 1.1 名称迁移

当前正式名称是 `debate-agent`，Python 包名为 `debate_agent`，项目目录为
`agent/debate`。旧版环境可以通过 `agent/discuss` 符号链接和
`discuss-agent` 兼容命令继续启动；已有
`~/.config/discuss-agent/secrets.env` 与 `.agent/discuss/<run-id>` 也可继续读取。
所有新配置、文档和审计路径应使用新名称。

```text
用户目标
  -> Supervisor 建立 GoalContract 和唯一写租约
  -> 选中的 CLI Agent 交替写入、提交、读取其他参与者变化并审查
  -> 最终验证
  -> 所有参与者独立 complete Ballot
  -> 产物与精简审计组成固定 final tree
  -> 所有参与者 TreeAck
  -> 合并目标分支，或保留独立结果分支
```

## 2. 前置条件

### 2.1 操作系统

推荐 macOS、Linux 或 Windows 上的 WSL2。项目使用 POSIX 进程组来中止 CLI
子进程，原生 Windows 不是当前主要支持环境。

### 2.2 必需软件

| 软件 | 要求 | 用途 |
| --- | --- | --- |
| Python | 3.10 或更高 | 运行 LangGraph、Streamlit 和 Supervisor |
| Git | 建议 2.28 或更高 | 使用 `git init -b`、临时分支、worktree 和检查点 |
| Codex CLI | 按需安装并登录 | 可选 CLI Agent |
| Claude Code CLI | 按需安装并登录 | 可选 CLI Agent |
| Qoder CLI | 按需安装并登录 | 可选 CLI Agent，命令为 `qodercli` |
| OpenCode CLI | 按需安装并登录 | 可选 CLI Agent，命令为 `opencode` |
| OpenClaw | 按需安装并启动 Gateway | 可选 CLI Agent，命令为 `openclaw` |
| Hermes | 按需安装并配置 Provider | 实验性 CLI Agent，命令为 `hermes` |
| CodeBuddy | 按需安装并登录 | 实验性 CLI Agent，命令为 `codebuddy` |
| 网络 | 能访问已选 CLI 的模型服务 | 参与 Agent 执行任务 |
| DeepSeek API Key | 可选 | Facilitator 语义路由；缺失时自动降级 |

还需要满足：

- 目标工作空间对当前用户可读写；
- 已有 Git 仓库必须处于命名分支，且工作树干净；
- Git author 可用，以便 Supervisor 创建检查点提交；
- 同一个仓库和目标分支同一时刻只运行一个 Debate Run；
- 默认“完全授权执行命令”只应对可信工作空间开启。

## 3. 全新环境初始化

下面以项目代码已经位于 `/path/to/Py36Project` 为例。

### 3.1 安装基础软件

```bash
python3 --version
git --version
git config --global user.name "你的名字"
git config --global user.email "你的邮箱"
```

### 3.2 安装并登录 Codex CLI

安装方式以 [OpenAI Codex CLI 官方文档](https://developers.openai.com/codex/cli/)
为准。使用 npm 的常见安装命令为：

```bash
npm install -g @openai/codex
codex --version
codex login
codex login status
```

也可以让 Codex 从标准输入读取 API Key，避免把 Key 放入命令参数：

```bash
printenv OPENAI_API_KEY | codex login --with-api-key
```

完成后单独运行一次 `codex`，确认账号、模型和本机配置可用。

### 3.3 安装并登录 Claude Code CLI

安装方式以 [Claude Code 官方安装文档](https://docs.anthropic.com/en/docs/claude-code/setup)
为准。macOS/Linux 的官方原生安装器通常可以这样执行：

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
claude auth login
claude auth status
claude doctor
```

完成后单独运行一次 `claude`，确认账号和模型服务正常。

### 3.3A 安装并登录 Qoder CLI

安装后确认 `qodercli` 可执行并已登录：

```bash
qodercli --version
qodercli login
qodercli status
```

讨论任务中 Qoder 默认使用 `qodercli --yolo -p`，因此只应选择可信工作空间。

### 3.3B 安装并登录 OpenCode CLI

OpenCode 是外部 CLI，不是本项目的 Python 或 Node 运行依赖；请按其官方发行方式安装，
再完成本机认证。安装后只做不写入检查：

```bash
opencode --version
opencode auth login
opencode auth list --pure
opencode run --help
```

在讨论页面选择 OpenCode 后，填写或确认一个单独的 `provider/model`。可选 Variant 会
映射为 `--variant` 并冻结在本次 Run 中；不要把 Token、配置内容或认证文件复制到页面。

### 3.3C OpenCode MCP 模式

默认 `minimal`：预检会发现 MCP 名称并以受控配置全部禁用；选择 `full` 时保留经过预检
的 MCP 集合。两种模式都强制关闭分享、快照、自动升级和默认插件。若 `auth list --pure`、
模型、MCP 或受控配置检查失败，修复 OpenCode 环境后重新新建 Run；不能用普通恢复绕过
启动预检。

### 3.3D 安装并检查 OpenClaw

按 OpenClaw 官方方式安装并完成本机配置，然后确认 Gateway 正常：

```bash
openclaw --version
openclaw agent --help
openclaw gateway status --json
```

页面选择 OpenClaw 后，可以填写原生 Agent/Profile、Model 和 Thinking。它始终通过
Gateway 调用，不会回退 `--local`，也不会使用 `--deliver` 或外部 Channel。首次调用
创建本次 Run 专用 Session Key，后续持续复用；Gateway 超时或连接中断后，系统先读取
Session 状态，只有证明未接收时才重试。

### 3.3E 安装并检查 Hermes

按 Hermes Agent 官方方式安装。当前已验证版本为 v0.19.0，其 `chat` 没有调用级
Profile 参数，因此页面只要求 Provider 与 Model：

```bash
hermes --version
hermes status
hermes chat --help
```

当前推荐填写 Provider `deepseek`、Model `deepseek-v4-flash`。debate-agent 会把默认
secrets 文件中的 `DEEPSEEK_API_KEY` 仅注入 Hermes 子进程。健康检查必须执行一次真实
Query；`hermes status` 成功不代表认证可用。Hermes 使用 `--yolo --no-restore-cwd`，
不会启用其 `--worktree`。由于 Prompt 通过 `--query` 传递，同机进程查看者可能短暂
看到 argv；审计会替换为 `<redacted>`，UTF-8 超过 65,536 字节则启动前拒绝。

### 3.3F 安装并检查 CodeBuddy

按 [CodeBuddy CLI 官方文档](https://www.codebuddy.ai/docs/cli/cli-reference) 安装并完成
一次交互登录。npm 安装方式为：

```bash
npm install -g @tencent-ai/codebuddy-code
codebuddy --version
codebuddy
```

页面选择 CodeBuddy 后，可显式填写 Model 和原生 Agent/Profile；模型留空时会从
`CODEBUDDY_MODEL` 或 `~/.codebuddy/settings.json` 发现。健康检查在临时 Git 仓库中
执行无工具结构化 Query，并用首轮 `session_id` 做第二轮 `--resume`；只有 Session
一致才允许进入目标工作区。默认采用 minimal MCP、固定工具白名单，并关闭自动记忆、
热更新、市场、自动升级和遥测。

使用 API 方式认证时，可按官方文档设置 `CODEBUDDY_API_KEY` 或
`CODEBUDDY_AUTH_TOKEN`；这些变量仅传给 CodeBuddy 子进程并被脱敏，不要粘贴到页面
或提交到仓库。完整边界见 [CodeBuddy 接入说明](CODEBUDDY_INTEGRATION.md)。

### 3.4 创建 Python 虚拟环境

```bash
cd /path/to/Py36Project/agent/debate
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e '.[ui]'
.venv/bin/debate-agent --help
```

如需运行测试、Ruff、Mypy 和构建检查，安装开发依赖：

```bash
.venv/bin/pip install -e '.[dev,ui]'
```

### 3.5 配置 DeepSeek Facilitator

DeepSeek Key 是可选项。不配置时 Run 不会中断，但 Facilitator 会记录
`missing_api_key` 并使用确定性路由。

```bash
install -d -m 700 ~/.config/debate-agent
touch ~/.config/debate-agent/secrets.env
chmod 600 ~/.config/debate-agent/secrets.env
```

编辑 `~/.config/debate-agent/secrets.env`，写入：

```bash
export DEEPSEEK_API_KEY="替换为真实 Key"
```

也支持不带 `export` 的 `DEEPSEEK_API_KEY="..."`。推荐保留 `export`，这样该文件
仍兼容手工 `source`；正常启动不需要执行 `source`。`debate-agent` 会安全解析固定
文件，只读取该 Key，不执行其中的 shell 命令。显式设置的环境变量优先于文件值。

验证时不要打印 Key 本身：

```bash
.venv/bin/python -c "import os; from debate_agent.secrets import load_default_deepseek_key as load; load(); print('DeepSeek Key 已加载' if os.environ.get('DEEPSEEK_API_KEY') else 'DeepSeek Key 未配置')"
```

### 3.6 执行初始化自检

```bash
cd /path/to/Py36Project/agent/debate

command -v git
command -v codex
command -v claude
command -v openclaw
command -v hermes
codex login status
claude auth status
.venv/bin/python -c "import debate_agent, langgraph, streamlit; print('Python 依赖正常')"
```

确认所有计划参与的 CLI 均可执行、已认证并显示 `Python 依赖正常`，说明环境已经
就绪；默认只需检查 Codex 与 Claude，选择其他 Agent 时检查对应 CLI。

## 4. 准备目标工作空间

### 4.1 使用已有 Git 仓库

```bash
cd /path/to/target-repo
git branch --show-current
git status --short
```

要求：

- `git branch --show-current` 返回实际分支名；
- `git status --short` 没有输出；
- 不要在 Run 期间手工切换分支、提交或修改工作树；
- 如果目标分支被其他任务更新，最终集成会进入 `integration_blocked`，不会强推。

### 4.2 使用尚未初始化 Git 的目录

普通目录可以直接填入 Web UI。启动 Run 时系统会自动：

1. `git init -b <目标分支>`；
2. 为缺少 author 的新仓库配置本地默认 author；
3. 把现有非保护文件提交为初始基线；
4. 创建本次 Run 的临时 `debate/<run-id>` 分支。

如果目录里已有不应提交的文件，请先建立 `.gitignore`。自动初始化会把未忽略的
现有文件纳入初始基线。

## 5. 启动 Web UI

```bash
cd /path/to/Py36Project/agent/debate
.venv/bin/debate-agent ui
```

该命令会自动加载 `~/.config/debate-agent/secrets.env`。如果 UI 已经在运行，修改
密钥文件后需要重启 UI 才能使用新值。

浏览器打开 `http://127.0.0.1:8502/`。指定端口和状态目录：

```bash
.venv/bin/debate-agent ui \
  --port 8503 \
  --state-root /absolute/path/to/discuss-runtime
```

UI 只监听 `127.0.0.1`，不是面向公网部署的多人服务。

### 5.1 新建讨论页面字段

| 字段 | 建议 | 说明 |
| --- | --- | --- |
| 工作空间 | 填绝对路径 | 页面会实时读取当前 Git 分支 |
| 任务目标 | 写清交付物和完成条件 | 所有已选 Agent 共享的 GoalContract 来源 |
| 参与讨论的 CLI Agent | 至少选择两个 | 默认 Codex + Claude，可加入 Qoder、OpenCode、OpenClaw、Hermes 或 CodeBuddy；只检查已选 CLI |
| 允许修改的路径 | 尽量缩小 | 每行或逗号分隔，`.` 表示整个仓库 |
| 验证命令 | 代码任务应填写 | 每行一条命令，不经过 shell 拼接 |
| DeepSeek Facilitator | 默认开启 | 无 Key 时自动降级，不阻断 |
| 完成后合并回目标分支 | 按需求选择 | 关闭后保留结果分支供继续修改 |
| 使用独立 Git worktree | 复杂任务建议开启 | 避免原目录停留在 debate 分支 |
| 目标分支覆盖 | 通常保留自动值 | 必须与目标仓库当前分支一致 |
| 首位 Writer | 任意 | 只决定谁先写，不影响结束门禁 |
| 最大写入回合 | 默认 8 | 达到后仍未完成会停止为 `max_turns` |
| 总超时 | 默认 120 分钟 | Run 总预算，任何单次调用都不能突破剩余时间 |
| 各 CLI 路径 | 自动发现绝对路径 | 只预检并冻结选中的 Agent |
| 各 CLI 模型 | 自动发现或显式填写 | Qoder 留空时使用 CLI 默认模型；OpenCode 必须为 `provider/model` |
| OpenCode Variant | 可留空 | 仅 OpenCode 卡显示，传入 `--variant` 并在预检后冻结 |
| OpenClaw Agent / Thinking | 按本机配置 | 分别传入 `--agent` 与 `--thinking`，并冻结 Gateway 配置摘要 |
| Hermes Provider / Model | 必填 | Hermes 卡显示实验性提示，当前推荐 `deepseek` / `deepseek-v4-flash` |
| CodeBuddy Agent / Model | 可留空 | CodeBuddy 卡显示实验性提示；可冻结原生 Agent 和模型 |
| 启动/空闲/硬上限 | 300/300/3600 秒 | 分别限制首次活动、工作后静默和单次墙钟 |
| 基础设施失败重试 | 默认 2 | 只重试工作区干净且可安全重放的故障 |
| 显示 CLI 实时输出 | 默认开启 | 每两秒增量展示所有已选 CLI 的受限事件 |
| raw 尾部行数 | 默认 500 | 只在折叠区读取脱敏后的最后 N 行 |
| MCP 模式 | 默认 `minimal` | `full` 才读取用户机器上的可选 MCP |
| 代理/证书变量名 | 默认不传 | 只选择变量名，页面和配置不显示值 |
| 环境准备命令 | 按需填写 | 例如安装目标项目依赖 |
| 完全授权执行命令 | 仅可信仓库开启 | 默认开启，允许完整 Bash 命令 |

### 5.2 推荐的第一次试运行

第一次运行建议开启独立 worktree、关闭自动合并、只允许修改一个测试文件，并配置
一条可机械判断的验证命令。

任务目标：

```text
创建 hello.txt，写一段不超过 100 字的中文自我介绍。文件必须非空；不要修改其他文件。
```

允许路径：

```text
hello.txt
```

验证命令：

```text
python -c "from pathlib import Path; p=Path('hello.txt'); raise SystemExit(0 if p.exists() and p.read_text().strip() else 1)"
```

点击“检查配置”，再点击“开始讨论”。页面会每两秒刷新阶段、当前 Writer、HEAD，
并在“Codex 实时”“Claude 实时”中分别展示 CLI 工具、文件、消息和心跳事件。
“原始输出”默认折叠，只显示脱敏尾部。完成后会显示结果分支、结果 worktree、最终聚焦提交和
`completed` 状态；目标分支保持不变，可以进入结果 worktree 继续修改。

## 6. 两种工作区模式

### 6.1 默认直接工作区模式

不开启 worktree 时，系统在原工作目录切换到 `debate/<run-id>` 临时分支。

- 选择最终合并：Supervisor 切回目标分支、快进聚焦提交并删除临时分支；
- 选择保留结果：原工作目录停留在 `debate/<run-id>`，方便继续修改；
- Run 期间不要在该目录手工切换分支或写文件。

### 6.2 独立 worktree 模式

开启后，目标仓库保持在原分支，Agent 在下面的独立目录工作：

```text
<state-root>/worktrees/<run-id>/workspace
```

- 选择最终合并：成功后删除结果 worktree 和临时分支；
- 选择保留结果：worktree 和 `debate/<run-id>` 都保留；
- 适合希望随时查看原仓库，或者完成后继续打磨候选结果的任务。

## 7. 两种完成交付方式

### 7.1 合并回目标分支

开启“完成后合并回目标分支”时，Supervisor 会把已由所有已选 Agent 确认的 final tree 创建为
一个以原始 base 为唯一 parent 的聚焦提交，然后安全快进目标分支。

目标分支只保留最终结果，不保留每个 Agent 的零散中间提交。系统不会自动 push。

### 7.2 保留独立结果分支

关闭该开关时：

- 目标分支 HEAD 不变；
- 产物和审计仍组成同一个最终聚焦提交；
- 聚焦提交固定到 `debate/<run-id>`；
- Supervisor 释放仓库锁；
- 结果分支或独立 worktree 保留，供后续继续修改；
- raw 输出、调用 journal 和 Session 等敏感运行材料仍会清理。

目前“是否合并”开关由 Streamlit UI 提供。命令行 `run` 没有对应开关，CLI 启动
默认按 `integrate_to_target=true` 执行。

## 8. 命令行使用

不使用 Web UI 时，可以直接启动同步 Run：

```bash
cd /path/to/Py36Project/agent/debate

.venv/bin/debate-agent run \
  --workspace /absolute/path/to/target-repo \
  --topic "实现目标并补充测试" \
  --allow-path src \
  --allow-path tests \
  --validation-command-json '["python","-m","pytest","-q"]'
```

也可以使用目标文件：

```bash
.venv/bin/debate-agent run \
  --workspace /absolute/path/to/target-repo \
  --topic-file /absolute/path/to/requirements.md
```

常用参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--target-branch` | 当前分支 | 覆盖自动检测的目标分支 |
| `--worktree` | 关闭 | 使用独立 worktree |
| `--participant` | `codex`、`claude` | 可重复指定，至少两个，可加入 `qoder`、`opencode`、`openclaw`、`hermes` 或 `codebuddy` |
| `--first-agent` | `codex` | 首位 Writer |
| `--max-turns` | `8` | 最大写入回合 |
| `--total-timeout-seconds` | `7200` | Run 总时限 |
| `--max-process-failures` | `2` | 可安全重试的基础设施失败预算 |
| `--format-retries` | `1` | 结构化格式最多重试一次 |
| `--stall-turns` | `2` | 无有效进展的停滞预算 |
| `--codex-model` | 未指定 | 覆盖 Codex CLI 默认模型 |
| `--claude-model` | 未指定 | 覆盖 Claude Code 默认模型 |
| `--qoder-model` | 未指定 | 覆盖 Qoder 模型；留空使用 CLI 默认模型 |
| `--opencode-model` | 未指定 | OpenCode 显式 `provider/model`；预检不接受缺失或多模型值 |
| `--opencode-variant` | 未指定 | 仅 OpenCode 接受；冻结后传给 `opencode run --variant` |
| `--openclaw-native-profile` | 未指定 | OpenClaw 原生 Agent/Profile，传给 `--agent` |
| `--openclaw-variant` | 未指定 | OpenClaw Thinking，传给 `--thinking` |
| `--hermes-provider` | 未指定 | Hermes 必填 Provider |
| `--hermes-model` | 未指定 | Hermes 必填 Model |
| `--codebuddy-native-profile` | 未指定 | CodeBuddy 原生 Agent/Profile，传给 `--agent` |
| `--codebuddy-model` | 未指定 | CodeBuddy 模型；留空时从本机设置发现并冻结 |
| `--*-startup-timeout-seconds` | `300` | 首个有效活动前的时限 |
| `--*-idle-timeout-seconds` | `300` | 两次有效活动之间的静默时限 |
| `--*-hard-timeout-seconds` | `3600` | 单次 CLI 不可突破的墙钟上限 |
| `--*-heartbeat-interval-seconds` | `5` | 页面快照间隔，心跳不延长 idle |
| `--mcp-mode` | `minimal` | 可选 `full` 读取本机 MCP |
| `--proxy-env` | 未指定 | 可重复选择代理/证书环境变量名 |
| `--realtime-output` | 开启 | 写入调用级脱敏实时事件 |
| `--raw-output-tail-lines` | `500` | 页面 raw 脱敏尾部行数 |
| `--no-facilitator` | Facilitator 开启 | 禁用 DeepSeek，使用确定性路由 |
| `--unrestricted-commands` | 开启 | 可信模式，不限制命令白名单 |
| `--no-unrestricted-commands` | - | 启用严格命令白名单 |
| `--allow-network` | 关闭 | 仅影响严格模式下的已批准联网命令 |
| `--codex-resume-sandbox-verified` | 开启 | 同一 Run 复用 Codex Session |
| `--state-root` | `agent/debate/.runtime` | 运行状态目录 |

命令文本必须使用 JSON argv，例如：

```bash
--setup-command-json '["npm","install"]'
--validation-command-json '["npm","test","--","--runInBand"]'
```

不要传入 `"sh -c ..."` 形式的单一字符串来模拟 argv。

## 9. 运行期间会发生什么

1. **CLI health**：在仓库副作用前核验绝对路径、版本、显式模型、参数、登录、只读模型探针、MCP 和熔断。
2. **Preflight**：检查 Git、目标分支、干净状态和运行配置。
3. **Workspace**：取得仓库锁，创建 `debate/<run-id>` 与可选 worktree。
4. **GoalContract**：冻结目标、允许路径和验证命令。
5. **Writer**：只有一个 Agent 获得写租约并修改工作区。
6. **Checkpoint**：Supervisor 检查范围、执行验证并以真实 Agent 名义创建提交。
7. **Handoff**：生成变化摘要，其他已选 Agent 分别读取 Git 全文并返回绑定当前 HEAD 的 ChangeAck。
8. **Review**：按当前 participants 路由的已选 Agent 判断接受、修订或阻断，并可成为下一 Writer。
9. **Final validation**：运行权威最终验证。
10. **Ballot**：所有已选 Agent 相互不可见地独立判断是否完成。
11. **Audit**：把结果摘要、决策和验证报告写入 final tree。
12. **TreeAck**：所有已选 Agent 只读确认同一个最终 tree。
13. **Delivery**：按启动配置集成目标分支或保留结果分支。

Supervisor 只维护执行秩序和机械门禁，不替所有已选 Agent 裁决方案优劣。结束要求不是
“部分参与者说法一致”这么简单，而是所有已选 Agent 都明确判断 GoalContract 已完成、最终验证通过并确认同一 final tree。默认 Codex + Claude 仅是默认组合；选择 Qoder、OpenCode、OpenClaw、Hermes 或 CodeBuddy 时同一门槛适用于它们。

## 10. 状态、恢复、中止与清理

### 10.1 查询状态

```bash
.venv/bin/debate-agent status \
  --run-id RUN-... \
  --state-root /absolute/path/to/state-root
```

Web UI 的“运行监控”和“历史运行”也会展示同一状态。

### 10.2 恢复运行

```bash
.venv/bin/debate-agent resume \
  --run-id RUN-... \
  --state-root /absolute/path/to/state-root
```

恢复只从该 Run 的最新 SQLite checkpoint 继续，不重新开始辩论。若中断时 Writer
已经写入，只有 active 租约、调用 journal 和当前 HEAD 能唯一证明变更来源时，
Supervisor 才会接纳脏改动并补建 recovered checkpoint。

### 10.3 中止运行

```bash
.venv/bin/debate-agent abort \
  --run-id RUN-... \
  --state-root /absolute/path/to/state-root
```

中止会终止当前 CLI 进程组并保留恢复现场，不会自动删除结果分支。

### 10.4 清理现场

```bash
.venv/bin/debate-agent cleanup \
  --run-id RUN-... \
  --state-root /absolute/path/to/state-root
```

运行中不能清理。对于“保留结果”的已完成 Run，清理会删除保留的 worktree 和
`debate/<run-id>` 分支；确认不再需要候选结果后再执行。

### 10.5 常见状态

| 状态 | 含义 | 建议动作 |
| --- | --- | --- |
| `running` | Worker 正在执行 | 等待或查看进度 |
| `completed` | 双完成判断、验证、TreeAck 和交付成功 | 检查最终提交并按需 push |
| `preflight_blocked` | 基线环境或初始验证失败 | 修复依赖/命令后恢复 |
| `blocked` | 普通验证阻断 | 查看验证报告后恢复 |
| `policy_violation` | 命令或修改范围违规 | 调整范围或配置，勿强行接纳 |
| `workspace_race` | 租约外或验证后的工作区变化 | 停止外部写入，检查来源 |
| `recovery_blocked` | 无法证明脏改动属于当前 Writer | 人工检查现场 |
| `max_turns` | 达到最大写入回合 | 增加回合或收窄目标后重开 |
| `stalled` | 连续没有有效变化 | 检查目标或验证是否不可达 |
| `integration_blocked` | 目标分支漂移或 tree 不一致 | 检查分支，从新基线重开 |
| `failed` | CLI、格式、文件或未知错误 | 查看日志和诊断 |
| `aborted` | 用户主动中止 | 可恢复或清理 |

## 11. 结果与审计

完成时，产物和审计位于同一个 Git tree：

```text
.agent/debate/<run-id>/
├── goal-contract.md
├── final-summary.md
├── change-summary.md
├── decisions.jsonl
├── facilitator-decisions.jsonl
└── validation-report.md
```

- `goal-contract.md`：目标、范围和验收要求；
- `final-summary.md`：最终行为、贡献、限制和所有已选 Agent 的完成判断；
- `change-summary.md`：各 Writer 检查点摘要；
- `decisions.jsonl`：Writer、Reviewer、Ballot 等结构化决定；
- `facilitator-decisions.jsonl`：DeepSeek 决定或 fallback，不含完整推理；
- `validation-report.md`：baseline、checkpoint 和 final 验证结果。

最终提交 message 包含：

```text
Debate-Run: RUN-...
Debate-Agents: codex, claude
Goal-Contract: v1
Final-Tree: <tree-sha>
Final-Tree-Ack: codex, claude
Validation-Status: passed
Audit-Path: .agent/debate/RUN-...
```

系统不会自动 push、发布或部署。

## 12. 安全边界

### 12.1 完全授权的真实含义

`unrestricted_commands=true` 会移除编排层命令白名单，Claude 可以使用完整 Bash，
Agent 可以执行 `sh`、Python、包管理器和联网命令。

它不会移除：

- 唯一 Writer 租约；
- Codex 的 `workspace-write` sandbox；
- GoalContract 修改范围；
- `.git`、`.agent/debate`、`.env` 和密钥文件保护；
- Git 检查点、验证和工作区指纹检查。

Claude 与 Supervisor 的 Bash 没有 Codex 同等级别的 OS sandbox。不要对不可信代码
目录开启完全授权；需要更强隔离时，把整个工具运行在容器或隔离虚拟机中。

### 12.2 密钥边界

- DeepSeek Key 供 Facilitator 使用；显式选择 Hermes 时也只注入 Hermes 子进程；
- Key 不写入 Run 的冻结 `config.json`；
- 默认环境白名单不会把 `DEEPSEEK_API_KEY` 传给 Codex、Claude、Qoder、OpenCode
  或 OpenClaw；只有显式选择 Hermes 时，才把该 Key 注入 Hermes 子进程；
- CodeBuddy 认证变量只注入 CodeBuddy 子进程；Key、Token 和自定义 Header 会脱敏，
  不写入公开事件、冻结配置或审计；
- 不要把 `secrets.env` 放入目标仓库；
- 不要提交 `.runtime/runs/<run-id>/raw` 或 Session 文件。

## 13. 常见问题排查

### 13.1 Facilitator 显示 `missing_api_key`

```bash
.venv/bin/python -c "import os; from debate_agent.secrets import load_default_deepseek_key as load; load(); print('DeepSeek Key 已加载' if os.environ.get('DEEPSEEK_API_KEY') else 'DeepSeek Key 未配置')"
```

确认文件路径是 `~/.config/debate-agent/secrets.env`，内容使用
`export DEEPSEEK_API_KEY="..."` 或 `DEEPSEEK_API_KEY="..."`。格式错误时启动命令会
报告行号，但不会打印 Key。已运行的 Streamlit 不会重新读取后来修改的文件，需要
重启 UI。

### 13.2 页面找不到 Codex 或 Claude

```bash
command -v codex
command -v claude
codex login status
claude auth status
```

确保从同一个 shell 启动 Streamlit。GUI 应用、IDE 和交互 shell 的 `PATH` 可能不同。
`debate-agent` 启动入口会自动补充包含可执行文件的常见本机目录，包括 ServBay 的
Node `current/bin`；如果 CLI 安装在自定义目录，仍需把该目录加入启动进程的 `PATH`。

### 13.3 `fatal: ambiguous argument 'main'`

新版本会自动检测当前分支，并能对未初始化目录执行 `git init`。如果仍出现：

```bash
git -C /path/to/workspace branch --show-current
git -C /path/to/workspace log -1 --oneline
```

确认页面的“目标分支覆盖”与实际分支一致。未初始化目录应先刷新页面预检，不要手工
填写一个尚不存在且与 Git 默认分支不同的名字。

### 13.4 工作空间不干净

```bash
git -C /path/to/workspace status --short
```

先提交、暂存到其他安全位置或明确处理现有改动。系统不会覆盖用户脏改动。

### 13.5 `CLI 退出码 1`

检查：

```text
<state-root>/runs/<run-id>/status.json
<state-root>/runs/<run-id>/worker.log
<state-root>/runs/<run-id>/diagnostics/
<state-root>/runs/<run-id>/invocations/
<state-root>/runs/<run-id>/raw/
```

失败 Run 会保留这些材料；成功 Run 会清理 raw、invocation journal 和 Session。
不要直接分享未经检查的 raw 输出。

### 13.6 提示“未批准命令: sh”

这是严格白名单模式的行为。在可信工作空间内，可在页面开启“完全授权执行命令”，
或 CLI 使用 `--unrestricted-commands`。不可信仓库不要以此绕过门禁，应使用隔离容器。

### 13.7 恢复时提示脏改动没有 active 写租约

这通常表示 Run 之外的进程修改了工作区，或中断发生在无法安全归属的阶段：

1. 停止 IDE 自动格式化、其他 Agent 和后台脚本；
2. 查看 `git status`、当前 HEAD、lease 和 invocation journal；
3. 不要直接删除不明改动；
4. 能确认来源后再恢复，否则保留现场并人工处理。

### 13.8 `CLI 结构化输出被截断`

Codex 的大体积 JSONL 会从完整落盘日志流式解析。若仍报错，检查 raw 日志是否缺少
最终 `agent_message`/`turn.completed` 或 CLI 是否被超时/信号终止。Claude 新调用
使用 `stream-json`，内存窗口不足时同样从完整 raw 文件查找最终 `result`；如果流在
终局前断开，partial 文本不会被误认成成功。

### 13.8A OpenCode 预检、认证或 Session 问题

OpenCode 的失败通常出现在写入前。先在与页面相同的用户环境中运行 `opencode --version`、
`opencode auth list --pure` 和 `opencode run --help`，确认目标 provider 已认证且当前版本
支持 `--format`、`--dir`、`--model`、`--agent`、`--auto`、`--pure`、`--session`、`--variant`。
不要把 `auth list` 的完整输出、Token、`OPENCODE_CONFIG_CONTENT` 或 raw 日志提交到仓库。

若报模型或认证错误，请明确填写 `--opencode-model provider/model`（或页面同名字段）并
重新开始新 Run；若报 Session 边界错误，系统会拒绝接受不同 `sessionID` 的续接输出，保留
运行现场供检查，而不会把新会话混入旧 Run。MCP 问题先回到 `minimal`；只有任务明确需要
本机 MCP 时才切换 `full` 并重新通过预检。

### 13.8B OpenClaw Gateway、Session 或模糊传输问题

先运行 `openclaw gateway status --json`，确认 `rpc.ok=true` 且 CLI/daemon 配置均存在。
debate-agent 不会以 `--local` 绕过 Gateway，也不会使用 `--deliver`。若页面显示
`ambiguous_transport`，表示 Gateway 可能已经接收任务但调用端失去确定结果；系统会先
查询对应 Session。已完成则恢复结果、仍运行则等待或取消、明确未接收才重试，未知时
保留现场进入可恢复失败。不要手工重复同一 Prompt。

### 13.8C Hermes Provider、认证、长度或 Session 问题

`hermes status` 只用于发现信息。先用页面相同 Provider/Model 做一条无工具真实 Query，
并确认默认 secrets 文件可读取；HTTP 401 即使 CLI 返回零退出码也视为认证失败。当前
v0.19.0 把正文写入 stdout、Session 状态写入 stderr，debate-agent 会分流解析并拒绝
Session 不一致。超长 Prompt 不会截断，而是在 65,536 UTF-8 字节门禁处失败。Hermes
卡始终标记为实验性；不要添加 `--worktree`、`--safe-mode` 或恢复历史 cwd 的参数。

### 13.8D CodeBuddy 安装、认证、模型或 Session 问题

先在启动 Streamlit 的同一用户环境中执行 `command -v codebuddy`、
`codebuddy --version` 和一次交互登录。若使用 API 认证，确认
`CODEBUDDY_API_KEY`/`CODEBUDDY_AUTH_TOKEN` 已存在于启动进程环境；系统不会从
DeepSeek secrets 文件推断 CodeBuddy 凭据。模型可在页面显式填写，或配置在
`CODEBUDDY_MODEL`/`~/.codebuddy/settings.json`。

预检会执行两轮真实、无工具结构化 Query。认证、JSON Schema、`structured_output`
或 resume Session 不一致都会在目标仓库写入前阻止启动。不要通过普通恢复绕过配置
漂移；修复环境后重新预检。CodeBuddy 终局采用单个 JSON，所以页面实时轨道主要显示
进程状态与安全终止事件，不保证逐 token 文本。

### 13.9 `startup_timeout`、`idle_timeout` 或 `hard_timeout`

- `startup_timeout`：300 秒内没有首个有效工具、消息、文件或终止事件；
- `idle_timeout`：已经工作，但连续 300 秒没有新有效活动；
- `hard_timeout`：即使持续活动也达到 3600 秒单次绝对上限；
- Run 剩余预算小于 hard 时，以剩余预算为准，默认总时限为 7200 秒。

先在当前 participants 动态出现的实时轨道查看最近活动，再展开“原始输出”的脱敏尾部。
启动/空闲超时可在新 Run 的高级设置中调整；不要为了掩盖重复外部错误而无限加大
hard。心跳、`thread.started` 和重复 cache/MCP 错误不会刷新 idle。

### 13.10 `network_error`、`cache_incompatible` 或健康预检熔断

`network_error` 通常来自模型主连接、TLS、代理或证书问题；`cache_incompatible`
表示当前 CLI 版本、参数能力或模型缓存不满足结构化协议。相同 CLI/版本/模型和错误
指纹在短窗口重复失败会打开熔断，冷却期内新 Run 会在仓库写入前快速失败。

1. 分别执行所有已选 CLI 的登录状态与最小模型调用；
2. 确认高级设置中的绝对 CLI 路径和显式模型正确；
3. 只按需勾选 `HTTPS_PROXY`、`SSL_CERT_FILE` 等变量名，不要把值粘贴到页面；
4. MCP 先保持 `minimal`；仅在任务确实需要时选择 `full`；
5. 修复外部连接后重新启动新 Run，成功健康探针会复位相应熔断记录。

### 13.11 `integration_blocked`

常见原因包括目标分支被其他任务提交、原工作空间切换分支、外部进程写入工作区，
或最终 tree 与已确认 tree 不一致。

不要使用强推或 `git reset --hard` 掩盖问题。先检查目标分支、结果分支和所有已选 Agent 确认的
tree。需要吸收新的目标分支提交时，通常应保留现有候选，并从最新目标分支新开 Run。

### 13.12 端口 8502 被占用

```bash
.venv/bin/debate-agent ui --port 8503
```

## 14. 升级与迁移

更新项目代码后，重新同步 Python 依赖：

```bash
cd /path/to/Py36Project/agent/debate
.venv/bin/pip install -e '.[ui]'
```

开发环境使用：

```bash
.venv/bin/pip install -e '.[dev,ui]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy src
```

不要移动正在运行或准备恢复的 `<state-root>`。Run 配置和仓库记录包含绝对路径；
移动目标仓库、状态目录或保留的 worktree 后，恢复边界校验会拒绝继续。

## 15. 新环境交付检查清单

- [ ] Python 3.10+ 与 Git 2.28+ 可用；
- [ ] `codex --version` 和 `codex login status` 成功；
- [ ] `claude --version`、`claude auth status` 和 `claude doctor` 成功；
- [ ] `agent/debate/.venv` 已创建并安装 `.[ui]`；
- [ ] `.venv/bin/debate-agent --help` 成功；
- [ ] 可选的 `secrets.env` 权限为 600，且包含 `DEEPSEEK_API_KEY`；
- [ ] 使用内置加载器的脱敏自检显示 `DeepSeek Key 已加载`；
- [ ] 目标仓库处于正确命名分支，`git status --short` 为空；
- [ ] 第一次 Run 使用独立 worktree、关闭自动合并并缩小允许路径；
- [ ] 第一次 Run 配置了可机械执行的验证命令；
- [ ] Run 达到 `completed`，结果分支、提交和审计文件均可读取；
- [ ] 确认系统没有自动 push、发布或部署。

## 相关文档

- [CLI Agent 默认执行参数](CLI_AGENT_EXECUTION_DEFAULTS.md)
- [项目 README](README.md)
- [真实双 CLI Smoke Test](SMOKE_TEST.md)
