# 多 Agent 一次性对抗审查方案

> 状态：精简后的建议方案，尚未实施
> 目标模块：`agent/debate`
> 核心决策：先用 Prompt 实验证明价值，再按观测结果逐步引入结构化协议
> 配套文档：[最小协议](ADVERSARIAL_REVIEW_PROTOCOL.md) · [验证计划](ADVERSARIAL_REVIEW_VALIDATION.md)

## 1. 结论

本改造有条件接受。

需要解决的核心问题是：CLI Agent 不应因为 Peer Agent 表达合理、声称测试通过或
给出看似完整的实现，就过早接受其结论。Reviewer 应在一次调用中完成多角度、证据
驱动的审查，并把真正阻止完成的问题一次性给出。

但不应一次性引入完整的 Peer Claim 状态机、五状态 Issue Ledger、独立 Challenge
Brief、Writer 六维自审、Reviewer 六维审查和 Ballot 六维复审。它们会重复表达相同
事实，并显著增加恢复、兼容、测试和 Prompt 成本。

推荐方案只保留：

1. 四个固定核心审查面；
2. 一个最强完成性反例；
3. 机器可验证的证据引用；
4. Reviewer 一次性提交全部阻断问题；
5. 必要时才启用三状态 Issue Ledger；
6. 现有全员 Blind Ballot 与 TreeAck 完成门槛。

## 2. 要解决的问题

当前 Writer 和 Reviewer 会读取 Git、代码、测试和 Peer Memory，但审查内容主要依赖
模型自由发挥，存在以下风险：

- 只检查最显眼的代码路径，遗漏目标、集成或验证问题；
- 直接复述对方的“已修复”“已测试”结论；
- 把测试通过等同于完整业务目标已经完成；
- 同一问题在后续回合中被换一种说法重复提出；
- Reviewer 分多轮逐步暴露问题，导致无效拉长讨论；
- `issues=[]` 无法区分“完整审查后无问题”和“未充分审查”。

本方案只针对这些问题，不重新设计整个 Debate Runtime。

## 3. 保持不变的系统约束

以下机制已经存在，本方案只引用，不重新实现：

- 每次只有一个 Agent 持有写租约；
- Writer 修改工作空间，Reviewer 严格只读；
- Supervisor 负责权威验证和 Git 检查点提交；
- Peer Agent 接手前必须读取当前 HEAD 和工作空间变化；
- Facilitator 只给出语义建议，不投票、不关闭争议、不宣布完成；
- Supervisor 只执行确定性门禁，不判断业务观点谁正确；
- 所有参与 Agent 都判断 `complete` 后才能进入最终验证；
- 最终验证通过后，仍需全员 Blind Ballot 和同一 final tree 的 TreeAck；
- 用户可以选择合并目标分支或保留结果分支；
- 常规技术问题不要求用户中途参与。

## 4. 最小审查模型

### 4.1 四个核心审查面

Reviewer 在一次调用中检查以下四个方面。

#### 目标与范围 `goal_scope`

- GoalContract 的交付物和验收条件是否全部落地；
- 是否遗漏用户明确要求；
- 是否出现未经批准的功能、重构或路径扩张；
- 当前修改是否仍处于允许范围内。

#### 正确性 `correctness`

- 主流程、异常流程和边界输入是否正确；
- 状态转换、恢复、幂等和并发假设是否成立；
- Agent 声称修复的问题是否实际被修复；
- 错误处理是否掩盖失败。

#### 集成与兼容 `integration`

- 调用方、被调用方、CLI、配置和 Schema 是否一致；
- UI、CLI、恢复流程和审计是否能读取新行为；
- 是否破坏其他 Agent 组合或已有持久化格式；
- 是否依赖未声明的环境或外部状态。

#### 验证 `validation`

- 验证是否真实运行并绑定当前 HEAD；
- 测试是否覆盖修改行为和主要失败路径；
- 是否用局部测试代替完整完成证明；
- 验证报告是否可以由 Supervisor 复核。

安全、性能、资源泄漏、可观察性和维护成本不是第五个强制矩阵。Reviewer 只在它们与
当前任务相关时作为 Finding 报告，避免机械填写大量 `not_applicable`。

### 4.2 最强完成性反例

Reviewer 必须给出一个最可能推翻“当前目标已经完成”的反例，并说明：

- 反例成立：创建阻断 Finding，并判断 `incomplete`；
- 反例已被消除：引用可验证证据并说明原因；
- 证据暂时不足：判断 `incomplete`，但不能伪造问题或证据。

