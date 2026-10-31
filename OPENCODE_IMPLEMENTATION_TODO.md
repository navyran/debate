# OpenCode CLI 与内置 Agent 注册机制：模块拆解验收清单

本清单是
[OPENCODE_AGENT_REGISTRY_PLAN.md](OPENCODE_AGENT_REGISTRY_PLAN.md) 的唯一实施账本。
必须按 Module 顺序执行；一个 Module 只有在清单、聚焦测试、全量测试、Lint、Type Check、
Build、diff 自审和独立 Git Commit 全部完成后，才能进入下一个 Module。

> 状态：执行中
> 基线分支：`master`
> 开发分支：`codex/opencode-agent-registry`
> 禁止事项：不 push、不引入 OpenCode SDK/Server、不实现动态插件、不降低全体完成条件

## 0. 全局执行门禁

每个 Module 必须遵循：

1. 先写测试并运行，确认因目标能力尚未实现而失败；
2. 实现最小完整行为；
3. 运行聚焦测试；
4. 运行以下全量门禁：

   ```bash
   cd agent/debate
   .venv/bin/python -m pytest -q
   .venv/bin/ruff check src tests
   .venv/bin/mypy src
   .venv/bin/python -m build
   ```

5. 执行 `git diff --check`、范围检查和无关改动检查；
6. 更新本清单中的 Module 状态、验收证据和 Commit；
7. 只 stage 当前 Module 相关文件；
8. 创建一个独立 Git Commit；
9. Commit 后确认工作树没有本 Module 遗留改动，再进入下一 Module。

任何新增业务方法必须按仓库规则添加中文注释，说明功能、参数、返回值和异常。

## 1. 架构覆盖映射

| 方案章节 | 实施 Module | 权威验收证据 |
| --- | --- | --- |
| 1～5 背景、目标、不变量、架构 | Module 0、8 | 清单映射和最终逐项审计 |
| 6 Agent Catalog | Module 1 | Catalog 单测、注册完整性单测 |
| 7 Adapter Protocol/Registry | Module 2 | 三方 Adapter 回归和类型检查 |
| 8 Invocation 环境覆盖 | Module 4 | 环境白名单、进程环境合并测试 |
| 9 OpenCode Adapter/Session/JSON | Module 4 | Adapter 红绿测试和 Session 测试 |
| 10 完全授权和 Git 边界 | Module 5、6 | resolved config、HEAD/租约测试 |
| 11 MCP 隔离 | Module 5 | minimal/full MCP 健康测试 |
| 12 Health Strategy | Module 2、5 | 三方回归和 OpenCode 健康测试 |
| 13 配置模型 | Module 1、4 | 迁移、variant、Profile 冻结测试 |
| 14 CLI | Module 3、7 | 参数兼容和 OpenCode CLI 测试 |
| 15 Streamlit | Module 3、7 | 动态表单、track 和页面源码测试 |
| 16 Prompt 和记忆 | Module 6 | N 方广播和 OpenCode 记忆测试 |
| 17 调度与完成条件 | Module 6 | 四方调度、Ballot、Tree Ack 测试 |
| 18 Schema 与审计 | Module 6 | Schema 枚举一致、审计输出测试 |
| 19 错误与恢复 | Module 4～6 | 故障分类、Session 恢复、策略测试 |
| 20 文件边界 | Module 1～7 | 每模块 diff 范围审计 |
| 21 实施阶段 | Module 1～7 | 独立 Commit 序列 |
| 22 测试计划 | Module 1～8 | 测试文件与门禁输出 |
| 23 E2E 矩阵 | Module 7 | 组合 Runtime/Smoke 证据 |
| 24 锁定决策 | Module 8 | OC-D1～OC-D14 审计表 |
| 25 最终验收 | Module 8 | 验收清单全部勾选 |

## Module 0：实施清单、基线与隔离工作区

### 目标

在修改生产代码前，冻结方案、完整清单、仓库基线和开发隔离边界。

### 交付物

- [x] `OPENCODE_AGENT_REGISTRY_PLAN.md` 已存在且标记“待实施”；
- [x] `OPENCODE_IMPLEMENTATION_TODO.md` 覆盖方案所有章节、模块、接口、配置和验收；
- [x] `.worktrees/` 加入仓库忽略规则；
- [x] Module 0 全量质量门禁通过；
- [x] Module 0 独立提交；
- [x] 从 Module 0 提交创建 `codex/opencode-agent-registry` 隔离 worktree；
- [x] 隔离 worktree 复用或创建可用 `.venv`；
- [x] 隔离 worktree 基线全量质量门禁通过；
- [x] 建立 `.superpowers/sdd/progress.md` 持久执行账本。

