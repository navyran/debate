# OpenClaw 与 Hermes Agent 接入实施方案

本文定义 `debate-agent` 在现有 Codex、Claude Code、Qoder、OpenCode 四个内置
参与者基础上，新增 OpenClaw 和 Hermes Agent 两个可选 CLI 参与者的实施方案。
方案延续已经建立的内置 Agent 注册机制，不改变 LangGraph 主流程、Supervisor
职责、单写者约束和全体完成确认条件。

> 状态：已实施（2026-07-26）
> 方案日期：2026-07-26
> 默认参与者：仍为 Codex + Claude
> 接入等级：OpenClaw 为正式候选；Hermes Agent 首版标记为实验性
> 调用方式：本地 CLI 子进程，不引入厂商 SDK，不让厂商框架接管 Debate 编排
>
> 实施证据：[模块验收清单](OPENCLAW_HERMES_IMPLEMENTATION_TODO.md)、
> [兼容性报告](OPENCLAW_HERMES_COMPATIBILITY_REPORT.md)、
> [最终验收报告](OPENCLAW_HERMES_IMPLEMENTATION_REPORT.md)

## 1. 结论

两个 CLI 都可以接入，但成熟度和实施顺序不同：

| Agent | 可行性 | 结论 |
| --- | --- | --- |
| OpenClaw | 高 | 优先按正式内置 Agent 接入 |
| Hermes Agent | 中 | 可以接入，但首版必须标记为实验性 |

当前机器已经具备基础运行条件：

- OpenClaw `2026.7.1-2` 已安装，Gateway 正在运行且配置审计有效；
- Hermes Agent `v0.19.0` 已安装；默认自定义服务端的 `glm-5.1` 返回 401，经真实探针
  确认显式 `deepseek/deepseek-v4-flash` 可用；
- Hermes 的状态信息只能证明存在配置，生产预检仍执行真实模型调用。

OpenClaw 官方 `agent` 命令提供显式 Session Key、消息文件、模型、推理等级、超时和
JSON 输出，能够较自然地映射到现有 Adapter：

- <https://docs.openclaw.ai/cli/agent>

Hermes 官方 `chat` 命令提供单次 Query、Session 恢复、Provider、Model、Quiet 和
Yolo 模式，但当前没有为 `chat` 提供文档化的 JSON/JSONL 输出或 Query 文件输入：

- <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md>

因此 OpenClaw 可以直接进入正式实现阶段；Hermes 必须先通过兼容性探针，并保留
Prompt 位于进程参数中的已知限制。

## 2. 目标与非目标

### 2.1 目标

- 页面可以把 OpenClaw 和 Hermes 作为可选参与者；
- 任意一次 Run 仍要求至少选择两个内置 CLI Agent；
- 同一个 Run 内，每个 OpenClaw 或 Hermes 参与者持续复用首次建立的原生 Session；
- 不同 Run 默认使用相互隔离的 Session，不继承其他 Debate Run 的业务上下文；
- Writer 在 active 写租约内可以修改共享工作空间并执行命令；
- Reviewer、终审和投票调用仍不得修改工作区；
- 每次写入后由 Supervisor 创建带 Agent 标识的 Git 提交；
- 下一个 Agent 在执行前读取当前 HEAD、Git 变化全文和记忆更新；
- 两个新 Agent 使用现有统一协议、审计、实时状态和恢复机制；
- 所有选中 Agent 都接受并都判断 GoalContract 已完成后，Run 才能结束。

### 2.2 非目标

- 不让 OpenClaw Gateway 或 Hermes Gateway 取代 LangGraph；
- 不把 OpenClaw 或 Hermes 内部的多 Agent 编排映射成多个 Debate 参与者；
- 不允许两个 Agent 同时写工作空间；
- 不使用 Hermes 自带的 `--worktree` 管理共享工作空间；
- 不使用 OpenClaw 的 `--deliver` 向聊天平台发送 Debate 输出；
- 不引入动态 Python 插件或运行时加载任意第三方 Adapter；
- 不在本次接入中重构稳定的 Supervisor、GoalContract、审计和最终集成流程；
- 不承诺两种 CLI 首版提供与 Codex JSONL 相同粒度的实时工具事件。

