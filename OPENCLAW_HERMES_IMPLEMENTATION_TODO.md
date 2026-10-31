# OpenClaw 与 Hermes Agent：模块拆解验收清单

本文是
[OPENCLAW_HERMES_INTEGRATION_PLAN.md](OPENCLAW_HERMES_INTEGRATION_PLAN.md)
的实施控制清单。所有开发严格按模块顺序推进；前一模块只有在验收、质量门禁和 Git
提交全部完成后，才能进入下一模块。

> 状态：已完成（2026-07-26）
>
> 基线分支：`master`
>
> 实施分支：`codex/openclaw-hermes-integration`
>
> 默认参与者：Codex + Claude
>
> Push：禁止，除非用户另行明确要求

## 0. 状态与统一门禁

状态：

- `[ ]`：未开始；
- `[-]`：开发或验收中；
- `[x]`：验收通过且已提交；
- `[!]`：存在阻塞，但不代表可跳过。

每个模块都必须执行以下质量门禁：

```bash
python -m pytest -q
python -m ruff check src tests
python -m mypy src
python -m build
git diff --check
```

每个模块提交前还必须：

- 检查 `git status --short`，只包含当前模块文件；
- 检查 `git diff --stat` 和完整 diff；
- 确认没有 `TODO`、占位实现、跳过测试或无关格式化；
- 确认所有新增业务方法包含中文注释，说明功能、参数、返回值和异常；
- 记录 RED 测试失败证据、GREEN 测试成功证据和全量门禁结果；
- 使用聚焦 Git message 提交，不把下一模块内容混入当前提交。

## M0：实施清单与基线

- [x] 完整读取架构方案并把全部目标、模块、接口、配置、测试和验收条件映射到本清单。
- [x] 确认当前工作位于独立 worktree 和 `codex/openclaw-hermes-integration` 分支。
- [x] 运行未修改代码的 pytest、Ruff、Mypy 和 Build 基线。
- [x] 记录基线失败；如有失败，先判断是否为已有问题，不带病进入开发。
- [x] 修复 master 已有但被 `.gitignore` 漏提交的最终基准 Fixture，使隔离 worktree
  可以复现既有 `test_adversarial_review_benchmark.py`。
- [x] 检查清单覆盖方案第 2～18 节，没有遗漏关键行为。
- [x] 提交本清单。

验收标准：

- 清单覆盖注册、Profile、Prompt 传输、两个 Adapter、Session、恢复、健康检查、UI、
  审计、实时状态、组合测试、真实 CLI 探针、文档和最终覆盖审计；
- 基线所有质量门禁退出码为 0；
- 本模块除清单外，只允许补回既有测试明确引用、且与既有最终基准报告一致的缺失
  Fixture；不得修改生产代码或放宽测试；
- 产生独立 Git commit。

## M1：OpenClaw 真实 CLI 兼容性探针

### OpenClaw 探针

- [x] 冻结 `openclaw --version`、可执行文件路径和 `agent --help` 能力摘要。
- [x] 验证 `gateway status --json` 可连接、配置有效，不静默回退 `--local`。
- [x] 使用独立临时 Session Key 和临时 Git 仓库执行真实单轮 JSON 调用。
- [x] 验证 `--message-file`、`--model`、`--thinking`、`--timeout` 和 `--json`。
- [x] 验证同一 Session Key 的第二轮可识别第一轮标记。
- [x] 验证 Writer 能在临时仓库产生目标文件变化。
- [x] 验证 Reviewer Prompt 不产生工作区变化。
- [x] 验证 SIGTERM 后的 Session 状态和文件边界，记录模糊传输的防重放约束。
- [x] 保存脱敏 stdout/stderr 结构样本，不保存 Prompt、Token 或 Session 凭据原文。

### 交付物

- [x] 新增兼容性报告，记录版本、命令形态、输出边界、Session 语义和 Go/No-Go 结论。
- [x] 把稳定且脱敏的输出样本保存为测试 Fixture。
- [x] 对无法自动化的中断行为保存可复现命令和观察结果。

验收标准：

