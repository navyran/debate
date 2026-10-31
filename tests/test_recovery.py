from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from git_helpers import create_repo, git

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import RunConfig
from debate_agent.git_workspace import GitWorkspace, GitWorkspaceError
from debate_agent.issue_ledger import IssueLedgerError, IssueLedgerStore
from debate_agent.models import AgentName, ReviewProtocolVersion, RunStatus
from debate_agent.policy import CommandPolicy, WritePolicy
from debate_agent.process import ProcessRunner
from debate_agent.recovery import RecoveryBlockedError, RecoveryManager
from debate_agent.supervisor import Supervisor
from debate_agent.validation import ValidationRunner


def config(tmp_path: Path, workspace: Path) -> RunConfig:
    return RunConfig(
        workspace=workspace,
        topic="recover goal",
        state_root=tmp_path / "state",
        validation_commands=((sys.executable, "-c", "raise SystemExit(0)"),),
    )


def build_workspace(tmp_path: Path) -> tuple[RunConfig, ArtifactStore, GitWorkspace]:
    repo = create_repo(tmp_path)
    run_config = config(tmp_path, repo)
    artifacts = ArtifactStore.create(run_config, run_id="RUN-1")
    workspace = GitWorkspace.preflight(
        repo,
        target_branch="main",
        base_ref="HEAD",
        state_root=run_config.state_root,
        run_id="RUN-1",
    )
    workspace.acquire_lock()
    workspace.create_worktree()
    artifacts.write_repo(workspace.to_record())
    return run_config, artifacts, workspace


def test_artifact_store_creates_runtime_layout_and_idempotent_journal(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    run_config = config(tmp_path, repo)
    store = ArtifactStore.create(run_config, run_id="RUN-1")

    required_dirs = {
        "goal-contract",
        "updates",
        "acks",
        "leases",
        "invocations",
        "validation",
        "raw",
    }
    assert required_dirs.issubset(
        {path.name for path in store.run_dir.iterdir() if path.is_dir()}
    )
    assert store.load_config() == run_config
    assert store.read_status()["status"] == RunStatus.RUNNING.value
    created_at = store.read_status()["created_at"]
    store.write_status(RunStatus.ABORTED, stop_reason="测试")
    assert store.read_status()["created_at"] == created_at

    store.prepare_invocation(
        invocation_id="INV-1",
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit="abc",
        goal_contract_version=1,
        prompt_hash="sha256:0123456789abcdef",
    )
    store.mark_invocation_running("INV-1", pid=123, process_group=123)
    store.mark_invocation_result("INV-1", result_ref="raw/INV-1.json", ok=True)
    store.mark_invocation_commit("INV-1", commit_sha="def")
    store.mark_invocation_checkpointed("INV-1")

    journal = store.read_invocation("INV-1")
    assert journal["status"] == "checkpointed"
    assert journal["commit_sha"] == "def"
    serialized = json.dumps(journal).lower().replace("prompt_hash", "")
    assert "prompt" not in serialized
    assert store.completed_invocation("INV-1") == journal


def test_recovery_stops_on_semantically_invalid_issue_ledger(tmp_path: Path) -> None:
    """验证恢复前读取到语义损坏 Ledger 时明确停止而非伪造空状态。"""
    repo = create_repo(tmp_path)
    run_config = config(tmp_path, repo).model_copy(
        update={
            "protocol_version": ReviewProtocolVersion.REVIEW_V1,
            "issue_ledger_enabled": True,
        }
    )
    artifacts = ArtifactStore.create(run_config, run_id="RUN-LEDGER-RECOVERY")
    artifacts.write_issue_ledger_payload(
        {"protocol_version": "review-v1", "issues": [{"issue_id": "ISSUE-BAD"}]}
    )

    with pytest.raises(IssueLedgerError, match="已损坏"):
        IssueLedgerStore(artifacts=artifacts).load()

    diagnostic = json.loads(
        (artifacts.run_dir / "diagnostic.json").read_text(encoding="utf-8")
    )
    assert diagnostic["phase"] == "issue_ledger_recovery"


def test_recovery_commits_uniquely_attributable_dirty_change(tmp_path: Path) -> None:
    run_config, artifacts, workspace = build_workspace(tmp_path)
    supervisor = Supervisor(state_root=run_config.state_root, run_id="RUN-1")
    supervisor.grant_write_lease(
        agent=AgentName.CLAUDE,
        turn_id="TURN-2",
        base_commit=workspace.head(),
        goal_contract_version=1,
    )
    artifacts.prepare_invocation(
        invocation_id="INV-2",
        agent=AgentName.CLAUDE,
        turn_id="TURN-2",
        base_commit=workspace.head(),
        goal_contract_version=1,
        prompt_hash="sha256:x",
    )
    source = workspace.worktree_path / "src"
    source.mkdir()
    (source / "recovered.py").write_text("VALUE = 1\n", encoding="utf-8")
    process = ProcessRunner(raw_root=artifacts.run_dir / "raw")
    validation = ValidationRunner(
        state_root=run_config.state_root,
        run_id="RUN-1",
        process_runner=process,
        command_policy=CommandPolicy(approved_commands=run_config.validation_commands),
    )
    manager = RecoveryManager(
        workspace=workspace,
        artifacts=artifacts,
        supervisor=supervisor,
        write_policy=WritePolicy(
            worktree=workspace.worktree_path, allowed_scope=["src"]
        ),
        validation_runner=validation,
        validation_commands=run_config.validation_commands,
        env={},
    )

    recovered = manager.recover_dirty_checkpoint()

    assert recovered is not None
    assert recovered.commit_sha == git(workspace.worktree_path, "rev-parse", "HEAD")
    message = git(workspace.worktree_path, "show", "-s", "--format=%B")
    assert "[debate][claude][TURN-2][recovered-passed]" in message
    assert git(workspace.worktree_path, "status", "--porcelain") == ""
    assert artifacts.read_invocation("INV-2")["commit_sha"] == recovered.commit_sha


def test_recovery_blocks_unknown_dirty_change_without_committing(
    tmp_path: Path,
) -> None:
    run_config, artifacts, workspace = build_workspace(tmp_path)
    (workspace.worktree_path / "unknown.py").write_text("x = 1\n", encoding="utf-8")
    manager = RecoveryManager(
        workspace=workspace,
        artifacts=artifacts,
        supervisor=Supervisor(state_root=run_config.state_root, run_id="RUN-1"),
        write_policy=WritePolicy(worktree=workspace.worktree_path, allowed_scope=["."]),
        validation_runner=ValidationRunner(
            state_root=run_config.state_root,
            run_id="RUN-1",
            process_runner=ProcessRunner(raw_root=artifacts.run_dir / "raw"),
            command_policy=CommandPolicy(),
        ),
        validation_commands=(),
        env={},
    )

    with pytest.raises(RecoveryBlockedError, match="来源"):
        manager.recover_dirty_checkpoint()

    assert git(workspace.worktree_path, "rev-parse", "HEAD") == workspace.base_commit
    assert git(workspace.worktree_path, "diff", "--cached", "--name-only") == ""


def test_git_recovery_record_cannot_redirect_cleanup_outside_run_layout(
    tmp_path: Path,
) -> None:
    run_config, artifacts, workspace = build_workspace(tmp_path)
    record = artifacts.read_repo()
    record["worktree_path"] = str(tmp_path / "unrelated")

    with pytest.raises(GitWorkspaceError, match="布局"):
        GitWorkspace.from_record(
            record,
            expected_state_root=run_config.state_root,
            expected_run_id="RUN-1",
            require_worktree=False,
        )

    assert workspace.worktree_path.exists()
