# 辩论 Agent 执行稳定性与 CLI 实时输出优化方案

本文定义 `debate-agent` 的下一阶段稳定性改造。目标是在不改变“双 Agent 自主讨论、
Supervisor 只负责确定性编排”的前提下，让长时间 CLI 调用可观察、可区分故障、可
安全恢复，并避免仍在有效工作的 Agent 被固定墙钟超时误杀。

> 状态：P0 已于 2026-07-21 按
> [P0_IMPLEMENTATION_TODO.md](P0_IMPLEMENTATION_TODO.md) 完成；P1 自动中断检查点与
> 权威活动、P2 InvocationRuntime 职责重构仍未实施。文中的“当前”描述改造前基线，
> “目标”保留完整分阶段设计。

## 1. 背景与问题

`RUN-20260721-025132-2cf22f01` 暴露了两个相互关联的问题：

1. `ensure_sessions` 中首次 Codex 调用没有产生有效模型结果，等待完整的 1200 秒后
   才超时；期间页面只显示节点开始。
2. 第 1 个 Writer 回合实际执行了 15 条命令和 18 次文件编辑，并在超时前持续输出，
   但固定的 1200 秒墙钟时限仍终止了进程。最终结构化结果没有返回，Run 失败，脏改动
   和 active 写租约留给恢复流程处理。

现场还出现了以下外部依赖错误：

- Codex 模型缓存缺少 `supports_reasoning_summaries` 字段；
- 模型列表刷新超时；
- ChatGPT responses WebSocket TLS 握手失败；
- ChatGPT MCP 与 AnySearch MCP 初始化失败；
- Agent 自行生成的验证命令直接比较中文 Git 路径，受 `core.quotepath` 影响而误判。

这些现象说明，单纯调大 `AgentProfile.timeout_seconds` 不能解决问题。系统需要区分
“没有有效活动”“仍在正常工作”“基础设施不可用”和“已经写入但没有返回协议”四种
状态，并对每种状态采取不同动作。

## 2. 目标与非目标

### 2.1 目标

- 页面以 1～2 秒延迟展示 Codex 和 Claude 的脱敏实时事件；
- 只有持续无有效活动的调用才触发空闲超时；
- 活跃调用允许超过旧的 1200 秒，但仍受单次硬上限和 Run 总预算限制；
- 网络、缓存、MCP、协议、验证和越界写入产生不同的错误类别；
- 可唯一归属的中断写入自动保存为恢复检查点，不丢失也不直接视为完成；
- Worker 崩溃或 CLI 超时后，系统可以在不要求用户参与的情况下安全接力；
- Prompt、密钥、环境变量和 Session 凭据不进入页面实时事件；
- 保留唯一 Writer、Git 归属校验、双 Agent 完成判断和最终聚焦提交等现有不变量。

### 2.2 非目标

- Supervisor 不根据 CLI 输出判断业务方案谁对谁错；
- 不因为 CLI 超时而跳过 Codex 或 Claude 的最终完成确认；
- 不自动 push、发布或部署；
- 不把运行期 raw 日志合并进最终目标分支；
- 不默认把未脱敏的完整终端输出公开给远程用户；
- 不用无限超时掩盖模型服务或 MCP 故障。

## 3. 必须保留的系统不变量

1. 同一时刻只有一个 Agent 持有 active 写租约。
2. CLI 写入必须位于 GoalContract 的有效范围内。
3. Supervisor 负责租约、Git 检查点、验证、恢复和审计，不负责业务裁决。
4. 不确定的 Prompt 不盲目重放。
5. 双方都判断完整目标完成、最终验证通过并确认同一 final tree 后才能结束。
6. 目标分支只得到一个聚焦结果提交，中间恢复提交只存在于 Debate 分支历史。

## 4. 改造前实现与主要缺口

