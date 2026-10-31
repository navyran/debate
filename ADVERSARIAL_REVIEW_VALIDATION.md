# 对抗审查验证计划

> 目标：在增加永久协议和状态之前，证明对抗审查能提高有效缺陷发现能力
> 原则：每个阶段单独做 Go/No-Go 决策，不默认实施后续阶段

## 1. 基准任务

建立一组隔离、可重复运行的任务。每个任务包含已知真值，并至少覆盖：

- 一个遗漏的验收条件；
- 一个主流程或边界错误；
- 一个调用方、配置或恢复兼容问题；
- 一个测试覆盖或验证绑定缺口；
- 一个看似合理但没有证据的完成声明；
- 一个不应被升级为阻断项的风格问题；
- 一个没有真实缺陷的干净任务。

每个任务固定：

- 初始 Commit；
- GoalContract；
- 参与 Agent 和模型配置；
- 已知缺陷标签；
- 权威验证命令；
- 最大轮次和超时；
- 是否使用 Facilitator。

## 2. 对照组

同一批任务至少运行：

1. 当前 Reviewer Prompt；
2. 精简后的四面审查 Prompt；
3. 必要时再运行结构化 Review 版本。

控制变量：

- 相同 Agent 组合；
- 相同模型和推理配置；
- 相同初始 Commit；
- 相同 GoalContract；
- 相同验证命令；
- 相同最大轮数。

真实 CLI 存在非确定性，每个组合应重复多次，不能用单次成功证明效果。

## 3. 核心指标

### 3.1 质量

- 已知阻断缺陷发现率；
- 遗漏验收条件发现率；
- 无证据阻断 Finding 比例；
- 风格问题被错误升级为阻断项的比例；
- 修复后仍重复提出同一问题的比例；
- 最终 Ballot 推翻过程 `complete` 的比例。

### 3.2 成本

- Reviewer 输入和输出 Token；
- Reviewer 调用耗时；
- 完成任务的写入轮数；
- 总 CLI 调用次数；
- 达到 `max_turns` 或 `stalled` 的比例；
- 用户中途参与次数。

### 3.3 协议可靠性

- 缺少核心 Coverage 的输出比例；
- 非法 EvidenceRef 比例；
- Structured Output 修复或降级比例；
- Session 重建后审查上下文丢失比例；
- 恢复后 HEAD、Review 和 Ledger 绑定错误比例。

不在生产运行中统计 `false-consensus rate`。没有标注真值时，该指标不可直接观测；
它只用于上述带真值的基准任务。

## 4. Phase 1：Prompt 实验

验证内容：

- Reviewer 是否一次性覆盖四个核心审查面；
- 是否能识别已知实质问题；
- 是否避免机械接受 Writer 声明；
- 是否避免为了严格而制造问题；
- 是否把所有当前可识别阻断项一次性给出；
- 是否仍会在后续回合重复同一问题。

建议在实验开始前冻结 Go 标准。初始建议：

- 已知阻断缺陷发现率不低于基线，并有明确提升；
- 无证据阻断项和风格误报不高于基线；
- 重复问题率下降；
- 中位写入轮数不增加；
- Reviewer 中位耗时和输出长度增幅处于可接受预算；
- 干净任务不会因为固定矩阵被稳定判为 `incomplete`。

如果质量没有提高，停止在 Phase 1，继续调整 Prompt，不进入结构化协议。

## 5. Phase 2：结构化协议

### 5.1 模型测试

- 四个核心 Coverage 缺失、重复或使用未知值时拒绝；
- `unknown` 与 `complete` 同时出现时拒绝；
- 存在阻断 Finding 但声明 `complete` 时拒绝；
- Finding 缺少影响、建议或合法证据时不能成为阻断项；
- Review HEAD 或 GoalContract 版本不匹配时拒绝；
- Reviewer 修改工作空间时拒绝结果；
- Protocol Normalizer 不得补造 Coverage、Finding 或 EvidenceRef。

### 5.2 EvidenceRef 测试

- 当前 Commit 中存在的 Git 路径可以通过；
- 不存在路径、错误 HEAD、绝对路径和 `..` 被拒绝；
- ValidationReport 引用必须存在并绑定同一 HEAD；
- Agent 自行声明的测试结果不能冒充 Supervisor 产物；
- 旧验证报告不能用于证明新 HEAD。