- OpenClaw 探针全部通过后才允许进入正式 OpenClaw 实现；
- 探针只在临时仓库和独立 Session 中运行；
- 报告和 Fixture 不包含秘密、完整业务 Prompt 或可复用 Session 凭据；
- 全量质量门禁通过并产生独立 Git commit。

## M1-H：Hermes 真实 CLI 兼容性硬门禁

该门禁独立于 OpenClaw，可在 M2～M4 开发期间保持未完成；但未通过时禁止进入 M5
Hermes 生产实现。这样既不以 fake 结果冒充 Hermes 可用，也不让 Hermes 的本机认证问题
阻断可独立完成的共享基础设施和 OpenClaw 模块。

- [x] 冻结 `hermes --version`、可执行文件路径和 `chat --help` 能力摘要。
- [x] 验证 `hermes status` 能发现 Profile、Provider 和 Model。
- [x] 证明默认 `custom/glm-5.1` 的 401 不可用，并以现有默认密钥文件验证显式
  `deepseek/deepseek-v4-flash` 真实 Query。
- [x] 使用临时 Git 仓库执行真实 `chat --quiet --yolo --no-restore-cwd` 成功调用。
- [x] 证明能够可靠分离最终回复、Session ID 和诊断输出。
- [x] 使用 `--resume` 执行第二轮并验证原生记忆连续性。
- [x] 验证恢复后仍保持 debate-agent 指定 cwd。
- [x] 验证 Writer 能产生目标文件变化，且不创建 Hermes worktree。
- [x] 验证 Reviewer Prompt 不产生工作区变化。
- [x] 采用 65,536 UTF-8 字节的保守 Query 上限并验证启动前拒绝、不截断。
- [x] 保存当前 401 的脱敏 stdout/stderr 边界，不保存 Prompt、Key 或 Session ID 原文。

验收标准：

- Hermes 真实 Query、Session 恢复、cwd、Writer、Reviewer 和长度探针全部通过；
- 401、凭据缺失或仅有 `hermes status` 均为 No-Go；
- 全部探针只使用临时 Git 仓库和独立 Session；
- 通过后单独提交更新后的兼容性报告和成功 Fixture，才能进入 M5。

## M2：共享注册与安全 Prompt 传输基础设施

### Catalog 与 Profile 能力

- [x] 先写失败测试覆盖 `AgentDefinition` 新能力元数据。
- [x] 增加 `variant_label`、`supports_native_profile`、`supports_provider` 和
  `experimental`，保持已有四个 Agent 行为不变。
- [x] 先写失败测试覆盖 `AgentProfile.native_profile` 和 `provider`。
- [x] 新字段进入冻结配置、序列化、反序列化、迁移和恢复比较。
- [x] UI Profile 构造函数能够从动态 Catalog 收集新字段，不新增厂商散落分支。

### Invocation Prompt 传输

- [x] 先写失败测试定义 `PromptTransport.STDIN/TEMP_FILE/ARGV`。
- [x] 定义 `TemporaryInput` 的内容、替换标记和安全文件属性。
- [x] `Invocation.stdin` 支持 `None`，并增加 `audit_argv`、`prompt_transport` 和
  `temporary_inputs`。
- [x] 为现有四个 Adapter 补齐默认 `STDIN` 和安全 `audit_argv`，行为不变。
- [x] ProcessRunner 在工作区外创建权限 `0600` 的临时文件。
- [x] ProcessRunner 在正常退出、非零退出、异常、启动超时、空闲超时、硬超时和取消
  路径删除临时文件。
- [x] 事件、raw 元数据和审计只能持久化 `audit_argv`，不能持久化真实敏感 argv。
- [x] 增加通用 UTF-8 argv Prompt 长度校验，超限抛出明确的启动前错误，绝不截断。
- [x] 环境覆盖白名单仍然闭合，不因新字段扩大密钥传播范围。

验收标准：

- 所有新行为都有经过 RED→GREEN 的测试；
- 现有四个 CLI Adapter 的 argv、stdin、Session 和输出测试无回归；
- 临时文件权限和所有清理路径均有自动化测试；
- Prompt 不出现在审计 argv、Invocation 事件或测试失败快照；
- 全量质量门禁通过并产生独立 Git commit。

