"""test_runtime.py 按职责拆分后的测试集合。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from git_helpers import create_repo, git
from runtime_test_support import (
    FakeAgentBackend,
    FakeFacilitatorService,
    StructuredReviewBackend,
)

from debate_agent.artifacts import ArtifactStore
from debate_agent.cli_health import CliIdentity
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import (
    AgentName,
    Ballot,
    CompletionAssessment,
    ReviewProtocolVersion,
    RunStatus,
)
from debate_agent.review_models import (
    EvidenceKind,
)
from debate_agent.runtime import DebateLifecycleService, DebateRuntime


def test_runtime_cli_identity_uses_catalog_capabilities(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    base = RunConfig(
        workspace=repo,
        topic="身份能力边界",
        state_root=tmp_path / "state",
    )
    profiles = dict(base.profiles)
    for agent in (AgentName.CODEX, AgentName.CLAUDE, AgentName.QODER):
        profiles[agent] = AgentProfile(
            command=agent.value,
            model="legacy-model",
            version="1.2.3",
            variant="legacy-variant",
            runtime_config_hash="a" * 64,
            runtime_config_content_hash="b" * 64,
        )
    profiles[AgentName.OPENCODE] = AgentProfile(
        command="opencode",
        model="provider/model",
        version="1.2.3",
        variant="quality",
        runtime_config_hash="c" * 64,
        runtime_config_content_hash="d" * 64,
    )
    profiles[AgentName.HERMES] = AgentProfile(
        command="hermes",
        model="deepseek-v4-flash",
        provider="deepseek",
        version="0.19.0",
        runtime_config_hash="f" * 64,
    )
    profiles[AgentName.CODEBUDDY] = AgentProfile(
        command="codebuddy",
        model="gpt-5",
        version="2.125.4",
        native_profile="reviewer",
        runtime_config_hash="e" * 64,
    )
    config = base.model_copy(update={"profiles": profiles})
    artifacts = ArtifactStore.create(config, run_id="RUN-CLI-IDENTITY")
    runtime = DebateRuntime(
        config=config,
        run_id="RUN-CLI-IDENTITY",
        artifacts=artifacts,
        backend=FakeAgentBackend(),
    )

    for agent in (AgentName.CODEX, AgentName.CLAUDE, AgentName.QODER):
        assert runtime._cli_identity(agent) == CliIdentity(
            agent=agent,
            executable=agent.value,
            version="1.2.3",
            model="legacy-model",
        )
    assert runtime._cli_identity(AgentName.OPENCODE) == CliIdentity(
        agent=AgentName.OPENCODE,
        executable="opencode",
        version="1.2.3",
        model="provider/model",
        variant="quality",
        runtime_config_hash="c" * 64,
        runtime_config_content_hash="d" * 64,
    )
    assert runtime._cli_identity(AgentName.CODEBUDDY) == CliIdentity(
        agent=AgentName.CODEBUDDY,
        executable="codebuddy",
        version="2.125.4",
        model="gpt-5",
        runtime_config_hash="e" * 64,
    )
    assert runtime._cli_identity(AgentName.HERMES) == CliIdentity(
        agent=AgentName.HERMES,
        executable="hermes",
        version="0.19.0",
        model="deepseek-v4-flash",
        runtime_config_hash="f" * 64,
    )

def test_incomplete_ballot_feedback_is_injected_only_for_its_agent() -> None:
    """验证未完成票理由回传给原投票 Agent，避免下一轮 Writer 无信息空转。"""
    state = {
        "ballots": {
            "codex": {
                "agent": "codex",
                "commit_sha": "abc",
                "goal_contract_version": 1,
                "completion": "incomplete",
                "criteria": [],
                "rationale": "微信 APIv3 没有独立沙箱，应修正文档。",
            },
            "claude": {
                "agent": "claude",
                "commit_sha": "abc",
                "goal_contract_version": 1,
                "completion": "complete",
                "criteria": [],
                "rationale": "目标已完成。",
            },
        }
    }

    codex_feedback = DebateRuntime._ballot_feedback_prompt(state, AgentName.CODEX)
    claude_feedback = DebateRuntime._ballot_feedback_prompt(state, AgentName.CLAUDE)

    assert "微信 APIv3 没有独立沙箱" in codex_feedback
    assert claude_feedback == ""

def test_runtime_completes_two_agent_debate_and_focused_integration(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    validation = (
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('result.txt'); "
        "value=p.read_text() if p.exists() else ''; "
        "raise SystemExit(0 if not value or "
        "('codex' in value and 'claude' in value) else 1)",
    )
    config = RunConfig(
        workspace=repo,
        topic="让 Codex 和 Claude 共同生成 result.txt",
        state_root=tmp_path / "state",
        max_turns=4,
        validation_commands=(validation,),
        allowed_scope=("result.txt",),
    )
    backend = FakeAgentBackend()
    facilitator = FakeFacilitatorService()
    service = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: backend,
        facilitator_factory=lambda run_config, artifacts: facilitator,
    )

    result = service.run(config)

    assert result["status"] == "completed"
    assert git(repo, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert (repo / "result.txt").read_text(encoding="utf-8") == "codex\nclaude\n"
    run_id = str(result["run_id"])
    audit_path = repo / ".agent" / "debate" / run_id
    assert (audit_path / "final-summary.md").exists()
    final_summary = (audit_path / "final-summary.md").read_text(encoding="utf-8")
    assert "审查当前实现" not in final_summary
    checkpoint_commits: dict[str, str] = {}
    for block in (audit_path / "change-summary.md").read_text().split("\n## ")[1:]:
        heading, *lines = block.splitlines()
        agent = heading.split(" · ", maxsplit=1)[1]
        commit = next(line for line in lines if line.startswith("- Commit: "))
        checkpoint_commits[agent] = commit.removeprefix("- Commit: ")
    writer_decisions = {
        row["agent"]: row["commit_sha"]
        for row in (
            json.loads(line)
            for line in (audit_path / "decisions.jsonl").read_text().splitlines()
        )
        if row["stage"] == "writer"
    }
    assert writer_decisions == checkpoint_commits
    store = ArtifactStore.open(state_root=config.state_root, run_id=run_id)
    repo_record = store.read_repo()
    assert repo_record["use_worktree"] is False
    assert Path(str(repo_record["worktree_path"])) == repo.resolve()
    assert git(repo, "branch", "--list", str(repo_record["debate_branch"])) == ""
    assert store.read_status()["status"] == "completed"
    assert {call[0] for call in backend.calls} == {AgentName.CODEX, AgentName.CLAUDE}
    assert any(call[2] is False for call in backend.calls)
    assert any(call[2] is True for call in backend.calls)
    assessment_prompts = [
        prompt
        for model, _read_only, prompt in backend.prompts
        if model == "TurnResult"
    ]
    assert assessment_prompts
    assert all(
        "不要等待后续 Ballot 或 TreeAck" in prompt for prompt in assessment_prompts
    )
    writer_prompts = [
        prompt
        for model, read_only, prompt in backend.prompts
        if model == "TurnResult" and not read_only
    ]
    assert facilitator.contexts
    assert any("Facilitator 建议（非授权命令）" in prompt for prompt in writer_prompts)
    assert all("DEEPSEEK_API_KEY" not in prompt for prompt in writer_prompts)

def test_runtime_completes_selected_three_agent_debate(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="由三个 CLI Agent 共同完成 result.txt",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.CLAUDE, AgentName.QODER),
        max_turns=5,
        allowed_scope=("result.txt",),
    )
    backend = FakeAgentBackend()
    result = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: backend,
        facilitator_factory=lambda run_config, artifacts: FakeFacilitatorService(),
    ).run(config)

    assert result["status"] == RunStatus.COMPLETED.value
    assert {call[0] for call in backend.calls} == set(config.participants)
    run_id = str(result["run_id"])
    message = git(repo, "show", "-s", "--format=%B", "HEAD")
    assert "Debate-Agents: codex, claude, qoder" in message
    assert "Final-Tree-Ack: codex, claude, qoder" in message
    summary = (
        repo / ".agent" / "debate" / run_id / "final-summary.md"
    ).read_text(encoding="utf-8")
    assert "## Qoder 贡献" in summary

def test_review_v1_persists_current_head_review_and_post_commit_evidence(
    tmp_path: Path,
) -> None:
    """验证 review-v1 只接受提交后 Supervisor 重新登记的当前 HEAD 证据。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="用结构化 Reviewer 完成 result.txt",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        issue_ledger_enabled=True,
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    backend = StructuredReviewBackend()
    facilitator = FakeFacilitatorService()
    result = DebateLifecycleService(
        backend_factory=lambda _config, _artifacts: backend,
        facilitator_factory=lambda _config, _artifacts: facilitator,
    ).run(config)

    assert result["status"] == RunStatus.COMPLETED.value
    store = ArtifactStore.open(
        state_root=config.state_root, run_id=str(result["run_id"])
    )
    state = store.read_state()
    review = store.recover_review_result(state["review_result_ref"])
    assert review is not None
    assert review.head_sha == state["current_head"]
    validation_ref = next(
        ref.artifact_ref
        for coverage in review.coverage
        for ref in coverage.evidence_refs
        if ref.kind is EvidenceKind.VALIDATION_REPORT
    )
    assert validation_ref is not None
    report = json.loads((store.run_dir / validation_ref).read_text(encoding="utf-8"))
    assert report["head_sha"] == review.head_sha
    assert str(validation_ref).startswith("validation/REVIEW-HEAD-")
    assert any(
        name == "ReviewResult" and read_only
        for _, name, read_only, _ in backend.calls
    )
    assert backend.calls[-1][1] != "ReviewResult" or backend.calls[-1][2] is True
    assert facilitator.contexts
    assert validation_ref in facilitator.contexts[-1].review_evidence_refs
    assert facilitator.contexts[-1].unresolved_review_criteria == []
    assert store.read_issue_ledger_payload() == {
        "protocol_version": "review-v1",
        "issues": [],
        "transitions": [],
    }
    audit_ledger = (
        repo / ".agent" / "debate" / str(result["run_id"]) / "issue-ledger.json"
    )
    assert json.loads(audit_ledger.read_text(encoding="utf-8")) == {
        "protocol_version": "review-v1",
        "issues": [],
        "transitions": [],
    }

