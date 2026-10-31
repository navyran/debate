"""test_runtime.py 按职责拆分后的测试集合。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from git_helpers import create_repo
from runtime_test_support import (
    CleanInvalidWriterOutputBackend,
    CrashingAckBackend,
    CrashingWriterBackend,
    FakeAgentBackend,
    FakeFacilitatorService,
    InvalidWriterOutputBackend,
    OneAuditDisputeBackend,
)

from debate_agent.artifacts import ArtifactStore
from debate_agent.audit import FinalTreeError
from debate_agent.cli_health import CliPreflightError
from debate_agent.config import RunConfig
from debate_agent.facilitator import (
    select_facilitator_mode,
)
from debate_agent.git_workspace import GitWorkspace
from debate_agent.models import (
    AgentName,
    FacilitatorPurpose,
    RunStatus,
)
from debate_agent.runtime import DebateLifecycleService, DebateRuntime


def test_runtime_uses_thinking_facilitator_for_audit_dispute(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    validation = (
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('result.txt'); "
        "v=p.read_text() if p.exists() else ''; "
        "raise SystemExit(0 if not v or ('codex' in v and 'claude' in v) else 1)",
    )
    config = RunConfig(
        workspace=repo,
        topic="完成 result.txt 并处理审计异议",
        state_root=tmp_path / "state",
        max_turns=4,
        validation_commands=(validation,),
        allowed_scope=("result.txt",),
    )
    backend = OneAuditDisputeBackend()
    facilitator = FakeFacilitatorService()
    service = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: backend,
        facilitator_factory=lambda run_config, artifacts: facilitator,
    )

    result = service.run(config)

    assert result["status"] == RunStatus.COMPLETED.value
    audit_contexts = [
        context
        for context in facilitator.contexts
        if context.purpose is FacilitatorPurpose.AUDIT_DISPUTE
    ]
    assert len(audit_contexts) == 1
    assert select_facilitator_mode(audit_contexts[0]).value == "thinking"

def test_resume_recovers_dirty_writer_without_replaying_uncertain_prompt(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    validation = (
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('result.txt'); "
        "v=p.read_text() if p.exists() else ''; "
        "raise SystemExit(0 if not v or "
        "('codex' in v and 'claude' in v) else 1)",
    )
    config = RunConfig(
        workspace=repo,
        topic="恢复中断后由双方完成 result.txt",
        state_root=tmp_path / "state",
        max_turns=4,
        validation_commands=(validation,),
        allowed_scope=("result.txt",),
    )
    crashing = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: CrashingWriterBackend()
    )

    with pytest.raises(SystemExit, match="simulated hard crash"):
        crashing.run(config)

    run_dirs = list((config.state_root / "runs").iterdir())
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name
    backend = FakeAgentBackend()
    resumed = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: backend
    ).resume(state_root=config.state_root, run_id=run_id)

    assert resumed["status"] == RunStatus.COMPLETED.value
    result = (repo / "result.txt").read_text(encoding="utf-8")
    assert "codex" in result
    assert "claude" in result
    writer_agents = [
        call[0]
        for call in backend.calls
        if call[1] == "TurnResult" and call[2] is False
    ]
    assert writer_agents and writer_agents[0] is AgentName.CLAUDE

def test_resume_rebuilds_session_for_clean_interrupted_invocation(
    tmp_path: Path,
) -> None:
    """验证干净工作区的中断调用会失效旧 Session 并从新代次继续。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="恢复中断同步后完成 result.txt",
        state_root=tmp_path / "state",
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    crashing = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: CrashingAckBackend()
    )

    with pytest.raises(SystemExit, match="simulated ack crash"):
        crashing.run(config)

    run_id = next((config.state_root / "runs").iterdir()).name
    backend = FakeAgentBackend()
    resumed = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: backend
    ).resume(state_root=config.state_root, run_id=run_id)

    assert resumed["status"] == RunStatus.COMPLETED.value
    assert backend.calls[0][3] is None