| 能力 | 当前实现 | 缺口 |
|---|---|---|
| CLI 输出 | Codex 使用 JSONL；Claude 使用单个 JSON | 运行结束前 UI 不消费输出 |
| 子进程管理 | 独立进程组、取消、固定墙钟超时 | 没有启动超时、空闲超时和活动回调 |
| 进度事件 | LangGraph 节点开始/完成时发布 | CLI 内部执行 20 分钟时页面不更新 |
| 重试 | 工作区未变化时重建 Session | 基础设施故障和业务失败没有分类 |
| 脏树恢复 | Resume 时可恢复有唯一归属的脏树 | 失败当下不自动回收租约和保存 WIP |
| CLI 预检 | 检查可执行文件是否存在 | 不验证版本、登录、模型服务、缓存和 MCP |
| UI 配置 | Run 总时限、最大回合等 | 不提供 CLI 路径、模型和分层超时配置 |
| 错误展示 | 最终显示 `CLI 调用超时` 等摘要 | 看不到最近活动、空闲时间和故障类别 |

## 5. 目标执行架构

```text
Streamlit UI
  │  每 1～2 秒增量读取
  ▼
InvocationEventStore ───────► invocation-events.jsonl
  ▲                                  │
  │ 脱敏事件/心跳                     │ 运行审计，不含 Prompt/Key
  │                                  ▼
InvocationRunner ───────────► raw/*.stdout.log / stderr.log
  │
  ├─ Codex JSONL parser
  ├─ Claude stream-json parser
  ├─ useful-activity detector
  ├─ timeout policy
  └─ failure classifier
  │
  ▼
Runtime / Supervisor
  ├─ 唯一写租约
  ├─ Git 范围与指纹校验
  ├─ 正常检查点
  ├─ 中断恢复检查点
  └─ Agent Session 接力
```

原始输出继续落盘，结构化实时事件使用独立存储。这样不会扩大现有 `RunEvent` 的节点
语义，也不会让页面为了显示最后几十条事件而反复读取完整 raw 文件。

## 6. 分层超时策略

### 6.1 建议配置

| 配置 | 建议默认值 | 含义 |
|---|---:|---|
| `startup_timeout_seconds` | 300 | 启动后没有首个有效活动的最大等待时间 |
| `idle_timeout_seconds` | 300 | 两次有效活动之间允许的最大静默时间 |
| `hard_timeout_seconds` | 3600 | 单次 CLI 调用不可突破的墙钟上限 |
| `heartbeat_interval_seconds` | 5 | 运行快照最小刷新间隔 |
| `max_process_failures` | 2 | 同一逻辑调用的基础设施重试上限 |
| `total_timeout_seconds` | 7200 | 整个 Run 的累计活跃时间上限 |

现有 `AgentProfile.timeout_seconds` 在迁移期间作为 `hard_timeout_seconds` 的兼容来源。
新配置缺失时，历史 Run 仍按冻结配置恢复，不能静默改变其执行边界。

### 6.2 有效活动定义

以下事件刷新空闲计时：

- Codex/Claude 产生新的工具开始或完成事件；
- Agent 产生新的文本或结构化消息；
- CLI 报告文件修改；
- 工作区指纹发生变化；
- 最终协议或终止事件到达。

以下内容不刷新空闲计时：

- 相同模型缓存错误的重复输出；
- 相同 MCP 连接错误的重复输出；
- 仅有 `thread.started`、进程仍存活或日志文件 mtime 变化；
- 无业务进展的周期性重连提示。

### 6.3 超时判定

```text
没有首个有效活动达到 300 秒
  → startup_timeout
有过有效活动，但连续静默达到 300 秒
  → idle_timeout
持续活跃但总时长达到 3600 秒
  → hard_timeout
Run 累计活跃时间达到 7200 秒
  → total_timeout
```

固定硬上限仍然存在，避免 Agent 无限扩张任务；但像本次事故中“超时前 24 秒仍在执行
验证”的调用不会再被 1200 秒旧上限误杀。

