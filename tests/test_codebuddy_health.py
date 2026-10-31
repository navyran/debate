from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from debate_agent.cli_health import (
    CLI_DEFAULT_MODEL,
    CliCommandResult,
    CliHealthChecker,
    CliPreflightError,
    CodeBuddyCliHealthStrategy,
    rebuild_codebuddy_resume_config,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import AgentName


class CodeBuddyHealthExecutor:
    """返回 CodeBuddy 专属健康阶段结果并记录环境。"""

    def __init__(
        self,
        *,
        session_id: str = "codebuddy-session",
        resume_session_id: str | None = None,
        authenticated: bool = True,
        transient_probe_failures: int = 0,
    ) -> None:
        """创建执行器。

        参数为首轮/恢复 Session、认证状态和瞬时探针失败次数；返回实例。
        """
        self.session_id = session_id
        self.resume_session_id = resume_session_id or session_id
        self.authenticated = authenticated
        self.transient_probe_failures = transient_probe_failures
        self.calls: list[
            tuple[tuple[str, ...], str, Path, dict[str, str]]
        ] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> CliCommandResult:
        """返回模拟结果；参数为命令上下文，返回阶段输出，不执行外部进程。"""
        del timeout_seconds
        command = tuple(argv)
        self.calls.append((command, stdin, cwd, dict(env)))
        if command[-1] == "--version":
            return CliCommandResult(
                return_code=0,
                stdout="CodeBuddy Code 2.125.4",
                stderr="",
            )
        if command[-1] == "--help":
            return CliCommandResult(
                return_code=0,
                stdout=(
                    "-p, --print --output-format --json-schema --resume --model "
                    "--agent --tools --setting-sources --strict-mcp-config "
                    "--mcp-config --dangerously-skip-permissions"
                ),
                stderr="",
            )
        if command[1:3] == ("mcp", "list"):
            return CliCommandResult(return_code=0, stdout="", stderr="")
        if "-p" in command:
            if not self.authenticated:
                return CliCommandResult(
                    return_code=1,
                    stdout="",
                    stderr="HTTP 401 unauthorized",
                )
            if self.transient_probe_failures > 0:
                self.transient_probe_failures -= 1
                return CliCommandResult(
                    return_code=1,
                    stdout="",
                    stderr="temporary process failure",
                )
            session_id = (
                self.resume_session_id if "--resume" in command else self.session_id
            )
            return CliCommandResult(
                return_code=0,
                stdout=json.dumps(
                    {
                        "session_id": session_id,
                        "structured_output": {"ok": True},
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"未知 CodeBuddy 健康命令: {command[1:]}")


def test_codebuddy_health_uses_two_turn_session_probe_and_scoped_credentials(
    tmp_path: Path,
) -> None:
    """验证 CodeBuddy 预检双轮复用、隔离环境和凭据脱敏；参数为目录，无返回值。"""
    secret = "codebuddy-secret-value"
    executor = CodeBuddyHealthExecutor()
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/codebuddy",
        environment={
            "PATH": "/opt/tools",
            "CODEBUDDY_API_KEY": secret,
            "CODEBUDDY_INTERNET_ENVIRONMENT": "internal",
            "UNRELATED_SECRET": "must-not-pass",
        },
        probe_root=tmp_path / "health",
    )

    report = checker.check(
        agent=AgentName.CODEBUDDY,
        profile=AgentProfile(
            command="codebuddy",
            model="gpt-5",
            native_profile="reviewer",
        ),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is True
    assert report.identity is not None
    assert report.identity.model == "gpt-5"
    assert report.identity.runtime_config_hash is not None
    probe_calls = [call for call in executor.calls if "-p" in call[0]]
    assert len(probe_calls) == 2
    assert "--resume" not in probe_calls[0][0]
    assert probe_calls[1][0][probe_calls[1][0].index("--resume") + 1] == (
        "codebuddy-session"
    )
    assert all(call[2] != tmp_path for call in probe_calls)
    assert all(call[3]["CODEBUDDY_API_KEY"] == secret for call in probe_calls)
    assert all(
        call[3]["CODEBUDDY_INTERNET_ENVIRONMENT"] == "internal"
        for call in probe_calls
    )
    assert all("StructuredOutput 工具" not in call[1] for call in probe_calls)
    assert all('{"ok":true}' in call[1] for call in probe_calls)
    assert all("UNRELATED_SECRET" not in call[3] for call in probe_calls)
    assert secret not in report.model_dump_json()
    assert "codebuddy-session" not in report.model_dump_json()
    assert "<redacted-session>" in report.model_dump_json()


def test_codebuddy_health_uses_cli_default_without_model_argument(
    tmp_path: Path,
) -> None:
    """验证 CodeBuddy 默认模型由 CLI 选择且预检不传模型参数；参数为目录，无返回值。"""
    executor = CodeBuddyHealthExecutor()
    report = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/codebuddy",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    ).check(
        agent=AgentName.CODEBUDDY,
        profile=AgentProfile(command="codebuddy", model=CLI_DEFAULT_MODEL),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is True
    assert report.identity is not None
    assert report.identity.model == CLI_DEFAULT_MODEL
    probe_calls = [call for call in executor.calls if "-p" in call[0]]
    assert len(probe_calls) == 2
    assert all("--model" not in call[0] for call in probe_calls)


def test_codebuddy_health_retries_transient_process_failure(tmp_path: Path) -> None:
    """验证 CodeBuddy 健康探针会重试瞬时进程错误。"""
    executor = CodeBuddyHealthExecutor(transient_probe_failures=1)
    report = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/codebuddy",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    ).check(
        agent=AgentName.CODEBUDDY,
        profile=AgentProfile(command="codebuddy", model="kimi-k3-1"),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is True
    assert len([call for call in executor.calls if "-p" in call[0]]) == 3


def test_codebuddy_health_accepts_current_event_array_terminal() -> None:
    """验证健康探针兼容新版 CodeBuddy 事件数组终局；无参数，无返回值。"""
    result = CliCommandResult(
        return_code=0,
        stdout=json.dumps(
            [
                {"type": "message", "sessionId": "codebuddy-session"},
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "codebuddy-session",
                    "result": (
                        '<StructuredOutput>\n{"ok": true}\n</StructuredOutput>'
                    ),
                },
            ]
        ),
        stderr="",
    )

    assert CodeBuddyCliHealthStrategy._probe_payload(result) == (
        "codebuddy-session",
        {"ok": True},
    )
    tool_call_result = result.model_copy(
        update={
            "stdout": json.dumps(
                [
                    {
                        "type": "result",
                        "subtype": "success",
                        "session_id": "codebuddy-session",
                        "result": (
                            "<tool_calls:abc123>\n"
                            "<tool_call:abc123>StructuredOutput\n"
                            '{"ok":true}'
                        ),
                    }
                ]
            )
        }
    )
    assert CodeBuddyCliHealthStrategy._probe_payload(tool_call_result) == (
        "codebuddy-session",
        {"ok": True},
    )


def test_codebuddy_health_rejects_auth_failure_and_resume_session_drift(
    tmp_path: Path,
) -> None:
    """验证 CodeBuddy 认证失败和恢复串线均阻断；参数为目录，无返回值。"""

    def check(executor: CodeBuddyHealthExecutor):
        """执行一次 CodeBuddy 健康检查；参数为执行器，返回报告。"""
        return CliHealthChecker(
            executor=executor,
            which=lambda _command: "/opt/tools/codebuddy",
            environment={"PATH": "/opt/tools"},
            probe_root=tmp_path / f"health-{len(executor.calls)}",
        ).check(
            agent=AgentName.CODEBUDDY,
            profile=AgentProfile(command="codebuddy", model="gpt-5"),
            workspace=tmp_path,
            mcp_mode="minimal",
            proxy_environment_allowlist=(),
        )

    unauthorized = check(CodeBuddyHealthExecutor(authenticated=False))
    drift = check(
        CodeBuddyHealthExecutor(resume_session_id="different-session")
    )

    assert unauthorized.can_start is False
    assert unauthorized.by_key("probe").category is not None
    assert unauthorized.by_key("probe").category.value == "authentication_error"
    assert drift.can_start is False
    assert drift.by_key("session_resume").category is not None
    assert drift.by_key("session_resume").category.value == "protocol_error"


def test_codebuddy_resume_rechecks_frozen_identity(tmp_path: Path) -> None:
    """验证 CodeBuddy 恢复预检并拒绝版本或路径漂移；参数为目录，无返回值。"""
    executor = CodeBuddyHealthExecutor()
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/codebuddy",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )
    initial = AgentProfile(
        command="codebuddy",
        model="gpt-5",
        native_profile="reviewer",
    )
    report = checker.check(
        agent=AgentName.CODEBUDDY,
        profile=initial,
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )
    assert report.identity is not None
    frozen = initial.model_copy(
        update={
            "command": report.identity.executable,
            "version": report.identity.version,
            "model": report.identity.model,
            "runtime_config_hash": report.identity.runtime_config_hash,
        }
    )
    config = RunConfig(
        workspace=tmp_path,
        topic="恢复 CodeBuddy",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.CODEBUDDY),
        profiles={
            AgentName.CODEX: AgentProfile(command="codex"),
            AgentName.CODEBUDDY: frozen,
        },
    )

    assert (
        rebuild_codebuddy_resume_config(
            config,
            checker=checker,
            environment={"PATH": "/opt/tools"},
        )
        == config
    )
    drift_checker = CliHealthChecker(
        executor=CodeBuddyHealthExecutor(),
        which=lambda _command: "/different/codebuddy",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "drift",
    )
    with pytest.raises(CliPreflightError, match="漂移"):
        rebuild_codebuddy_resume_config(
            config,
            checker=drift_checker,
            environment={"PATH": "/opt/tools"},
        )
