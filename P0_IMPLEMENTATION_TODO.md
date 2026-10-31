# P0 执行稳定性与 CLI 实时输出：模块拆解验收清单

依据：[RELIABILITY_AND_LIVE_OUTPUT_PLAN.md](RELIABILITY_AND_LIVE_OUTPUT_PLAN.md)。
本清单只实施方案第 20 节标记为 P0 的事项；P1/P2 仍需纳入覆盖审计，
但不得混入本轮实现或被误报为 P0 缺口。

## 0. 执行门禁与范围

- [x] 每个开发模块严格执行 RED → GREEN → REFACTOR：先增加失败测试并确认失败原因是功能缺失，再写最小实现。
- [x] 单模块依次通过聚焦测试、全量测试、Ruff、Mypy、Build 和 `git diff --check` 后，更新本清单并创建独立 Git Commit。
- [x] 上一模块未验收、未提交，不进入下一模块；不把多个模块合并为一次提交。
- [x] 所有新增或修改的业务方法使用中文 docstring，说明功能、参数、返回值和可能异常。
- [x] 保留单 Writer、GoalContract 范围、Supervisor 非业务裁判、Prompt 不盲目重放、双 Agent 完成确认、最终聚焦集成提交等不变量。
- [x] 不提交 raw CLI 日志、Prompt、Key、Token、完整 Session ID、环境变量值或运行期 `.runtime` 数据。
- [x] 不修改 `agent/debate` 外的业务代码；不暂存用户无关改动；不执行 `git push`。
- [x] 当前基线中 `src/debate_agent/adapters.py` 与 `tests/test_adapters.py` 已有未提交的 `--ignore-user-config` 改动；Module 3 已验证当前 Codex 版本支持并纳入对应提交。

### P0 覆盖边界

| 方案条目 | 本轮处理 |
|---|---|
| 实时事件、脱敏、增量存储、UI 心跳 | Module 1、3、6 |
| 启动/空闲/硬上限/Run 总时限 | Module 1、2、5 |
| CLI 健康预检、绝对路径、版本、显式模型 | Module 4 |
| 精简 MCP、代理/证书环境名白名单 | Module 4 |
| 故障分类、有限基础设施重试、熔断 | Module 4、5 |
| 自动 interrupted 检查点与新 Session 接力 | P1，不在本轮实现；Module 7 审计现有手工 Resume 不回归 |
| 非交互 Prompt、权威验证与工具活动预算 | P1，不在本轮实现 |
| 抽出完整 InvocationRuntime | P2，不在本轮做职责重构；P0 可新增内聚的小型事件/分类组件 |

## Module 0：方案映射与开发基线

### 交付物

- [x] 本文件完整列出 P0 涉及的模块、接口、配置、迁移行为、测试和验收标准。
- [x] 记录开发前 Git HEAD、分支、Python、Codex CLI、Claude Code CLI 版本和全量质量基线。
- [x] 记录并隔离开发前未提交改动，后续每次只暂存当前模块文件。

### 验收标准

- [x] 对照方案第 2～20 节逐项核对，无遗漏的 P0 条目，P1/P2 有明确边界。
- [x] 基线全量 pytest、Ruff、Mypy、Build、`git diff --check` 全部通过；若基线失败，记录并先修复/隔离，不能带病进入 Module 1。
- [x] 本清单单独提交，不包含生产代码或既有脏改动。

### Module 0 验收证据

- 开发前 HEAD：`d91d6d2dbf30d50f0f040a4e5f6d7c5f15cf9c97`；分支：`master`。
- Python：`.venv` 为 `3.12.13`（系统 `python3` 为 `3.9.6`）；Git：`2.50.1`。
- Codex CLI：`0.144.5`；Claude Code：`2.1.211`。
- 基线：`145 passed in 50.14s`；Ruff、Mypy、Build、`git diff --check` 全部通过。
- 既有脏改动仅为 `adapters.py`/`test_adapters.py` 的 `--ignore-user-config`，未纳入 Module 0。

## Module 1：冻结配置、实时事件模型、增量存储与脱敏

### 文件与接口

