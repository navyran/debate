# 七个 CLI Agent 的默认执行参数

> 最近核验：2026-07-26
> 适用范围：`debate-agent` 新建 Run。历史 Run 恢复时继续使用自身冻结配置。

## 1. 默认参数概览

| Agent | 模型/Profile | Session | Prompt/输出 | 权限与边界 |
| --- | --- | --- | --- | --- |
| Codex | 显式值或本机配置 | `exec resume` | stdin / JSONL + Schema | `workspace-write` |
| Claude | 显式值或本机配置 | `--session-id` / `--resume` | stdin / stream-json | `acceptEdits` |
| Qoder | 显式值或 CLI 默认 | `--session-id` / `--resume` | stdin / stream-json | `--yolo` |
| OpenCode | 单个 `provider/model`，可选 Variant | 响应 ID / `--session` | stdin / JSONL | `--auto` + 受控配置 |
| OpenClaw | Model、原生 Agent、Thinking | 显式 Gateway Session Key | 0600 消息文件 / JSON 外层 | 不用 `--local`、`--deliver` |
| Hermes | 显式 Provider/Model | 响应 ID / `--resume` | argv Query / Quiet 双流 | 实验性、`--yolo --no-restore-cwd` |
| CodeBuddy | 显式值或本机设置 | 响应 ID / `--resume` | stdin / JSON + Schema | 实验性、`-y` + 固定工具集 |

七个 Agent 默认启动/空闲/硬上限均为 300/300/3600 秒，心跳为 5 秒。绝对 CLI
路径、版本、模型及各自配置摘要会在健康预检后冻结。

新 Run 启动前会做 CLI 健康预检：定位可执行文件、读取版本、核验必需参数、检查登录、
执行短时限只读模型探针，并检查相同 CLI/版本/模型是否处于熔断冷却期。成功后，绝对
路径、版本、显式模型和超时配置写入该 Run 的 `config.json`。模型无法发现、CLI 未
登录或结构化探针失败都会阻止进入仓库写入阶段。

## 2. 分层超时与活动

- `startup_timeout_seconds=300`：首个有效工具、消息、文件变化或终止事件到达前的上限；
- `idle_timeout_seconds=300`：已有有效活动后，连续没有新活动的上限；
- `hard_timeout_seconds=3600`：无论是否持续活动，单次调用都不能突破；
- `heartbeat_interval_seconds=5`：页面快照刷新间隔，心跳本身不延长 idle；
- `total_timeout_seconds=7200`：整个 Run 的剩余预算会进一步压低单次 hard 上限；
- `max_process_failures=2`：只对工作区干净且可安全重放的基础设施故障有限重试。

每个内置 Agent 都有 `--<agent>-timeout-seconds` 历史兼容参数；指定时覆盖新的 hard
上限。历史 Run 缺少新字段时按其原有冻结超时恢复，不改写为新默认值。

工具开始/完成、Agent 文本、结构化消息、文件变化和工作区指纹变化会刷新活动。
`thread.started`、单纯存活、心跳以及重复的 cache/MCP 错误不会制造伪活动。

## 3. Codex CLI

### 3.1 首次调用

```bash
codex exec \
  -C <workspace> \
  --sandbox workspace-write \
  --ignore-user-config \
  --json \
  --output-schema <schema> \
  --model <frozen-model> \
  -
```

Prompt 经标准输入传入，不出现在 argv 或 manifest。`--ignore-user-config` 避免用户
级 MCP、instructions 或动态模型选择改变已预检的运行边界；该参数已用当前支持版本
的 `--help` 和自动化测试核验。

### 3.2 Session 复用

同一次 Run 后续调用大致为：

```bash
codex exec resume <session-id> \
  -c 'sandbox_mode="workspace-write"' \
  --ignore-user-config \
  --json \
  --output-schema <schema> \
  --model <frozen-model> \
  -
```

只有 `codex_resume_sandbox_verified=true` 时允许复用。页面和实时事件不显示完整
Session ID。阶段性 `agent_message` 可用于展示，但只有最后一个合法协议且出现
`turn.completed` 才算成功；内存窗口被截断时会从完整 raw JSONL 流式寻找终局协议。

## 4. Claude Code CLI

首次调用大致为：

```bash
claude -p \
  --session-id <session-id> \
  --permission-mode acceptEdits \
  --tools Read,Edit,Write,Glob,Grep,Bash \
  --allowedTools <policy-tools> \
  --output-format stream-json \
  --include-partial-messages \
  --verbose \
  --json-schema <schema> \
  --strict-mcp-config \
  --mcp-config '{}' \
  --model <frozen-model>
```

后续把 `--session-id` 替换为 `--resume <session-id>`。默认 `mcp_mode=minimal`，因此
不会加载用户机器上的可选 MCP；显式选择 full 时才允许使用本机 MCP 配置。Claude
Adapter 仍能解析历史单 JSON 结果，但新调用统一使用 `stream-json`。partial 文本只
用于实时展示，最终必须出现合法 `result/structured_output` 并通过 Schema 校验。