## M3：OpenClaw 正式垂直接入

### 注册与配置

- [x] 先写失败测试增加 `AgentName.OPENCLAW`。
- [x] 在 Catalog 注册 OpenClaw，模型策略为 `DISCOVER_AND_FREEZE`。
- [x] `supports_variant=True`、`variant_label="Thinking"`、
  `supports_native_profile=True`、`experimental=False`。
- [x] 默认命令为 `openclaw`，默认不加入 participants。
- [x] Adapter、Health Strategy、环境覆盖白名单和敏感键表注册完整。

### OpenClawAdapter

- [x] 先写失败测试覆盖首轮和续轮 argv。
- [x] 使用 `openclaw agent`、`--message-file`、`--session-key`、`--json` 和
  `--timeout`。
- [x] 配置时映射 `native_profile -> --agent`、`model -> --model`、
  `variant -> --thinking`。
- [x] 永远不生成 `--local`、`--deliver` 或外部 Channel 参数。
- [x] 同一个 Run/Agent 使用稳定的显式 Session Key，不同 Run 不复用。
- [x] Prompt 只使用 `TEMP_FILE`，真实 Prompt 不进入 argv。
- [x] 解析 JSON 外层，提取最终正文和规范 Session 标识，忽略未知字段。
- [x] 最终正文进入现有 Normalizer、Schema 校验和一次格式修复流程。
- [x] JSON 不合法、正文缺失、Session 不一致时抛出明确 `AdapterOutputError` 或
  `SessionBoundaryError`。
- [x] Stream Parser 只产生有证据的启动、诊断和终止事件，不虚构工具事件。

### 模糊传输恢复

- [x] 定义 OpenClaw `ambiguous_transport` 故障分类。
- [x] 连接丢失或 Gateway 超时时，不进入普通自动重放。
- [x] 先查询 Session：已完成则恢复结果；仍运行则等待/取消；证明未接收才允许重试。
- [x] 无法证明状态时进入可恢复失败，保留租约、Git 和 Session 审计证据。
- [x] 防重放逻辑有完成、运行中、未接收、未知四种测试。

验收标准：

- `AGENT_CATALOG`、`ADAPTER_FACTORIES` 和临时 Health 注册均完整覆盖 `AgentName`；
- Adapter 参数、Prompt 文件、Session、终止解析和防重放测试全部通过；
- OpenClaw 与 Codex 的最小 Runtime 测试通过；
- 全量质量门禁通过并产生独立 Git commit。

## M4：OpenClaw 健康检查、UI、审计和恢复

### OpenClawHealthStrategy

- [x] 先写失败测试覆盖可执行文件、版本、Gateway、Profile、Model 和协议检查。
- [x] 读取 `gateway status --json` 并验证 Gateway 可连接、配置存在且有效。
- [x] Gateway 不可用时给出修复建议，不静默使用 `--local`。
- [x] 验证 Agent/Profile、provider/model 和 thinking。
- [x] 执行单轮协议、两轮 Session、Writer、Reviewer、取消和模糊传输探针。
  完整破坏性矩阵由 M1 兼容性门禁执行；常规启动只执行只读单轮探针。
- [x] 预检结果冻结 CLI 路径、版本、Profile、Model、Thinking 和配置摘要。
- [x] 未选择 OpenClaw 时不执行其预检。

### UI 与配置

- [x] 页面动态显示 OpenClaw 卡片、CLI 路径、Agent/Profile、Model、Thinking、
  Gateway 和 Session 状态。
- [x] OpenClaw 默认不勾选，不影响历史 Run 和默认 Codex + Claude。
- [x] 运行轨道显示心跳、最近活动、Session 摘要、Git 变化和终止结果。
- [x] 没有结构化厂商证据时不显示伪造的工具级事件。

### 审计与恢复

- [x] 审计记录 OpenClaw CLI 路径/版本、Profile、Model、Thinking、配置摘要、
  Session 摘要/代次、Prompt Transport、预检和错误分类。