- [x] `config.py` 新增分层调用配置：`startup_timeout_seconds=300`、`idle_timeout_seconds=300`、`hard_timeout_seconds=3600`、`heartbeat_interval_seconds=5`；保留历史 `timeout_seconds` 作为旧 Run 的硬上限兼容来源。
- [x] `config.py` 新增实时输出、基础设施重试、尾部窗口、MCP 模式和代理/证书变量名配置；数值、枚举和环境变量名严格校验。
- [x] 新增 `invocation_events.py`：`InvocationEvent`、`InvocationProgress`、事件严重度/类型、`InvocationEventStore`、`Redactor` 和安全 raw 尾部读取接口。
- [x] `InvocationEvent` 覆盖 `seq/timestamp/run_id/invocation_id/agent/turn_id/event_type/title/summary/severity/elapsed_seconds/idle_seconds/changed_paths/terminal`，限制摘要 500 字符，路径只能是工作区相对路径。
- [x] Store 使用 `invocation-events.jsonl` 和原子 `invocation-progress.json`；单写者 append 后 flush；读取支持 `after_seq`、`limit`、按 Agent 过滤和未完成尾行容错。
- [x] `InvocationProgress` 可按 Agent/调用恢复最后序号、最后有效活动、事件数、文件数、运行/空闲时长、终止类别。
- [x] Redactor 默认丢弃未知厂商字段，并遮罩 Prompt、常见 Key/Token/Cookie/Authorization、URL 凭据、环境变量值、完整 Session ID、工作区外绝对路径和超长工具输出。
- [x] raw 尾部读取仅返回脱敏后的最后 N 行，不一次性装载完整文件，不改变原始文件。

### 兼容与安全验收

- [x] 历史 Run 缺少新字段仍可反序列化，并继续使用其冻结的 `timeout_seconds`，不能静默改为 3600 秒。
- [x] 新 Run 默认采用 300/300/3600/5 秒策略和 7200 秒 Run 总时限。
- [x] 事件或快照不得出现测试注入的 Prompt、Key、Token、Cookie、完整 Session ID、代理值或外部绝对路径。
- [x] 1000 条以上事件可通过游标和尾部窗口读取；未完成 JSONL 最后一行不会使 UI 读取失败或重复已确认事件。

### 测试与提交门禁

- [x] RED：新增配置迁移、事件模型、增量读取、断行、背压、脱敏和 raw tail 测试，并确认因接口缺失失败。
- [x] GREEN：Module 1 聚焦测试全部通过。
- [x] 全量 pytest、Ruff、Mypy、Build、`git diff --check` 全部通过。
- [x] Module 1 独立提交完成。

### Module 1 验收证据

- RED：`ModuleNotFoundError: debate_agent.invocation_events`。
- 聚焦 GREEN：`25 passed`；全量：`152 passed in 45.88s`。
- Ruff：通过；Mypy：28 个源码文件通过；sdist/wheel 构建成功；`git diff --check` 通过。

## Module 2：ProcessRunner 增量输出与活动感知超时

### 文件与接口

- [x] `process.py` 在保留 argv、`shell=False`、独立进程组、取消和 raw 文件语义基础上，增量读取 stdout/stderr，安全处理 UTF-8 半字符、半行和部分 JSONL。
- [x] `ProcessRunner.run()` 接收启动/空闲/硬上限/心跳策略、stdout/stderr 增量回调、有效活动判定回调和可选工作区指纹回调。
- [x] `ProcessResult` 记录明确的终止类型（成功、启动超时、空闲超时、硬超时、取消、进程错误）、最后有效活动时间和计数；旧 `timed_out` 消费者继续兼容。
- [x] 首个有效活动前仅计算 startup timeout；有过有效活动后计算 idle timeout；持续活动只能延后 idle，不能突破 hard timeout。
- [x] 工具开始/完成、Agent 文本/结构化消息、文件修改、工作区指纹变化和合法终止刷新活动；仅进程存活、mtime、`thread.started`、重复缓存/MCP 错误不刷新活动。
- [x] 心跳按最小间隔发出运行快照，但不计为有效活动。
- [x] stdout 内存上限不影响完整 raw 文件和最终协议从完整文件读取；超大输出不导致死锁或协议必然截断。
- [x] 超时/取消仍终止整个进程组、等待子进程退出并原子落盘 manifest。

### 行为验收

- [x] 按比例缩短的测试中，持续有效输出超过旧墙钟边界仍继续，最终成功。
- [x] 只有 `thread.started` 后沉默触发 `startup_timeout`。
- [x] 有效工作一段时间后沉默触发 `idle_timeout`。
- [x] 持续有效输出到达硬上限触发 `hard_timeout`。
- [x] 重复缓存/MCP 错误不会制造伪活动；工作区真实指纹变化会刷新活动。
- [x] 取消、部分 UTF-8/JSONL、超大输出和进程组清理的既有行为不回归。

### 测试与提交门禁