def test_resume_revalidates_selected_opencode_before_rebuilding_runtime(
    tmp_path: Path,
) -> None:
    """验证含 OpenCode 的中断 run 会在恢复时先重建受控运行配置。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="恢复 OpenCode Session",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    crashing = DebateLifecycleService(
        backend_factory=lambda _config, _store: CrashingAckBackend()
    )
    with pytest.raises(SystemExit, match="simulated ack crash"):
        crashing.run(config)

    preflight_calls: list[RunConfig] = []

    def rebuild_capability(candidate: RunConfig) -> RunConfig:
        """模拟恢复预检；参数为持久化配置，返回运行期配置。"""
        preflight_calls.append(candidate)
        return candidate

    run_id = next((config.state_root / "runs").iterdir()).name
    resumed = DebateLifecycleService(
        backend_factory=lambda _config, _store: FakeAgentBackend(),
        resume_config_preflight=rebuild_capability,
    ).resume(state_root=config.state_root, run_id=run_id)

    assert resumed["status"] == RunStatus.COMPLETED.value
    assert preflight_calls and preflight_calls[0].participants == (
        AgentName.CODEX,
        AgentName.OPENCODE,
    )

@pytest.mark.parametrize(
    "managed_agent",
    (AgentName.HERMES, AgentName.CODEBUDDY),
)
def test_resume_blocks_managed_agent_when_runtime_config_revalidation_fails(
    tmp_path: Path,
    managed_agent: AgentName,
) -> None:
    """验证受控配置 Agent 恢复失败时在打开 checkpoint 前阻断。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="恢复受控 Session",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, managed_agent),
        allowed_scope=("result.txt",),
    )
    run_id = f"RUN-RESUME-{managed_agent.value.upper()}"
    artifacts = ArtifactStore.create(config, run_id=run_id)
    artifacts.write_status(RunStatus.FAILED, stop_reason="模拟中断")
    (artifacts.run_dir / "checkpoint.sqlite").write_bytes(b"not-opened")
    preflight_calls: list[RunConfig] = []

    def reject_drift(candidate: RunConfig) -> RunConfig:
        """模拟恢复配置漂移；参数为持久化配置，始终抛预检错误。"""
        preflight_calls.append(candidate)
        raise CliPreflightError("运行配置漂移")

    resumed = DebateLifecycleService(
        backend_factory=lambda _config, _store: FakeAgentBackend(),
        resume_config_preflight=reject_drift,
    ).resume(state_root=config.state_root, run_id=run_id)

    assert resumed == {
        "run_id": run_id,
        "status": RunStatus.RECOVERY_BLOCKED.value,
        "stop_reason": "运行配置漂移",
    }
    assert preflight_calls == [config]

def test_resume_recovers_writer_changes_after_invalid_structured_output(
    tmp_path: Path,
) -> None:
    """验证 Writer 格式失败不会丢失脏改动的调用归属，可由恢复流程提交。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="恢复格式失败 Writer 后完成 result.txt",
        state_root=tmp_path / "state",
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    failing = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: InvalidWriterOutputBackend()
    )

    failed = failing.run(config)

    assert failed["status"] == RunStatus.FAILED.value

    run_id = next((config.state_root / "runs").iterdir()).name
    backend = FakeAgentBackend()
    resumed = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: backend
    ).resume(state_root=config.state_root, run_id=run_id)

    assert resumed["status"] == RunStatus.COMPLETED.value
    result = (repo / "result.txt").read_text(encoding="utf-8")
    assert result.startswith("codex\n")
    assert "claude\n" in result


def test_resume_revokes_clean_writer_lease_after_invalid_output(
    tmp_path: Path,
) -> None:
    """验证干净 Writer 格式失败后的恢复会撤销遗留租约并继续执行。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="恢复未修改文件的格式失败 Writer",
        state_root=tmp_path / "state",
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    failed = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: (
            CleanInvalidWriterOutputBackend()
        )
    ).run(config)

    assert failed["status"] == RunStatus.FAILED.value
    run_id = next((config.state_root / "runs").iterdir()).name
    active_lease = config.state_root / "runs" / run_id / "leases" / "active.json"
    assert active_lease.is_file()

    resumed = DebateLifecycleService(
        backend_factory=lambda run_config, artifacts: FakeAgentBackend()
    ).resume(state_root=config.state_root, run_id=run_id)

    assert resumed["status"] == RunStatus.COMPLETED.value
    assert not active_lease.exists()