- [x] 审计不记录 Prompt、Gateway Token、Session 凭据或外部投递目标。
- [x] 恢复使用冻结版本和 Profile；版本漂移时重新预检。
- [x] Writer 检查点 message 包含 `agent:openclaw`。
- [x] Peer Memory 向全部其他参与者广播 OpenClaw Git 全文和摘要。

验收标准：

- UI、CLI、配置、健康、审计和恢复测试覆盖 OpenClaw；
- OpenClaw Reviewer 写入被现有工作区指纹门禁拒绝；
- OpenClaw 未选择时不触发安装、认证或 Gateway 要求；
- 全量质量门禁通过并产生独立 Git commit。

## M5：Hermes 实验性垂直接入

### 注册与配置

- [x] 先写失败测试增加 `AgentName.HERMES`。
- [x] 在 Catalog 注册 Hermes，模型策略为 `DISCOVER_AND_FREEZE`。
- [x] `supports_provider=True`、`experimental=True`；v0.19.0 没有调用级
  `--profile`，因此不虚构 `supports_native_profile`。
- [x] 默认命令为 `hermes`，默认不加入 participants。
- [x] Adapter、Health Strategy、环境覆盖白名单和敏感键表注册完整。

### HermesAdapter

- [x] 先写失败测试覆盖首轮和续轮 argv。
- [x] 生成 `hermes chat --quiet --yolo --no-restore-cwd`。
- [x] 映射 `provider -> --provider`、`model -> --model`。
- [x] 首轮不传 `--resume`，续轮复用首次 Session ID。
- [x] 永远不生成 `--worktree`、`--ignore-rules`、`--safe-mode`、Gateway 或 Kanban
  参数。
- [x] Prompt 使用 `ARGV`，`audit_argv` 中 Query 必须为 `<redacted>`。
- [x] 实施经 M1 实测确定的 UTF-8 字节上限，超限启动前失败且不截断。
- [x] 可靠分离 stdout 最终回复、状态行 Session ID 和诊断信息。
- [x] Banner、Spinner、恢复提示和 Session 行不得污染业务正文。
- [x] 最终正文进入现有 Normalizer、Schema 校验和一次格式修复流程。
- [x] 无法提取 Session、正文被污染或 Session 不一致时抛出明确错误。
- [x] Stream Parser 只产生有证据的诊断和终止事件。

验收标准：

- Hermes 首轮、续轮、Profile、Provider、Model、脱敏、长度和输出解析测试通过；
- Hermes 与 Claude 的最小 Runtime 测试通过；
- 现有四个 Agent 和 OpenClaw 无回归；
- 全量质量门禁通过并产生独立 Git commit。

## M6：Hermes 健康检查、UI、审计和恢复

### HermesHealthStrategy

- [x] 先写失败测试覆盖可执行文件、版本、Status、Provider、Model 和协议。
- [x] `hermes status` 只能作为发现信息，必须执行真实模型 Query。
- [x] 验证最终回复与 Session ID 可分离。
- [x] 验证 `--resume` 原生记忆连续性。
- [x] 验证 `--no-restore-cwd`、Writer、Reviewer 和 Query 超限。
- [x] 关键探针失败时保持卡片可见但不可选择，并展示具体原因。
- [x] 未选择 Hermes 时不执行其预检。

### UI 与配置

- [x] 页面动态显示 Hermes 卡片、实验性标识、CLI 路径、Provider、Model、
  Session 状态和 argv 可见性说明。
- [x] Hermes 默认不勾选，不影响历史 Run 和默认 Codex + Claude。
- [x] 运行轨道显示心跳、最近活动、Session 摘要、Git 变化和终止结果。

### 审计与恢复

- [x] 审计记录 Hermes CLI 身份/版本、Provider、Model、配置摘要、Session
  摘要/代次、`ARGV` Prompt Transport、实验性状态、预检和错误分类。
- [x] 审计不记录 Prompt、Provider Key、`.env` 内容或 Session ID 原文。
- [x] 恢复使用冻结 Provider、Model 和版本；版本漂移时重新预检。
- [x] Writer 检查点 message 包含 `agent:hermes`。
- [x] Peer Memory 向全部其他参与者广播 Hermes Git 全文和摘要。