- [x] RED：先增加上述 ProcessRunner 行为测试并确认旧固定墙钟实现失败。
- [x] GREEN：`tests/test_process.py` 全部通过。
- [x] 全量 pytest、Ruff、Mypy、Build、`git diff --check` 全部通过。
- [x] Module 2 独立提交完成。

### Module 2 验收证据

- RED：`ImportError: cannot import name 'ProcessOutput'`。
- 聚焦 GREEN：`10 passed`；全量：`160 passed in 48.43s`。
- Ruff、Mypy、sdist/wheel Build、`git diff --check` 全部通过。

## Module 3：Codex/Claude 流事件适配与最终协议兼容

### 文件与接口

- [x] `adapters.py` 为 Codex JSONL 增加增量事件映射：生命周期、命令/工具、文件、Agent 消息、错误和终止；完整 Session ID 不进入公开事件。
- [x] Codex 中间 `agent_message` 可展示但不能作为成功结果；最终仍以最后一个满足 Schema 的消息为唯一协议。
- [x] Codex 内存 stdout 被截断时，从完整 stdout raw 文件流式寻找最终合法协议，不能因页面内存上限误报“结构化输出被截断”。
- [x] Claude 调用迁移为 `--output-format stream-json --include-partial-messages`，同时保留旧单 JSON 解码。
- [x] Claude 增量映射工具、文件、文本、错误和 `result/structured_output`；partial message 可展示但不能当作最终协议。
- [x] 两个 Adapter 都将厂商事件转换为 Module 1 的受限字段，不传递未知 raw 字段，并为 Module 2 提供“是否有效活动”的判定。
- [x] 非零退出、缺 Session、非法 JSON、Schema 错误、断流和无合法终止结果继续产生明确协议/进程错误。
- [x] 核验基线中的 `--ignore-user-config` 是否为已安装 Codex 版本支持且符合精简 MCP 目标；不支持则以可验证的等价参数替代，不盲目保留。

### 行为验收

- [x] Codex/Claude 在进程结束前即可发布工具、文件和文本事件。
- [x] Codex 最终协议位于超过内存窗口的位置时仍解析成功。
- [x] Claude stream-json 正常、partial、断流、非法终止和旧单 JSON fixture 均有测试。
- [x] 实时解析不改变 Pydantic/JSON Schema 最终校验，不泄露 Session 或敏感字段。

### 测试与提交门禁

- [x] RED：新增流解析、双格式、partial、断流、raw fallback 测试并确认失败。
- [x] GREEN：`tests/test_adapters.py` 全部通过，并用本机两种 CLI `--help` 核验参数存在。
- [x] 全量 pytest、Ruff、Mypy、Build、`git diff --check` 全部通过。
- [x] Module 3 独立提交完成。

### Module 3 验收证据

- RED：缺少 `create_stream_parser()`、Claude JSONL 解码和 raw fallback，共 `4 failed`。
- 聚焦 GREEN：`18 passed`；全量：`166 passed in 46.08s`。
- 本机帮助核验：Codex `0.144.5` 支持 `--ignore-user-config/--json/--output-schema`；Claude `2.1.211` 支持 `stream-json/partial/strict-mcp-config`。
- Ruff、Mypy、sdist/wheel Build、`git diff --check` 全部通过。

## Module 4：CLI 健康预检、冻结执行环境与基础设施熔断

### 配置与接口

- [x] 新增健康检查模型/服务，顺序覆盖：绝对可执行文件、版本范围、登录状态、短时限最小结构化只读探针、模型缓存解析、模型主连接、MCP 启用/失败项。
- [x] 预检服务不依赖或获取仓库锁/写租约；关键失败阻止调用方继续，非关键 MCP 失败仅 warning；全局执行顺序在 Module 5 接线验证。
- [x] RunConfig 可冻结两种 CLI 的绝对路径、实际版本、显式模型、分层超时、MCP 模式和启用的代理/证书变量名；恢复时可直接读取冻结身份。
- [x] 提供页面/CLI 共用的路径、版本和本机模型发现接口；模型无法确定时返回可操作错误；页面启动接线在 Module 6 完成。
- [x] 提供精简/完整 MCP 配置和检查接口；完整本机 MCP 必须显式选择，非关键 MCP 启动失败不阻断模型探针；Runtime 接线在 Module 5 完成。
- [x] 子进程环境过滤器仅允许基础白名单及用户显式选择的 `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY/NO_PROXY/SSL_CERT_FILE/SSL_CERT_DIR`；配置和报告只记录变量名/启用状态，不记录值。
- [x] 新增基础设施故障分类：`startup_timeout/idle_timeout/hard_timeout/network_error/cache_incompatible/mcp_optional_error/protocol_error/validation_error/policy_violation/workspace_race`。
- [x] 新增持久化熔断状态：相同 CLI/版本/模型/错误指纹在短窗口达到阈值后打开；冷却期内预检快速失败；成功探针可关闭/复位。

