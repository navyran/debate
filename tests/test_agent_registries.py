from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from typing import Literal, cast

import pytest

from debate_agent.adapters import (
    ADAPTER_FACTORIES,
    AGENT_ENVIRONMENT_PASSTHROUGH,
    ENVIRONMENT_OVERRIDE_ALLOWLIST,
    SENSITIVE_AGENT_ENVIRONMENT_KEYS,
    AdapterContext,
    AgentAdapter,
    ClaudeAdapter,
    CodeBuddyAdapter,
    CodexAdapter,
    HermesAdapter,
    OpenClawAdapter,
    QoderAdapter,
    adapter_factory,
    create_claude_adapter,
    create_codebuddy_adapter,
    create_codex_adapter,
    create_hermes_adapter,
    create_openclaw_adapter,
    create_qoder_adapter,
)
from debate_agent.agent_catalog import AGENT_CATALOG
from debate_agent.artifacts import ArtifactStore
from debate_agent.cli_health import (
    CLI_DEFAULT_MODEL,
    HEALTH_STRATEGIES,
    CliCommandResult,
    CliHealthChecker,
    CliHealthStrategy,
    CodeBuddyCliHealthStrategy,
    HermesCliHealthStrategy,
    OpenClawCliHealthStrategy,
    OpenCodeHealthStrategy,
    discover_cli_model,
    health_strategy,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import AgentName
from debate_agent.policy import CommandPolicy, PolicyViolationError
from debate_agent.runtime import CLIBackend


def schema_file(tmp_path: Path) -> Path:
    """创建测试 Schema；参数为临时目录，返回文件路径，写入失败时抛 OSError。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    return schema


def adapter_context(
    tmp_path: Path, *, mcp_mode: Literal["minimal", "full"] = "minimal"
) -> AdapterContext:
    """创建 Adapter 上下文；参数为目录/MCP 模式，返回冻结上下文，非法模式时抛错。"""
    policy = CommandPolicy(unrestricted_commands=True)
    return AdapterContext(
        schema_path=schema_file(tmp_path),
        mcp_mode=mcp_mode,
        unrestricted_commands=True,
        codex_resume_sandbox_verified=True,
        command_policy=policy,
    )


def test_adapter_registry_matches_catalog_order_and_rejects_compatibility_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert set(ADAPTER_FACTORIES) == set(AGENT_CATALOG) == set(AgentName)
    assert tuple(ADAPTER_FACTORIES) == (
        AgentName.CODEX,
        AgentName.CLAUDE,
        AgentName.QODER,
        AgentName.OPENCODE,
        AgentName.OPENCLAW,
        AgentName.HERMES,
        AgentName.CODEBUDDY,
    )

    with pytest.raises(LookupError, match="Adapter.*未注册"):
        adapter_factory(cast(AgentName, "codex"))
    with pytest.raises(LookupError, match="Adapter.*未注册"):
        adapter_factory(cast(AgentName, object()))

    monkeypatch.delitem(ADAPTER_FACTORIES, AgentName.CODEX)
    with pytest.raises(LookupError, match="Adapter.*未注册"):
        adapter_factory(AgentName.CODEX)


def test_environment_override_allowlist_is_static_and_agent_scoped() -> None:
    assert tuple(ENVIRONMENT_OVERRIDE_ALLOWLIST) == tuple(AgentName)
    assert {
        AgentName.CODEX: frozenset(),
        AgentName.CLAUDE: frozenset(),
        AgentName.QODER: frozenset(),
        AgentName.OPENCODE: frozenset(
            {
                "OPENCODE_CONFIG_CONTENT",
                "OPENCODE_DISABLE_AUTOUPDATE",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS",
            }
        ),
        AgentName.OPENCLAW: frozenset(),
        AgentName.HERMES: frozenset(),
        AgentName.CODEBUDDY: frozenset(
            {
                "CODEBUDDY_DISABLE_AUTO_MEMORY",
                "CODEBUDDY_DISABLE_HOT_RELOAD",
                "CODEBUDDY_PROMPT_SUGGESTION_DISABLED",
                "CODEBUDDY_SKIP_BUILTIN_MARKETPLACE",
                "DISABLE_AUTOUPDATER",
                "DISABLE_ERROR_REPORTING",
                "DISABLE_TELEMETRY",
            }
        ),
    } == ENVIRONMENT_OVERRIDE_ALLOWLIST
    assert tuple(AGENT_ENVIRONMENT_PASSTHROUGH) == tuple(AgentName)
    assert AGENT_ENVIRONMENT_PASSTHROUGH[AgentName.CODEBUDDY] == frozenset(
        {
            "CODEBUDDY_API_KEY",
            "CODEBUDDY_AUTH_TOKEN",
            "CODEBUDDY_BASE_URL",
            "CODEBUDDY_CUSTOM_HEADERS",
            "CODEBUDDY_INTERNET_ENVIRONMENT",
        }
    )
    assert tuple(SENSITIVE_AGENT_ENVIRONMENT_KEYS) == tuple(AgentName)
    assert SENSITIVE_AGENT_ENVIRONMENT_KEYS[AgentName.CODEBUDDY] == frozenset(
        {
            "CODEBUDDY_API_KEY",
            "CODEBUDDY_AUTH_TOKEN",
            "CODEBUDDY_CUSTOM_HEADERS",
        }
    )


def test_adapter_context_and_named_factories_preserve_vendor_configuration(
    tmp_path: Path,
) -> None:
    context = adapter_context(tmp_path)
    profile = AgentProfile(command="agent", model="model")

    assert [field.name for field in fields(context)] == [
        "schema_path",
        "mcp_mode",
        "unrestricted_commands",
        "codex_resume_sandbox_verified",
        "command_policy",
    ]
    with pytest.raises(FrozenInstanceError):
        context.mcp_mode = "full"  # type: ignore[misc]

    codex = create_codex_adapter(profile, context)
    claude = create_claude_adapter(profile, context)
    qoder = create_qoder_adapter(profile, context)
    from debate_agent.adapters import OpenCodeAdapter, create_opencode_adapter

    opencode = create_opencode_adapter(profile, context)
    openclaw = create_openclaw_adapter(profile, context)
    hermes = create_hermes_adapter(profile, context)
    codebuddy = create_codebuddy_adapter(profile, context)

    assert isinstance(codex, CodexAdapter)
    assert codex.schema_path == context.schema_path.resolve()
    assert codex.resume_sandbox_verified is True
    assert isinstance(claude, ClaudeAdapter)
    assert claude.allowed_tools == context.command_policy.claude_allowed_tools()
    assert claude.minimal_mcp is True
    assert isinstance(qoder, QoderAdapter)
    assert qoder.minimal_mcp is True
    assert qoder.yolo is True
    assert isinstance(opencode, OpenCodeAdapter)
    assert isinstance(openclaw, OpenClawAdapter)
    assert isinstance(hermes, HermesAdapter)
    assert isinstance(codebuddy, CodeBuddyAdapter)

    restricted = replace(
        context,
        unrestricted_commands=False,
        command_policy=CommandPolicy(unrestricted_commands=False),
    )
    with pytest.raises(PolicyViolationError, match="OpenCode|完全授权"):
        create_opencode_adapter(profile, restricted)
    with pytest.raises(PolicyViolationError, match="OpenClaw|完全授权"):
        create_openclaw_adapter(profile, restricted)
    with pytest.raises(PolicyViolationError, match="Hermes|完全授权"):
        create_hermes_adapter(profile, restricted)
    with pytest.raises(PolicyViolationError, match="CodeBuddy|完全授权"):
        create_codebuddy_adapter(profile, restricted)


def test_cli_backend_creates_adapter_through_registered_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RunConfig(workspace=tmp_path, topic="目标", state_root=tmp_path / "state")
    artifacts = ArtifactStore.create(config, run_id="RUN-REGISTRY")
    policy = CommandPolicy(unrestricted_commands=True)
    backend = CLIBackend(
        config=config,
        artifacts=artifacts,
        command_policy=policy,
        env={},
    )
    schema = schema_file(tmp_path)
    captured: list[tuple[AgentProfile, AdapterContext]] = []

    def registered_factory(
        profile: AgentProfile, context: AdapterContext
    ) -> AgentAdapter:
        """记录 Runtime 委托；参数为 Profile/上下文，返回 Codex Adapter。"""
        captured.append((profile, context))
        return CodexAdapter(
            profile,
            schema_path=context.schema_path,
            resume_sandbox_verified=context.codex_resume_sandbox_verified,
        )

    monkeypatch.setitem(ADAPTER_FACTORIES, AgentName.CODEX, registered_factory)
    profile = config.profiles[AgentName.CODEX]

    adapter = backend._adapter(AgentName.CODEX, profile, schema)

    assert isinstance(adapter, CodexAdapter)
    assert len(captured) == 1
    delegated_profile, context = captured[0]
    assert delegated_profile is profile
    assert context == AdapterContext(
        schema_path=schema,
        mcp_mode=config.mcp_mode,
        unrestricted_commands=config.unrestricted_commands,
        codex_resume_sandbox_verified=config.codex_resume_sandbox_verified,
        command_policy=policy,
    )


class RecordingExecutor:
    """记录健康策略生成的命令并返回确定性成功结果。"""

    def __init__(self) -> None:
        """创建记录器；无参数，返回实例，不抛异常。"""
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        stdin: str,
        timeout_seconds: float,
        env: Mapping[str, str],
    ) -> CliCommandResult:
        """执行模拟命令；参数为探针上下文，返回成功结果，不抛异常。"""
        command = tuple(argv)
        self.calls.append(command)
        if command[-1] == "--version":
            return CliCommandResult(return_code=0, stdout="test 1.2.3", stderr="")
        if command[-1] == "strategy-help":
            return CliCommandResult(return_code=0, stdout="strategy-feature", stderr="")
        if command[-1] == "strategy-probe":
            return CliCommandResult(
                return_code=0, stdout="strategy-terminal", stderr=""
            )
        return CliCommandResult(return_code=0, stdout="ok", stderr="")


class RecordingHealthStrategy:
    """用非厂商命令证明健康检查确实经过 Registry 委托。"""

    def __init__(self) -> None:
        """创建策略；无参数，返回实例，不抛异常。"""
        self.discoveries = 0

    def discover_model(
        self, *, home: Path, environment: Mapping[str, str]
    ) -> str | None:
        """返回测试模型；参数为 HOME/环境，返回固定模型，不抛异常。"""
        self.discoveries += 1
        return "strategy-model"

    def help_argv(self, executable: str) -> tuple[str, ...]:
        """构建 help 命令；参数为可执行文件，返回 argv，不抛异常。"""
        return executable, "strategy-help"

    def required_features(self) -> tuple[str, ...]:
        """返回必需特征；无参数，返回固定元组，不抛异常。"""
        return ("strategy-feature",)

    def authentication_argv(self, executable: str) -> tuple[str, ...]:
        """构建认证命令；参数为可执行文件，返回 argv，不抛异常。"""
        return executable, "strategy-auth"

    def authentication_valid(self, result: CliCommandResult) -> bool:
        """验证认证；参数为命令结果，返回是否成功，不抛异常。"""
        return result.return_code == 0

    def mcp_argv(self, executable: str) -> tuple[str, ...]:
        """构建 MCP 命令；参数为可执行文件，返回 argv，不抛异常。"""
        return executable, "strategy-mcp"

    def mcp_valid(self, result: CliCommandResult) -> bool:
        """验证 MCP；参数为命令结果，返回是否成功，不抛异常。"""
        return result.return_code == 0

    def probe_argv(
        self, *, executable: str, model: str, probe_root: Path
    ) -> tuple[str, ...]:
        """构建模型探针；参数为命令/模型/目录，返回 argv，不抛异常。"""
        return executable, model, "strategy-probe"

    def probe_has_terminal_event(self, stdout: str) -> bool:
        """验证终止事件；参数为 stdout，返回是否命中固定事件，不抛异常。"""
        return stdout == "strategy-terminal"


def test_health_registry_matches_catalog_order_and_rejects_compatibility_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert set(HEALTH_STRATEGIES) == set(AGENT_CATALOG) == set(AgentName)
    assert tuple(HEALTH_STRATEGIES) == (
        AgentName.CODEX,
        AgentName.CLAUDE,
        AgentName.QODER,
        AgentName.OPENCODE,
        AgentName.OPENCLAW,
        AgentName.HERMES,
        AgentName.CODEBUDDY,
    )

    with pytest.raises(LookupError, match="健康策略.*未注册"):
        health_strategy(cast(AgentName, "claude"))
    with pytest.raises(LookupError, match="健康策略.*未注册"):
        health_strategy(cast(AgentName, object()))

    monkeypatch.delitem(HEALTH_STRATEGIES, AgentName.CLAUDE)
    with pytest.raises(LookupError, match="健康策略.*未注册"):
        health_strategy(AgentName.CLAUDE)


def test_model_discovery_and_checker_delegate_to_registered_health_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy: CliHealthStrategy = RecordingHealthStrategy()
    monkeypatch.setitem(HEALTH_STRATEGIES, AgentName.CODEX, strategy)

    assert discover_cli_model(AgentName.CODEX, home=tmp_path, environment={}) == (
        "strategy-model"
    )
    executor = RecordingExecutor()
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: "/opt/tools/codex",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "health",
    )

    report = checker.check(
        agent=AgentName.CODEX,
        profile=AgentProfile(command="codex", model="strategy-model"),
        workspace=tmp_path,
        mcp_mode="full",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is True
    assert executor.calls == [
        ("/opt/tools/codex", "--version"),
        ("/opt/tools/codex", "strategy-help"),
        ("/opt/tools/codex", "strategy-auth"),
        ("/opt/tools/codex", "strategy-mcp"),
        ("/opt/tools/codex", "strategy-model", "strategy-probe"),
    ]


def test_opencode_health_registry_uses_real_strategy(tmp_path: Path) -> None:
    strategy = health_strategy(AgentName.OPENCODE)

    assert isinstance(strategy, OpenCodeHealthStrategy)
    assert strategy.discover_model(home=tmp_path, environment={}) is None


def test_openclaw_health_registry_discovers_frozen_model(tmp_path: Path) -> None:
    """验证 OpenClaw 健康策略读取本机默认模型；参数为临时目录，无返回值。"""
    config = tmp_path / ".openclaw" / "openclaw.json"
    config.parent.mkdir()
    config.write_text(
        '{"agents":{"defaults":{"model":{"primary":"provider/model"}}}}',
        encoding="utf-8",
    )
    strategy = health_strategy(AgentName.OPENCLAW)

    assert isinstance(strategy, OpenClawCliHealthStrategy)
    assert strategy.discover_model(home=tmp_path, environment={}) == (
        "provider/model"
    )
    assert strategy.authentication_valid(
        CliCommandResult(
            return_code=0,
            stdout=(
                '{"rpc":{"ok":true},'
                '"config":{"cli":"configured","daemon":"configured"}}'
            ),
            stderr="",
        )
    )
    assert not strategy.authentication_valid(
        CliCommandResult(
            return_code=0,
            stdout='{"rpc":{"ok":false},"config":{}}',
            stderr="",
        )
    )


def test_hermes_health_registry_is_experimental_managed_strategy() -> None:
    """验证 Hermes 注册真实健康策略；无参数，无返回值。"""
    strategy = health_strategy(AgentName.HERMES)

    assert isinstance(strategy, HermesCliHealthStrategy)
    assert strategy.discover_model(
        home=Path("/tmp"),
        environment={"HERMES_MODEL": "deepseek-v4-flash"},
    ) == "deepseek-v4-flash"


def test_codebuddy_health_registry_discovers_user_model(tmp_path: Path) -> None:
    """验证 CodeBuddy 注册真实健康策略并发现用户模型；参数为临时目录，无返回值。"""
    config = tmp_path / ".codebuddy" / "settings.json"
    config.parent.mkdir()
    config.write_text('{"model":"gpt-5"}', encoding="utf-8")

    strategy = health_strategy(AgentName.CODEBUDDY)

    assert isinstance(strategy, CodeBuddyCliHealthStrategy)
    assert strategy.discover_model(home=tmp_path, environment={}) == "gpt-5"
    assert (
        strategy.discover_model(home=tmp_path / "missing", environment={})
        == CLI_DEFAULT_MODEL
    )


@pytest.mark.parametrize(
    ("agent", "help_tail", "auth_tail", "model", "probe_contains", "terminal"),
    [
        (
            AgentName.CODEX,
            ("exec", "--help"),
            ("login", "status"),
            "gpt-test",
            ("exec", "--sandbox", "read-only", "--json", "--output-schema"),
            '{"type":"turn.completed"}',
        ),
        (
            AgentName.CLAUDE,
            ("--help",),
            ("auth", "status", "--json"),
            "sonnet",
            ("--permission-mode", "plan", "--include-partial-messages"),
            '{"type":"result"}',
        ),
        (
            AgentName.QODER,
            ("--help",),
            ("status",),
            CLI_DEFAULT_MODEL,
            ("--yolo", "-p", "--strict-mcp-config"),
            '{"type":"result"}',
        ),
        (
            AgentName.OPENCLAW,
            ("agent", "--help"),
            ("gateway", "status", "--json"),
            "provider/model",
            ("agent", "--session-key", "--model", "--json"),
            '{"status":"ok","result":{"payloads":[{"text":"{\\"ok\\":true}"}]}}',
        ),
        (
            AgentName.CODEBUDDY,
            ("--help",),
            ("config", "get", "model"),
            "gpt-5",
            ("-p", "-y", "--json-schema", "--setting-sources", "--model"),
            (
                '{"session_id":"codebuddy-session",'
                '"structured_output":{"ok":true}}'
            ),
        ),
    ],
)
def test_health_strategies_preserve_vendor_commands_and_terminal_protocol(
    tmp_path: Path,
    agent: AgentName,
    help_tail: tuple[str, ...],
    auth_tail: tuple[str, ...],
    model: str,
    probe_contains: tuple[str, ...],
    terminal: str,
) -> None:
    strategy = health_strategy(agent)

    assert strategy.help_argv("/cli") == ("/cli", *help_tail)
    assert strategy.authentication_argv("/cli") == ("/cli", *auth_tail)
    expected_mcp = (
        ("/cli", "skills", "list", "--json")
        if agent is AgentName.OPENCLAW
        else ("/cli", "mcp", "list")
    )
    assert strategy.mcp_argv("/cli") == expected_mcp
    probe = strategy.probe_argv(
        executable="/cli", model=model, probe_root=tmp_path / "probe"
    )
    assert all(item in probe for item in probe_contains)
    assert strategy.probe_has_terminal_event(terminal)
    assert not strategy.probe_has_terminal_event('{"type":"other"}')
    if agent is AgentName.QODER:
        assert "--model" not in probe