## 3. 必须保留的系统不变量

1. 同一时刻只有一个 Agent 持有 active 写租约。
2. Writer 只能修改 GoalContract 允许的路径。
3. Reviewer、终审和投票调用前后工作区指纹必须一致。
4. CLI Agent 不得自行提交、切换分支、重置历史、合并目标分支或 push。
5. Git 检查点、恢复提交和最终集成都由 Supervisor 完成。
6. 每次写入后，所有其他参与者都收到 Git 变化全文和受限变更摘要。
7. 每个参与者在同一 Run 内持续复用自己的原生 Session。
8. Session 丢失或无法证明连续性时，必须创建新代次并重新注入权威记忆。
9. 最终验证通过、所有参与者接受、所有参与者判断目标完成，并确认同一个 final
   tree 后才能成功结束。
10. 默认参与者继续为 Codex + Claude；新增 Agent 不自动勾选，也不在未选中时预检。

## 4. 现有架构适配性

当前核心 Runtime 已经按 `config.participants` 处理任意数量的内置参与者：

- `models.py` 使用 `AgentName` 标识参与者；
- `agent_catalog.py` 保存内置 Agent 元数据；
- `adapters.py` 通过 `ADAPTER_FACTORIES` 注册 CLI Adapter；
- `cli_health.py` 通过 `HEALTH_STRATEGIES` 注册预检策略；
- `sessions.py` 按 Agent 保存 Session ID、代次和健康状态；
- Runtime、投票、TreeAck、记忆广播和审计均遍历 `participants`；
- Streamlit 的参与者、轨道和标签主要由 Catalog 驱动生成。

因此本次不新增 LangGraph 节点，不改变 Writer/Reviewer/Facilitator/Supervisor 的角色
边界。主要改造集中在：

```text
AgentName / Catalog
        │
        ├─ OpenClawAdapter ── OpenClawHealthStrategy
        ├─ HermesAdapter ──── HermesHealthStrategy
        │
        ├─ Prompt 传输与 argv 脱敏
        └─ Streamlit 配置和状态展示
```

少量仍然只针对 OpenCode 的 UI、模型冻结和运行配置特判，应改为由 Catalog 能力字段
驱动，但不能借此重构其他无关 Runtime 代码。

## 5. 注册模型改造

### 5.1 AgentName

在 `src/debate_agent/models.py` 增加：

```python
class AgentName(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    QODER = "qoder"
    OPENCODE = "opencode"
    OPENCLAW = "openclaw"
    HERMES = "hermes"
```

### 5.2 AgentDefinition

在现有 `AgentDefinition` 上增加最少的展示能力元数据：

```python
@dataclass(frozen=True, slots=True)
class AgentDefinition:
    name: AgentName
    display_name: str
    default_command: str
    model_policy: ModelPolicy
    supports_variant: bool = False
    variant_label: str | None = None
    supports_native_profile: bool = False
    supports_provider: bool = False
    experimental: bool = False
    supports_mcp: bool = True
    ui_color: str = "gray"
```

这些字段只驱动配置表单和状态展示。具体命令、Session 和安全行为仍然属于 Adapter，
不放进 Catalog。

新增定义：

```python
AgentName.OPENCLAW: AgentDefinition(
    name=AgentName.OPENCLAW,
    display_name="OpenClaw",
    default_command="openclaw",
    model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
    supports_variant=True,
    variant_label="Thinking",
    supports_native_profile=True,
    ui_color="#d97706",
)

AgentName.HERMES: AgentDefinition(
    name=AgentName.HERMES,
    display_name="Hermes",
    default_command="hermes",
    model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
    supports_provider=True,
    experimental=True,
    ui_color="#7c3aed",
)
```

### 5.3 AgentProfile

在冻结运行参数中增加：

```python
native_profile: str | None = None
provider: str | None = None
```

映射规则：

| Agent | `native_profile` | `provider` | `model` | `variant` |
| --- | --- | --- | --- | --- |
| OpenClaw | `--agent` | 模型 ID 内自带或为空 | `--model` | `--thinking` |
| Hermes | 当前版本不支持调用级 Profile | `--provider` | `--model` | 不使用 |

