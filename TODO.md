# CLI Debate Agent 模块拆解验收清单

依据：`docs/superpowers/specs/2026-07-16-cli-debate-agent-design.md`

## 执行门禁

- [x] 每个模块严格执行 RED → GREEN → REFACTOR；生产代码前必须先看到对应测试因功能缺失而失败。
- [x] 每个模块完成后运行该模块全部测试、Lint、Type Check；结果通过后更新本清单并独立提交。
- [x] 上一模块未验收或未提交，不进入下一模块。
- [x] 所有业务函数和方法使用中文 docstring，说明功能、参数、返回值和可能异常。
- [x] 不修改 `agent/debate`、本清单和必要方案文档之外的业务目录。
- [x] 不暂存或提交现有 `.DS_Store` 等无关文件。
- [x] 不执行 `git push`。

## Module 0：开发基线与清单

### 交付物

- [x] `agent/debate/TODO.md` 完整覆盖方案中的模块、接口、配置、状态、测试和运行验收。
- [x] 创建隔离开发分支与 worktree，不直接在 `master` 开发生产代码。
- [x] 记录当前基线 commit、Python、Git、Codex CLI、Claude Code CLI 版本。

### 验收标准

- [x] 清单包含 Module 1—6 和最终验收，且每个模块给出文件、接口、行为、异常和验证命令。
- [x] `git diff --check` 通过。
- [x] 清单作为单独 Git Commit 提交。

### 基线证据

- 基线提交：`d54ca78df299ff82e97149c202888a4433f9d8e6`。
- 开发分支：`codex/cli-debate-agent`。
- 隔离 worktree：`/path/to/debate-worktree`。
- Python：`3.14.4`；Git：`2.50.1 (Apple Git-155)`。
- Codex CLI：`0.137.0`；Claude Code：`2.1.138`。
- `agent/debate` 基线仅包含验收清单，尚无可执行包和测试；Module 1 从失败测试开始建立独立 Python 包。
- 清单初始提交：`d54ca78d docs: add debate agent acceptance checklist`。

## Module 1：包结构、配置、协议模型与 Schema

### 文件

- [x] 创建 `agent/debate/pyproject.toml`：声明 Python `>=3.10`、运行依赖、dev 依赖、pytest、ruff、mypy 和 build 配置。
- [x] 创建 `agent/debate/.gitignore`：忽略 `.runtime/`、`.venv/`、缓存、构建产物和测试缓存。
- [x] 创建 `agent/debate/src/debate_agent/__init__.py` 与 `__main__.py`。
- [x] 创建 `config.py`、`models.py`、`cli.py`。
- [x] 创建 `turn.schema.json`、`change-ack.schema.json`、`ballot.schema.json`、`tree-ack.schema.json`。
- [x] 创建 `tests/test_models.py`、`tests/test_cli.py`。

### 配置接口

- [x] `AgentProfile`：`command`、`model`、`timeout_seconds`；默认 Codex/Claude 和 1200 秒。
- [x] `RunConfig`：workspace、topic/topic-file、target branch、base ref、first agent、max turns、总时限、失败/停滞预算、state root、setup/validation 命令、联网开关、环境变量白名单。
- [x] 配置校验：workspace/topic、正整数预算、Agent 枚举、互斥参数、路径解析和序列化。
- [x] 同一 run 的 Agent Profile 冻结；无法确认模型时允许记录 `unknown`。

### 协议模型

- [x] Agent/Decision/Completion/SessionHealth/LeaseStatus/RunStatus 等枚举完整覆盖方案状态。
- [x] `AgentSessionState`、`LeaseState`、`AgentResult`、`Issue`、`TurnResult`。
- [x] `ChangeSummary`、`ChangeAck`、`PeerMemoryUpdate`、`Ballot`、`TreeAck`。
- [x] `DebateState` 覆盖 repo、branch、worktree、GoalContract、HEAD、turn、sessions、updates、validation、ballots、final tree、失败与停滞计数。
- [x] 模型禁止未知关键字段或明确处理扩展字段；所有路径/枚举/时间字段可 JSON 序列化。