### 验收标准

- TODO 对方案 1～25 章均有明确 Module 映射；
- TODO 明确每个 Module 的红绿测试、全量门禁、Commit 和状态证据；
- 生产代码在 Module 0 不发生变化；
- 基线测试、Ruff、Mypy 和 Build 均为 exit 0；
- 开发不直接发生在 `master`；
- 未执行 push。

### 验收证据

- 状态：`completed`
- Commit：`4ed5fef5 docs(discuss): plan OpenCode registry implementation`；隔离证据为后续 Module 0 证据提交
- 测试：当前 checkout 与隔离 worktree 均为 `222 passed`；Ruff、Mypy、Build 均通过
- 范围审计：仅方案、实施清单和 `.worktrees/` 忽略规则，无生产代码变更

## Module 1：内置 Agent Catalog 与配置基线

### 目标

先用现有 Codex、Claude、Qoder 建立纯元数据 Catalog；本 Module 不接入 OpenCode，
确保是行为不变的基础重构。

### 生产代码

- [x] 新增 `src/debate_agent/agent_catalog.py`；
- [x] 新增 `ModelPolicy`；
- [x] 新增冻结 `AgentDefinition`；
- [x] Catalog 注册 Codex、Claude、Qoder；
- [x] 提供稳定顺序迭代、按名读取和默认命令读取方法；
- [x] `config._default_profiles()` 改为从 Catalog 生成；
- [x] 不在 Catalog 中导入 Adapter 或 Health 实现；
- [x] 不增加动态插件、entry point 或用户自定义实现加载；
- [x] 保持默认参与者 Codex + Claude；
- [x] 保持 Qoder CLI 默认模型语义；
- [x] 为新增业务方法添加中文注释。

### 测试

- [x] 先添加 Catalog 不存在的失败测试并确认 RED；
- [x] Catalog 键集合等于当前 `AgentName`；
- [x] 三个定义的显示名、默认命令、模型策略、variant/MCP 能力准确；
- [x] 迭代顺序稳定为 Codex、Claude、Qoder；
- [x] 未知 Agent 查询失败；
- [x] 默认 Profile 与迁移前完全一致；
- [x] 历史 RunConfig 迁移行为不变；
- [x] 全量质量门禁通过。

### 验收标准

- 现有三方调用、CLI、UI 和审计可观察行为不变；
- Catalog 只承载元数据，没有实现依赖和循环导入；
- 没有 OpenCode 生产代码提前进入本 Module；
- 独立 Commit 完成且无遗留改动。

### 验收证据

- 状态：`completed`
- Commit：`5fa43c14 refactor(discuss): introduce built-in agent catalog`；`ef41e7a5 fix(discuss): reject string catalog lookups`
- RED：`.venv/bin/python -m pytest tests/test_agent_catalog.py -q`，结果为 `2 failed, 2 passed`，核心错误为 `ModuleNotFoundError: No module named 'debate_agent.agent_catalog'`
- GREEN/全量门禁：最终聚焦 `28 passed`；最终全量 `229 passed`；Ruff、Mypy、Build、`git diff --check` 均通过
- 范围审计：独立复审 `APPROVED`；仅 `agent_catalog.py`、`config.py`、Catalog 测试和本模块实施证据；未增加 OpenCode，未修改 Adapter/Health/UI/Runtime

## Module 2：Adapter 与健康策略注册化（三方行为不变）

### 目标

建立统一 Adapter/Stream Parser Protocol 和 Health Strategy Registry，迁移现有三方，
不改变任何已有 argv、Session、预检和错误分类。

### Adapter 接口

- [x] 定义 `AgentStreamParser` Protocol；
- [x] 定义 `AgentAdapter` Protocol；
- [x] 定义 `AdapterContext`；
- [x] 定义 `AdapterFactory` 类型；
- [x] 建立 `ADAPTER_FACTORIES`；
- [x] 注册 Codex、Claude、Qoder factory；
- [x] `DebateRuntime._adapter()` 改为 Registry 查找；
- [x] Runtime 不再返回三方联合类型；
- [x] Codex resume sandbox 配置继续传入；
- [x] Claude allowed tools/minimal MCP 继续传入；
- [x] Qoder `--yolo`/minimal MCP 继续传入。