新增字段必须进入冻结配置和审计，恢复运行时不得从当前页面或当前用户配置静默覆盖。

## 6. Invocation 与 Prompt 传输

### 6.1 当前缺口

现有 Adapter 主要通过 stdin 传递 Prompt。OpenClaw 支持 `--message-file`，Hermes
当前只支持 `--query <text>`。不能直接把完整 Prompt 拼进现有 `argv` 后继续按普通
参数记录，否则会把目标、代码上下文和其他 Agent 摘要写入运行日志。

### 6.2 推荐扩展

扩展 `Invocation`，明确 Prompt 传输和审计参数：

```python
class PromptTransport(str, Enum):
    STDIN = "stdin"
    TEMP_FILE = "temp_file"
    ARGV = "argv"


@dataclass(frozen=True, slots=True)
class Invocation:
    argv: tuple[str, ...]
    audit_argv: tuple[str, ...]
    cwd: Path
    stdin: str | None
    prompt_transport: PromptTransport
    temporary_inputs: tuple[TemporaryInput, ...]
    ...
```

要求：

- `audit_argv` 中的 Prompt、Session 凭据和敏感配置一律显示为 `<redacted>`；
- 临时 Prompt 文件创建在工作空间外，权限为 `0600`；
- 正常退出、异常、超时和取消路径都必须删除临时文件；
- 临时文件路径可以审计，文件内容不能进入 Invocation 事件；
- `ARGV` 传输必须设置字节上限，超过上限时在启动前失败；
- 现有 Codex、Claude、Qoder、OpenCode 继续使用 stdin，不改变已有行为。

## 7. OpenClaw Adapter

### 7.1 调用方式

推荐使用 Gateway-backed Agent：

```bash
openclaw agent \
  --agent <native_profile> \
  --session-key debate:<run_id>:openclaw \
  --message-file <temporary_prompt_file> \
  --model <provider/model> \
  --thinking <variant> \
  --timeout <seconds> \
  --json
```

参数规则：

- `--agent` 仅在配置了 `native_profile` 时传入；
- `--session-key` 首轮即创建，并在同一个 Run 中持续复用；
- 使用 `--message-file`，不把 Prompt 放入 argv；
- `--model` 和 `--thinking` 使用预检后冻结的值；
- `--timeout` 与当前 hard timeout 对齐；
- 始终使用 `--json`；
- 永远不传 `--deliver`；
- 默认不使用 `--local`，因为本方案要求稳定的 Gateway Session。

### 7.2 Session 标识

Session Manager 保存的是可恢复的显式 Session Key，而不是依赖默认 main session：

```text
agent:<openclaw-agent-id>:debate:<run-id>:openclaw
```

若没有配置 `--agent`，保存由默认 Agent 作用域解析后的规范 Session Key。不同 Run
不得复用同一个 key。

### 7.3 终止输出

OpenClaw `--json` 只保证厂商外层为 JSON，不等于业务回复满足 Debate Schema。
Adapter 必须：

1. 解析 stdout 的 OpenClaw JSON；
2. 提取最终回复和规范 Session 信息；
3. 丢弃未知厂商字段；
4. 把回复交给现有 `ProtocolNormalizer`；
5. 通过当前 Pydantic Schema 校验；
6. 最多执行一次格式修复，仍失败则抛出 `AdapterOutputError`。

### 7.4 中断与重复执行

Gateway 传输丢失时，OpenClaw 可能已经接受任务。此时不能立即重放 Prompt：

1. 标记当前调用为 `ambiguous_transport`;
2. 查询 Gateway 状态和对应 Session 的最新记录；
3. 如果发现该 turn 已完成，恢复其结果；
4. 如果仍在运行，继续等待或发送取消；
5. 只有证明未接受时才能重试；
6. 无法证明时停止自动重放，转入可恢复失败。

这项行为必须成为 OpenClaw Adapter 的专项恢复策略，不能复用普通“退出码非零即重试”
逻辑。

## 8. Hermes Adapter

### 8.1 调用方式

首次调用：