## 7. CLI 实时输出

### 7.1 Codex

Codex 已使用 `--json` 输出 JSONL。实时解析器需要识别：

- `thread.started`：记录 Session 建立，不展示完整 Session ID；
- `turn.started` / `turn.completed`：标记调用生命周期；
- `item.started` / `item.completed`：工具、命令、文件变更和 Agent 消息；
- `error`：重连、网络和协议错误；
- 最后一个满足 Schema 的 `agent_message`：只交给 Adapter 作为最终协议。

实时展示不能改变最终解析规则。没有合法终止结果时，即使页面看到了阶段性
`agent_message`，该调用仍不能记为成功。

### 7.2 Claude

Claude 从：

```text
--output-format json
```

迁移为：

```text
--output-format stream-json
--include-partial-messages
```

Claude Adapter 同时支持旧的单 JSON 和新的 stream-json，以便历史 Run 恢复和版本
回滚。最终 `result`/`structured_output` 仍必须通过 Pydantic 和目标 JSON Schema 校验。

### 7.3 实时事件模型

建议新增 `InvocationEvent`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `seq` | `int` | Run 内单调递增序号 |
| `timestamp` | `datetime` | UTC 时间 |
| `run_id` | `str` | Run 标识 |
| `invocation_id` | `str` | CLI 调用标识 |
| `agent` | `codex/claude` | 事件来源 |
| `turn_id` | `str` | 业务回合 |
| `event_type` | `str` | 工具、文件、消息、心跳、错误或终止 |
| `title` | `str` | 页面可直接展示的短标题 |
| `summary` | `str` | 最长 500 字符的脱敏摘要 |
| `severity` | `info/warning/error` | 展示级别 |
| `elapsed_seconds` | `float` | 当前调用已运行时间 |
| `idle_seconds` | `float` | 距离上次有效活动的时间 |
| `changed_paths` | `tuple[str, ...]` | 已验证为工作区内的相对路径 |
| `terminal` | `bool` | 是否为调用终止事件 |

存储布局：

```text
.runtime/runs/<run-id>/
├── invocation-events.jsonl
├── invocation-progress.json
├── events.jsonl
├── progress.json
└── raw/
    ├── <invocation>.stdout.log
    └── <invocation>.stderr.log
```

`events.jsonl` 继续表示 LangGraph 节点接力；`invocation-events.jsonl` 表示 CLI 内部
活动。两个流通过 `invocation_id`、`turn_id` 和 `agent` 关联。

### 7.4 增量读取和背压

- Worker 是实时事件的唯一写者，UI 只读；
- JSONL 每行写完后 flush，必要时按批次 fsync；
- UI 保存最后读取的 `seq`，每 1～2 秒只读取新增事件；
- 遇到未完整写入的最后一行时暂时忽略，下次轮询继续；
- 页面默认保留最近 500～1000 条事件；
- raw 日志继续受磁盘保留和 cleanup 生命周期管理；
- UI 不把完整日志一次性装入内存。

## 8. 页面方案

```text
┌ Run 状态：RUNNING ─ writer_turn ─ TURN 1/8 ───────────────┐
│ Codex 已运行 18:32 · 空闲 00:08 · 硬上限剩余 41:28       │
└───────────────────────────────────────────────────────────┘

┌ Codex CLI ─────────────────┐  ┌ Claude CLI ──────────────┐
│ 当前：验证方案文档          │  │ 等待接力                  │
│ 命令 15 · 文件修改 18       │  │ 最近确认：HEAD 92dc...    │
│ 最近错误：模型缓存兼容警告  │  │                           │
└────────────────────────────┘  └───────────────────────────┘

[进度摘要] [Codex 实时] [Claude 实时] [原始输出] [验证/结论]
```

页面行为：

