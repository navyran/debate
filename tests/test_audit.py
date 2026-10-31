from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from git_helpers import create_repo, git

import debate_agent.audit as audit_module
from debate_agent.agent_catalog import agent_definition
from debate_agent.audit import (
    AgentAuditMetadata,
    AuditBundleInput,
    AuditBundleWriter,
    DecisionRecord,
    FinalTreeCandidate,
    FinalTreeCoordinator,
    FinalTreeError,
)
from debate_agent.git_workspace import GitWorkspace
from debate_agent.goal_contract import GoalContract
from debate_agent.models import (
    AgentName,
    Ballot,
    ChangeSummary,
    CompletionAssessment,
    Decision,
    FacilitatorAction,
    FacilitatorDecision,
    FacilitatorMode,
    FacilitatorPurpose,
    FacilitatorRecord,
    FacilitatorStatus,
    TreeAck,
)
from debate_agent.policy import WritePolicy
from debate_agent.supervisor import Supervisor
from debate_agent.validation import (
    ValidationCommandResult,
    ValidationPhase,
    ValidationReport,
)


def contract() -> GoalContract:
    return GoalContract(
        version=2,
        objective="完成目标 /Users/private/workspace",
        deliverables=("src/app.py",),
        acceptance_criteria=("tests pass",),
        baseline_validation_commands=(("python", "-m", "pytest"),),
        added_validation_commands=(("ruff", "check"),),
        allowed_scope=("src",),
        protected_scope=(".git",),
        derived_scope=("tests",),
        immutable_constraints=("不得 push",),
    )


def summary() -> ChangeSummary:
    return ChangeSummary(
        update_id="UPD-1",
        source_agent=AgentName.CODEX,
        turn_id="TURN-1",
        parent_commit="abc",
        commit_sha="def",
        goal_contract_version=2,
        changed_paths=["src/app.py"],
        behavior_summary=["增加功能"],
        opened_issues=[],
        resolved_issues=["ISSUE-1"],
        validation_status="passed",
        validation_report_ref="validation/TURN-1.json",
        requested_attention=[],
    )


def validation_report() -> ValidationReport:
    return ValidationReport(
        report_id="FINAL",
        phase=ValidationPhase.FINAL,
        passed=True,
        commands=(
            ValidationCommandResult(
                argv=("/Users/test/.venv/bin/python", "-m", "pytest"),
                return_code=0,
                elapsed_seconds=1.2,
                stdout_summary="TOKEN=top-secret\n72 passed",
                stderr_summary="",
                raw_output_ref="raw/FINAL.json",
                timed_out=False,
            ),
        ),
        report_ref="validation/FINAL.json",
    )


def ballots(commit_sha: str) -> dict[AgentName, Ballot]:
    return {
        agent: Ballot(
            agent=agent,
            commit_sha=commit_sha,
            goal_contract_version=2,
            completion=CompletionAssessment.COMPLETE,
            criteria=[{"criterion": "tests", "satisfied": True}],
            rationale=f"{agent.value} confirms completion",
        )
        for agent in (AgentName.CODEX, AgentName.CLAUDE)
    }


def audit_input(commit_sha: str = "def") -> AuditBundleInput:
    facilitator_decision = FacilitatorDecision(
        decision_id="FAC-1",
        purpose=FacilitatorPurpose.TURN_ROUTING,
        head_sha=commit_sha,
        goal_contract_version=2,
        mode=FacilitatorMode.NON_THINKING,
        action=FacilitatorAction.CONTINUE_DEBATE,
        recommended_next_writer=AgentName.CLAUDE,
        next_instruction="处理 TOKEN=private，但不保存 hidden_reasoning",
        unresolved_criteria=["tests"],
        risk_flags=[],
        evidence_refs=["validation/FINAL.json"],
    )
    return AuditBundleInput(
        goal_contract=contract(),
        behavior_changes=("最终行为已完成",),
        contributions={
            AgentName.CODEX: ("实现代码",),
            AgentName.CLAUDE: ("修复 TOKEN=private",),
        },
        change_summaries=(summary(),),
        decisions=(
            DecisionRecord(
                agent=AgentName.CODEX,
                stage="review",
                decision=Decision.ACCEPT,
                rationale="SESSION_ID=secret-session 接受",
                commit_sha=commit_sha,
            ),
        ),
        disputes=("无未解决争议",),
        limitations=("不包含 hidden_reasoning 或 raw_output",),
        validation_reports=(validation_report(),),
        ballots=ballots(commit_sha),
        facilitator_records=(
            FacilitatorRecord(
                decision_id=facilitator_decision.decision_id,
                purpose=facilitator_decision.purpose,
                head_sha=facilitator_decision.head_sha,
                goal_contract_version=facilitator_decision.goal_contract_version,
                mode=facilitator_decision.mode,
                status=FacilitatorStatus.DECISION,
                decision=facilitator_decision,
                deterministic_route="continue:claude",
            ),
        ),
    )


