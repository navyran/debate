# 内置 Agent 注册机制与 OpenCode CLI 接入方案

本文定义 `debate-agent` 从 Codex、Claude Code、Qoder 三个内置参与者扩展到
OpenCode CLI 的改造方案。方案先建立轻量的内置 Agent 注册机制，再接入 OpenCode，
避免继续在 Runtime、健康检查、Streamlit、审计和实时视图中增加第四套硬编码分支。

> 状态：待实施
> 方案日期：2026-07-22
> 默认参与者：仍为 Codex + Claude
> OpenCode 接入方式：本地 CLI 子进程，不引入 OpenCode SDK 或常驻 Server

## 1. 背景

`debate-agent` 已支持在发起任务时选择 Codex、Claude Code 和 Qoder。核心运行语义
已经基于 `participants` 集合工作：

- LangGraph 可以在选中的参与者之间调度回合；
- 同一时刻只有一个 Agent 持有写租约；
- Supervisor 在写入后创建带 Agent 标识的 Git 提交；
- 其他 Agent 下一轮先读取当前 HEAD、Git 变化全文和变更摘要；
- 记忆更新会广播给所有其他参与者；
- 所有已选 Agent 都接受且都判断目标完成后，运行才能结束。

当前主要限制位于接入边界：

- `AgentName` 只有三个内置值；
- Runtime 使用按 Agent 枚举构造 Adapter 的局部字典；
- 健康检查包含 Codex、Claude 和 Qoder 分支；
- CLI 和 Streamlit 表单仍显式声明每一个 Agent 的配置项；
- `InvocationLiveView` 固定保存 `codex`、`claude`、`qoder` 三条轨道；
- 审计显示名和四份协议 Schema 固定列出三个 Agent。

直接增加第四套 `if agent is AgentName.OPENCODE` 可以短期工作，但下一次增加 CLI 时会
重复修改同样的边界。推荐先把这些边界收敛为内置注册机制，再注册 OpenCode。

## 2. 目标与非目标

### 2.1 目标

- 增加 OpenCode CLI 作为可选且平级的讨论参与者；
- 发起任务时可以选择任意至少两个内置 CLI Agent；
- 同一次 Run 内，OpenCode 始终复用第一次创建的原生 Session；
- OpenCode 可以在写租约内修改共享工作空间并执行命令，不产生交互式授权中断；
- OpenCode 的 JSON 事件实时显示在页面上；
- OpenCode 写入后，由 Supervisor 创建带 `agent:opencode` 标识的 Git 提交；
- 所有其他参与者都能读取 OpenCode 的 Git 变化全文和记忆摘要；
- 不改变现有单写者、范围检查、最终共识和审计语义；
- 后续增加内置 CLI 时，只需新增定义、Adapter 和健康策略，不再修改每个展示分支。

### 2.2 非目标

- 不实现运行时安装第三方 Agent 插件；
- 不允许用户从页面加载任意 Python Adapter；
- 不引入 OpenCode JavaScript/TypeScript SDK；
- 不运行常驻 `opencode serve` 服务；
- 不改变 DeepSeek Facilitator 的业务权限；
- 不让 Supervisor 判断哪一个 Agent 的业务结论更正确；
- 不允许 OpenCode 自行提交、切换 Git 分支或集成目标分支；
- 不因为参与者增加而降低“全部参与者都认为目标完成”的结束条件。

## 3. 必须保留的系统不变量

1. 同一时刻只有一个 Agent 持有 active 写租约。
2. Writer 只能修改 GoalContract 允许的路径。
3. Reviewer 和终审调用不能改变工作区指纹。
4. CLI Agent 不得创建提交、改变 HEAD、切换分支、重置历史或 push。
5. 所有 Git 检查点和最终集成都由 Supervisor 完成。
6. 每次写入提交后，所有其他参与者都收到 Git 变化和变更摘要。
7. 每个参与者在同一 Run 内持续复用自己的原生 Session。
8. Session 丢失时必须创建新代次，并重新注入完整权威记忆。
9. 所有选中 Agent 都接受、都判断 GoalContract 完成、最终验证通过，并确认同一个
   final tree 后才能结束。
10. 最终目标分支只保留聚焦结果；中间讨论通过结果内的审计摘要追踪。

## 4. 方案选择

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| 直接增加第四套分支 | 开发量最小 | 固定分支继续扩散，下一次接入仍需全局修改 | 不采用 |
| 内置 Catalog + 实现注册表 | 接入边界清晰，保留静态类型和内置安全边界 | 需要先迁移现有三方 | 采用 |
| 动态插件和 Python entry point | 可由外部包扩展 | 配置、可信代码和兼容性成本过高 | 暂不采用 |
| OpenCode Server/SDK | 原生结构化输出和事件 API 更完整 | 引入 Node 依赖和服务生命周期，偏离 CLI 编排目标 | 暂不采用 |