- 继续使用现有 `@st.fragment(run_every=2)` 局部刷新；
- Agent 卡片展示运行时间、空闲时间、事件数、修改文件数和最近活动；
- 实时标签按时间倒序或自动跟随最新事件；
- 错误按基础设施、协议、验证、策略四类显示；
- 原始输出默认折叠，只展示脱敏后的尾部内容；
- 使用 `st.code`/文本组件输出终端内容，不用 `unsafe_allow_html` 渲染模型文本；
- 页面刷新或切换 Run 后可以从落盘序号恢复，不依赖浏览器内 Session 状态；
- Run 停止后仍可查看最后事件、超时原因和恢复建议。

## 9. 安全与脱敏

实时事件默认不得包含：

- Prompt 全文和 GoalContract 原始 JSON；
- DeepSeek Key、代理凭据和全部环境变量；
- Codex/Claude 完整 Session ID；
- 授权 Header、Cookie、Token 和私钥；
- 命令 stdin；
- 目标工作区之外的敏感绝对路径；
- 未经裁剪的工具输出。

建议采用两层处理：

1. Adapter 将厂商事件转换为受限字段模型，未知字段默认丢弃；
2. 统一 Redactor 对标题、摘要和命令预览执行密钥模式、环境变量名、Authorization、
   Cookie 和 URL 凭据遮罩。

命令预览只保留可执行文件、子命令和有限参数；单条摘要最多 500 字符。完整 raw 日志
仍只存在于本机状态目录，cleanup 时按现有生命周期删除，不进入最终审计提交。

## 10. CLI 健康预检与执行环境

### 10.1 预检顺序

在获取仓库写锁之前完成：

1. 解析 Codex/Claude 的绝对可执行文件；
2. 读取 CLI 版本并与允许范围匹配；
3. 验证登录状态；
4. 用短时限运行最小结构化只读探针；
5. 检查模型缓存是否能被当前 CLI 解析；
6. 检查必需模型服务连接；
7. 记录启用的 MCP 数量和失败项；
8. 只有关键检查通过后才获取仓库锁和写租约。

可执行文件路径、版本、模型和超时策略必须冻结到 Run 配置。不能只记录
`command="codex"`、`model=null`，否则恢复时可能解析到不同版本。

### 10.2 精简 MCP 配置

普通代码和文档任务默认只加载必要的文件、Shell 与 Git 能力。Computer Use、Draw.io、
浏览器、AnySearch 等 MCP 按任务显式启用。单个非关键 MCP 初始化失败只产生 warning，
不能拖住整个模型调用；模型主连接失败则进入基础设施故障处理。

### 10.3 代理与证书环境

当前子进程默认环境白名单只有 `PATH`、`HOME`、`LANG`、`LC_ALL` 和 `TERM`。如果本机
模型服务依赖代理或自定义证书，应允许用户显式加入：

```text
HTTPS_PROXY
HTTP_PROXY
ALL_PROXY
NO_PROXY
SSL_CERT_FILE
SSL_CERT_DIR
```

这些变量只传给 CLI 子进程，不写入配置值、事件、日志或审计文件。页面只记录变量名
是否启用，不展示其值。

## 11. 故障分类、重试与熔断

| 类别 | 例子 | 自动动作 |
|---|---|---|
| `startup_timeout` | 只有 thread.started，无有效响应 | 结束进程；工作区干净时退避重试 |
| `idle_timeout` | 工具执行后长期无新活动 | 结束进程；按工作区是否变化进入重试或恢复 |
| `hard_timeout` | 持续活动但超过单次上限 | 保存可归属 WIP；新 Session 接力 |
| `network_error` | TLS、WebSocket、DNS、连接重置 | 短退避；达到阈值后熔断 |
| `cache_incompatible` | 模型缓存字段不兼容 | 快速失败；提示切换已验证 CLI 版本 |
| `mcp_optional_error` | 非关键 MCP 初始化失败 | warning；不阻断主调用 |
| `protocol_error` | 缺 Session ID、JSON Schema 无效 | 最多一次格式修复 |
| `validation_error` | 权威验证退出非零 | 交回 Agent 修订，不归类为基础设施故障 |
| `policy_violation` | 越界写入、未授权命令 | 立即停止，不重试 |
| `workspace_race` | 只读阶段发生写入 | 立即停止并保留现场 |

