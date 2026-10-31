from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from debate_agent.cli_health import (
    CircuitBreakerStore,
    CliCommandResult,
    CliHealthChecker,
    CliIdentity,
    FailureCategory,
    FailureClassifier,
    SubprocessCommandExecutor,
    discover_cli_model,
    filter_cli_environment,
    freeze_run_config,
    preflight_run_config,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import AgentName
from debate_agent.process import ProcessTermination


@pytest.mark.skipif(os.name != "posix", reason="进程组清理仅适用于 POSIX")
def test_subprocess_executor_kills_process_group_after_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证健康探针超时会终止整个进程组；参数为临时目录和补丁器，无返回值。"""

    class TimedOutProcess:
        """模拟首次通信超时、终止后正常回收的子进程。"""

        pid = 4242
        returncode = -signal.SIGTERM

        def __init__(self) -> None:
            """初始化通信计数；无参数，无返回值。"""
            self.communicate_calls = 0

        def communicate(
            self,
            input: str | None = None,
            timeout: float | None = None,
        ) -> tuple[str, str]:
            """模拟通信；参数为输入和超时，首次抛超时，之后返回空输出。"""
            del input, timeout
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(("codebuddy",), 1)
            return "", ""

    process = TimedOutProcess()
    popen_options: dict[str, object] = {}

    def fake_popen(
        argv: Sequence[str], **kwargs: object
    ) -> TimedOutProcess:
        """返回模拟进程；参数为命令和选项，返回进程并记录选项。"""
        del argv
        popen_options.update(kwargs)
        return process

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    result = SubprocessCommandExecutor().run(
        ("codebuddy", "-p"),
        cwd=tmp_path,
        stdin="{}",
        timeout_seconds=1,
        env={"PATH": "/bin"},
    )

    assert result.return_code == 124
    assert result.stderr == "health probe timeout"
    assert popen_options["start_new_session"] is True
    assert signals == [(4242, signal.SIGTERM)]
    assert process.communicate_calls == 2


class FakeExecutor:
    """按命令特征返回可配置的 CLI 预检结果。"""

    def __init__(
        self,
        *,
        auth_ok: bool = True,
        probe_ok: bool = True,
        probe_structured: bool = True,
        help_ok: bool = True,
        mcp_ok: bool = True,
    ) -> None:
        """创建执行器；参数为各阶段开关，返回实例，不执行命令。"""
        self.auth_ok = auth_ok
        self.probe_ok = probe_ok
        self.probe_structured = probe_structured
        self.help_ok = help_ok
        self.mcp_ok = mcp_ok
        self.calls: list[tuple[tuple[str, ...], str, Mapping[str, str]]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> CliCommandResult:
        """返回模拟结果；参数为命令上下文，返回结果，不抛异常。"""
        command = tuple(argv)
        self.calls.append((command, stdin, dict(env)))
        executable = Path(command[0]).name
        if "--version" in command:
            version = "codex-cli 0.144.5" if executable == "codex" else "2.1.211"
            return CliCommandResult(return_code=0, stdout=version, stderr="")
        if "--help" in command:
            if not self.help_ok:
                return CliCommandResult(return_code=0, stdout="--model", stderr="")
            flags = (
                "--json --output-schema --ignore-user-config --sandbox --model"
                if executable == "codex"
                else (
                    "--output-format --session-id --resume --strict-mcp-config"
                    if executable == "qodercli"
                    else
                    "--output-format stream-json --include-partial-messages "
                    "--strict-mcp-config --json-schema --model"
                )
            )
            return CliCommandResult(return_code=0, stdout=flags, stderr="")
        if "status" in command:
            return CliCommandResult(
                return_code=0 if self.auth_ok else 1,
                stdout="logged in" if self.auth_ok else "",
                stderr="" if self.auth_ok else "not logged in",
            )
        if "mcp" in command:
            return CliCommandResult(
                return_code=0 if self.mcp_ok else 1,
                stdout="server-a connected" if self.mcp_ok else "",
                stderr="" if self.mcp_ok else "optional mcp unavailable",
            )
        if self.probe_ok:
            if self.probe_structured:
                payload = (
                    '{"type":"turn.completed"}\n'
                    if executable == "codex"
                    else '{"type":"result","session_id":"masked","result":{}}\n'
                )
            else:
                payload = "plain text without terminal protocol"
            return CliCommandResult(return_code=0, stdout=payload, stderr="")
        return CliCommandResult(
            return_code=1,
            stdout="",
            stderr="WebSocket TLS handshake failed",
        )


def test_health_checker_freezes_identity_and_uses_readonly_minimal_probe(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor()
    locations = {
        "codex": "/opt/tools/codex",
        "claude": "/opt/tools/claude",
    }
    checker = CliHealthChecker(
        executor=executor,
        which=lambda command: locations.get(command),
        environment={
            "PATH": "/opt/tools",
            "HTTPS_PROXY": "proxy-secret",
            "DEEPSEEK_API_KEY": "must-not-pass",
        },
        probe_root=tmp_path / "health",
    )

    codex = checker.check(
        agent=AgentName.CODEX,
        profile=AgentProfile(command="codex", model="gpt-test"),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=("HTTPS_PROXY",),
    )
    claude = checker.check(
        agent=AgentName.CLAUDE,
        profile=AgentProfile(command="claude", model="sonnet"),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=("HTTPS_PROXY",),
    )

    assert codex.can_start and claude.can_start
    assert codex.identity == CliIdentity(
        agent=AgentName.CODEX,
        executable="/opt/tools/codex",
        version="0.144.5",
        model="gpt-test",
    )
    assert claude.identity is not None
    assert claude.identity.version == "2.1.211"
    probe_calls = [call for call in executor.calls if call[1]]
    assert any("read-only" in argv for argv, _, _ in probe_calls)
    assert any("plan" in argv for argv, _, _ in probe_calls)
    claude_probe = next(
        argv for argv, _, _ in probe_calls if Path(argv[0]).name == "claude"
    )
    mcp_config = claude_probe[claude_probe.index("--mcp-config") + 1]
    assert json.loads(mcp_config) == {"mcpServers": {}}
    assert all(env["HTTPS_PROXY"] == "proxy-secret" for _, _, env in probe_calls)
    assert all("DEEPSEEK_API_KEY" not in env for _, _, env in probe_calls)
    assert "proxy-secret" not in codex.model_dump_json()


def test_health_checker_blocks_missing_model_feature_auth_and_main_connection(
    tmp_path: Path,
) -> None:
    def check(executor: FakeExecutor, model: str | None = "gpt-test"):
        checker = CliHealthChecker(
            executor=executor,
            which=lambda _command: "/opt/tools/codex",
            environment={"PATH": "/opt/tools"},
            probe_root=tmp_path / "health",
        )
        return checker.check(
            agent=AgentName.CODEX,
            profile=AgentProfile(command="codex", model=model),
            workspace=tmp_path,
            mcp_mode="minimal",
            proxy_environment_allowlist=(),
        )

    missing_model = check(FakeExecutor(), model=None)
    missing_feature = check(FakeExecutor(help_ok=False))
    missing_auth = check(FakeExecutor(auth_ok=False))
    network = check(FakeExecutor(probe_ok=False))

    assert missing_model.by_key("model").category == FailureCategory.CONFIGURATION
    assert missing_feature.by_key("features").category == (
        FailureCategory.CACHE_INCOMPATIBLE
    )
    assert missing_auth.by_key("authentication").category == (
        FailureCategory.AUTHENTICATION_ERROR
    )
    assert network.by_key("probe").category == FailureCategory.NETWORK_ERROR
    assert not any(stdin for _argv, stdin, _env in missing_model.command_evidence)


def test_health_checker_accepts_qoder_default_model_and_yolo_probe(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor()
    checker = CliHealthChecker(
        executor=executor,
        which=lambda command: f"/opt/tools/{command}",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )

    report = checker.check(
        agent=AgentName.QODER,
        profile=AgentProfile(command="qodercli", model="__cli_default__"),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is True
    probe = next(argv for argv, stdin, _env in executor.calls if stdin)
    assert probe[:3] == ("/opt/tools/qodercli", "--yolo", "-p")
    assert "--model" not in probe


def test_full_mcp_failure_is_warning_and_does_not_block_model_probe(
    tmp_path: Path,
) -> None:
    executor = FakeExecutor(mcp_ok=False)
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/claude",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )

    report = checker.check(
        agent=AgentName.CLAUDE,
        profile=AgentProfile(command="claude", model="sonnet"),
        workspace=tmp_path,
        mcp_mode="full",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is True
    assert report.by_key("mcp").level == "warning"
    assert report.by_key("mcp").category == FailureCategory.MCP_OPTIONAL_ERROR
    assert report.by_key("probe").level == "success"


def test_probe_requires_terminal_structured_event(tmp_path: Path) -> None:
    checker = CliHealthChecker(
        executor=FakeExecutor(probe_structured=False),
        which=lambda _command: "/opt/tools/codex",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )

    report = checker.check(
        agent=AgentName.CODEX,
        profile=AgentProfile(command="codex", model="gpt-test"),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is False
    assert report.by_key("probe").category == FailureCategory.PROTOCOL_ERROR


def test_open_circuit_skips_auth_mcp_and_model_probe(tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    circuit = CircuitBreakerStore(
        state_root=tmp_path,
        threshold=1,
        window_seconds=60,
        cooldown_seconds=120,
        now=lambda: now,
    )
    identity = CliIdentity(
        agent=AgentName.CODEX,
        executable="/opt/tools/codex",
        version="0.144.5",
        model="gpt-test",
    )
    circuit.record_failure(
        identity,
        category=FailureCategory.NETWORK_ERROR,
        signature="tls",
    )
    executor = FakeExecutor()
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/codex",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )

    report = checker.check(
        agent=AgentName.CODEX,
        profile=AgentProfile(command="codex", model="gpt-test"),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
        circuit_breaker=circuit,
    )

    assert report.can_start is False
    assert report.by_key("circuit_breaker").category == (
        FailureCategory.NETWORK_ERROR
    )
    assert all(not stdin for _argv, stdin, _env in report.command_evidence)
    assert len(executor.calls) == 2


def test_model_discovery_reads_local_configs_without_returning_secrets(
    tmp_path: Path,
) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        'model = "gpt-local"\napi_key = "secret"\n', encoding="utf-8"
    )
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"model": "sonnet-local", "apiKey": "secret"}),
        encoding="utf-8",
    )

    assert discover_cli_model(AgentName.CODEX, home=tmp_path, environment={}) == (
        "gpt-local"
    )
    assert discover_cli_model(AgentName.CLAUDE, home=tmp_path, environment={}) == (
        "sonnet-local"
    )


def test_model_discovery_reads_claude_settings_environment(tmp_path: Path) -> None:
    """验证 Claude settings.env 中的显式模型可被发现且不会读取密钥。"""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_MODEL": "deepseek-v4-pro",
                    "ANTHROPIC_API_KEY": "secret",
                }
            }
        ),
        encoding="utf-8",
    )

    assert discover_cli_model(AgentName.CLAUDE, home=tmp_path, environment={}) == (
        "deepseek-v4-pro"
    )


def test_filter_cli_environment_only_passes_base_and_selected_proxy_names() -> None:
    filtered = filter_cli_environment(
        {
            "PATH": "/bin",
            "HOME": "/home/test",
            "HTTPS_PROXY": "proxy-secret",
            "SSL_CERT_FILE": "/cert.pem",
            "DEEPSEEK_API_KEY": "key-secret",
        },
        base_names=("PATH", "HOME"),
        proxy_names=("HTTPS_PROXY",),
    )

    assert filtered == {
        "PATH": "/bin",
        "HOME": "/home/test",
        "HTTPS_PROXY": "proxy-secret",
    }


def test_failure_classifier_distinguishes_timeout_network_cache_and_policy() -> None:
    classifier = FailureClassifier()

    assert classifier.classify(
        "", termination=ProcessTermination.STARTUP_TIMEOUT
    ) == FailureCategory.STARTUP_TIMEOUT
    assert classifier.classify(
        "", termination=ProcessTermination.IDLE_TIMEOUT
    ) == FailureCategory.IDLE_TIMEOUT
    assert classifier.classify(
        "", termination=ProcessTermination.HARD_TIMEOUT
    ) == FailureCategory.HARD_TIMEOUT
    assert classifier.classify("TLS handshake failed") == FailureCategory.NETWORK_ERROR
    assert classifier.classify("supports_reasoning_summaries missing") == (
        FailureCategory.CACHE_INCOMPATIBLE
    )
    assert classifier.classify("未经批准的命令") == FailureCategory.POLICY_VIOLATION


def test_circuit_breaker_is_scoped_persistent_and_resettable(tmp_path: Path) -> None:
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    clock = lambda: now  # noqa: E731
    store = CircuitBreakerStore(
        state_root=tmp_path,
        threshold=2,
        window_seconds=60,
        cooldown_seconds=120,
        now=clock,
    )
    identity = CliIdentity(
        agent=AgentName.CODEX,
        executable="/opt/codex",
        version="0.144.5",
        model="gpt-test",
    )

    store.record_failure(
        identity,
        category=FailureCategory.NETWORK_ERROR,
        signature="tls handshake failed",
    )
    assert store.active(identity) is None
    opened = store.record_failure(
        identity,
        category=FailureCategory.NETWORK_ERROR,
        signature="tls handshake failed",
    )

    assert opened is not None
    assert store.active(identity) is not None
    assert CircuitBreakerStore(
        state_root=tmp_path,
        threshold=2,
        window_seconds=60,
        cooldown_seconds=120,
        now=clock,
    ).active(identity) is not None
    other_model = identity.model_copy(update={"model": "other"})
    assert store.active(other_model) is None
    store.record_success(identity)
    assert store.active(identity) is None
    store.record_failure(
        identity,
        category=FailureCategory.NETWORK_ERROR,
        signature="tls handshake failed",
    )
    store.record_failure(
        identity,
        category=FailureCategory.NETWORK_ERROR,
        signature="tls handshake failed",
    )
    now += timedelta(seconds=121)
    assert store.active(identity) is None


def test_freeze_run_config_persists_verified_path_version_model_and_names(
    tmp_path: Path,
) -> None:
    checker = CliHealthChecker(
        executor=FakeExecutor(),
        which=lambda command: f"/opt/tools/{command}",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        state_root=tmp_path / "state",
        profiles={
            AgentName.CODEX: AgentProfile(
                command="codex",
                model="gpt-test",
                variant="legacy-option",
            ),
            AgentName.CLAUDE: AgentProfile(command="claude", model="sonnet"),
        },
        proxy_environment_allowlist=("HTTPS_PROXY",),
    )
    reports = {
        agent: checker.check(
            agent=agent,
            profile=config.profiles[agent],
            workspace=tmp_path,
            mcp_mode="minimal",
            proxy_environment_allowlist=config.proxy_environment_allowlist,
        )
        for agent in config.participants
    }

    frozen = freeze_run_config(config, reports=reports)

    assert frozen.profiles[AgentName.CODEX].command == "/opt/tools/codex"
    assert frozen.profiles[AgentName.CODEX].version == "0.144.5"
    assert frozen.profiles[AgentName.CODEX].model == "gpt-test"
    assert frozen.profiles[AgentName.CODEX].variant == "legacy-option"
    assert "HTTPS_PROXY" in frozen.model_dump_json()
    assert "proxy-secret" not in frozen.model_dump_json()


def test_codex_legacy_variant_and_config_hashes_do_not_drift_circuit_identity(
    tmp_path: Path,
) -> None:
    checker = CliHealthChecker(
        executor=FakeExecutor(),
        which=lambda _command: "/opt/tools/codex",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )
    first = checker.check(
        agent=AgentName.CODEX,
        profile=AgentProfile(
            command="codex",
            model="gpt-test",
            variant="legacy-one",
            runtime_config_hash="a" * 64,
            runtime_config_content_hash="b" * 64,
        ),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )
    second = checker.check(
        agent=AgentName.CODEX,
        profile=AgentProfile(
            command="codex",
            model="gpt-test",
            variant="legacy-two",
            runtime_config_hash="c" * 64,
            runtime_config_content_hash="d" * 64,
        ),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert first.identity == second.identity
    assert first.identity is not None
    assert first.identity.variant is None
    assert first.identity.runtime_config_hash is None
    assert first.identity.runtime_config_content_hash is None
    store = CircuitBreakerStore(
        state_root=tmp_path / "state",
        threshold=2,
        window_seconds=60,
        cooldown_seconds=120,
    )
    store.record_failure(
        first.identity,
        category=FailureCategory.NETWORK_ERROR,
        signature="same failure",
    )
    opened = store.record_failure(
        second.identity,
        category=FailureCategory.NETWORK_ERROR,
        signature="same failure",
    )
    assert opened is not None


def test_preflight_does_not_touch_unselected_opencode(tmp_path: Path) -> None:
    executor = FakeExecutor()
    which_calls: list[str] = []

    def which(command: str) -> str:
        """记录命令解析；参数为命令；返回测试绝对路径；不抛异常。"""
        which_calls.append(command)
        return f"/opt/tools/{command}"

    checker = CliHealthChecker(
        executor=executor,
        which=which,
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        state_root=tmp_path / "state",
        profiles={
            AgentName.CODEX: AgentProfile(command="codex", model="gpt-test"),
            AgentName.CLAUDE: AgentProfile(command="claude", model="sonnet"),
            AgentName.OPENCODE: AgentProfile(command="opencode"),
        },
    )

    frozen = preflight_run_config(
        config,
        checker=checker,
        environment={"PATH": "/opt/tools"},
        home=tmp_path,
    )

    assert frozen.participants == (AgentName.CODEX, AgentName.CLAUDE)
    assert which_calls == ["codex", "claude"]
    assert all(
        Path(argv[0]).name != "opencode"
        for argv, _stdin, _env in executor.calls
    )


def test_health_checker_reports_symlink_loop_as_executable_configuration_error(
    tmp_path: Path,
) -> None:
    """验证循环链接稳定报告可执行文件配置错误；参数为临时目录；无返回值。"""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.symlink_to(second)
    second.symlink_to(first)
    executor = FakeExecutor()
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: str(first),
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )

    report = checker.check(
        agent=AgentName.CODEX,
        profile=AgentProfile(command="codex", model="gpt-test"),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is False
    assert report.by_key("executable").category is FailureCategory.CONFIGURATION
    assert executor.calls == []