```bash
hermes chat \
  --quiet \
  --yolo \
  --no-restore-cwd \
  --provider <provider> \
  --model <model> \
  --query <prompt>
```

后续调用增加：

```bash
--resume <session_id>
```

Hermes v0.19.0 的 `chat --help` 没有调用级 `--profile`，因此实现不展示或生成该
参数；Provider 与 Model 必须显式冻结。

### 8.2 强制参数约束

- 默认使用 `--yolo`，避免 Writer 因危险命令确认而中断；
- 始终使用 `--no-restore-cwd`，防止恢复旧 Session 时切到历史工作目录；
- 禁止使用 `--worktree`，工作区和 worktree 只由 debate-agent 管理；
- 不默认使用 `--ignore-rules`，目标项目的 `AGENTS.md` 等规则必须继续生效；
- 不默认使用 `--safe-mode`，否则会关闭用户实际依赖的模型和工具配置；
- 不启用 Hermes Gateway、Kanban 或内部多参与者编排。

### 8.3 Prompt 风险

Hermes 当前通过 `--query` 接收 Prompt，存在两个已知限制：

1. Prompt 在进程运行期间可能被同机进程列表看到；
2. 长 Prompt 受操作系统 argv 长度限制。

首版处理：

- Hermes 在页面标记为“实验性”；
- `audit_argv` 必须把 Query 替换为 `<redacted>`；
- 对 Prompt 设置明确的 UTF-8 字节上限；
- 超限时不截断权威上下文，而是在调用前返回可操作错误；
- 文档明确说明同机进程可见性；
- Hermes 后续提供 stdin 或 query-file 后，优先迁移并移除该限制。

不采用交互式 REPL 或伪终端绕过 `--query`，因为其终止边界、输出回显和取消恢复不稳定，
会给生产运行带来更大风险。

### 8.4 终止输出

Hermes `--quiet` 仍可能同时输出最终回复和 Session 信息，Adapter 必须分别解析：

- stdout：最终业务回复；
- stderr 或受控状态行：Session ID 和诊断信息；
- 不把 Banner、Spinner、恢复提示或 Session 状态拼进业务 JSON；
- 最终回复继续经过现有协议提示、Normalizer、Schema 校验和一次格式修复。

任何无法明确分离最终回复与 Session 元数据的版本都不能通过预检。

## 9. 权限、写租约与 Agent 记忆

### 9.1 Writer

OpenClaw 和 Hermes Writer 均获得完全 CLI 命令能力，但写入权限仍由外部 Supervisor
控制：

```text
Supervisor 发放 active 写租约
  → Agent 读取当前 HEAD 和 peer 更新
  → Agent 修改工作区并返回摘要
  → Supervisor 校验范围、HEAD 和 Git 状态
  → Supervisor 创建 agent:<name> 检查点提交
  → 广播 Git 变化全文和记忆摘要
  → 回收写租约
```

建议提交 message 延续现有格式，并支持：

```text
debate(<run-id>): checkpoint [agent:openclaw]
debate(<run-id>): checkpoint [agent:hermes]
```

### 9.2 Reviewer

两种 CLI 都不能只依赖厂商自身“只读模式”：

- OpenClaw 的 Sandbox、Tool Policy 和 Elevated 权限是不同层；
- Hermes 的 `--yolo` 会绕过命令确认；
- Agent 仍可能通过终端命令修改文件，而不是调用显式写文件工具。

因此继续使用现有外部硬门禁：

1. 调用前记录 HEAD、Git 状态和工作区指纹；
2. Reviewer 不获得 active 写租约；
3. 调用后重新计算指纹；
4. 出现任何变化立即判定越权；
5. 只回滚能够唯一归属于本次调用的变化；
6. 无法唯一归属时停止自动处理并保留审计证据。

OpenClaw 沙箱说明：

- <https://docs.openclaw.ai/gateway/sandboxing>

### 9.3 Session 与权威记忆

原生 Session 只负责保存该 CLI 自身的对话记忆。跨 Agent 的事实仍由 debate-agent
显式注入：

- 当前 GoalContract；
- 当前 HEAD；
- 自该 Agent 上次执行后的 Git 提交和变化全文；
- 其他 Agent 的变更摘要和未决问题；
- 当前验证结果；
- 当前角色是 Writer、Reviewer、投票还是终审。