### CLI 骨架

- [x] `python -m debate_agent run` 接受 workspace、topic/topic-file、target branch、base ref、first agent、max turns。
- [x] `status` 能读取 run 状态；未实现的运行编排不得伪装成功。
- [x] `resume/abort/cleanup` 参数契约先定义，具体行为在 Module 6 落地。
- [x] CLI 错误返回非零退出码，stdout 输出简洁状态，敏感信息不进入日志。

### Schema 验收

- [x] 四个 JSON Schema 与 Pydantic 模型字段、枚举和 required 项一致。
- [x] Turn Schema 覆盖 base/observed HEAD、GoalContract version、decision、issues、scope/validation additions、completion assessment。
- [x] Ack/Ballot/TreeAck 均绑定当前 commit 或 tree，不能只用自然语言关键词。

### 验收命令

- [x] RED：`python -m pytest agent/debate/tests/test_cli.py agent/debate/tests/test_models.py -q` 因模块缺失失败。
- [x] GREEN：同一命令全部通过。
- [x] `python -m ruff check agent/debate/src agent/debate/tests` 通过。
- [x] `python -m mypy agent/debate/src` 通过。
- [x] `python -m build agent/debate` 成功生成 wheel 和 sdist。
- [x] Module 1 独立提交完成。

### Module 1 验收证据

- RED：初始 2 个导入错误；最小导入骨架后 14 个行为失败；随后分别捕获 Schema required、Issue 字段和 wheel Schema 打包缺口。
- GREEN：`20 passed`。
- Ruff：`All checks passed!`；Mypy：`Success: no issues found in 5 source files`。
- Build：成功生成 `debate_agent-0.1.0.tar.gz` 与 wheel，wheel 内包含四个运行时 Schema。

## Module 2：Git worktree、仓库锁、Supervisor 事务与策略

### 文件

- [x] 创建 `git_workspace.py`、`supervisor.py`、`policy.py`。
- [x] 创建 `tests/test_git_workspace.py`、`tests/test_supervisor.py`、`tests/test_policy.py` 与 Git 仓库 fixture。

### GitWorkspace 接口

- [x] 预检 workspace 是可读写 Git 仓库，解析 repo root、target branch、base ref 和 base commit。
- [x] 拒绝已修改、已暂存或未跟踪文件；在调用模型前返回 `preflight_blocked`。
- [x] 仓库锁粒度为 real repo path + target branch；锁记录 run ID、PID、base、worktree、时间。
- [x] 同目标分支拒绝第二个 run；仅在旧进程不存在且状态可恢复时接管遗留锁。
- [x] 创建 `debate/<run_id>` 分支及 `<state-root>/worktrees/<run_id>/workspace`，两个 Agent 共用 cwd。
- [x] 获取 status/diff/untracked/changed paths、HEAD、tree hash，并支持静默状态比较。
- [x] checkpoint commit 由 Supervisor 暂存允许变更，message/trailer 标明 run、Agent、turn、GoalContract、validation status。
- [x] 普通 `failed-validation` 可提交；策略违规、来源不明和工作树竞态不可提交。
- [x] 支持以 base commit 为 parent、final tree 为 tree 的单提交创建和 fast-forward 集成。
- [x] 目标 branch 漂移/不干净/不可快进时返回 `integration_blocked`，不强制改写历史。
- [x] 成功后验证目标 tree 并清理临时 worktree/branch；失败保留恢复资源。

### Supervisor 与租约接口

- [x] 单写者租约是唯一合法写入授权，Reviewer 和终审调用均不持有写权限。
- [x] 单个 run 同时最多一个 active `LeaseState`。
- [x] 发放租约前写入 prepared 日志；Writer 结束后先撤销租约再验证/提交。
- [x] 无租约写入、Reviewer 写入和 tree-ack 写入均识别为协议违规。
- [x] 提交后换手；异议方优先成为下一 Writer；双方 incomplete 时由非当前 HEAD 作者接手。
- [x] Supervisor 只做确定性门禁，不生成业务代码、不替 Agent 裁决。

