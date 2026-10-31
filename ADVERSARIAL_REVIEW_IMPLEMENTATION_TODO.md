# 对抗审查实施验收清单

> 依据：`ADVERSARIAL_REVIEW_PLAN.md`、`ADVERSARIAL_REVIEW_PROTOCOL.md`、`ADVERSARIAL_REVIEW_VALIDATION.md`
> 执行规则：每个模块必须先完成全部验收命令、更新本清单、创建独立 Git Commit，才能开始下一模块；不执行 `git push`。模块 1--5（Phase 2）仅可在 Phase 1 阶段报告达到 Go 后开始；Phase 1 为 No-Go 时，只能在其独立提交中继续调整 Prompt 并重跑基准，禁止实施结构化协议、Ledger 或 Ballot。

## 全局不变量

- [x] 所有新增公开 Python 方法均包含中文 docstring，说明功能、参数、返回值和异常。
- [x] 项目代码兼容 Python >=3.10；基线安装、测试、Ruff、Mypy 和构建验证固定在 Python 3.11+ 隔离环境执行。
- [x] Writer 继续使用 `TurnResult`；Reviewer 使用独立 `ReviewResult`；不得把 Reviewer 字段塞回 Writer 协议。
- [x] 旧 Run 保持 `legacy-v0` 语义；不得补造 Review、Evidence 或 Ledger 状态。
- [x] 仅 Supervisor 持久化 Review/Ledger 并创建 Git 提交；Reviewer、Facilitator、Ballot 阶段严格只读。
- [x] Facilitator 只能读取投影和输出现有 `FacilitatorDecision`；不得创建权威证据、修改 Ledger 或宣布完成。
- [x] 现有单 Writer、GoalContract、最终验证、全员 Blind Ballot、TreeAck 和集成/保留分支门禁不得回归。
- [x] 每个模块提交前运行：对应 pytest、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build`、`git diff --check`。
- [x] 每个阶段报告必须记录固定控制变量、逐次运行原始聚合指标、Go/No-Go 判定和允许继续的阶段；报告不能以单次成功替代重复基准。

## 模块 0：实施计划与干净基线

- [x] 创建本清单和 `docs/superpowers/plans/2026-07-24-adversarial-review.md`，逐项映射三份设计文档、源文件、测试文件和验收命令。
  - 验收：覆盖协议版本、EvidenceRef、ReviewResult、ReviewPolicy、持久化/恢复、Reviewer 调用、Facilitator、Ledger、Ballot、Audit、UI、基准和真实 CLI 验收；无 `TODO` 或占位步骤。
- [x] 在 Python 3.11+ 隔离环境安装 `.[dev,ui]`，记录基线测试、Ruff、Mypy 和构建结果。
  - 验收：2026-07-24 基线为 `408 passed`、Ruff 通过、Mypy 通过、sdist/wheel 构建通过。
- [x] 提交规划模块。
  - 提交范围：仅实施计划/清单文档，不包含功能代码。

## Phase 1：Prompt 对照实验（结构化协议前置门禁）

- [x] 新建固定 fake-CLI 基准 Fixtures 与 `tests/test_adversarial_review_prompt_benchmark.py`，只复用现有 Writer `TurnResult` 及当前运行路径；本阶段**不得**实现 `ReviewResult`、JSON Schema、结构化协议、Ledger 或 Ballot 改动。
- [x] 使用同一固定 fake-CLI 任务集执行“当前 Reviewer Prompt”与“四面 Prompt”（验收条件遗漏、边界/反例、集成兼容、验证证据）对照。每组至少连续运行 3 次；报告必须逐次固定并记录全部控制变量：Agent 组合、每个 Agent 的 model/variant/推理配置、初始 Commit SHA、GoalContract、验证命令、`max_turns`、超时、Facilitator 是否启用，以及启用时 Facilitator 的 model/variant/推理配置；同时固定任务集、随机种子（若运行器支持）和无用户参与。任何变量变化必须重新开始该组。
- [x] 冻结 Go/No-Go 阈值：在 3 次配对运行的每一次中，四面 Prompt 对已知阻断缺陷的发现数不得低于当前 Prompt；三次合计已知阻断缺陷发现率必须至少提高 10 个百分点；无证据阻断和风格误报的合计次数不得高于当前 Prompt；平均总 CLI 调用数与平均轮次均不得增加超过 20%；三次配对运行中，干净任务均不得因四面 Prompt 在不存在阻断 Finding 时返回 `incomplete`，且四面 Prompt 的干净任务完成率不得低于基线。基准集必须同时含已知阻断、遗漏验收、无证据完成、风格诱饵与干净任务，并逐项列出期望结论。
- [x] 生成 `docs/superpowers/reports/adversarial-review-phase-1.md`，逐次记录任务结果、质量/成本指标、失败或降级、环境与控制变量，并明确 `Go` 或 `No-Go`。仅当全部冻结阈值达到时报告为 Go，才允许进入模块 1；任何阈值不满足即 No-Go，只允许继续 Prompt 调整和重跑本 Phase 1。
  - 验收：`.venv/bin/python -m pytest tests/test_adversarial_review_prompt_benchmark.py -q`；`ruff check src tests`；`mypy src`；`python -m build --outdir /tmp/debate-agent-build`；`git diff --check`。测试必须断言控制变量、三次运行、指标聚合和阈值判定可重复。
- [x] 提交 Phase 1。
  - 提交范围：`tests/test_adversarial_review_prompt_benchmark.py`、`tests/fixtures/adversarial_review_prompt/**`、`docs/superpowers/reports/adversarial-review-phase-1.md`，以及为该基准唯一新增的最小辅助文件；不得包含任何 `src/debate_agent/**` 结构化协议功能代码。
  - 提交门禁：上述验收通过、阶段报告结论明确且仅提交范围内有本阶段改动；独立提交后才可按报告继续。No-Go 提交不授权任何后续模块。

## 模块 1：协议版本和配置入口

> Phase 2 起点门禁：仅在 `docs/superpowers/reports/adversarial-review-phase-1.md` 的最新、可复现报告为 Go，且其独立提交已完成时实施；否则本模块及后续模块不得开始。

- [x] 在 `src/debate_agent/models.py` 定义 `ReviewProtocolVersion`：`LEGACY_V0`、`REVIEW_V1`；在 `RunConfig` 增加冻结且可序列化的外部字段 `protocol_version`。
- [x] 在 `src/debate_agent/config.py` 实现旧 `config.json` 缺失 `protocol_version` 时显式写入/加载为 `legacy-v0`；新 Run 必须通过显式 CLI/UI 输入选择 `review-v1`。`config.json` 只使用 `protocol_version`，不得保留双字段或隐式字段映射。
- [x] 在 `src/debate_agent/cli.py` 增加 `--protocol-version {legacy-v0,review-v1}`；在 `src/debate_agent/ui/app.py` 和 `ui/view_models.py` 以 `protocol_version` 展示并提交该输入。
- [x] 更新 `tests/test_models.py`、`tests/test_cli.py`、`tests/test_ui_app.py`、`tests/test_ui_view_models.py`。
  - 验收：旧配置缺失字段时显式映射为 `legacy-v0`；新 CLI/UI RunConfig 的冻结序列化字段和 `config.json` 均为 `protocol_version`，恢复后版本不变；不得出现旧字段、双字段或隐式映射；非法版本被拒绝。
- [x] 提交模块 1。
  - 提交范围：`src/debate_agent/models.py`、`src/debate_agent/config.py`、`src/debate_agent/cli.py`、`src/debate_agent/ui/app.py`、`src/debate_agent/ui/view_models.py`、`tests/test_models.py`、`tests/test_cli.py`、`tests/test_ui_app.py`、`tests/test_ui_view_models.py`。因旧 `config.json` 的协议版本兼容性修复，额外允许 `src/debate_agent/artifacts.py` 与 `tests/test_artifacts.py`。
  - 提交门禁：模块 1 验收通过，且仅上述路径有本模块改动；运行相关 pytest、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build` 和 `git diff --check` 均通过。

## 模块 2：Review 协议模型和 JSON Schema

- [x] 新建 `src/debate_agent/review_models.py`，定义：`EvidenceKind`、`EvidenceRef`、`ReviewDimension`、`ReviewCoverageStatus`、`ReviewCoverage`、`ReviewFinding`、`ReviewResult`。
- [x] `ReviewResult` 必须绑定 `agent`、`turn_id`、`head_sha`、GoalContract 版本和协议版本；四个核心 Coverage 各出现一次；`unknown` 或阻断 Finding 时禁止 `complete`；`complete` 强制最强反例及处理说明。
- [x] 新建 `schemas/review.schema.json`，并更新 Hatch force include、Schema 注册、Gateway 泛型支持。
- [x] 新增 `tests/test_review_models.py` 与扩展 `tests/test_models.py`。
  - 验收：Pydantic/JSON Schema 字段和枚举一致；缺失/重复维度、非法完成判断、无证据阻断项、错误绑定均被拒绝；Writer `TurnResult` 无新增 Reviewer 必填字段。
- [x] 提交模块 2。
  - 提交范围：`src/debate_agent/review_models.py`、`schemas/review.schema.json`、`pyproject.toml`、`src/debate_agent/protocol_gateway.py`、`tests/test_review_models.py`、`tests/test_models.py`，以及仅用于同步模块状态与范围说明的 `ADVERSARIAL_REVIEW_IMPLEMENTATION_TODO.md`。
  - 提交门禁：模块 2 验收通过，且仅上述路径有本模块改动；运行相关 pytest、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build` 和 `git diff --check` 均通过。

## 模块 3：EvidenceRef 与 ReviewPolicy

- [x] 新建 `src/debate_agent/review_policy.py`，实现 `EvidenceResolver` 和 `ReviewPolicy`。
- [x] 校验 `git_path`、`git_diff`、`validation_report`、`test_result` 四类引用：拒绝绝对路径、`..`、不存在路径、错误 HEAD、非 Supervisor 产物和过期报告。
- [x] 通过 `GitWorkspace`/同一 run 的 `ArtifactStore` 解析当前 HEAD；验证报告和原始测试结果必须具有 Supervisor 产物证明及内容摘要，不允许 Agent 自述、Peer Memory 或伪造 JSON 作为机器权威 Evidence。
- [x] 新增 `tests/test_review_policy.py`，补充 `tests/test_git_workspace.py` 和 `tests/test_validation.py`。
  - 验收：合法当前 Commit 文件和同 HEAD ValidationReport 通过；所有非法来源失败并给出可诊断错误；Policy 不判断业务语义。
- [x] 提交模块 3。
  - 提交范围：`src/debate_agent/review_policy.py`、`src/debate_agent/git_workspace.py`、`src/debate_agent/validation.py`、`src/debate_agent/artifacts.py`、`tests/test_review_policy.py`、`tests/test_git_workspace.py`、`tests/test_validation.py`、`tests/test_artifacts.py`，以及仅用于同步模块状态与范围说明的本清单。
  - 提交门禁：模块 3 验收通过，且仅上述路径有本模块改动；运行相关 pytest、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build` 和 `git diff --check` 均通过。

## 模块 4：Review 持久化、协议网关与恢复

- [x] 扩展 `ArtifactStore` 增加 `reviews/` 目录，以及按 `head_sha + reviewer + turn_id` 幂等读写 `ReviewResult` 的方法。
- [x] 扩展 `protocol_gateway.py`、`protocol_normalizer.py`，支持 Reviewer 的 `review.schema.json`；Normalizer 仅恢复格式，禁止补造 Coverage、Finding 或 EvidenceRef。
- [x] 为 `RunConfig.protocol_version` 增加恢复分支：`legacy-v0` 不读取 Review；`review-v1` 缺失/损坏 Review 明确失败且写诊断。
- [x] 新增或扩展 `tests/test_artifacts.py`、`tests/test_protocol_gateway.py`、`tests/test_protocol_normalizer.py`、`tests/test_recovery.py`。
  - 验收：重复写幂等；错误身份、重复不同内容、损坏文件均失败；旧 Run 可恢复；新协议 Run 不静默降级为完成。
- [x] 提交模块 4。
  - 提交范围：`src/debate_agent/artifacts.py`、`src/debate_agent/protocol_gateway.py`、`src/debate_agent/protocol_normalizer.py`、`src/debate_agent/config.py`、`tests/test_artifacts.py`、`tests/test_protocol_gateway.py`、`tests/test_protocol_normalizer.py`、`tests/test_recovery.py`。
  - 提交门禁：模块 4 验收通过，且仅上述路径有本模块改动；运行相关 pytest、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build` 和 `git diff --check` 均通过。

## 模块 5：Reviewer Runtime 集成与过程门禁

- [x] 新建 `src/debate_agent/review_runtime.py`，封装 Reviewer Prompt、只读调用前后指纹检查、ReviewResult 绑定校验和结构化摘要；由 Supervisor 调用并授权 `ArtifactStore` 持久化 ReviewResult，ReviewRuntime 不直接取得独立写入权。
- [x] 修改 `runtime.py`：仅在 `review-v1` 使用 `ReviewRuntime` 调用 Reviewer；legacy 继续原 `TurnResult` 路径；不得新增 LangGraph 节点。
- [x] **时序硬门禁**：任何供 `review-v1` EvidenceRef 使用的 `validation_report` 或 `test_result` 必须在对应 Writer checkpoint 已提交后，由 Supervisor 对该提交后的当前 HEAD 重新执行并登记；不得把 pre-commit checkpoint validation 仅补填 `head_sha` 后作为证据。最终验证和中断恢复也必须在其实际验证时的 current HEAD 登记，若提交导致 HEAD 变化则旧报告只能作为历史记录、不得作为新 HEAD Evidence。
- [x] 扩展 `GraphState`/初始状态保存 `review_result_ref`、当前阻断 Finding 摘要和协议版本；Progress Gate 在 `review-v1` 下阻止未解决阻断项或 `unknown` 进入最终阶段。
- [x] 更新 Facilitator Context：从 ReviewResult 摘要传入未解决标准和合法 EvidenceRef；继续用现有 `FacilitatorDecision` 生成下一轮提示。
- [x] 扩展 `tests/test_runtime.py`、`tests/test_graph.py`、`tests/test_facilitator.py`。
  - 验收：Reviewer 严格只读；Review 绑定当前 HEAD/GoalContract；无 Facilitator 时确定性路由可用；legacy 行为与既有测试一致；新协议的阻断/unknown 无法提前 final。
- [x] 新建 `tests/test_adversarial_review_phase_2_benchmark.py`、固定 Fixtures 和 `docs/superpowers/reports/adversarial-review-phase-2.md`，以与 Phase 1 相同的冻结控制变量（包括完全相同的验证 argv `python -m pytest -q`）重复至少 3 次运行结构化协议。固定 Git 基线含可通过该 argv 的 pytest 用例，避免“未收集测试”退出码掩盖生命周期验证。报告必须逐次记录与 Phase 1 及最终三组对照一致的全部控制变量：Agent 组合、每个 Agent 的 model/variant/推理配置、初始 Commit SHA、GoalContract、验证命令、`max_turns`、超时、Facilitator 是否启用及其 model/variant/推理配置；必须判定协议稳定性、重复 Issue 指纹/次数/比例，并记录过程 `complete` 后最终 Ballot 是否投回 `incomplete`。该报告是 Ledger 与 Ballot 门禁的唯一输入。
  - Go 规则（全部必须通过，任一失败即 No-Go 且不得进入 Ledger）：固定基准中的全部结构化 Review 必须成功解析且通过 Coverage/绑定校验；合法 Evidence 必须 100% 接受，非法 Evidence 必须 100% 拒绝；所有规定的恢复与只读故障注入必须通过；相对通过的 Phase 1 配对结果，已知阻断发现和遗漏验收发现必须不少于，无证据阻断和风格误报必须不多于，干净任务完成率必须不低于。
  - 阶段验收：`.venv/bin/python -m pytest tests/test_adversarial_review_phase_2_benchmark.py -q`、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build`、`git diff --check`。测试必须验证报告的运行数、全部控制变量、结构化/证据/故障注入 Go 规则、质量指标与 Phase 1 的逐项比较，以及两项门禁计数。
- [x] 提交模块 5。
- 提交范围：`src/debate_agent/review_runtime.py`、`src/debate_agent/runtime.py`、`src/debate_agent/graph.py`、`src/debate_agent/models.py`、`src/debate_agent/prompts.py`、`src/debate_agent/facilitator.py`、`src/debate_agent/artifacts.py`、`tests/test_runtime.py`、`tests/test_graph.py`、`tests/test_facilitator.py`、`tests/test_adversarial_review_phase_2_benchmark.py`、`tests/fixtures/adversarial_review_phase_2/**`、`docs/superpowers/reports/adversarial-review-phase-2.md`，以及仅为将共享的确定性验证 argv 重基线为 `python -m pytest -q` 而同步的 `tests/test_adversarial_review_prompt_benchmark.py`、`tests/fixtures/adversarial_review_prompt/**`、`docs/superpowers/reports/adversarial-review-phase-1.md`；该例外只用于保证 Phase 1/Phase 2 的冻结验证命令相同，不得调整 Prompt、任务集、阈值或任何 Phase 1 结论。允许仅用于同步模块 5 状态与提交范围说明的本清单改动。
  - 提交门禁：模块 5 验收通过，且仅上述路径有本模块改动；运行相关 pytest、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build` 和 `git diff --check` 均通过。

## 模块 6：三状态 Issue Ledger 与派生 Peer Memory

> Ledger 实施门禁：仅当 `docs/superpowers/reports/adversarial-review-phase-2.md` 的全部 Phase 2 Go 规则通过（结构化解析/Coverage/绑定、合法/非法 Evidence、恢复与只读故障注入、五项质量指标均不差于通过的 Phase 1 配对结果），且重复 Issue 成本达到预定义阈值，才可开始。阈值冻结为：同一已解决问题在至少 3 次独立完成的基准运行中各被重复提出至少 2 次，且重复 Finding 占全部 Finding 的比例至少 25%；阶段报告必须给出 Issue 指纹、运行 ID、计数与分母。执行前复跑 `.venv/bin/python -m pytest tests/test_adversarial_review_phase_2_benchmark.py -q`，并在模块 6 独立提交中保留该报告；任一 Go 项失败只能继续稳定化协议或基准，不得实施 Ledger。

- [x] 新建 `src/debate_agent/issue_ledger.py`，定义 `IssueStatus`、`LedgerIssue`、`IssueTransition` 和单写、幂等 `IssueLedgerStore`。
- [x] 实现 `OPEN`、`RESOLVED`、`DISMISSED`：Writer 只能登记 `addressed_by_commit`；非 Writer 验证才能 `RESOLVED`；Reviewer 证据否定才能 `DISMISSED`；`duplicate_of` 不继承状态。
- [x] 将 Ledger 仅对 `review-v1` 且显式启用时接入 `ArtifactStore`、`MemoryStore`、Facilitator Context、Audit 和 UI；所有视图从同一 Ledger 投影派生。
- [x] 扩展 CLI/UI 配置增加显式 `--issue-ledger` 开关，默认关闭，避免未通过 Phase 2 数据门槛时自动启用。
- [x] 为已存在的 `OPEN` Issue 增加最小、稳定 ID 绑定的 Reviewer 处置字段；Runtime 只接受
  对本轮注入 Issue 的完整 `open`/`resolved`/`dismissed` 处置，并把关闭证据交给
  ReviewPolicy/EvidenceResolver 校验，Writer `TurnResult` 保持不变。
- [x] 新增 `tests/test_issue_ledger.py`，扩展 `tests/test_memory.py`、`tests/test_audit.py`、`tests/test_ui_app.py`、`tests/test_ui_view_models.py`、`tests/test_recovery.py`。
  - 验收：权限、HEAD、证据和幂等性均受控；中断恢复无漂移；Ledger 损坏明确停止；legacy 不加载 Ledger；UI/Audit/Peer Memory 显示相同阻断项。
- [x] 提交模块 6。
  - 提交范围：`src/debate_agent/issue_ledger.py`、`src/debate_agent/artifacts.py`、`src/debate_agent/memory.py`、`src/debate_agent/facilitator.py`、`src/debate_agent/audit.py`、`src/debate_agent/cli.py`、`src/debate_agent/ui/app.py`、`src/debate_agent/ui/view_models.py`、`tests/test_issue_ledger.py`、`tests/test_memory.py`、`tests/test_audit.py`、`tests/test_ui_app.py`、`tests/test_ui_view_models.py`、`tests/test_recovery.py`、`docs/superpowers/reports/adversarial-review-phase-2.md`。为将显式 opt-in 冻结到 `config.json`、将唯一 Ledger 投影接入现有运行链并验证其恢复边界，额外允许 `src/debate_agent/config.py`、`src/debate_agent/runtime.py`、`tests/test_models.py`、`tests/test_cli.py`、`tests/test_runtime.py` 与本清单。为落实设计中“Reviewer 对已有阻断项确认 resolved/dismissed/open”而避免按 claim 猜测，额外允许最小的 `src/debate_agent/review_models.py`、`src/debate_agent/review_policy.py`、`src/debate_agent/review_runtime.py`、`src/debate_agent/prompts.py`、`schemas/review.schema.json`、`tests/test_review_models.py`、`tests/test_review_policy.py`、`tests/test_protocol_gateway.py`、`tests/test_protocol_normalizer.py` 和 `ADVERSARIAL_REVIEW_PROTOCOL.md` 同步；不得修改 Graph、Writer 协议或 Ballot。
  - 提交门禁：模块 6 验收通过，且仅上述路径有本模块改动；运行相关 pytest、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build` 和 `git diff --check` 均通过。

## 模块 7：可选反证式 Blind Ballot

> Ballot 实施门禁：仅当 `docs/superpowers/reports/adversarial-review-phase-2.md` 中至少 2 次独立完成的固定基准运行出现过程阶段已判定 `complete` 后最终 Ballot 投回 `incomplete`，且报告逐次记录运行 ID、过程结论、最终 Ballot、证据和原因时，才可开始。执行前复跑 `.venv/bin/python -m pytest tests/test_adversarial_review_phase_2_benchmark.py -q`，并在模块 7 独立提交中保留该报告；未达此条件不得实施 Ballot，只能继续收集基准或调整前置阶段。

- [x] 扩展 `Ballot` 和 `ballot.schema.json` 增加 `strongest_counterexample`、`counterexample_resolution`，仅在 `review-v1` 且通过 `src/debate_agent/cli.py` 显式 `--counterexample-ballot` opt-in 时要求；`src/debate_agent/ui/app.py` 必须显示并提交与 CLI 相同的 `counterexample_ballot` 显式 opt-in，`src/debate_agent/ui/view_models.py` 必须传递该字段。
- [x] 修改最终 Ballot Prompt 和收集逻辑：所有 Agent 仍相互不可见、绑定同一 HEAD/GoalContract；存在未解决阻断 Ledger Issue 时必须 `incomplete`。
- [x] 更新 Audit 展示反例及其证据，不重复执行四面 Reviewer 矩阵。
- [x] 扩展 `tests/test_graph.py`、`tests/test_runtime.py`、`tests/test_audit.py`、`tests/test_models.py`、`tests/test_cli.py`、`tests/test_ui_app.py`、`tests/test_ui_view_models.py`。
  - 验收：CLI 与 UI 的 `counterexample_ballot` 默认关闭；UI 启用时向 RunConfig/执行路径传递该字段；启用时字段缺失、绑定错误和未关闭阻断项均被拒绝；未启用时旧 UI 与旧 Ballot 完全兼容；TreeAck/集成门禁不变。
- [x] 提交模块 7。
  - 提交范围：`src/debate_agent/models.py`、`schemas/ballot.schema.json`、`src/debate_agent/runtime.py`、`src/debate_agent/graph.py`、`src/debate_agent/audit.py`、`src/debate_agent/cli.py`、`src/debate_agent/ui/app.py`、`src/debate_agent/ui/view_models.py`、`src/debate_agent/config.py`、`tests/test_graph.py`、`tests/test_runtime.py`、`tests/test_audit.py`、`tests/test_models.py`、`tests/test_cli.py`、`tests/test_ui_app.py`、`tests/test_ui_view_models.py`、本清单。审查修复额外允许 `src/debate_agent/protocol_gateway.py`、`src/debate_agent/protocol_normalizer.py`、`tests/test_protocol_gateway.py`、`tests/test_protocol_normalizer.py`，确保启用反例投票时转码器不能补造字段。Phase 2 报告保留为前置门禁证据，不为重复记录重写。
  - 提交门禁：模块 7 验收通过，且仅上述路径有本模块改动；运行相关 pytest、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build` 和 `git diff --check` 均通过。

## 模块 8：基准、故障注入、真实 CLI 验收和发布前审计

- [x] 新建 `tests/test_adversarial_review_benchmark.py` 和固定 fake-CLI 基准 Fixtures，完整执行三组对照：当前 Prompt、Phase 1 四面 Prompt、Phase 2 结构化版本。三组每次运行必须使用并记录相同的全部控制变量：Agent 组合、每个 Agent 的 model/variant/推理配置、初始 Commit SHA、GoalContract、验证命令、`max_turns`、超时、Facilitator 是否启用及其 model/variant/推理配置，以及任务集、随机种子（若支持）和重复次数（每组至少 3 次）；覆盖遗漏验收条件、边界错误、集成兼容、验证缺口、无证据完成声明、风格误报和干净任务。
- [x] 在 `src/debate_agent/review_benchmark.py` 新建结果报告生成器，并生成 `docs/superpowers/reports/adversarial-review-final-benchmark.md`；逐次写入 Run/Audit 结构化摘要且不记录隐藏推理或完整原始输出。质量指标必须包括：已知阻断发现、遗漏验收、无证据阻断、风格误报、重复、Ballot 推翻；成本指标必须包括：输入/输出 Token、耗时、轮次、总 CLI 调用、`max_turns`/`stalled`、用户参与；可靠性指标必须包括：Coverage 缺失、非法 Evidence、Output 修复/降级、Session 上下文丢失、恢复绑定错误。
- [x] 增加 Structured Output 截断、非法 Evidence、Reviewer 写入、Facilitator 越权、Worker 中断、Session 丢失、HEAD 变化和 CLI 不可用的故障注入。
- [x] 执行 2/3/4 Agent fake 集成矩阵和三组对照的重复运行；真实 CLI 验收必须逐项执行 Codex+Claude、Codex+Qoder、Claude+OpenCode、一个三 Agent 组合、一个四 Agent 组合的最小隔离任务。每个组合都必须记录预检、执行命令、Run ID、终态和验收证据；不得写成可选，也不得以凭据或 CLI 缺失跳过。最终报告必须按 Phase 1、Phase 2、Ledger、Ballot 分别声明可继续、不可继续或因门禁未触发而未实施，并引用对应阈值结果。
- [x] 运行全量 pytest、Ruff、Mypy、Build、`.venv/bin/debate-agent --help`、`.venv/bin/debate-agent ui --help` 和 UI health 冒烟；UI 启动/终止必须由测试或脚本受控，并使用 `curl -fsS http://127.0.0.1:<port>/_stcore/health` 检查健康状态；检查方案与实际 Git 提交覆盖，确认无无关改动。
  - 验收：`pytest -q`、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build`、`.venv/bin/debate-agent --help`、`.venv/bin/debate-agent ui --help`、由测试或脚本受控启动/终止 UI 后执行 `curl -fsS http://127.0.0.1:<port>/_stcore/health`，以及 `git diff --check` 全部通过；基准报告完整列出三组、固定控制变量与每次运行，证明结构化版本不降低已知阻断发现且干净任务不稳定误判，并明确各阶段门禁结论。未达到 Ledger/Ballot 门禁属于有效 No-Go，不能以“无未完成模块”掩盖。
- [x] 提交模块 8。
  - 提交范围：`src/debate_agent/review_benchmark.py`、`tests/test_adversarial_review_benchmark.py`、`tests/fixtures/adversarial_review/**`、`src/debate_agent/audit.py`、`src/debate_agent/artifacts.py`、`docs/superpowers/reports/adversarial-review-*.md`。真实 CLI 验收发现的 strict response-format 兼容性缺口，额外允许最小修复 `schemas/review.schema.json`、`schemas/ballot.schema.json`、`src/debate_agent/prompts.py`、`src/debate_agent/adapters.py`、`tests/test_review_models.py`、`tests/test_models.py`、`tests/test_adapters.py`；Ballot 反例字段仅在 schema 中显式 nullable 且必现，不改变运行时 `counterexample_ballot` opt-in 语义；不得借此放宽 Evidence/只读策略。
  - 提交门禁：模块 8 验收通过，且仅上述路径有本模块改动；运行全量 pytest、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build`、`.venv/bin/debate-agent --help`、`.venv/bin/debate-agent ui --help`、由测试或脚本受控启动/终止 UI 后执行 `curl -fsS http://127.0.0.1:<port>/_stcore/health` 和 `git diff --check` 均通过。五个真实 CLI 组合中任一组合未完成（包括 CLI 或凭据不可用），必须写入阻塞报告；模块 8 不得标记完成或提交为完成，且不得以 fake 结果替代该组合。

## 最终验收

- [x] 审计每一条三份设计文档的功能要求，给出对应代码、测试和 Commit 证据。
- [x] 确认所有模块已独立提交且没有越过验收门禁。
- [x] 运行全量验证：`pytest -q`、`ruff check src tests`、`mypy src`、`python -m build --outdir /tmp/debate-agent-build`、CLI/UI 冒烟和 `git diff --check`。
- [x] 检查 `git log` 与分支差异，列出方案外改动、遗留风险和无法自动完成的真实 CLI 验收条件。
- [x] 核验五个真实 CLI 组合（Codex+Claude、Codex+Qoder、Claude+OpenCode、一个三 Agent 组合、一个四 Agent 组合）均已完成并留有验收证据；任一组合未完成时必须保留阻塞报告，模块 8 与最终验收均不得标记完成，且不得以 fake 结果替代。