### 行为验收

- [x] 未找到 CLI、版本不兼容、未登录、模型为空、探针主连接失败和缓存不兼容分别有明确检查项与故障类别。
- [x] 可选 MCP 失败仅 warning；模型主连接失败为 error；所有探针严格只读且有短硬上限。
- [x] 同类基础设施错误达到阈值后新 Run 快速熔断；不同 CLI/模型不串扰；冷却/成功后恢复。
- [x] 预检组件没有仓库锁或 active 写租约依赖；系统级顺序留给 Module 5 集成测试证明。
- [x] 健康报告与保存配置不包含任何代理/证书变量值。

### 测试与提交门禁

- [x] RED：使用 fake CLI 覆盖路径/版本/登录/探针/cache/MCP/熔断和环境过滤并确认接口缺失失败。
- [x] GREEN：预检、配置、熔断、页面 view-model 聚焦测试全部通过。
- [x] 全量 pytest、Ruff、Mypy、Build、`git diff --check` 全部通过。
- [x] Module 4 提交完成；因并行 Codex 任务抢先提交，提交中含已存在的 session/graph/audit 改动，最终审计必须单列。

### Module 4 验收证据

- RED：`ModuleNotFoundError: debate_agent.cli_health`；随后分别捕获未校验终止协议、未接熔断、CLI 参数和 RunDraft 字段缺口。
- 聚焦 GREEN：健康预检 `10 passed`，配置模型聚焦 `28 passed`；全量：`177 passed in 40.25s`。
- Ruff：通过；Mypy：29 个源码文件通过；sdist/wheel Build、`git diff --check` 通过。
- Commit：`e97df4b5`；其中 CLI health/config 属于 Module 4，audit/graph/sessions 及对应测试是并行任务已有改动，非本模块主动扩展。

## Module 5：Runtime/Supervisor 接入、有限重试与心跳

### 文件与接口

- [x] `runtime.py`/调用入口为每次 CLI 调用生成稳定 `invocation_id`，把 run/turn/agent 关联传入事件 Store、ProcessRunner 和 Adapter。
- [x] 调用开始、有效活动、周期心跳、分类错误和终止均更新 `invocation-events.jsonl`/`invocation-progress.json`；现有节点级 `events.jsonl` 语义保持不变。
- [x] Run 总时限继续为 7200 秒默认值，并在调用前/调用中共同约束剩余硬上限；任何调用不能突破 Run 剩余预算。
- [x] 工作区干净且属于基础设施故障时，按 `max_process_failures` 有限退避并重建 Session；业务不完整、验证错误、协议策略错误不错误套用基础设施重试。
- [x] 网络/缓存/关键模型/MCP 分类结果写入熔断器；可选 MCP warning 不消耗完整调用失败预算。
- [x] 工作区有改动的异常仍执行既有来源/范围/租约保护，不盲目重放 Prompt、不跳过另一 Agent 的 ACK 或最终完成确认；P1 自动 checkpoint 不在本模块实现。
- [x] 超时/失败/取消后撤销运行中的进程资源并发布终止事件；不把阶段性消息当作成功。
- [x] 旧 Run 继续按冻结的历史配置 Resume；新字段缺失不导致 checkpoint 反序列化失败。
- [x] 新 Run 的 CLI 健康预检和配置冻结发生在 Artifact/GitWorkspace/仓库锁/写租约之前；UI 预分配 Run 可在无仓库副作用时安全替换为冻结配置。

### 行为验收

- [x] fake CLI 先基础设施失败、后成功时只做有限重试并保留连续调用记录；真实 fake CLI 在结束前可观察。
- [x] 连续相同基础设施失败达到预算后停止并记录熔断；下一 Run 在模型探针前快速失败。
- [x] 活跃调用超过旧 1200 秒等比例边界不会失败，但 3600 秒硬上限与 Run 剩余预算生效。
- [x] 工作区变化、Reviewer/终审写入、越界写入、验证失败和协议失败仍遵守原有门禁。
- [x] 恢复流程不会因新心跳/事件跳过 PeerMemory、ChangeAck、blind ballots 或双方完成判断。