### Policy 接口

- [x] 允许 worktree 内 GoalContract 范围的读写、测试、编译、静态检查、格式化和只读 Git 命令。
- [x] 禁止 Agent 执行 add/commit/reset/checkout/switch/merge/rebase/worktree/push 等 Git 状态变更。
- [x] 禁止 `.git`、审计目录、运行锁、外部真实路径、外部符号链接、嵌套仓库和敏感路径写入。
- [x] 高影响文件仅在 GoalContract 明确允许时可写。
- [x] setup/validation 命令以 argv 形式校验，不使用 shell 拼接；联网和环境变量执行白名单。

### 验收命令

- [x] RED：Module 2 三个测试文件因实现缺失而失败。
- [x] GREEN：`python -m pytest agent/debate/tests/test_git_workspace.py agent/debate/tests/test_supervisor.py agent/debate/tests/test_policy.py -q` 全部通过。
- [x] 全量 Module 1—2 测试、ruff、mypy 通过。
- [x] Module 2 独立提交完成。

### Module 2 验收证据

- RED：三个模块最初均因导入缺失失败；增强契约又在缺少 `WorkspaceRaceError` 时按预期失败。
- GREEN：Module 2 `18 passed`；Module 1—2 全量 `38 passed`。
- Ruff：`All checks passed!`；Mypy：`Success: no issues found in 8 source files`；`git diff --check` 通过。
- 覆盖：干净仓库预检、活锁/显式遗留锁接管、共享 worktree、写前日志、唯一租约、验证后竞态、路径策略、Agent 提交归属、单 parent 集成及漂移阻断。

## Module 3：进程、CLI Adapter、Session 与跨 Agent 记忆

### 文件

- [x] 创建 `process.py`、`adapters.py`、`sessions.py`、`memory.py`、`prompts.py`。
- [x] 创建 `tests/test_process.py`、`tests/test_adapters.py`、`tests/test_sessions.py`、`tests/test_memory.py`。

### ProcessRunner 接口

- [x] 使用 argv、`shell=False`、指定 cwd、stdin、独立进程组和 UTF-8 安全解码。
- [x] 捕获 stdout/stderr、return code、耗时；控制最大输出并将 raw output 原子写入运行目录。
- [x] 超时/取消时终止整个进程组；回收残留子进程并等待工作树静默。
- [x] 日志隐藏 Prompt、环境变量、认证信息和敏感输出。

### Adapter 接口

- [x] Codex 首次命令包含 `-C`、`--sandbox workspace-write`、`--json`、`--output-schema`、stdin `-`，不含危险/临时/猜测会话参数。
- [x] Codex resume 使用明确 session ID；不能证明续接安全边界时拒绝并触发 Session 重建。
- [x] Claude 首次命令使用 UUID、acceptEdits、工具/命令白名单、JSON/Schema；resume 使用明确 session ID。
- [x] Claude 不使用 `--continue`、无持久化、bare 或危险权限参数；Prompt 优先通过 stdin。
- [x] 两个 Adapter 解析结构化输出、识别 session ID，并统一返回 `AgentResult`。
- [x] 非零退出、空响应、非法 JSON、Schema 错误和超时有明确错误；格式错误仅按配置重试一次。

### SessionManager 接口

- [x] run ID 与 Codex/Claude Session ID、generation、health、last ack commit/update 持久化绑定。
- [x] 禁止 Session 跨 run、跨 workspace、跨 Profile 复用。
- [x] Session 丢失/损坏/不确定时 generation +1，按 GoalContract、HEAD、共享摘要、问题和验证状态 bootstrap。
- [x] 重建后需对当前 HEAD 返回新 ChangeAck；不强制逐提交复述。

