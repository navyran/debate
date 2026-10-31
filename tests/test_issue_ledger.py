from __future__ import annotations

import json
from pathlib import Path

import pytest

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import RunConfig
from debate_agent.issue_ledger import (
    IssueLedgerError,
    IssueLedgerStore,
    IssueStatus,
)
from debate_agent.models import (
    AgentName,
    CompletionAssessment,
    Decision,
    ReviewProtocolVersion,
)
from debate_agent.review_models import (
    ReviewDimension,
    ReviewIssueDisposition,
    ReviewIssueDispositionStatus,
    ReviewResult,
)


def enabled_store(tmp_path: Path) -> IssueLedgerStore:
    """创建已显式开启 review-v1 Ledger 的测试 Store；参数为临时目录，返回 Store。"""
    config = RunConfig(
        workspace=tmp_path,
        topic="ledger goal",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        issue_ledger_enabled=True,
    )
    return IssueLedgerStore(artifacts=ArtifactStore.create(config, run_id="RUN-LEDGER"))


def review(*, agent: AgentName = AgentName.CLAUDE) -> ReviewResult:
    """构建带阻断 Finding 的最小 Review；参数为 Reviewer，返回 ReviewResult。"""
    return ReviewResult.model_validate(
        {
            "agent": agent.value,
            "turn_id": "REVIEW-TURN-1",
            "head_sha": "abc",
            "goal_contract_version": 1,
            "protocol_version": "review-v1",
            "coverage": [
                {
                    "dimension": dimension.value,
                    "status": (
                        "challenge"
                        if dimension is ReviewDimension.CORRECTNESS
                        else "pass"
                    ),
                    "summary": f"检查 {dimension.value}",
                    "evidence_refs": [],
                }
                for dimension in ReviewDimension
            ],
            "findings": [
                {
                    "finding_id": "FIND-1",
                    "dimension": "correctness",
                    "claim": "边界输入仍会失败",
                    "impact": "目标无法完成",
                    "blocking": True,
                    "evidence_refs": [
                        {
                            "kind": "git_path",
                            "head_sha": "abc",
                            "artifact_ref": None,
                            "path": "src/app.py",
                            "locator": "run",
                        }
                    ],
                    "proposed_change": "处理边界输入",
                }
            ],
            "issue_dispositions": [],
            "strongest_counterexample": "空输入",
            "counterexample_resolution": None,
            "completion_assessment": CompletionAssessment.INCOMPLETE.value,
            "decision": Decision.REVISE.value,
            "summary": "发现阻断问题",
        }
    )


def test_register_review_is_idempotent_and_derives_open_blocker(tmp_path: Path) -> None:
    """验证阻断 Finding 单写幂等登记；参数为临时目录，无返回值。"""
    store = enabled_store(tmp_path)

    ledger, reference = store.register_review(review())
    repeated, repeated_reference = store.register_review(review())

    assert reference == "issue-ledger/ledger.json"
    assert repeated_reference == reference
    assert repeated == ledger
    assert len(ledger.issues) == len(ledger.transitions) == 1
    assert ledger.issues[0].status is IssueStatus.OPEN
    assert [issue.issue_id for issue in store.blocking_projection()] == [
        ledger.issues[0].issue_id
    ]


