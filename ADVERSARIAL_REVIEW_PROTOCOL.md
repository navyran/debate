# 对抗审查最小协议

> 状态：Phase 2 候选协议，不在 Phase 1 实施
> 原则：只保存单一事实来源；派生摘要不复制状态

## 1. 协议版本

每个 Run 在创建时固定：

```json
{
  "protocol_version": "review-v1"
}
```

- 已存在 Run 继续使用创建时的协议；
- 协议版本在运行期间不可升级；
- 新版本不能要求旧 Run 补造字段；
- Protocol Normalizer 只能修复表示形式，不能生成业务结论或证据。

## 2. EvidenceRef

```json
{
  "kind": "git_path",
  "head_sha": "0123456789abcdef",
  "artifact_ref": null,
  "path": "src/example.py",
  "locator": "ExampleService.run"
}
```

字段：

- `kind`：`git_path`、`git_diff`、`validation_report` 或 `test_result`；
- `head_sha`：证据绑定的 Commit；
- `artifact_ref`：Supervisor 运行产物的相对路径；
- `path`：Git 中的仓库相对路径；
- `locator`：函数、类、测试名或行号等非权威定位提示。

校验规则：

- 所有引用必须使用仓库或 Run 目录相对路径；
- 禁止绝对路径和 `..`；
- `git_path`、`git_diff` 必须能在 `head_sha` 中解析；
- `validation_report`、`test_result` 必须由 Supervisor 生成；
- 运行产物中的 HEAD 必须等于 EvidenceRef 的 `head_sha`；
- Agent 自述、Peer Memory 和自由文本摘要不能作为权威 EvidenceRef。

Supervisor 只校验存在性、来源和绑定关系，不判断证据的业务充分性。

## 3. ReviewCoverage

四个核心审查面：

```text
goal_scope
correctness
integration
validation
```

每个核心面返回：

```json
{
  "dimension": "correctness",
  "status": "pass",
  "summary": "已检查主流程、异常路径和本轮受影响状态转换",
  "evidence_refs": []
}
```

`status`：

- `pass`：未发现阻断问题；
- `challenge`：存在对应 Finding；
- `unknown`：证据不足，当前不能判断完成。

核心审查面不使用 `not_applicable`。可选风险 Finding 不要求出现在 Coverage 中。

`pass` 不要求为每个检查动作生成大量引用，但 Reviewer 判断整体 `complete` 时，至少
需要为 GoalContract 的交付和验证结论提供可验证证据。

## 4. ReviewFinding

```json
{
  "finding_id": "FIND-claude-REVIEW-TURN-003-01",
  "dimension": "integration",
  "claim": "恢复路径仍然读取旧版状态字段",
  "impact": "恢复运行可能在写租约发放前失败",
  "blocking": true,
  "evidence_refs": [
    {
      "kind": "git_path",
      "head_sha": "0123456789abcdef",
      "path": "src/runtime.py",
      "artifact_ref": null,
      "locator": "recover_run"
    }
  ],
  "proposed_change": "在协议版本分支中保留旧字段读取"
}
```

规则：

- `claim` 必须可被代码、Git 或验证结果证伪；
- 阻断 Finding 必须说明对 GoalContract 或用户结果的影响；
- `proposed_change` 只要求最小修复方向，不要求完整实现方案；
- 无 EvidenceRef 的 Finding 只能作为非阻断观察项；
- 风格偏好不能成为阻断 Finding。

## 5. ReviewResult

Reviewer 使用独立 `ReviewResult`，不继续扩大 Writer 的 `TurnResult`：

```json
{
  "agent": "claude",
  "turn_id": "REVIEW-TURN-003",
  "head_sha": "0123456789abcdef",
  "goal_contract_version": 2,
  "protocol_version": "review-v1",
  "coverage": [],
  "findings": [],
  "issue_dispositions": [],
  "strongest_counterexample": "旧 Run 可能无法按原协议恢复",
  "counterexample_resolution": "当前版本按 Run 固定协议，旧 Run 仍走 v1",
  "completion_assessment": "complete",
  "decision": "accept",
  "summary": "四个核心审查面均已检查"
}
```

一致性规则：

- 四个核心 Coverage 必须各出现一次；
- 任一 Coverage 为 `unknown` 时必须 `incomplete`；
- 存在阻断 Finding 时必须 `incomplete`；
- `complete` 时必须提供最强反例及其消除理由；
- `head_sha` 和 GoalContract 版本必须与调用上下文一致；
- Reviewer 调用前后的工作空间指纹必须一致。

当本轮已启用 Ledger 且存在既有 `OPEN` Issue 时，Supervisor 将其稳定 `issue_id`
和必要上下文注入 Reviewer Prompt。`issue_dispositions` 必须对每个注入的 ID 恰好
给出一次：

```json
{
  "issue_id": "ISSUE-0007",
  "status": "resolved",
  "rationale": "已读取 Writer 在当前 HEAD 的处理和权威验证。",
  "evidence_refs": []
}
```