### 测试与提交门禁

- [x] RED：新增 runtime 集成测试并确认旧调用路径不能发布实时事件、分类或有限重试。
- [x] GREEN：`tests/test_runtime.py`、`tests/test_events.py`、相关 graph/recovery 测试全部通过。
- [x] 全量 pytest、Ruff、Mypy、Build、`git diff --check` 全部通过。
- [x] Module 5 独立提交完成。

### Module 5 验收证据

- RED：CLIBackend 缺少剩余预算/事件接线、Lifecycle 缺少配置预检、Runtime 缺少分类重试，共 `4 failed`。
- 聚焦 GREEN：runtime/artifacts/health 组合 `31 passed`；全量：`182 passed in 53.50s`。
- 真实 fake Codex 证明工具事件在 CLI 完成前落盘；TLS 为 `network_error`；0.08 秒 Run 剩余预算覆盖 2 秒 Profile 硬上限。
- Ruff、Mypy（29 个源码文件）、sdist/wheel Build、`git diff --check` 全部通过。

## Module 6：Streamlit 实时轨道、活动指标与高级配置

### 页面与 ViewModel

- [x] `ui/view_models.py` 提供按 `after_seq` 增量读取、Codex/Claude 分轨、最近活动、运行/空闲时间、命令数、文件数、错误类别和终止原因的纯数据模型。
- [x] `ui/app.py` 继续使用 `@st.fragment(run_every=2)`，在调用未结束时读取新增 invocation 事件，不重复加载完整历史。
- [x] 页面显示 Run 状态/节点/回合，以及 Codex/Claude 两张卡片；每张卡片展示运行时长、空闲时长、事件数、文件数和最近活动。
- [x] 页面提供“进度摘要 / Codex 实时 / Claude 实时 / 原始输出 / 验证与结论”区域；raw 默认折叠且只显示脱敏尾部。
- [x] 模型文本使用 `st.code`/普通文本，不使用 `unsafe_allow_html`；页面切换/刷新 Run 后从落盘序号恢复。
- [x] 高级设置新增两个 CLI 的绝对路径、显式模型、启动/空闲/硬超时、基础设施重试、实时输出开关、raw 尾部行数、MCP 模式和代理/证书变量名。
- [x] 默认界面保持简洁；高级项折叠并填入自动发现的 CLI 路径/本机模型；无效字段和不可执行路径在启动前提示，完整健康预检继续由 Worker 在仓库副作用前执行。
- [x] Run 停止后仍可查看最后事件、分类超时原因、熔断原因和安全恢复建议。

### 行为验收

- [x] 首个 CLI 事件产生后，页面按 2 秒轮询边界显示；Codex/Claude 可分别查看。
- [x] 1000 条以上事件只保留配置的尾部窗口，重复轮询不造成页面内存持续增长。
- [x] 未完成 JSONL 行、页面刷新、Run 切换、raw 文件不存在或损坏时均安全降级。
- [x] UI 渲染测试证明模型 HTML 不会被执行，且 Prompt/Key/Token/Session ID/代理值不可见。
- [x] 既有启动、恢复、终止、清理、是否集成目标分支功能不回归。

### 测试与提交门禁

- [x] RED：新增 view-model/UI 测试并确认旧页面看不到 CLI 过程或高级配置。
- [x] GREEN：`tests/test_ui_view_models.py`、`tests/test_ui_app.py` 全部通过。
- [x] 全量 pytest、Ruff、Mypy、Build、`git diff --check` 全部通过。
- [x] Module 6 独立提交完成。

### Module 6 验收证据

- RED：`ImportError: cannot import name 'read_invocation_live_view'`；随后捕获首次 Claude Session UUID 在 raw 尾部尚无已知 Session 映射时的泄露风险。
- 聚焦 GREEN：invocation/UI 组合 `18 passed`；全量：`186 passed in 50.32s`。
- 1005 条事件测试只返回最后 1000 条；未完成 JSONL 尾行不阻断读取；HTML 事件正文由 Streamlit `st.code` 渲染。
- Ruff、Mypy（29 个源码文件）、sdist/wheel Build、`git diff --check` 全部通过。
- Commit：`0964b1f2`。

## Module 7：P0 端到端验收、文档与最终审计

### 自动化与故障注入