### Memory/Handoff 接口

- [x] 每个 checkpoint 生成 PeerMemoryUpdate 和 ChangeSummary；机器字段来自 Git/Supervisor，语义字段来自 Agent 结构化输出。
- [x] 接手 Agent 在正式审查前通过原生 Session 接收 update，并自主读取 Git/工作空间。
- [x] ChangeAck 至少绑定当前 HEAD、理解、判断和理由；不机械要求 reviewed paths 全覆盖。
- [x] pending update/ack 游标保证未同步不能进入下一正式回合。
- [x] 不传播隐藏推理、完整原始输出或敏感终端日志。

### 验收命令

- [x] RED：Module 3 测试文件因实现缺失而失败。
- [x] GREEN：`python -m pytest agent/debate/tests/test_process.py agent/debate/tests/test_adapters.py agent/debate/tests/test_sessions.py agent/debate/tests/test_memory.py -q` 全部通过。
- [x] 全量 Module 1—3 测试、ruff、mypy 通过。
- [x] Module 3 独立提交完成。

### Module 3 验收证据

- RED：`process`、`adapters`、`sessions`、`memory` 四个导入错误；增强契约分别捕获变更路径缺失、输出截断、静默点、Claude structured output 和重建历史缺口。
- GREEN：Module 3 `19 passed`；Module 1—3 全量 `57 passed`。
- Ruff：`All checks passed!`；Mypy：`Success: no issues found in 13 source files`；`git diff --check` 通过。
- 本机 CLI 契约对照：Codex `0.137.0` 的 `exec/resume --help` 与 Claude Code `2.1.138 --help`；Codex resume 未暴露独立 sandbox，因此默认拒绝续接，待 Module 6 真实 smoke 证明后才可开启。

## Module 4：GoalContract、权威验证与 LangGraph 多轮编排

### 文件

- [x] 创建 `goal_contract.py`、`validation.py`、`graph.py`。
- [x] 创建 `tests/test_goal_contract.py`、`tests/test_validation.py`、`tests/test_graph.py`。

### GoalContract 接口

- [x] 保存 objective、deliverables、acceptance criteria、baseline/added validation、allowed/protected/derived scope、immutable constraints 和 version。
- [x] 两个 Agent 首次写入前确认当前版本。
- [x] 原目标、交付物、不可弱化约束、基线验证不可删除或放宽。
- [x] 新验收/验证只增不减；命令先通过 Policy。
- [x] 必要派生范围仅在双方一致时加入新版本；新增功能/顺手重构进入 `scope_blocked`。

### Validation 接口

- [x] Supervisor 运行 setup、baseline、checkpoint、final validation；记录 argv、cwd、退出码、耗时、脱敏摘要。
- [x] Agent 自报测试不是权威结果。
- [x] setup 默认无网络；失败为 `preflight_blocked`。
- [x] checkpoint 验证失败可继续；final validation 未满足不得进入 completed。

### LangGraph 接口

- [x] 实现方案列出的全部节点和条件边，State 只存小对象/引用。
- [x] 顺序覆盖 preflight → lock/worktree/setup/contract/session → sync/ack/lease/write/quiesce/policy/validate/commit/handoff/review。
- [x] progress gate 正确处理换手、派生范围、验证增量、停滞、max turns 和失败预算。
- [x] blind ballots 串行收集且互不可见；任一 incomplete 继续，双 complete 才进入审计。
- [x] 投票前后检测 Git 变化；Reviewer/终审无权写入。
- [x] 使用 SQLite Checkpointer；run ID 作为 thread ID；最新 checkpoint 可 resume。
- [x] 所有非 completed 状态明确 stop reason，不误判成功。

### 验收命令