### Health 接口

- [x] 定义 `CliHealthStrategy` Protocol；
- [x] 建立 `HEALTH_STRATEGIES`；
- [x] 迁移模型发现；
- [x] 迁移 help argv 和 required features；
- [x] 迁移认证检查；
- [x] 迁移 MCP 检查；
- [x] 迁移模型探针 argv；
- [x] 迁移终止事件验证；
- [x] `CliHealthChecker` 保留通用执行、脱敏、报告和熔断；
- [x] 未知或漏注册 Agent 产生明确配置错误。

### 测试

- [x] 先添加 Registry/Protocol 失败测试并确认 RED；
- [x] `ADAPTER_FACTORIES` 键集合与 Catalog 一致；
- [x] `HEALTH_STRATEGIES` 键集合与 Catalog 一致；
- [x] 三方首次/续接 argv 与迁移前一致；
- [x] 三方增量事件和终止协议与迁移前一致；
- [x] 三方模型发现、认证、MCP、探针和错误分类与迁移前一致；
- [x] Runtime Adapter 构造测试通过；
- [x] Mypy 不再依赖 Adapter 联合返回类型；
- [x] 全量质量门禁通过。

### 验收标准

- 现有三方行为无变化；
- 新增 Agent 时 Runtime 和通用健康检查不需要新增 `if/elif`；
- 注册集合一致性由测试强制；
- 独立 Commit 完成且无遗留改动。

### 验收证据

- 状态：`completed`
- Commit：`2385c631 refactor(discuss): register adapters and health strategies`
- RED：`.venv/bin/python -m pytest tests/test_agent_registries.py -q`，收集失败，核心错误为 `ImportError: cannot import name 'ADAPTER_FACTORIES'`
- GREEN/全量门禁：聚焦 `66 passed`；全量 `237 passed`；Ruff、Mypy、Build、`git diff --check` 均通过
- 范围审计：独立复审 `APPROVED`；仅 Adapter、Health、Runtime、Registry 测试和本 Module 验收证据；未增加 OpenCode，未修改 CLI/UI/Audit/schema

## Module 3：Catalog 驱动的 CLI、UI、实时视图与审计（三方行为不变）

### 目标

在仍只有三个 Agent 的前提下，清除接入表面的固定三方字段，保证本 Module 是纯重构。

### CLI

- [x] `--participant` 和 `--first-agent` choices 从 Catalog 生成；
- [x] Agent command/model/timeout 参数按 Catalog 循环生成；
- [x] 保留所有已有参数名和默认值；
- [x] Profile 构建按 Catalog 循环完成；
- [x] 不增加任意 `--agent-option` 注入能力。

### UI/ViewModel

- [x] `RunDraft` 用 Profile 映射替代固定 command/model 字段；
- [x] `inspect_workspace` 用 Catalog 检查选中 CLI；
- [x] Streamlit Agent 配置卡按 Catalog 生成；
- [x] 默认选中 Codex + Claude；
- [x] `first_agent` 只从已选参与者选择；
- [x] `InvocationLiveView` 改为 `tracks` 映射；
- [x] 删除固定 `codex`、`claude`、`qoder` 轨道字段；
- [x] Tab、Lane、原始输出和错误分类按 `participants` 生成；
- [x] 页面显示名、颜色和帮助文本读取 Catalog。

### Audit

- [x] 贡献章节显示名读取 Catalog；
- [x] 动态遍历参与者生成贡献、Ballot 和 Tree Ack 文本；
- [x] 三方现有审计文本兼容。

### 测试

- [x] 先添加动态 Profile/track 失败测试并确认 RED；
- [x] CLI 旧参数与默认值回归；
- [x] RunDraft 三方 Profile 回归；
- [x] 三方任意组合的 workspace 检查只检查已选 CLI；
- [x] `tracks` 只包含参与者或可安全按参与者读取；
- [x] UI 源码不再出现固定三列分支；
- [x] Audit 显示名和三方贡献回归；
- [x] 全量质量门禁通过。

### 验收标准

- 三方用户可见行为不变；
- 新增第四个 Catalog 定义后，CLI/UI/审计不需要第四套手写分支；
- 实时视图可按任意参与者集合生成；
- 独立 Commit 完成且无遗留改动。