本次注册机制只服务于仓库内置实现，不构建通用插件框架。

## 5. 目标架构

```text
Agent Catalog
  ├─ CLI 参数和默认命令
  ├─ Streamlit 配置卡、显示名和颜色
  └─ 审计显示名

Debate Runtime
  ├─ Adapter Registry
  │    ├─ CodexAdapter
  │    ├─ ClaudeAdapter
  │    ├─ QoderAdapter
  │    └─ OpenCodeAdapter
  ├─ Health Strategy Registry
  │    ├─ CodexHealthStrategy
  │    ├─ ClaudeHealthStrategy
  │    ├─ QoderHealthStrategy
  │    └─ OpenCodeHealthStrategy
  └─ LangGraph / Supervisor
       ├─ 单写者租约
       ├─ Git 检查点
       ├─ Peer memory 广播
       └─ 全体完成确认
```

Catalog 只保存不依赖运行实现的元数据。Adapter 和健康检查分别维护实现注册表，避免
`config -> catalog -> adapters -> config` 循环依赖。

## 6. Agent Catalog

新增 `src/debate_agent/agent_catalog.py`：

```python
@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """描述一个受信任的内置 CLI Agent。"""

    name: AgentName
    display_name: str
    default_command: str
    model_policy: ModelPolicy
    supports_variant: bool = False
    supports_mcp: bool = True
    ui_color: str = "gray"
```

模型策略：

```python
class ModelPolicy(str, Enum):
    DISCOVER_AND_FREEZE = "discover_and_freeze"
    CLI_DEFAULT = "cli_default"
    EXPLICIT = "explicit"
```

内置定义：

```python
AGENT_CATALOG = {
    AgentName.CODEX: AgentDefinition(
        name=AgentName.CODEX,
        display_name="Codex",
        default_command="codex",
        model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
    ),
    AgentName.CLAUDE: AgentDefinition(
        name=AgentName.CLAUDE,
        display_name="Claude",
        default_command="claude",
        model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
    ),
    AgentName.QODER: AgentDefinition(
        name=AgentName.QODER,
        display_name="Qoder",
        default_command="qodercli",
        model_policy=ModelPolicy.CLI_DEFAULT,
    ),
    AgentName.OPENCODE: AgentDefinition(
        name=AgentName.OPENCODE,
        display_name="OpenCode",
        default_command="opencode",
        model_policy=ModelPolicy.DISCOVER_AND_FREEZE,
        supports_variant=True,
    ),
}
```

必须增加注册完整性测试：

```python
assert set(AGENT_CATALOG) == set(AgentName)
assert set(ADAPTER_FACTORIES) == set(AgentName)
assert set(HEALTH_STRATEGIES) == set(AgentName)
```

这样新增枚举但漏掉 Adapter、健康检查或展示定义时，测试会立即失败。

## 7. 统一 Adapter 接口

### 7.1 协议接口

Runtime 不再返回 `CodexAdapter | ClaudeAdapter | QoderAdapter` 联合类型，改为协议：

```python
class AgentStreamParser(Protocol):
    """把一个 CLI 的增量输出映射为统一安全事件。"""

    def feed(self, output: ProcessOutput) -> tuple[AdapterStreamEvent, ...]:
        ...


class AgentAdapter(Protocol):
    """定义所有内置 CLI Adapter 必须实现的能力。"""

    agent: AgentName

    def create_stream_parser(
        self,
        *,
        workspace: Path | None = None,
    ) -> AgentStreamParser:
        ...

    def build_invocation(
        self,
        *,
        worktree: Path,
        prompt: str,
        session_id: str | None,
    ) -> Invocation:
        ...

    def extract_terminal(self, result: ProcessResult) -> VendorTerminal:
        ...

    def failure_detail(self, stdout: str) -> str | None:
        ...
```

### 7.2 Adapter 创建上下文

```python
@dataclass(frozen=True, slots=True)
class AdapterContext:
    schema_path: Path
    mcp_mode: Literal["minimal", "full"]
    unrestricted_commands: bool
    codex_resume_sandbox_verified: bool
    command_policy: CommandPolicy
```

### 7.3 Adapter Registry

```python
ADAPTER_FACTORIES: Mapping[AgentName, AdapterFactory] = {
    AgentName.CODEX: build_codex_adapter,
    AgentName.CLAUDE: build_claude_adapter,
    AgentName.QODER: build_qoder_adapter,
    AgentName.OPENCODE: build_opencode_adapter,
}
```

`DebateRuntime._adapter()` 最终只负责查表：

```python
factory = ADAPTER_FACTORIES[agent]
return factory(profile, context)
```

## 8. Invocation 环境覆盖