def test_counterexample_ballot_is_prompted_validated_and_audited(
    tmp_path: Path,
) -> None:
    """验证显式反例终局投票沿 Runtime、盲投和审计完整传递。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="以结构化反例完成 result.txt",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        counterexample_ballot=True,
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    backend = StructuredReviewBackend()
    result = DebateLifecycleService(
        backend_factory=lambda _config, _artifacts: backend,
        facilitator_factory=lambda _config, _artifacts: FakeFacilitatorService(),
    ).run(config)

    assert result["status"] == RunStatus.COMPLETED.value
    ballot_prompts = [
        prompt
        for response_model, _read_only, prompt in backend.prompts
        if response_model == "Ballot"
    ]
    assert ballot_prompts
    assert all("strongest_counterexample" in prompt for prompt in ballot_prompts)
    store = ArtifactStore.open(
        state_root=config.state_root, run_id=str(result["run_id"])
    )
    ballots = store.read_state()["ballots"]
    assert all(
        ballot["strongest_counterexample"]
        and ballot["counterexample_resolution"]
        for ballot in ballots.values()
    )
    summary = (
        repo / ".agent" / "debate" / str(result["run_id"]) / "final-summary.md"
    ).read_text(encoding="utf-8")
    assert "最强反例" in summary
    assert "处理与证据" in summary

def test_final_ballot_rejects_missing_counterexample_and_open_ledger_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证反例字段缺失及未关闭阻断账本项均不能通过终局收集。"""
    config = RunConfig(
        workspace=tmp_path,
        topic="终局门禁",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        counterexample_ballot=True,
    )
    artifacts = ArtifactStore.create(config, run_id="RUN-COUNTEREXAMPLE-GATE")
    runtime = DebateRuntime(
        config=config,
        run_id="RUN-COUNTEREXAMPLE-GATE",
        artifacts=artifacts,
        backend=FakeAgentBackend(),
    )
    ballot = Ballot(
        agent=AgentName.CODEX,
        commit_sha="head",
        goal_contract_version=1,
        completion=CompletionAssessment.COMPLETE,
        criteria=[],
        rationale="完成",
    )

    with pytest.raises(ValueError, match="必须提供最强反例"):
        runtime._validate_final_ballot(ballot)

    complete_with_counterexample = ballot.model_copy(
        update={
            "strongest_counterexample": "旧实现未处理边界",
            "counterexample_resolution": "已由当前 HEAD 验证报告覆盖。",
        }
    )
    monkeypatch.setattr(
        runtime,
        "_unresolved_blocking_ledger_issues",
        lambda: (SimpleNamespace(issue_id="ISSUE-OPEN"),),
    )
    with pytest.raises(ValueError, match="必须投 incomplete: ISSUE-OPEN"):
        runtime._validate_final_ballot(complete_with_counterexample)
    runtime._validate_final_ballot(
        complete_with_counterexample.model_copy(
            update={"completion": CompletionAssessment.INCOMPLETE}
        )
    )

