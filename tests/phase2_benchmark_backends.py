"""Phase 2 基准测试的固定数据与模拟后端。"""

import json
from pathlib import Path
from typing import TypeVar

from git_helpers import git
from pydantic import BaseModel

from debate_agent.models import (
    AgentName,
    AgentResult,
    Ballot,
    ChangeAck,
    CompletionAssessment,
    Decision,
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
    ReviewFinding,
    ReviewResult,
)
from debate_agent.runtime import AgentBackend

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "adversarial_review_phase_2"

PHASE_ONE_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "adversarial_review_prompt"
)

REPORT = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "superpowers"
    / "reports"
    / "adversarial-review-phase-2.md"
)

PHASE_ONE_REPORT = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "superpowers"
    / "reports"
    / "adversarial-review-phase-1.md"
)

REQUIRED_CONTROLS = {
    "protocol_version",
    "agent_combination",
    "agent_profiles",
    "initial_commit_sha",
    "goal_contract",
    "validation_commands",
    "max_turns",
    "timeout_seconds",
    "facilitator",
    "random_seed",
    "user_participation",
    "repetitions",
}

REQUIRED_GOAL_CONTRACT_FIELDS = {
    "version",
    "objective",
    "deliverables",
    "acceptance_criteria",
    "baseline_validation_commands",
    "added_validation_commands",
    "allowed_scope",
    "protected_scope",
    "derived_scope",
    "immutable_constraints",
}

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)

class FixedReviewBackend(AgentBackend):
    """用固定输入经真实 Runtime 节点驱动 review-v1 的 fake CLI。"""

    def __init__(
        self,
        *,
        controls: dict[str, object],
        task: dict[str, object] | None = None,
        mutate_review: bool = False,
        assert_full_goal_contract: bool = True,
    ) -> None:
        """创建基准 fake；参数为冻结控制变量和只读故障开关，无返回值。"""
        self.controls = controls
        self.task = task
        self.mutate_review = mutate_review
        self.assert_full_goal_contract = assert_full_goal_contract
        self.calls: list[tuple[str, bool, str]] = []

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
        """返回绑定 Prompt/current HEAD 的 ChangeAck 或 ReviewResult。

        参数与 AgentBackend 一致；返回确定性 AgentResult。协议不匹配、调用非只读
        或 Review 故意写入以外的格式错误时抛 AssertionError。
        """
        self.calls.append((response_model.__name__, read_only, prompt))
        head = git(worktree, "rev-parse", "HEAD")
        if response_model is ChangeAck:
            payload: BaseModel = ChangeAck(
                agent=agent,
                commit_sha=head,
                reviewed_paths=["result.txt"],
                understood_changes=["已读取固定 Writer checkpoint"],
                risks_or_disputes=[],
                decision=Decision.ACCEPT,
            )
        elif response_model is ReviewResult:
            assert schema_name == "review.schema.json"
            assert read_only is True
            assert "review-v1" in prompt
            assert f"CURRENT_HEAD={head}" in prompt
            if self.assert_full_goal_contract:
                prompt_goal_contract = json.loads(
                    prompt.split("GoalContract:\n", 1)[1].split(
                        "\n返回 ReviewResult JSON", 1
                    )[0]
                )
                assert prompt_goal_contract == self.controls["goal_contract"]
            if self.mutate_review:
                (worktree / "reviewer-write.txt").write_text(
                    "forbidden", encoding="utf-8"
                )
            report_ref = prompt.split("CURRENT_VALIDATION_REPORT=", 1)[1].splitlines(
            )[0]
            git_ref = EvidenceRef(
                kind=EvidenceKind.GIT_PATH,
                head_sha=head,
                artifact_ref=None,
                path="result.txt",
                locator=None,
            )
            validation_ref = EvidenceRef(
                kind=EvidenceKind.VALIDATION_REPORT,
                head_sha=head,
                artifact_ref=report_ref,
                path=None,
                locator=None,
            )
            blocking = bool(self.task and self.task["blocking"])
            finding = (
                ReviewFinding(
                    finding_id=f"FIND-{self.task['fingerprint']}",
                    dimension=ReviewDimension.GOAL_SCOPE,
                    claim=f"{self.task['id']} 未满足目标",
                    impact="用户目标不能完成",
                    blocking=blocking,
                    evidence_refs=[git_ref],
                    proposed_change="补齐对应验收实现和验证",
                )
                if self.task is not None and self.task["id"] != "clean-task"
                else None
            )
            payload = ReviewResult(
                agent=agent,
                turn_id=turn_id,
                head_sha=head,
                goal_contract_version=1,
                protocol_version=ReviewProtocolVersion.REVIEW_V1,
                coverage=[
                    ReviewCoverage(
                        dimension=dimension,
                        status=ReviewCoverageStatus.PASS,
                        summary=f"通过 {dimension.value}",
                        evidence_refs=(
                            [validation_ref]
                            if dimension is ReviewDimension.VALIDATION
                            else [git_ref]
                        ),
                    )
                    for dimension in ReviewDimension
                ],
                findings=[finding] if finding is not None else [],
                issue_dispositions=[],
                strongest_counterexample="result.txt 没有 Writer checkpoint",
                counterexample_resolution="当前 HEAD 的 Git 路径和验证报告可复核",
                completion_assessment=(
                    CompletionAssessment.INCOMPLETE
                    if blocking
                    else CompletionAssessment.COMPLETE
                ),
                decision=Decision.REVISE if blocking else Decision.ACCEPT,
                summary="固定四面结构化审查通过",
            )
        else:
            raise AssertionError(response_model)
        parsed = response_model.model_validate(payload)
        return AgentResult(
            ok=True,
            agent=agent,
            session_id=f"{agent.value}-session",
            session_generation=session_generation,
            turn_id=turn_id,
            observed_head=getattr(parsed, "observed_head", None),
            structured_output=parsed.model_dump(mode="json"),
            return_code=0,
            elapsed_seconds=0.01,
            raw_output_ref=f"raw/{invocation_id}.json",
        )