OpenCode 需要通过环境变量注入运行期配置。扩展现有 `Invocation`：

```python
@dataclass(frozen=True, slots=True)
class Invocation:
    argv: tuple[str, ...]
    cwd: Path
    stdin: str
    timeout_seconds: int
    startup_timeout_seconds: int
    idle_timeout_seconds: int
    hard_timeout_seconds: int
    heartbeat_interval_seconds: int
    session_id: str | None
    environment_overrides: tuple[tuple[str, str], ...] = ()
```

环境合并顺序：

1. 读取 Run 冻结的环境白名单；
2. 加入代理和证书白名单值；
3. 加入 Adapter 生成的受控环境覆盖；
4. 拒绝不属于该 Agent 允许集合的覆盖变量；
5. 不把环境值写入公开日志、状态页或审计摘要。

OpenCode Adapter 只允许生成以下变量：

```text
OPENCODE_CONFIG_CONTENT
OPENCODE_DISABLE_AUTOUPDATE
OPENCODE_DISABLE_DEFAULT_PLUGINS
```

页面和普通运行配置不提供任意 `environment_overrides` 输入，避免把注册机制变成环境变量
注入通道。

## 9. OpenCode CLI Adapter

### 9.1 默认调用

本机设计核验版本为 OpenCode `1.18.3`。默认调用形态：

```bash
opencode run \
  --format json \
  --dir <workspace> \
  --model <provider/model> \
  --agent build \
  --auto \
  --pure \
  [--variant <variant>] \
  [--session <session-id>]
```

Prompt 通过 stdin 传入，不进入 argv。OpenCode 当前 CLI 支持 `--format json`、
`--session`、`--model`、`--variant`、`--dir` 和 `--auto`，实现时仍必须通过健康检查
核验当前安装版本的真实参数：

- <https://dev.opencode.ai/docs/cli/>

### 9.2 Session 规则

1. 第一次调用不传 `--session`；
2. 从返回事件提取首次创建的 `sessionID`；
3. 将 Session ID 写入现有 `AgentSessionState`；
4. 同一 Run 后续调用始终传 `--session <id>`；
5. 不使用 `--continue`，避免续接其他任务最近的 Session；
6. 不使用 `--fork`，保证复用首次 Session；
7. Session 不存在或不一致时标记为 `LOST`，创建新 generation，并重新注入完整权威记忆。

Writer、Reviewer、终审均使用 OpenCode 的 `build` Agent。是否允许写入由 Supervisor
租约和工作区指纹决定，不在同一原生 Session 内切换 `plan`/`build` 语义。

### 9.3 JSON 事件映射

| OpenCode JSON 类型 | 统一事件 | 页面用途 |
| --- | --- | --- |
| `step_start` | `STARTED` | 显示开始处理 |
| `tool_use` | `TOOL` | 显示命令、读取、编辑等有限摘要 |
| `text` | `MESSAGE` | 显示阶段文本或最终响应摘要 |
| `error` | `ERROR` | 显示脱敏错误类别 |
| `step_finish` | `PROGRESS` | 更新活动时间和步骤状态 |

`step_finish` 不能直接视作整个调用结束，因为一次调用可能包含多个模型和工具步骤。
最终成功必须同时满足：

- 进程正常退出；
- 没有有效 `error` 终止事件；
- 所有事件中的 `sessionID` 一致；
- 存在非空的最终 `text`；
- 最终文本能解析成当前阶段协议对象。

### 9.4 最终协议

`--format json` 只保证 OpenCode 事件流是 JSON，不保证模型最终回答满足辩论协议。
Adapter 继续采用 Qoder 的协议策略：

```text
公共业务 Prompt
+ 当前 Agent 身份
+ 当前写租约状态
+ Peer memory 更新
+ 当前 Git HEAD 和变更范围
+ 当前阶段 JSON Schema
```

解析顺序：

1. 流式读取完整 JSONL；
2. 校验 `sessionID` 一致；
3. 收集完整的 `text.part.text`；
4. 取最后一个非空文本；
5. 提取其中的 JSON 对象；
6. 使用当前 Pydantic 协议模型严格验证；
7. 记录协议错误时不泄露完整 Prompt 或未脱敏输出。

格式重试继续遵循现有门禁：

- 最多一次；
- 只有工作区没有变化时才允许；
- 已经发生文件修改时不得盲目重放；
- stdout 内存窗口截断时，从完整 raw spool 读取，而不是直接报截断。

### 9.5 变更路径

OpenCode `tool_use` 事件中的文件路径只用于页面实时提示。Adapter 必须：

- 丢弃工作区外绝对路径；
- 丢弃包含 `..` 的路径；
- 只返回相对工作区的安全路径；
- 对重复路径去重；
- 限制事件中的路径数量和文本长度。

