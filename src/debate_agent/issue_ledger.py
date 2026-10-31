"""review-v1 可选 Issue Ledger 的唯一事实来源与受控状态转换。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import Enum

from pydantic import Field, model_validator

from debate_agent.artifacts import ArtifactError, ArtifactStore
from debate_agent.models import AgentName, StrictModel
from debate_agent.review_models import (
    EvidenceRef,
    ReviewDimension,
    ReviewIssueDispositionStatus,
    ReviewResult,
)


class IssueLedgerError(RuntimeError):
    """表示 Ledger 权限、绑定、证据或持久化状态不满足协议。"""


class IssueStatus(str, Enum):
    """标识最小三状态 Issue 生命周期。"""

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class LedgerIssue(StrictModel):
    """保存一个从结构化 Reviewer Finding 派生的稳定 Issue。"""

    issue_id: str = Field(min_length=1)
    origin_finding_id: str = Field(min_length=1)
    status: IssueStatus
    dimension: ReviewDimension
    claim: str = Field(min_length=1)
    blocking: bool
    affected_paths: tuple[str, ...] = ()
    opened_head: str = Field(min_length=1)
    opened_by: AgentName
    evidence_refs: tuple[EvidenceRef, ...]
    addressed_by_commit: str | None = None
    addressed_by_agent: AgentName | None = None
    resolved_head: str | None = None
    resolved_by: AgentName | None = None
    resolution_evidence_refs: tuple[EvidenceRef, ...] = ()
    duplicate_of: str | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> LedgerIssue:
        """校验 Issue 状态与处理/结论字段的一致性。

        无参数；返回自身；状态缺少所需证据、Writer 直接关闭或关联自身重复时抛
        ``ValueError``。
        """
        if any(ref.head_sha != self.opened_head for ref in self.evidence_refs):
            raise ValueError("Issue 初始 EvidenceRef 必须绑定 opened_head")
        if self.duplicate_of == self.issue_id:
            raise ValueError("duplicate_of 不能指向自身")
        if self.addressed_by_commit is None:
            if self.addressed_by_agent is not None:
                raise ValueError("未登记处理提交时不得记录 Writer")
        elif self.addressed_by_agent is None:
            raise ValueError("处理提交必须记录 Writer")
        if self.status is IssueStatus.OPEN:
            if self.resolved_head is not None or self.resolved_by is not None:
                raise ValueError("OPEN Issue 不得包含关闭信息")
            if self.resolution_evidence_refs:
                raise ValueError("OPEN Issue 不得包含结论证据")
        else:
            if self.resolved_head is None or self.resolved_by is None:
                raise ValueError("已关闭 Issue 必须记录结论 HEAD 和 Agent")
            if not self.resolution_evidence_refs:
                raise ValueError("已关闭 Issue 必须提供结论 EvidenceRef")
            if any(
                ref.head_sha != self.resolved_head
                for ref in self.resolution_evidence_refs
            ):
                raise ValueError("结论 EvidenceRef 必须绑定 resolved_head")
            if (
                self.status is IssueStatus.RESOLVED
                and self.resolved_by is self.addressed_by_agent
            ):
                raise ValueError("Writer 不得自行将 Issue 标为 resolved")
        return self


class IssueTransition(StrictModel):
    """保存一次由 Supervisor 记录的不可变 Issue 生命周期事件。"""

    transition_id: str = Field(min_length=1)
    issue_id: str = Field(min_length=1)
    actor: AgentName
    head_sha: str = Field(min_length=1)
    action: str = Field(min_length=1)
    from_status: IssueStatus | None = None
    to_status: IssueStatus | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    source_finding_id: str | None = None
    occurred_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class IssueLedger(StrictModel):
    """保存 run 内唯一可变账本及其追加式转换记录。"""

    protocol_version: str = "review-v1"
    issues: tuple[LedgerIssue, ...] = ()
    transitions: tuple[IssueTransition, ...] = ()

    @model_validator(mode="after")
    def validate_unique_identity(self) -> IssueLedger:
        """校验 Ledger 中 Issue 和转换 ID 的唯一性。

        无参数；返回自身；ID 重复或转换引用不存在 Issue 时抛 ``ValueError``。
        """
        issue_ids = [issue.issue_id for issue in self.issues]
        transition_ids = [transition.transition_id for transition in self.transitions]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("Issue Ledger 不允许重复 issue_id")
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("Issue Ledger 不允许重复 transition_id")
        if any(transition.issue_id not in issue_ids for transition in self.transitions):
            raise ValueError("IssueTransition 引用了不存在的 Issue")
        return self


def _stable_id(prefix: str, *parts: str) -> str:
    """生成账本幂等身份；参数为前缀/稳定字段，返回短 SHA 标识，不抛异常。"""
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _paths_from_evidence(references: Iterable[EvidenceRef]) -> tuple[str, ...]:
    """从证据投影受影响 Git 路径；参数为证据，返回去重路径，不抛异常。"""
    return tuple(dict.fromkeys(ref.path for ref in references if ref.path))


class IssueLedgerStore:
    """由 Supervisor 独占写入的 Ledger Store，不承担业务语义裁决。"""

    def __init__(self, *, artifacts: ArtifactStore) -> None:
        """创建 Store；参数为本 run ArtifactStore，返回实例，不读取文件。"""
        self.artifacts = artifacts

    def _enabled(self) -> None:
        """确认当前 run 允许读取账本；无参数，无返回值，禁用或旧协议时抛错。"""
        config = self.artifacts.load_config()
        if config.protocol_version.value != "review-v1":
            raise IssueLedgerError("legacy-v0 不得加载 Issue Ledger")
        if not config.issue_ledger_enabled:
            raise IssueLedgerError("当前 Run 未显式启用 Issue Ledger")

    def load(self) -> IssueLedger:
        """加载唯一账本。

        无参数；返回当前 Ledger；未创建时返回空账本，损坏时写诊断并抛
        ``IssueLedgerError``，绝不静默恢复默认状态。
        """
        self._enabled()
        try:
            payload = self.artifacts.read_issue_ledger_payload()
            return (
                IssueLedger()
                if payload is None
                else IssueLedger.model_validate(payload)
            )
        except (ArtifactError, ValueError) as exc:
            self.artifacts.write_diagnostic(
                phase="issue_ledger_recovery", error=str(exc)
            )
            raise IssueLedgerError("Issue Ledger 已损坏，已停止恢复") from exc

    def _save(self, ledger: IssueLedger) -> str:
        """保存经校验账本；参数为账本，返回相对引用，写入失败时抛错。"""
        self._enabled()
        try:
            return self.artifacts.write_issue_ledger_payload(
                ledger.model_dump(mode="json")
            )
        except ArtifactError as exc:
            raise IssueLedgerError("Issue Ledger 持久化失败") from exc

    @staticmethod
    def _assert_evidence(*, references: tuple[EvidenceRef, ...], head_sha: str) -> None:
        """校验转换证据绑定；参数为证据/HEAD，无返回值，缺失或错绑时抛错。"""
        if not references:
            raise IssueLedgerError("关闭 Issue 必须提供已校验 EvidenceRef")
        if any(reference.head_sha != head_sha for reference in references):
            raise IssueLedgerError("Issue 转换 EvidenceRef 必须绑定当前 HEAD")

    @staticmethod
    def _assert_current_head(*, supplied_head: str, current_head: str) -> None:
        """校验转换绑定当前 Supervisor HEAD。

        参数为调用携带的 HEAD 与 Supervisor 当前 HEAD；无返回值；两者不一致时抛
        ``IssueLedgerError``。Store 不读取 Git，因此 Runtime 必须传入真实 HEAD。
        """
        if supplied_head != current_head:
            raise IssueLedgerError("Issue 转换必须绑定当前 HEAD")

    @staticmethod
    def _replace_issue(ledger: IssueLedger, replacement: LedgerIssue) -> IssueLedger:
        """替换账本中单个 Issue；参数为账本/新 Issue，返回新账本，不抛异常。"""
        return ledger.model_copy(
            update={
                "issues": tuple(
                    replacement if item.issue_id == replacement.issue_id else item
                    for item in ledger.issues
                )
            }
        )

    @staticmethod
    def _append_transition(
        ledger: IssueLedger, transition: IssueTransition
    ) -> IssueLedger:
        """追加幂等转换；参数为账本/转换，返回账本，冲突时抛 ``IssueLedgerError``。"""
        existing = next(
            (
                item
                for item in ledger.transitions
                if item.transition_id == transition.transition_id
            ),
            None,
        )
        if existing is not None:
            if existing != transition:
                raise IssueLedgerError("相同 IssueTransition ID 对应不同内容")
            return ledger
        return ledger.model_copy(
            update={"transitions": (*ledger.transitions, transition)}
        )

    def register_review(
        self, review: ReviewResult, *, current_head: str | None = None
    ) -> tuple[IssueLedger, str]:
        """将阻断 Reviewer Finding 幂等登记为 OPEN Issue。

        参数为已通过 ReviewPolicy 的 ReviewResult；返回更新后 Ledger 与引用；非
        review-v1、无证据 Finding 或身份冲突时抛 ``IssueLedgerError``。
        """
        self._enabled()
        if current_head is not None:
            self._assert_current_head(
                supplied_head=review.head_sha, current_head=current_head
            )
        ledger = self.load()
        for finding in review.findings:
            if not finding.blocking:
                continue
            references = tuple(finding.evidence_refs)
            self._assert_evidence(references=references, head_sha=review.head_sha)
            issue_id = _stable_id("ISSUE", review.agent.value, finding.finding_id)
            issue = LedgerIssue(
                issue_id=issue_id,
                origin_finding_id=finding.finding_id,
                status=IssueStatus.OPEN,
                dimension=finding.dimension,
                claim=finding.claim,
                blocking=True,
                affected_paths=_paths_from_evidence(references),
                opened_head=review.head_sha,
                opened_by=review.agent,
                evidence_refs=references,
            )
            existing = next(
                (item for item in ledger.issues if item.issue_id == issue_id), None
            )
            if existing is not None:
                if existing != issue:
                    raise IssueLedgerError("相同 Finding 已对应不同 Issue 内容")
                continue
            transition = IssueTransition(
                transition_id=_stable_id("ITR", issue_id, "open", review.head_sha),
                issue_id=issue_id,
                actor=review.agent,
                head_sha=review.head_sha,
                action="open_from_review",
                to_status=IssueStatus.OPEN,
                evidence_refs=references,
                source_finding_id=finding.finding_id,
            )
            ledger = ledger.model_copy(update={"issues": (*ledger.issues, issue)})
            ledger = self._append_transition(ledger, transition)
        return ledger, self._save(ledger)

    def record_addressed(
        self,
        *,
        issue_id: str,
        writer: AgentName,
        commit_sha: str,
        current_head: str,
    ) -> tuple[IssueLedger, str]:
        """登记 Writer 声称处理的提交但保持 OPEN。

        参数为 Issue、Writer 和其提交 HEAD；返回账本/引用；Issue 不存在、已关闭、
        提交与当前 HEAD 不一致或重复内容冲突时抛 ``IssueLedgerError``。OPEN Issue
        可登记后续 Writer checkpoint；账本保留每次尝试的不可变转换，并将最新尝试
        作为待复核的 ``addressed_by_commit``。
        """
        self._assert_current_head(
            supplied_head=commit_sha, current_head=current_head
        )
        ledger = self.load()
        issue = next(
            (item for item in ledger.issues if item.issue_id == issue_id), None
        )
        if issue is None:
            raise IssueLedgerError("Issue 不存在")
        replacement = issue.model_copy(
            update={"addressed_by_commit": commit_sha, "addressed_by_agent": writer}
        )
        if issue.status is not IssueStatus.OPEN:
            if issue == replacement:
                return ledger, self._save(ledger)
            raise IssueLedgerError("已关闭 Issue 不得登记不同的 Writer 处理")
        if issue == replacement:
            return ledger, self._save(ledger)
        transition = IssueTransition(
            transition_id=_stable_id(
                "ITR", issue_id, "address", writer.value, commit_sha
            ),
            issue_id=issue_id,
            actor=writer,
            head_sha=commit_sha,
            action="addressed_by_writer",
            from_status=IssueStatus.OPEN,
            to_status=IssueStatus.OPEN,
        )
        ledger = self._replace_issue(ledger, replacement)
        ledger = self._append_transition(ledger, transition)
        return ledger, self._save(ledger)

    def close_with_reviewer_evidence(
        self,
        *,
        issue_id: str,
        reviewer: AgentName,
        head_sha: str,
        status: IssueStatus,
        evidence_refs: tuple[EvidenceRef, ...],
        current_head: str,
        validated_evidence_refs: tuple[EvidenceRef, ...],
    ) -> tuple[IssueLedger, str]:
        """由 Reviewer 用证据关闭 Issue。

        参数为 Issue、Reviewer、HEAD、目标状态和已校验证据；返回账本/引用；状态非
        RESOLVED/DISMISSED、Writer 自证、证据错绑或重复冲突时抛 ``IssueLedgerError``。
        """
        if status not in {IssueStatus.RESOLVED, IssueStatus.DISMISSED}:
            raise IssueLedgerError("Reviewer 只能关闭为 resolved 或 dismissed")
        self._assert_current_head(supplied_head=head_sha, current_head=current_head)
        self._assert_evidence(references=evidence_refs, head_sha=head_sha)
        if any(reference not in validated_evidence_refs for reference in evidence_refs):
            raise IssueLedgerError("关闭 Issue 的 EvidenceRef 未经 ReviewPolicy 校验")
        ledger = self.load()
        issue = next(
            (item for item in ledger.issues if item.issue_id == issue_id), None
        )
        if issue is None:
            raise IssueLedgerError("Issue 不存在")
        if issue.status is not IssueStatus.OPEN:
            expected = issue.model_copy(
                update={
                    "status": status,
                    "resolved_head": head_sha,
                    "resolved_by": reviewer,
                    "resolution_evidence_refs": evidence_refs,
                }
            )
            if issue == expected:
                return ledger, self._save(ledger)
            raise IssueLedgerError("已关闭 Issue 不得重复转换为不同结论")
        if status is IssueStatus.RESOLVED and issue.addressed_by_commit is None:
            raise IssueLedgerError("resolved Issue 必须先登记 Writer 处理提交")
        if status is IssueStatus.RESOLVED and reviewer is issue.addressed_by_agent:
            raise IssueLedgerError("Writer 不得自行验证为 resolved")
        replacement = issue.model_copy(
            update={
                "status": status,
                "resolved_head": head_sha,
                "resolved_by": reviewer,
                "resolution_evidence_refs": evidence_refs,
            }
        )
        transition = IssueTransition(
            transition_id=_stable_id(
                "ITR", issue_id, status.value, reviewer.value, head_sha
            ),
            issue_id=issue_id,
            actor=reviewer,
            head_sha=head_sha,
            action=f"reviewer_{status.value}",
            from_status=IssueStatus.OPEN,
            to_status=status,
            evidence_refs=evidence_refs,
            source_finding_id=issue.origin_finding_id,
        )
        ledger = self._replace_issue(ledger, replacement)
        ledger = self._append_transition(ledger, transition)
        return ledger, self._save(ledger)

    def apply_review_dispositions(
        self,
        *,
        review: ReviewResult,
        current_head: str,
        validated_evidence_refs: tuple[EvidenceRef, ...],
    ) -> tuple[IssueLedger, str]:
        """按已校验 ReviewResult 对既有 OPEN Issue 应用处置。

        参数为真实 Review、Supervisor 当前 HEAD 和已由 ReviewPolicy/EvidenceResolver
        解析过的证据；返回更新后的 Ledger 与引用。处置遗漏、引用非本 Review 证据、
        错误 HEAD 或非法角色均抛 ``IssueLedgerError``，不根据 claim 文本猜测关联。
        """
        self._assert_current_head(
            supplied_head=review.head_sha, current_head=current_head
        )
        ledger = self.load()
        issues_by_id = {issue.issue_id: issue for issue in ledger.issues}
        finding_ids = {finding.finding_id for finding in review.findings}
        open_ids = {
            issue.issue_id
            for issue in ledger.issues
            if issue.status is IssueStatus.OPEN and issue.blocking
            and not (
                issue.opened_head == review.head_sha
                and issue.opened_by is review.agent
                and issue.origin_finding_id in finding_ids
            )
        }
        dispositions = {item.issue_id: item for item in review.issue_dispositions}
        if not open_ids.issubset(dispositions) or not set(dispositions).issubset(
            issues_by_id
        ):
            raise IssueLedgerError("ReviewResult 必须完整处置审查开始前的 OPEN Issue")
        for disposition in review.issue_dispositions:
            issue = issues_by_id[disposition.issue_id]
            if issue.status is not IssueStatus.OPEN:
                if (
                    disposition.status.value == issue.status.value
                    and issue.resolved_head == review.head_sha
                    and issue.resolved_by is review.agent
                    and issue.resolution_evidence_refs
                    == tuple(disposition.evidence_refs)
                ):
                    continue
                raise IssueLedgerError("已关闭 Issue 的重放处置与既有结论不一致")
            if disposition.status is ReviewIssueDispositionStatus.OPEN:
                continue
            status = IssueStatus(disposition.status.value)
            self.close_with_reviewer_evidence(
                issue_id=disposition.issue_id,
                reviewer=review.agent,
                head_sha=review.head_sha,
                status=status,
                evidence_refs=tuple(disposition.evidence_refs),
                current_head=current_head,
                validated_evidence_refs=validated_evidence_refs,
            )
        updated = self.load()
        return updated, self._save(updated)

    def set_duplicate_of(
        self, *, issue_id: str, duplicate_of: str | None
    ) -> tuple[IssueLedger, str]:
        """记录重复关系而不继承目标 Issue 状态。

        参数为 Issue 和可选重复目标；返回账本/引用；目标不存在、自指或重复内容冲突
        时抛 ``IssueLedgerError``。
        """
        ledger = self.load()
        issue = next(
            (item for item in ledger.issues if item.issue_id == issue_id), None
        )
        if issue is None:
            raise IssueLedgerError("Issue 不存在")
        if duplicate_of is not None and not any(
            item.issue_id == duplicate_of for item in ledger.issues
        ):
            raise IssueLedgerError("duplicate_of 指向不存在的 Issue")
        replacement = issue.model_copy(update={"duplicate_of": duplicate_of})
        if issue.duplicate_of is not None and issue != replacement:
            raise IssueLedgerError("Issue 已关联不同 duplicate_of")
        transition = IssueTransition(
            transition_id=_stable_id(
                "ITR", issue_id, "duplicate", duplicate_of or "none"
            ),
            issue_id=issue_id,
            actor=issue.opened_by,
            head_sha=issue.opened_head,
            action="set_duplicate_of",
            from_status=issue.status,
            to_status=issue.status,
            source_finding_id=issue.origin_finding_id,
        )
        ledger = self._replace_issue(ledger, replacement)
        ledger = self._append_transition(ledger, transition)
        return ledger, self._save(ledger)

    def blocking_projection(self) -> tuple[LedgerIssue, ...]:
        """投影所有未关闭阻断 Issue；无参数，返回稳定排序元组，损坏时抛异常。"""
        return tuple(
            issue
            for issue in self.load().issues
            if issue.status is IssueStatus.OPEN and issue.blocking
        )
