"""CodeBuddy CLI Adapter 与实时流解析。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from debate_agent.adapter_core import (
    AdapterOutputError,
    AdapterStreamEvent,
    Invocation,
    SessionBoundaryError,
    _BaseAdapter,
    _BaseStreamParser,
)
from debate_agent.codebuddy_protocol import (
    decode_codebuddy_output,
    parse_codebuddy_terminal,
)
from debate_agent.config import AgentProfile
from debate_agent.health_models import CLI_DEFAULT_MODEL
from debate_agent.invocation_events import InvocationEventType
from debate_agent.models import AgentName
from debate_agent.process import (
    ProcessOutput,
    ProcessResult,
)


class CodeBuddyStreamParser(_BaseStreamParser):
    """把 CodeBuddy JSON 终止输出映射为受限实时事件。"""

    _MAX_BUFFER_CHARS = 2 * 1024 * 1024

    def __init__(self, *, workspace: Path | None = None) -> None:
        """创建解析器；参数为可选工作区，返回实例，不读取外部状态。"""
        super().__init__(workspace=workspace)
        self._buffer = ""
        self._terminated = False

    def feed(self, output: ProcessOutput) -> tuple[AdapterStreamEvent, ...]:
        """消费 CodeBuddy 输出。

        参数为增量输出；返回安全生命周期事件；单 JSON 尚未完整时继续缓冲，
        超限或 stderr 仅报告固定错误，不回显厂商任意内容。
        """
        if output.stream != "stdout":
            return (
                self._error_event("CodeBuddy 写入了诊断输出")
                if output.text.strip()
                else ()
            )
        if self._terminated or not output.text:
            return ()
        self._buffer += output.text
        if len(self._buffer) > self._MAX_BUFFER_CHARS:
            self._buffer = ""
            return self._error_event("CodeBuddy JSON 输出超过实时解析上限")
        events: list[AdapterStreamEvent] = []
        decoder = json.JSONDecoder()
        while self._buffer:
            stripped = self._buffer.lstrip()
            if not stripped:
                self._buffer = ""
                break
            try:
                payload, consumed = decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                break
            self._buffer = stripped[consumed:]
            events.extend(self._events_for_payload(payload))
            if self._terminated:
                self._buffer = ""
                break
        return tuple(events)

    def _events_for_payload(
        self, payload: object
    ) -> tuple[AdapterStreamEvent, ...]:
        """映射单个 CodeBuddy 事件。

        参数为已解码 JSON 值；返回安全生命周期事件；未知中间事件被忽略。
        """
        if isinstance(payload, list):
            terminal = parse_codebuddy_terminal(payload)
            if terminal is None:
                return self._error_event("CodeBuddy JSON 事件数组缺少合法终止结果")
            self._terminated = True
            return (
                self._event(
                    InvocationEventType.TERMINATED,
                    "CodeBuddy 回合已完成",
                    useful=True,
                    terminal=True,
                ),
            )
        if not isinstance(payload, dict):
            return self._error_event("CodeBuddy JSON 顶层格式无效")
        if payload.get("type") == "system" and payload.get("subtype") == "init":
            return (
                self._event(
                    InvocationEventType.STARTED,
                    "CodeBuddy Session 已建立",
                ),
            )
        if payload.get("type") == "error":
            return self._error_event("CodeBuddy 报告错误")
        if "structured_output" not in payload and payload.get("type") != "result":
            return ()
        self._terminated = True
        return (
            self._event(
                InvocationEventType.TERMINATED,
                "CodeBuddy 回合已完成",
                useful=True,
                terminal=True,
            ),
        )


class CodeBuddyAdapter(_BaseAdapter):
    """构建 CodeBuddy headless 调用并解析结构化 JSON 终止结果。"""

    agent = AgentName.CODEBUDDY
    _SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
    _NATIVE_PROFILE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    _TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
    _ENVIRONMENT_OVERRIDES = (
        ("CODEBUDDY_DISABLE_AUTO_MEMORY", "1"),
        ("CODEBUDDY_DISABLE_HOT_RELOAD", "1"),
        ("CODEBUDDY_PROMPT_SUGGESTION_DISABLED", "1"),
        ("CODEBUDDY_SKIP_BUILTIN_MARKETPLACE", "1"),
        ("DISABLE_AUTOUPDATER", "1"),
        ("DISABLE_ERROR_REPORTING", "1"),
        ("DISABLE_TELEMETRY", "1"),
    )

    def __init__(
        self,
        profile: AgentProfile,
        *,
        schema_path: Path,
        minimal_mcp: bool = True,
    ) -> None:
        """创建 CodeBuddy Adapter。

        参数为冻结 Profile、协议 Schema 和 MCP 模式；返回实例；
        Schema 缺失时抛 ValueError。
        """
        super().__init__(profile, schema_path=schema_path)
        self.minimal_mcp = minimal_mcp
        self._expected_session_id: str | None = None

    def create_stream_parser(
        self, *, workspace: Path | None = None
    ) -> CodeBuddyStreamParser:
        """创建安全流解析器；参数为可选工作区，返回独立解析器。"""
        return CodeBuddyStreamParser(workspace=workspace)

    def build_invocation(
        self, *, worktree: Path, prompt: str, session_id: str | None
    ) -> Invocation:
        """构建 CodeBuddy 无头调用。

        参数为工作树、完整协议 Prompt 和可选历史 Session；返回 stdin 调用；
        原生 Agent 或历史 Session 非法时抛边界错误。
        """
        cwd = worktree.expanduser().resolve()
        if (
            session_id is not None
            and self._SESSION_PATTERN.fullmatch(session_id) is None
        ):
            raise SessionBoundaryError("CodeBuddy Session ID 格式无效")
        native_profile = (
            self.profile.native_profile.strip() if self.profile.native_profile else ""
        )
        if (
            native_profile
            and self._NATIVE_PROFILE_PATTERN.fullmatch(native_profile) is None
        ):
            raise ValueError("CodeBuddy 原生 Agent 名称格式无效")
        schema_payload = json.loads(self.schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema_payload, dict):
            raise ValueError("CodeBuddy JSON Schema 顶层必须是对象")
        schema_payload.pop("$schema", None)
        schema = json.dumps(
            schema_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        argv = [
            self.profile.command,
            "-p",
            "-y",
            "--output-format",
            "stream-json",
            "--json-schema",
            schema,
            "--tools",
            self._TOOLS,
            "--setting-sources",
            "project",
        ]
        if session_id is not None:
            argv.extend(["--resume", session_id])
        if native_profile:
            argv.extend(["--agent", native_profile])
        if self.profile.model and self.profile.model != CLI_DEFAULT_MODEL:
            argv.extend(["--model", self.profile.model.strip()])
        if self.minimal_mcp:
            argv.extend(["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])
        self._expected_session_id = session_id
        return Invocation(
            argv=tuple(argv),
            cwd=cwd,
            stdin=prompt,
            timeout_seconds=self.profile.effective_hard_timeout_seconds,
            startup_timeout_seconds=self.profile.startup_timeout_seconds,
            idle_timeout_seconds=self.profile.idle_timeout_seconds,
            hard_timeout_seconds=self.profile.effective_hard_timeout_seconds,
            heartbeat_interval_seconds=self.profile.heartbeat_interval_seconds,
            session_id=session_id,
            environment_overrides=self._ENVIRONMENT_OVERRIDES,
        )

    def _decode(self, stdout: str) -> tuple[str | None, object]:
        """解码 CodeBuddy JSON。

        参数为完整 stdout；返回唯一 Session 和 structured_output；
        字段缺失、格式异常或恢复串线时抛明确协议错误。
        """
        payload = decode_codebuddy_output(stdout)
        if payload is None:
            raise AdapterOutputError("CodeBuddy JSON/JSONL 终止输出不完整")
        parsed = parse_codebuddy_terminal(payload)
        if parsed is None:
            raise AdapterOutputError("CodeBuddy 响应缺少合法 structured_output")
        actual_session, structured_output = parsed
        if (
            self._expected_session_id is not None
            and actual_session != self._expected_session_id
        ):
            raise SessionBoundaryError("CodeBuddy resume 返回不同 Session ID")
        return actual_session, structured_output

    def _decode_result(self, result: ProcessResult) -> tuple[str | None, object]:
        """解码 CodeBuddy 进程结果。

        参数为受控进程结果；返回 Session/结构化内容；内存截断时读取完整 raw spool。
        """
        if not result.stdout_truncated:
            return self._decode(result.stdout)
        if result.stdout_path is None or not result.stdout_path.is_file():
            raise AdapterOutputError("CLI 结构化输出被截断")
        return self._decode(
            result.stdout_path.read_text(encoding="utf-8", errors="replace")
        )

    def failure_detail(self, stdout: str) -> str | None:
        """返回固定失败摘要；参数为 stdout，返回不含任意厂商原文的文本。"""
        return "CodeBuddy CLI 失败"
