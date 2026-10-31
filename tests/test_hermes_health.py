from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from debate_agent.cli_health import (
    CliCommandResult,
    CliHealthChecker,
    CliPreflightError,
    rebuild_hermes_resume_config,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import AgentName


class HermesHealthExecutor:
    """返回 Hermes 专属健康阶段结果并记录环境。"""

    def __init__(self, *, authenticated: bool = True) -> None:
        """创建执行器；参数为认证状态，返回实例。"""
        self.authenticated = authenticated
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> CliCommandResult:
        """返回模拟结果；参数为健康命令上下文，返回阶段输出。"""
        command = tuple(argv)
        self.calls.append((command, dict(env)))
        if command[-1] == "--version":
            return CliCommandResult(
                return_code=0,
                stdout="Hermes Agent v0.19.0",
                stderr="",
            )
        if command[-2:] == ("chat", "--help"):
            return CliCommandResult(
                return_code=0,
                stdout=(
                    "--provider --model --resume --no-restore-cwd "
                    "--yolo --quiet"
                ),
                stderr="",
            )
        if command[-1] == "status":
            return CliCommandResult(return_code=0, stdout="configured", stderr="")
        if "--query" in command:
            return CliCommandResult(
                return_code=0,
                stdout=(
                    "\nHTTP 401: invalid token\n"
                    if not self.authenticated
                    else '{"ok":true}\n'
                ),
                stderr=(
                    ""
                    if not self.authenticated
                    else "\nsession_id: 20260726_130800_45de97\n"
                ),
            )
        raise AssertionError(f"未知 Hermes 健康命令: {command[1:]}")


def test_hermes_health_uses_real_query_scoped_key_and_redacted_evidence(
    tmp_path: Path,
) -> None:
    """验证 Hermes 预检真实 Query、密钥隔离和 argv 脱敏；参数为目录，无返回值。"""
    executor = HermesHealthExecutor()
    secret = "deepseek-secret-value"
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/hermes",
        environment={"PATH": "/opt/tools", "DEEPSEEK_API_KEY": secret},
        probe_root=tmp_path / "health",
    )

    report = checker.check(
        agent=AgentName.HERMES,
        profile=AgentProfile(
            command="hermes",
            provider="deepseek",
            model="deepseek-v4-flash",
        ),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is True
    assert report.identity is not None
    assert report.identity.model == "deepseek-v4-flash"
    assert report.identity.runtime_config_hash is not None
    probe_call = next(call for call in executor.calls if "--query" in call[0])
    assert probe_call[1]["DEEPSEEK_API_KEY"] == secret
    serialized_evidence = report.model_dump_json()
    assert secret not in serialized_evidence
    assert "不要调用工具" not in serialized_evidence
    assert "<redacted>" in serialized_evidence


def test_hermes_health_rejects_http_401_even_when_exit_code_is_zero(
    tmp_path: Path,
) -> None:
    """验证 Hermes 认证正文错误不会被零退出码伪装成功；参数为目录，无返回值。"""
    checker = CliHealthChecker(
        executor=HermesHealthExecutor(authenticated=False),
        which=lambda _command: "/opt/tools/hermes",
        environment={
            "PATH": "/opt/tools",
            "DEEPSEEK_API_KEY": "expired",
        },
        probe_root=tmp_path / "health",
    )

    report = checker.check(
        agent=AgentName.HERMES,
        profile=AgentProfile(
            command="hermes",
            provider="deepseek",
            model="deepseek-v4-flash",
        ),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is False
    assert report.by_key("probe").category is not None
    assert report.by_key("probe").category.value == "authentication_error"


def test_hermes_resume_rechecks_frozen_provider_model_and_version(
    tmp_path: Path,
) -> None:
    """验证 Hermes 恢复预检并拒绝版本漂移；参数为目录，无返回值。"""
    checker = CliHealthChecker(
        executor=HermesHealthExecutor(),
        which=lambda _command: "/opt/tools/hermes",
        environment={
            "PATH": "/opt/tools",
            "DEEPSEEK_API_KEY": "valid",
        },
        probe_root=tmp_path / "health",
    )
    initial = AgentProfile(
        command="hermes",
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    report = checker.check(
        agent=AgentName.HERMES,
        profile=initial,
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )
    assert report.identity is not None
    identity = report.identity
    frozen = initial.model_copy(
        update={
            "command": identity.executable,
            "version": identity.version,
            "model": identity.model,
            "runtime_config_hash": identity.runtime_config_hash,
        }
    )
    config = RunConfig(
        workspace=tmp_path,
        topic="恢复 Hermes",
        state_root=tmp_path / "state",
        participants=(AgentName.CLAUDE, AgentName.HERMES),
        first_agent=AgentName.CLAUDE,
        profiles={
            AgentName.CLAUDE: AgentProfile(command="claude"),
            AgentName.HERMES: frozen,
        },
    )

    assert (
        rebuild_hermes_resume_config(
            config,
            checker=checker,
            environment={"DEEPSEEK_API_KEY": "valid"},
        )
        == config
    )
    drift_checker = CliHealthChecker(
        executor=HermesHealthExecutor(),
        which=lambda _command: "/different/hermes",
        environment={
            "PATH": "/opt/tools",
            "DEEPSEEK_API_KEY": "valid",
        },
        probe_root=tmp_path / "drift",
    )
    with pytest.raises(CliPreflightError, match="漂移"):
        rebuild_hermes_resume_config(
            config,
            checker=drift_checker,
            environment={"DEEPSEEK_API_KEY": "valid"},
        )