最终权威变更范围始终来自 Supervisor 执行的 Git diff，不能依赖模型或厂商事件声明。

## 10. OpenCode 完全授权

### 10.1 运行期配置

通过 `OPENCODE_CONFIG_CONTENT` 注入：

```json
{
  "permission": "allow",
  "share": "disabled",
  "snapshot": false,
  "autoupdate": false
}
```

同时使用 `--auto`：

- `permission: allow` 允许工具、命令和编辑；
- `--auto` 自动批准原本需要询问的权限；
- `share: disabled` 防止意外公开 Session；
- `snapshot: false` 关闭 OpenCode 自身快照，统一使用 Supervisor Git 检查点；
- `autoupdate: false` 防止 Run 中途升级 CLI；
- `--pure` 禁止外部插件影响运行边界。

OpenCode 官方说明 `--auto` 不覆盖显式 deny，`permission: "allow"` 可以设置全局允许：

- <https://opencode.ai/docs/permissions/>

组织级 Managed Config 仍可能覆盖运行期配置。健康预检必须检查最终解析结果；如果仍存在
会导致无交互运行被阻断的显式 deny，应在进入工作空间前失败，而不是执行中途等待用户。

### 10.2 完全授权不等于获得编排权限

OpenCode 可以运行任意命令和修改文件，但仍受 Supervisor 事务边界约束：

- Agent 调用前后的 Git HEAD 必须一致；
- Agent 不得自行 `git commit`、`git reset`、切换分支或 push；
- Writer 只能留下未提交工作区变化；
- Reviewer 和终审不能留下任何工作区变化；
- Writer 改动必须位于有效 scope；
- 写入结束后只有 Supervisor 可以创建提交；
- OpenCode 调用的整个进程树均归属于同一写租约和 Agent 身份。

如果 OpenCode 自行改变 HEAD，应分类为策略违规并停止自动继续，不能把该提交当作合法
Supervisor 检查点。

## 11. MCP 隔离

OpenCode 配置文件采用合并语义，传入空的 `mcp` 对象不能保证清除全局或项目 MCP：

- <https://opencode.ai/docs/config/>

### 11.1 minimal 模式

1. 使用受限输出运行 `opencode debug config`；
2. 在内存中解析配置，只提取 MCP 名称；
3. 不持久化或显示原始 resolved config；
4. 为每个已配置 MCP 生成 `enabled: false` 覆盖；
5. 合并到 `OPENCODE_CONFIG_CONTENT`；
6. 使用相同覆盖再次解析配置；
7. 确认最终启用 MCP 数为零；
8. 配合 `--pure` 禁止外部插件。

示意覆盖：

```json
{
  "mcp": {
    "server_a": {"enabled": false},
    "server_b": {"enabled": false}
  }
}
```

`opencode debug config` 可能包含 provider 配置或环境变量替换结果，因此：

- stdout 设置严格大小上限；
- 原始内容不进入 raw 调用日志、健康报告、状态页或异常文本；
- 只保存 MCP 数量、名称哈希和最终启用数量；
- 解析完成后立即丢弃原文。

### 11.2 full 模式

full 模式保留用户的 MCP 配置。健康检查执行 `opencode mcp list`，记录启用数量和状态；
可选 MCP 失败是否阻断沿用现有 Run 配置和健康检查规则。

## 12. 健康策略注册机制

### 12.1 接口

```python
class CliHealthStrategy(Protocol):
    """描述一个 CLI 的发现、能力、认证和模型探针。"""

    def discover_model(...) -> str | None:
        ...

    def required_features(...) -> tuple[str, ...]:
        ...

    def auth_check(...) -> CliCommandResult:
        ...

    def prepare_environment(...) -> PreparedEnvironment:
        ...

    def build_probe(...) -> ProbeInvocation:
        ...

    def validate_probe(...) -> bool:
        ...
```

```python
HEALTH_STRATEGIES = {
    AgentName.CODEX: CodexHealthStrategy(),
    AgentName.CLAUDE: ClaudeHealthStrategy(),
    AgentName.QODER: QoderHealthStrategy(),
    AgentName.OPENCODE: OpenCodeHealthStrategy(),
}
```

`CliHealthChecker` 保留通用顺序、错误模型、熔断和报告生成；厂商差异由 Strategy 提供。

### 12.2 OpenCode 预检顺序

1. 使用 `shutil.which` 或绝对路径定位 `opencode`；
2. 读取并冻结版本；
3. 检查 `opencode run --help` 是否包含必需参数；
4. 从 resolved config 发现默认模型；
5. 最终模型必须冻结为 `provider/model`；
6. 检查目标 provider 的认证状态；
7. 构造完全授权、禁止分享、禁止快照和 MCP 隔离覆盖；
8. 验证运行期覆盖后的 resolved config；
9. 在临时 Git 目录运行只读 JSON 模型探针；
10. 检查退出码、`sessionID` 和最终 `text`；
11. 尝试删除健康探针产生的临时 Session；
12. 冻结可执行文件、版本、模型、variant 和运行配置摘要哈希。

