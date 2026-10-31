"""对抗审查协议的结构化模型。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from debate_agent.models import (
    AgentName,
    CompletionAssessment,
    Decision,
    ReviewProtocolVersion,
    StrictModel,
)


class EvidenceKind(str, Enum):
    """标识机器可校验证据的来源类型。"""

    GIT_PATH = "git_path"
    GIT_DIFF = "git_diff"
    VALIDATION_REPORT = "validation_report"
    TEST_RESULT = "test_result"


class ReviewDimension(str, Enum):
    """标识四个必须覆盖的核心审查面。"""

    GOAL_SCOPE = "goal_scope"
    CORRECTNESS = "correctness"
    INTEGRATION = "integration"
    VALIDATION = "validation"


class ReviewCoverageStatus(str, Enum):
    """标识某个核心审查面的结论状态。"""

    PASS = "pass"
    CHALLENGE = "challenge"
    UNKNOWN = "unknown"


class ReviewIssueDispositionStatus(str, Enum):
    """标识 Reviewer 对既有 Ledger Issue 的本轮处置。"""

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class EvidenceRef(StrictModel):
    """保存一条绑定到指定提交或运行产物的证据引用。"""

    kind: EvidenceKind
    head_sha: str = Field(min_length=1)
    artifact_ref: str | None
    path: str | None
    locator: str | None

    @model_validator(mode="after")
    def validate_reference_shape(self) -> EvidenceRef:
        """校验证据引用的最小结构。

        无参数，返回自身；Git 证据缺少仓库路径或运行产物证据缺少产物引用时抛
        ``ValueError``。该方法不检查路径存在性或 Git 提交，相关策略由
        ``ReviewPolicy`` 在后续模块负责。
        """
        if self.kind in {EvidenceKind.GIT_PATH, EvidenceKind.GIT_DIFF}:
            if not self.path:
                raise ValueError("Git 证据必须提供 path")
        elif not self.artifact_ref:
            raise ValueError("运行产物证据必须提供 artifact_ref")
        return self


class ReviewCoverage(StrictModel):
    """保存一个核心审查面及其可选证据。"""

    dimension: ReviewDimension
    status: ReviewCoverageStatus
    summary: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef]


class ReviewFinding(StrictModel):
    """保存一项可由代码、Git 或验证结果反证的审查发现。"""

    finding_id: str = Field(min_length=1)
    dimension: ReviewDimension
    claim: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    blocking: bool
    evidence_refs: list[EvidenceRef]
    proposed_change: str | None

    @model_validator(mode="after")
    def validate_blocking_evidence(self) -> ReviewFinding:
        """拒绝没有机器证据的阻断发现。

        无参数，返回自身；阻断发现没有 ``evidence_refs`` 时抛 ``ValueError``。
        证据的来源、路径与提交有效性不在本模型中判断。
        """
        if self.blocking and not self.evidence_refs:
            raise ValueError("阻断 Finding 必须提供至少一条 EvidenceRef")
        return self


class ReviewIssueDisposition(StrictModel):
    """保存 Reviewer 对调用上下文中既有 Issue 的证据化处置。"""

    issue_id: str = Field(min_length=1)
    status: ReviewIssueDispositionStatus
    rationale: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = []

    @model_validator(mode="after")
    def validate_closed_disposition_evidence(self) -> ReviewIssueDisposition:
        """拒绝无机器证据的关闭处置。

        无参数；返回自身；``resolved`` 或 ``dismissed`` 未提供 EvidenceRef 时抛
        ``ValueError``。证据与当前 HEAD 的真实性由 ``ReviewPolicy`` 校验。
        """
        if (
            self.status
            in {
                ReviewIssueDispositionStatus.RESOLVED,
                ReviewIssueDispositionStatus.DISMISSED,
            }
            and not self.evidence_refs
        ):
            raise ValueError("关闭既有 Issue 必须提供至少一条 EvidenceRef")
        return self


class ReviewResult(StrictModel):
    """保存 Reviewer 对固定 HEAD 的独立结构化审查结论。"""

    agent: AgentName
    turn_id: str = Field(min_length=1)
    head_sha: str = Field(min_length=1)
    goal_contract_version: int = Field(ge=1)
    protocol_version: Literal[ReviewProtocolVersion.REVIEW_V1]
    coverage: list[ReviewCoverage]
    findings: list[ReviewFinding]
    issue_dispositions: list[ReviewIssueDisposition]
    strongest_counterexample: str | None
    counterexample_resolution: str | None
    completion_assessment: CompletionAssessment
    decision: Decision
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_review_consistency(self) -> ReviewResult:
        """校验覆盖完整性和完成结论的一致性。

        无参数，返回自身；核心维度缺失或重复、未知覆盖或阻断发现却宣称完成、
        以及完成时缺少反例/处理说明时抛 ``ValueError``。
        """
        dimensions = [item.dimension for item in self.coverage]
        required_dimensions = set(ReviewDimension)
        if (
            len(dimensions) != len(required_dimensions)
            or set(dimensions) != required_dimensions
        ):
            raise ValueError("四个核心 ReviewCoverage 必须各出现一次")
        issue_ids = [item.issue_id for item in self.issue_dispositions]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("同一既有 Issue 只能出现一次处置")
        if self.completion_assessment is CompletionAssessment.COMPLETE:
            if any(
                item.status is ReviewCoverageStatus.UNKNOWN
                for item in self.coverage
            ):
                raise ValueError("存在 unknown Coverage 时不得标记 complete")
            if any(item.blocking for item in self.findings):
                raise ValueError("存在阻断 Finding 时不得标记 complete")
            if not self.strongest_counterexample or not self.counterexample_resolution:
                raise ValueError("complete Review 必须提供最强反例及处理说明")
            coverage_by_dimension = {
                item.dimension: item for item in self.coverage
            }
            for dimension in (
                ReviewDimension.GOAL_SCOPE,
                ReviewDimension.VALIDATION,
            ):
                if not coverage_by_dimension[dimension].evidence_refs:
                    raise ValueError(
                        "complete Review 的 goal_scope 和 validation "
                        "必须提供 EvidenceRef"
                    )
        return self