相同基础设施错误在短窗口内连续出现时打开熔断器。熔断期间新 Run 在预检阶段直接显示
原因，不再占用仓库锁和完整的 20 分钟调用预算。

## 12. 中断写入的自动恢复

### 12.1 状态机

```text
CLI 异常终止
  │
  ├─ 工作区未变化
  │    ├─ 基础设施错误且预算充足 → 退避后重建 Session
  │    └─ 其他错误              → 停止并报告
  │
  └─ 工作区有变化
       ├─ active 租约 + 唯一 journal + 范围合法
       │    → 撤销租约
       │    → 执行恢复验证
       │    → 创建 interrupted 检查点
       │    → 新 Session 阅读 diff/失败摘要后接力
       └─ 无法唯一归属
            → recovery_blocked，不自动提交
```

建议恢复提交消息：

```text
[debate][codex][TURN-001][interrupted-validation-failed]
```

恢复提交不是业务完成结论。下一代同一 Agent 必须读取完整 diff、恢复验证和失败摘要，
明确接受或修订；另一 Agent 随后仍按正常流程确认新 HEAD。

### 12.2 与现有恢复能力的关系

现有 `RecoveryManager.recover_dirty_checkpoint()` 已校验 active 租约、Invocation Journal、
base commit、Agent、turn、GoalContract 版本和允许路径，并能创建恢复检查点。建议把这套
逻辑从“用户点击恢复后运行”前移到“CLI 异常终止后自动运行”，并保留 Resume 作为
Worker 硬崩溃后的兜底入口。

## 13. Agent 执行预算与 Prompt 约束

每个 Writer Prompt 应明确：

- 本 Run 是非交互执行，不能等待用户逐步批准；
- GoalContract 是本轮目标和范围授权；
- 不使用要求用户现场确认才能继续的 Skill；
- 先消费 ACK、PeerMemory 和已有验证摘要，不重复全量扫描；
- 优先使用 Supervisor 提供的权威验证命令；
- 自行检查不得扩大目标范围；
- 建议在 25 次工具活动内完成，40 次为软警戒线；
- 接近预算或硬上限时先返回合法结构化结果和剩余问题。

工具预算只约束执行规模，不改变业务完成标准。Agent 返回 `incomplete` 后仍可由下一轮
继续，而不是为了赶时间错误声明 complete。

## 14. 配置与页面入口

页面“高级设置”建议新增：

| 字段 | 默认值 | 说明 |
|---|---|---|
| Codex 可执行文件 | 自动解析后的绝对路径 | 冻结到 Run |
| Claude 可执行文件 | 自动解析后的绝对路径 | 冻结到 Run |
| Codex/Claude 模型 | 本机验证后的显式模型 | 不再依赖隐式默认 |
| 首次活动超时 | 300 秒 | 两个 CLI 可分别配置 |
| 空闲超时 | 300 秒 | 两个 CLI 可分别配置 |
| 单次硬上限 | 3600 秒 | 两个 CLI 可分别配置 |
| 基础设施重试 | 2 | 不包括业务修订轮次 |
| 实时输出 | 开启 | 关闭时仍保存 raw 日志 |
| 原始输出尾部行数 | 500 | 页面内存上限 |
| MCP 配置 | 精简 | 可切换为本机完整配置 |
| 代理/证书变量名 | 空 | 只允许白名单名称 |

基础设置页面继续保持简洁；这些字段默认折叠，不要求普通用户每次填写。

## 15. 代码改造边界

建议新增：

