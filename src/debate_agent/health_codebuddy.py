"""CodeBuddy 的双轮 Session 健康策略。"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from debate_agent.circuit_breaker import CircuitBreakerStore
from debate_agent.codebuddy_protocol import parse_codebuddy_terminal
from debate_agent.config import (
    AgentProfile,
)
from debate_agent.health_models import (
    CLI_DEFAULT_MODEL,
    CliCommandResult,
    CliHealthCheck,
    CliHealthStrategyContext,
    CliHealthStrategyResult,
    CliIdentity,
    FailureCategory,
    FailureClassifier,
    _BaseCliHealthStrategy,
)
from debate_agent.models import AgentName

CODEBUDDY_MIN_PROBE_TIMEOUT_SECONDS = 60


class CodeBuddyCliHealthStrategy(_BaseCliHealthStrategy):
    """实现 CodeBuddy 的隔离配置、真实模型和双轮 Session 探针。"""

    _PROBE_SCHEMA = (
        '{"type":"object","properties":{"ok":{"type":"boolean"}},'
        '"required":["ok"],"additionalProperties":false}'
    )
    _ENVIRONMENT_OVERRIDES = {
        "CODEBUDDY_DISABLE_AUTO_MEMORY": "1",
        "CODEBUDDY_DISABLE_HOT_RELOAD": "1",
        "CODEBUDDY_PROMPT_SUGGESTION_DISABLED": "1",
        "CODEBUDDY_SKIP_BUILTIN_MARKETPLACE": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_TELEMETRY": "1",
    }

    @staticmethod
    def discover_model(*, home: Path, environment: Mapping[str, str]) -> str | None:
        """发现 CodeBuddy 模型。

        参数为 HOME 和环境；返回显式环境或用户 settings.json 中的模型；
        配置缺失、损坏或类型非法时返回 CLI 默认模型占位。
        """
        configured = environment.get("CODEBUDDY_MODEL", "").strip()
        if configured:
            return configured
        path = home / ".codebuddy" / "settings.json"
        if not path.is_file():
            return CLI_DEFAULT_MODEL
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return CLI_DEFAULT_MODEL
        model = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(model, str) and model.strip():
            return model.strip()
        return CLI_DEFAULT_MODEL

    @staticmethod
    def help_argv(executable: str) -> tuple[str, ...]:
        """构建 CodeBuddy help；参数为命令，返回 argv。"""
        return executable, "--help"

    @staticmethod
    def required_features() -> tuple[str, ...]:
        """返回 CodeBuddy 必需参数；无参数，返回固定标记。"""
        return (
            "--print",
            "--output-format",
            "--json-schema",
            "--resume",
            "--model",
            "--agent",
            "--tools",
            "--setting-sources",
            "--strict-mcp-config",
            "--mcp-config",
            "--dangerously-skip-permissions",
        )

    @staticmethod
    def authentication_argv(executable: str) -> tuple[str, ...]:
        """构建只读配置查询；参数为命令，返回 argv；真实认证由模型探针判断。"""
        return executable, "config", "get", "model"

    @staticmethod
    def mcp_argv(executable: str) -> tuple[str, ...]:
        """构建 CodeBuddy MCP 清单命令；参数为命令，返回 argv。"""
        return executable, "mcp", "list"

    @staticmethod
    def probe_argv(*, executable: str, model: str, probe_root: Path) -> tuple[str, ...]:
        """构建 CodeBuddy 首轮只读探针。

        参数为命令、模型和临时目录；返回无工具、项目设置源和精简 MCP 的 argv；
        临时目录只作为接口边界，不写入。
        """
        del probe_root
        model_args = () if model == CLI_DEFAULT_MODEL else ("--model", model)
        return (
            executable,
            "-p",
            "-y",
            "--output-format",
            "json",
            "--json-schema",
            CodeBuddyCliHealthStrategy._PROBE_SCHEMA,
            *model_args,
            "--tools",
            "",
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        )

    @classmethod
    def _probe_payload(cls, result: CliCommandResult) -> tuple[str, object] | None:
        """解析 CodeBuddy 探针。

        参数为命令结果；返回合法 Session 和 structured_output，非零退出、无效 JSON、
        字段缺失或 Session 格式异常时返回 None。
        """
        if result.return_code != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return parse_codebuddy_terminal(payload)

    @classmethod
    def probe_has_terminal_event(cls, stdout: str) -> bool:
        """验证 CodeBuddy 终止协议；参数为 stdout，返回是否含合法结构化结果。"""
        parsed = cls._probe_payload(
            CliCommandResult(return_code=0, stdout=stdout, stderr="")
        )
        return parsed is not None

    @staticmethod
    def _probe_is_ok(parsed: tuple[str, object] | None) -> bool:
        """判断探针内容是否可确定性恢复。

        参数为已解析 Session/内容；返回是否明确表示 ok=true；
        内容不是对象或不含完整 JSON 对象时返回 False，不抛异常。
        """
        if parsed is None:
            return False
        content = parsed[1]
        if content == {"ok": True}:
            return True
        if not isinstance(content, str):
            return False
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < start:
            return False
        try:
            recovered: object = json.loads(content[start : end + 1])
            return recovered == {"ok": True}
        except json.JSONDecodeError:
            return False

    @classmethod
    def initial_environment(
        cls, *, base: Mapping[str, str], mcp_mode: str
    ) -> Mapping[str, str]:
        """准备 CodeBuddy 隔离环境；参数为基础环境/MCP 模式，返回新映射。"""
        if mcp_mode not in {"minimal", "full"}:
            raise ValueError("CodeBuddy MCP 模式无效")
        return {**base, **cls._ENVIRONMENT_OVERRIDES}

    @staticmethod
    def _failure_category(result: CliCommandResult) -> FailureCategory:
        """分类 CodeBuddy 失败；参数为结果，返回认证、网络或进程类别。"""
        combined = f"{result.stderr}\n{result.stdout}"
        return FailureClassifier().classify(combined)

    @classmethod
    def _probe_failure_retryable(cls, result: CliCommandResult) -> bool:
        """判断健康探针失败是否适合立即重试。

        参数为命令结果；返回网络或普通进程错误是否可重试；认证等确定性错误返回
        False，不抛异常。
        """
        return result.return_code != 0 and cls._failure_category(result) in {
            FailureCategory.NETWORK_ERROR,
            FailureCategory.PROCESS_ERROR,
        }

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
        """运行 CodeBuddy 专属预检。

        参数为受控上下文、CLI 身份、候选 Profile、MCP 模式和熔断器；
        返回包含双轮真实 Query 的安全检查结果；不保存 Prompt、凭据或 Session 明文。
        """
        checks: list[CliHealthCheck] = []
        model = profile.model.strip() if profile.model else ""
        native_profile = (
            profile.native_profile.strip() if profile.native_profile else ""
        )
        if not model:
            checks.append(
                CliHealthCheck(
                    key="model",
                    label="显式模型",
                    level="error",
                    detail="未配置可冻结的 CodeBuddy 模型",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        if (
            native_profile
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", native_profile)
            is None
        ):
            checks.append(
                CliHealthCheck(
                    key="native_profile",
                    label="原生 Agent",
                    level="error",
                    detail="CodeBuddy 原生 Agent 名称格式无效",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))

        help_result = context.execute(self.help_argv(executable))
        help_text = f"{help_result.stdout}\n{help_result.stderr}"
        if help_result.return_code != 0 or any(
            feature not in help_text for feature in self.required_features()
        ):
            checks.append(
                CliHealthCheck(
                    key="features",
                    label="CodeBuddy CLI 能力",
                    level="error",
                    detail="当前版本缺少 Headless、Schema、Session 或隔离参数",
                    category=FailureCategory.CACHE_INCOMPATIBLE,
                )
            )
            return CliHealthStrategyResult(identity=None, checks=tuple(checks))
        checks.append(
            CliHealthCheck(
                key="features",
                label="CodeBuddy CLI 能力",
                level="success",
                detail="Headless、Schema、Session 和隔离参数齐全",
            )
        )

        config_summary = json.dumps(
            {
                "agent": native_profile or None,
                "mcp_mode": mcp_mode,
                "model": model,
                "setting_sources": "project",
                "tools": "Bash,Read,Edit,Write,Glob,Grep",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        config_hash = hashlib.sha256(config_summary.encode()).hexdigest()
        identity = CliIdentity(
            agent=AgentName.CODEBUDDY,
            executable=executable,
            version=version,
            model=model,
            runtime_config_hash=config_hash,
        )
        if circuit_breaker is not None:
            active = circuit_breaker.active(identity)
            if active is not None:
                checks.append(
                    CliHealthCheck(
                        key="circuit_breaker",
                        label="基础设施熔断",
                        level="error",
                        detail="相同 CodeBuddy 身份仍在冷却期",
                        category=active.category,
                    )
                )
                return CliHealthStrategyResult(
                    identity=identity,
                    checks=tuple(checks),
                )

        if mcp_mode == "full":
            mcp = context.execute(self.mcp_argv(executable))
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="success" if mcp.return_code == 0 else "warning",
                    detail=(
                        "已读取本机 CodeBuddy MCP"
                        if mcp.return_code == 0
                        else "可选 MCP 不可用，将不阻断模型探针"
                    ),
                    category=(
                        None
                        if mcp.return_code == 0
                        else FailureCategory.MCP_OPTIONAL_ERROR
                    ),
                )
            )
        else:
            checks.append(
                CliHealthCheck(
                    key="mcp",
                    label="MCP",
                    level="success",
                    detail="精简模式：严格使用空 MCP 配置",
                )
            )

        try:
            context.probe_root.mkdir(parents=True, exist_ok=True)
            temporary = tempfile.TemporaryDirectory(
                prefix="codebuddy-health-",
                dir=context.probe_root,
            )
        except OSError:
            checks.append(
                CliHealthCheck(
                    key="probe_repository",
                    label="临时 Git 探针库",
                    level="error",
                    detail="无法创建临时 Git 探针库",
                    category=FailureCategory.CONFIGURATION,
                )
            )
            return CliHealthStrategyResult(
                identity=identity,
                checks=tuple(checks),
            )
        try:
            repository = Path(temporary.name).resolve()
            if not context.initialize_git_repository(repository):
                checks.append(
                    CliHealthCheck(
                        key="probe_repository",
                        label="临时 Git 探针库",
                        level="error",
                        detail="无法初始化临时 Git 探针库",
                        category=FailureCategory.CONFIGURATION,
                    )
                )
                return CliHealthStrategyResult(
                    identity=identity,
                    checks=tuple(checks),
                )
            first_argv = list(
                self.probe_argv(
                    executable=executable,
                    model=model,
                    probe_root=repository,
                )
            )
            if mcp_mode == "full":
                mcp_index = first_argv.index("--strict-mcp-config")
                del first_argv[mcp_index : mcp_index + 4]
            if native_profile:
                first_argv.extend(["--agent", native_profile])
            first_payload: tuple[str, object] | None = None
            for _attempt in range(3):
                first = context.execute(
                    first_argv,
                    cwd=repository,
                    stdin=(
                        '最终响应只输出 JSON 对象 {"ok":true}；'
                        "不要调用工具，不要输出解释文字。"
                    ),
                )
                first_payload = self._probe_payload(first)
                if self._probe_is_ok(first_payload):
                    break
                if (
                    first.return_code != 0
                    and not self._probe_failure_retryable(first)
                ):
                    break
            if not self._probe_is_ok(first_payload):
                category = (
                    self._failure_category(first)
                    if first.return_code != 0
                    else FailureCategory.PROTOCOL_ERROR
                )
                checks.append(
                    CliHealthCheck(
                        key="probe",
                        label="CodeBuddy 模型与结构化输出",
                        level="error",
                        detail=f"首轮真实 Query 失败：{category.value}",
                        category=category,
                    )
                )
                return CliHealthStrategyResult(
                    identity=identity,
                    checks=tuple(checks),
                )
            assert first_payload is not None
            session_id = first_payload[0]
            resumed_argv = [*first_argv, "--resume", session_id]
            resumed_payload: tuple[str, object] | None = None
            for _attempt in range(3):
                resumed = context.execute(
                    resumed_argv,
                    cwd=repository,
                    stdin=(
                        '继续同一会话，最终响应只输出 JSON 对象 {"ok":true}；'
                        "不要调用工具，不要输出解释文字。"
                    ),
                    evidence_argv=(
                        *first_argv,
                        "--resume",
                        "<redacted-session>",
                    ),
                )
                resumed_payload = self._probe_payload(resumed)
                if (
                    resumed_payload is not None
                    and resumed_payload[0] == session_id
                    and self._probe_is_ok(resumed_payload)
                ):
                    break
                if (
                    resumed.return_code != 0
                    and not self._probe_failure_retryable(resumed)
                ):
                    break
            if (
                resumed_payload is None
                or resumed_payload[0] != session_id
                or not self._probe_is_ok(resumed_payload)
            ):
                category = (
                    self._failure_category(resumed)
                    if resumed.return_code != 0
                    else FailureCategory.PROTOCOL_ERROR
                )
                checks.append(
                    CliHealthCheck(
                        key="session_resume",
                        label="CodeBuddy Session 复用",
                        level="error",
                        detail=f"显式 resume 探针失败：{category.value}",
                        category=category,
                    )
                )
                return CliHealthStrategyResult(
                    identity=identity,
                    checks=tuple(checks),
                )
        finally:
            try:
                temporary.cleanup()
            except OSError:
                checks.append(
                    CliHealthCheck(
                        key="probe_repository_cleanup",
                        label="临时 Git 探针库清理",
                        level="warning",
                        detail="临时 Git 探针库清理失败",
                        category=FailureCategory.PROCESS_ERROR,
                    )
                )

        checks.extend(
            (
                CliHealthCheck(
                    key="probe",
                    label="CodeBuddy 模型与结构化输出",
                    level="success",
                    detail="模型主连接和 JSON Schema 输出可用",
                ),
                CliHealthCheck(
                    key="session_resume",
                    label="CodeBuddy Session 复用",
                    level="success",
                    detail="显式 Session ID 可稳定恢复",
                ),
                CliHealthCheck(
                    key="settings",
                    label="CodeBuddy 配置边界",
                    level="success",
                    detail="固定 project 设置源、工具白名单和更新/记忆开关",
                ),
            )
        )
        return CliHealthStrategyResult(
            identity=identity,
            checks=tuple(checks),
        )
