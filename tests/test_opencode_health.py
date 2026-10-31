"""test_opencode_health.py 按职责拆分后的测试集合。"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path

import pytest
from opencode_health_support import (
    HELP_FLAGS,
    OpenCodeExecutor,
    check_opencode,
    command_tails,
    freeze_opencode_profile,
)

from debate_agent.cli_health import (
    CliCommandResult,
    CliHealthChecker,
    CliHealthStrategyContext,
    CliHealthStrategyResult,
    CliPreflightError,
    FailureCategory,
    OpenCodeHealthStrategy,
    health_strategy,
    rebuild_opencode_resume_config,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import AgentName


def test_resume_rebuilds_ephemeral_opencode_capability_from_frozen_identity(
    tmp_path: Path,
) -> None:
    """验证恢复只重建进程内能力且不改变冻结的 CLI 身份。"""
    frozen_profile, _ = freeze_opencode_profile(tmp_path, OpenCodeExecutor())
    persisted = RunConfig(
        workspace=tmp_path,
        topic="恢复 OpenCode 运行配置",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        profiles={
            AgentName.CODEX: AgentProfile(
                command="/opt/tools/codex", model="gpt-test", version="1.2.3"
            ),
            AgentName.OPENCODE: AgentProfile.model_validate(
                frozen_profile.model_dump(mode="json")
            ),
        },
    )
    assert (
        persisted.profiles[AgentName.OPENCODE].internal_runtime_config_capability()
        is None
    )
    checker = CliHealthChecker(
        executor=OpenCodeExecutor(),
        which=lambda _command: "/opt/tools/opencode",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "rebuild-probes",
    )

    rebuilt = rebuild_opencode_resume_config(
        persisted,
        checker=checker,
        environment={"PATH": "/opt/tools"},
    )

    assert rebuilt.profiles[AgentName.OPENCODE].model == frozen_profile.model
    assert rebuilt.profiles[AgentName.OPENCODE].variant == frozen_profile.variant
    assert rebuilt.profiles[AgentName.OPENCODE].version == frozen_profile.version
    assert rebuilt.profiles[AgentName.OPENCODE].runtime_config_hash == (
        frozen_profile.runtime_config_hash
    )
    assert (
        rebuilt.profiles[AgentName.OPENCODE].internal_runtime_config_capability()
        is not None
    )

def test_resume_blocks_opencode_identity_or_resolved_config_drift(
    tmp_path: Path,
) -> None:
    """验证恢复预检发现冻结身份漂移时拒绝继续。"""
    frozen_profile, _ = freeze_opencode_profile(tmp_path, OpenCodeExecutor())
    persisted = RunConfig(
        workspace=tmp_path,
        topic="阻止 OpenCode 身份漂移",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        profiles={
            AgentName.CODEX: AgentProfile(
                command="/opt/tools/codex", model="gpt-test", version="1.2.3"
            ),
            AgentName.OPENCODE: AgentProfile.model_validate(
                frozen_profile.model_dump(mode="json")
            ),
        },
    )
    checker = CliHealthChecker(
        executor=OpenCodeExecutor(
            final_config={
                "model": "provider-x/model-y",
                "permission": {"*": "allow"},
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "mcp": {},
                "changed": True,
            }
        ),
        which=lambda _command: "/opt/tools/opencode",
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "drift-probes",
    )

    with pytest.raises(CliPreflightError, match="冻结身份或运行配置摘要发生漂移"):
        rebuild_opencode_resume_config(
            persisted,
            checker=checker,
            environment={"PATH": "/opt/tools"},
        )

def test_opencode_strategy_replaces_bridge_without_checker_agent_branch() -> None:
    strategy = health_strategy(AgentName.OPENCODE)

    assert isinstance(strategy, OpenCodeHealthStrategy)
    assert "OPENCODE" not in inspect.getsource(CliHealthChecker)

def test_strategy_internal_containers_block_generic_sensitive_exports(
    tmp_path: Path,
) -> None:
    marker = "private-proxy-and-config-marker"
    context = CliHealthStrategyContext(
        execute=lambda *_args, **_kwargs: CliCommandResult(
            return_code=0,
            stdout="",
            stderr="",
        ),
        workspace=tmp_path,
        probe_root=tmp_path / "probes",
        environment={"HTTPS_PROXY": marker},
        initialize_git_repository=lambda _path: True,
    )
    result = CliHealthStrategyResult(
        identity=None,
        checks=(),
        runtime_config_content=marker,
    )

    for container in (context, result):
        assert is_dataclass(container) is False
        assert marker not in repr(container)
        with pytest.raises(TypeError):
            asdict(container)
        with pytest.raises(TypeError):
            vars(container)

def test_opencode_missing_executable_fails_before_commands(tmp_path: Path) -> None:
    executor = OpenCodeExecutor()
    checker = CliHealthChecker(
        executor=executor,
        which=lambda _command: None,
        environment={"PATH": "/opt/tools"},
        probe_root=tmp_path / "probes",
    )

    report = checker.check(
        agent=AgentName.OPENCODE,
        profile=AgentProfile(command="opencode"),
        workspace=tmp_path,
        mcp_mode="minimal",
        proxy_environment_allowlist=(),
    )

    assert report.can_start is False
    assert report.by_key("executable").category is FailureCategory.CONFIGURATION
    assert executor.calls == []

def test_opencode_unrecognized_version_fails_before_help(tmp_path: Path) -> None:
    executor = OpenCodeExecutor(version_output="development build")

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("version").category is FailureCategory.CONFIGURATION
    assert command_tails(executor) == [("--version",)]

@pytest.mark.parametrize("model", ["provider", "/model", "provider/", "a/b/c", "a /b"])
def test_opencode_rejects_missing_or_invalid_provider_model(
    tmp_path: Path, model: str
) -> None:
    executor = OpenCodeExecutor(initial_config={"model": model, "mcp": {}})

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("model").category is FailureCategory.CONFIGURATION
    assert not any(
        command[:2] == ("auth", "list") for command in command_tails(executor)
    )

def test_opencode_explicit_model_precedes_resolved_default_and_auth_matches_provider(
    tmp_path: Path,
) -> None:
    executor = OpenCodeExecutor(
        initial_config={"model": "ignored/default", "mcp": {}},
        final_config={
            "model": "ignored/default",
            "permission": {"*": "allow"},
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {},
        },
        auth_output="│  ℹ provider-x oauth",
    )

    report = check_opencode(
        tmp_path,
        executor,
        model="provider-x/model-y",
        variant="quality",
    )

    assert report.can_start is True
    assert report.identity is not None
    assert report.identity.model == "provider-x/model-y"
    assert report.identity.variant == "quality"
    tails = command_tails(executor)
    assert tails[:4] == [
        ("--version",),
        ("run", "--help"),
        ("debug", "config", "--pure"),
        ("auth", "list", "--pure"),
    ]
    assert not any("models" in command for command in tails)

def test_opencode_discovers_only_top_level_resolved_model(tmp_path: Path) -> None:
    executor = OpenCodeExecutor(
        initial_config={
            "model": "provider-x/resolved-model",
            "nested": {"model": "ignored/nested"},
            "mcp": {},
        }
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is True
    assert report.identity is not None
    assert report.identity.model == "provider-x/resolved-model"

def test_opencode_requires_all_run_help_features(tmp_path: Path) -> None:
    executor = OpenCodeExecutor(help_text=HELP_FLAGS.replace("--variant", ""))

    report = check_opencode(tmp_path, executor, model="provider-x/model-y")

    assert report.can_start is False
    assert report.by_key("features").category is FailureCategory.CACHE_INCOMPATIBLE
    assert command_tails(executor) == [("--version",), ("run", "--help")]

def test_opencode_help_requires_exact_option_tokens(tmp_path: Path) -> None:
    deceptive = " ".join(f"{flag}-extended" for flag in HELP_FLAGS.split())

    report = check_opencode(
        tmp_path,
        OpenCodeExecutor(help_text=deceptive),
        model="provider-x/model-y",
    )

    assert report.can_start is False
    assert report.by_key("features").category is FailureCategory.CACHE_INCOMPATIBLE

def test_opencode_auth_accepts_ansi_colored_explicit_provider_record(
    tmp_path: Path,
) -> None:
    executor = OpenCodeExecutor(
        auth_output="│  ℹ \x1b[32mOpenAI\x1b[0m oauth"
    )

    report = check_opencode(tmp_path, executor, model="openai/gpt-5")

    assert report.can_start is True

@pytest.mark.parametrize(
    "record",
    [
        "provider-x",
        "provider-x unauthenticated",
        "provider-x authentication failed",
        "provider-x logged out",
        "│  ℹ provider-x unauthenticated api",
        "│  ℹ provider-x authentication failed oauth",
        "│  ℹ provider-x logged out api",
    ],
)
def test_opencode_auth_rejects_non_credential_records(
    tmp_path: Path,
    record: str,
) -> None:
    report = check_opencode(
        tmp_path,
        OpenCodeExecutor(auth_output=record),
        model="provider-x/model-y",
    )

    assert report.can_start is False
    assert report.by_key("authentication").category is (
        FailureCategory.AUTHENTICATION_ERROR
    )

def test_opencode_auth_rejects_similar_provider_substring(tmp_path: Path) -> None:
    report = check_opencode(
        tmp_path,
        OpenCodeExecutor(auth_output="provider-x-extra authenticated"),
        model="provider-x/model-y",
    )

    assert report.can_start is False
    assert report.by_key("authentication").category is (
        FailureCategory.AUTHENTICATION_ERROR
    )

def test_opencode_auth_must_prove_target_provider_without_output_leak(
    tmp_path: Path,
) -> None:
    executor = OpenCodeExecutor(auth_output="different-provider authenticated")

    report = check_opencode(tmp_path, executor, model="provider-x/model-y")

    assert report.can_start is False
    assert report.by_key("authentication").category is (
        FailureCategory.AUTHENTICATION_ERROR
    )
    serialized = report.model_dump_json()
    assert executor.auth_output not in serialized
    assert "different-provider" not in serialized

def test_opencode_builtin_provider_uses_model_probe_without_local_credentials(
    tmp_path: Path,
) -> None:
    """验证 OpenCode 内置 provider 无本地凭据时仍由真实模型探针判定可用。"""
    executor = OpenCodeExecutor(
        initial_config={"model": "opencode/deepseek-v4-flash-free", "mcp": {}},
        final_config={
            "model": "opencode/deepseek-v4-flash-free",
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {},
        },
        auth_output="└  0 credentials",
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is True
    assert report.identity is not None
    assert report.identity.model == "opencode/deepseek-v4-flash-free"
    assert report.by_key("authentication").detail == (
        "OpenCode 内置 provider 由模型探针验证"
    )

def test_opencode_configured_provider_uses_model_probe_without_auth_record(
    tmp_path: Path,
) -> None:
    """验证配置型 provider 无 auth.json 凭据时仍可由真实模型探针验证。"""
    executor = OpenCodeExecutor(
        initial_config={
            "model": "glm/glm-5.2",
            "provider": {"glm": {"models": {"glm-5.2": {}}}},
            "mcp": {},
        },
        final_config={
            "model": "glm/glm-5.2",
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {},
        },
        auth_output="└  0 credentials",
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is True
    assert report.identity is not None
    assert report.identity.model == "glm/glm-5.2"
    assert report.by_key("authentication").detail == (
        "目标 provider 已由 OpenCode 配置，待模型探针验证"
    )

@pytest.mark.parametrize(
    "permission",
    ["allow", {"*": "allow"}, {"*": "allow", "edit": "ask"}],
)
def test_opencode_accepts_semantic_global_allow(
    tmp_path: Path, permission: object
) -> None:
    executor = OpenCodeExecutor(
        final_config={
            "permission": permission,
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {},
        }
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is True

@pytest.mark.parametrize(
    "final_config",
    [
        {
            "permission": "deny",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {},
        },
        {
            "permission": {"*": "allow", "bash": {"danger": "deny"}},
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {},
        },
        {
            "permission": "allow",
            "agent": {"build": {"permission": {"*": "allow", "edit": "deny"}}},
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {},
        },
    ],
)
def test_opencode_blocks_top_level_or_build_agent_deny(
    tmp_path: Path, final_config: Mapping[str, object]
) -> None:
    report = check_opencode(tmp_path, OpenCodeExecutor(final_config=final_config))

    assert report.can_start is False
    assert report.by_key("permission").category is FailureCategory.POLICY_VIOLATION

@pytest.mark.parametrize(
    ("field", "value"),
    [("share", "enabled"), ("snapshot", True), ("autoupdate", True)],
)
def test_opencode_blocks_unsafe_final_switches(
    tmp_path: Path, field: str, value: object
) -> None:
    final = {
        "permission": "allow",
        "share": "disabled",
        "snapshot": False,
        "autoupdate": False,
        "mcp": {},
    }
    final[field] = value

    report = check_opencode(tmp_path, OpenCodeExecutor(final_config=final))

    assert report.can_start is False
    assert report.by_key("resolved_config_final").category is (
        FailureCategory.CONFIGURATION
    )