### 5.3 集成测试

- Writer 仍使用现有 TurnResult；
- Reviewer 使用独立 ReviewResult；
- Review 失败不会留下部分状态；
- Facilitator 不可用时使用确定性路由；
- 新协议不改变写租约、Git 检查点和最终集成行为；
- 协议版本在 Run 内保持不变。

Phase 2 的 Go 标准：

- 结构化输出成功率达到现有 TurnResult 的可接受水平；
- EvidenceRef 校验未显著增加误失败；
- 没有扩大 Runtime 的恢复故障面；
- 质量指标至少保持 Phase 1 水平。

## 6. Phase 3：Issue Ledger

只有 Phase 2 数据证明重复问题造成明显成本时才执行。

### 6.1 状态测试

- 新 Issue 只能由 Reviewer Finding 创建；
- Writer 只能填写处理 Commit，不能自行解决 Issue；
- 其他参与 Agent 验证后才能进入 `RESOLVED`；
- 有证据否定时可以进入 `DISMISSED`；
- 非法角色、错误 HEAD 和缺失证据的转换被拒绝；
- `duplicate_of` 不自动改变状态；
- Ledger 更新是单写和幂等的。

### 6.2 恢复测试

- Worker 中断后状态、Agent、HEAD 和处理 Commit 保持一致；
- Session 丢失后 Peer Memory 能从 Ledger 投影恢复；
- 损坏 Ledger 明确停止，不静默回退为空；
- 旧协议 Run 不尝试加载 Ledger；
- Audit、UI 和 Facilitator读取同一 Ledger 投影。

Phase 3 的 Go 标准：

- 重复问题率和平均关闭轮次下降；
- 没有出现 Ledger 与 Peer Memory 状态漂移；
- 恢复成功率不低于 Phase 2；
- Ledger 带来的 Token 和 I/O 成本小于减少重复讨论节省的成本。

## 7. Phase 4：Ballot 可选增强

仅在最终 Ballot 经常推翻过程阶段 `complete` 时评估。

测试：

- 所有 Agent 仍绑定同一个 HEAD 和 GoalContract；
- Ballot 互相不可见；
- 每条验收条件仍独立判断；
- 最强反例存在且不能由 Normalizer 补造；
- 存在未解决阻断 Issue 时必须投 `incomplete`；
- Ballot 增强不重复执行完整四面 Reviewer 审查。

如果增强字段不能降低最终阶段打回率，不保留该扩展。

## 8. 故障注入

每个已实施阶段覆盖：

- CLI 超时或被终止；
- Structured Output 截断；
- Agent 返回空审查；
- Agent 对所有项目机械返回 `pass`；
- Agent 引用不存在的代码或验证报告；
- Reviewer 尝试写入工作空间；
- Facilitator 尝试关闭 Issue 或宣布完成；
- Checkpoint 后进程中断；
- Session ID 丢失；
- 恢复时工作空间 HEAD 已变化；
- 多 Agent 组合中的一个 CLI 不可用。

所有错误必须在 UI 和审计记录中可定位，不能只显示通用“CLI 退出码 1”。

## 9. 真实 CLI 验收

至少覆盖：

- Codex + Claude；
- Codex + Qoder；
- Claude + OpenCode；
- 三 Agent 组合；
- 四 Agent 组合。

验收重点：

- 每个 Checkpoint 始终只有一个 Writer；
- Reviewer 严格只读；
- Peer Agent 能读取当前 Git 变化全文；
- Reviewer 一次性给出全部当前阻断项；
- 下一 Writer 能收到聚焦且不重复的上下文；
- 不请求用户回答可由工作空间调查的问题；
- 全员 `complete`、最终验证和 TreeAck 门槛保持不变。

## 10. 结果报告

每个阶段输出一份短报告：

- 基准配置；
- 对照组和实验组结果；
- 发现的真实问题和误报；
- Token、耗时和轮次变化；
- 恢复与协议错误；
- 是否达到预先冻结的 Go 标准；
- 进入下一阶段、继续调整或停止的明确决策。

阶段报告不保存隐藏推理或完整 CLI 原始响应，只引用结构化产物和可复核证据。