未选择 OpenCode 时，不执行其路径、认证、模型、配置或 MCP 检查。

### 12.3 模型策略

页面允许 OpenCode 模型留空，但真正启动前必须解析并冻结为显式 `provider/model`：

- 优先使用用户填写的模型；
- 否则读取 OpenCode resolved config 中的默认模型；
- 无法发现时阻止启动；
- 不从模型列表中任意选择第一个模型；
- 不允许运行中自动切换 provider 或 model；
- `variant` 可留空，填写后与模型一起冻结。

`opencode models` 可用于帮助用户查看候选模型，但不作为自动选择依据。

## 13. 配置模型

扩展 `AgentName`：

```python
class AgentName(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    QODER = "qoder"
    OPENCODE = "opencode"
```

扩展 `AgentProfile`：

```python
class AgentProfile(BaseModel):
    command: str
    model: str | None = None
    variant: str | None = None
    version: str | None = None
    timeout_seconds: int = 3600
    startup_timeout_seconds: int = 300
    idle_timeout_seconds: int = 300
    hard_timeout_seconds: int = 3600
    heartbeat_interval_seconds: int = 5
```

默认 Profile 从 Catalog 生成：

```python
def default_profiles() -> dict[AgentName, AgentProfile]:
    return {
        definition.name: AgentProfile(command=definition.default_command)
        for definition in agent_definitions()
    }
```

兼容规则：

- 历史配置没有 OpenCode Profile 仍可恢复；
- 只有选择 OpenCode 时才要求存在其 Profile；
- 历史配置缺少 `participants` 时仍迁移为 Codex + Claude；
- 默认参与者仍为 Codex + Claude；
- `first_agent` 仍必须属于已选参与者；
- 参与者列表在 Run 创建后冻结；
- `max_turns=8` 语义保持为全局写入回合上限，不按人数自动强制增加。

选择四个参与者时，默认八个写入回合约等于两轮完整接力。页面可以提示该关系，但不
自动改变用户配置。

## 14. CLI 改造

CLI 参数根据 Catalog 循环生成，保留现有公开参数兼容性：

```text
--participant opencode
--opencode-command
--opencode-model
--opencode-variant
--opencode-timeout-seconds
--opencode-startup-timeout-seconds
--opencode-idle-timeout-seconds
--opencode-hard-timeout-seconds
--opencode-heartbeat-interval-seconds
```

通用参数按定义循环添加：

```python
for definition in agent_definitions():
    add_command_argument(definition)
    add_model_argument(definition)
    if definition.supports_variant:
        add_variant_argument(definition)
    add_timeout_arguments(definition)
```

本次不增加通用的 `--agent-option key=value`，避免任意厂商参数绕过 Adapter 的参数和
安全校验。

## 15. Streamlit 页面

### 15.1 发起任务

- 参与者多选增加 OpenCode；
- 默认仍选择 Codex 和 Claude；
- 至少选择两个参与者；
- 首位 Writer 下拉框只显示已选参与者；
- 高级设置根据 Catalog 渲染 Agent 配置卡；
- 四个内置 Agent 使用两列布局；
- OpenCode 卡显示命令、模型、variant；
- 只有 `supports_variant=True` 时显示 variant 输入；
- 未选中的 Agent Profile 可以展示，但不进行健康检查。

OpenCode 默认命令使用 `shutil.which("opencode")` 自动发现绝对路径。模型输入帮助文本
明确要求 `provider/model`；留空时由预检尝试发现，而不是在页面渲染时运行模型请求。

### 15.2 实时页面

`InvocationLiveView` 改为动态轨道：

```python
class InvocationLiveView(BaseModel):
    run_id: str
    after_seq: int
    last_seq: int
    events: tuple[InvocationEvent, ...] = ()
    tracks: dict[AgentName, AgentLiveTrack]
    warning: str | None = None
```

页面使用：

```python
track = live.tracks[agent]
```

不再维护固定的 `live.codex`、`live.claude`、`live.qoder` 字段。Tab、进度 Lane、原始
输出和错误提示全部按 `config.participants` 生成。

## 16. Prompt 与记忆

OpenCode 不使用独立业务 Prompt。所有 Agent 继续共享相同的阶段合同，仅由 Runtime
注入当前 Agent 身份和状态：

- 当前目标和验收条件；
- 当前 Agent 身份；
- 本轮是 Writer、Reviewer、Ballot 还是 Tree Ack；
- 当前 HEAD 和最近提交；
- 尚未确认的 Peer memory 更新；
- 上一个 Agent 的变更路径和行为摘要；
- 是否持有写租约；
- 禁止自行提交和改变 HEAD；
- 最终输出协议 Schema。