### 验收证据

- 状态：`completed`
- Commit：`e71bf578 refactor(discuss): make agent surfaces catalog-driven`
- RED：`.venv/bin/python -m pytest` 聚焦动态测试，结果 `8 failed, 9 passed`；核心错误为 Catalog 颜色仍为 `gray`、CLI/Audit 未委托 Catalog、RunDraft/LiveView 仍为固定字段
- GREEN/全量门禁：聚焦 `73 passed`；全量 `249 passed`；Ruff、Mypy、Build、`git diff --check` 均通过
- 范围审计：独立复审 `APPROVED`；仅 Catalog 展示元数据、CLI、UI/ViewModel、Audit、直接测试和本 Module 证据；未增加 OpenCode，未修改 Adapter/Health/Runtime/schema

## Module 4：OpenCode Adapter、Session、协议与受控环境

### 目标

注册 OpenCode，完成 CLI argv、stdin Prompt、JSON 实时事件、终止协议、Session 复用和
受控进程环境；本 Module 不负责完整健康配置解析。

### 配置与注册

- [x] 增加 `AgentName.OPENCODE = "opencode"`；
- [x] Catalog 增加 OpenCode 定义；
- [x] 默认命令为 `opencode`；
- [x] 模型策略为启动前发现并冻结；
- [x] `AgentProfile` 增加可选 `variant`；
- [x] 历史配置无 OpenCode Profile 时仍可恢复；
- [x] 默认参与者仍为 Codex + Claude；
- [x] Adapter Registry 注册 OpenCode factory。

### Invocation 环境

- [x] `Invocation` 增加冻结 `environment_overrides`；
- [x] 旧 Adapter 默认覆盖为空；
- [x] 运行器按冻结白名单、代理/证书、Adapter 覆盖顺序合并环境；
- [x] OpenCode 只允许 `OPENCODE_CONFIG_CONTENT`、
      `OPENCODE_DISABLE_AUTOUPDATE`、`OPENCODE_DISABLE_DEFAULT_PLUGINS`；
- [x] 非允许变量产生配置/策略错误；
- [x] 环境值不进入 manifest、页面或公开事件。

### OpenCode argv 与 Session

- [x] 首次调用为 `opencode run`；
- [x] 包含 `--format json`、`--dir`、`--model`、`--agent build`、`--auto`、`--pure`；
- [x] 可选传递 `--variant`；
- [x] Prompt 只通过 stdin；
- [x] 首次调用不传 `--session`；
- [x] 从 JSON 事件提取唯一 Session ID；
- [x] 后续调用传 `--session <id>`；
- [x] 永不使用 `--continue` 和 `--fork`；
- [x] Session ID 缺失或冲突产生协议/边界错误。

### JSON Parser 与协议

- [x] 映射 `step_start`、`tool_use`、`text`、`error`、`step_finish`；
- [x] `step_finish` 不被错误标记为整轮终止；
- [x] 过滤工作区外和 `..` 路径；
- [x] 路径去重并限制数量和长度；
- [x] 收集最后一个非空完整 text；
- [x] 最终 Prompt 追加当前 JSON Schema；
- [x] 提取 JSON 对象并交给 Pydantic 协议模型；
- [x] 支持 raw spool 终局解析；
- [x] 错误详情脱敏并限制长度；
- [x] 格式重试继续受“工作区无变化、最多一次”门禁。

### 测试

- [x] 每项能力先有失败测试并确认 RED；
- [x] OpenCode argv/Session/stdin/variant 测试；
- [x] 所有事件类型和路径过滤测试；
- [x] 唯一/冲突 Session 测试；
- [x] 无 text、非法 JSON、error 事件测试；
- [x] 截断内存 + raw spool 测试；
- [x] 环境覆盖白名单和泄漏测试；
- [x] Qoder/Codex/Claude Adapter 回归；
- [x] 全量质量门禁通过。

### 验收标准

- OpenCode 可以作为受控 CLI Adapter 完成首次和续接调用；
- 同一 Run 始终复用首次 Session；
- 实时输出和最终协议互不混淆；
- 完全授权所需配置只通过受控环境生成；
- 独立 Commit 完成且无遗留改动。

### 验收证据

