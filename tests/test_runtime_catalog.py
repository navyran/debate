"""test_runtime.py 按职责拆分后的测试集合。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from git_helpers import create_repo, git
from runtime_test_support import (
    FakeAgentBackend,
    FakeFacilitatorService,
    OneFormatFailureBackend,
    OneNetworkFailureBackend,
    OneOpenCodeFormatFailureBackend,
    OneProcessFailureBackend,
    StructuredReviewBackend,
    WriterBoundaryFailureBackend,
)

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import AgentProfile, RunConfig, _default_profiles
from debate_agent.models import (
    AgentName,
    Ballot,
    CompletionAssessment,
    ReviewProtocolVersion,
    RunStatus,
    TreeAck,
)
from debate_agent.runtime import DebateLifecycleService


def test_review_v1_rejects_reviewer_workspace_write(tmp_path: Path) -> None:
    """验证 Reviewer 只读调用写入工作区会被 Runtime 拒绝。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="Reviewer 只读门禁",
        state_root=tmp_path / "state",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        max_turns=2,
        allowed_scope=("result.txt", "reviewer-write.txt"),
    )
    backend = StructuredReviewBackend(mutate_review=True)

    result = DebateLifecycleService(
        backend_factory=lambda _config, _artifacts: backend,
        facilitator_factory=lambda _config, _artifacts: FakeFacilitatorService(),
    ).run(config)

    assert result["status"] == RunStatus.WORKSPACE_RACE.value