- [x] Fake Codex/Claude 覆盖：先网络失败后成功、持续活动越过旧边界、启动沉默、工作后沉默、持续活动触发硬上限、Claude stream-json 断流、超大 Codex 输出、重复 MCP/cache 错误。
- [x] 证明页面事件可在进程完成前被 Store 读取，延迟不超过 2 秒轮询边界。
- [x] 证明无活动在默认 300 秒的等比例测试内分类，随后进入有限重试或熔断。
- [x] 证明新 Run 受 3600 秒单次硬上限和 7200 秒 Run 总时限，历史 Run 使用冻结旧上限。
- [x] 证明所有实时事件、普通日志、配置、状态和审计产物中无 Prompt、Key、Token、Cookie、完整 Session ID、代理值。
- [x] 证明旧 Claude 单 JSON、旧 Run 配置和手工 Resume 仍工作。
- [x] 证明单 Writer、范围校验、Git 归属、双方 ACK、双 complete、最终验证和聚焦集成全部不变。

### 文档与最终审计

- [x] 更新 `README.md`、`USER_GUIDE.md`、`CLI_AGENT_EXECUTION_DEFAULTS.md`：实时输出、分层超时、健康预检、模型/CLI 冻结、MCP、代理变量名、熔断和排障说明与实现一致。
- [x] 不留下 `TODO`、占位实现、skip/xfail 或未解释的临时兼容分支。
- [x] 对照本清单与方案第 2～20 节逐项审计 P0 覆盖，并明确列出按优先级未实现的 P1/P2，不能把它们误报为完成。
- [x] 检查每个 Git Commit 和 `git diff <开发前HEAD>..HEAD --stat`；无 `agent/debate` 外无关改动，Module 4 并发提交例外见下方审计记录。
- [x] 最终全量 pytest、Ruff、Mypy、Build、`git diff --check` 全部通过，工作区只允许保留已识别的用户原有改动。
- [x] Module 7 文档/验收独立提交完成；不执行 push。

### Module 7 验收证据

- RED：文档一致性测试发现旧 `1200` 秒固定超时、Claude 单 JSON、UI 不支持模型配置等过期说明；同步三份用户文档后 GREEN。
- 故障注入：`test_process.py` 覆盖活动/启动/空闲/硬上限/重复错误，`test_adapter_streams.py` 覆盖双 CLI 流与截断，`test_runtime.py` 覆盖首次 `network_error` 后安全重试并完成全生命周期。
- 安全与兼容：invocation/health/audit/process/UI 测试覆盖脱敏；models/sessions/adapters/graph 测试覆盖旧配置、旧 Claude JSON 和 Resume。
- 不变量：supervisor/policy/memory/graph/audit/runtime 测试覆盖单 Writer、范围、Git 归属、ChangeAck、盲投双 complete、TreeAck 与聚焦集成。
- 最终全量：`189 passed in 46.38s`；Ruff、Mypy（29 个源码文件）、sdist/wheel Build、`git diff --check` 全部通过。
- 提交范围：`d91d6d2d..HEAD` 仅修改 `agent/debate`。`e97df4b5` 被并行 Codex 任务抢先创建，除 Module 4 的 health/config/CLI/UI 外还含其已有 audit/graph/sessions 改动及测试；这些改动均在本项目内且全量门禁通过，但该提交不是纯 Module 4 范围，未改写共享历史。
- Module 7 Commit：本清单所在提交；未执行 push。

## 最终交付记录（完成时填写）

- 开发前 HEAD：`d91d6d2dbf30d50f0f040a4e5f6d7c5f15cf9c97`
- 开发分支：`master`
- Python / Git / Codex / Claude 版本：`3.12.13 / 2.50.1 / 0.144.5 / 2.1.211`
- Module 0 Commit：`54d77e60`
- Module 1 Commit：`9b37d5ad`
- Module 2 Commit：`f6244573`
- Module 3 Commit：`08af350e`
- Module 4 Commit：`e97df4b5`（验收记录 `3aed5f92`）
- Module 5 Commit：`51154387`
- Module 6 Commit：`0964b1f2`
- Module 7 Commit：本清单所在提交
- 最终全量测试：`189 passed in 46.38s`
- 最终 Ruff / Mypy / Build：全部通过（29 个源码文件；sdist/wheel 成功）
- P1 未实施：异常结束后自动 interrupted 检查点与新 Session 接力；非交互 Prompt、权威验证和工具活动预算。
- P2 未实施：抽出独立 `InvocationRuntime`，降低 `runtime.py` 的职责和回归风险。
- 无关改动与遗留风险：无 `agent/debate` 外改动；`e97df4b5` 含并行任务的 audit/graph/sessions 改动，已单列且未改写共享历史。