- 状态：`completed`
- Commit：`3650dd41 feat(discuss): add opencode cli adapter`；`367f5ead fix(discuss): harden opencode redaction boundaries`
- RED：分组复现缺少 OpenCode 枚举、Adapter 调用、环境覆盖、raw spool、历史 Profile、路径泄漏、低熵脱敏和路径边界缺陷
- GREEN/全量门禁：最终聚焦 `124 passed`；全量 `279 passed`；Ruff、Mypy、Build、`git diff --check` 均通过
- 范围审计：独立复审 `APPROVED`；Health 仅为 fail-closed 桥接，未提前实现 resolved config/认证/MCP/探针；未引入 SDK、Node 或 Server

## Module 5：OpenCode 健康检查、完全授权与 MCP 隔离

### 目标

让所有会导致 OpenCode 中途等待、失败或污染运行边界的问题在进入工作空间前被发现，
并冻结可重复的运行身份和配置。

### OpenCode Health Strategy

- [x] 注册 `OpenCodeHealthStrategy`；
- [x] 定位并冻结绝对可执行文件；
- [x] 读取并冻结版本；
- [x] 核验 `run --help` 必需参数；
- [x] 用户模型优先，否则从 resolved config 发现；
- [x] 最终模型必须为显式 `provider/model`；
- [x] 不从模型列表任意选择第一个；
- [x] 检查目标 provider 认证；
- [x] 在临时 Git 目录运行 JSON 模型探针；
- [x] 验证退出码、唯一 Session ID 和最终 text；
- [x] 尝试清理探针 Session；
- [x] 未选择 OpenCode 时完全不执行其命令。

### 完全授权配置

- [x] 生成 `permission: "allow"`；
- [x] 生成 `share: "disabled"`；
- [x] 生成 `snapshot: false`；
- [x] 生成 `autoupdate: false`；
- [x] Adapter argv 同时包含 `--auto` 和 `--pure`；
- [x] 用相同环境读取最终 resolved config；
- [x] 组织级或最终显式 deny 会阻止启动；
- [x] 权限询问事件不会转为用户交互等待。

### MCP 隔离

- [x] minimal 模式读取 resolved config；
- [x] 只提取 MCP 名称，不保存原始配置；
- [x] 为每个 MCP 生成 `enabled: false` 覆盖；
- [x] 再次解析并确认最终启用数为零；
- [x] full 模式保留用户 MCP 并检查 `mcp list`；
- [x] resolved config stdout 有大小上限；
- [x] resolved config 原文不进入日志、错误、页面或报告；
- [x] 只保存 MCP 数量、名称哈希、最终启用数和配置摘要哈希。

### 冻结身份

- [x] 冻结 command/version/model/variant；
- [x] 冻结运行配置摘要哈希；
- [x] 熔断身份包含足以区分 OpenCode 运行配置的字段；
- [x] Resume 使用冻结配置，不重新选择模型。

### 测试

- [x] 每项能力先有失败测试并确认 RED；
- [x] 未安装、缺参数、无模型、未认证失败测试；
- [x] 默认模型发现和显式模型优先测试；
- [x] permission allow 与 managed deny 测试；
- [x] minimal MCP 两阶段禁用测试；
- [x] full MCP 保留测试；
- [x] resolved config 不泄漏秘密测试；
- [x] 模型探针成功/错误/无 text/Session 冲突测试；
- [x] 未选中 OpenCode 不调用测试；
- [x] 三方健康回归；
- [x] 全量质量门禁通过。

### 验收标准

- OpenCode 可重复运行身份在启动前冻结；
- 默认完全授权不产生中途审批；
- minimal MCP 的最终启用数严格为零；
- 敏感 resolved config 不落盘、不显示；
- 独立 Commit 完成且无遗留改动。

### 验收证据

- 状态：`completed`
- Commit：`5f5e9b64 feat(discuss): validate opencode health and isolation`；`2a84208e fix(discuss): harden opencode health boundaries`；`87c4df75 fix(discuss): seal opencode health capabilities`；`582a6649 fix(discuss): harden opencode health encoding`
- RED：Strategy/能力/模型认证、resolved config/权限/MCP、临时 Git 探针/协议清理、冻结/运行覆盖，以及配置能力、UTF-8 与符号链接边界均先以失败测试复现。
- GREEN/全量门禁：最终 OpenCode 健康 `96 passed`、CLI health `15 passed`；完整 pytest `376 passed`；Ruff、Mypy、Build、`git diff --check` 均通过。
- 范围审计：第三轮独立复审 `APPROVED`；能力封印、minimal/full MCP、resolved-config SHA-256 身份、认证正向记录、稳定异常分类及敏感信息边界均已复核；仅健康检查、配置冻结、运行适配与相应测试改动。

