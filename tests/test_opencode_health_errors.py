"""test_opencode_health.py 按职责拆分后的测试集合。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opencode_health_support import (
    OpenCodeExecutor,
    check_opencode,
    command_tails,
)

import debate_agent.health_opencode as cli_health_module
from debate_agent.cli_health import (
    CliHealthChecker,
    FailureCategory,
    OpenCodeHealthStrategy,
)
from debate_agent.config import AgentProfile
from debate_agent.models import AgentName


def test_opencode_does_not_swallow_programming_errors(tmp_path: Path) -> None:
    executor = OpenCodeExecutor(raise_stage="help", stage_error=RuntimeError)

    with pytest.raises(RuntimeError, match="sensitive help"):
        check_opencode(tmp_path, executor)

def test_opencode_does_not_swallow_probe_parser_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_parser(_stdout: str) -> tuple[tuple[str, ...], str, bool, bool, bool]:
        raise ValueError("programming parser failure")

    monkeypatch.setattr(
        OpenCodeHealthStrategy,
        "_probe_protocol",
        staticmethod(fail_parser),
    )

    with pytest.raises(ValueError, match="programming parser failure"):
        check_opencode(tmp_path, OpenCodeExecutor())

def test_opencode_git_initializer_oserror_becomes_repository_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_git(_path: Path) -> bool:
        raise OSError("sensitive git detail")

    monkeypatch.setattr(
        CliHealthChecker,
        "_initialize_git_repository",
        staticmethod(fail_git),
    )

    report = check_opencode(tmp_path, OpenCodeExecutor())

    assert report.can_start is False
    assert report.by_key("probe_repository").category is (
        FailureCategory.CONFIGURATION
    )
    assert "sensitive git" not in report.model_dump_json()

def test_opencode_git_initializer_value_error_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_git(_path: Path) -> bool:
        raise ValueError("programming git failure")

    monkeypatch.setattr(
        CliHealthChecker,
        "_initialize_git_repository",
        staticmethod(fail_git),
    )

    with pytest.raises(ValueError, match="programming git failure"):
        check_opencode(tmp_path, OpenCodeExecutor())

def test_opencode_tempdir_oserror_becomes_repository_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_tempdir(*_args: object, **_kwargs: object) -> object:
        raise OSError("sensitive tempdir detail")

    monkeypatch.setattr(
        cli_health_module.tempfile,
        "TemporaryDirectory",
        fail_tempdir,
    )

    report = check_opencode(tmp_path, OpenCodeExecutor())

    assert report.can_start is False
    assert report.by_key("probe_repository").category is (
        FailureCategory.CONFIGURATION
    )
    assert "sensitive tempdir" not in report.model_dump_json()

def test_opencode_tempdir_value_error_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_tempdir(*_args: object, **_kwargs: object) -> object:
        raise ValueError("programming tempdir failure")

    monkeypatch.setattr(
        cli_health_module.tempfile,
        "TemporaryDirectory",
        fail_tempdir,
    )

    with pytest.raises(ValueError, match="programming tempdir failure"):
        check_opencode(tmp_path, OpenCodeExecutor())

def test_opencode_tempdir_cleanup_oserror_preserves_probe_diagnosis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = cli_health_module.tempfile.TemporaryDirectory

    class CleanupFailure(original):  # type: ignore[misc, valid-type]
        """模拟目录已删除但 cleanup 抛 OSError。"""

        def cleanup(self) -> None:
            """清理目录后抛错；无参数和返回值；用于验证诊断保留。"""
            super().cleanup()
            raise OSError("sensitive tempdir cleanup detail")

    monkeypatch.setattr(
        cli_health_module.tempfile,
        "TemporaryDirectory",
        CleanupFailure,
    )
    executor = OpenCodeExecutor(
        probe_stdout=json.dumps(
            {"type": "error", "sessionID": "cleanup-diagnostic-session"}
        )
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("probe").category is FailureCategory.PROTOCOL_ERROR
    assert report.by_key("probe_repository_cleanup").level == "warning"
    assert "sensitive tempdir cleanup" not in report.model_dump_json()

def test_opencode_validates_argv_before_executor_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = OpenCodeExecutor()
    monkeypatch.setattr(
        OpenCodeHealthStrategy,
        "help_argv",
        staticmethod(lambda executable: (executable, "run", "bad\0token")),
    )

    with pytest.raises(ValueError, match="argv"):
        check_opencode(tmp_path, executor)

    assert command_tails(executor) == [("--version",)]

def test_opencode_all_commands_use_three_controlled_overrides(tmp_path: Path) -> None:
    source_environment = {"PATH": "/opt/tools"}
    executor = OpenCodeExecutor()
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/opencode",
        environment=source_environment,
        probe_root=tmp_path / "probes",
    )

    report = checker.check(
        agent=AgentName.OPENCODE,
        profile=AgentProfile(command="opencode"),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is True
    expected_names = {
        "PATH",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS",
    }
    assert all(
        set(environment) == expected_names
        for *_prefix, environment in executor.calls
    )
    assert set(report.enabled_environment_names) == expected_names
    assert source_environment == {"PATH": "/opt/tools"}
