"""跨模块共享的状态和协议模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentName(str, Enum):
    """标识可参与讨论的内置 CLI。"""

    CODEX = "codex"
    CLAUDE = "claude"
    QODER = "qoder"
    OPENCODE = "opencode"
    OPENCLAW = "openclaw"
    HERMES = "hermes"
    CODEBUDDY = "codebuddy"


class ReviewProtocolVersion(str, Enum):
    """标识一次 Run 固定使用的审查协议版本。"""

    LEGACY_V0 = "legacy-v0"
    REVIEW_V1 = "review-v1"


class Decision(str, Enum):
    """表示 Agent 对当前版本的处理意见。"""

    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"
    BLOCKED = "blocked"


class CompletionAssessment(str, Enum):
    """表示 Agent 是否判断 GoalContract 已完成。"""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class FacilitatorPurpose(str, Enum):
    """表示 Facilitator 的调用目的。"""

    TURN_ROUTING = "turn_routing"
    AUDIT_DISPUTE = "audit_dispute"


class FacilitatorMode(str, Enum):
    """表示 DeepSeek 的确定性推理模式。"""

    NON_THINKING = "non_thinking"
    THINKING = "thinking"


class FacilitatorAction(str, Enum):
    """表示 Facilitator 可以提出的有限建议动作。"""

    CONTINUE_DEBATE = "continue_debate"
    PREPARE_FINAL_REVIEW = "prepare_final_review"
    DECLARE_STALL = "declare_stall"
    REGENERATE_AUDIT = "regenerate_audit"


class FacilitatorStatus(str, Enum):
    """表示 Facilitator 记录的持久化阶段。"""

    PREPARED = "prepared"
    DECISION = "decision"
    FALLBACK = "facilitator_fallback"


class SessionHealth(str, Enum):
    """表示原生 CLI Session 的可用状态。"""

    NEW = "new"
    ACTIVE = "active"
    UNCERTAIN = "uncertain"
    LOST = "lost"


class LeaseStatus(str, Enum):
    """表示单写者租约的事务阶段。"""

    PREPARED = "prepared"
    ACTIVE = "active"
    REVOKED = "revoked"
    COMMITTED = "committed"
    FAILED = "failed"


class RunStatus(str, Enum):
    """表示运行的终止或执行状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    PREFLIGHT_BLOCKED = "preflight_blocked"
    SCOPE_BLOCKED = "scope_blocked"
    BLOCKED = "blocked"
    STALLED = "stalled"
    MAX_TURNS = "max_turns"
    FAILED = "failed"
    POLICY_VIOLATION = "policy_violation"
    WORKSPACE_RACE = "workspace_race"
    RECOVERY_BLOCKED = "recovery_blocked"
    INTEGRATION_BLOCKED = "integration_blocked"
    ABORTED = "aborted"


