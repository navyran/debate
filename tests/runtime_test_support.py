"""test_runtime.py 的共享测试桩与断言辅助函数。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from git_helpers import git
from pydantic import BaseModel

from debate_agent.adapters import AdapterOutputError, SessionBoundaryError
from debate_agent.cli_health import FailureCategory
from debate_agent.facilitator import (
    build_facilitator_decision_id,
    select_facilitator_mode,
)
from debate_agent.models import (
    AgentName,
    AgentResult,
    Ballot,
    ChangeAck,
    CompletionAssessment,
    Decision,
    FacilitatorAction,
    FacilitatorContext,
    FacilitatorDecision,
    FacilitatorPurpose,
    FacilitatorRecord,
    FacilitatorStatus,
    ReviewProtocolVersion,
    TreeAck,
    TurnResult,
)
from debate_agent.review_models import (
    EvidenceKind,
    EvidenceRef,
    ReviewCoverage,
    ReviewCoverageStatus,
    ReviewDimension,
    ReviewResult,
)
from debate_agent.runtime import AgentBackend

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)

class FakeFacilitatorService:
    """在网络边界之外返回与当前上下文绑定的合法建议。"""

    def __init__(
        self, recommended_next_writer: AgentName | None = AgentName.CLAUDE
    ) -> None:
        """创建 fake Facilitator；参数为可选 Writer 建议，返回实例，不访问网络。"""
        self.contexts: list[FacilitatorContext] = []
        self.recommended_next_writer = recommended_next_writer

    def evaluate(self, context: FacilitatorContext) -> FacilitatorRecord:
        """生成合法建议；参数为上下文，返回决定记录，不访问网络。"""
        self.contexts.append(context)
        audit_dispute = context.purpose is FacilitatorPurpose.AUDIT_DISPUTE
        decision = FacilitatorDecision(
            decision_id=build_facilitator_decision_id(context),
            purpose=context.purpose,
            head_sha=context.head_sha,
            goal_contract_version=context.goal_contract_version,
            mode=select_facilitator_mode(context),
            action=(
                FacilitatorAction.REGENERATE_AUDIT
                if audit_dispute
                else FacilitatorAction.CONTINUE_DEBATE
            ),
            recommended_next_writer=(
                None if audit_dispute else self.recommended_next_writer
            ),
            next_instruction=(
                "在审计摘要中准确记录双方 TreeAck 争议"
                if audit_dispute
                else "先读取当前 HEAD，再修复失败验收条件"
            ),
            unresolved_criteria=["最终验证通过"],
            risk_flags=[],
            evidence_refs=[context.validation_report_ref]
            if context.validation_report_ref
            else [],
        )
        return FacilitatorRecord(
            decision_id=decision.decision_id,
            purpose=decision.purpose,
            head_sha=decision.head_sha,
            goal_contract_version=decision.goal_contract_version,
            mode=decision.mode,
            status=FacilitatorStatus.DECISION,
            decision=decision,
        )

class FakeAgentBackend(AgentBackend):
    """用确定性结构化响应模拟两个有独立 Session 的 CLI。"""

    def __init__(self) -> None:
        self.writer_count = 0
        self.calls: list[tuple[AgentName, str, bool, str | None]] = []
        self.prompts: list[tuple[str, bool, str]] = []

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        self.calls.append((agent, response_model.__name__, read_only, session_id))
        self.prompts.append((response_model.__name__, read_only, prompt))
        head = git(worktree, "rev-parse", "HEAD")
        if response_model is ChangeAck:
            payload = ChangeAck(
                agent=agent,
                commit_sha=head,
                reviewed_paths=["result.txt"]
                if (worktree / "result.txt").exists()
                else [],
                understood_changes=["已读取当前 HEAD 和 GoalContract"],
                risks_or_disputes=[],
                decision=Decision.ACCEPT,
            )
        elif response_model is TurnResult:
            if read_only:
                complete = self.writer_count >= 2
                payload = TurnResult(
                    agent=agent,
                    turn_id=turn_id,
                    base_commit=head,
                    observed_head=head,
                    goal_contract_version=1,
                    decision=Decision.ACCEPT if complete else Decision.REVISE,
                    summary="审查当前实现",
                    accepted_points=["已读取对方 checkpoint"],
                    issues=[],
                    requested_scope_additions=[],
                    requested_validation_additions=[],
                    completion_assessment=(
                        CompletionAssessment.COMPLETE
                        if complete
                        else CompletionAssessment.INCOMPLETE
                    ),
                )
            else:
                self.writer_count += 1
                target = worktree / "result.txt"
                previous = target.read_text(encoding="utf-8") if target.exists() else ""
                target.write_text(previous + f"{agent.value}\n", encoding="utf-8")
                payload = TurnResult(
                    agent=agent,
                    turn_id=turn_id,
                    base_commit=head,
                    observed_head=head,
                    goal_contract_version=1,
                    decision=(
                        Decision.ACCEPT if self.writer_count >= 2 else Decision.REVISE
                    ),
                    summary=f"{agent.value} 更新结果",
                    accepted_points=[],
                    issues=[],
                    requested_scope_additions=[],
                    requested_validation_additions=[],
                    completion_assessment=(
                        CompletionAssessment.COMPLETE
                        if self.writer_count >= 2
                        else CompletionAssessment.INCOMPLETE
                    ),
                )
        elif response_model is Ballot:
            counterexample_enabled = "strongest_counterexample" in prompt
            payload = Ballot(
                agent=agent,
                commit_sha=head,
                goal_contract_version=1,
                completion=CompletionAssessment.COMPLETE,
                criteria=[{"criterion": "validation", "satisfied": True}],
                rationale="目标和验证均已完成",
                strongest_counterexample=(
                    "不存在能推翻完成结论的反例" if counterexample_enabled else None
                ),
                counterexample_resolution=(
                    "已读取当前 HEAD 的权威验证报告，未发现未关闭阻断项。"
                    if counterexample_enabled
                    else None
                ),
            )
        elif response_model is TreeAck:
            tree = prompt.split("FINAL_TREE_SHA=", 1)[1].splitlines()[0]
            payload = TreeAck(
                agent=agent,
                final_tree_sha=tree,
                accepted=True,
                rationale="代码与审计准确",
            )
        else:
            raise AssertionError(response_model)
        validated = response_model.model_validate(payload)
        return AgentResult(
            ok=True,
            agent=agent,
            session_id=f"{agent.value}-session",
            session_generation=session_generation,
            turn_id=turn_id,
            observed_head=getattr(validated, "observed_head", None),
            structured_output=validated.model_dump(mode="json"),
            return_code=0,
            elapsed_seconds=0.01,
            raw_output_ref=f"raw/{invocation_id}.json",
        )

class StructuredReviewBackend(FakeAgentBackend):
    """为 review-v1 返回当前 HEAD 绑定的合法结构化审查。"""

    def __init__(
        self,
        *,
        unknown_coverage: bool = False,
        mutate_review: bool = False,
        ledger_lifecycle: bool = False,
        ledger_runtime_evidence: bool = False,
        initial_review_count: int = 0,
    ) -> None:
        """创建结构化 fake。

        参数分别控制 unknown 覆盖、只读写入故障、Ledger 生命周期、其是否只用运行
        产物证据和已持久化审查数；无返回值。审查调用时从 Prompt 读取 Supervisor
        验证报告引用，无法解析时抛 AssertionError。
        """
        super().__init__()
        self.unknown_coverage = unknown_coverage
        self.mutate_review = mutate_review
        self.ledger_lifecycle = ledger_lifecycle
        self.ledger_runtime_evidence = ledger_runtime_evidence
        self.review_count = initial_review_count
        self.review_turns: list[str] = []

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """执行固定结构化审查；参数同 AgentBackend，返回合法 AgentResult。"""
        if response_model is not ReviewResult:
            return super().invoke(
                agent=agent,
                response_model=response_model,
                schema_name=schema_name,
                prompt=prompt,
                worktree=worktree,
                session_id=session_id,
                session_generation=session_generation,
                invocation_id=invocation_id,
                turn_id=turn_id,
                read_only=read_only,
            )
        self.calls.append((agent, response_model.__name__, read_only, session_id))
        self.prompts.append((response_model.__name__, read_only, prompt))
        self.review_turns.append(turn_id)
        assert read_only is True
        if self.mutate_review:
            (worktree / "reviewer-write.txt").write_text("forbidden", encoding="utf-8")
        head = git(worktree, "rev-parse", "HEAD")
        report_ref = prompt.split("CURRENT_VALIDATION_REPORT=", 1)[1].splitlines()[0]
        git_evidence = EvidenceRef(
            kind=EvidenceKind.GIT_PATH,
            head_sha=head,
            artifact_ref=None,
            path="result.txt",
            locator=None,
        )
        validation_evidence = EvidenceRef(
            kind=EvidenceKind.VALIDATION_REPORT,
            head_sha=head,
            artifact_ref=report_ref,
            path=None,
            locator=None,
        )
        coverage = [
            ReviewCoverage(
                dimension=dimension,
                status=(
                    ReviewCoverageStatus.UNKNOWN
                    if (
                        self.unknown_coverage
                        and dimension is ReviewDimension.INTEGRATION
                    )
                    else ReviewCoverageStatus.PASS
                ),
                summary=f"已检查 {dimension.value}",
                evidence_refs=(
                    [validation_evidence]
                    if dimension is ReviewDimension.VALIDATION
                    else [git_evidence]
                ),
            )
            for dimension in ReviewDimension
        ]
        self.review_count += 1
        finding = None
        dispositions: list[dict[str, object]] = []
        if self.ledger_lifecycle and self.review_count == 1:
            finding = {
                "finding_id": "FIND-LEDGER-1",
                "dimension": ReviewDimension.CORRECTNESS.value,
                "claim": "result.txt 尚未包含第二位参与者的结果",
                "impact": "交付要求尚未完成",
                "blocking": True,
                "evidence_refs": [
                    (
                        validation_evidence
                        if self.ledger_runtime_evidence
                        else git_evidence
                    ).model_dump(mode="json")
                ],
                "proposed_change": "由另一位 Writer 更新 result.txt",
            }
        elif self.ledger_lifecycle and self.review_count > 1:
            issue_context = json.loads(prompt.split("OPEN_LEDGER_ISSUES=", 1)[1])
            if issue_context:
                assert len(issue_context) == 1
                dispositions = [
                    {
                        "issue_id": issue_context[0]["issue_id"],
                        "status": "resolved",
                        "rationale": "已读取 Writer 的当前 HEAD 修改与权威验证报告。",
                        "evidence_refs": [
                            validation_evidence.model_dump(mode="json"),
                        ],
                    }
                ]
        incomplete = self.unknown_coverage or finding is not None
        payload = ReviewResult(
            agent=agent,
            turn_id=turn_id,
            head_sha=head,
            goal_contract_version=1,
            protocol_version=ReviewProtocolVersion.REVIEW_V1,
            coverage=coverage,
            findings=[] if finding is None else [finding],
            issue_dispositions=dispositions,
            strongest_counterexample="result.txt 缺少参与 Agent 的输出",
            counterexample_resolution=(
                "当前 HEAD 的 result.txt 已由 Git 路径和验证报告覆盖"
            ),
            completion_assessment=(
                CompletionAssessment.INCOMPLETE
                if incomplete
                else CompletionAssessment.COMPLETE
            ),
            decision=(Decision.REVISE if incomplete else Decision.ACCEPT),
            summary="结构化四面审查完成",
        )
        return AgentResult(
            ok=True,
            agent=agent,
            session_id=f"{agent.value}-session",
            session_generation=session_generation,
            turn_id=turn_id,
            structured_output=payload.model_dump(mode="json"),
            return_code=0,
            elapsed_seconds=0.01,
            raw_output_ref=f"raw/{invocation_id}.json",
        )

class OneAuditDisputeBackend(FakeAgentBackend):
    """首次 TreeAck 否决审计，后续调用接受新候选。"""

    def __init__(self) -> None:
        super().__init__()
        self.tree_ack_calls = 0

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """模拟一次审计异议；参数同 AgentBackend，返回完整结构化结果。"""
        if response_model is not TreeAck:
            return super().invoke(
                agent=agent,
                response_model=response_model,
                schema_name=schema_name,
                prompt=prompt,
                worktree=worktree,
                session_id=session_id,
                session_generation=session_generation,
                invocation_id=invocation_id,
                turn_id=turn_id,
                read_only=read_only,
            )
        self.calls.append((agent, response_model.__name__, read_only, session_id))
        self.prompts.append((response_model.__name__, read_only, prompt))
        self.tree_ack_calls += 1
        tree = prompt.split("FINAL_TREE_SHA=", 1)[1].splitlines()[0]
        ack = TreeAck(
            agent=agent,
            final_tree_sha=tree,
            accepted=self.tree_ack_calls != 1,
            rationale=(
                "审计摘要遗漏本轮贡献"
                if self.tree_ack_calls == 1
                else "代码与修订后的审计准确"
            ),
        )
        return AgentResult(
            ok=True,
            agent=agent,
            session_id=f"{agent.value}-session",
            session_generation=session_generation,
            turn_id=turn_id,
            structured_output=ack.model_dump(mode="json"),
            return_code=0,
            elapsed_seconds=0.01,
            raw_output_ref=f"raw/{invocation_id}.json",
        )

class CrashingWriterBackend(FakeAgentBackend):
    """在首次 Writer 已写文件但尚未返回协议结果时模拟进程崩溃。"""

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """执行模拟调用；Writer 写入后抛 SystemExit，其余调用沿用父实现。"""
        if response_model is TurnResult and not read_only:
            (worktree / "result.txt").write_text("codex\n", encoding="utf-8")
            raise SystemExit("simulated hard crash")
        return super().invoke(
            agent=agent,
            response_model=response_model,
            schema_name=schema_name,
            prompt=prompt,
            worktree=worktree,
            session_id=session_id,
            session_generation=session_generation,
            invocation_id=invocation_id,
            turn_id=turn_id,
            read_only=read_only,
        )

class CrashingAckBackend(FakeAgentBackend):
    """在首次只读同步尚未返回结果时模拟 Worker 硬退出。"""

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """中断首次 ChangeAck；参数同后端协议，其余调用沿用父实现。"""
        if response_model is ChangeAck:
            raise SystemExit("simulated ack crash")
        return super().invoke(
            agent=agent,
            response_model=response_model,
            schema_name=schema_name,
            prompt=prompt,
            worktree=worktree,
            session_id=session_id,
            session_generation=session_generation,
            invocation_id=invocation_id,
            turn_id=turn_id,
            read_only=read_only,
        )

class InvalidWriterOutputBackend(FakeAgentBackend):
    """模拟 Writer 已修改工作区但最终结构化输出损坏。"""

    def __init__(self) -> None:
        """创建一次性失败后端；无参数，返回实例，不抛异常。"""
        super().__init__()
        self.failed_writer = False

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """修改 Writer 文件后抛格式错误；参数同后端协议，其余调用沿用父实现。"""
        if response_model is TurnResult and not read_only and not self.failed_writer:
            self.failed_writer = True
            (worktree / "result.txt").write_text("codex\n", encoding="utf-8")
            raise AdapterOutputError("invalid writer output")
        return super().invoke(
            agent=agent,
            response_model=response_model,
            schema_name=schema_name,
            prompt=prompt,
            worktree=worktree,
            session_id=session_id,
            session_generation=session_generation,
            invocation_id=invocation_id,
            turn_id=turn_id,
            read_only=read_only,
        )


class CleanInvalidWriterOutputBackend(FakeAgentBackend):
    """模拟 Writer 未改工作树但最终结构化输出损坏。"""

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """Writer 始终抛格式错误；参数同后端协议，其余调用沿用父实现。"""
        if response_model is TurnResult and not read_only:
            raise AdapterOutputError("invalid clean writer output")
        return super().invoke(
            agent=agent,
            response_model=response_model,
            schema_name=schema_name,
            prompt=prompt,
            worktree=worktree,
            session_id=session_id,
            session_generation=session_generation,
            invocation_id=invocation_id,
            turn_id=turn_id,
            read_only=read_only,
        )


class OneFormatFailureBackend(FakeAgentBackend):
    """首次只读调用返回格式错误，随后正常响应。"""

    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """执行模拟调用；首次抛格式错误，后续委托父实现。"""
        if not self.failed_once:
            self.failed_once = True
            raise AdapterOutputError("invalid structured output")
        return super().invoke(
            agent=agent,
            response_model=response_model,
            schema_name=schema_name,
            prompt=prompt,
            worktree=worktree,
            session_id=session_id,
            session_generation=session_generation,
            invocation_id=invocation_id,
            turn_id=turn_id,
            read_only=read_only,
        )

class OneOpenCodeFormatFailureBackend(FakeAgentBackend):
    """模拟 OpenCode 首次协议失败且工作区未变化。"""

    def __init__(self) -> None:
        """创建后端；无参数；返回实例；首次 OpenCode 调用尚未失败。"""
        super().__init__()
        self.failed_once = False
        self.open_code_calls = 0

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """执行模拟调用；参数同后端协议；OpenCode 首次抛格式错误，之后正常返回。"""
        if agent is AgentName.OPENCODE:
            self.open_code_calls += 1
            if not self.failed_once:
                self.failed_once = True
                raise AdapterOutputError("invalid opencode structured output")
        return super().invoke(
            agent=agent,
            response_model=response_model,
            schema_name=schema_name,
            prompt=prompt,
            worktree=worktree,
            session_id=session_id,
            session_generation=session_generation,
            invocation_id=invocation_id,
            turn_id=turn_id,
            read_only=read_only,
        )

class OneProcessFailureBackend(FakeAgentBackend):
    """首次调用返回进程失败，随后正常响应。"""

    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """执行模拟调用；首次返回失败 AgentResult，后续委托父实现。"""
        if not self.failed_once:
            self.failed_once = True
            return AgentResult(
                ok=False,
                agent=agent,
                session_generation=session_generation,
                turn_id=turn_id,
                return_code=1,
                elapsed_seconds=0.01,
                raw_output_ref=f"raw/{invocation_id}.json",
                error="simulated process failure",
            )
        return super().invoke(
            agent=agent,
            response_model=response_model,
            schema_name=schema_name,
            prompt=prompt,
            worktree=worktree,
            session_id=session_id,
            session_generation=session_generation,
            invocation_id=invocation_id,
            turn_id=turn_id,
            read_only=read_only,
        )

class OneNetworkFailureBackend(FakeAgentBackend):
    """首次调用返回网络失败，随后正常响应。"""

    def __init__(self) -> None:
        """创建一次性网络失败后端；无参数，返回实例，不抛异常。"""
        super().__init__()
        self.failed_once = False

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """执行模拟调用；首次返回网络失败，后续委托父实现。"""
        if not self.failed_once:
            self.failed_once = True
            return AgentResult(
                ok=False,
                agent=agent,
                session_generation=session_generation,
                turn_id=turn_id,
                return_code=1,
                elapsed_seconds=0.01,
                raw_output_ref=f"raw/{invocation_id}.json",
                error="WebSocket TLS handshake failed",
                failure_category=FailureCategory.NETWORK_ERROR.value,
            )
        return super().invoke(
            agent=agent,
            response_model=response_model,
            schema_name=schema_name,
            prompt=prompt,
            worktree=worktree,
            session_id=session_id,
            session_generation=session_generation,
            invocation_id=invocation_id,
            turn_id=turn_id,
            read_only=read_only,
        )

class WriterBoundaryFailureBackend(FakeAgentBackend):
    """首次 Writer 命中 Session 边界，重建后正常响应。"""

    def __init__(self) -> None:
        super().__init__()
        self.failed_writer = False
        self.failed_once = False

    def invoke(
        self,
        *,
        agent: AgentName,
        response_model: type[ResponseModel],
        schema_name: str,
        prompt: str,
        worktree: Path,
        session_id: str | None,
        session_generation: int,
        invocation_id: str,
        turn_id: str,
        read_only: bool,
    ) -> AgentResult:
        """模拟 Writer Session 边界失败；参数同后端协议，返回结果或首次抛错。"""
        if response_model is TurnResult and not read_only and not self.failed_writer:
            self.failed_writer = True
            self.failed_once = True
            raise SessionBoundaryError("resume boundary")
        return super().invoke(
            agent=agent,
            response_model=response_model,
            schema_name=schema_name,
            prompt=prompt,
            worktree=worktree,
            session_id=session_id,
            session_generation=session_generation,
            invocation_id=invocation_id,
            turn_id=turn_id,
            read_only=read_only,
        )