def test_review_v1_unknown_coverage_cannot_enter_final_stage(tmp_path: Path) -> None:
    """验证 unknown Coverage 通过确定性门禁阻止 review-v1 进入最终阶段。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="unknown 不得进入 final",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        max_turns=2,
        allowed_scope=("result.txt",),
    )
    backend = StructuredReviewBackend(unknown_coverage=True)
    result = DebateLifecycleService(
        backend_factory=lambda _config, _artifacts: backend,
        facilitator_factory=lambda _config, _artifacts: FakeFacilitatorService(),
    ).run(config)

    assert result["status"] == RunStatus.MAX_TURNS.value
    assert not any(name == "Ballot" for _, name, _, _ in backend.calls)
    store = ArtifactStore.open(
        state_root=config.state_root, run_id=str(result["run_id"])
    )
    state = store.read_state()
    assert state["review_blocking_summary"]
    assert any("integration" in item for item in state["review_blocking_summary"])

def test_review_v1_ledger_lifecycle_uses_actual_checkpoint_and_review_evidence(
    tmp_path: Path,
) -> None:
    """验证账本生命周期由真实 checkpoint 与受策略校验证据驱动。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="用 Ledger 完成 result.txt",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        issue_ledger_enabled=True,
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    backend = StructuredReviewBackend(ledger_lifecycle=True)
    result = DebateLifecycleService(
        backend_factory=lambda _config, _artifacts: backend,
        facilitator_factory=lambda _config, _artifacts: FakeFacilitatorService(),
    ).run(config)

    assert result["status"] == RunStatus.COMPLETED.value
    store = ArtifactStore.open(
        state_root=config.state_root, run_id=str(result["run_id"])
    )
    ledger = store.read_issue_ledger_payload()
    assert ledger is not None
    assert len(ledger["issues"]) == 1
    issue = ledger["issues"][0]
    assert issue["status"] == "resolved"
    assert issue["addressed_by_agent"] == AgentName.CLAUDE.value
    assert issue["addressed_by_commit"] == issue["resolved_head"]
    assert issue["resolved_by"] == AgentName.CODEX.value
    assert [transition["action"] for transition in ledger["transitions"]] == [
        "open_from_review",
        "addressed_by_writer",
        "reviewer_resolved",
    ]
    assert all(transition["occurred_at"] for transition in ledger["transitions"])
    state = store.read_state()
    summary = store.run_dir / state["latest_change_summary_ref"]
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["source_agent"] == AgentName.CODEX.value
    assert payload["resolved_issues"] == [issue["issue_id"]]
    assert payload["requested_attention"] == []
    audit_payload = json.loads(
        (
            repo / ".agent" / "debate" / str(result["run_id"]) / "issue-ledger.json"
        ).read_text(encoding="utf-8")
    )
    assert audit_payload == ledger