OpenCode 写入并由 Supervisor 提交后：

1. 生成一次权威 Git diff 和变更摘要；
2. 为每个其他参与者创建独立的 pending memory update；
3. 记录 source agent 为 `opencode`；
4. 其他参与者调用前先读取当前 HEAD 和变化全文；
5. Prompt 再注入面向该目标 Agent 的 pending 摘要；
6. 目标 Agent 返回确认后更新自己的 ack 游标。

OpenCode 收取其他参与者更新时使用同一机制，不引入 OpenCode 专用记忆文件。

## 17. 调度与完成条件

OpenCode 不获得特殊优先级：

- 谁先写由 `first_agent` 或 Facilitator 建议决定；
- 确定性回退继续在未完成参与者之间轮转；
- Facilitator 的 `recommended_next_writer` 只能从已选参与者中选择；
- OpenCode 的一次 CLI 调用即使内部启动子 Agent，仍归属于 OpenCode 这一参与者；
- 同一写租约覆盖 OpenCode 的整个进程树；
- 任何参与者返回 `incomplete` 或不接受，都不能进入完成状态；
- 最终 Ballot 和 Tree Ack 的 required agents 等于已冻结的参与者集合。

Supervisor 仍不根据观点内容决定胜负，只检查状态、协议、Git、验证和全体确认是否满足。

## 18. Schema 与审计

四份静态协议 Schema 的 Agent 枚举增加：

```json
["codex", "claude", "qoder", "opencode"]
```

本次不引入运行时 Schema 生成，避免扩大改造范围。增加测试保证每份 Schema 中的 Agent
枚举与 `AgentName` 完全一致。

审计展示名从 Catalog 获取，最终摘要增加：

```markdown
## OpenCode 贡献（检查点）
```

运行审计保存：

- OpenCode Session ID 的脱敏引用；
- CLI 绝对路径、版本、provider/model 和 variant；
- 运行配置摘要哈希；
- 调用开始、活动、终止和错误分类；
- Supervisor 创建的 `agent:opencode` Git 检查点；
- OpenCode 的贡献、争议、限制、最终 Ballot 和 Tree Ack。

最终集成继续只保留聚焦后的结果提交。中间提交不进入目标分支长期历史，讨论依据保留在
结果内的审计摘要中。

## 19. 错误与恢复

| 故障 | 分类 | 自动动作 |
| --- | --- | --- |
| OpenCode 不存在 | configuration error | 预检阻止启动 |
| 版本缺少必需参数 | cache incompatible | 预检阻止启动 |
| 模型无法发现 | configuration error | 要求显式配置模型 |
| provider 未认证 | authentication error | 预检阻止启动 |
| 权限仍含显式 deny | policy/configuration error | 预检阻止启动 |
| minimal 模式仍有 MCP 启用 | MCP/configuration error | 预检阻止启动 |
| Session 不存在 | session lost | 创建新 generation 并重放权威记忆 |
| Session ID 在单次调用中变化 | session boundary error | 终止调用，不接受结果 |
| 输出无最终 text | protocol error | 工作区干净时有限重试 |
| 最终 JSON 不合法 | protocol error | 工作区干净时一次格式重试 |
| CLI 长时间无活动 | idle timeout | 终止进程组并进入现有恢复流程 |
| CLI 超过单次上限 | hard timeout | 终止进程组并保存可归属改动 |
| Writer 修改越界 | policy violation | 拒绝提交并进入恢复/阻断 |
| Reviewer 修改工作区 | policy violation | 拒绝结果并恢复干净状态 |
| Agent 改变 HEAD | policy violation | 阻断自动继续，不能当作合法提交 |

OpenCode 返回权限询问事件但没有被自动处理时，不等待用户输入，应视为配置或交互要求
错误。讨论运行的默认目标仍是中间不需要用户参与。

## 20. 文件改造边界

| 文件 | 计划变更 |
| --- | --- |
| `src/debate_agent/agent_catalog.py` | 新增内置 Agent 元数据 Catalog |
| `src/debate_agent/models.py` | 增加 `AgentName.OPENCODE` |
| `src/debate_agent/config.py` | Catalog 默认 Profile、`variant` 和兼容校验 |
| `src/debate_agent/adapters.py` | Adapter Protocol、Registry、OpenCode Adapter/Parser |
| `src/debate_agent/runtime.py` | 通过 Registry 创建 Adapter，合并受控环境 |
| `src/debate_agent/cli_health.py` | Health Strategy Registry 和 OpenCode 预检 |
| `src/debate_agent/cli.py` | 按 Catalog 生成参数和 Profile |
| `src/debate_agent/ui/app.py` | 动态 Agent 配置卡、Tab 和实时轨道 |
| `src/debate_agent/ui/view_models.py` | `RunDraft.profiles` 和动态 `tracks` |
| `src/debate_agent/audit.py` | Catalog 显示名和 OpenCode 贡献 |
| `schemas/*.schema.json` | 增加 `opencode` 枚举 |
| `tests/` | 注册完整性、Adapter、健康、UI、Runtime 和 E2E 测试 |
| `README.md` / `USER_GUIDE.md` | 实施完成后补充安装和使用说明 |
| `CLI_AGENT_EXECUTION_DEFAULTS.md` | 实施完成后记录 OpenCode 冻结参数 |