不能假设 OpenClaw 或 Hermes 能自动读取其他 Agent 的原生 Session。

## 10. 结构化输出与实时状态

### 10.1 统一终止协议

```text
CLI 原始输出
  → Vendor Adapter 提取终止回复
  → ProtocolNormalizer
  → Pydantic Schema
  → 一次格式修复
  → AgentResult / ReviewResult / Ballot / TreeAck
```

两个新 Agent 不新增业务协议 Schema，也不降低最终输出验证标准。

### 10.2 首版实时事件

OpenClaw 和 Hermes 当前 CLI 不提供已经验证的、与 Codex JSONL 等价的结构化工具事件。
首版页面只承诺显示：

- 进程启动和结束；
- 启动、空闲和硬超时倒计时；
- 周期性心跳和最近有效活动时间；
- stderr 的脱敏诊断摘要；
- 当前 Session 标识摘要；
- Supervisor 观察到的 Git 变化；
- 最终结构化回复或错误类别。

不得把无法证明语义的任意文本行标记为“执行命令”“修改文件”或“已完成”。

后续只有在厂商提供稳定 JSONL/事件接口后，才增加工具级实时解析。

## 11. 健康检查

### 11.1 OpenClawHealthStrategy

依次检查：

1. `openclaw` 可执行文件存在；
2. 版本满足已经验证的最低版本；
3. `openclaw gateway status --json` 成功；
4. Gateway 配置存在、有效且能够连接；
5. 指定 Agent/Profile 存在；
6. provider/model 和 thinking 配置有效；
7. 单轮 `--message-file --json` 调用成功；
8. 返回内容可通过统一协议解析；
9. 同一 Session Key 的第二轮能读取第一轮标记；
10. 临时 Git 仓库中的 Writer 调用能产生预期变化；
11. Reviewer 探针前后工作区指纹一致；
12. 取消和模糊传输恢复不会重复执行。

Gateway 没有运行时，预检应给出明确修复命令，不应静默切换 `--local`，因为这会改变
冻结 Session 语义。

### 11.2 HermesHealthStrategy

依次检查：

1. `hermes` 可执行文件存在；
2. 当前版本满足已经验证的最低版本；
3. `hermes status` 可读取 Provider、Model 和 Profile；
4. 执行一次真实模型 Query；
5. 能从输出中可靠提取最终回复和 Session ID；
6. 使用 `--resume` 的第二轮能读取第一轮标记；
7. `--no-restore-cwd` 能保持 debate-agent 指定工作目录；
8. `--quiet` 输出没有污染业务终止协议；
9. 临时 Git 仓库中的 `--yolo` Writer 能产生预期变化；
10. Reviewer 探针前后工作区指纹一致；
11. Prompt 超限和 Session 丢失产生明确错误类别。

任何一个关键探针失败时，Hermes 保持可见但不可选，页面展示具体原因。不能仅因为
`hermes --version` 成功就通过预检。

## 12. Streamlit 页面改造

参与者选择区新增 OpenClaw 和 Hermes 卡片。

OpenClaw 配置：

- CLI 命令路径；
- OpenClaw Agent/Profile；
- provider/model；
- Thinking；
- Gateway 状态；
- Session 复用探针状态。

Hermes 配置：

- CLI 命令路径；
- Provider；
- Model；
- 实验性标识；
- Session 恢复探针状态；
- Prompt argv 可见性说明。

页面行为：

- 默认仍只勾选 Codex + Claude；
- 未选中的 Agent 不预检、不要求认证；
- 选择 Hermes 时展示实验性告警，但不要求用户每轮确认；
- 启动前一次性完成所有已选 Agent 的预检；
- 运行中 Agent 轨道按参与者动态生成；
- 不改变用户选择“集成回目标分支”或“保留结果分支”的流程。

## 13. 审计和恢复

每个新 Agent 的审计元数据至少包含：

