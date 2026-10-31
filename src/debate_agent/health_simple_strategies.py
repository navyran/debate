"""Codex、Claude、Qoder 与 OpenClaw 的健康策略。"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from debate_agent.health_models import (
    CLI_DEFAULT_MODEL,
    CliCommandResult,
    _BaseCliHealthStrategy,
)


class CodexCliHealthStrategy(_BaseCliHealthStrategy):
    """实现 Codex CLI 的模型发现和健康命令协议。"""

    def discover_model(
        self, *, home: Path, environment: Mapping[str, str]
    ) -> str | None:
        """发现 Codex 模型；参数为 HOME/环境，返回模型或 None，损坏配置被忽略。"""
        if environment.get("CODEX_MODEL"):
            return environment["CODEX_MODEL"].strip() or None
        path = home / ".codex" / "config.toml"
        if not path.is_file():
            return None
        try:
            module_name = "tomllib" if sys.version_info >= (3, 11) else "tomli"
            toml_module = importlib.import_module(module_name)
            payload = toml_module.loads(path.read_text(encoding="utf-8"))
            model = payload.get("model")
            return str(model).strip() if model else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def help_argv(executable: str) -> tuple[str, ...]:
        """构建 Codex help；参数为可执行文件，返回 exec help argv，不抛异常。"""
        return executable, "exec", "--help"

    @staticmethod
    def required_features() -> tuple[str, ...]:
        """返回 Codex 必需特征；无参数，返回固定标记元组，不抛异常。"""
        return (
            "--json",
            "--output-schema",
            "--ignore-user-config",
            "--sandbox",
            "--model",
        )

    @staticmethod
    def authentication_argv(executable: str) -> tuple[str, ...]:
        """构建 Codex 认证命令；参数为可执行文件，返回 login status argv。"""
        return executable, "login", "status"

    @staticmethod
    def probe_argv(*, executable: str, model: str, probe_root: Path) -> tuple[str, ...]:
        """构建 Codex 只读探针；参数为命令/模型/目录，返回 argv，写入失败时抛错。"""
        probe_root.mkdir(parents=True, exist_ok=True)
        schema_path = probe_root / "probe.schema.json"
        schema_path.write_text(
            '{"type":"object","properties":{"ok":{"type":"boolean"}},'
            '"required":["ok"],"additionalProperties":false}',
            encoding="utf-8",
        )
        return (
            executable,
            "exec",
            "--ignore-user-config",
            "--model",
            model,
            "--sandbox",
            "read-only",
            "--json",
            "--output-schema",
            str(schema_path),
            "-",
        )

    def probe_has_terminal_event(self, stdout: str) -> bool:
        """验证 Codex 终止事件；参数为 stdout，返回是否含 turn.completed。"""
        return self._has_terminal(stdout, "turn.completed")


class ClaudeCliHealthStrategy(_BaseCliHealthStrategy):
    """实现 Claude CLI 的模型发现和健康命令协议。"""

    def discover_model(
        self, *, home: Path, environment: Mapping[str, str]
    ) -> str | None:
        """发现 Claude 模型；参数为 HOME/环境，返回模型或 None，损坏配置被忽略。"""
        for name in ("CLAUDE_MODEL", "ANTHROPIC_MODEL"):
            if environment.get(name):
                return environment[name].strip() or None
        path = home / ".claude" / "settings.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            model = payload.get("model")
            if model:
                return str(model).strip() or None
            settings_environment = payload.get("env")
            if isinstance(settings_environment, dict):
                for name in ("CLAUDE_MODEL", "ANTHROPIC_MODEL"):
                    model = settings_environment.get(name)
                    if model:
                        return str(model).strip() or None
            return None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def help_argv(executable: str) -> tuple[str, ...]:
        """构建 Claude help；参数为可执行文件，返回 help argv，不抛异常。"""
        return executable, "--help"

    @staticmethod
    def required_features() -> tuple[str, ...]:
        """返回 Claude 必需特征；无参数，返回固定标记元组，不抛异常。"""
        return (
            "stream-json",
            "--include-partial-messages",
            "--strict-mcp-config",
            "--json-schema",
            "--model",
        )

    @staticmethod
    def authentication_argv(executable: str) -> tuple[str, ...]:
        """构建 Claude 认证命令；参数为可执行文件，返回 JSON 状态 argv。"""
        return executable, "auth", "status", "--json"

    @staticmethod
    def probe_argv(*, executable: str, model: str, probe_root: Path) -> tuple[str, ...]:
        """构建 Claude 只读探针；参数为命令/模型/目录，返回 argv，不写文件。"""
        return (
            executable,
            "-p",
            "--model",
            model,
            "--permission-mode",
            "plan",
            "--tools",
            "",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--json-schema",
            '{"type":"object","properties":{"ok":{"type":"boolean"}},'
            '"required":["ok"],"additionalProperties":false}',
        )

    def probe_has_terminal_event(self, stdout: str) -> bool:
        """验证 Claude 终止事件；参数为 stdout，返回是否含 result。"""
        return self._has_terminal(stdout, "result")


class QoderCliHealthStrategy(_BaseCliHealthStrategy):
    """实现 Qoder CLI 的模型发现和健康命令协议。"""

    @staticmethod
    def discover_model(*, home: Path, environment: Mapping[str, str]) -> str | None:
        """发现 Qoder 模型；参数为 HOME/环境，返回显式模型或 CLI 默认占位。"""
        model = environment.get("QODER_MODEL", "").strip()
        return model or CLI_DEFAULT_MODEL

    @staticmethod
    def help_argv(executable: str) -> tuple[str, ...]:
        """构建 Qoder help；参数为可执行文件，返回 help argv，不抛异常。"""
        return executable, "--help"

    @staticmethod
    def required_features() -> tuple[str, ...]:
        """返回 Qoder 必需特征；无参数，返回固定标记元组，不抛异常。"""
        return (
            "--output-format",
            "--session-id",
            "--resume",
            "--strict-mcp-config",
        )

    @staticmethod
    def authentication_argv(executable: str) -> tuple[str, ...]:
        """构建 Qoder 认证命令；参数为可执行文件，返回 status argv。"""
        return executable, "status"

    @staticmethod
    def probe_argv(*, executable: str, model: str, probe_root: Path) -> tuple[str, ...]:
        """构建 Qoder 只读探针；参数为命令/模型/目录，返回 yolo argv，不写文件。"""
        model_args = () if model == CLI_DEFAULT_MODEL else ("--model", model)
        return (
            executable,
            "--yolo",
            "-p",
            *model_args,
            "--output-format",
            "stream-json",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        )

    def probe_has_terminal_event(self, stdout: str) -> bool:
        """验证 Qoder 终止事件；参数为 stdout，返回是否含 result。"""
        return self._has_terminal(stdout, "result")


class OpenClawCliHealthStrategy(_BaseCliHealthStrategy):
    """实现 OpenClaw 的模型发现、Gateway 健康与只读模型探针。"""

    @staticmethod
    def discover_model(*, home: Path, environment: Mapping[str, str]) -> str | None:
        """发现 OpenClaw 模型。

        参数：home 为用户目录，environment 为过滤前发现环境。
        返回值：显式环境模型或本机 agents.defaults.model.primary。
        异常：配置缺失或损坏时返回 None。
        """
        configured = environment.get("OPENCLAW_MODEL", "").strip()
        if configured:
            return configured
        path = home / ".openclaw" / "openclaw.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            agents = payload.get("agents") if isinstance(payload, dict) else None
            defaults = agents.get("defaults") if isinstance(agents, dict) else None
            model = defaults.get("model") if isinstance(defaults, dict) else None
            primary = model.get("primary") if isinstance(model, dict) else None
            return (
                primary.strip()
                if isinstance(primary, str) and primary.strip()
                else None
            )
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def help_argv(executable: str) -> tuple[str, ...]:
        """构建 OpenClaw agent help；参数为命令，返回 argv。"""
        return executable, "agent", "--help"

    @staticmethod
    def required_features() -> tuple[str, ...]:
        """返回 OpenClaw 必需参数；无参数，返回固定标记。"""
        return (
            "--message-file",
            "--session-key",
            "--json",
            "--timeout",
            "--model",
            "--thinking",
        )

    @staticmethod
    def authentication_argv(executable: str) -> tuple[str, ...]:
        """构建 Gateway 状态命令；参数为命令，返回只读 JSON argv。"""
        return executable, "gateway", "status", "--json"

    @staticmethod
    def authentication_valid(result: CliCommandResult) -> bool:
        """验证 Gateway 可连接且配置存在。

        参数：result 为状态命令结果。
        返回值：退出成功、rpc.ok 为真且 cli/daemon 配置均存在时为 True。
        异常：无效 JSON 返回 False。
        """
        if result.return_code != 0:
            return False
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        rpc = payload.get("rpc")
        config = payload.get("config")
        return (
            isinstance(rpc, dict)
            and rpc.get("ok") is True
            and isinstance(config, dict)
            and bool(config.get("cli"))
            and bool(config.get("daemon"))
        )

    @staticmethod
    def mcp_argv(executable: str) -> tuple[str, ...]:
        """构建能力清单命令；参数为命令，返回只读 skills list argv。"""
        return executable, "skills", "list", "--json"

    @staticmethod
    def probe_argv(*, executable: str, model: str, probe_root: Path) -> tuple[str, ...]:
        """构建 OpenClaw 只读结构化探针。

        参数：executable 为命令，model 为冻结模型，probe_root 仅用于接口兼容。
        返回值：包含固定非敏感 Prompt 和独立 Session 的 argv。
        异常：不抛出异常。
        """
        del probe_root
        return (
            executable,
            "agent",
            "--session-key",
            f"debate:health:{uuid4().hex}",
            "--message",
            '不要调用工具，只返回 {"ok": true}。',
            "--model",
            model,
            "--thinking",
            "off",
            "--timeout",
            "30",
            "--json",
        )

    @staticmethod
    def probe_has_terminal_event(stdout: str) -> bool:
        """验证 OpenClaw 探针终止 JSON。

        参数：stdout 为完整输出。
        返回值：状态成功且至少有一段非空正文时为 True。
        异常：无效 JSON 返回 False。
        """
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return False
        result = payload.get("result")
        payloads = result.get("payloads") if isinstance(result, dict) else None
        return isinstance(payloads, list) and any(
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and bool(item["text"].strip())
            for item in payloads
        )
