from __future__ import annotations

from pathlib import Path

import pytest

from debate_agent.policy import CommandPolicy, PolicyViolationError, WritePolicy


def test_command_policy_allows_readonly_git_and_configured_validation() -> None:
    policy = CommandPolicy(
        approved_commands=[("python", "-m", "pytest"), ("ruff", "check")],
        environment_allowlist=["PATH", "LANG"],
    )

    policy.validate(["git", "status", "--short"])
    policy.validate(["git", "diff", "HEAD"])
    policy.validate(["python", "-m", "pytest", "tests/test_api.py"])
    with pytest.raises(PolicyViolationError, match="Git 状态变更"):
        policy.validate(["git", "commit", "-m", "forbidden"])
    with pytest.raises(PolicyViolationError, match="未批准命令"):
        policy.validate(["curl", "https://example.com"])
    assert policy.filter_environment(
        {"PATH": "/bin", "LANG": "zh_CN.UTF-8", "TOKEN": "secret"}
    ) == {"PATH": "/bin", "LANG": "zh_CN.UTF-8"}


def test_network_commands_require_both_configuration_and_approval() -> None:
    offline = CommandPolicy(approved_commands=[("curl",)])
    with pytest.raises(PolicyViolationError, match="联网"):
        offline.validate(["curl", "https://example.com"])

    online = CommandPolicy(
        approved_commands=[("curl", "https://example.com")],
        network_enabled=True,
    )
    assert online.validate(["curl", "https://example.com"]) == (
        "curl",
        "https://example.com",
    )


def test_unrestricted_command_policy_accepts_arbitrary_workspace_commands() -> None:
    """验证可信工作区模式不限制命令类型或联网工具。"""
    policy = CommandPolicy(unrestricted_commands=True)

    assert policy.validate(["sh", "-c", "rg TODO ."]) == (
        "sh",
        "-c",
        "rg TODO .",
    )
    assert policy.validate(["git", "commit", "-m", "agent checkpoint"])[1] == (
        "commit"
    )
    assert policy.validate(["curl", "https://example.com"])[0] == "curl"
    assert "Bash" in policy.claude_allowed_tools()
    assert not any(rule.startswith("Bash(") for rule in policy.claude_allowed_tools())


def test_write_policy_accepts_allowed_paths_and_rejects_protected_paths(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    policy = WritePolicy(
        worktree=worktree,
        allowed_scope=["src", "tests"],
        protected_scope=["src/secret.py"],
    )

    assert policy.validate_paths(["src/app.py"]) == ["src/app.py"]
    with pytest.raises(PolicyViolationError, match="保护范围"):
        policy.validate_paths(["src/secret.py"])
    with pytest.raises(PolicyViolationError, match="允许范围"):
        policy.validate_paths(["README.md"])
    with pytest.raises(PolicyViolationError, match="敏感路径"):
        policy.validate_paths([".env"])
    with pytest.raises(PolicyViolationError, match="审计目录"):
        policy.validate_paths([".agent/debate/RUN-1/final-summary.md"])
    with pytest.raises(PolicyViolationError, match="审计目录"):
        policy.validate_paths([".agent/discuss/RUN-1/final-summary.md"])


def test_write_policy_rejects_symlink_escape_and_nested_repository(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (worktree / "escape").symlink_to(external, target_is_directory=True)
    nested = worktree / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    (nested / "file.py").write_text("x\n", encoding="utf-8")
    policy = WritePolicy(worktree=worktree, allowed_scope=["."])

    with pytest.raises(PolicyViolationError, match="worktree 之外"):
        policy.validate_paths(["escape/file.txt"])
    with pytest.raises(PolicyViolationError, match="嵌套仓库"):
        policy.validate_paths(["nested/file.py"])


def test_high_impact_paths_require_explicit_scope(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    policy = WritePolicy(worktree=worktree, allowed_scope=["."])

    with pytest.raises(PolicyViolationError, match="高影响"):
        policy.validate_paths(["pyproject.toml"])

    explicit = WritePolicy(
        worktree=worktree,
        allowed_scope=["."],
        high_impact_scope=["pyproject.toml"],
    )
    assert explicit.validate_paths(["pyproject.toml"]) == ["pyproject.toml"]