def test_review_v1_addresses_validation_backed_blocker_after_passing_checkpoint(
    tmp_path: Path,
) -> None:
    """验证无 Git 路径的验证证据也能登记真实 Writer 检查点供独立复核。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="验证证据 Ledger 闭环",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        issue_ledger_enabled=True,
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    result = DebateLifecycleService(
        backend_factory=lambda _config, _artifacts: StructuredReviewBackend(
            ledger_lifecycle=True, ledger_runtime_evidence=True
        ),
        facilitator_factory=lambda _config, _artifacts: FakeFacilitatorService(),
    ).run(config)

    assert result["status"] == RunStatus.COMPLETED.value
    ledger = ArtifactStore.open(
        state_root=config.state_root, run_id=str(result["run_id"])
    ).read_issue_ledger_payload()
    assert ledger is not None
    issue = ledger["issues"][0]
    assert issue["affected_paths"] == []
    assert issue["addressed_by_commit"] == issue["resolved_head"]
    assert [item["action"] for item in ledger["transitions"]] == [
        "open_from_review",
        "addressed_by_writer",
        "reviewer_resolved",
    ]

def test_resume_replays_review_that_opened_new_ledger_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证中断发生在登记新 Finding 后仍可复用已持久化的 Review。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="重放持久化 Ledger Review",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        issue_ledger_enabled=True,
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    original_handoff = DebateRuntime._publish_ledger_handoff

    def crash_after_ledger_update(
        self: DebateRuntime, **_kwargs: object
    ) -> tuple[str, dict[str, list[str]]]:
        raise SystemExit("simulated ledger handoff crash")

    monkeypatch.setattr(
        DebateRuntime, "_publish_ledger_handoff", crash_after_ledger_update
    )
    with pytest.raises(SystemExit, match="ledger handoff crash"):
        DebateLifecycleService(
            backend_factory=lambda _config, _artifacts: StructuredReviewBackend(
                ledger_lifecycle=True
            ),
            facilitator_factory=lambda _config, _artifacts: FakeFacilitatorService(),
        ).run(config)

    run_id = next((config.state_root / "runs").iterdir()).name
    monkeypatch.setattr(DebateRuntime, "_publish_ledger_handoff", original_handoff)
    resumed_backend = StructuredReviewBackend(
        ledger_lifecycle=True, initial_review_count=1
    )
    resumed = DebateLifecycleService(
        backend_factory=lambda _config, _artifacts: resumed_backend,
        facilitator_factory=lambda _config, _artifacts: FakeFacilitatorService(),
    ).resume(state_root=config.state_root, run_id=run_id)

    assert resumed["status"] == RunStatus.COMPLETED.value
    assert "REVIEW-TURN-001" not in resumed_backend.review_turns
    assert resumed_backend.review_turns[0] == "REVIEW-TURN-002"
    ledger = ArtifactStore.open(
        state_root=config.state_root, run_id=run_id
    ).read_issue_ledger_payload()
    assert ledger is not None
    assert [issue["status"] for issue in ledger["issues"]] == ["resolved"]

def test_checkpoint_handoff_publishes_only_canonical_ledger_peer_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 Writer checkpoint 后待同步的 PeerMemory 只投影 Ledger。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="检查点 Ledger 交接",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        issue_ledger_enabled=True,
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    original_review = DebateRuntime._node_review_current_head

    def crash_before_second_reviewer_sync(
        self: DebateRuntime, state: dict[str, object]
    ) -> dict[str, object]:
        if int(state["current_turn"]) == 2:
            raise SystemExit("simulated pre-review crash")
        return dict(original_review(self, state))

    monkeypatch.setattr(
        DebateRuntime, "_node_review_current_head", crash_before_second_reviewer_sync
    )
    with pytest.raises(SystemExit, match="pre-review crash"):
        DebateLifecycleService(
            backend_factory=lambda _config, _artifacts: StructuredReviewBackend(
                ledger_lifecycle=True
            ),
            facilitator_factory=lambda _config, _artifacts: FakeFacilitatorService(),
        ).run(config)

    run_id = next((config.state_root / "runs").iterdir()).name
    store = ArtifactStore.open(state_root=config.state_root, run_id=run_id)
    ledger = store.read_issue_ledger_payload()
    assert ledger is not None
    issue_id = ledger["issues"][0]["issue_id"]
    update_id = "UPD-TURN-002-claude"
    update = json.loads(
        (store.run_dir / "updates" / f"{update_id}-codex.json").read_text(
            encoding="utf-8"
        )
    )
    cursors = json.loads(
        (store.run_dir / "memory-cursors.json").read_text(encoding="utf-8")
    )

    assert update["opened_issues"] == [issue_id]
    assert update["resolved_issues"] == []
    assert update["requested_attention"] == [
        f"{issue_id}: {ledger['issues'][0]['claim']}"
    ]
    assert update["update_id"] in cursors["pending"][AgentName.CODEX.value]
