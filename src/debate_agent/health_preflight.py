"""CLI 新建 Run 预检、身份冻结与恢复重验证。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from debate_agent.circuit_breaker import CircuitBreakerStore
from debate_agent.config import (
    RunConfig,
)
from debate_agent.health_checker import CliHealthChecker
from debate_agent.health_codebuddy import CODEBUDDY_MIN_PROBE_TIMEOUT_SECONDS
from debate_agent.health_models import (
    CliHealthReport,
    CliIdentity,
    CliPreflightError,
    FailureCategory,
)
from debate_agent.health_registry import discover_cli_model
from debate_agent.models import AgentName


def freeze_run_config(
    config: RunConfig,
    *,
    reports: Mapping[AgentName, CliHealthReport],
) -> RunConfig:
    """冻结已验证 CLI 身份。

    参数为原配置和参与者健康报告；返回复制配置；任一报告失败时抛 ValueError。
    """
    profiles = dict(config.profiles)
    for agent in config.participants:
        report = reports.get(agent)
        if report is None or not report.can_start or report.identity is None:
            raise ValueError(f"{agent.value} CLI 健康预检未通过")
        identity = report.identity
        profile_updates: dict[str, object] = {
            "command": identity.executable,
            "version": identity.version,
            "model": identity.model,
            "runtime_config_hash": identity.runtime_config_hash,
            "runtime_config_content_hash": identity.runtime_config_content_hash,
            "runtime_config_resolved_hash": identity.runtime_config_resolved_hash,
        }
        if identity.variant is not None:
            profile_updates["variant"] = identity.variant
        profiles[agent] = config.profiles[agent].model_copy(update=profile_updates)
        runtime_config_capability = report.internal_runtime_config_capability()
        if runtime_config_capability is not None:
            profiles[agent] = profiles[agent].with_internal_runtime_config(
                runtime_config_capability
            )
    return config.model_copy(update={"profiles": profiles})


def preflight_run_config(
    config: RunConfig,
    *,
    checker: CliHealthChecker | None = None,
    circuit_breaker: CircuitBreakerStore | None = None,
    environment: Mapping[str, str] = os.environ,
    home: Path | None = None,
) -> RunConfig:
    """预检并冻结新 Run 的全部参与 CLI。

    参数为配置和可注入检查器/熔断器/环境/HOME；返回冻结配置；任一关键检查失败
    时抛 CliPreflightError，且该函数不获取仓库锁或写租约。
    """
    resolved_home = home or Path(environment.get("HOME", str(Path.home())))
    profiles = dict(config.profiles)
    for agent in config.participants:
        profile = config.profiles[agent]
        model = profile.model or discover_cli_model(
            agent,
            home=resolved_home,
            environment=environment,
        )
        profiles[agent] = profile.model_copy(update={"model": model})
    candidate = config.model_copy(update={"profiles": profiles})
    health_checker = checker or CliHealthChecker(
        environment=environment,
        probe_root=config.state_root / "health" / "probes",
        probe_timeout_seconds=config.health_probe_timeout_seconds,
    )
    breaker = circuit_breaker or CircuitBreakerStore(
        state_root=config.state_root,
        threshold=config.circuit_breaker_threshold,
        window_seconds=config.circuit_breaker_window_seconds,
        cooldown_seconds=config.circuit_breaker_cooldown_seconds,
    )
    reports: dict[AgentName, CliHealthReport] = {}
    infrastructure_categories = {
        FailureCategory.STARTUP_TIMEOUT,
        FailureCategory.IDLE_TIMEOUT,
        FailureCategory.NETWORK_ERROR,
        FailureCategory.CACHE_INCOMPATIBLE,
        FailureCategory.PROCESS_ERROR,
    }
    for agent in candidate.participants:
        report = health_checker.check(
            agent=agent,
            profile=candidate.profiles[agent],
            workspace=candidate.workspace,
            mcp_mode=candidate.mcp_mode,
            proxy_environment_allowlist=candidate.proxy_environment_allowlist,
            circuit_breaker=breaker,
        )
        reports[agent] = report
        if not report.can_start:
            failed = next(check for check in report.checks if check.level == "error")
            if (
                report.identity is not None
                and failed.category in infrastructure_categories
            ):
                breaker.record_failure(
                    report.identity,
                    category=failed.category,
                    signature=f"preflight:{failed.key}:{failed.category.value}",
                )
            raise CliPreflightError(
                f"{agent.value} CLI 预检失败 [{failed.key}]: {failed.detail}"
            )
        if report.identity is not None:
            breaker.record_success(report.identity)
    return freeze_run_config(candidate, reports=reports)


def rebuild_opencode_resume_config(
    config: RunConfig,
    *,
    checker: CliHealthChecker | None = None,
    circuit_breaker: CircuitBreakerStore | None = None,
    environment: Mapping[str, str] = os.environ,
) -> RunConfig:
    """恢复时重新预检并重建 OpenCode 的进程内配置能力。

    参数为持久化 RunConfig、可注入预检器/熔断器/环境；返回仅重新附加
    capability 的配置。OpenCode 未参与时直接返回原配置；预检失败、能力缺失或冻结
    身份（命令、版本、模型、variant、配置摘要）漂移时抛 CliPreflightError。
    """
    if AgentName.OPENCODE not in config.participants:
        return config
    frozen_profile = config.profiles[AgentName.OPENCODE]
    if (
        frozen_profile.version is None
        or frozen_profile.model is None
        or frozen_profile.runtime_config_hash is None
        or frozen_profile.runtime_config_content_hash is None
        or frozen_profile.runtime_config_resolved_hash is None
    ):
        raise CliPreflightError("OpenCode 恢复缺少冻结身份或运行配置摘要")
    health_checker = checker or CliHealthChecker(
        environment=environment,
        probe_root=config.state_root / "health" / "resume-probes",
        probe_timeout_seconds=config.health_probe_timeout_seconds,
    )
    breaker = circuit_breaker or CircuitBreakerStore(
        state_root=config.state_root,
        threshold=config.circuit_breaker_threshold,
        window_seconds=config.circuit_breaker_window_seconds,
        cooldown_seconds=config.circuit_breaker_cooldown_seconds,
    )
    report = health_checker.check(
        agent=AgentName.OPENCODE,
        profile=frozen_profile,
        workspace=config.workspace,
        mcp_mode=config.mcp_mode,
        proxy_environment_allowlist=config.proxy_environment_allowlist,
        circuit_breaker=breaker,
    )
    if not report.can_start or report.identity is None:
        failed = next(
            (check for check in report.checks if check.level == "error"), None
        )
        detail = failed.detail if failed is not None else "健康预检未提供错误详情"
        raise CliPreflightError(f"OpenCode 恢复预检失败: {detail}")
    expected = CliIdentity(
        agent=AgentName.OPENCODE,
        executable=frozen_profile.command,
        version=frozen_profile.version,
        model=frozen_profile.model,
        variant=frozen_profile.variant,
        runtime_config_hash=frozen_profile.runtime_config_hash,
        runtime_config_content_hash=frozen_profile.runtime_config_content_hash,
        runtime_config_resolved_hash=frozen_profile.runtime_config_resolved_hash,
    )
    if report.identity != expected:
        raise CliPreflightError("OpenCode 冻结身份或运行配置摘要发生漂移")
    capability = report.internal_runtime_config_capability()
    if capability is None:
        raise CliPreflightError("OpenCode 恢复预检未重建受控运行配置能力")
    profiles = dict(config.profiles)
    profiles[AgentName.OPENCODE] = frozen_profile.with_internal_runtime_config(
        capability
    )
    return config.model_copy(update={"profiles": profiles})


def rebuild_openclaw_resume_config(
    config: RunConfig,
    *,
    checker: CliHealthChecker | None = None,
    circuit_breaker: CircuitBreakerStore | None = None,
    environment: Mapping[str, str] = os.environ,
) -> RunConfig:
    """恢复时重新验证 OpenClaw 冻结身份。

    参数：config 为持久化配置，其余参数为可注入健康依赖。
    返回值：身份、Profile、模型、Thinking 和配置摘要未漂移的原配置。
    异常：冻结字段缺失、健康失败或身份漂移时抛 CliPreflightError。
    """
    if AgentName.OPENCLAW not in config.participants:
        return config
    profile = config.profiles[AgentName.OPENCLAW]
    if (
        profile.version is None
        or profile.model is None
        or profile.runtime_config_hash is None
    ):
        raise CliPreflightError("OpenClaw 恢复缺少冻结身份或配置摘要")
    health_checker = checker or CliHealthChecker(
        environment=environment,
        probe_root=config.state_root / "health" / "resume-probes",
        probe_timeout_seconds=config.health_probe_timeout_seconds,
    )
    breaker = circuit_breaker or CircuitBreakerStore(
        state_root=config.state_root,
        threshold=config.circuit_breaker_threshold,
        window_seconds=config.circuit_breaker_window_seconds,
        cooldown_seconds=config.circuit_breaker_cooldown_seconds,
    )
    report = health_checker.check(
        agent=AgentName.OPENCLAW,
        profile=profile,
        workspace=config.workspace,
        mcp_mode=config.mcp_mode,
        proxy_environment_allowlist=config.proxy_environment_allowlist,
        circuit_breaker=breaker,
    )
    if not report.can_start or report.identity is None:
        failed = next(
            (check for check in report.checks if check.level == "error"),
            None,
        )
        detail = failed.detail if failed is not None else "健康预检未提供错误详情"
        raise CliPreflightError(f"OpenClaw 恢复预检失败: {detail}")
    expected = CliIdentity(
        agent=AgentName.OPENCLAW,
        executable=profile.command,
        version=profile.version,
        model=profile.model,
        variant=profile.variant,
        runtime_config_hash=profile.runtime_config_hash,
        runtime_config_content_hash=profile.runtime_config_content_hash,
        runtime_config_resolved_hash=profile.runtime_config_resolved_hash,
    )
    if report.identity != expected:
        raise CliPreflightError("OpenClaw 冻结身份、Profile 或配置摘要发生漂移")
    return config


def rebuild_resume_config(
    config: RunConfig,
    *,
    checker: CliHealthChecker | None = None,
    circuit_breaker: CircuitBreakerStore | None = None,
    environment: Mapping[str, str] = os.environ,
) -> RunConfig:
    """恢复时依次重建需要专属验证的 Agent 配置。

    参数：config 为持久化配置，其余参数为共享健康依赖。
    返回值：通过 OpenCode 能力重建、OpenClaw、Hermes 和 CodeBuddy 身份复核的配置。
    异常：任一选中 Agent 恢复预检失败时抛 CliPreflightError。
    """
    rebuilt = rebuild_opencode_resume_config(
        config,
        checker=checker,
        circuit_breaker=circuit_breaker,
        environment=environment,
    )
    rebuilt = rebuild_openclaw_resume_config(
        rebuilt,
        checker=checker,
        circuit_breaker=circuit_breaker,
        environment=environment,
    )
    rebuilt = rebuild_hermes_resume_config(
        rebuilt,
        checker=checker,
        circuit_breaker=circuit_breaker,
        environment=environment,
    )
    return rebuild_codebuddy_resume_config(
        rebuilt,
        checker=checker,
        circuit_breaker=circuit_breaker,
        environment=environment,
    )


def rebuild_hermes_resume_config(
    config: RunConfig,
    *,
    checker: CliHealthChecker | None = None,
    circuit_breaker: CircuitBreakerStore | None = None,
    environment: Mapping[str, str] = os.environ,
) -> RunConfig:
    """恢复时重新验证 Hermes 冻结身份和 Provider 配置摘要。

    参数：config 为持久化配置，其余参数为可注入健康依赖。
    返回值：版本、Provider、Model 和配置摘要未漂移的原配置。
    异常：字段缺失、真实 Query 失败或身份漂移时抛 CliPreflightError。
    """
    if AgentName.HERMES not in config.participants:
        return config
    profile = config.profiles[AgentName.HERMES]
    if (
        profile.version is None
        or profile.provider is None
        or profile.model is None
        or profile.runtime_config_hash is None
    ):
        raise CliPreflightError("Hermes 恢复缺少冻结身份、Provider 或配置摘要")
    health_checker = checker or CliHealthChecker(
        environment=environment,
        probe_root=config.state_root / "health" / "resume-probes",
        probe_timeout_seconds=config.health_probe_timeout_seconds,
    )
    breaker = circuit_breaker or CircuitBreakerStore(
        state_root=config.state_root,
        threshold=config.circuit_breaker_threshold,
        window_seconds=config.circuit_breaker_window_seconds,
        cooldown_seconds=config.circuit_breaker_cooldown_seconds,
    )
    report = health_checker.check(
        agent=AgentName.HERMES,
        profile=profile,
        workspace=config.workspace,
        mcp_mode=config.mcp_mode,
        proxy_environment_allowlist=config.proxy_environment_allowlist,
        circuit_breaker=breaker,
    )
    if not report.can_start or report.identity is None:
        failed = next(
            (check for check in report.checks if check.level == "error"),
            None,
        )
        detail = failed.detail if failed is not None else "健康预检未提供错误详情"
        raise CliPreflightError(f"Hermes 恢复预检失败: {detail}")
    expected = CliIdentity(
        agent=AgentName.HERMES,
        executable=profile.command,
        version=profile.version,
        model=profile.model,
        runtime_config_hash=profile.runtime_config_hash,
    )
    if report.identity != expected:
        raise CliPreflightError("Hermes 冻结版本、Provider 或 Model 发生漂移")
    return config


def rebuild_codebuddy_resume_config(
    config: RunConfig,
    *,
    checker: CliHealthChecker | None = None,
    circuit_breaker: CircuitBreakerStore | None = None,
    environment: Mapping[str, str] = os.environ,
) -> RunConfig:
    """恢复时重新验证 CodeBuddy 冻结身份和 Session 能力。

    参数为持久化配置、可注入健康检查器、熔断器和环境；返回身份未漂移的原配置；
    CodeBuddy 未参与时直接返回，字段缺失、双轮探针失败或摘要漂移时抛预检错误。
    """
    if AgentName.CODEBUDDY not in config.participants:
        return config
    profile = config.profiles[AgentName.CODEBUDDY]
    if (
        profile.version is None
        or profile.model is None
        or profile.runtime_config_hash is None
    ):
        raise CliPreflightError("CodeBuddy 恢复缺少冻结身份或配置摘要")
    health_checker = checker or CliHealthChecker(
        environment=environment,
        probe_root=config.state_root / "health" / "resume-probes",
        probe_timeout_seconds=max(
            config.health_probe_timeout_seconds,
            CODEBUDDY_MIN_PROBE_TIMEOUT_SECONDS,
        ),
    )
    breaker = circuit_breaker or CircuitBreakerStore(
        state_root=config.state_root,
        threshold=config.circuit_breaker_threshold,
        window_seconds=config.circuit_breaker_window_seconds,
        cooldown_seconds=config.circuit_breaker_cooldown_seconds,
    )
    report = health_checker.check(
        agent=AgentName.CODEBUDDY,
        profile=profile,
        workspace=config.workspace,
        mcp_mode=config.mcp_mode,
        proxy_environment_allowlist=config.proxy_environment_allowlist,
        circuit_breaker=breaker,
    )
    if not report.can_start or report.identity is None:
        failed = next(
            (check for check in report.checks if check.level == "error"),
            None,
        )
        detail = failed.detail if failed is not None else "健康预检未提供错误详情"
        raise CliPreflightError(f"CodeBuddy 恢复预检失败: {detail}")
    expected = CliIdentity(
        agent=AgentName.CODEBUDDY,
        executable=profile.command,
        version=profile.version,
        model=profile.model,
        runtime_config_hash=profile.runtime_config_hash,
    )
    if report.identity != expected:
        raise CliPreflightError("CodeBuddy 冻结版本、模型、Agent 或配置摘要发生漂移")
    return config