def test_resume_restarts_run_when_preflight_failed_before_checkpoint(
    tmp_path: Path,
) -> None:
    """验证预检阶段失败且无 checkpoint 时恢复会重新预检并启动原 Run。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="预检恢复后由双方完成 result.txt",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        max_turns=4,
        allowed_scope=("result.txt",),
    )
    run_id = "RUN-PREFLIGHT-RECOVERY"
    artifacts = ArtifactStore.create(config, run_id=run_id)
    artifacts.write_status(
        RunStatus.FAILED,
        stop_reason="claude CLI 预检失败 [model]",
    )
    preflight_calls: list[RunConfig] = []
    resume_preflight_calls: list[RunConfig] = []

    def freeze_profiles(candidate: RunConfig) -> RunConfig:
        """模拟成功预检；参数为原配置，返回带冻结身份的配置。"""
        preflight_calls.append(candidate)
        profiles = {
            agent: profile.model_copy(
                update={
                    "model": "gpt-test" if agent is AgentName.CODEX else "sonnet",
                    "version": "1.0.0",
                }
            )
            for agent, profile in candidate.profiles.items()
        }
        return candidate.model_copy(update={"profiles": profiles})

    backend = FakeAgentBackend()

    def rebuild_capability(candidate: RunConfig) -> RunConfig:
        """记录错误的恢复预检调用；参数为配置，返回原配置。"""
        resume_preflight_calls.append(candidate)
        return candidate

    resumed = DebateLifecycleService(
        backend_factory=lambda run_config, store: backend,
        config_preflight=freeze_profiles,
        resume_config_preflight=rebuild_capability,
    ).resume(state_root=config.state_root, run_id=run_id)

    assert resumed["status"] == RunStatus.COMPLETED.value
    assert preflight_calls == [config]
    assert resume_preflight_calls == []
    frozen = artifacts.load_config()
    assert frozen.profiles[AgentName.OPENCODE].model == "sonnet"
    assert frozen.profiles[AgentName.OPENCODE].version == "1.0.0"

def test_abort_preserves_resources_and_cleanup_is_run_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="生命周期测试",
        state_root=tmp_path / "state",
    )
    artifacts = ArtifactStore.create(config, run_id="RUN-LIFECYCLE")
    workspace = GitWorkspace.preflight(
        repo,
        target_branch="main",
        base_ref="HEAD",
        state_root=config.state_root,
        run_id="RUN-LIFECYCLE",
    )
    workspace.acquire_lock()
    workspace.create_worktree()
    artifacts.write_repo(workspace.to_record())
    service = DebateLifecycleService()

    aborted = service.abort(
        state_root=config.state_root,
        run_id="RUN-LIFECYCLE",
    )

    assert aborted["status"] == RunStatus.ABORTED.value
    assert workspace.worktree_path.exists()
    assert workspace.lock_path.exists()

    first = service.cleanup(
        state_root=config.state_root,
        run_id="RUN-LIFECYCLE",
    )
    second = service.cleanup(
        state_root=config.state_root,
        run_id="RUN-LIFECYCLE",
    )

    assert first["status"] == second["status"] == "cleaned"
    assert not workspace.worktree_path.exists()
    assert not workspace.lock_path.exists()

    artifacts.write_status(RunStatus.COMPLETED, stop_reason=None)
    completed = service.abort(
        state_root=config.state_root,
        run_id="RUN-LIFECYCLE",
    )
    assert completed["status"] == RunStatus.COMPLETED.value
    assert not artifacts.abort_requested()

@pytest.mark.parametrize("error_type", [RuntimeError, FinalTreeError])
def test_abort_request_wins_over_inflight_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="运行中取消测试",
        state_root=tmp_path / "state",
    )
    artifacts = ArtifactStore.create(config, run_id="RUN-CANCEL")
    runtime = DebateRuntime(
        config=config,
        run_id="RUN-CANCEL",
        artifacts=artifacts,
        backend=FakeAgentBackend(),
    )

    def raise_cancelled(_state: object) -> None:
        raise error_type("CLI 调用已取消")

    monkeypatch.setattr(runtime, "_node_cancelled", raise_cancelled, raising=False)
    artifacts.request_abort()

    update = runtime.run_node("cancelled", {})

    assert update == {
        "status": RunStatus.ABORTED.value,
        "stop_reason": "收到 abort 请求",
    }