不修改 LangGraph 的核心完成语义，不清理被静态工具误判为“无调用者”的图节点。LangGraph
节点通过图注册间接调用，不能按普通 Python caller 分析删除。

## 21. 分阶段实施

### 阶段 A：引入 Catalog，保持三方行为不变

提交建议：

```text
refactor(discuss): introduce built-in agent catalog
```

内容：

- Catalog 仅注册 Codex、Claude、Qoder；
- 默认 Profile、显示名和 CLI 参数读取 Catalog；
- 增加注册完整性测试；
- 不增加 `AgentName.OPENCODE`；
- 所有现有测试必须保持通过。

### 阶段 B：注册 Adapter 和健康策略

提交建议：

```text
refactor(discuss): register adapters and health strategies
```

内容：

- 引入 `AgentAdapter` 和 `CliHealthStrategy` Protocol；
- 迁移三方 Adapter factory；
- 迁移三方健康检查；
- Runtime 不再使用 Adapter 联合返回类型；
- 三方调用 argv、Session 和预检结果必须与迁移前一致。

### 阶段 C：动态 CLI、UI、审计和实时视图

提交建议：

```text
refactor(discuss): make agent surfaces catalog-driven
```

内容：

- CLI 参数和 Profile 按 Catalog 构建；
- `RunDraft` 不再保存固定 Agent 字段；
- `InvocationLiveView` 改为动态 tracks；
- 页面配置卡、Tab、Lane 和审计显示名动态化；
- 仍只注册现有三方，确保这是纯重构。

### 阶段 D：接入 OpenCode

提交建议：

```text
feat(discuss): add opencode cli participant
```

内容：

- 增加 `AgentName.OPENCODE` 和 Catalog 定义；
- 增加 OpenCode Adapter、JSON parser 和 Session 续接；
- 增加受控环境覆盖；
- 增加完全授权、禁止分享、禁止快照和 MCP 隔离；
- 增加 OpenCode 健康策略；
- 更新 Schema 和测试。

### 阶段 E：端到端验收和用户文档

提交建议：

```text
docs(discuss): document opencode setup and execution
```

内容：

- 更新 README、USER_GUIDE 和默认参数文档；
- 记录 OpenCode 安装、认证、模型选择和故障排查；
- 执行四 Agent 组合 Smoke Test；
- 保存最终测试和审计证据。

## 22. 测试计划

### 22.1 Catalog

- Catalog、Adapter Registry、Health Registry 与 `AgentName` 键集合一致；
- 显示名、默认命令和能力字段完整；
- 重复定义和未知 Agent 失败；
- CLI 参数循环保留已有选项名和默认值。

### 22.2 OpenCode Adapter

- 首次调用 argv 不含 `--session`；
- 首次调用从事件中提取 Session ID；
- 后续调用使用相同 `--session`；
- Prompt 只进入 stdin；
- model 和 variant 正确传递；
- `--format json`、`--dir`、`--agent build`、`--auto`、`--pure` 存在；
- `step_start`、`tool_use`、`text`、`error`、`step_finish` 映射正确；
- 路径过滤阻止工作区外路径和 `..`；
- 多个不同 Session ID 被拒绝；
- 最终 text 缺失被拒绝；
- 厂商错误可提取脱敏详情；
- 截断内存输出后可以从完整 raw 文件解析终局协议。

### 22.3 健康检查

- 未安装 OpenCode 时，选择 OpenCode 会阻止启动；
- 未选择 OpenCode 时不执行任何 OpenCode 命令；
- 缺少必需参数的版本被拒绝；
- 默认模型可发现并冻结；
- 无模型时阻止启动；
- provider 未认证时阻止启动；
- 权限覆盖后仍有 deny 时阻止启动；
- minimal MCP 的最终启用数为零；
- full MCP 保留用户配置；
- 探针验证 Session ID 和最终 text；
- 健康检查不把 resolved config 原文写入日志。

### 22.4 Runtime 和记忆

