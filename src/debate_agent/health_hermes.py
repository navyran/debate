"""Hermes 的显式 Provider/Model 健康策略。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from debate_agent.circuit_breaker import CircuitBreakerStore
from debate_agent.config import (
    AgentProfile,
)
from debate_agent.health_models import (
    CliHealthCheck,
    CliHealthStrategyContext,
    CliHealthStrategyResult,
    CliIdentity,
    FailureCategory,
    _BaseCliHealthStrategy,
)
from debate_agent.models import AgentName


class HermesCliHealthStrategy(_BaseCliHealthStrategy):
    """实现 Hermes 实验性 CLI 的显式 Provider/Model 真实探针。"""

    @staticmethod
    def discover_model(*, home: Path, environment: Mapping[str, str]) -> str | None:
        """发现 Hermes 模型；参数为 HOME/环境，返回显式环境模型或 None。"""
        del home
        value = environment.get("HERMES_MODEL", "").strip()
        return value or None

    @staticmethod
    def required_features() -> tuple[str, ...]:
        """返回 Hermes 必需参数；无参数，返回固定元组。"""
        return (
            "--provider",
            "--model",
            "--resume",
            "--no-restore-cwd",
            "--yolo",
            "--quiet",
        )

    @staticmethod
    def help_argv(executable: str) -> tuple[str, ...]:
        """构建 Hermes chat help；参数为命令，返回 argv。"""
        return executable, "chat", "--help"

    @staticmethod
    def authentication_argv(executable: str) -> tuple[str, ...]:
        """构建 Hermes 状态命令；参数为命令，返回只读 argv。"""
        return executable, "status"

    @staticmethod
    def mcp_argv(executable: str) -> tuple[str, ...]:
        """构建 Hermes MCP 清单；参数为命令，返回只读 argv。"""
        return executable, "mcp", "list"

    @staticmethod
    def probe_argv(*, executable: str, model: str, probe_root: Path) -> tuple[str, ...]:
        """拒绝缺 Provider 的通用探针；参数用于接口兼容，返回不可执行占位 argv。"""
        del probe_root
        return executable, "chat", "--model", model

    @staticmethod
    def probe_has_terminal_event(stdout: str, stderr: str = "") -> bool:
        """验证 Hermes Quiet 终止边界。

        参数：stdout 为最终正文，stderr 为 v0.19 Session 状态。
        返回值：双流中存在唯一 Session，且 stdout 有非错误正文时为 True。
        异常：不抛出解析异常。
        """
        has_session = any(
            re.fullmatch(
                r"\s*session_id:\s*[A-Za-z0-9][A-Za-z0-9._-]{0,127}\s*",
                line,
            )
            for line in (*stdout.splitlines(), *stderr.splitlines())
        )
        content = [
            line
            for line in stdout.splitlines()
            if line.strip()
            and not line.lstrip().startswith("session_id:")
            and not line.lstrip().startswith("↻ Resumed session ")
        ]
        return has_session and bool(content) and not content[-1].startswith("HTTP ")

    @staticmethod
    def initial_environment(
        *, base: Mapping[str, str], mcp_mode: str
    ) -> Mapping[str, str]:
        """返回 Hermes 隔离环境；参数为基础环境/MCP 模式，返回副本。"""
        del mcp_mode
        return dict(base)

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
        """运行 Hermes 专属预检。

        参数：context 为受控执行器，其余为冻结候选身份和运行模式。
        返回值：包含真实 Query 证据的健康结果。
        异常：不回显 Provider Key 或 Query 正文。
        """
        del mcp_mode, circuit_breaker
        checks: list[CliHealthCheck] = []
        provider = profile.provider.strip() if profile.provider else ""
        model = profile.model.strip() if profile.model else ""
        if not provider or not model:
            checks.append(
                CliHealthCheck(
                    key="provider_model",
                    label="Provider / Model",
                    level="error",
                    detail="必须显式配置 Hermes Provider 和 Model",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        help_result = context.execute(self.help_argv(executable))
        help_text = f"{help_result.stdout}\n{help_result.stderr}"
        if help_result.return_code != 0 or any(
            item not in help_text for item in self.required_features()
        ):
            checks.append(
                CliHealthCheck(
                    key="features",
                    label="Hermes CLI 能力",
                    level="error",
                    detail="当前版本缺少 Quiet/Session/Provider 必需参数",
                    category=FailureCategory.CACHE_INCOMPATIBLE,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        checks.append(
            CliHealthCheck(
                key="features",
                label="Hermes CLI 能力",
                level="success",
                detail="必需参数齐全",
            )
        )
        context.execute(self.authentication_argv(executable))
        probe_prompt = '不要调用工具，只返回 {"ok": true}。'
        probe_argv = (
            executable,
            "chat",
            "--quiet",
            "--yolo",
            "--no-restore-cwd",
            "--provider",
            provider,
            "--model",
            model,
            "--query",
            probe_prompt,
        )
        probe = context.execute(
            probe_argv,
            evidence_argv=(*probe_argv[:-1], "<redacted>"),
        )
        if probe.return_code != 0 or not self.probe_has_terminal_event(
            probe.stdout, probe.stderr
        ):
            category = (
                FailureCategory.AUTHENTICATION_ERROR
                if "HTTP 401" in probe.stdout or "HTTP 401" in probe.stderr
                else FailureCategory.PROTOCOL_ERROR
            )
            checks.append(
                CliHealthCheck(
                    key="probe",
                    label="Hermes 模型与 Session 探针",
                    level="error",
                    detail=f"真实 Query 失败：{category.value}",
                    category=category,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        checks.append(
            CliHealthCheck(
                key="probe",
                label="Hermes 模型与 Session 探针",
                level="success",
                detail="Provider、Model、Quiet 正文和 Session 可用",
            )
        )
        config_hash = hashlib.sha256(
            json.dumps(
                {"provider": provider, "model": model},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return CliHealthStrategyResult(
            identity=CliIdentity(
                agent=AgentName.HERMES,
                executable=executable,
                version=version,
                model=model,
                runtime_config_hash=config_hash,
            ),
            checks=tuple(checks),
        )