- [x] RED：Module 4 三个测试文件因实现缺失而失败。
- [x] GREEN：`python -m pytest agent/debate/tests/test_goal_contract.py agent/debate/tests/test_validation.py agent/debate/tests/test_graph.py -q` 全部通过。
- [x] 图测试覆盖失败 checkpoint 被对方修复、Session 重建、双 complete、预算/停滞/策略/集成分支。
- [x] 全量 Module 1—4 测试、ruff、mypy 通过。
- [x] Module 4 独立提交完成。

### Module 4 验收证据

- RED：`goal_contract`、`validation`、`graph` 三个导入错误；增强测试随后捕获“最终验证否决后无法重新终审”的真实状态机缺陷。
- GREEN：Module 4 `15 passed`；Module 1—4 全量 `72 passed`。
- SQLite：测试在 `writer_turn` 后中断，关闭并重新打开数据库，再以同一 run/thread ID 从 `quiesce_workspace` 继续直至 completed。
- 盲投票：Codex/Claude 串行收到不含任何 ballots 的同一候选深拷贝，投票前后 workspace fingerprint 必须一致，且 ballot 绑定相同 HEAD/GoalContract 版本。
- Ruff：`All checks passed!`；Mypy：`Success: no issues found in 16 source files`；`git diff --check` 通过。

## Module 5：仓库内审计、final tree 与单提交自动集成

### 文件

- [x] 创建 `audit.py`、`tests/test_audit.py`，扩展 graph/git workspace 测试。

### Audit 接口

- [x] 默认生成 `.agent/debate/<run_id>/goal-contract.md`、`final-summary.md`、`change-summary.md`、`decisions.jsonl`、`validation-report.md`。
- [x] 内容包含目标/契约/派生范围、最终行为、双方贡献、关键争议、验证与终审结论。
- [x] 不包含隐藏推理、raw output、Session ID、绝对路径、环境变量、认证信息或重复大段代码。
- [x] 审计目录只允许 Supervisor 写；Agent 仅能返回 audit dispute。
- [x] 非完成状态在临时 worktree 生成脱敏终止审计，不暂存违规内容。

### Final tree 与集成接口

- [x] 双 complete 后生成审计，暂存最终代码和审计并计算 `final_tree_sha`。
- [x] Codex/Claude 分别只读确认相同 tree；有 dispute 只重生成审计，不修改产品代码。
- [x] tree ack 前后检测工作树无写入。
- [x] `git commit-tree` 以 base commit 为唯一 parent，最终 commit 不携带中间历史。
- [x] 最终 message/trailer 包含 run、agents、GoalContract、final tree、tree ack、validation、audit path。
- [x] 目标 branch 仍在 base 且源工作区干净时 fast-forward；完成后 tree 相等。
- [x] 成功清理临时 branch/worktree/敏感运行产物；integration blocked 保留恢复资源。

### 验收命令

- [x] RED：审计与 final tree 测试因实现缺失而失败。
- [x] GREEN：`python -m pytest agent/debate/tests/test_audit.py agent/debate/tests/test_graph.py agent/debate/tests/test_git_workspace.py -q` 全部通过。
- [x] 全量 Module 1—5 测试、ruff、mypy、build 通过。
- [x] Module 5 独立提交完成。

### Module 5 验收证据

- RED：`debate_agent.audit` 导入缺失导致审计测试收集失败。
- GREEN：审计/final tree/graph/Git 联合 `20 passed`；Module 1—5 全量 `76 passed`。
- 审计：固定五文件集合；显式输入模型不接收 raw/session 字段；测试证明本机绝对路径、Token、Session ID、hidden reasoning/raw output 标识不会进入仓库审计。
- Final tree：只暂存 `.agent/debate/<run_id>`，tree ack 前后 fingerprint 不变；任一拒绝、产品代码漂移或 final validation 非 passed 均阻断提交。
- 集成：最终提交以 base 为唯一 parent，目标分支仅新增一个提交，tree 等于代码+审计 final tree，所有规定 trailer 完整。
- Ruff、Mypy、Build 和 `git diff --check` 均通过。