验收标准：

- UI、CLI、配置、健康、审计和恢复测试覆盖 Hermes；
- Hermes Reviewer 写入被现有工作区指纹门禁拒绝；
- Hermes 未选择时不触发安装、认证或模型要求；
- 全量质量门禁通过并产生独立 Git commit。

## M7：组合测试、文档与兼容性收口

### Runtime 组合和不变量

- [x] OpenClaw + Codex 双参与者全流程测试。
- [x] Hermes + Claude 双参与者全流程测试。
- [x] OpenClaw + Hermes 双参与者全流程测试。
- [x] 六参与者全流程或等价确定性 Runtime 测试。
- [x] Writer 顺序不固定，Facilitator 建议和确定性轮转都可选择新 Agent。
- [x] 任意时刻最多一个 active 写租约。
- [x] 两个新 Agent 的 Reviewer、投票和终审调用都保持工作区不变。
- [x] 写入后所有其他 Agent 收到 Git 全文和摘要。
- [x] 每个 Agent 在同一 Run 复用 Session，丢失时创建新代次并注入权威记忆。
- [x] 少任一参与者的 `accept` 或 `complete` 都不能结束。
- [x] 所有参与者确认同一 final tree，最终集成仅产生一个聚焦提交。

### 真实 E2E

- [x] 增加 `DEBATE_AGENT_E2E_OPENCLAW=1` 的隔离真实测试入口。
- [x] 增加 `DEBATE_AGENT_E2E_HERMES=1` 的隔离真实测试入口。
- [x] 默认测试不调用模型、不消耗额度。
- [x] E2E 只使用临时 Git 仓库和独立 Session，结束后清理临时输入与仓库；
  Hermes 精确删除 Session，OpenClaw 使用唯一 Key 并运行官方 maintenance。
- [x] 在当前已安装 CLI 上各执行至少一次真实 E2E。

### 文档

- [x] 更新 README 支持列表和前置条件。
- [x] 更新用户手册的安装、认证、页面配置、Session 和故障排查。
- [x] 记录 OpenClaw Gateway 要求、禁用 `--deliver/--local` 和模糊传输恢复。
- [x] 记录 Hermes 实验性状态、argv 可见性、长度门禁和禁止 `--worktree`。
- [x] 更新默认执行参数文档和相关架构说明。
- [x] 将实施方案状态更新为已实施，并链接本清单和兼容性报告。

验收标准：

- 方案第 2～18 节所有自动化行为都有测试或明确的真实探针证据；
- 两个真实 E2E 通过；
- 全量 pytest、Ruff、Mypy、Build 和 `git diff --check` 通过；
- 文档与真实命令和页面一致；
- 产生独立 Git commit。

## M8：最终覆盖与提交审计

- [x] 逐条检查实施方案第 2～18 节和第 16 节 16 项最终验收标准。
- [x] 为每一项记录代码、测试、命令输出或真实运行证据。
- [x] 检查本清单所有项均为 `[x]`，不存在 `[ ]`、`[-]` 或 `[!]`。
- [x] 检查完整提交序列，每个模块恰有聚焦提交且顺序正确。
- [x] 对比 `master...HEAD` 完整 diff，确认没有无关重构、格式化或秘密。
- [x] 搜索新增代码中的 TODO、FIXME、占位、skip、xfail 和临时调试输出。
- [x] 运行最终全量 pytest、Ruff、Mypy、Build 和 `git diff --check`。
- [x] 重新执行两个真实 CLI E2E。
- [x] 生成最终实施验收报告，列出模块、Commit、验证、未完成项、无关改动和遗留风险。
- [x] 只有全部证据成立后，提交最终验收报告。

验收标准：

- 方案全部要求有直接证据，不能用“测试未发现问题”代替覆盖证明；
- 未完成内容为“无”，无关改动为“无”，否则继续开发或修正；
- 最终工作树干净；
- 不执行 git push；
- 产生最终审计 Git commit。
