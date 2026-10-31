"""review-v1 Reviewer 的只读调用与机器门禁封装。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from debate_agent.git_workspace import WorkspaceRaceError
from debate_agent.models import AgentName
from debate_agent.prompts import build_structured_reviewer_prompt
from debate_agent.review_models import ReviewResult
from debate_agent.review_policy import ReviewPolicy


class ReviewRuntimeError(RuntimeError):
    """表示结构化审查的绑定、只读或持久化门禁失败。"""


@dataclass(frozen=True, slots=True)
class ReviewInvocation:
    """保存一次结构化审查的已冻结调用上下文。"""

    reviewer: AgentName
    turn_id: str
    head_sha: str
    goal_contract_version: int
    goal_contract_json: str
    validation_report_ref: str
    open_issues: tuple[dict[str, str], ...] = ()


class ReviewRuntime:
    """执行 review-v1 的只读调用，不持有独立的产物写入能力。"""

    def __init__(
        self,
        *,
        invoke: Callable[[str], ReviewResult],
        fingerprint: Callable[[], str],
        policy: ReviewPolicy,
        persist: Callable[[ReviewResult], str],
    ) -> None:
        """创建只读 Review 调用器。

        参数为受控调用、工作区指纹、机器策略和 Supervisor 注入的持久化回调；返回
        实例。调用、指纹、证据校验或持久化失败时由 ``run`` 抛
        ``ReviewRuntimeError``。本对象不直接取得 ArtifactStore 写权限。
        """
        self._invoke = invoke
        self._fingerprint = fingerprint
        self._policy = policy
        self._persist = persist

    def run(self, invocation: ReviewInvocation) -> tuple[ReviewResult, str]:
        """运行并校验一个绑定当前 HEAD 的结构化审查。

        参数为冻结调用上下文；返回 ReviewResult 与 Supervisor 持久化引用。Reviewer
        修改工作区、返回身份/HEAD/GoalContract 不匹配、证据不合法或写入失败时抛
        ``ReviewRuntimeError``。
        """
        before = self._fingerprint()
        prompt = build_structured_reviewer_prompt(
            reviewer=invocation.reviewer.value,
            head_sha=invocation.head_sha,
            goal_contract_json=invocation.goal_contract_json,
            turn_id=invocation.turn_id,
            goal_contract_version=invocation.goal_contract_version,
            validation_report_ref=invocation.validation_report_ref,
            open_issues=invocation.open_issues,
        )
        try:
            review = self._invoke(prompt)
        except WorkspaceRaceError:
            raise
        except Exception as exc:  # pragma: no cover - 由调用端具体异常覆盖
            raise ReviewRuntimeError("Reviewer 结构化调用失败") from exc
        if self._fingerprint() != before:
            raise ReviewRuntimeError("Reviewer 只读调用修改了工作空间")
        if (
            review.agent is not invocation.reviewer
            or review.turn_id != invocation.turn_id
            or review.head_sha != invocation.head_sha
            or review.goal_contract_version != invocation.goal_contract_version
        ):
            raise ReviewRuntimeError(
                "ReviewResult 未绑定当前 Reviewer、HEAD 或 GoalContract"
            )
        expected_issue_ids = {item["issue_id"] for item in invocation.open_issues}
        disposition_issue_ids = {
            item.issue_id for item in review.issue_dispositions
        }
        if disposition_issue_ids != expected_issue_ids:
            raise ReviewRuntimeError("ReviewResult 未完整处置调用上下文中的 OPEN Issue")
        try:
            self._policy.validate_review(review)
            review_ref = self._persist(review)
        except Exception as exc:  # ReviewPolicy/ArtifactError 均转为稳定领域错误
            raise ReviewRuntimeError("ReviewResult 证据校验或持久化失败") from exc
        return review, review_ref
