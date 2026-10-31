from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from debate_agent.cli_health import (
    CliCommandResult,
    CliHealthChecker,
    CliPreflightError,
    rebuild_openclaw_resume_config,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import AgentName


class OpenClawHealthExecutor:
    """返回 OpenClaw 预检的确定性结果。"""

    def __init__(self, *, version: str = "2026.7.1") -> None:
        """创建执行器；参数为版本，返回实例，不执行外部命令。"""
        self.version = version

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> CliCommandResult:
        """返回命令阶段结果；参数为健康上下文，返回模拟结果。"""
        command = tuple(argv)
        if command[-1] == "--version":
            return CliCommandResult(
                return_code=0,
                stdout=f"OpenClaw {self.version}",
                stderr="",
            )
        if command[-2:] == ("agent", "--help"):
            return CliCommandResult(
                return_code=0,
                stdout=(
                    "--message-file --session-key --json --timeout "
                    "--model --thinking"
                ),
                stderr="",
            )
        if command[-3:] == ("gateway", "status", "--json"):
            return CliCommandResult(
                return_code=0,
                stdout=(
                    '{"rpc":{"ok":true},'
                    '"config":{"cli":"configured","daemon":"configured"}}'
                ),
                stderr="",
            )
        if "agent" in command and "--json" in command:
            return CliCommandResult(
                return_code=0,
                stdout=(
                    '{"status":"ok","result":'
                    '{"payloads":[{"text":"{\\"ok\\":true}"}]}}'
                ),
                stderr="",
            )
        raise AssertionError(f"未知 OpenClaw 健康命令: {command[1:]}")


def _checker(tmp_path: Path, *, version: str = "2026.7.1") -> CliHealthChecker:
    """创建 OpenClaw Checker；参数为目录/版本，返回预检器。"""
    return CliHealthChecker(
        executor=OpenClawHealthExecutor(version=version),
        which=lambda _command: "/opt/tools/openclaw",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )


def test_openclaw_health_freezes_gateway_profile_model_thinking_and_hash(
    tmp_path: Path,
) -> None:
    """验证 OpenClaw 健康身份包含配置摘要；参数为目录，无返回值。"""
    profile = AgentProfile(
        command="openclaw",
        model="deepseek/deepseek-v4-pro",
        variant="off",
        native_profile="main",
    )

    report = _checker(tmp_path).check(
        agent=AgentName.OPENCLAW,
        profile=profile,
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is True
    assert report.identity is not None
    assert report.identity.model == "deepseek/deepseek-v4-pro"
    assert report.identity.variant == "off"
    assert report.identity.runtime_config_hash is not None
    assert report.by_key("authentication").level == "success"
    assert report.by_key("probe").level == "success"


def test_openclaw_resume_rechecks_frozen_identity_and_blocks_version_drift(
    tmp_path: Path,
) -> None:
    """验证恢复重新预检且拒绝版本漂移；参数为目录，无返回值。"""
    initial = AgentProfile(
        command="openclaw",
        model="deepseek/deepseek-v4-pro",
        variant="off",
        native_profile="main",
    )
    report = _checker(tmp_path).check(
        agent=AgentName.OPENCLAW,
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
            "variant": identity.variant,
            "runtime_config_hash": identity.runtime_config_hash,
        }
    )
    config = RunConfig(
        workspace=tmp_path,
        topic="恢复 OpenClaw",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCLAW),
        profiles={
            AgentName.CODEX: AgentProfile(command="codex"),
            AgentName.OPENCLAW: frozen,
        },
    )

    assert (
        rebuild_openclaw_resume_config(
            config,
            checker=_checker(tmp_path),
            environment={"PATH": "/opt/tools"},
        )
        == config
    )
    with pytest.raises(CliPreflightError, match="漂移"):
        rebuild_openclaw_resume_config(
            config,
            checker=_checker(tmp_path, version="2026.7.2"),
            environment={"PATH": "/opt/tools"},
        )