def test_writer_can_only_record_address_and_non_writer_resolves_with_evidence(
    tmp_path: Path,
) -> None:
    """验证 Writer 不能自证关闭，非 Writer 需当前 HEAD 证据；参数为临时目录。"""
    store = enabled_store(tmp_path)
    issue = store.register_review(review())[0].issues[0]
    addressed = store.record_addressed(
        issue_id=issue.issue_id,
        writer=AgentName.CODEX,
        commit_sha="def",
        current_head="def",
    )[0].issues[0]
    evidence = tuple(
        reference.model_copy(update={"head_sha": "def"})
        for reference in issue.evidence_refs
    )

    with pytest.raises(IssueLedgerError, match="Writer"):
        store.close_with_reviewer_evidence(
            issue_id=issue.issue_id,
            reviewer=AgentName.CODEX,
            head_sha="def",
            status=IssueStatus.RESOLVED,
            evidence_refs=evidence,
            current_head="def",
            validated_evidence_refs=evidence,
        )
    with pytest.raises(IssueLedgerError, match="当前 HEAD"):
        store.close_with_reviewer_evidence(
            issue_id=issue.issue_id,
            reviewer=AgentName.QODER,
            head_sha="def",
            status=IssueStatus.RESOLVED,
            evidence_refs=issue.evidence_refs,
            current_head="def",
            validated_evidence_refs=evidence,
        )

    resolved = store.close_with_reviewer_evidence(
        issue_id=issue.issue_id,
        reviewer=AgentName.QODER,
        head_sha="def",
        status=IssueStatus.RESOLVED,
        evidence_refs=evidence,
        current_head="def",
        validated_evidence_refs=evidence,
    )[0].issues[0]
    repeated = store.close_with_reviewer_evidence(
        issue_id=issue.issue_id,
        reviewer=AgentName.QODER,
        head_sha="def",
        status=IssueStatus.RESOLVED,
        evidence_refs=evidence,
        current_head="def",
        validated_evidence_refs=evidence,
    )[0].issues[0]

    assert addressed.status is IssueStatus.OPEN
    assert resolved.status is IssueStatus.RESOLVED
    assert resolved.resolved_by is AgentName.QODER
    assert repeated == resolved


def test_writer_can_record_later_open_checkpoint_before_independent_resolution(
    tmp_path: Path,
) -> None:
    """验证 OPEN Issue 保留多次 Writer 尝试并以最新提交供 Reviewer 复核。"""
    store = enabled_store(tmp_path)
    issue = store.register_review(review())[0].issues[0]
    first = store.record_addressed(
        issue_id=issue.issue_id,
        writer=AgentName.CODEX,
        commit_sha="def",
        current_head="def",
    )[0]
    second = store.record_addressed(
        issue_id=issue.issue_id,
        writer=AgentName.CLAUDE,
        commit_sha="ghi",
        current_head="ghi",
    )[0]
    evidence = tuple(
        reference.model_copy(update={"head_sha": "ghi"})
        for reference in issue.evidence_refs
    )
    resolved = store.close_with_reviewer_evidence(
        issue_id=issue.issue_id,
        reviewer=AgentName.QODER,
        head_sha="ghi",
        status=IssueStatus.RESOLVED,
        evidence_refs=evidence,
        current_head="ghi",
        validated_evidence_refs=evidence,
    )[0]

    assert first.issues[0].addressed_by_commit == "def"
    assert second.issues[0].addressed_by_commit == "ghi"
    assert [item.head_sha for item in second.transitions] == ["abc", "def", "ghi"]
    assert resolved.issues[0].status is IssueStatus.RESOLVED
    assert resolved.issues[0].addressed_by_commit == "ghi"


def test_duplicate_relation_never_inherits_closed_status(tmp_path: Path) -> None:
    """验证 duplicate_of 仅关联关系；参数为临时目录，无返回值。"""
    store = enabled_store(tmp_path)
    first = store.register_review(review())[0].issues[0]
    second_review = review(agent=AgentName.QODER).model_copy(
        update={
            "turn_id": "REVIEW-TURN-2",
            "findings": [
                review(agent=AgentName.QODER)
                .findings[0]
                .model_copy(update={"finding_id": "FIND-2"})
            ],
        }
    )
    second = store.register_review(second_review)[0].issues[1]
    evidence = tuple(
        reference.model_copy(update={"head_sha": "def"})
        for reference in first.evidence_refs
    )
    store.close_with_reviewer_evidence(
        issue_id=first.issue_id,
        reviewer=AgentName.QODER,
        head_sha="def",
        status=IssueStatus.DISMISSED,
        evidence_refs=evidence,
        current_head="def",
        validated_evidence_refs=evidence,
    )

    ledger = store.set_duplicate_of(
        issue_id=second.issue_id, duplicate_of=first.issue_id
    )[0]
    duplicate = next(
        issue for issue in ledger.issues if issue.issue_id == second.issue_id
    )

    assert duplicate.status is IssueStatus.OPEN
    assert duplicate.duplicate_of == first.issue_id