@pytest.mark.parametrize(
    ("participants", "first_agent", "expected_writers"),
    [
        (
            (AgentName.CODEX, AgentName.CLAUDE),
            AgentName.CODEX,
            (AgentName.CODEX, AgentName.CLAUDE),
        ),
        (
            (AgentName.CODEX, AgentName.QODER),
            AgentName.CODEX,
            (AgentName.CODEX, AgentName.QODER),
        ),
        (
            (AgentName.CODEX, AgentName.OPENCODE),
            AgentName.OPENCODE,
            (AgentName.OPENCODE, AgentName.CODEX),
        ),
        (
            (AgentName.CLAUDE, AgentName.OPENCODE),
            AgentName.CLAUDE,
            (AgentName.CLAUDE, AgentName.OPENCODE),
        ),
        (
            (AgentName.QODER, AgentName.OPENCODE),
            AgentName.QODER,
            (AgentName.QODER, AgentName.OPENCODE),
        ),
        (
            (AgentName.CODEX, AgentName.OPENCLAW),
            AgentName.OPENCLAW,
            (AgentName.OPENCLAW, AgentName.CODEX),
        ),
        (
            (AgentName.CLAUDE, AgentName.HERMES),
            AgentName.HERMES,
            (AgentName.HERMES, AgentName.CLAUDE),
        ),
        (
            (AgentName.OPENCLAW, AgentName.HERMES),
            AgentName.OPENCLAW,
            (AgentName.OPENCLAW, AgentName.HERMES),
        ),
        (
            (
                AgentName.CODEX,
                AgentName.CLAUDE,
                AgentName.QODER,
                AgentName.OPENCODE,
            ),
            AgentName.OPENCODE,
            (AgentName.OPENCODE, AgentName.CODEX, AgentName.QODER),
        ),
        (
            tuple(AgentName),
            AgentName.OPENCLAW,
            (
                AgentName.OPENCLAW,
                AgentName.HERMES,
                AgentName.CODEX,
                AgentName.QODER,
                AgentName.OPENCLAW,
            ),
        ),
    ],
)
def test_runtime_completes_catalog_combination_matrix(
    tmp_path: Path,
    participants: tuple[AgentName, ...],
    first_agent: AgentName,
    expected_writers: tuple[AgentName, ...],
) -> None:
    """验证 Catalog 组合完整运行；参数为组合/首位/Writer 序列，无返回值。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="多 CLI 完成 result.txt",
        state_root=tmp_path / "state",
        participants=participants,
        first_agent=first_agent,
        max_turns=8,
        allowed_scope=("result.txt",),
        profiles={
            **_default_profiles(),
            AgentName.CODEBUDDY: AgentProfile(command=sys.executable),
        },
    )
    backend = FakeAgentBackend()

    result = DebateLifecycleService(
        backend_factory=lambda _config, _store: backend,
        facilitator_factory=lambda _config, _store: FakeFacilitatorService(
            recommended_next_writer=None
        ),
    ).run(config)

    assert result["status"] == RunStatus.COMPLETED.value
    assert {call[0] for call in backend.calls} == set(participants)
    writer_agents = [
        agent
        for agent, schema_name, read_only, _session_id in backend.calls
        if schema_name == "TurnResult" and not read_only
    ]
    assert tuple(writer_agents) == expected_writers
    session_agents = {
        agent
        for agent, _schema_name, _read_only, session_id in backend.calls
        if session_id == f"{agent.value}-session"
    }
    assert session_agents == set(participants)
    memory_ack_agents = {
        agent
        for agent, schema_name, _read_only, _session_id in backend.calls
        if schema_name == "ChangeAck"
    }
    assert memory_ack_agents == set(participants)
    ballot_agents = {
        agent
        for agent, schema_name, _read_only, _session_id in backend.calls
        if schema_name == "Ballot"
    }
    tree_ack_agents = {
        agent
        for agent, schema_name, _read_only, _session_id in backend.calls
        if schema_name == "TreeAck"
    }
    assert ballot_agents == set(participants)
    assert tree_ack_agents == set(participants)
    run_id = str(result["run_id"])
    store = ArtifactStore.open(state_root=config.state_root, run_id=run_id)
    state = store.read_state()
    ballots = {
        AgentName(agent): Ballot.model_validate(payload)
        for agent, payload in state["ballots"].items()
    }
    assert set(ballots) == set(participants)
    assert all(
        ballot.completion is CompletionAssessment.COMPLETE
        for ballot in ballots.values()
    )
    assert {ballot.commit_sha for ballot in ballots.values()} == {
        state["final_candidate"]["code_commit"]
    }
    tree_acks = {
        AgentName(agent): TreeAck.model_validate(payload)
        for agent, payload in state["tree_acks"].items()
    }
    assert set(tree_acks) == set(participants)
    assert {ack.final_tree_sha for ack in tree_acks.values()} == {
        state["final_tree_sha"]
    }
    ack_paths = tuple((store.run_dir / "acks").glob("*.json"))
    assert {
        AgentName(json.loads(path.read_text(encoding="utf-8"))["agent"])
        for path in ack_paths
    } == set(participants)
    final_summary = (
        repo / ".agent" / "debate" / run_id / "final-summary.md"
    ).read_text(encoding="utf-8")
    for agent in participants:
        assert f"- {agent.value}: complete;" in final_summary
    checkpoint_summary = (
        repo / ".agent" / "debate" / run_id / "change-summary.md"
    ).read_text(encoding="utf-8")
    if first_agent is AgentName.OPENCODE:
        assert "## TURN-001 · opencode" in checkpoint_summary
    final_message = git(repo, "show", "-s", "--format=%B", "HEAD")
    selected = ", ".join(agent.value for agent in participants)
    assert f"Debate-Agents: {selected}" in final_message
    assert f"Final-Tree-Ack: {selected}" in final_message

@pytest.mark.parametrize("use_worktree", [False, True])
def test_runtime_can_preserve_completed_result_without_target_integration(
    tmp_path: Path, use_worktree: bool
) -> None:
    """验证关闭集成后目标分支不变且结果分支可继续修改。"""
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
        topic="完成 result.txt 但保留结果分支",
        state_root=tmp_path / "state",
        use_worktree=use_worktree,
        integrate_to_target=False,
        max_turns=4,
        validation_commands=(validation,),
        allowed_scope=("result.txt",),
    )

    result = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: FakeAgentBackend(),
        facilitator_factory=lambda run_config, artifacts: FakeFacilitatorService(),
    ).run(config)

    assert result["status"] == RunStatus.COMPLETED.value
    assert result["integration_succeeded"] is False
    assert result["integration_skipped"] is True
    assert git(repo, "rev-parse", "refs/heads/main") == base
    run_id = str(result["run_id"])
    store = ArtifactStore.open(state_root=config.state_root, run_id=run_id)
    state = store.read_state()
    result_branch = str(state["result_branch"])
    result_worktree = Path(str(state["result_worktree"]))
    assert result_branch == f"debate/{run_id}"
    assert git(repo, "rev-parse", result_branch) == result["current_head"]
    assert (
        git(repo, "rev-parse", f"{result_branch}^{{tree}}")
        == result["final_tree_sha"]
    )
    assert git(result_worktree, "status", "--porcelain") == ""
    assert (result_worktree / "result.txt").read_text(encoding="utf-8") == (
        "codex\nclaude\n"
    )
    assert (
        result_worktree / ".agent" / "debate" / run_id / "final-summary.md"
    ).exists()
    if use_worktree:
        assert git(repo, "branch", "--show-current") == "main"
        assert result_worktree != repo
    else:
        assert git(repo, "branch", "--show-current") == result_branch
        assert result_worktree == repo

@pytest.mark.parametrize(
    "backend_type",
    [
        OneFormatFailureBackend,
        OneProcessFailureBackend,
        OneNetworkFailureBackend,
        WriterBoundaryFailureBackend,
    ],
)
def test_runtime_retries_safe_failure_only_when_workspace_is_unchanged(
    tmp_path: Path,
    backend_type: type[FakeAgentBackend],
) -> None:
    repo = create_repo(tmp_path)
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
        topic="格式重试后完成 result.txt",
        state_root=tmp_path / "state",
        max_turns=4,
        format_retries=1,
        validation_commands=(validation,),
        allowed_scope=("result.txt",),
    )
    backend = backend_type()

    result = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: backend
    ).run(config)

    assert backend.failed_once is True
    assert result["status"] == RunStatus.COMPLETED.value
    run_id = str(result["run_id"])
    facilitator_log = (
        repo / ".agent" / "debate" / run_id / "facilitator-decisions.jsonl"
    ).read_text(encoding="utf-8")
    records = [json.loads(line) for line in facilitator_log.splitlines()]
    assert records
    assert all(record["status"] == "facilitator_fallback" for record in records)
    assert all(record["error_category"] == "missing_api_key" for record in records)

def test_opencode_format_error_uses_existing_single_retry_without_bypass(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    validation = (
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('result.txt'); "
        "value=p.read_text() if p.exists() else ''; "
        "raise SystemExit(0 if not value or "
        "('codex' in value and 'opencode' in value) else 1)",
    )
    config = RunConfig(
        workspace=repo,
        topic="OpenCode 格式重试后完成 result.txt",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        max_turns=4,
        format_retries=1,
        validation_commands=(validation,),
        allowed_scope=("result.txt",),
    )
    backend = OneOpenCodeFormatFailureBackend()

    result = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: backend
    ).run(config)

    assert result["status"] == RunStatus.COMPLETED.value
    assert backend.failed_once is True
    assert backend.open_code_calls >= 2
    assert sum(
        "上一次响应格式无效" in prompt
        for _model, _read_only, prompt in backend.prompts
    ) == 1