- `src/debate_agent/invocation_events.py`：事件模型、JSONL 存储和脱敏；
- `src/debate_agent/invocation_runtime.py`：活动检测、分层超时和故障分类。

建议修改：

- `config.py`：分层超时、显式 CLI 路径/模型及兼容迁移；
- `process.py`：增量读取、活动回调、空闲与硬超时；
- `adapters.py`：Codex 事件映射、Claude stream-json 和双格式兼容；
- `runtime.py`：把调用重试交给 InvocationRuntime，发布心跳并自动触发恢复；
- `recovery.py`：支持异常结束后的自动 interrupted 检查点；
- `events.py`：保持节点事件不变，只增加与 Invocation 进度的关联字段；
- `ui/view_models.py`：实时事件读取和运行指标模型；
- `ui/app.py`：CLI 实时标签、活动指标和错误分类展示。

`runtime.py` 已接近 2000 行。稳定性改造不应继续把解析、超时和事件逻辑塞入该文件；
但也不做无关的全面重构，先抽出 Invocation 相关职责。

## 16. 实施顺序

### 阶段 A：实时事件基础

1. 定义 `InvocationEvent`、`InvocationProgress` 和脱敏规则；
2. 为 Codex JSONL 建立增量解析器；
3. 为 Claude 增加 stream-json 解析并兼容旧 JSON；
4. 新增 JSONL Store 和尾部增量读取；
5. 保持最终 Adapter 校验行为不变。

### 阶段 B：活动感知超时

1. 为 ProcessRunner 增加活动回调；
2. 实现启动、空闲、硬上限三个计时器；
3. 对重复错误去重，避免伪活动延长调用；
4. 记录终止类型和最后有效活动；
5. 保留取消与进程组清理能力。

### 阶段 C：自动恢复与故障分类

1. 建立错误分类器和熔断器；
2. 工作区干净时执行有限基础设施重试；
3. 工作区有变更时自动创建 interrupted 检查点；
4. 新 Session 接收恢复摘要，不盲目重放 Prompt；
5. 保证恢复后双方重新确认 HEAD。

### 阶段 D：Streamlit 展示与配置

1. 显示 Agent 实时轨道和调用指标；
2. 增加 Codex、Claude、错误和原始输出标签；
3. 增加高级超时、CLI、模型和环境白名单配置；
4. 提供熔断、恢复和最后活动的可操作提示；
5. 验证长时间页面轮询不会持续增长内存。

### 阶段 E：健康预检与发布

1. 加入最小 CLI 结构化探针；
2. 固定经过验证的 CLI 路径、版本和模型；
3. 提供精简 MCP 配置；
4. 先以 shadow 模式记录活动但不改变超时行为；
5. 对比误超时率后启用新策略。

## 17. 测试方案

### 17.1 ProcessRunner

- 子进程持续输出超过旧的固定时限，只要仍有有效活动就继续；
- 子进程只输出 `thread.started` 后沉默，触发 startup timeout；
- 子进程工作一段时间后沉默，触发 idle timeout；
- 子进程持续输出仍在 hard timeout 被终止；
- 重复缓存错误不被计为有效活动；
- 取消请求仍终止整个进程组；
- UTF-8 半行、部分 JSONL 和超大输出不会破坏解析。

### 17.2 Adapter

- Codex 中间 Agent 消息可展示，但只有最终合法 Schema 作为结果；
- Codex raw 超过内存上限后仍能从完整文件读取最终协议；
- Claude stream-json 工具事件实时可见；
- Claude 旧单 JSON 仍能解析；
- Claude partial message 不被误当成最终结构化结果；
- 缺 Session、格式错误和截断继续产生明确错误。

### 17.3 Runtime 与恢复