class LifecycleBenchmarkBackend(FixedReviewBackend):
    """驱动完整 LangGraph 生命周期并注入一次真实最终 Ballot 打回。"""

    def __init__(
        self,
        *,
        controls: dict[str, object],
        state_root: Path,
        run_id: str,
        reject_first_ballot: bool,
    ) -> None:
        """创建生命周期 fake。

        参数为冻结控制变量、实际产物目录、运行 ID 和首次否决开关；无返回值。首次
        否决时从已经持久化的当前 HEAD 验证报告中读取证据引用，避免把内存标记伪装成
        Ballot 证据。
        """
        super().__init__(controls=controls, assert_full_goal_contract=False)
        self.state_root = state_root
        self.run_id = run_id
        self.reject_first_ballot = reject_first_ballot
        self.writer_count = 0
        self.ballot_round = 0
        self.ballot_outcomes: list[CompletionAssessment] = []

    def _current_validation_evidence(self) -> str:
        """读取当前 run 已持久化的 review HEAD 验证报告。

        无参数，返回相对报告引用；找不到唯一当前报告时抛 AssertionError。
        """
        reports = sorted(
            (
                path.relative_to(self.state_root / "runs" / self.run_id).as_posix()
                for path in (
                    self.state_root / "runs" / self.run_id / "validation"
                ).glob("REVIEW-HEAD-*.json")
            ),
        )
        assert reports
        return reports[-1]

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
        """执行 Writer、Review、Ballot 和 TreeAck 的完整固定生命周期调用。"""
        if response_model is TurnResult:
            assert read_only is False
            head = git(worktree, "rev-parse", "HEAD")
            self.writer_count += 1
            (worktree / "result.txt").write_text(
                f"writer-{self.writer_count}\n", encoding="utf-8"
            )
            payload: BaseModel = TurnResult(
                agent=agent,
                turn_id=turn_id,
                base_commit=head,
                observed_head=head,
                goal_contract_version=1,
                decision=Decision.ACCEPT,
                summary="生命周期 Writer checkpoint",
                accepted_points=[],
                issues=[],
                requested_scope_additions=[],
                requested_validation_additions=[],
                completion_assessment=CompletionAssessment.COMPLETE,
            )
        elif response_model is Ballot:
            self.ballot_round += 1
            incomplete = self.reject_first_ballot and self.ballot_round == 2
            evidence_ref = self._current_validation_evidence()
            payload = Ballot(
                agent=agent,
                commit_sha=git(worktree, "rev-parse", "HEAD"),
                goal_contract_version=1,
                completion=(
                    CompletionAssessment.INCOMPLETE
                    if incomplete
                    else CompletionAssessment.COMPLETE
                ),
                criteria=[{"criterion": "fixed", "satisfied": not incomplete}],
                rationale=(
                    f"反例揭示遗漏验收；Evidence: {evidence_ref}"
                    if incomplete
                    else "验收满足"
                ),
            )
            self.ballot_outcomes.append(payload.completion)
        elif response_model is TreeAck:
            tree = prompt.split("FINAL_TREE_SHA=", 1)[1].splitlines()[0]
            payload = TreeAck(
                agent=agent,
                final_tree_sha=tree,
                accepted=True,
                rationale="固定 tree 已确认",
            )
        else:
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
        self.calls.append((response_model.__name__, read_only, prompt))
        parsed = response_model.model_validate(payload)
        return AgentResult(
            ok=True,
            agent=agent,
            session_id=f"{agent.value}-session",
            session_generation=session_generation,
            turn_id=turn_id,
            observed_head=getattr(parsed, "observed_head", None),
            structured_output=parsed.model_dump(mode="json"),
            return_code=0,
            elapsed_seconds=0.01,
            raw_output_ref=f"raw/{invocation_id}.json",
        )