- Agent 名称和显示名；
- CLI 绝对路径和版本；
- 原生 Profile/Agent ID；
- Provider、Model 和 Thinking；
- 运行配置摘要；
- Session ID 或 Session Key 的脱敏摘要；
- Session 代次；
- Prompt transport；
- 是否为实验性 Adapter；
- 预检结果；
- 每轮退出码、超时类型和错误分类；
- Writer 检查点提交及变更摘要。

不得记录：

- 完整 Prompt 的 argv；
- Provider API Key；
- Gateway Token；
- Session 凭据原文；
- OpenClaw 外部投递目标；
- Hermes `.env` 内容。

恢复时必须继续使用冻结的 Adapter 类型、CLI 版本、Provider、Model、Profile 和
Session 代次。若本机 CLI 已升级到未验证版本，先重新预检，不能直接继续。

## 14. 测试计划

### 14.1 注册完整性

```python
assert set(AGENT_CATALOG) == set(AgentName)
assert set(ADAPTER_FACTORIES) == set(AgentName)
assert set(HEALTH_STRATEGIES) == set(AgentName)
```

### 14.2 Adapter 单元测试

OpenClaw：

- argv 不包含 Prompt；
- Prompt 使用权限为 `0600` 的临时文件；
- 不包含 `--local` 和 `--deliver`；
- Session Key 在同一个 Run 中保持不变；
- JSON 外层和业务回复正确分离；
- 超时、取消和模糊传输分类正确；
- 临时文件在全部退出路径中删除。

Hermes：

- 默认包含 `--quiet --yolo --no-restore-cwd`；
- 不包含 `--worktree`；
- 首轮不传 `--resume`，后续使用同一 Session ID；
- `audit_argv` 不含 Prompt；
- 超长 Prompt 在启动前失败且不截断；
- stdout/stderr 中的回复和 Session 信息正确分离；
- 格式污染不能绕过 Schema 校验。

### 14.3 Runtime 和安全测试

- OpenClaw + Codex 双参与者；
- Hermes + Claude 双参与者；
- OpenClaw + Hermes 双参与者；
- Codex + Claude + Qoder + OpenCode + OpenClaw + Hermes 六参与者；
- Writer 顺序可以由 Facilitator 建议或确定性轮转，不固定谁先写；
- 任意时刻最多一个 active 写租约；
- Reviewer 写入触发越权；
- 写入后所有其他 Agent 收到 Git 全文和记忆摘要；
- 每个 Agent 在同一个 Run 内复用自己的 Session；
- Session 丢失后创建新代次并注入权威记忆；
- 少一个 Agent 的 accept 或 complete 都不能结束；
- 最终集成只产生一个聚焦提交。

### 14.4 可选真实 CLI 测试

真实测试通过环境变量显式启用，默认单元测试不消耗模型额度：

```text
DEBATE_AGENT_E2E_OPENCLAW=1
DEBATE_AGENT_E2E_HERMES=1
```

测试必须使用临时 Git 仓库和独立 Session，不在项目源码工作区执行写入探针。

## 15. 分阶段实施

### 阶段 P0：兼容性探针

目标：证明当前安装版本满足 Adapter 的最小协议。

- 冻结 `openclaw --version` 和 `hermes --version`；
- 验证真实模型调用；
- 验证两轮 Session 记忆；
- 保存脱敏 stdout/stderr 样本；
- 验证取消、超时和工作目录；
- 确认 Hermes Query 的长度边界和 Session 输出格式。

完成门槛：

- OpenClaw 全部探针通过，进入 P1；
- Hermes 全部关键探针通过，进入 P2；
- Hermes 若无法可靠提取 Session 或终止回复，则暂不出现在正式参与者列表。

### 阶段 P1：OpenClaw 正式接入

- 扩展枚举、Catalog 和 Profile；
- 增加临时 Prompt 文件能力；
- 实现 OpenClaw Adapter、流解析器和健康策略；
- 增加 UI、审计和恢复；
- 完成单元测试和双 Agent E2E；
- 更新使用说明。

完成后 OpenClaw 作为普通可选 Agent，但仍不默认勾选。

### 阶段 P2：Hermes 实验性接入

- 增加 argv Prompt 脱敏和长度保护；
- 实现 Hermes Adapter、输出解析和健康策略；
- 增加 Profile/Provider/Model 页面配置；
- 增加实验性标识；
- 完成 Session 恢复、Writer、Reviewer 和组合测试；
- 更新安全限制说明。