- `open` 保持原状态；
- `resolved` 与 `dismissed` 必须带当前 HEAD 的 EvidenceRef，并由 ReviewPolicy/
  EvidenceResolver 校验；
- `resolved` 仅适用于已有 `addressed_by_commit` 且 Reviewer 不是该 Writer 的 Issue；
- 处置只按稳定 `issue_id` 关联，禁止根据 claim 文本或语义相似度猜测；
- Reviewer 对既有 Issue 使用处置字段，不将同一问题重复创建为新的 Finding。

持久化 Review 重放时，Supervisor 仍使用该 Review 首次调用时已校验的注入 ID
集合；若同一 Review 的 `findings` 已登记为新的 Issue，则以
`opened_head + opened_by + origin_finding_id` 识别并排除，绝不反过来要求该 Review
处置自己的新 Finding。

Writer 继续返回现有 `TurnResult`，避免为 Writer 强制生成无意义的 Reviewer 字段。

## 6. 最小 Issue Ledger

Issue Ledger 仅在 Phase 3 启用。

```json
{
  "issue_id": "ISSUE-0007",
  "origin_finding_id": "FIND-claude-REVIEW-TURN-003-01",
  "status": "open",
  "dimension": "integration",
  "claim": "恢复路径仍然读取旧版状态字段",
  "blocking": true,
  "affected_paths": ["src/runtime.py"],
  "opened_head": "0123456789abcdef",
  "opened_by": "claude",
  "evidence_refs": [],
  "addressed_by_commit": null,
  "addressed_by_agent": null,
  "resolved_head": null,
  "resolved_by": null,
  "resolution_evidence_refs": [],
  "duplicate_of": null
}
```

状态：

```text
OPEN
RESOLVED
DISMISSED
```

转换规则：

- Reviewer 创建 Issue 时进入 `OPEN`；
- Writer 只能填写 `addressed_by_commit`，不能把 Issue 改为 `RESOLVED`；OPEN Issue
  可登记多个真实 checkpoint，转换历史保留全部尝试，而字段保存最新待复核提交；
- 非 Writer 的参与 Agent 验证后可改为 `RESOLVED`；
- Reviewer 用证据证明问题不成立或不适用时可改为 `DISMISSED`；
- Supervisor 只检查转换身份、HEAD 和 EvidenceRef，不作语义裁决；
- `duplicate_of` 只建立关系，不自动继承另一 Issue 的状态；
- 相关路径变化后，不自动重开旧 Issue；Reviewer 应创建新 Finding 并显式关联旧 Issue。

## 7. 单一事实来源

以下数据由 `ReviewResult` 和 Issue Ledger 派生，不分别维护独立状态：

- Peer Memory 中的未决问题；
- Facilitator 的下一轮聚焦提示；
- Audit 中的 Issue 生命周期；
- UI 中的阻断问题列表；
- 最终 Ballot Prompt 中的未关闭阻断项。

禁止把同一 Issue 的状态分别写入多个可变文件。

启用 Ledger 时，每次 Writer checkpoint 的 `opened_issues`、`resolved_issues` 与
`requested_attention` 都从同一个持久化 Ledger 快照投影；不得混入 Writer 自述。
UI 必须先按 `IssueLedger` 模型验证该快照，语义损坏时明确报错而非展示原始 JSON。

## 8. Facilitator 边界

Facilitator 可以读取结构化 Review 和 Ledger 投影，并返回现有
`FacilitatorDecision`：

- 推荐下一 Writer；
- 指出未解决标准；
- 建议两个 Issue 可能重复；
- 生成下一轮 `next_instruction`。

Facilitator不能：

- 创建机器权威 EvidenceRef；
- 改变 Issue 状态；
- 合并或删除 Issue；
- 修改 GoalContract；
- 宣布完成。

Facilitator不可用时，Supervisor继续按现有确定性参与者轮转和完成门禁运行。

## 9. 持久化和恢复

- ReviewResult 以 `head_sha + reviewer + turn_id` 唯一标识；
- 相同标识重复写入必须幂等；
- Ledger 由 Supervisor 单写；
- 每次状态转换记录 Agent、HEAD、时间和来源 Finding；
- 恢复时先验证协议版本，再加载对应模型；
- 损坏的 Review 或 Ledger 使当前 Run 明确停止，不静默生成默认完成状态；
- 审计读取结构化结果，不依赖完整 CLI 原始输出。

## 10. 模块建议

```text
review_models.py
  EvidenceRef、ReviewCoverage、ReviewFinding、ReviewResult

review_policy.py
  EvidenceRef 解析、Coverage 完整性、一致性校验

review_runtime.py
  Reviewer 调用、只读指纹检查、结果持久化

issue_ledger.py
  Phase 3 的 Ledger Store、转换和投影
```

这些模块不得反向负责 Git 提交、Agent Session、目标分支集成或 UI。