## 5. Qoder CLI

首次调用大致为：

```bash
qodercli --yolo -p \
  --cwd <workspace> \
  --session-id <session-id> \
  --output-format stream-json \
  --strict-mcp-config \
  --mcp-config '{}'
```

后续将 `--session-id` 替换为 `--resume <session-id>`。Qoder 默认使用 `--yolo`，Prompt
通过标准输入传入，并附加当前协议 JSON Schema；终止 `result` 中的 JSON 仍会经过
Pydantic 严格校验。若没有填写模型，则使用健康预检时冻结的 CLI 默认模型语义。

## 6. OpenCode CLI

OpenCode 是外部 CLI，不是本项目安装的 Python 或 Node 依赖；默认参与者仍为 Codex + Claude，未选择 OpenCode 不会预检它。被选择时，模型必须冻结为一个 `provider/model`，可选 variant 也冻结在本次 Run 内。

首次与后续调用共用下列 argv 骨架；Prompt 和协议 Schema 通过标准输入传入：

```bash
opencode run \
  --format json \
  --dir <workspace> \
  --model <frozen-provider/model> \
  --agent build \
  --auto \
  --pure \
  --variant <optional-frozen-variant> \
  --session <only-after-first-session>
```

`--auto` 仅允许可信工作区的自动确认，`--pure` 保持 JSONL 协议的非交互输出；两者不取消 Supervisor 的单 Writer、范围、Git 和验证门禁。首次终止 JSONL 必须给出唯一 `sessionID`，后续调用使用 `--session <sessionID>`；返回不同 ID 时拒绝结果。最后一个非空 JSONL `text` 才进入协议校验。

预检依次验证版本、`run --help`、`debug config --pure`、`auth list --pure`、受控配置和短时只读模型探针，成功后删除探针 Session。调用环境仅额外注入 `OPENCODE_CONFIG_CONTENT`、`OPENCODE_DISABLE_AUTOUPDATE=true`、`OPENCODE_DISABLE_DEFAULT_PLUGINS=true`；配置内容只在进程内使用，以哈希冻结身份，不写进公开事件、状态或审计。

`mcp_mode=minimal` 将初始配置发现的 MCP 逐个设为 `enabled=false`；`full` 保留通过预检的 MCP 集合。两种模式都强制 `permission=allow`、`share=disabled`、`snapshot=false`、`autoupdate=false`。OpenCode 的 variant 只由 `--opencode-variant` 或其页面配置卡提供；OpenClaw 的 variant 独立映射为 Thinking。

## 7. OpenClaw

OpenClaw 首次和续轮使用同一个显式 Gateway Session Key：

```bash
openclaw agent \
  --agent <frozen-native-profile> \
  --session-key <run-scoped-key> \
  --message-file <temporary-0600-file> \
  --model <frozen-model> \
  --thinking <frozen-thinking> \
  --timeout <hard-limit> \
  --json
```

Prompt 与协议 Schema 写入工作区外的 0600 临时文件，进程结束、超时、取消和异常路径
都会清理。调用从不生成 `--local`、`--deliver`、`--channel` 或外部目标。JSON 只是
厂商外层，最终正文仍进入统一 Schema 校验。Gateway 超时或连接丢失属于
`ambiguous_transport`：先查询 Session 的完成/运行/未接收/未知状态，不能按普通进程
错误盲目重放。

健康检查验证可执行文件、版本、Gateway RPC、配置、原生 Agent、Model、Thinking 和
真实只读 Query。冻结配置漂移时恢复必须重新预检。

## 8. Hermes（实验性）

Hermes v0.19.0 调用骨架为：

```bash
hermes chat \
  --quiet \
  --yolo \
  --no-restore-cwd \
  --provider <frozen-provider> \
  --model <frozen-model> \
  --resume <only-after-first-session> \
  --query <protocol-prompt>
```

当前版本没有调用级 `--profile`、stdin 或 query-file。Prompt 因此会短暂出现在本机
argv，但审计参数固定替换为 `<redacted>`，`Invocation` 的 repr 也不回显；超过 65,536
UTF-8 字节会在启动前拒绝且绝不截断。正文从 stdout 读取，v0.19.0 的 Session 状态从
stderr 分离；恢复 ID 不一致会拒绝结果。系统禁止 `--worktree`、`--ignore-rules`、
`--safe-mode`、Gateway 与 Kanban。

预检不仅运行 `hermes status`，还会使用显式 Provider/Model 做真实 Query。当前
DeepSeek Key 从默认 secrets 文件只注入 Hermes，HTTP 401 即使退出码为零也会阻止启动。

## 9. CodeBuddy（实验性）

CodeBuddy headless 调用骨架为：

