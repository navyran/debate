from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from git_helpers import create_repo, git

import debate_agent.cli as cli_module
from debate_agent.agent_catalog import agent_definitions
from debate_agent.cli import LifecycleService, build_parser, main
from debate_agent.models import AgentName, ReviewProtocolVersion, RunStatus


class FakeService(LifecycleService):
    """记录 CLI 生命周期委派的测试服务。"""

    def __init__(self, status: RunStatus = RunStatus.COMPLETED) -> None:
        self.status = status
        self.calls: list[tuple[str, object]] = []

    def run(self, config: object) -> dict[str, object]:
        self.calls.append(("run", config))
        return {"run_id": "RUN-1", "status": self.status.value}

    def resume(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        self.calls.append(("resume", run_id))
        return {"run_id": run_id, "status": self.status.value}

    def abort(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        self.calls.append(("abort", run_id))
        return {"run_id": run_id, "status": RunStatus.ABORTED.value}

    def cleanup(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        self.calls.append(("cleanup", run_id))
        return {"run_id": run_id, "status": "cleaned"}


def test_parser_and_profiles_delegate_agent_surfaces_to_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 CLI 表面委托 Catalog；参数为目录/补丁夹具，无返回值，非法配置抛错。"""
    definitions = tuple(reversed(agent_definitions()))
    definitions = tuple(
        replace(definition, default_command="qoder-next")
        if definition.name is AgentName.QODER
        else definition
        for definition in definitions
    )
    monkeypatch.setattr(cli_module, "agent_definitions", lambda: definitions)

    parser = build_parser()
    run_parser = parser._subparsers._group_actions[0].choices["run"]
    run_action = next(
        action
        for action in run_parser._actions
        if action.dest == "participant"
    )
    first_action = next(
        action for action in run_parser._actions if action.dest == "first_agent"
    )
    args = parser.parse_args(
        ["run", "--workspace", str(tmp_path), "--topic", "目标"]
    )

    assert tuple(run_action.choices) == tuple(
        definition.name.value for definition in definitions
    )
    assert tuple(first_action.choices) == tuple(
        definition.name.value for definition in definitions
    )
    assert not any(
        "--agent-option" in action.option_strings
        for action in run_parser._actions
    )
    assert args.qoder_command == "qoder-next"

    cli_directory = tmp_path / "catalog-bin"
    cli_directory.mkdir()
    catalog_cli = cli_directory / "qoder-next"
    catalog_cli.write_text("#!/bin/sh\n", encoding="utf-8")
    catalog_cli.chmod(0o755)
    assert cli_module._extend_cli_path(
        environment={"PATH": "/usr/bin"},
        candidate_directories=(cli_directory,),
    ) == (str(cli_directory),)

    service = FakeService()
    assert main(
        ["run", "--workspace", str(tmp_path), "--topic", "目标"],
        service=service,
    ) == 0
    config = service.calls[0][1]
    assert tuple(config.profiles) == tuple(
        definition.name for definition in definitions
    )
    assert config.profiles[AgentName.QODER].command == "qoder-next"
    assert config.protocol_version is ReviewProtocolVersion.REVIEW_V1


@pytest.mark.parametrize("agent", tuple(AgentName))
def test_cli_preserves_all_agent_profile_options_and_legacy_timeout_override(
    tmp_path: Path, agent: AgentName
) -> None:
    """验证三方旧参数契约；参数为目录/Agent，返回值为空，解析失败由 CLI 报错。"""
    service = FakeService()
    prefix = agent.value
    command = f"/opt/{prefix}-custom"

    assert main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--topic",
            "目标",
            f"--{prefix}-command",
            command,
            f"--{prefix}-model",
            "model-x",
            f"--{prefix}-timeout-seconds",
            "901",
            f"--{prefix}-startup-timeout-seconds",
            "101",
            f"--{prefix}-idle-timeout-seconds",
            "202",
            f"--{prefix}-hard-timeout-seconds",
            "303",
            f"--{prefix}-heartbeat-interval-seconds",
            "4",
        ],
        service=service,
    ) == 0
    profile = service.calls[0][1].profiles[agent]
    assert profile.command == command
    assert profile.model == "model-x"
    assert profile.timeout_seconds == 901
    assert profile.startup_timeout_seconds == 101
    assert profile.idle_timeout_seconds == 202
    assert profile.hard_timeout_seconds == 901
    assert profile.heartbeat_interval_seconds == 4


def test_parser_defines_all_lifecycle_commands() -> None:
    parser = build_parser()

    run_args = parser.parse_args(
        [
            "run",
            "--workspace",
            "/tmp/project",
            "--topic",
            "实现目标",
            "--target-branch",
            "main",
            "--first-agent",
            "claude",
            "--max-turns",
            "5",
            "--protocol-version",
            "legacy-v0",
            "--codex-model",
            "gpt-5.4",
            "--claude-timeout-seconds",
            "600",
            "--codex-startup-timeout-seconds",
            "240",
            "--codex-idle-timeout-seconds",
            "180",
            "--codex-hard-timeout-seconds",
            "4200",
            "--codex-heartbeat-interval-seconds",
            "4",
            "--mcp-mode",
            "full",
            "--proxy-env",
            "HTTPS_PROXY",
            "--no-facilitator",
            "--facilitator-model",
            "deepseek-v4-flash",
            "--facilitator-base-url",
            "https://api.deepseek.com",
            "--facilitator-timeout-seconds",
            "90",
        ]
    )

    assert run_args.command == "run"
    assert run_args.first_agent == "claude"
    assert run_args.max_turns == 5
    assert run_args.protocol_version == "legacy-v0"
    assert run_args.codex_model == "gpt-5.4"
    assert run_args.claude_timeout_seconds == 600
    assert run_args.codex_startup_timeout_seconds == 240
    assert run_args.codex_idle_timeout_seconds == 180
    assert run_args.codex_hard_timeout_seconds == 4200
    assert run_args.codex_heartbeat_interval_seconds == 4
    assert run_args.mcp_mode == "full"
    assert run_args.proxy_env == ["HTTPS_PROXY"]
    assert run_args.facilitator_enabled is False
    assert run_args.use_worktree is False
    assert run_args.facilitator_model == "deepseek-v4-flash"
    assert run_args.facilitator_timeout_seconds == 90
    assert run_args.codex_resume_sandbox_verified is True
    assert run_args.unrestricted_commands is True
    assert run_args.qoder_command == "qodercli"
    no_codex_resume_args = parser.parse_args(
        [
            "run",
            "--workspace",
            "/tmp/project",
            "--topic",
            "目标",
            "--no-codex-resume-sandbox-verified",
        ]
    )
    assert no_codex_resume_args.codex_resume_sandbox_verified is False
    restricted_args = parser.parse_args(
        [
            "run",
            "--workspace",
            "/tmp/project",
            "--topic",
            "目标",
            "--no-unrestricted-commands",
        ]
    )
    assert restricted_args.unrestricted_commands is False
    worktree_args = parser.parse_args(
        ["run", "--workspace", "/tmp/project", "--topic", "目标", "--worktree"]
    )
    assert worktree_args.use_worktree is True
    assert worktree_args.protocol_version == "review-v1"
    for command in ("resume", "status", "abort", "cleanup"):
        args = parser.parse_args([command, "--run-id", "RUN-1"])
        assert args.command == command
    ui_args = parser.parse_args(["ui", "--port", "8600"])
    assert ui_args.command == "ui"
    assert ui_args.port == 8600
    expected_ui_state = (Path(__file__).parents[1] / ".runtime").resolve()
    assert ui_args.state_root.resolve() == expected_ui_state


def test_cli_accepts_selected_qoder_participants(tmp_path: Path) -> None:
    service = FakeService()

    exit_code = main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--topic",
            "Codex 与 Qoder 讨论",
            "--participant",
            "codex",
            "--participant",
            "qoder",
            "--first-agent",
            "qoder",
            "--qoder-command",
            "/opt/qodercli",
        ],
        service=service,
    )

    config = service.calls[0][1]
    assert exit_code == 0
    assert config.participants == (AgentName.CODEX, AgentName.QODER)
    assert config.first_agent is AgentName.QODER
    assert config.profiles[AgentName.QODER].command == "/opt/qodercli"


def test_cli_protocol_version_is_frozen_and_rejects_unknown_value(
    tmp_path: Path,
) -> None:
    """验证 CLI 显式冻结协议版本；参数为临时目录，无返回值，非法参数由解析器退出。"""
    service = FakeService()

    assert main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--topic",
            "兼容旧协议",
            "--protocol-version",
            "legacy-v0",
        ],
        service=service,
    ) == 0
    assert service.calls[0][1].protocol_version is ReviewProtocolVersion.LEGACY_V0
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "run",
                "--workspace",
                str(tmp_path),
                "--topic",
                "非法协议",
                "--protocol-version",
                "review-v2",
            ]
        )


def test_cli_issue_ledger_is_explicit_and_default_off(tmp_path: Path) -> None:
    """验证 CLI Ledger 开关冻结且旧协议被配置校验拒绝；参数为临时目录。"""
    service = FakeService()
    assert main(
        ["run", "--workspace", str(tmp_path), "--topic", "默认不开启账本"],
        service=service,
    ) == 0
    assert service.calls[-1][1].issue_ledger_enabled is False
    assert main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--topic",
            "启用账本",
            "--issue-ledger",
        ],
        service=service,
    ) == 0
    assert service.calls[-1][1].issue_ledger_enabled is True
    assert main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--topic",
            "旧协议",
            "--protocol-version",
            "legacy-v0",
            "--issue-ledger",
        ],
        service=service,
    ) == 2


def test_cli_counterexample_ballot_is_explicit_and_review_v1_only(
    tmp_path: Path,
) -> None:
    """验证 CLI 反例投票默认关闭、可显式开启且拒绝 legacy-v0。"""
    service = FakeService()
    assert main(
        ["run", "--workspace", str(tmp_path), "--topic", "默认终局投票"],
        service=service,
    ) == 0
    assert service.calls[-1][1].counterexample_ballot is False
    assert main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--topic",
            "启用反例终局投票",
            "--counterexample-ballot",
        ],
        service=service,
    ) == 0
    assert service.calls[-1][1].counterexample_ballot is True
    assert main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--topic",
            "旧协议反例终局投票",
            "--protocol-version",
            "legacy-v0",
            "--counterexample-ballot",
        ],
        service=service,
    ) == 2


def test_cli_exposes_opencode_variant_only_for_opencode(tmp_path: Path) -> None:
    """验证 OpenCode variant 写入 Profile；参数为临时目录，无返回值。"""
    service = FakeService()

    assert main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--topic",
            "Codex 与 OpenCode 讨论",
            "--participant",
            "codex",
            "--participant",
            "opencode",
            "--opencode-variant",
            "quality",
        ],
        service=service,
    ) == 0

    config = service.calls[0][1]
    assert config.participants == (AgentName.CODEX, AgentName.OPENCODE)
    assert config.profiles[AgentName.OPENCODE].variant == "quality"
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "run",
                "--workspace",
                str(tmp_path),
                "--topic",
                "目标",
                "--codex-variant",
                "quality",
            ]
        )


def test_status_reads_persisted_status_json(tmp_path: Path, capsys: object) -> None:
    run_dir = tmp_path / "runs" / "RUN-1"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(
        json.dumps({"run_id": "RUN-1", "status": "blocked"}), encoding="utf-8"
    )

    exit_code = main(["status", "--run-id", "RUN-1", "--state-root", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {"run_id": "RUN-1", "status": "blocked"}


def test_status_missing_run_is_nonzero(tmp_path: Path, capsys: object) -> None:
    exit_code = main(["status", "--run-id", "MISSING", "--state-root", str(tmp_path)])

    assert exit_code == 2
    assert "MISSING" in capsys.readouterr().err


def test_lifecycle_commands_delegate_and_map_completion_exit_codes(
    tmp_path: Path, capsys: object
) -> None:
    service = FakeService()
    exit_code = main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--topic",
            "实现目标",
            "--target-branch",
            "main",
        ],
        service=service,
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert service.calls[0][0] == "run"

    for command in ("resume", "abort", "cleanup"):
        assert (
            main(
                [command, "--run-id", "RUN-1", "--state-root", str(tmp_path)],
                service=service,
            )
            == 0
        )
    blocked = FakeService(RunStatus.MAX_TURNS)
    assert (
        main(
            [
                "run",
                "--workspace",
                str(tmp_path),
                "--topic",
                "goal",
                "--target-branch",
                "main",
            ],
            service=blocked,
        )
        == 3
    )


def test_run_auto_detects_master_and_can_disable_worktree(
    tmp_path: Path, capsys: object
) -> None:
    """验证 CLI 自动识别分支并传递直连模式；参数为临时目录/输出夹具，无返回值。"""
    repo = create_repo(tmp_path)
    git(repo, "branch", "-m", "master")
    service = FakeService()

    exit_code = main(
        [
            "run",
            "--workspace",
            str(repo),
            "--topic",
            "实现目标",
            "--no-worktree",
        ],
        service=service,
    )

    assert exit_code == 0
    config = service.calls[0][1]
    assert config.target_branch == "master"
    assert config.use_worktree is False
    capsys.readouterr()


def test_run_parses_safe_json_argv_and_rejects_non_array(tmp_path: Path) -> None:
    service = FakeService()
    assert (
        main(
            [
                "run",
                "--workspace",
                str(tmp_path),
                "--topic",
                "goal",
                "--target-branch",
                "main",
                "--validation-command-json",
                '["python", "-m", "pytest"]',
                "--env",
                "CUSTOM_SAFE_NAME",
                "--codex-model",
                "gpt-test",
                "--claude-model",
                "sonnet",
                "--codex-startup-timeout-seconds",
                "240",
                "--proxy-env",
                "HTTPS_PROXY",
            ],
            service=service,
        )
        == 0
    )
    config = service.calls[0][1]
    assert config.validation_commands == (("python", "-m", "pytest"),)
    assert "PATH" in config.environment_allowlist
    assert "CUSTOM_SAFE_NAME" in config.environment_allowlist
    assert config.profiles[AgentName.CODEX].command == "codex"
    assert config.profiles[AgentName.CODEX].model == "gpt-test"
    assert config.profiles[AgentName.CODEX].startup_timeout_seconds == 240
    assert config.profiles[AgentName.CODEX].hard_timeout_seconds == 3600
    assert config.profiles[AgentName.CLAUDE].model == "sonnet"
    assert config.proxy_environment_allowlist == ("HTTPS_PROXY",)
    assert config.facilitator_enabled is True
    assert config.facilitator_model == "deepseek-v4-flash"
    assert (
        main(
            [
                "run",
                "--workspace",
                str(tmp_path),
                "--topic",
                "goal",
                "--target-branch",
                "main",
                "--setup-command-json",
                '"python -m pip install bad"',
            ],
            service=service,
        )
        == 2
    )


def test_ui_command_launches_local_streamlit_with_state_root(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, int, Path]] = []

    def launch(host: str, port: int, state_root: Path) -> int:
        calls.append((host, port, state_root))
        return 0

    exit_code = main(
        ["ui", "--port", "8600", "--state-root", str(tmp_path)],
        ui_launcher=launch,
    )

    assert exit_code == 0
    assert calls == [("127.0.0.1", 8600, tmp_path.resolve())]


def test_extend_cli_path_adds_only_directory_with_executable(tmp_path: Path) -> None:
    cli_directory = tmp_path / "node-bin"
    cli_directory.mkdir()
    codex = cli_directory / "codex"
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    codex.chmod(0o755)
    empty_directory = tmp_path / "empty"
    empty_directory.mkdir()
    environment = {"PATH": "/usr/bin:/bin"}

    appended = cli_module._extend_cli_path(
        environment=environment,
        candidate_directories=(empty_directory, cli_directory),
    )

    assert appended == (str(cli_directory),)
    assert environment["PATH"] == f"/usr/bin:/bin:{cli_directory}"


def test_entrypoint_loads_default_key_before_dispatch(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        cli_module, "load_default_deepseek_key", lambda: events.append("load")
    )
    monkeypatch.setattr(cli_module, "_extend_cli_path", lambda: events.append("path"))
    monkeypatch.setattr(cli_module, "main", lambda: events.append("main") or 0)

    with pytest.raises(SystemExit) as captured:
        cli_module.entrypoint()

    assert captured.value.code == 0
    assert events == ["load", "path", "main"]