完成后 Hermes 可以被用户主动选择，但页面持续展示实验性状态。

### 阶段 P3：稳定性观察

- 在真实任务中连续完成不少于 10 个 Hermes Run；
- 至少覆盖两种参与者组合和一次恢复；
- 没有 Session 串线、Prompt 泄露到审计、重复执行或 Reviewer 写入；
- 结构化输出成功率达到与当前内置 Agent 可接受的同级水平；
- 再决定是否取消 Hermes 实验性标记。

不因为达到固定运行数量自动升级，仍需根据真实失败类型判断。

## 16. 验收标准

以下条件全部满足才视为实施完成：

1. OpenClaw 和 Hermes 在页面中可选，默认均不选。
2. 未选择的 Agent 不预检、不要求认证。
3. OpenClaw 使用 Gateway Session Key，同一 Run 内稳定复用。
4. Hermes 使用同一 Session ID 恢复，且不会恢复到错误工作目录。
5. OpenClaw Prompt 不进入 argv；Hermes Prompt 不进入审计 argv。
6. Hermes 超长 Prompt 不会被静默截断。
7. 两个 Agent 都能在写租约内修改共享工作区。
8. 两个 Agent 的 Reviewer 调用都不能留下工作区变化。
9. 两个 Agent 都不能自行提交、切换分支或创建额外 worktree。
10. 写入提交包含 `agent:openclaw` 或 `agent:hermes` 标识。
11. 其他 Agent 下一轮能读取对应 Git 变化全文和记忆摘要。
12. 页面能显示心跳、Session、Git 变化和最终结果。
13. 原始输出被保存但不泄露 Prompt、Key 和 Session 凭据。
14. 中断恢复不会盲目重复执行 OpenClaw 已接受的任务。
15. 所有参与者都接受且都判断目标完成，Run 才能成功。
16. 现有 Codex、Claude、Qoder 和 OpenCode 测试无回归。

## 17. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| OpenClaw Gateway 接收任务后连接丢失 | 盲目重试造成重复修改 | 查询 Session 后再决定恢复或重试 |
| OpenClaw JSON 仅为厂商外层 | 普通文本被误认为协议结果 | 继续执行 Normalizer 和 Schema 校验 |
| Hermes Prompt 位于 argv | 同机可见、长度受限 | 脱敏审计、长度门禁、实验性标识 |
| Hermes Quiet 输出格式变化 | Session 或回复解析失败 | 版本冻结、真实预检、严格终止解析 |
| Hermes 恢复历史 cwd | 修改错误工作区 | 强制 `--no-restore-cwd` 并校验 cwd |
| Hermes 自建 worktree | 与 Supervisor 的 Git 模型冲突 | 禁止 `--worktree` |
| 厂商内部多 Agent 自动委派 | 参与者身份和责任范围模糊 | 使用专用 Profile，关闭外部投递和内部编排 |
| CLI 升级改变参数或输出 | 历史 Run 无法安全恢复 | 冻结版本，升级后重新预检 |
| 参与者增加导致轮次和成本膨胀 | 运行时间过长 | 默认仍为两 Agent，用户主动选择新增参与者 |

## 18. 最终建议

采用以下决策：

1. 接入 OpenClaw，按正式内置 Agent 标准实施。
2. 接入 Hermes，但首版标记为实验性。
3. 不修改 LangGraph 主图和 Supervisor 业务边界。
4. 不改变单写者、Git 检查点、Peer Memory 和最终全体完成条件。
5. 默认参与者继续为 Codex + Claude。
6. OpenClaw 使用 Gateway、显式 Session Key 和消息文件。
7. Hermes 使用 `--yolo --no-restore-cwd`，禁止自建 worktree。
8. Hermes 的 argv Prompt 限制必须公开、脱敏并设置长度门禁。
9. 先完成兼容性探针，再分别进入正式和实验性实现。

这套方案的主要开发量位于 Adapter、健康检查和 Prompt 传输边界，属于中等复杂度；
不需要为两个新 CLI 重写多 Agent 辩论流程。