## Module 6：Schema、Runtime、记忆、审计、共识与恢复集成

### 目标

证明 OpenCode 不是只有 CLI 能启动，而是完整参与多方讨论、写入接力、记忆更新、恢复、
最终 Ballot 和 Tree Ack。

### Schema

- [x] 四份静态 Schema 增加 `opencode`；
- [x] Schema 枚举与 `AgentName` 完全一致；
- [x] 不引入运行时 Schema 生成；
- [x] OpenCode 的 turn/change-ack/ballot/tree-ack 均可验证。

### Runtime 与 Supervisor

- [x] OpenCode 可被选为 first writer；
- [x] OpenCode 调用使用当前写租约和工作树；
- [x] OpenCode 进程树归属同一 Agent/turn；
- [x] Writer 调用前后 HEAD 必须一致；
- [x] OpenCode 自行 commit/reset/checkout 被拒绝；
- [x] Writer 改动经过 scope 检查；
- [x] Reviewer/终审改动工作区被拒绝；
- [x] Supervisor 创建带 OpenCode 标识的提交；
- [x] Facilitator 推荐只允许已选参与者；
- [x] 确定性回退可轮转 OpenCode。

### Memory 与 Prompt

- [x] OpenCode 写入提交生成权威 Git diff；
- [x] 为每个其他参与者创建独立 pending update；
- [x] update source agent 为 OpenCode；
- [x] 其他参与者先读取当前 HEAD 和变化全文；
- [x] 其他参与者分别 ack 自己的 update；
- [x] OpenCode 收到所有其他参与者更新；
- [x] OpenCode 使用公共 Prompt 合同，不增加业务特判；
- [x] Prompt 包含当前身份、角色、HEAD、Peer update、租约和禁止提交要求。

### Session 恢复

- [x] OpenCode Session 健康状态持久化；
- [x] Session 丢失时 generation 增加；
- [x] 新 Session 重放完整权威记忆；
- [x] 不重放已产生未知写入的 Prompt；
- [x] 恢复后继续使用冻结模型/variant/配置。

### 共识与审计

- [x] 四方 assessments 动态计算；
- [x] 任一参与者 incomplete 时不能完成；
- [x] Ballot required agents 等于参与者集合；
- [x] Tree Ack required agents 等于参与者集合；
- [x] 四方确认相同 final tree 才能集成；
- [x] OpenCode 贡献章节使用 Catalog 显示名；
- [x] 审计保存版本、模型、variant、Session 引用和配置哈希；
- [x] 最终目标分支仍只保留聚焦结果提交。

### 测试

- [x] 每项能力先有失败测试并确认 RED；
- [x] Codex + OpenCode Runtime 测试；
- [x] Claude + OpenCode Runtime 测试；
- [x] Qoder + OpenCode Runtime 测试；
- [x] 四方调度和 memory fan-out 测试；
- [x] OpenCode 首位 Writer 和来源提交测试；
- [x] HEAD 变化、越界写、Reviewer 写入策略测试；
- [x] Session 丢失与记忆重放测试；
- [x] 任一 incomplete 阻止完成测试；
- [x] 四方 Ballot/Tree Ack 测试；
- [x] 审计内容测试；
- [x] 全量质量门禁通过。

### 验收标准

- OpenCode 完整进入 Runtime，而不是 UI/Adapter 孤立功能；
- 单写者、Git 所有权和 scope 不变量保持；
- N 方记忆和完成条件无硬编码遗漏；
- 恢复语义与现有 Agent 一致；
- 独立 Commit 完成且无遗留改动。

### 验收证据

- 状态：`completed`
- Commit：`f5ffc602 feat(discuss): integrate opencode debate runtime`
- RED：恢复 capability、生命周期恢复预检、OpenCode 审计冻结字段与四方 memory/共识均先以失败测试复现。
- GREEN/全量门禁：最终 `389 passed`（非 Runtime `362` + Runtime 六组共 `27`）；Ruff、Mypy、Build、`git diff --check` 均通过。
- 范围审计：独立复审 `APPROVED`；恢复前 OpenCode 重新预检并严格冻结身份，动态 participants 贯穿租约、记忆、Facilitator、Ballot/Tree Ack，审计仅保存 SHA-256 引用和摘要。

