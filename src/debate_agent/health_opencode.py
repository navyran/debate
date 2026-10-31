"""OpenCode 的受控配置与真实模型健康策略。"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from debate_agent.circuit_breaker import CircuitBreakerStore
from debate_agent.config import (
    AgentProfile,
)
from debate_agent.health_models import (
    OPENCODE_SESSION_ID_PATTERN,
    CliCommandResult,
    CliHealthCheck,
    CliHealthStrategyContext,
    CliHealthStrategyResult,
    CliIdentity,
    FailureCategory,
    FailureClassifier,
)
from debate_agent.health_opencode_policy import OpenCodePolicySupport
from debate_agent.models import AgentName


class OpenCodeHealthStrategy(OpenCodePolicySupport):
    """实现 OpenCode 的受控配置、认证、模型探针与 Session 清理。"""

    @staticmethod
    def discover_model(*, home: Path, environment: Mapping[str, str]) -> str | None:
        """延迟 OpenCode 模型发现；参数为 HOME/环境；返回 None；不读取本机配置。"""
        return None

    @staticmethod
    def help_argv(executable: str) -> tuple[str, ...]:
        """构建 OpenCode run help；参数为命令；返回 argv；不抛异常。"""
        return executable, "run", "--help"

    @staticmethod
    def required_features() -> tuple[str, ...]:
        """返回 OpenCode 必需参数；无参数；返回固定元组；不抛异常。"""
        return (
            "--format",
            "--dir",
            "--model",
            "--agent",
            "--auto",
            "--pure",
            "--session",
            "--variant",
        )

    @staticmethod
    def authentication_argv(executable: str) -> tuple[str, ...]:
        """构建 OpenCode 认证查询；参数为命令；返回纯模式 argv；不抛异常。"""
        return executable, "auth", "list", "--pure"

    @staticmethod
    def authentication_valid(result: CliCommandResult) -> bool:
        """验证认证命令退出码；参数为结果；返回是否成功；不抛异常。"""
        return result.return_code == 0

    @staticmethod
    def mcp_argv(executable: str) -> tuple[str, ...]:
        """构建 OpenCode MCP 查询；参数为命令；返回纯模式 argv；不抛异常。"""
        return executable, "mcp", "list", "--pure"

    @staticmethod
    def mcp_valid(result: CliCommandResult) -> bool:
        """验证 MCP 查询退出码；参数为结果；返回是否成功；不抛异常。"""
        return result.return_code == 0

    def probe_argv(
        self, *, executable: str, model: str, probe_root: Path
    ) -> tuple[str, ...]:
        """构建 OpenCode 新 Session 探针；参数为命令/模型/目录；返回 argv。"""
        return (
            executable,
            "run",
            "--format",
            "json",
            "--dir",
            str(probe_root),
            "--model",
            model,
            "--agent",
            "build",
            "--auto",
            "--pure",
        )

    @staticmethod
    def probe_has_terminal_event(stdout: str) -> bool:
        """验证 OpenCode 基础终止事件；参数为 JSONL；返回是否含 Session 与 text。"""
        session_ids, last_text, _error, _wait, invalid = (
            OpenCodeHealthStrategy._probe_protocol(stdout)
        )
        return not invalid and len(session_ids) == 1 and last_text is not None

    def run_managed_checks(
        self,
        *,
        context: CliHealthStrategyContext,
        executable: str,
        version: str,
        profile: AgentProfile,
        mcp_mode: str,
        circuit_breaker: CircuitBreakerStore | None,
    ) -> CliHealthStrategyResult:
        """按 OpenCode 顺序执行检查；参数为受控上下文/身份/Profile；返回安全结果。"""
        checks: list[CliHealthCheck] = []
        help_result = context.execute(self.help_argv(executable))
        help_text = f"{help_result.stdout}\n{help_result.stderr}"
        options = set(re.findall(r"(?<![\w-])--[A-Za-z0-9][A-Za-z0-9-]*", help_text))
        missing = [item for item in self.required_features() if item not in options]
        if help_result.return_code != 0 or missing:
            checks.append(
                CliHealthCheck(
                    key="features",
                    label="结构化流能力",
                    level="error",
                    detail="当前版本缺少所需参数",
                    category=FailureCategory.CACHE_INCOMPATIBLE,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        checks.append(
            CliHealthCheck(
                key="features",
                label="结构化流能力",
                level="success",
                detail="必需参数齐全",
            )
        )

        first_debug = context.execute((executable, "debug", "config", "--pure"))
        initial_config = self._resolved_config(first_debug)
        del first_debug
        if initial_config is None:
            checks.append(
                CliHealthCheck(
                    key="resolved_config_initial",
                    label="初始 resolved config",
                    level="error",
                    detail="无法解析安全配置",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        checks.append(
            CliHealthCheck(
                key="resolved_config_initial",
                label="初始 resolved config",
                level="success",
                detail="配置可解析",
            )
        )
        mcp_names = self._mcp_names(initial_config)
        if mcp_names is None:
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="error",
                    detail="MCP 配置名称或数量无效",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        mcp_configured_count = len(mcp_names)
        mcp_names_hash = self._mcp_names_hash(mcp_names)
        if mcp_names_hash is None:
            checks.append(
                CliHealthCheck(
                    key="resolved_config_initial",
                    label="初始 resolved config",
                    level="error",
                    detail="无法解析安全配置",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        resolved_model = initial_config.get("model")

        candidate_model: object = profile.model or resolved_model
        model = self._model(candidate_model)
        if model is None:
            checks.append(
                CliHealthCheck(
                    key="model",
                    label="显式模型",
                    level="error",
                    detail="模型必须是单个 provider/model",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        checks.append(
            CliHealthCheck(key="model", label="显式模型", level="success", detail=model)
        )

        provider = model.split("/", maxsplit=1)[0]
        auth = context.execute(self.authentication_argv(executable))
        built_in_provider = provider.casefold() == "opencode"
        configured_provider = self._provider_configured(initial_config, provider)
        provider_authenticated = (
            built_in_provider
            or configured_provider
            or self._provider_authenticated(auth, provider)
        )
        del initial_config
        del auth
        if not provider_authenticated:
            checks.append(
                CliHealthCheck(
                    key="authentication",
                    label="登录状态",
                    level="error",
                    detail="目标模型 provider 未认证",
                    category=FailureCategory.AUTHENTICATION_ERROR,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        checks.append(
            CliHealthCheck(
                key="authentication",
                label="登录状态",
                level="success",
                detail=(
                    "OpenCode 内置 provider 由模型探针验证"
                    if built_in_provider
                    else (
                        "目标 provider 已由 OpenCode 配置，待模型探针验证"
                        if configured_provider
                        else "目标 provider 凭据可用"
                    )
                ),
            )
        )

        config_content = self._config_content(
            mcp_names if mcp_mode == "minimal" else None
        )
        runtime_config_content_hash = hashlib.sha256(
            config_content.encode()
        ).hexdigest()
        final_environment = self._controlled_environment(
            context.environment, config_content=config_content
        )
        final_debug = context.execute(
            (executable, "debug", "config", "--pure"),
            environment=final_environment,
        )
        final_config = self._resolved_config(final_debug)
        del final_debug
        identity = CliIdentity(
            agent=AgentName.OPENCODE,
            executable=executable,
            version=version,
            model=model,
            variant=profile.variant,
            runtime_config_content_hash=runtime_config_content_hash,
        )
        if final_config is None:
            checks.append(
                CliHealthCheck(
                    key="resolved_config_final",
                    label="最终安全配置",
                    level="error",
                    detail="最终配置不满足安全约束",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(
                identity=identity,
                checks=tuple(checks),
                mcp_configured_count=mcp_configured_count,
                mcp_names_hash=mcp_names_hash,
            )
        runtime_config_resolved_hash = self._resolved_config_hash(final_config)
        if runtime_config_resolved_hash is None:
            checks.append(
                CliHealthCheck(
                    key="resolved_config_final",
                    label="最终安全配置",
                    level="error",
                    detail="最终配置不满足安全约束",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(
                identity=identity,
                checks=tuple(checks),
                mcp_configured_count=mcp_configured_count,
                mcp_names_hash=mcp_names_hash,
            )
        permission_valid = self._permission_valid(final_config)
        safety_switches_valid = self._safety_switches_valid(final_config)
        final_mcp_names = self._mcp_names(final_config)
        mcp_enabled_count = self._mcp_enabled_count(final_config)
        del final_config
        if not permission_valid:
            checks.append(
                CliHealthCheck(
                    key="permission",
                    label="完全授权",
                    level="error",
                    detail="最终权限不是无 deny 的全局 allow",
                    category=FailureCategory.POLICY_VIOLATION,
                )
            )
            return CliHealthStrategyResult(
                identity=identity,
                checks=tuple(checks),
                mcp_configured_count=mcp_configured_count,
                mcp_names_hash=mcp_names_hash,
            )
        if not safety_switches_valid:
            checks.append(
                CliHealthCheck(
                    key="resolved_config_final",
                    label="最终安全配置",
                    level="error",
                    detail="最终共享、快照或升级开关不安全",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(
                identity=identity,
                checks=tuple(checks),
                mcp_configured_count=mcp_configured_count,
                mcp_names_hash=mcp_names_hash,
            )
        if mcp_enabled_count is None:
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="error",
                    detail="最终 MCP 配置无效",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(
                identity=identity,
                checks=tuple(checks),
                mcp_configured_count=mcp_configured_count,
                mcp_names_hash=mcp_names_hash,
            )
        assert final_mcp_names is not None
        final_mcp_names_hash = self._mcp_names_hash(final_mcp_names)
        if final_mcp_names_hash is None:
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="error",
                    detail="最终 MCP 配置无效",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(
                identity=identity,
                checks=tuple(checks),
                mcp_configured_count=mcp_configured_count,
                mcp_names_hash=mcp_names_hash,
            )
        if mcp_mode == "minimal" and mcp_enabled_count != 0:
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="error",
                    detail="精简模式仍有 MCP 启用项",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(
                identity=identity,
                checks=tuple(checks),
                mcp_configured_count=mcp_configured_count,
                mcp_enabled_count=mcp_enabled_count,
                mcp_names_hash=mcp_names_hash,
            )
        runtime_identity_summary = json.dumps(
            {
                "mcp_configured_count": mcp_configured_count,
                "mcp_enabled_count": mcp_enabled_count,
                "mcp_mode": mcp_mode,
                "mcp_names_hash": mcp_names_hash,
                "permission_safe": permission_valid,
                "runtime_config_resolved_hash": runtime_config_resolved_hash,
                "resolved_mcp_names_hash": final_mcp_names_hash,
                "safety_switches_safe": safety_switches_valid,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        identity = identity.model_copy(
            update={
                "runtime_config_hash": hashlib.sha256(
                    runtime_identity_summary.encode()
                ).hexdigest(),
                "runtime_config_resolved_hash": runtime_config_resolved_hash,
            }
        )
        checks.append(
            CliHealthCheck(
                key="resolved_config_final",
                label="最终安全配置",
                level="success",
                detail="完全授权与隔离配置有效",
            )
        )

        if circuit_breaker is not None:
            active = circuit_breaker.active(identity)
            if active is not None:
                checks.append(
                    CliHealthCheck(
                        key="circuit_breaker",
                        label="基础设施熔断",
                        level="error",
                        detail="相同 CLI 身份的基础设施故障仍在冷却期",
                        category=active.category,
                    )
                )
                return CliHealthStrategyResult(
                    identity=identity,
                    checks=tuple(checks),
                    mcp_configured_count=mcp_configured_count,
                    mcp_enabled_count=mcp_enabled_count,
                    mcp_names_hash=mcp_names_hash,
                )

        mcp_list_total_count: int | None = None
        mcp_list_enabled_count: int | None = None
        mcp_list_healthy_count: int | None = None
        mcp_list_status: str | None = None
        if mcp_mode == "full":
            mcp = context.execute(
                self.mcp_argv(executable), environment=final_environment
            )
            summary = self._mcp_list_summary(mcp)
            if mcp.return_code != 0:
                mcp_list_status = "unavailable"
            elif summary is None:
                mcp_list_status = "invalid"
            else:
                mcp_list_total_count = summary.total_count
                mcp_list_enabled_count = summary.enabled_count
                mcp_list_healthy_count = summary.healthy_count
                mcp_list_status = summary.status
            del mcp
            inconsistent = summary is not None and (
                summary.total_count != len(final_mcp_names)
                or summary.enabled_count != mcp_enabled_count
                or summary.names_hash != final_mcp_names_hash
            )
            if inconsistent:
                mcp_list_status = "inconsistent"
            healthy = (
                summary is not None and summary.status == "healthy" and not inconsistent
            )
            if healthy:
                assert summary is not None
                detail = (
                    "已读取 MCP："
                    f"总数 {summary.total_count}，"
                    f"启用 {summary.enabled_count}，"
                    f"健康 {summary.healthy_count}"
                )
            elif inconsistent:
                detail = "MCP list 与最终配置状态不一致，将不阻断模型探针"
            else:
                detail = "可选 MCP 状态不可用或异常，将不阻断模型探针"
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="success" if healthy else "warning",
                    detail=detail,
                    category=None if healthy else FailureCategory.MCP_OPTIONAL_ERROR,
                )
            )
        else:
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="success",
                    detail="精简模式：最终启用项 0",
                )
            )

        probe: CliCommandResult | None = None
        session_ids: tuple[str, ...] = ()
        last_text: str | None = None
        error = False
        wait = False
        invalid = False
        invalid_session = False
        cleanup_failed = False
        repository_failed = False
        repository_cleanup_failed = False
        try:
            context.probe_root.mkdir(parents=True, exist_ok=True)
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="opencode-health-", dir=context.probe_root
            )
        except OSError:
            repository_failed = True
        else:
            try:
                repository = Path(temporary_directory.name).resolve()
                try:
                    repository_ready = context.initialize_git_repository(repository)
                except OSError:
                    repository_ready = False
                if not repository_ready:
                    repository_failed = True
                else:
                    probe_argv = list(
                        self.probe_argv(
                            executable=executable,
                            model=model,
                            probe_root=repository,
                        )
                    )
                    if profile.variant:
                        probe_argv.extend(("--variant", profile.variant))
                    probe = context.execute(
                        probe_argv,
                        cwd=repository,
                        stdin='只返回 {"ok":true} 且不要修改任何文件。',
                        environment=final_environment,
                    )
                    (
                        session_ids,
                        last_text,
                        error,
                        wait,
                        invalid,
                    ) = self._probe_protocol(probe.stdout)
                    for session_id in session_ids:
                        if OPENCODE_SESSION_ID_PATTERN.fullmatch(session_id) is None:
                            invalid_session = True
                            cleanup_failed = True
                            continue
                        cleanup = context.execute(
                            (
                                executable,
                                "session",
                                "delete",
                                session_id,
                                "--pure",
                            ),
                            environment=final_environment,
                            evidence_argv=(
                                executable,
                                "session",
                                "delete",
                                "<redacted-session>",
                                "--pure",
                            ),
                        )
                        cleanup_failed = cleanup_failed or cleanup.return_code != 0
            finally:
                try:
                    temporary_directory.cleanup()
                except OSError:
                    repository_cleanup_failed = True
        if repository_failed:
            checks.append(
                CliHealthCheck(
                    key="probe_repository",
                    label="临时 Git 探针库",
                    level="error",
                    detail="无法创建临时 Git 探针库",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            if repository_cleanup_failed:
                checks.append(
                    CliHealthCheck(
                        key="probe_repository_cleanup",
                        label="临时 Git 探针库清理",
                        level="warning",
                        detail="临时 Git 探针库清理失败",
                        category=FailureCategory.PROCESS_ERROR,
                    )
                )
            return CliHealthStrategyResult(identity=identity, checks=tuple(checks))
        assert probe is not None

        probe_failed = (
            probe.return_code != 0
            or error
            or wait
            or invalid
            or invalid_session
            or len(session_ids) != 1
            or last_text is None
        )
        if probe_failed:
            if wait:
                category = FailureCategory.POLICY_VIOLATION
            elif probe.return_code != 0:
                category = FailureClassifier().classify(
                    f"{probe.stderr}\n{probe.stdout}"
                )
            else:
                category = FailureCategory.PROTOCOL_ERROR
            checks.append(
                CliHealthCheck(
                    key="probe",
                    label="模型结构化探针",
                    level="error",
                    detail=f"探针失败：{category.value}",
                    category=category,
                )
            )
        else:
            checks.append(
                CliHealthCheck(
                    key="probe",
                    label="模型结构化探针",
                    level="success",
                    detail="模型主连接与结构化输出可用",
                )
            )
        if cleanup_failed:
            checks.append(
                CliHealthCheck(
                    key="session_cleanup",
                    label="Session 清理",
                    level="warning",
                    detail="探针 Session 清理失败",
                    category=FailureCategory.PROCESS_ERROR,
                )
            )
        if repository_cleanup_failed:
            checks.append(
                CliHealthCheck(
                    key="probe_repository_cleanup",
                    label="临时 Git 探针库清理",
                    level="warning",
                    detail="临时 Git 探针库清理失败",
                    category=FailureCategory.PROCESS_ERROR,
                )
            )
        return CliHealthStrategyResult(
            identity=identity,
            checks=tuple(checks),
            runtime_config_content=config_content,
            runtime_config_content_hash=runtime_config_content_hash,
            mcp_configured_count=mcp_configured_count,
            mcp_enabled_count=mcp_enabled_count,
            mcp_list_total_count=mcp_list_total_count,
            mcp_list_enabled_count=mcp_list_enabled_count,
            mcp_list_healthy_count=mcp_list_healthy_count,
            mcp_list_status=mcp_list_status,
            mcp_mode=mcp_mode,
            mcp_names=mcp_names,
            mcp_names_hash=mcp_names_hash,
            runtime_config_resolved_hash=runtime_config_resolved_hash,
        )