- Codex + OpenCode 可以运行；
- Claude + OpenCode 可以运行；
- Qoder + OpenCode 可以运行；
- 四个 Agent 可以同时参与；
- OpenCode 写入后 Supervisor 创建正确 Agent 标识提交；
- 所有其他 Agent 收到 OpenCode 的独立 memory update；
- OpenCode 收到其他所有参与者更新；
- OpenCode Session 丢失后重建 generation 并重放记忆；
- OpenCode 自行改变 HEAD 被拒绝；
- Reviewer 修改工作区被拒绝；
- 任一参与者 `incomplete` 时不能完成；
- 四方都 `complete`、接受并确认 final tree 后才能完成。

### 22.5 UI 和 CLI

- 页面默认仍选择 Codex + Claude；
- OpenCode 出现在参与者列表；
- 至少选择两个参与者；
- OpenCode 卡显示命令、模型和 variant；
- 未选择 OpenCode 时不阻断启动；
- 实时 Tab 数量和顺序等于参与者集合；
- `tracks[AgentName.OPENCODE]` 正确展示事件；
- CLI 支持重复 `--participant opencode` 组合；
- CLI 和 UI 生成相同的 OpenCode Profile。

### 22.6 回归门禁

每个阶段至少执行：

```bash
cd agent/debate
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/python -m build
```

实现阶段应先运行涉及模块的聚焦测试，再运行全量门禁。

## 23. 端到端验收矩阵

| 组合 | 要求 |
| --- | --- |
| Codex + Claude | 默认行为完全不变 |
| Codex + Qoder | 已有 Qoder 行为不变 |
| Codex + OpenCode | OpenCode Session、写入、提交、Peer memory 正常 |
| Claude + OpenCode | 两方均可轮流写入和审查 |
| Qoder + OpenCode | 两个 Prompt 校验型 Adapter 均可稳定返回协议 |
| Codex + Claude + Qoder + OpenCode | 四方调度、广播、Ballot 和 Tree Ack 正常 |

端到端成功标准：

- 页面持续显示所有选中 CLI 的实时事件；
- 没有权限询问或人工确认导致的中断；
- 任意时刻只有一个 Writer；
- 每次写入均由 Supervisor 提交并标记来源 Agent；
- 下一 Agent 能读取上一提交的完整变化；
- 四方均判断目标完成并确认同一 final tree；
- 目标分支只得到聚焦结果；
- 结果中保留审计摘要。

## 24. 已锁定设计决策

| 编号 | 决策 |
| --- | --- |
| OC-D1 | 采用内置 Catalog，不实现运行时插件系统 |
| OC-D2 | OpenCode 使用 `opencode run` 子进程，不接 SDK/Server |
| OC-D3 | 默认参与者仍为 Codex + Claude，OpenCode 主动选择 |
| OC-D4 | 同一 Run 内复用首次创建的 OpenCode Session |
| OC-D5 | OpenCode 固定使用 `build` Agent，写权限由 Supervisor 控制 |
| OC-D6 | OpenCode 默认完全授权并禁止交互审批 |
| OC-D7 | OpenCode 不得自行提交，Git 仍由 Supervisor 管理 |
| OC-D8 | OpenCode 模型在启动前冻结为显式 `provider/model` |
| OC-D9 | `variant` 可选，填写后随 Run 冻结 |
| OC-D10 | minimal MCP 必须验证最终启用数为零 |
| OC-D11 | 最终协议继续使用 Prompt Schema + Pydantic 校验 |
| OC-D12 | 完成条件仍为所有已选 Agent 接受且均判断目标完成 |
| OC-D13 | 不自动按参与者数量调整 `max_turns` |
| OC-D14 | 最终目标分支不保留零散过程提交，审计随结果保存 |

## 25. 验收清单

- [ ] 原 Codex + Claude 默认行为不变；
- [ ] 原 Qoder 组合行为不变；
- [ ] OpenCode 首轮创建 Session，后续复用同一 Session；
- [ ] 任意至少两个内置 Agent 可以组合；
- [ ] OpenCode 完全授权模式不产生审批等待；
- [ ] OpenCode 不能绕过 Supervisor 改变 HEAD；
- [ ] OpenCode 写入后其他 Agent 能读取完整 Git 变化；
- [ ] OpenCode 能收到所有其他 Agent 的更新；
- [ ] minimal MCP 下 OpenCode 最终启用 MCP 数为零；
- [ ] OpenCode 未安装但未被选择时不影响启动；
- [ ] 页面显示 OpenCode 实时事件和原始输出尾部；
- [ ] 所有已选 Agent 均接受且均判断目标完成后才能结束；
- [ ] 四 Agent 组合完成一次端到端 Smoke Test；
- [ ] 不新增 OpenCode SDK 或 Node 运行依赖；
- [ ] README、USER_GUIDE 和默认参数文档在实现完成后同步更新。