## Module 7：OpenCode UI/CLI、用户文档与端到端组合验收

### 目标

完成用户入口、可观察性、前置条件、使用说明和组合验收，使 OpenCode 能通过 Web UI 和
CLI 实际选择并运行。

### CLI/UI

- [x] `--participant opencode` 可重复组合；
- [x] 增加全部 `--opencode-*` 参数；
- [x] 只有 OpenCode 显示/接受 `variant`；
- [x] UI 参与者多选显示 OpenCode；
- [x] 默认仍为 Codex + Claude；
- [x] OpenCode 配置卡显示 command/model/variant；
- [x] 默认命令自动发现绝对路径；
- [x] 未选择 OpenCode 时不阻断启动；
- [x] OpenCode 实时 Tab、Lane、raw tail 和错误建议正常；
- [x] CLI 和 UI 生成等价 Profile。

### 用户文档

- [x] README 增加 OpenCode 能力和快速入口；
- [x] USER_GUIDE 增加安装、认证、模型、页面和故障排查；
- [x] CLI_AGENT_EXECUTION_DEFAULTS 增加真实 argv、Session、权限、MCP 和 variant；
- [x] SMOKE_TEST 增加 OpenCode 和四方组合步骤；
- [x] 文档明确 OpenCode 是外部前置 CLI，不是 Python/Node 包依赖；
- [x] 文档明确不 push、Supervisor 管 Git、默认不选择 OpenCode；
- [x] 文档中的默认值与代码一致；
- [x] 文档测试覆盖 OpenCode 关键事实。

### 组合验收

- [x] Codex + Claude 默认回归；
- [x] Codex + Qoder 回归；
- [x] Codex + OpenCode；
- [x] Claude + OpenCode；
- [x] Qoder + OpenCode；
- [x] Codex + Claude + Qoder + OpenCode；
- [x] 每个组合验证参与者、Session、轮转、memory、Ballot 和 Tree Ack；
- [x] 使用 fake CLI 完成稳定自动化 E2E；
- [x] 在条件具备时执行本机 OpenCode 非写入 Smoke，缺少模型/认证时记录为外部环境风险，
      但不能以此替代自动化 E2E。

### 测试

- [x] 每项入口能力先有失败测试并确认 RED；
- [x] CLI parser/config 测试；
- [x] UI view model/source 测试；
- [x] 文档一致性测试；
- [x] fake CLI 组合 E2E；
- [x] 全量质量门禁通过。

### 验收标准

- 用户可以从页面和 CLI 选择 OpenCode；
- 页面可观察 OpenCode 的实时执行；
- 新环境前置条件和初始化步骤完整；
- 自动化组合矩阵覆盖全部二方代表组合和四方组合；
- 独立 Commit 完成且无遗留改动。

### 验收证据

- 状态：`completed`
- Commit：`3068ab14 feat(discuss): expose opencode in cli ui and docs`；`9b1c442e test(discuss): strengthen opencode e2e coverage`
- RED：CLI variant、UI 动态 variant/profile 映射、文档一致性和组合 E2E 均先以失败测试复现。
- GREEN/全量门禁：最终 `396 passed`；Ruff、Mypy、Build、`git diff --check` 均通过。
- Smoke/E2E：fake Backend 覆盖默认、Codex+Qoder、三种含 OpenCode 两方与四方组合，并从 state/acks/audit 验证 Session、轮转、memory、Ballot 与同一 Tree Ack；本机 OpenCode 1.18.3 的只读 help/auth smoke 成功但为 `0 credentials`，未运行真实模型或写入任务。
- 范围审计：独立复审 `APPROVED`；CLI/UI capability 驱动、文档默认值和组合 E2E 均已复核，未引入 Runtime/Health 旁路。

## Module 8：最终方案完整度、提交与无关改动审计

### 目标

不以“测试没有失败”代替方案完成度证明，逐条验证方案、清单、提交范围和最终质量门禁。

### 方案逐项审计

- [x] 方案 1～25 章逐条标记证据；
- [x] OC-D1～OC-D14 逐条验证；
- [x] 方案最终 15 项验收清单全部有直接证据；
- [x] 本 TODO 所有生产代码和测试项均已完成；
- [x] 不存在 TODO、FIXME、占位实现、`pass` 或跳过测试；
- [x] 不存在仅为测试通过而绕过真实行为的特判；
- [x] 不存在未接入 Runtime 的孤立 OpenCode 功能。