def test_review_disposition_requires_policy_validated_evidence_and_replays(
    tmp_path: Path,
) -> None:
    """验证 Reviewer 处置不接受自述证据且可按同一 Review 幂等重放。"""
    store = enabled_store(tmp_path)
    issue = store.register_review(review())[0].issues[0]
    store.record_addressed(
        issue_id=issue.issue_id,
        writer=AgentName.CODEX,
        commit_sha="def",
        current_head="def",
    )
    evidence = tuple(
        reference.model_copy(update={"head_sha": "def"})
        for reference in issue.evidence_refs
    )
    closing_review = review(agent=AgentName.QODER).model_copy(
        update={
            "head_sha": "def",
            "findings": [],
            "issue_dispositions": [
                ReviewIssueDisposition(
                    issue_id=issue.issue_id,
                    status=ReviewIssueDispositionStatus.RESOLVED,
                    rationale="当前 HEAD 的处理已由独立 Reviewer 复核。",
                    evidence_refs=list(evidence),
                )
            ],
        }
    )

    with pytest.raises(IssueLedgerError, match="ReviewPolicy"):
        store.apply_review_dispositions(
            review=closing_review,
            current_head="def",
            validated_evidence_refs=(),
        )

    applied = store.apply_review_dispositions(
        review=closing_review,
        current_head="def",
        validated_evidence_refs=evidence,
    )[0]
    replayed = store.apply_review_dispositions(
        review=closing_review,
        current_head="def",
        validated_evidence_refs=evidence,
    )[0]

    assert applied == replayed
    assert applied.issues[0].status is IssueStatus.RESOLVED


def test_review_replay_ignores_its_own_newly_opened_finding(
    tmp_path: Path,
) -> None:
    """验证持久化 Review 重放时不要求处置该 Review 自己新开的 Issue。"""
    store = enabled_store(tmp_path)
    existing = store.register_review(review())[0].issues[0]
    store.record_addressed(
        issue_id=existing.issue_id,
        writer=AgentName.CODEX,
        commit_sha="def",
        current_head="def",
    )
    closing_evidence = tuple(
        reference.model_copy(update={"head_sha": "def"})
        for reference in existing.evidence_refs
    )
    new_finding = review(agent=AgentName.QODER).findings[0].model_copy(
        update={
            "finding_id": "FIND-NEW",
            "evidence_refs": list(closing_evidence),
        }
    )
    replay = review(agent=AgentName.QODER).model_copy(
        update={
            "head_sha": "def",
            "findings": [new_finding],
            "issue_dispositions": [
                ReviewIssueDisposition(
                    issue_id=existing.issue_id,
                    status=ReviewIssueDispositionStatus.RESOLVED,
                    rationale="当前 HEAD 的既有问题已由独立证据复核。",
                    evidence_refs=list(closing_evidence),
                )
            ],
        }
    )

    store.apply_review_dispositions(
        review=replay,
        current_head="def",
        validated_evidence_refs=closing_evidence,
    )
    store.register_review(replay, current_head="def")
    replayed = store.apply_review_dispositions(
        review=replay,
        current_head="def",
        validated_evidence_refs=closing_evidence,
    )[0]

    assert [issue.status for issue in replayed.issues] == [
        IssueStatus.RESOLVED,
        IssueStatus.OPEN,
    ]


def test_disabled_legacy_and_corrupt_ledger_stop_instead_of_drifting(
    tmp_path: Path,
) -> None:
    """验证默认关闭、旧协议和损坏 Ledger 均不静默恢复；参数为临时目录。"""
    disabled = RunConfig(
        workspace=tmp_path, topic="disabled", state_root=tmp_path / "off"
    )
    disabled_store = IssueLedgerStore(
        artifacts=ArtifactStore.create(disabled, run_id="RUN-DISABLED")
    )
    with pytest.raises(IssueLedgerError, match="legacy-v0"):
        disabled_store.load()

    store = enabled_store(tmp_path)
    store.artifacts.issue_ledger_path.parent.mkdir(exist_ok=True)
    store.artifacts.issue_ledger_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(IssueLedgerError, match="损坏"):
        store.load()
    diagnostic = json.loads(
        (store.artifacts.run_dir / "diagnostic.json").read_text(encoding="utf-8")
    )
    assert diagnostic["phase"] == "issue_ledger_recovery"