完成性反例是 Reviewer 的顶层检查方法，不是第五个审查维度。

### 4.3 Finding 的最低要求

有效 Finding 至少包含：

- 稳定 ID；
- 所属审查面；
- 明确且可证伪的主张；
- 对用户目标的影响；
- 是否阻止完成；
- 最小修改建议；
- 至少一个合法证据引用。

风格偏好、无证据猜测和无法说明影响的意见不能成为阻断项。

## 5. 可验证证据

`evidence_refs` 不能继续使用任意自由字符串作为完成门禁。合法证据只能来自：

1. Supervisor 已产生并持久化的运行产物；
2. 当前 Git Commit 中可以解析的对象、路径或 diff。

每条证据必须绑定：

- `kind`；
- `head_sha`；
- `artifact_ref` 或 Git `path`；
- 可选的精确定位信息。

Supervisor 只验证证据存在、类型合法并绑定当前 HEAD，不判断证据是否足以证明业务
观点。证据的语义充分性仍由参与 Agent 在 Reviewer 和 Blind Ballot 阶段判断。

具体结构见 [最小协议](ADVERSARIAL_REVIEW_PROTOCOL.md)。

## 6. 角色协作

### 6.1 Writer

Writer 继续负责：

- 读取当前 HEAD、GoalContract、Peer Memory 和已有阻断问题；
- 修改工作空间并运行必要验证；
- 报告修改摘要、已知风险和完成判断；
- 对上轮阻断问题给出处理 Commit 和证据。

Writer 不执行完整审查矩阵，也不需要重复生成最强反例。完整对抗审查由 Reviewer
负责，避免同一轮重复消耗。

### 6.2 Reviewer

Reviewer 严格只读：

- 自主读取当前 HEAD、Writer Checkpoint、Git diff、代码和验证报告；
- 一次性完成四个核心审查面；
- 提交当前能够识别的全部阻断 Finding；
- 给出最强完成性反例；
- 对已有阻断问题确认 `resolved`、`dismissed` 或继续保持 `open`；
- 独立给出 `Decision` 和 `CompletionAssessment`。

Reviewer 的目标不是尽可能多地找问题，而是避免遗漏真实阻断项。

### 6.3 Facilitator

Facilitator 继续使用现有 `FacilitatorDecision`：

- 汇总仍未解决的阻断项；
- 区分代码缺陷、证据缺口和重复意见；
- 生成下一轮 `next_instruction`；
- 推荐仍为 `incomplete` 的参与者作为下一 Writer。

第一阶段不新增独立 `ChallengeBrief` 持久化模型。现有
`next_instruction + unresolved_criteria + evidence_refs` 足以承载聚焦提示。

Facilitator 可以建议两个 Issue 重复，但不能自动合并、关闭 Issue 或改变完成门槛。

### 6.4 Supervisor

Supervisor 只执行机械规则：

- Review 是否覆盖四个核心审查面；
- Finding 字段是否完整；
- EvidenceRef 是否存在并绑定当前 HEAD；
- 状态转换是否由合法角色触发；
- 是否仍存在未解决的阻断项；
- 所有参与 Agent 是否都为 `complete`；
- 最终验证、Blind Ballot 和 TreeAck 是否通过。

Supervisor 不判断 Finding 的业务主张正确与否。

## 7. 推荐运行流程

初始阶段不改变 LangGraph 主链：

```text
sync_peer_memory
  -> grant_write_lease
  -> writer_turn
  -> enforce_policy
  -> checkpoint_validation
  -> commit_checkpoint
  -> publish_handoff
  -> review_current_head
       -> four core review dimensions
       -> strongest counterexample
  -> update_goal_contract
  -> facilitator_assess
  -> progress_gate
  -> final_validation
  -> blind_ballots
  -> audit_and_tree_acks
  -> integrate_or_preserve
```

结构化 Review 和 Issue Ledger 可以封装在现有 `review_current_head` 内。只有在证明需要
独立恢复点后，才考虑增加新的 LangGraph 节点。

## 8. 最小 Issue Ledger

Issue Ledger 不是第一阶段前置条件。只有真实运行证明重复问题率较高时才引入。

状态只保留：

```text
OPEN
RESOLVED
DISMISSED
```

Writer 声称已修复时，Issue 仍保持 `OPEN`，仅记录：

- `addressed_by_commit`；
- `addressed_by_agent`。

