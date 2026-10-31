"""test_adversarial_review_phase_2_benchmark.py 按职责拆分后的测试集合。"""

import json
from pathlib import Path

import pytest
from phase2_benchmark_support import (
    REPORT,
    REQUIRED_CONTROLS,
    _assert_phase_one_pairing,
    _assert_quality_not_worse_than_phase_one,
    _build_final_validation_runtime,
    _create_fixed_baseline_repo,
    _load,
    _persisted_ballot_rounds,
    _phase_one_four_lens_quality,
    _phase_two_quality,
    _run_fixed_review,
    _run_full_lifecycle,
)

from debate_agent.git_workspace import GitWorkspace, WorkspaceRaceError
from debate_agent.models import (
    AgentName,
    CompletionAssessment,
    ReviewProtocolVersion,
    RunStatus,
)
from debate_agent.review_models import (
    EvidenceKind,
    EvidenceRef,
    ReviewDimension,
)
from debate_agent.review_policy import EvidenceResolver, ReviewPolicyError


def test_phase_two_fixed_benchmark_satisfies_all_go_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证三次真实 Runtime 结构化运行、证据/故障门禁和 Phase 1 质量下限。"""
    controls = _load("controls.json")
    assert isinstance(controls, dict)
    assert set(controls) == REQUIRED_CONTROLS
    assert controls["protocol_version"] == "review-v1"
    assert controls["repetitions"] == 3
    phase_one = _assert_phase_one_pairing(controls)
    phase_one_tasks = phase_one["tasks"]
    assert isinstance(phase_one_tasks, list)
    tasks = _load("tasks.json")
    assert isinstance(tasks, list)
    assert [task["id"] for task in tasks] == [
        task["id"] for task in phase_one_tasks
    ]
    baseline_repo = _create_fixed_baseline_repo(
        root=tmp_path, expected_head=str(controls["initial_commit_sha"])
    )
    actual_runs = [
        _run_fixed_review(
            tmp_path=tmp_path,
            baseline_repo=baseline_repo,
            ordinal=repeat * len(tasks) + index + 1,
            task=task,
        )
        for repeat in range(int(controls["repetitions"]))
        for index, task in enumerate(tasks)
    ]
    reviews = [item[0] for item in actual_runs]
    assert all(
        review.protocol_version is ReviewProtocolVersion.REVIEW_V1
        for review in reviews
    )
    assert all(len(review.coverage) == len(ReviewDimension) for review in reviews)
    assert all(
        any(
            name == "ReviewResult" and read_only
            for name, read_only, _ in backend.calls
        )
        for _, _, backend, _ in actual_runs
    )
    for review, artifacts, _backend, _runtime in actual_runs:
        validation_refs = [
            ref.artifact_ref
            for coverage in review.coverage
            for ref in coverage.evidence_refs
            if ref.kind is EvidenceKind.VALIDATION_REPORT
        ]
        assert len(validation_refs) == 1
        report_path = artifacts.run_dir / str(validation_refs[0])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["head_sha"] == review.head_sha
        assert str(validation_refs[0]).startswith("validation/REVIEW-HEAD-")
        workspace = GitWorkspace.from_record(
            artifacts.read_repo(),
            expected_state_root=artifacts.state_root,
            expected_run_id=artifacts.run_id,
        )
        resolver = EvidenceResolver(workspace=workspace, artifacts=artifacts)
        with pytest.raises(ReviewPolicyError):
            resolver.resolve(
                EvidenceRef(
                    kind=EvidenceKind.GIT_PATH,
                    head_sha=review.head_sha,
                    artifact_ref=None,
                    path="../forged.py",
                    locator=None,
                )
            )
    first_review, first_artifacts, _, first_runtime = actual_runs[0]
    first_validation_ref = next(
        ref.artifact_ref
        for coverage in first_review.coverage
        for ref in coverage.evidence_refs
        if ref.kind is EvidenceKind.VALIDATION_REPORT
    )
    assert first_validation_ref is not None
    injected_report_path = first_artifacts.run_dir / first_validation_ref
    injected_report = json.loads(injected_report_path.read_text(encoding="utf-8"))
    injected_report["head_sha"] = "injected-wrong-head"
    injected_report_path.write_text(
        json.dumps(injected_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    injected_workspace = GitWorkspace.from_record(
        first_artifacts.read_repo(),
        expected_state_root=first_artifacts.state_root,
        expected_run_id=first_artifacts.run_id,
    )
    with pytest.raises(ReviewPolicyError, match="过期或未绑定当前 HEAD"):
        EvidenceResolver(
            workspace=injected_workspace, artifacts=first_artifacts
        ).resolve(
            EvidenceRef(
                kind=EvidenceKind.VALIDATION_REPORT,
                head_sha=first_review.head_sha,
                artifact_ref=first_validation_ref,
                path=None,
                locator=None,
            )
        )
    recovery_workspace = GitWorkspace.from_record(
        first_artifacts.read_repo(),
        expected_state_root=first_artifacts.state_root,
        expected_run_id=first_artifacts.run_id,
    )
    assert first_runtime.supervisor is not None
    lease = first_runtime.supervisor.grant_write_lease(
        agent=AgentName.CLAUDE,
        turn_id="TURN-RECOVER",
        base_commit=recovery_workspace.head(),
        goal_contract_version=1,
    )
    first_artifacts.prepare_invocation(
        invocation_id="INV-RECOVER",
        agent=lease.agent,
        turn_id=lease.turn_id,
        base_commit=lease.base_commit,
        goal_contract_version=lease.goal_contract_version,
        prompt_hash="sha256:fixed-recovery",
    )
    (recovery_workspace.worktree_path / "result.txt").write_text(
        "checkpoint after recovery\n", encoding="utf-8"
    )
    recovery_update = first_runtime._node_recover_run({})
    recovery_report_ref = recovery_update["validation_report_ref"]
    assert isinstance(recovery_report_ref, str)
    recovery_report = json.loads(
        (first_artifacts.run_dir / recovery_report_ref).read_text(encoding="utf-8")
    )
    assert recovery_report["head_sha"] == recovery_workspace.head()
    recovery_resolver = EvidenceResolver(
        workspace=recovery_workspace, artifacts=first_artifacts
    )
    with pytest.raises(ReviewPolicyError, match="当前 HEAD"):
        recovery_resolver.resolve(first_review.coverage[0].evidence_refs[0])
    with pytest.raises(WorkspaceRaceError):
        _run_fixed_review(
            tmp_path=tmp_path,
            baseline_repo=baseline_repo,
            ordinal=999,
            mutate_review=True,
        )
    lifecycle_runs = [
        _run_full_lifecycle(
            tmp_path=tmp_path, baseline_repo=baseline_repo, ordinal=index
        )
        for index in range(1, 4)
    ]
    assert all(
        result["status"] == RunStatus.COMPLETED.value
        for result, _, _ in lifecycle_runs
    )
    ballot_overrides = [
        (backend, artifacts)
        for _, backend, artifacts in lifecycle_runs
        if CompletionAssessment.INCOMPLETE in backend.ballot_outcomes
    ]
    assert len(ballot_overrides) == 2
    for backend, artifacts in ballot_overrides:
        rounds = _persisted_ballot_rounds(artifacts)
        assert len(rounds) == 2
        first_round = rounds[0]
        assert {
            ballot.completion for ballot in first_round.values()
        } == {CompletionAssessment.COMPLETE, CompletionAssessment.INCOMPLETE}
        rejected = next(
            ballot
            for ballot in first_round.values()
            if ballot.completion is CompletionAssessment.INCOMPLETE
        )
        assert rejected.rationale.startswith("反例揭示遗漏验收；Evidence: ")
        evidence_ref = rejected.rationale.removeprefix(
            "反例揭示遗漏验收；Evidence: "
        )
        evidence = json.loads((artifacts.run_dir / evidence_ref).read_text("utf-8"))
        assert evidence["head_sha"] == rejected.commit_sha
        second_round = rounds[1]
        assert all(
            ballot.completion is CompletionAssessment.COMPLETE
            for ballot in second_round.values()
        )
        assert backend.ballot_outcomes == [
            CompletionAssessment.COMPLETE,
            CompletionAssessment.INCOMPLETE,
            CompletionAssessment.COMPLETE,
            CompletionAssessment.COMPLETE,
        ]
    complete_only_backend, complete_only_artifacts = lifecycle_runs[2][1:]
    complete_only_rounds = _persisted_ballot_rounds(complete_only_artifacts)
    assert len(complete_only_rounds) == 1
    assert all(
        ballot.completion is CompletionAssessment.COMPLETE
        for ballot in complete_only_rounds[0].values()
    )
    assert complete_only_backend.ballot_outcomes == [
        CompletionAssessment.COMPLETE,
        CompletionAssessment.COMPLETE,
    ]
    final_runtime, final_artifacts, final_workspace = _build_final_validation_runtime(
        tmp_path=tmp_path,
        baseline_repo=baseline_repo,
        label="workspace-mutation",
    )
    before_final_head = final_workspace.head()
    with pytest.raises(WorkspaceRaceError, match="最终权威验证命令修改"):
        final_runtime._node_run_final_validation({"current_turn": 0})
    final_report = json.loads(
        (
            final_artifacts.run_dir / "validation" / "FINAL-0.json"
        ).read_text(encoding="utf-8")
    )
    assert final_report["head_sha"] == before_final_head
    assert final_workspace.head() != before_final_head
    wrong_head_runtime, _wrong_head_artifacts, wrong_head_workspace = (
        _build_final_validation_runtime(
            tmp_path=tmp_path,
            baseline_repo=baseline_repo,
            label="wrong-head",
            validation_commands=tuple(
                tuple(command) for command in controls["validation_commands"]
            ),
        )
    )
    assert wrong_head_runtime.validation_runner is not None
    validation_runner = wrong_head_runtime.validation_runner
    original_run = validation_runner.run
    wrong_head_before = wrong_head_workspace.head()

    def return_wrong_head(**kwargs: object):
        """注入错误报告绑定；参数为验证器关键字参数，返回错误 HEAD 的报告。"""
        report = original_run(**kwargs)
        return report.model_copy(update={"head_sha": "injected-wrong-head"})

    monkeypatch.setattr(validation_runner, "run", return_wrong_head)
    with pytest.raises(RuntimeError, match="最终验证报告未绑定实际 current HEAD"):
        wrong_head_runtime._node_run_final_validation({"current_turn": 0})
    assert wrong_head_workspace.head() == wrong_head_before
    phase_one_quality = _phase_one_four_lens_quality(phase_one_tasks)
    phase_two_quality_runs = [
        _phase_two_quality(
            tasks=tasks,
            reviews=reviews[repeat * len(tasks) : (repeat + 1) * len(tasks)],
        )
        for repeat in range(int(controls["repetitions"]))
    ]
    assert len(phase_two_quality_runs) == int(controls["repetitions"])
    for phase_two_quality in phase_two_quality_runs:
        _assert_quality_not_worse_than_phase_one(
            phase_one=phase_one_quality, phase_two=phase_two_quality
        )
    fingerprints = [
        finding.finding_id
        for review in reviews
        for finding in review.findings
    ]
    repeated = sum(name == "FIND-RESOLVED-0001" for name in fingerprints)
    assert repeated >= 6
    assert repeated / len(fingerprints) >= 0.25

def test_phase_two_report_records_controls_and_gate_decisions() -> None:
    """验证报告明确记录固定控制变量、Go 结论和后续门禁事实。"""
    content = REPORT.read_text(encoding="utf-8")
    for fragment in (
        "**判定：Go。**",
        "固定控制变量",
        "phase2-1",
        "phase2-2",
        "phase2-3",
        "Ledger 门禁：满足",
        "Ballot 门禁：满足",
        "结构化解析",
        "只读故障注入",
        "恢复故障注入",
        "SQLite checkpoint",
        "初始 SHA 由与 Phase 1 相同的确定性 Git 基线实际",
    ):
        assert fragment in content