def test_audit_contribution_heading_delegates_display_name_to_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证贡献标题读取 Catalog；参数为目录/补丁夹具，无返回值，写入失败抛错。"""
    original = agent_definition

    def renamed(agent: AgentName):
        definition = original(agent)
        return replace(definition, display_name=f"Catalog {definition.display_name}")

    monkeypatch.setattr(audit_module, "agent_definition", renamed)
    writer = AuditBundleWriter(worktree=tmp_path, run_id="RUN-CATALOG")

    summary = writer._final_summary_markdown(audit_input())

    assert "## Catalog Codex 贡献（检查点）" in summary
    assert "## Catalog Claude 贡献（检查点）" in summary


def test_audit_three_agent_contributions_and_ballots_remain_compatible(
    tmp_path: Path,
) -> None:
    """验证三方审计文本兼容；参数为目录，返回值为空，写入失败抛异常。"""
    data = audit_input().model_copy(
        update={
            "participants": (
                AgentName.CODEX,
                AgentName.CLAUDE,
                AgentName.QODER,
            ),
            "contributions": {
                **audit_input().contributions,
                AgentName.QODER: ("复核实现",),
            },
            "ballots": {
                **audit_input().ballots,
                AgentName.QODER: Ballot(
                    agent=AgentName.QODER,
                    commit_sha="def",
                    goal_contract_version=2,
                    completion=CompletionAssessment.COMPLETE,
                    criteria=[{"criterion": "tests", "satisfied": True}],
                    rationale="qoder confirms completion",
                ),
            },
        }
    )
    writer = AuditBundleWriter(worktree=tmp_path, run_id="RUN-THREE")

    summary = writer._final_summary_markdown(data)

    for display_name in ("Codex", "Claude", "Qoder"):
        assert f"## {display_name} 贡献（检查点）" in summary
    for agent in (AgentName.CODEX, AgentName.CLAUDE, AgentName.QODER):
        assert f"- {agent.value}: complete;" in summary


def test_audit_displays_opt_in_ballot_counterexample_and_resolution(
    tmp_path: Path,
) -> None:
    """验证审计仅在 Ballot 携带反例时显示反例和可核查处理说明。"""
    data = audit_input().model_copy(
        update={
            "ballots": {
                AgentName.CODEX: Ballot(
                    agent=AgentName.CODEX,
                    commit_sha="def",
                    goal_contract_version=2,
                    completion=CompletionAssessment.INCOMPLETE,
                    criteria=[{"criterion": "tests", "satisfied": False}],
                    rationale="发现边界风险",
                    strongest_counterexample="缺少空输入边界测试",
                    counterexample_resolution="validation/FINAL.json 显示尚未覆盖。",
                )
            }
        }
    )
    writer = AuditBundleWriter(worktree=tmp_path, run_id="RUN-COUNTEREXAMPLE")

    summary = writer._final_summary_markdown(data)

    assert "最强反例: 缺少空输入边界测试" in summary
    assert "处理与证据: validation/FINAL.json 显示尚未覆盖。" in summary
    assert "claude: complete" not in summary


def test_audit_records_opencode_safe_frozen_runtime_metadata(
    tmp_path: Path,
) -> None:
    """验证 OpenCode 审计使用目录显示名且只写入安全的冻结摘要。"""
    data = audit_input().model_copy(
        update={
            "participants": (AgentName.CODEX, AgentName.OPENCODE),
            "contributions": {
                AgentName.CODEX: ("实现代码",),
                AgentName.OPENCODE: ("复核并完成实现",),
            },
            "ballots": {
                AgentName.CODEX: ballots("def")[AgentName.CODEX],
                AgentName.OPENCODE: Ballot(
                    agent=AgentName.OPENCODE,
                    commit_sha="def",
                    goal_contract_version=2,
                    completion=CompletionAssessment.COMPLETE,
                    criteria=[{"criterion": "tests", "satisfied": True}],
                    rationale="opencode confirms completion",
                ),
            },
            "agent_runtime_metadata": {
                AgentName.OPENCODE: AgentAuditMetadata(
                    version="1.18.3",
                    model="provider-x/model-y",
                    variant="quality",
                    session_ref="sha256:" + "a" * 64,
                    runtime_config_hash="b" * 64,
                    runtime_config_content_hash="c" * 64,
                    runtime_config_resolved_hash="d" * 64,
                )
            },
        }
    )
    writer = AuditBundleWriter(worktree=tmp_path, run_id="RUN-OPENCODE")

    summary = writer._final_summary_markdown(data)

    assert "## OpenCode 贡献（检查点）" in summary
    assert "version=1.18.3" in summary
    assert "model=provider-x/model-y" in summary
    assert "variant=quality" in summary
    assert "session-ref=sha256:" + "a" * 64 in summary
    assert "runtime-config=" + "b" * 64 in summary
    assert "resolved config" not in summary.casefold()


def test_audit_records_openclaw_profile_transport_and_hashed_session(
    tmp_path: Path,
) -> None:
    """验证 OpenClaw 审计含冻结边界且不含原始 Session；参数为目录，无返回值。"""
    session_ref = "sha256:" + "e" * 64
    data = audit_input().model_copy(
        update={
            "participants": (AgentName.CODEX, AgentName.OPENCLAW),
            "contributions": {
                AgentName.CODEX: ("实现代码",),
                AgentName.OPENCLAW: ("复核实现",),
            },
            "agent_runtime_metadata": {
                AgentName.OPENCLAW: AgentAuditMetadata(
                    version="2026.7.1",
                    command="/opt/openclaw",
                    model="deepseek/deepseek-v4-pro",
                    variant="off",
                    native_profile="main",
                    prompt_transport="temp_file",
                    session_generation=2,
                    preflight_status="passed",
                    session_ref=session_ref,
                )
            },
        }
    )

    summary = AuditBundleWriter(
        worktree=tmp_path,
        run_id="RUN-OPENCLAW",
    )._final_summary_markdown(data)

    assert "## OpenClaw 贡献（检查点）" in summary
    assert "command=<redacted-path>" in summary
    assert "native-profile=main" in summary
    assert "prompt-transport=temp_file" in summary
    assert "session-generation=2" in summary
    assert "preflight=passed" in summary
    assert session_ref in summary
    assert "agent:main:" not in summary


def test_audit_records_hermes_experimental_provider_and_argv_transport(
    tmp_path: Path,
) -> None:
    """验证 Hermes 审计记录实验边界且不记录 Prompt/密钥；参数为目录，无返回值。"""
    session_ref = "sha256:" + "f" * 64
    metadata = AgentAuditMetadata(
        version="0.19.0",
        command="hermes",
        provider="deepseek",
        model="deepseek-v4-flash",
        experimental=True,
        prompt_transport="argv",
        session_generation=1,
        preflight_status="passed",
        session_ref=session_ref,
        runtime_config_hash="a" * 64,
    )
    data = audit_input().model_copy(
        update={
            "participants": (AgentName.CLAUDE, AgentName.HERMES),
            "contributions": {
                AgentName.CLAUDE: ("实现代码",),
                AgentName.HERMES: ("复核实现",),
            },
            "agent_runtime_metadata": {AgentName.HERMES: metadata},
        }
    )

    summary = AuditBundleWriter(
        worktree=tmp_path,
        run_id="RUN-HERMES",
    )._final_summary_markdown(data)

    assert "## Hermes 贡献（检查点）" in summary
    assert "provider=deepseek" in summary
    assert "model=deepseek-v4-flash" in summary
    assert "experimental=true" in summary
    assert "prompt-transport=argv" in summary
    assert session_ref in summary
    assert "DEEPSEEK_API_KEY" not in summary


def build_workspace(tmp_path: Path) -> tuple[Path, GitWorkspace]:
    repo = create_repo(tmp_path)
    workspace = GitWorkspace.preflight(
        repo,
        target_branch="main",
        base_ref="HEAD",
        state_root=tmp_path / "state",
        run_id="RUN-1",
    )
    workspace.acquire_lock()
    workspace.create_worktree()
    return repo, workspace


def commit_product_change(workspace: GitWorkspace) -> str:
    supervisor = Supervisor(state_root=workspace.state_root, run_id=workspace.run_id)
    supervisor.grant_write_lease(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit=workspace.head(),
        goal_contract_version=2,
    )
    source = workspace.worktree_path / "src"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    lease = supervisor.revoke_write_lease("TURN-1")
    snapshot = workspace.snapshot()
    commit = workspace.commit_checkpoint(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        goal_contract_version=2,
        validation_status="passed",
        summary="implement app",
        lease=lease,
        validated_snapshot=snapshot,
        validated_paths=["src/app.py"],
        write_policy=WritePolicy(
            worktree=workspace.worktree_path, allowed_scope=["src"]
        ),
    )
    assert commit is not None
    return commit


def test_audit_bundle_contains_required_files_and_redacts_runtime_secrets(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    writer = AuditBundleWriter(worktree=worktree, run_id="RUN-1")

    audit_path = writer.write_completed(audit_input())

    expected = {
        "goal-contract.md",
        "final-summary.md",
        "change-summary.md",
        "decisions.jsonl",
        "validation-report.md",
        "facilitator-decisions.jsonl",
        "issue-ledger.json",
    }
    assert {path.name for path in audit_path.iterdir()} == expected
    final_summary = (audit_path / "final-summary.md").read_text(encoding="utf-8")
    assert "- Status: ready-for-final-tree-ack" in final_summary
    assert "Final-Tree-Ack trailer" in final_summary
    assert "- Status: completed" not in final_summary
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in audit_path.iterdir()
    )
    assert all(line == line.rstrip() for line in combined.splitlines())
    assert "目标" in combined
    assert "Codex" in combined
    assert "Claude" in combined
    for forbidden in [
        "/Users/",
        "top-secret",
        "secret-session",
        "hidden_reasoning",
        "raw_output",
    ]:
        assert forbidden not in combined
    facilitator_log = (audit_path / "facilitator-decisions.jsonl").read_text(
        encoding="utf-8"
    )
    assert "continue_debate" in facilitator_log
    assert "continue:claude" in facilitator_log


def test_audit_writer_continues_existing_legacy_discuss_directory(
    tmp_path: Path,
) -> None:
    """验证旧运行恢复时继续写入已有 discuss 审计目录。"""
    worktree = tmp_path / "worktree"
    legacy = worktree / ".agent" / "discuss" / "RUN-LEGACY"
    legacy.mkdir(parents=True)

    writer = AuditBundleWriter(worktree=worktree, run_id="RUN-LEGACY")

    assert writer.relative_path == ".agent/discuss/RUN-LEGACY"


def test_non_success_audit_stages_only_audit_not_policy_violation(
    tmp_path: Path,
) -> None:
    _, workspace = build_workspace(tmp_path)
    writer = AuditBundleWriter(worktree=workspace.worktree_path, run_id="RUN-1")
    writer.write_non_success(
        audit_input(), status="policy_violation", stop_reason=".env was modified"
    )
    (workspace.worktree_path / ".env").write_text("TOKEN=bad\n", encoding="utf-8")

    staged = workspace.stage_audit_tree(
        ".agent/debate/RUN-1", require_only_audit=False
    )

    assert staged
    assert git(
        workspace.worktree_path, "diff", "--cached", "--name-only"
    ).splitlines() == [
        ".agent/debate/RUN-1/change-summary.md",
        ".agent/debate/RUN-1/decisions.jsonl",
        ".agent/debate/RUN-1/facilitator-decisions.jsonl",
        ".agent/debate/RUN-1/final-summary.md",
        ".agent/debate/RUN-1/goal-contract.md",
        ".agent/debate/RUN-1/issue-ledger.json",
        ".agent/debate/RUN-1/validation-report.md",
    ]
    assert ".env" in git(
        workspace.worktree_path, "status", "--porcelain", "--untracked-files=all"
    )


def test_final_tree_requires_readonly_double_ack_and_integrates_one_commit(
    tmp_path: Path,
) -> None:
    repo, workspace = build_workspace(tmp_path)
    code_commit = commit_product_change(workspace)
    writer = AuditBundleWriter(worktree=workspace.worktree_path, run_id="RUN-1")
    writer.write_completed(audit_input(code_commit))
    coordinator = FinalTreeCoordinator(workspace)
    candidate = coordinator.materialize_final_tree(".agent/debate/RUN-1")

    acks = coordinator.collect_tree_acks(
        candidate,
        collector=lambda agent, tree: TreeAck(
            agent=agent,
            final_tree_sha=tree.final_tree_sha,
            accepted=True,
            rationale="audit accurate",
        ),
    )
    rejected = dict(acks)
    rejected[AgentName.CLAUDE] = rejected[AgentName.CLAUDE].model_copy(
        update={"accepted": False}
    )
    with pytest.raises(FinalTreeError, match="双方尚未确认"):
        coordinator.create_integration_commit(
            candidate,
            tree_acks=rejected,
            ballots=ballots(code_commit),
            subject="feat: rejected",
            goal_contract_version=2,
            validation_status="passed",
        )
    with pytest.raises(FinalTreeError, match="权威验证"):
        coordinator.create_integration_commit(
            candidate,
            tree_acks=acks,
            ballots=ballots(code_commit),
            subject="feat: invalid",
            goal_contract_version=2,
            validation_status="failed",
        )
    final_commit = coordinator.create_integration_commit(
        candidate,
        tree_acks=acks,
        ballots=ballots(code_commit),
        subject="feat: implement debated result",
        goal_contract_version=2,
        validation_status="passed",
    )
    workspace.integrate_target(final_commit, candidate.final_tree_sha)

    message = git(repo, "show", "-s", "--format=%B", final_commit)
    assert git(repo, "rev-parse", "HEAD^{tree}") == candidate.final_tree_sha
    assert (repo / ".agent" / "debate" / "RUN-1" / "final-summary.md").exists()
    assert git(repo, "rev-list", "--count", f"{workspace.base_commit}..HEAD") == "1"
    assert git(repo, "rev-list", "--parents", "-n", "1", final_commit).split() == [
        final_commit,
        workspace.base_commit,
    ]
    for trailer in [
        "Debate-Run: RUN-1",
        "Debate-Agents: codex, claude",
        "Goal-Contract: v2",
        f"Final-Tree: {candidate.final_tree_sha}",
        "Final-Tree-Ack: codex, claude",
        "Validation-Status: passed",
        "Audit-Path: .agent/debate/RUN-1",
    ]:
        assert trailer in message


def test_final_tree_requires_all_four_selected_agents_to_complete_and_ack(
    tmp_path: Path,
) -> None:
    """验证四方任一遗漏、未完成或 tree 不一致都会阻止最终提交。"""
    _, workspace = build_workspace(tmp_path)
    code_commit = commit_product_change(workspace)
    writer = AuditBundleWriter(worktree=workspace.worktree_path, run_id="RUN-1")
    writer.write_completed(audit_input(code_commit))
    coordinator = FinalTreeCoordinator(workspace)
    candidate = coordinator.materialize_final_tree(".agent/debate/RUN-1")
    participants = (
        AgentName.CODEX,
        AgentName.CLAUDE,
        AgentName.QODER,
        AgentName.OPENCODE,
    )
    acks = coordinator.collect_tree_acks(
        candidate,
        participants=participants,
        collector=lambda agent, tree: TreeAck(
            agent=agent,
            final_tree_sha=tree.final_tree_sha,
            accepted=True,
            rationale="all selected agents confirm",
        ),
    )
    all_ballots = {
        agent: Ballot(
            agent=agent,
            commit_sha=code_commit,
            goal_contract_version=2,
            completion=CompletionAssessment.COMPLETE,
            criteria=[{"criterion": "tests", "satisfied": True}],
            rationale="complete",
        )
        for agent in participants
    }
    missing = dict(acks)
    missing.pop(AgentName.OPENCODE)
    with pytest.raises(FinalTreeError, match="所有参与者必须确认"):
        coordinator.create_integration_commit(
            candidate,
            tree_acks=missing,
            ballots=all_ballots,
            subject="feat: missing ack",
            goal_contract_version=2,
            validation_status="passed",
            participants=participants,
        )
    incomplete = dict(all_ballots)
    incomplete[AgentName.OPENCODE] = incomplete[AgentName.OPENCODE].model_copy(
        update={"completion": CompletionAssessment.INCOMPLETE}
    )
    with pytest.raises(FinalTreeError, match="所有参与者尚未"):
        coordinator.create_integration_commit(
            candidate,
            tree_acks=acks,
            ballots=incomplete,
            subject="feat: incomplete ballot",
            goal_contract_version=2,
            validation_status="passed",
            participants=participants,
        )


def test_tree_ack_write_or_audit_dispute_product_change_is_blocked(
    tmp_path: Path,
) -> None:
    _, workspace = build_workspace(tmp_path)
    code_commit = commit_product_change(workspace)
    writer = AuditBundleWriter(worktree=workspace.worktree_path, run_id="RUN-1")
    writer.write_completed(audit_input(code_commit))
    coordinator = FinalTreeCoordinator(workspace)
    candidate = coordinator.materialize_final_tree(".agent/debate/RUN-1")

    def mutating_ack(agent: AgentName, tree: FinalTreeCandidate) -> TreeAck:
        (workspace.worktree_path / "src" / "app.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        return TreeAck(
            agent=agent,
            final_tree_sha=candidate.final_tree_sha,
            accepted=True,
            rationale="changed",
        )

    with pytest.raises(FinalTreeError, match="只读"):
        coordinator.collect_tree_acks(candidate, collector=mutating_ack)

    with pytest.raises(FinalTreeError, match="产品代码"):
        coordinator.materialize_final_tree(".agent/debate/RUN-1")