- 基础设施失败且工作区干净时有限重试；
- Writer 超时且工作区有合法变更时自动提交 interrupted 检查点；
- 恢复提交撤销旧租约，新 Session 读取恢复摘要；
- 不可唯一归属的脏树保持 recovery blocked；
- 恢复验证失败仍保存检查点，但不能声明完成；
- 双 Agent 最终完成条件不因恢复而放宽。

### 17.4 实时事件与 UI

- CLI 未结束时页面已经能读取工具和文件事件；
- 页面刷新后从序号继续读取，不重复整段历史；
- 未完成 JSONL 尾行不会导致页面报错；
- Prompt、Key、Cookie、Token 和 Session ID 被遮罩；
- 1000 条以上事件时页面只保留尾部窗口；
- 模型文本按纯文本展示，不能注入 HTML。

### 17.5 端到端故障注入

使用 Fake CLI 覆盖：

1. 先网络失败、后重试成功；
2. 持续输出并编辑文件超过 20 分钟的等比例缩短场景；
3. 编辑后进程被杀，新 Session 接力；
4. 中文文件路径验证；
5. Claude stream-json 中途断流；
6. Worker 崩溃后手工 Resume 兜底。

## 18. 验收标准

- 页面在 CLI 首个事件后 2 秒内显示新活动；
- 页面能够分别查看 Codex 与 Claude 的实时脱敏事件；
- 活跃调用不会因为旧的 1200 秒固定上限而失败；
- 无有效活动的调用在 5 分钟内被识别并进入有限重试或熔断；
- 所有调用仍受 3600 秒单次硬上限和 Run 总时限约束；
- CLI 超时且产生合法范围变更时，改动自动保存为可审计恢复检查点；
- 自动恢复不会跳过另一个 Agent 的 HEAD 确认；
- 页面、事件和普通日志中不存在 Prompt、Key、Token 或完整 Session ID；
- 旧 Run 配置和 Claude 单 JSON 输出仍可恢复；
- 现有单 Writer、范围校验、最终双完成和聚焦集成测试全部通过。

## 19. 取舍与备选方案

### 19.1 只把超时调到 3600 秒

实现最简单，但模型服务断线时会等待更久，页面仍没有进展，且无法区分正在工作和已经
失联。因此不采用。

### 19.2 页面直接 tail raw 日志

可以快速看到文本，但 raw 可能包含敏感内容，Claude 单 JSON 也不能提供细粒度事件，
页面还需要反复解析大文件。因此 raw tail 只作为本机折叠诊断入口，不作为主方案。

### 19.3 CLI 失败后直接切换另一个 Agent

这会留下未经原 Writer 确认的修改，也可能跳过最终双方完成要求。因此只能先保存
interrupted 检查点，再由同一 Agent 新 Session 或双方正常接力审查，不能静默跳过。

### 19.4 Supervisor 判断中间改动是否正确

Supervisor 缺少业务裁决授权。它只能验证归属、范围、Git、机械验证和协议完整性；
业务正确性仍由 Codex 与 Claude 的结论决定。因此不采用 Supervisor 业务裁决。

## 20. 推荐决策

采用完整方案，优先级如下：

1. **P0**：实时事件、活动感知超时、脱敏和 UI 心跳；
2. **P0**：CLI 健康预检、明确路径/版本/模型和基础设施熔断；
3. **P1**：异常结束后自动 interrupted 检查点与新 Session 接力；
4. **P1**：非交互 Prompt、权威验证和工具活动预算；
5. **P2**：抽出 InvocationRuntime，降低 `runtime.py` 的职责和回归风险。

该顺序先解决“看不见”和“误杀”，再解决无人值守恢复，最后整理模块边界。每个阶段都
可以独立测试和启用，不需要一次性重写 LangGraph 工作流。

## 相关文档

- [项目 README](README.md)
- [辩论 Agent 使用与初始化手册](USER_GUIDE.md)
- [Codex 与 Claude Code 默认执行参数](CLI_AGENT_EXECUTION_DEFAULTS.md)