## Module 6：幂等恢复、完整 CLI、文档和真实 Smoke Test

### 文件

- [x] 创建 `recovery.py`、`artifacts.py`、`tests/test_recovery.py`。
- [x] 完成 `cli.py`、`README.md`、`.gitignore` 和确定性运行 fixture。

### Artifacts/Recovery 接口

- [x] `.runtime/runs/<run_id>` 包含 config/state/checkpoint/lock/sessions/contracts/updates/acks/leases/invocations/validation/raw/status。
- [x] 所有 JSON/摘要使用临时文件 + `os.replace` 原子写入。
- [x] invocation 写前记录 prepared/PID/result/commit/checkpoint；节点重跑复用完整结果。
- [x] 可归属脏改动重新 policy/validation，并形成 recovered checkpoint；来源不明进入 recovery blocked。
- [x] 不盲目重放可能已推进 Session 的 Prompt；格式重试仅在工作树指纹未变化时按配置至多一次。
- [x] `resume` 只接受最新 checkpoint；CLI 不接受旧 checkpoint 参数，避免在原 run 回退执行。
- [x] `abort` 停止并保留恢复资源；`cleanup` 仅清理明确 run ID；成功集成清理敏感 raw/session/lock。

### 完整 CLI/文档

- [x] `run/resume/status/abort/cleanup` 可运行，状态和退出码与终止状态一致。
- [x] README 覆盖安装、配置、权限、安全边界、示例、恢复、状态、失败排查和不自动 push。
- [x] 运行过程不请求用户中途裁决；重大歧义进入非完成状态。
- [x] 默认预算：8 写入回合、单次 1200 秒、总运行 7200 秒、连续进程失败 2、同问题两回合停滞。

### 真实 Smoke Test

- [x] 创建小型可信干净 Git fixture，记录 base commit/tree。
- [x] 真实 Codex 与 Claude 使用不同稳定 Session，在同一共享 worktree 交替修改。
- [x] 第一个 Agent checkpoint 包含正确归属；第二个读取 Git 并修订。
- [x] 普通失败验证可交给对方修复。
- [x] 确定性真实 Git fixture 中人为中断后 Supervisor、Session 状态和脏工作树可恢复且不重放 Prompt。
- [x] 双方独立终审并确认包含代码+审计的相同 final tree。
- [x] 目标 branch 只增加一个聚焦提交，tree 一致，中间 branch/worktree 清理。
- [x] 运行期间无 Agent Git 状态变更、外部路径写入、部署或 push。
- [x] 模拟目标 branch 漂移时不强行合并。
- [x] 保存脱敏 Smoke Test 结果，不保存 Session ID 和原始敏感输出。

### 验收命令

- [x] RED：恢复/CLI 测试因行为缺失而失败。
- [x] GREEN：`python -m pytest agent/debate/tests/test_recovery.py agent/debate/tests/test_cli.py agent/debate/tests/test_graph.py -q` 全部通过。
- [x] `python -m pytest agent/debate/tests -q` 全部通过且无警告。
- [x] `python -m ruff check agent/debate/src agent/debate/tests` 通过。
- [x] `python -m mypy agent/debate/src` 通过。
- [x] `python -m build agent/debate` 成功。
- [x] 真实 Smoke Test 清单全部通过。
- [x] Module 6 已保存独立实现提交，并以真实双 CLI follow-up 修复完成最终验收。

### Module 6 最终证据