class StrictModel(BaseModel):
    """为协议模型统一启用严格字段和冻结行为。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentSessionState(StrictModel):
    """保存某个 Agent 的 Session 代次和同步游标。"""

    session_id: str | None = None
    generation: int = Field(default=1, ge=1)
    health: SessionHealth = SessionHealth.NEW
    last_ack_commit: str | None = None
    last_update_id: str | None = None


class LeaseState(StrictModel):
    """保存一次写入租约的身份、基线和状态。"""

    run_id: str
    turn_id: str
    agent: AgentName
    base_commit: str
    goal_contract_version: int = Field(ge=1)
    issued_at: datetime
    status: LeaseStatus


class Issue(StrictModel):
    """表示 Agent 提出的可追踪问题。"""

    id: str
    severity: str
    claim: str
    evidence: list[str] = Field(default_factory=list)
    proposed_change: str | None = None


class FacilitatorTurnSummary(StrictModel):
    """保存供 Facilitator 使用的脱敏单轮语义摘要。"""

    agent: AgentName
    decision: Decision
    summary: str
    issues: list[str] = Field(default_factory=list)
    completion_assessment: CompletionAssessment


class FacilitatorContext(StrictModel):
    """保存一次 Facilitator 调用允许读取的结构化事实。"""

    run_id: str
    purpose: FacilitatorPurpose
    turn_id_or_revision: str
    head_sha: str
    goal_contract_version: int = Field(ge=1)
    objective: str
    deliverables: list[str]
    acceptance_criteria: list[str]
    effective_scope: list[str]
    writer: FacilitatorTurnSummary | None = None
    reviewer: FacilitatorTurnSummary | None = None
    changed_paths: list[str] = Field(default_factory=list)
    behavior_summary: list[str] = Field(default_factory=list)
    validation_passed: bool
    validation_report_ref: str | None = None
    unresolved_review_criteria: list[str] = Field(default_factory=list)
    review_evidence_refs: list[str] = Field(default_factory=list)
    assessments: dict[AgentName, CompletionAssessment]
    stall_count: int = Field(ge=0)
    remaining_turns: int = Field(ge=0)
    audit_disputes: list[str] = Field(default_factory=list)
    previous_decision_ref: str | None = None


class FacilitatorDecision(StrictModel):
    """保存 DeepSeek 返回且尚待 Supervisor 编译的有限建议。"""

    decision_id: str
    purpose: FacilitatorPurpose
    head_sha: str
    goal_contract_version: int = Field(ge=1)
    mode: FacilitatorMode
    action: FacilitatorAction
    recommended_next_writer: AgentName | None = None
    next_instruction: str
    unresolved_criteria: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class FacilitatorRecord(StrictModel):
    """保存可恢复且不含隐藏推理的 Facilitator 结果。"""

    decision_id: str
    purpose: FacilitatorPurpose
    head_sha: str
    goal_contract_version: int = Field(ge=1)
    mode: FacilitatorMode
    status: FacilitatorStatus
    decision: FacilitatorDecision | None = None
    error_category: str | None = None
    deterministic_route: str | None = None


class TurnResult(StrictModel):
    """表示 Writer 或 Reviewer 的结构化回合结果。"""

    agent: AgentName
    turn_id: str
    base_commit: str
    observed_head: str
    goal_contract_version: int = Field(ge=1)
    decision: Decision
    summary: str
    accepted_points: list[str]
    issues: list[Issue]
    requested_scope_additions: list[str]
    requested_validation_additions: list[list[str]]
    completion_assessment: CompletionAssessment


class ChangeSummary(StrictModel):
    """保存 Supervisor 从 Git 与 Agent 输出合成的交接摘要。"""

    update_id: str
    source_agent: AgentName
    turn_id: str
    parent_commit: str
    commit_sha: str
    goal_contract_version: int = Field(ge=1)
    changed_paths: list[str] = Field(default_factory=list)
    behavior_summary: list[str] = Field(default_factory=list)
    opened_issues: list[str] = Field(default_factory=list)
    resolved_issues: list[str] = Field(default_factory=list)
    validation_status: str
    validation_report_ref: str
    requested_attention: list[str] = Field(default_factory=list)


class PeerMemoryUpdate(StrictModel):
    """表示注入目标 Agent Session 的对方增量记忆。"""

    update_id: str
    source_agent: AgentName
    target_agent: AgentName
    turn_id: str
    commit_sha: str
    goal_contract_version: int = Field(ge=1)
    changed_paths: list[str] = Field(default_factory=list)
    behavior_summary: list[str] = Field(default_factory=list)
    opened_issues: list[str] = Field(default_factory=list)
    resolved_issues: list[str] = Field(default_factory=list)
    validation_status: str
    requested_attention: list[str] = Field(default_factory=list)

    @classmethod
    def from_change_summary(
        cls, summary: ChangeSummary, *, target_agent: AgentName
    ) -> PeerMemoryUpdate:
        """从交接摘要生成同伴记忆。

        参数为摘要和目标 Agent，返回更新对象，不产生外部副作用。
        """
        return cls(
            update_id=f"{summary.update_id}-{target_agent.value}",
            source_agent=summary.source_agent,
            target_agent=target_agent,
            turn_id=summary.turn_id,
            commit_sha=summary.commit_sha,
            goal_contract_version=summary.goal_contract_version,
            changed_paths=summary.changed_paths,
            behavior_summary=summary.behavior_summary,
            opened_issues=summary.opened_issues,
            resolved_issues=summary.resolved_issues,
            validation_status=summary.validation_status,
            requested_attention=summary.requested_attention,
        )


class ChangeAck(StrictModel):
    """表示接手 Agent 对当前 HEAD 的理解和判断。"""

    agent: AgentName
    commit_sha: str
    reviewed_paths: list[str]
    understood_changes: list[str]
    risks_or_disputes: list[str]
    decision: Decision


class CriterionAssessment(StrictModel):
    """表示终审对一个明确验收条件的布尔判断。"""

    criterion: str
    satisfied: bool


class Ballot(StrictModel):
    """表示 Agent 对固定代码候选的独立完成投票。"""

    agent: AgentName
    commit_sha: str
    goal_contract_version: int = Field(ge=1)
    completion: CompletionAssessment
    criteria: list[CriterionAssessment]
    rationale: str
    strongest_counterexample: str | None = Field(default=None, min_length=1)
    counterexample_resolution: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_counterexample_pair(self) -> Ballot:
        """校验可选反例字段成对出现。

        无参数；返回自身；仅提供其中一个反例字段、或字段仅含空白时抛 ``ValueError``。
        是否必须提供这对字段由冻结的 RunConfig 在终局收集时决定，避免旧 Ballot 协议
        被破坏。
        """
        counterexample_fields = (
            self.strongest_counterexample,
            self.counterexample_resolution,
        )
        if (counterexample_fields[0] is None) != (counterexample_fields[1] is None):
            raise ValueError("反例与反例处理说明必须同时提供")
        if any(
            value is not None and not value.strip() for value in counterexample_fields
        ):
            raise ValueError("反例字段不得仅包含空白")
        return self

    def require_counterexample(self) -> None:
        """确认显式反例投票已完整填写。

        无参数和返回值；启用反例式 Blind Ballot 时由 Supervisor 调用，任一必填字段
        缺失时抛 ``ValueError``，不为 legacy-v0 或默认关闭的 Run 增加要求。
        """
        if (
            self.strongest_counterexample is None
            or self.counterexample_resolution is None
        ):
            raise ValueError("启用反例式 Blind Ballot 时必须提供最强反例及其处理说明")


class TreeAck(StrictModel):
    """表示 Agent 对最终代码与审计 tree 的确认。"""

    agent: AgentName
    final_tree_sha: str
    accepted: bool
    rationale: str


class AgentResult(StrictModel):
    """统一描述一次 CLI 子进程调用结果。"""

    ok: bool
    agent: AgentName
    session_id: str | None = None
    session_generation: int = Field(ge=1)
    turn_id: str
    observed_head: str | None = None
    structured_output: dict[str, Any] | None = None
    return_code: int | None = None
    elapsed_seconds: float = Field(ge=0)
    raw_output_ref: str
    error: str | None = None
    failure_category: str | None = None
    protocol_method: Literal["strict", "deterministic", "deepseek"] | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> AgentResult:
        """校验成功与失败载荷；无参数，返回自身，结果自相矛盾时抛错。"""
        if self.ok and self.structured_output is None:
            raise ValueError("成功结果必须包含 structured_output")
        if not self.ok and not self.error:
            raise ValueError("失败结果必须包含 error")
        return self


class DebateState(StrictModel):
    """保存 LangGraph 可序列化的小型控制状态。"""

    run_id: str
    repo_root: str
    target_branch: str
    base_commit: str
    worktree_path: str
    debate_branch: str
    goal_contract_ref: str
    goal_contract_version: int = Field(ge=1)
    current_head: str
    current_turn: int = Field(ge=0)
    max_turns: int = Field(gt=0)
    participants: tuple[AgentName, ...] = (
        AgentName.CODEX,
        AgentName.CLAUDE,
    )
    next_writer: AgentName
    lease: LeaseState | None = None
    agent_sessions: dict[AgentName, AgentSessionState]
    pending_update_refs: dict[AgentName, list[str]]
    latest_change_summary_ref: str | None = None
    issue_ledger_ref: str
    validation_plan_ref: str
    validation_report_ref: str | None = None
    ballots: dict[AgentName, Ballot]
    final_tree_sha: str | None = None
    status: RunStatus
    stop_reason: str | None = None
    failure_count: int = Field(default=0, ge=0)
    stall_count: int = Field(default=0, ge=0)
