"""test_opencode_health.py 的共享测试桩与断言辅助函数。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from debate_agent.cli_health import (
    CliCommandResult,
    CliHealthChecker,
    CliHealthReport,
    CliIdentity,
    freeze_run_config,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import AgentName

HELP_FLAGS = (
    "--format --dir --model --agent --auto --pure --session --variant"
)

class OpenCodeExecutor:
    """按 OpenCode 预检阶段返回可控结果并记录安全调用上下文。"""

    def __init__(
        self,
        *,
        initial_config: Mapping[str, object] | None = None,
        final_config: Mapping[str, object] | None = None,
        help_text: str = HELP_FLAGS,
        auth_output: str = "│  ℹ provider-x api",
        initial_raw: str | None = None,
        final_raw: str | None = None,
        mcp_ok: bool = True,
        mcp_stdout: str | None = None,
        mcp_stderr: str = "",
        probe_stdout: str | None = None,
        probe_return_code: int = 0,
        probe_stderr: str = "",
        cleanup_ok: bool = True,
        cleanup_raises: bool = False,
        cleanup_error: type[Exception] = OSError,
        raise_stage: str | None = None,
        stage_error: type[Exception] = OSError,
        version_output: str = "opencode 1.18.3",
    ) -> None:
        """创建执行器；参数为阶段配置/help/auth/MCP；返回实例；不执行外部命令。"""
        self.initial_config = dict(
            initial_config or {"model": "provider-x/model-y", "mcp": {}}
        )
        self.final_config = dict(
            final_config
            or {
                "model": "provider-x/model-y",
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "mcp": {},
            }
        )
        self.help_text = help_text
        self.auth_output = auth_output
        self.initial_raw = initial_raw
        self.final_raw = final_raw
        self.mcp_ok = mcp_ok
        self.mcp_stdout = (
            mcp_stdout
            if mcp_stdout is not None
            else self._mcp_list_stdout(self.final_config)
        )
        self.mcp_stderr = mcp_stderr
        self.probe_stdout = probe_stdout
        self.probe_return_code = probe_return_code
        self.probe_stderr = probe_stderr
        self.cleanup_ok = cleanup_ok
        self.cleanup_raises = cleanup_raises
        self.cleanup_error = cleanup_error
        self.raise_stage = raise_stage
        self.stage_error = stage_error
        self.version_output = version_output
        self.probe_git_observed = False
        self.calls: list[
            tuple[tuple[str, ...], Path, str, dict[str, str]]
        ] = []
        self.debug_calls = 0

    @staticmethod
    def _mcp_list_stdout(config: Mapping[str, object]) -> str:
        """生成受控 MCP list 输出；参数为最终配置；返回不含凭据的模拟文本。"""
        mcp = config.get("mcp", {})
        if not isinstance(mcp, Mapping) or not mcp:
            return "▲  No MCP servers configured\n"
        lines = ["┌  MCP Servers", "│"]
        for name, value in mcp.items():
            disabled = isinstance(value, Mapping) and value.get("enabled") is False
            icon = "○" if disabled else "✓"
            status = "disabled" if disabled else "connected"
            lines.append(f"│  ℹ  {icon} {name} {status}")
        lines.extend(("│", f"└  {len(mcp)} server(s)"))
        return "\n".join(lines)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> CliCommandResult:
        """返回阶段结果；参数为命令上下文；返回模拟结果；未知命令断言失败。"""
        command = tuple(argv)
        self.calls.append((command, cwd, stdin, dict(env)))
        tail = command[1:]
        if tail == ("--version",):
            if self.raise_stage == "version":
                raise self.stage_error("sensitive version launch detail")
            return CliCommandResult(
                return_code=0, stdout=self.version_output, stderr=""
            )
        if tail == ("run", "--help"):
            if self.raise_stage == "help":
                raise self.stage_error("sensitive help launch detail")
            return CliCommandResult(return_code=0, stdout=self.help_text, stderr="")
        if tail == ("debug", "config", "--pure"):
            stage = "initial_debug" if self.debug_calls == 0 else "final_debug"
            if self.raise_stage == stage:
                raise self.stage_error(f"sensitive {stage} launch detail")
            payload = (
                self.initial_config
                if self.debug_calls == 0
                else self.final_config
            )
            raw = self.initial_raw if self.debug_calls == 0 else self.final_raw
            self.debug_calls += 1
            return CliCommandResult(
                return_code=0,
                stdout=raw if raw is not None else json.dumps(payload),
                stderr="",
            )
        if tail == ("auth", "list", "--pure"):
            if self.raise_stage == "auth":
                raise self.stage_error("sensitive auth launch detail")
            return CliCommandResult(
                return_code=0,
                stdout=self.auth_output,
                stderr="",
            )
        if tail[:1] == ("run",):
            if self.raise_stage == "probe":
                raise self.stage_error("sensitive probe launch detail")
            self.probe_git_observed = (cwd / ".git").is_dir()
            stdout = self.probe_stdout
            if stdout is None:
                stdout = json.dumps(
                    {
                        "type": "text",
                        "sessionID": "test-session",
                        "text": '{"ok":true}',
                    }
                )
            return CliCommandResult(
                return_code=self.probe_return_code,
                stdout=stdout,
                stderr=self.probe_stderr,
            )
        if tail[:2] == ("session", "delete"):
            if self.cleanup_raises:
                raise self.cleanup_error("sensitive cleanup launch detail")
            return CliCommandResult(
                return_code=0 if self.cleanup_ok else 1,
                stdout="",
                stderr="" if self.cleanup_ok else "cleanup failed",
            )
        if tail == ("mcp", "list", "--pure"):
            if self.raise_stage == "mcp":
                raise self.stage_error("sensitive mcp launch detail")
            return CliCommandResult(
                return_code=0 if self.mcp_ok else 1,
                stdout=self.mcp_stdout,
                stderr=self.mcp_stderr or ("" if self.mcp_ok else "optional failure"),
            )
        raise AssertionError(f"未覆盖的测试命令: {tail!r}")

def check_opencode(
    tmp_path: Path,
    executor: OpenCodeExecutor,
    *,
    model: str | None = None,
    variant: str | None = None,
    mcp_mode: str = "minimal",
):
    """执行 OpenCode 测试预检；参数为目录/执行器/Profile；返回报告；错误由断言呈现。"""
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/opencode",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "probes",
    )
    return checker.check(
        agent=AgentName.OPENCODE,
        profile=AgentProfile(
            command="opencode",
            model=model,
            variant=variant,
        ),
        workspace=tmp_path,
        mcp_mode=mcp_mode,
        proxy_environment_allowlist=(),
    )

def freeze_opencode_profile(
    tmp_path: Path,
    executor: OpenCodeExecutor,
    *,
    mcp_mode: str = "minimal",
) -> tuple[AgentProfile, CliHealthReport]:
    """冻结测试 Profile；参数为目录/执行器/模式；返回 Profile/报告；失败由断言呈现。"""
    report = check_opencode(tmp_path, executor, mcp_mode=mcp_mode)
    codex_identity = CliIdentity(
        agent=AgentName.CODEX,
        executable="/opt/tools/codex",
        version="1.2.3",
        model="gpt-test",
    )
    codex_report = CliHealthReport(
        agent=AgentName.CODEX,
        checked_at=report.checked_at,
        identity=codex_identity,
        checks=(),
        can_start=True,
        enabled_environment_names=("PATH",),
    )
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        profiles={
            AgentName.CODEX: AgentProfile(command="codex", model="gpt-test"),
            AgentName.OPENCODE: AgentProfile(command="opencode"),
        },
    )
    frozen = freeze_run_config(
        config,
        reports={AgentName.CODEX: codex_report, AgentName.OPENCODE: report},
    )
    return frozen.profiles[AgentName.OPENCODE], report

def command_tails(executor: OpenCodeExecutor) -> list[tuple[str, ...]]:
    """提取命令尾部；参数为测试执行器；返回不含绝对命令的顺序；不抛异常。"""
    return [argv[1:] for argv, _cwd, _stdin, _env in executor.calls]