- RED：`artifacts`/`recovery`/真实 lifecycle 初始缺失，测试按预期失败；恢复增强测试随后捕获 read-only journal 干扰唯一归属问题。
- GREEN：确定性真实 Git E2E 覆盖失败 checkpoint 换手修复、双 complete、审计/final tree、单提交集成、硬中断恢复、abort/cleanup 和安全格式重试。
- 最新本地验收：`90 passed`；Ruff、严格 Mypy、wheel/sdist Build、`git diff --check`、占位扫描和中文 docstring 扫描全部通过。
- 独立实现提交：`076695ff feat: add recoverable CLI debate runtime`。
- 真实 Codex 首次 sandbox + Schema 调用成功；当前默认模型与 Codex CLI 0.137.0 不兼容，显式 `gpt-5.4` 可用。
- 运行时等价的 Codex resume 探针固定 cwd 并显式传入 `sandbox_mode="workspace-write"`：同 Session 续接只能写 worktree 内，外部兄弟路径写入失败；开关仍默认 false，本次版本级验收后显式开启。
- Claude Code 2.1.211 的真实结构化调用可用；真实 run `RUN-20260716-175035-c85421a5` 在两个写回合后完成失败验证修复、双 complete ballot、同一 final tree 双 Ack、单提交集成和敏感产物清理。
- 真实 final tree：`249ba498fadaedd5668a82ab54f5d9a5dc7b97ed`；目标分支相对 base 仅增加一个 single-parent 提交，详见 `SMOKE_TEST.md`。

## 最终完成审计

- [x] 对照架构方案逐条复核目标、非目标、节点、状态、接口、配置、安全、恢复、审计和验收，无缺项。
- [x] 检查 Module 1—6 Git Commit，每个模块均有独立验收证据且没有跨模块混杂。
- [x] `git diff <开发基线>...HEAD --check` 通过。
- [x] `git diff <开发基线>...HEAD --stat` 仅包含 `agent/debate` 及本任务方案/清单所需文件。
- [x] 搜索生产代码不存在 `TODO`、`FIXME`、`pass` 占位、`NotImplementedError` 或空实现。
- [x] 全量 pytest、ruff、mypy、build 再次从干净环境通过。
- [x] 复核所有业务函数/方法都有中文 docstring。
- [x] 复核没有自动 push、发布、部署或生产外部写入。
- [x] 记录所有模块、Commit、验证结果、未完成项、无关改动和遗留风险。
- [x] 使用 `finishing-a-development-branch` 完成分支收尾。

### 最终审计证据

- 模块提交：Module 0 `d54ca78d`/`42b4e52f`；Module 1 `89947ff0`；Module 2 `f10c0e2b`；Module 3 `2fd7021b`；Module 4 `ce92f4ee`；Module 5 `eadbbff7`；Module 6 `076695ff`，真实 CLI follow-up `4ea9e20c`；阶段验收记录 `1ea0bb9b`。
- 方案覆盖：29 个 LangGraph 节点、GoalContract、单写者租约、Git checkpoint/handoff、双 Session 记忆、恢复、验证、blind ballot、审计、TreeAck、单提交集成及完整 CLI 均有实现与测试。
- 质量结果：全量 `90 passed`；Ruff、严格 Mypy、wheel/sdist Build 和开发基线到 HEAD 的 `git diff --check` 通过。
- 静态审计：生产代码无 `TODO`、`FIXME`、`pass`、`NotImplementedError` 占位；258 个函数/方法均有中文 docstring。
- 变更范围：开发基线后共 47 个文件，仅位于 `agent/debate` 和必要的本方案文档；没有 `.DS_Store` 或其他业务目录改动。
- 真实验收：Codex 0.137.0 与 Claude Code 2.1.211 完成失败验证交接、双 complete、同一 final tree 双 Ack、single-parent 聚焦提交和成功清理；详见 `SMOKE_TEST.md`。
- 未完成项：无。无关改动：无。未 push、发布或部署。
- 遗留风险：真实 CLI 行为仍受版本、模型服务和账号资源影响；Codex resume 证明只绑定本报告版本/固定 cwd/显式 sandbox 配置，默认开关保持关闭；无法依据机器记录改变 tree 的审计异议会明确进入 `integration_blocked`，需要修复事实来源后再 resume。
- 分支收尾：开发分支 `codex/cli-debate-agent` 与 worktree 原样保留，未获授权不合并、不 push、不删除。
