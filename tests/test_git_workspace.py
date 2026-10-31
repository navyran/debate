from __future__ import annotations

import json
from pathlib import Path

import pytest
from git_helpers import create_repo, git

from debate_agent.git_workspace import (
    DirtyWorkspaceError,
    GitWorkspace,
    GitWorkspaceError,
    IntegrationBlockedError,
    RepositoryLockError,
    WorkspaceRaceError,
)
from debate_agent.models import AgentName
from debate_agent.policy import PolicyViolationError, WritePolicy
from debate_agent.supervisor import Supervisor


def build_workspace(tmp_path: Path) -> tuple[Path, GitWorkspace]:
    repo = create_repo(tmp_path)
    workspace = GitWorkspace.preflight(
        repo,
        target_branch="main",
        base_ref="HEAD",
        state_root=tmp_path / "state",
        run_id="RUN-1",
    )
    return repo, workspace


def test_preflight_resolves_clean_repository_and_rejects_dirty_state(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    clean = GitWorkspace.preflight(
        repo,
        target_branch="main",
        base_ref="HEAD",
        state_root=tmp_path / "state",
        run_id="RUN-1",
    )

    assert clean.repo_root == repo.resolve()
    assert clean.base_commit == git(repo, "rev-parse", "HEAD")
    (repo / "untracked.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(DirtyWorkspaceError):
        GitWorkspace.preflight(
            repo,
            target_branch="main",
            base_ref="HEAD",
            state_root=tmp_path / "other-state",
            run_id="RUN-2",
        )


def test_workspace_resolves_current_head_paths_and_run_diffs(
    tmp_path: Path,
) -> None:
    """验证当前 HEAD 文件和 Run 累计差异；参数为临时目录，无返回值。"""
    repo, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    workspace.create_worktree()
    changed = workspace.worktree_path / "src" / "review.py"
    changed.parent.mkdir()
    changed.write_text("VALUE = 1\n", encoding="utf-8")
    git(workspace.worktree_path, "add", "src/review.py")
    git(workspace.worktree_path, "commit", "-m", "add review source")
    later = workspace.worktree_path / "docs" / "later.md"
    later.parent.mkdir()
    later.write_text("later\n", encoding="utf-8")
    git(workspace.worktree_path, "add", "docs/later.md")
    git(workspace.worktree_path, "commit", "-m", "add later document")

    assert workspace.path_exists_at_head("src/review.py") is True
    assert workspace.path_exists_at_head("missing.py") is False
    assert workspace.path_changed_at_head("src/review.py") is True
    assert workspace.path_changed_at_head("docs/later.md") is True
    assert workspace.path_changed_at_head("README.md") is False


def test_preflight_initializes_non_git_directory_with_safe_baseline(
    tmp_path: Path,
) -> None:
    """验证未初始化目录自动建立基线；参数为临时目录，无返回值。"""
    repo = tmp_path / "plain-workspace"
    repo.mkdir()
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    state_root = repo / ".runtime"
    state_root.mkdir()
    (state_root / "queued.json").write_text("{}\n", encoding="utf-8")

    workspace = GitWorkspace.preflight(
        repo,
        target_branch="master",
        base_ref="master",
        state_root=state_root,
        run_id="RUN-INIT",
        use_worktree=False,
        excluded_paths=(".env",),
    )

    assert workspace.repo_root == repo.resolve()
    assert git(repo, "branch", "--show-current") == "master"
    assert git(repo, "log", "-1", "--format=%s") == (
        "[debate][supervisor] initialize workspace"
    )
    assert git(repo, "ls-files") == "app.py"
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    audit = repo / ".agent" / "debate" / workspace.run_id
    audit.mkdir(parents=True)
    (audit / "summary.md").write_text("done\n", encoding="utf-8")

    workspace.stage_audit_tree(
        f".agent/debate/{workspace.run_id}", require_only_audit=True
    )

    assert git(repo, "diff", "--cached", "--name-only") == (
        ".agent/debate/RUN-INIT/summary.md"
    )


def test_preflight_creates_baseline_for_initialized_repo_without_commits(
    tmp_path: Path,
) -> None:
    """验证 unborn branch 自动获得首个提交；参数为临时目录，无返回值。"""
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")

    workspace = GitWorkspace.preflight(
        repo,
        target_branch="main",
        base_ref="main",
        state_root=tmp_path / "state",
        run_id="RUN-UNBORN",
        use_worktree=False,
        excluded_paths=(".env",),
    )

    assert workspace.base_commit == git(repo, "rev-parse", "main")
    assert git(repo, "log", "-1", "--format=%s") == (
        "[debate][supervisor] initialize workspace"
    )
    assert git(repo, "config", "--get", "user.name")
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_repository_lock_rejects_second_active_run(tmp_path: Path) -> None:
    _, first = build_workspace(tmp_path)
    second = GitWorkspace.preflight(
        first.repo_root,
        target_branch="main",
        base_ref="HEAD",
        state_root=first.state_root,
        run_id="RUN-2",
    )

    lock_path = first.acquire_lock()

    assert lock_path.exists()
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert payload["started_at"].endswith("+00:00")
    with pytest.raises(RepositoryLockError):
        second.acquire_lock()
    first.release_lock()
    assert not lock_path.exists()


def test_repository_lock_only_recovers_dead_owner_with_explicit_approval(
    tmp_path: Path,
) -> None:
    _, first = build_workspace(tmp_path)
    second = GitWorkspace.preflight(
        first.repo_root,
        target_branch="main",
        base_ref="HEAD",
        state_root=first.state_root,
        run_id="RUN-2",
    )
    lock_path = first.acquire_lock()
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    payload["pid"] = 999_999_999
    lock_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RepositoryLockError):
        second.acquire_lock()
    second.acquire_lock(stale_recovery=lambda lock: lock["run_id"] == "RUN-1")

    recovered = json.loads(lock_path.read_text(encoding="utf-8"))
    assert recovered["run_id"] == "RUN-2"


def test_shared_worktree_and_checkpoint_commit_record_agent(tmp_path: Path) -> None:
    _, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    supervisor = Supervisor(state_root=workspace.state_root, run_id=workspace.run_id)
    supervisor.grant_write_lease(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit=workspace.head(),
        goal_contract_version=2,
    )
    (worktree / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    lease = supervisor.revoke_write_lease("TURN-1")
    validated = workspace.snapshot()

    commit_sha = workspace.commit_checkpoint(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        goal_contract_version=2,
        validation_status="failed-validation",
        summary="implement feature",
        lease=lease,
        validated_snapshot=validated,
        validated_paths=["feature.py"],
        write_policy=WritePolicy(worktree=worktree, allowed_scope=["feature.py"]),
    )

    assert commit_sha == git(worktree, "rev-parse", "HEAD")
    message = git(worktree, "show", "-s", "--format=%B", commit_sha)
    assert "[debate][codex][TURN-1][failed-validation]" in message
    assert "Debate-Agent: codex" in message
    assert "Goal-Contract: v2" in message
    assert git(worktree, "status", "--porcelain") == ""


def test_checkpoint_rejects_active_lease_or_post_validation_write(
    tmp_path: Path,
) -> None:
    _, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    supervisor = Supervisor(state_root=workspace.state_root, run_id=workspace.run_id)
    active = supervisor.grant_write_lease(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit=workspace.head(),
        goal_contract_version=1,
    )
    (worktree / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = workspace.snapshot()

    with pytest.raises(WorkspaceRaceError, match="撤销"):
        workspace.commit_checkpoint(
            agent=AgentName.CODEX,
            turn_id="TURN-1",
            goal_contract_version=1,
            validation_status="passed",
            summary="feature",
            lease=active,
            validated_snapshot=snapshot,
            validated_paths=["feature.py"],
            write_policy=WritePolicy(worktree=worktree, allowed_scope=["feature.py"]),
        )

    revoked = supervisor.revoke_write_lease("TURN-1")
    (worktree / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(WorkspaceRaceError, match="验证后"):
        workspace.commit_checkpoint(
            agent=AgentName.CODEX,
            turn_id="TURN-1",
            goal_contract_version=1,
            validation_status="passed",
            summary="feature",
            lease=revoked,
            validated_snapshot=snapshot,
            validated_paths=["feature.py"],
            write_policy=WritePolicy(worktree=worktree, allowed_scope=["feature.py"]),
        )


def test_checkpoint_does_not_stage_policy_violation(tmp_path: Path) -> None:
    _, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    supervisor = Supervisor(state_root=workspace.state_root, run_id=workspace.run_id)
    supervisor.grant_write_lease(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit=workspace.head(),
        goal_contract_version=1,
    )
    (worktree / "outside.py").write_text("VALUE = 1\n", encoding="utf-8")
    revoked = supervisor.revoke_write_lease("TURN-1")
    snapshot = workspace.snapshot()

    with pytest.raises(PolicyViolationError):
        workspace.commit_checkpoint(
            agent=AgentName.CODEX,
            turn_id="TURN-1",
            goal_contract_version=1,
            validation_status="passed",
            summary="outside scope",
            lease=revoked,
            validated_snapshot=snapshot,
            validated_paths=["outside.py"],
            write_policy=WritePolicy(worktree=worktree, allowed_scope=["src"]),
        )

    assert git(worktree, "diff", "--cached", "--name-only") == ""


def test_final_commit_has_only_base_parent_and_fast_forwards_target(
    tmp_path: Path,
) -> None:
    repo, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    supervisor = Supervisor(state_root=workspace.state_root, run_id=workspace.run_id)
    supervisor.grant_write_lease(
        agent=AgentName.CLAUDE,
        turn_id="TURN-2",
        base_commit=workspace.head(),
        goal_contract_version=1,
    )
    (worktree / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    lease = supervisor.revoke_write_lease("TURN-2")
    snapshot = workspace.snapshot()
    workspace.commit_checkpoint(
        agent=AgentName.CLAUDE,
        turn_id="TURN-2",
        goal_contract_version=1,
        validation_status="passed",
        summary="finish feature",
        lease=lease,
        validated_snapshot=snapshot,
        validated_paths=["feature.py"],
        write_policy=WritePolicy(worktree=worktree, allowed_scope=["feature.py"]),
    )
    final_tree = git(worktree, "rev-parse", "HEAD^{tree}")

    final_commit = workspace.create_integration_commit(
        final_tree,
        "feat: final result\n\nDebate-Run: RUN-1\nFinal-Tree: " + final_tree,
    )
    workspace.integrate_target(final_commit, final_tree)

    assert git(repo, "rev-parse", "HEAD") == final_commit
    assert git(repo, "rev-parse", "HEAD^{tree}") == final_tree
    assert git(repo, "rev-list", "--count", f"{workspace.base_commit}..HEAD") == "1"
    assert git(repo, "rev-list", "--parents", "-n", "1", final_commit).split() == [
        final_commit,
        workspace.base_commit,
    ]


def test_target_drift_and_unrelated_dirty_change_are_preserved(
    tmp_path: Path,
) -> None:
    """验证目标前进时重放 Run 差异并保留不重叠脏改动；参数为目录，无返回值。"""
    repo, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    supervisor = Supervisor(state_root=workspace.state_root, run_id=workspace.run_id)
    supervisor.grant_write_lease(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit=workspace.head(),
        goal_contract_version=1,
    )
    (worktree / "feature.py").write_text("VALUE = 3\n", encoding="utf-8")
    lease = supervisor.revoke_write_lease("TURN-1")
    snapshot = workspace.snapshot()
    workspace.commit_checkpoint(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        goal_contract_version=1,
        validation_status="passed",
        summary="feature",
        lease=lease,
        validated_snapshot=snapshot,
        validated_paths=["feature.py"],
        write_policy=WritePolicy(worktree=worktree, allowed_scope=["feature.py"]),
    )
    final_tree = git(worktree, "rev-parse", "HEAD^{tree}")
    final_commit = workspace.create_integration_commit(final_tree, "feat: result")
    (repo / "drift.txt").write_text("drift\n", encoding="utf-8")
    git(repo, "add", "drift.txt")
    git(repo, "commit", "-m", "drift")
    target_head = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("local dirty\n", encoding="utf-8")

    integrated_commit, integrated_tree = workspace.integrate_target(
        final_commit, final_tree
    )

    assert integrated_commit != final_commit
    assert git(repo, "rev-parse", "HEAD") == integrated_commit
    assert git(repo, "rev-parse", "HEAD^{tree}") == integrated_tree
    assert git(repo, "rev-list", "--parents", "-n", "1", integrated_commit).split() == [
        integrated_commit,
        target_head,
    ]
    assert git(repo, "show", "HEAD:drift.txt") == "drift"
    assert git(repo, "show", "HEAD:feature.py") == "VALUE = 3"
    assert (repo / "README.md").read_text(encoding="utf-8") == "local dirty\n"
    assert git(repo, "status", "--short") == "M README.md"


def test_overlapping_dirty_change_is_three_way_preserved(tmp_path: Path) -> None:
    """验证同文件的不冲突本地改动可在集成后恢复；参数为目录，无返回值。"""
    repo = create_repo(tmp_path)
    (repo / "README.md").write_text(
        "first\nkeep-1\nkeep-2\nkeep-3\nkeep-4\nsecond\n",
        encoding="utf-8",
    )
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "expand readme")
    workspace = GitWorkspace.preflight(
        repo,
        target_branch="main",
        base_ref="HEAD",
        state_root=tmp_path / "state",
        run_id="RUN-1",
    )
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    target_head = git(repo, "rev-parse", "HEAD")
    (worktree / "README.md").write_text(
        "run\nkeep-1\nkeep-2\nkeep-3\nkeep-4\nsecond\n",
        encoding="utf-8",
    )
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "run result")
    final_tree = git(worktree, "rev-parse", "HEAD^{tree}")
    final_commit = workspace.create_integration_commit(final_tree, "feat: result")
    (repo / "README.md").write_text(
        "first\nkeep-1\nkeep-2\nkeep-3\nkeep-4\nlocal\n",
        encoding="utf-8",
    )

    integrated_commit, _ = workspace.integrate_target(final_commit, final_tree)

    assert git(repo, "rev-parse", "HEAD") == integrated_commit
    assert git(repo, "rev-list", "--parents", "-n", "1", integrated_commit).split() == [
        integrated_commit,
        target_head,
    ]
    assert git(repo, "show", "HEAD:README.md") == (
        "run\nkeep-1\nkeep-2\nkeep-3\nkeep-4\nsecond"
    )
    assert (repo / "README.md").read_text(encoding="utf-8") == (
        "run\nkeep-1\nkeep-2\nkeep-3\nkeep-4\nlocal\n"
    )
    assert git(repo, "status", "--short") == "M README.md"


def test_conflicting_dirty_change_blocks_without_mutation(tmp_path: Path) -> None:
    """验证同文件冲突本地改动保持原样并阻断；参数为目录，无返回值。"""
    repo, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    (worktree / "README.md").write_text("run result\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "run result")
    final_tree = git(worktree, "rev-parse", "HEAD^{tree}")
    final_commit = workspace.create_integration_commit(final_tree, "feat: result")
    target_head = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("local result\n", encoding="utf-8")

    with pytest.raises(IntegrationBlockedError, match="本地改动.*冲突"):
        workspace.integrate_target(final_commit, final_tree)

    assert git(repo, "rev-parse", "HEAD") == target_head
    assert (repo / "README.md").read_text(encoding="utf-8") == "local result\n"
    assert git(repo, "status", "--short") == "M README.md"


def test_target_drift_conflict_blocks_integration(tmp_path: Path) -> None:
    """验证目标前进与 Run 修改冲突时保持阻断；参数为目录，无返回值。"""
    repo, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    (worktree / "README.md").write_text("run result\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "run result")
    final_tree = git(worktree, "rev-parse", "HEAD^{tree}")
    final_commit = workspace.create_integration_commit(final_tree, "feat: result")
    (repo / "README.md").write_text("target result\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "target result")
    target_head = git(repo, "rev-parse", "HEAD")

    with pytest.raises(IntegrationBlockedError, match="冲突"):
        workspace.integrate_target(final_commit, final_tree)

    assert git(repo, "rev-parse", "HEAD") == target_head
    assert (repo / "README.md").read_text(encoding="utf-8") == "target result\n"


def test_source_branch_switch_blocks_integration_without_mutating_other_branch(
    tmp_path: Path,
) -> None:
    repo, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    (worktree / "feature.py").write_text("VALUE = 4\n", encoding="utf-8")
    git(worktree, "add", "feature.py")
    git(worktree, "commit", "-m", "temporary result")
    final_tree = git(worktree, "rev-parse", "HEAD^{tree}")
    final_commit = workspace.create_integration_commit(final_tree, "feat: result")
    git(repo, "branch", "other", workspace.base_commit)
    git(repo, "switch", "other")

    with pytest.raises(IntegrationBlockedError, match="不在目标分支"):
        workspace.integrate_target(final_commit, final_tree)

    assert git(repo, "rev-parse", "HEAD") == workspace.base_commit
    assert git(repo, "rev-parse", "main") == workspace.base_commit


def test_cleanup_removes_temporary_worktree_and_branch(tmp_path: Path) -> None:
    repo, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()

    workspace.cleanup_success()

    assert not worktree.exists()
    assert git(repo, "branch", "--list", workspace.debate_branch) == ""
    assert not workspace.lock_path.exists()


def test_direct_workspace_mode_squashes_result_and_restores_target_branch(
    tmp_path: Path,
) -> None:
    """验证无 worktree 模式仍隔离临时提交；参数为临时目录，无返回值。"""
    repo = create_repo(tmp_path)
    workspace = GitWorkspace.preflight(
        repo,
        target_branch="main",
        base_ref="HEAD",
        state_root=tmp_path / "state",
        run_id="RUN-DIRECT",
        use_worktree=False,
    )
    workspace.acquire_lock()

    direct = workspace.create_worktree()

    assert direct == repo.resolve()
    assert git(repo, "branch", "--show-current") == workspace.debate_branch
    supervisor = Supervisor(state_root=workspace.state_root, run_id=workspace.run_id)
    supervisor.grant_write_lease(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit=workspace.head(),
        goal_contract_version=1,
    )
    (repo / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    lease = supervisor.revoke_write_lease("TURN-1")
    snapshot = workspace.snapshot()
    workspace.commit_checkpoint(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        goal_contract_version=1,
        validation_status="passed",
        summary="direct feature",
        lease=lease,
        validated_snapshot=snapshot,
        validated_paths=["feature.py"],
        write_policy=WritePolicy(worktree=repo, allowed_scope=["."]),
    )
    audit = repo / ".agent" / "debate" / workspace.run_id
    audit.mkdir(parents=True)
    (audit / "summary.md").write_text("done\n", encoding="utf-8")
    final_tree = workspace.stage_audit_tree(
        f".agent/debate/{workspace.run_id}", require_only_audit=True
    )

    final_commit = workspace.create_integration_commit(
        final_tree,
        "feat: direct result",
    )
    workspace.integrate_target(final_commit, final_tree)

    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-parse", "HEAD") == final_commit
    assert git(repo, "rev-list", "--count", f"{workspace.base_commit}..HEAD") == "1"
    assert (repo / "feature.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (repo / ".agent/debate/RUN-DIRECT/summary.md").exists()

    workspace.cleanup_success()

    assert git(repo, "branch", "--list", workspace.debate_branch) == ""
    assert not workspace.lock_path.exists()


def test_discard_supervisor_audit_preserves_product_changes(tmp_path: Path) -> None:
    """验证恢复仅撤销固定审计文件；参数为临时目录，无返回值。"""
    _, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    audit = worktree / ".agent" / "debate" / workspace.run_id
    audit.mkdir(parents=True)
    expected = ("final-summary.md", "validation-report.md")
    for name in expected:
        (audit / name).write_text("failed\n", encoding="utf-8")
    workspace.stage_audit_tree(
        f".agent/debate/{workspace.run_id}", require_only_audit=False
    )
    (worktree / "product.txt").write_text("keep\n", encoding="utf-8")

    removed = workspace.discard_supervisor_audit(expected)

    assert removed is True
    assert not audit.exists()
    assert workspace.changed_paths() == ["product.txt"]


def test_discard_supervisor_audit_rejects_extra_file(tmp_path: Path) -> None:
    """验证审计目录混入额外文件时保持阻断；参数为临时目录，无返回值。"""
    _, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    audit = worktree / ".agent" / "debate" / workspace.run_id
    audit.mkdir(parents=True)
    (audit / "unexpected.txt").write_text("unknown\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceError, match="额外文件"):
        workspace.discard_supervisor_audit(("final-summary.md",))

    assert (audit / "unexpected.txt").exists()


def test_discard_supervisor_audit_rewinds_audit_only_commit(tmp_path: Path) -> None:
    """验证恢复可撤销误入 HEAD 的纯审计提交；参数为临时目录，无返回值。"""
    _, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    expected_head = workspace.head()
    audit = worktree / ".agent" / "debate" / workspace.run_id
    audit.mkdir(parents=True)
    expected = ("final-summary.md", "validation-report.md")
    for name in expected:
        (audit / name).write_text("failed\n", encoding="utf-8")
    git(worktree, "add", ".agent")
    git(worktree, "commit", "-m", "unexpected audit commit")

    removed = workspace.discard_supervisor_audit(
        expected,
        expected_head=expected_head,
    )

    assert removed is True
    assert workspace.head() == expected_head
    assert not audit.exists()
    assert workspace.changed_paths() == []


def test_discard_supervisor_audit_does_not_rewind_product_commit(
    tmp_path: Path,
) -> None:
    """验证恢复点后混入产品提交时拒绝回退；参数为临时目录，无返回值。"""
    _, workspace = build_workspace(tmp_path)
    workspace.acquire_lock()
    worktree = workspace.create_worktree()
    expected_head = workspace.head()
    audit = worktree / ".agent" / "debate" / workspace.run_id
    audit.mkdir(parents=True)
    (audit / "final-summary.md").write_text("failed\n", encoding="utf-8")
    (worktree / "product.txt").write_text("keep\n", encoding="utf-8")
    git(worktree, "add", ".agent", "product.txt")
    git(worktree, "commit", "-m", "mixed commit")
    current_head = workspace.head()

    with pytest.raises(GitWorkspaceError, match="非 Supervisor"):
        workspace.discard_supervisor_audit(
            ("final-summary.md",),
            expected_head=expected_head,
        )

    assert workspace.head() == current_head
    assert (worktree / "product.txt").exists()