```bash
codebuddy -p -y \
  --output-format json \
  --json-schema <schema> \
  --tools Bash,Read,Edit,Write,Glob,Grep \
  --setting-sources project \
  --model <frozen-model> \
  --agent <optional-native-agent> \
  --resume <only-after-first-session>
```

Prompt 通过 stdin 传入，不进入 argv。默认 `mcp_mode=minimal` 时追加
`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`。首次响应必须包含唯一
`session_id` 与 `structured_output`；后续调用显式 `--resume`，返回不同 Session 时
拒绝结果。健康检查在临时 Git 仓库执行首轮无工具结构化 Query 和第二轮 resume，
通过后冻结可执行文件、版本、模型、原生 Agent 与运行配置哈希。

CodeBuddy 的 `-y` 只解除其交互确认，不解除 Supervisor 的唯一 Writer、允许路径、
敏感文件、Git 和验证门禁。默认关闭自动记忆、热更新、Prompt 建议、内置市场、
自动升级、错误上报和遥测。终局是单个 JSON，因此页面可实时显示进程状态和安全终止
事件，但不承诺逐 token 文本流。

## 10. 实时输出与安全展示

所有参与 CLI 的受限事件写入：

```text
<state-root>/runs/<run-id>/invocation-events.jsonl
<state-root>/runs/<run-id>/invocation-progress.json
```

Streamlit 每 2 秒按 `seq` 增量读取，并按本次参与者动态提供实时页签和“原始输出”
页面。默认只在内存保留最近 1000 条事件；raw 默认折叠且只读取最后 500 行。模型文本
使用安全代码块展示，不按 HTML 执行。Prompt、Key、Token、Cookie、代理值、完整
Session ID 和工作区外绝对路径会被遮罩；不要把 `.runtime` 或 raw 文件提交到仓库。

## 11. MCP、环境和完全授权

默认 `mcp_mode=minimal`。CLI 子进程只接收基础环境名，以及用户显式选择的以下变量名：

```text
HTTPS_PROXY HTTP_PROXY ALL_PROXY NO_PROXY SSL_CERT_FILE SSL_CERT_DIR
```

配置、健康报告和页面只记录变量名，不记录变量值。DeepSeek Key 不会传给 Codex、
Claude、Qoder、OpenCode、OpenClaw 或 CodeBuddy；只有选择 Hermes 时才注入 Hermes
子进程。`CODEBUDDY_API_KEY`、`CODEBUDDY_AUTH_TOKEN`、`CODEBUDDY_BASE_URL`、
`CODEBUDDY_CUSTOM_HEADERS` 与 `CODEBUDDY_INTERNET_ENVIRONMENT` 只注入 CodeBuddy；
其中 Key、Token 和 Header 会加入脱敏器。

“完全授权执行命令”默认开启，只移除编排层命令白名单；它不会移除唯一 Writer、
GoalContract 范围、Git 归属、检查点、所有已选 Agent 的 complete Ballot 或 TreeAck。默认 Codex + Claude 只是默认参与者；选择其他任意 Agent 时同一完成门槛不变。Codex 仍受
`workspace-write` 约束；Claude 和 Supervisor 的 Bash 没有同等级 OS sandbox，
因此完全授权仅适合可信工作区。

## 12. 故障分类、有限重试与熔断

调用会区分 `startup_timeout`、`idle_timeout`、`hard_timeout`、`network_error`、
`cache_incompatible`、`mcp_optional_error`、`protocol_error`、`validation_error`、
`policy_violation` 和 `workspace_race`。只有安全且工作区干净的启动、空闲、网络或
进程类故障可有限重试；hard、协议、验证和策略错误不会盲目重放 Prompt。

相同 CLI/版本/模型/错误指纹在短窗口重复失败会打开持久化熔断。冷却期内新 Run 的
健康预检快速失败，避免每次等待相同外部故障；不同 CLI 或模型互不串扰，成功探针会
复位对应熔断记录。

## 13. 配置入口

CLI 与 Streamlit 高级设置均可配置：

```text
--<agent>-command / --<agent>-model
--opencode-variant
--openclaw-native-profile / --openclaw-variant
--hermes-provider / --hermes-model
--codebuddy-native-profile / --codebuddy-model
--*-startup-timeout-seconds
--*-idle-timeout-seconds
--*-hard-timeout-seconds
--*-heartbeat-interval-seconds
--max-process-failures
--mcp-mode minimal|full
--proxy-env NAME
--realtime-output / --no-realtime-output
--raw-output-tail-lines
```

Facilitator 的 `DEEPSEEK_API_KEY` 默认从
`~/.config/debate-agent/secrets.env` 自动加载。Facilitator 没有 Shell、Git 或文件
工具，其 `reasoning_effort="high"` 只用于特定语义路由，不代表 Codex 或 Claude 的
推理强度。
