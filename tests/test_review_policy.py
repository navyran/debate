"""EvidenceResolver 与 ReviewPolicy 的机器证据校验测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from git_helpers import create_repo, git

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import RunConfig
from debate_agent.git_workspace import GitWorkspace
from debate_agent.models import AgentName, CompletionAssessment, Decision
from debate_agent.policy import CommandPolicy
from debate_agent.process import ProcessRunner
from debate_agent.review_models import (
    EvidenceKind,
    EvidenceRef,
    ReviewCoverage,
    ReviewCoverageStatus,
    ReviewDimension,
    ReviewResult,
)
from debate_agent.review_policy import EvidenceResolver, ReviewPolicy, ReviewPolicyError
from debate_agent.validation import ValidationPhase, ValidationRunner


def build_context(tmp_path: Path) -> tuple[GitWorkspace, ArtifactStore, str]:
    """构造当前 HEAD 和权威验证产物；参数为临时目录，返回工作区/Store/HEAD。"""
    repo = create_repo(tmp_path)
    workspace = GitWorkspace.preflight(
        repo,
        target_branch="main",
        base_ref="HEAD",
        state_root=tmp_path / "state",
        run_id="RUN-REVIEW",
    )
    workspace.acquire_lock()
    workspace.create_worktree()
    source = workspace.worktree_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git(workspace.worktree_path, "add", "src/service.py")
    git(workspace.worktree_path, "commit", "-m", "add service")
    head = workspace.head()
    artifacts = ArtifactStore.create(
        RunConfig(
            workspace=repo,
            topic="验证 Reviewer 机器证据",
            state_root=tmp_path / "state",
        ),
        run_id="RUN-REVIEW",
    )
    runner = ValidationRunner(
        state_root=tmp_path / "state",
        run_id="RUN-REVIEW",
        process_runner=ProcessRunner(raw_root=artifacts.run_dir / "raw"),
        command_policy=CommandPolicy(approved_commands=[(sys.executable, "-c")]),
        artifact_store=artifacts,
    )
    runner.run(
        report_id="TURN-1",
        phase=ValidationPhase.CHECKPOINT,
        commands=[[sys.executable, "-c", "print('ok')"]],
        cwd=workspace.worktree_path,
        env={},
        head_sha=head,
    )
    return workspace, artifacts, head


def reference(
    kind: EvidenceKind,
    head_sha: str,
    *,
    artifact_ref: str | None = None,
    path: str | None = None,
) -> EvidenceRef:
    """构造协议证据；参数为类型/HEAD/产物或路径，返回 EvidenceRef。"""
    return EvidenceRef(
        kind=kind,
        head_sha=head_sha,
        artifact_ref=artifact_ref,
        path=path,
        locator=None,
    )


def test_resolver_accepts_current_git_path_diff_report_and_test_result(
    tmp_path: Path,
) -> None:
    """验证四类当前 Supervisor 证据可解析；参数为临时目录，无返回值。"""
    workspace, artifacts, head = build_context(tmp_path)
    resolver = EvidenceResolver(workspace=workspace, artifacts=artifacts)
    report_ref = "validation/TURN-1.json"
    raw_ref = "raw/VAL-checkpoint-TURN-1-1.json"

    resolved = [
        resolver.resolve(reference(EvidenceKind.GIT_PATH, head, path="src/service.py")),
        resolver.resolve(reference(EvidenceKind.GIT_PATH, head, path="src")),
        resolver.resolve(reference(EvidenceKind.GIT_DIFF, head, path="src/service.py")),
        resolver.resolve(
            reference(EvidenceKind.VALIDATION_REPORT, head, artifact_ref=report_ref)
        ),
        resolver.resolve(
            reference(EvidenceKind.TEST_RESULT, head, artifact_ref=raw_ref)
        ),
    ]

    assert [item.source_ref for item in resolved] == [
        "src/service.py",
        "src",
        "src/service.py",
        report_ref,
        report_ref,
    ]


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        (EvidenceKind.GIT_PATH, "/tmp/escape.py"),
        (EvidenceKind.GIT_PATH, "../escape.py"),
        (EvidenceKind.GIT_PATH, "missing.py"),
        (EvidenceKind.GIT_PATH, "sr"),
        (EvidenceKind.GIT_DIFF, "README.md"),
    ],
)
def test_resolver_rejects_unsafe_or_unresolvable_git_evidence(
    tmp_path: Path, kind: EvidenceKind, path: str
) -> None:
    """验证 Git 证据拒绝越界和不存在引用；参数为临时目录/类型/路径，无返回值。"""
    workspace, artifacts, head = build_context(tmp_path)
    resolver = EvidenceResolver(workspace=workspace, artifacts=artifacts)

    with pytest.raises(ReviewPolicyError):
        resolver.resolve(reference(kind, head, path=path))


def test_resolver_rejects_wrong_head_non_supervisor_and_stale_artifacts(
    tmp_path: Path,
) -> None:
    """验证错误 HEAD、非 Supervisor 与过期报告被拒绝；参数为临时目录，无返回值。"""
    workspace, artifacts, head = build_context(tmp_path)
    run_dir = artifacts.run_dir
    resolver = EvidenceResolver(workspace=workspace, artifacts=artifacts)
    (run_dir / "updates").mkdir(exist_ok=True)
    (run_dir / "updates" / "peer.json").write_text("{}", encoding="utf-8")
    stale = run_dir / "validation" / "STALE.json"
    stale.write_text(
        (run_dir / "validation" / "TURN-1.json")
        .read_text(encoding="utf-8")
        .replace("TURN-1", "STALE")
        .replace(head, "old-head"),
        encoding="utf-8",
    )

    with pytest.raises(ReviewPolicyError, match="当前 HEAD"):
        resolver.resolve(
            reference(EvidenceKind.GIT_PATH, "wrong-head", path="src/service.py")
        )
    with pytest.raises(ReviewPolicyError, match="validation/"):
        resolver.resolve(
            reference(
                EvidenceKind.VALIDATION_REPORT, head, artifact_ref="updates/peer.json"
            )
        )
    for unsafe_ref in ("/tmp/report.json", "../validation/TURN-1.json"):
        with pytest.raises(ReviewPolicyError, match="安全相对路径"):
            resolver.resolve(
                reference(
                    EvidenceKind.VALIDATION_REPORT,
                    head,
                    artifact_ref=unsafe_ref,
                )
            )
    with pytest.raises(ReviewPolicyError, match="过期"):
        resolver.resolve(
            reference(
                EvidenceKind.VALIDATION_REPORT,
                head,
                artifact_ref="validation/STALE.json",
            )
        )
    with pytest.raises(ReviewPolicyError, match="Supervisor"):
        resolver.resolve(
            reference(EvidenceKind.TEST_RESULT, head, artifact_ref="updates/peer.json")
        )


def test_resolver_rejects_forged_supervisor_files_and_cross_run_store(
    tmp_path: Path,
) -> None:
    """验证 Agent 伪造产物和其他 run Store 不可成为证据；参数为临时目录，无返回值。"""
    workspace, artifacts, head = build_context(tmp_path)
    forged = artifacts.run_dir / "validation" / "FORGED.json"
    forged.write_text(
        (artifacts.run_dir / "validation" / "TURN-1.json")
        .read_text(encoding="utf-8")
        .replace("TURN-1", "FORGED"),
        encoding="utf-8",
    )
    resolver = EvidenceResolver(workspace=workspace, artifacts=artifacts)

    with pytest.raises(ReviewPolicyError, match="可信 Supervisor"):
        resolver.resolve(
            reference(
                EvidenceKind.VALIDATION_REPORT,
                head,
                artifact_ref="validation/FORGED.json",
            )
        )

    other = ArtifactStore.create(
        RunConfig(
            workspace=workspace.repo_root,
            topic="另一运行",
            state_root=workspace.state_root,
        ),
        run_id="RUN-OTHER",
    )
    with pytest.raises(ReviewPolicyError, match="不属于当前工作区 run"):
        EvidenceResolver(workspace=workspace, artifacts=other)


def test_review_policy_only_validates_review_evidence_not_business_claims(
    tmp_path: Path,
) -> None:
    """验证策略仅解析证据且不裁决 Claim；参数为临时目录，无返回值。"""
    workspace, artifacts, head = build_context(tmp_path)
    git_evidence = reference(EvidenceKind.GIT_PATH, head, path="src/service.py")
    report_evidence = reference(
        EvidenceKind.VALIDATION_REPORT,
        head,
        artifact_ref="validation/TURN-1.json",
    )
    review = ReviewResult(
        agent=AgentName.CLAUDE,
        turn_id="REVIEW-1",
        head_sha=head,
        goal_contract_version=1,
        protocol_version="review-v1",
        coverage=[
            ReviewCoverage(
                dimension=dimension,
                status=ReviewCoverageStatus.PASS,
                summary="业务 Claim 可以由后续 Reviewer 解释",
                evidence_refs=(
                    [report_evidence]
                    if dimension is ReviewDimension.VALIDATION
                    else [git_evidence]
                    if dimension is ReviewDimension.GOAL_SCOPE
                    else []
                ),
            )
            for dimension in ReviewDimension
        ],
        findings=[],
        issue_dispositions=[],
        strongest_counterexample="最强反例已在审查中记录",
        counterexample_resolution="证据解析不评价该处理的正确性",
        completion_assessment=CompletionAssessment.COMPLETE,
        decision=Decision.ACCEPT,
        summary="仅校验引用来源和提交绑定",
    )

    resolved = ReviewPolicy(
        resolver=EvidenceResolver(workspace=workspace, artifacts=artifacts)
    ).validate_review(review)

    assert [item.source_ref for item in resolved] == [
        "src/service.py",
        "validation/TURN-1.json",
    ]