### Git 提交审计

- [x] Module 0～7 每个模块都有独立 Commit；
- [x] Commit 顺序符合依赖和清单顺序；
- [x] 每个 Commit 的 diff 只包含对应 Module；
- [x] 没有把用户已有或无关改动纳入提交；
- [x] 没有大范围格式化或相邻重构；
- [x] 没有执行 push；
- [x] 分支相对基线的完整 diff 可审查；
- [x] 审查发现的 Critical/Important 问题均有修复和复审证据。

### 最终质量门禁

- [x] 全量 pytest 通过；
- [x] Ruff 通过；
- [x] Mypy 通过；
- [x] Build 通过；
- [x] `git diff --check` 通过；
- [x] 文档链接和默认值一致性通过；
- [x] fake CLI E2E 组合矩阵通过；
- [x] 最终代码复核通过；
- [x] 工作树无目标内未提交改动；
- [x] 最终分支和 Commit 列表已记录。

### 最终汇总要求

- [x] 汇总每个 Module 的交付物；
- [x] 汇总每个 Commit hash 和标题；
- [x] 汇总每个验证命令及最新结果；
- [x] 明确列出方案中未完成内容，完成时写“无”；
- [x] 明确列出无关改动，完成时写“无”；
- [x] 明确列出遗留风险，不能把未验证项描述为已完成；
- [x] 只有所有要求有直接证据后，才允许将目标标记为 complete。

### 验收证据

- 状态：`completed`
- 最终审计 Commit：本次提交
- 全量门禁：在最终两项修复后重新执行 `.venv/bin/python -m pytest -q`，结果 `403 passed in 81.23s`；`.venv/bin/ruff check src tests`、`.venv/bin/mypy src`、`.venv/bin/python -m build` 和 `git diff --check 8a620777..HEAD` 均通过。
- 完成度审计：最终独立审计 `APPROVED`。章节 1～5 的目标/非目标/不变量由 Catalog、Registry、单写租约、受控环境和无 SDK/Server 扫描证明；章节 6～12 由 Catalog、Adapter/Health 策略、OpenCode argv/Session、完全授权和 MCP 两模式测试证明；章节 13～19 由冻结 Profile、CLI/UI、动态 Prompt/Memory、N 方共识、静态 Schema、审计和恢复测试证明；章节 20～23 由模块范围、提交序列和 fake CLI 组合 E2E 证明；章节 24 的 OC-D1～D14 分别由内置 Catalog、CLI 子进程、默认参与者、Session、build/完全授权/Supervisor、冻结模型与 variant、MCP、Schema、全员完成、固定 max_turns 和聚焦结果审计实现；章节 25 的 15 项均由回归、健康、Runtime、UI 和文档测试直接覆盖。
- Git 审计：基线为 `8a620777f449c2a114a92b5ef6f744ef2f47891f`；相对差异仅 `.gitignore` 与 `agent/debate/` 的方案相关文件，未混入实施期间推进到 `master` 的 `flask_tool/` 提交；无远端分支包含当前 HEAD，未执行 push。Module 0～7 均有独立提交和复审。Schema enum 在 `3650dd41` 因 Adapter 协议依赖提前落地，Module 6 完成集成验证；这是相关依赖的阶段交叠，不是无关改动。
- Commit 列表：`4ed5fef5`、`694857f0`、`ecb6fa79`、`5fa43c14`、`ef41e7a5`、`0fd14040`、`2385c631`、`435f1eee`、`e71bf578`、`0e96af7a`、`3650dd41`、`367f5ead`、`08c12d28`、`5f5e9b64`、`2a84208e`、`87c4df75`、`582a6649`、`24cb3f09`、`f5ffc602`、`1499f1e8`、`3068ab14`、`9b1c442e`、`54d0bb40`、`85260b71`、`dc942695` 及本次审计提交。
- 未完成内容：无。
- 无关改动：无。
- 遗留风险：本机 OpenCode 1.18.3 的 `auth list --pure` 为 `0 credentials`，因此没有执行真实模型/真实写入 E2E；这不影响 fake CLI 自动化矩阵，但仍是部署前需要用户配置 provider 认证的外部前置条件。完全授权只适用于可信工作区，编排层门禁不等同于操作系统隔离。