其他 Agent 读取代码并验证后，才能把它改为 `RESOLVED`。被证据否定或确定不适用时，
由 Reviewer 标记为 `DISMISSED`。

不再保留 `ADDRESSED -> VERIFIED -> CLOSED`，避免多个状态表达同一事实。

Issue ID 由首次提出者创建并保持稳定。重复问题使用 `duplicate_of` 显式关联。
Facilitator 可以建议重复关系，但不能依赖 LLM 语义哈希作为确定性完成门禁。

## 9. 协议版本与旧 Run

每个 Run 固定 `protocol_version`：

- 旧 Run 使用现有协议继续恢复和完成；
- 新协议只应用于创建时显式选择对应版本的新 Run；
- 不为旧 Run 补造空 Review；
- 不因为升级而把接近完成的旧 Run重新变为未完成；
- Protocol Normalizer 不生成虚假的审查结论或证据。

## 10. 分阶段实施与决策门槛

### Phase 1：Prompt 实验

仅调整 Reviewer Prompt，并使用现有 `summary`、`issues` 和
`completion_assessment` 承载结果。

实验内容：

- 一次性覆盖四个核心审查面；
- 给出最强完成性反例；
- Challenge 必须说明证据、影响和最小建议；
- 禁止机械接受和无证据反对；
- 禁止把问题保留到下一轮再逐个提出。

只有实验达到 [验证计划](ADVERSARIAL_REVIEW_VALIDATION.md) 中预先设定的 Go 标准，
才进入 Phase 2。

### Phase 2：最小结构化 Review

增加：

- `ReviewResult`；
- `ReviewFinding`；
- `EvidenceRef`；
- Review 协议校验；
- `protocol_version`。

不增加：

- `PeerClaimAssessment`；
- 独立 Challenge Brief；
- Issue Ledger；
- 新 LangGraph 节点；
- 扩展审计状态机。

### Phase 3：按数据决定是否增加 Issue Ledger

只有同时满足以下条件才实施：

- Phase 2 已稳定运行；
- 相同问题跨 HEAD 或跨 Agent 重复出现；
- 重复问题已显著增加轮数或 Token；
- 仅靠 Facilitator Prompt 无法可靠抑制。

实施三状态 Ledger，并让 Peer Memory、Facilitator 和 Audit 从 Ledger 派生视图。

### Phase 4：可选的最终反证 Ballot

只有最终 Blind Ballot 经常推翻过程阶段的 `complete` 判断时，才扩展 Ballot：

- `strongest_counterexample`；
- `counterexample_resolution`。

现有 Ballot 已经逐条判断验收条件，因此不重复加入完整审查矩阵。

## 11. 模块边界

进入 Phase 2 后，不把新逻辑继续堆入已经较大的 `runtime.py`：

```text
review_models.py
  ReviewResult、ReviewFinding、EvidenceRef

review_policy.py
  覆盖检查、证据合法性、字段一致性

review_runtime.py
  Reviewer 调用和结果持久化

issue_ledger.py
  Phase 3 才创建；Issue 状态和投影
```

`runtime.py` 只负责调用服务并把小型控制状态写回 LangGraph。

## 12. 明确删除的设计

精简方案不采用：

- 固定六维 Writer 自审；
- 独立 `PeerClaimAssessment` 状态模型；
- 五状态 Issue Ledger；
- 基于 `normalized_claim` 的确定性语义指纹；
- 独立持久化的 Challenge Brief；
- 每个阶段重复输出完整反证矩阵；
- 旧 Run 恢复时补造空 Review；
- 在生产运行中统计无法获得真值的 `false-consensus rate`；
- 把安全、性能等不适用项强制填满。

## 13. 完成标准

只有同时满足以下条件，本改造才视为完成：

- Reviewer 单次调用覆盖四个核心审查面；
- Reviewer 一次性提交当前可识别的全部阻断问题；
- 接受和反对结论均可追溯到合法 EvidenceRef；
- 无证据意见不能阻止完成；
- 最强完成性反例被明确提出并处理；
- Writer、Reviewer、Facilitator 和 Supervisor 权限边界保持不变；
- 新协议不会改变旧 Run 的恢复和完成语义；
- 新逻辑没有继续扩大 `runtime.py` 单体；
- 真实 CLI 基准证明缺陷发现能力提高；
- Token、耗时、无效阻断和重复轮次处于预设预算内；
- 最终仍由全部参与 Agent 的完成判断、最终验证和 TreeAck 共同决定结束。
