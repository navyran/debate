"""Claude Code CLI Adapter 与实时流解析。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import uuid4

from debate_agent.adapter_core import (
    AdapterOutputError,
    AdapterStreamEvent,
    Invocation,
    _BaseAdapter,
    _BaseStreamParser,
    _safe_changed_paths,
)
from debate_agent.config import AgentProfile
from debate_agent.invocation_events import InvocationEventType
from debate_agent.models import AgentName
from debate_agent.process import (
    ProcessOutput,
    ProcessResult,
)


class ClaudeStreamParser(_BaseStreamParser):
    """把 Claude stream-json 映射为受限实时事件。"""

    def _content_events(self, content: object) -> tuple[AdapterStreamEvent, ...]:
        """映射 Claude 内容块。

        参数为 message content；返回工具、文件或文本事件；未知内容块被丢弃。
        """
        blocks = content if isinstance(content, list) else [content]
        events: list[AdapterStreamEvent] = []
        for block in blocks:
            if isinstance(block, str):
                events.append(
                    self._event(
                        InvocationEventType.MESSAGE,
                        "Claude 更新结论",
                        summary=block,
                        useful=True,
                    )
                )
                continue
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                events.append(
                    self._event(
                        InvocationEventType.MESSAGE,
                        "Claude 更新结论",
                        summary=block.get("text", ""),
                        useful=True,
                    )
                )
                continue
            if block_type != "tool_use":
                continue
            name = str(block.get("name", "tool"))
            tool_input = block.get("input")
            path: object = None
            if isinstance(tool_input, dict):
                path = tool_input.get("file_path") or tool_input.get("path")
            paths = _safe_changed_paths((path,), workspace=self.workspace)
            events.append(
                self._event(
                    InvocationEventType.FILE_CHANGED
                    if name in {"Edit", "Write", "NotebookEdit"} and paths
                    else InvocationEventType.TOOL_STARTED,
                    f"Claude 调用 {name}",
                    changed_paths=paths,
                    useful=True,
                )
            )
        return tuple(events)

    def feed(self, output: ProcessOutput) -> tuple[AdapterStreamEvent, ...]:
        """消费一条 Claude 输出。

        参数为增量输出；返回零或多条安全事件；partial 只展示、不作为最终协议。
        """
        if output.stream != "stdout":
            return self._error_event(output.text.strip()) if output.text.strip() else ()
        try:
            payload = json.loads(output.text)
        except json.JSONDecodeError:
            if not output.complete_line:
                return ()
            return self._error_event("Claude 输出了无效 stream-json")
        if not isinstance(payload, dict):
            return ()
        event_type = payload.get("type")
        if event_type == "system" and payload.get("subtype") == "init":
            return (self._event(InvocationEventType.STARTED, "Claude Session 已建立"),)
        if event_type == "error":
            return self._error_event(payload.get("error") or payload.get("message"))
        if event_type == "assistant":
            message = payload.get("message")
            return self._content_events(
                message.get("content") if isinstance(message, dict) else None
            )
        if event_type == "stream_event":
            event = payload.get("event")
            if not isinstance(event, dict):
                return ()
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                return (
                    self._event(
                        InvocationEventType.MESSAGE,
                        "Claude 正在生成",
                        summary=delta.get("text", ""),
                        useful=True,
                    ),
                )
            return ()
        if event_type == "user":
            return (
                self._event(
                    InvocationEventType.TOOL_COMPLETED,
                    "Claude 工具已返回",
                    useful=True,
                ),
            )
        if event_type == "result":
            return (
                self._event(
                    InvocationEventType.TERMINATED,
                    "Claude 回合已完成",
                    useful=True,
                    terminal=True,
                ),
            )
        return ()


class ClaudeAdapter(_BaseAdapter):
    """构建 Claude print 调用并解析 stream-json 或历史单 JSON 结果。"""

    agent = AgentName.CLAUDE

    def __init__(
        self,
        profile: AgentProfile,
        *,
        schema_path: Path,
        allowed_tools: tuple[str, ...],
        minimal_mcp: bool = True,
    ) -> None:
        """创建 Claude Adapter。

        参数为 Profile、Schema 和固定工具规则；返回实例；规则为空时抛 ValueError。
        """
        super().__init__(profile, schema_path=schema_path)
        if not allowed_tools:
            raise ValueError("Claude allowed_tools 不能为空")
        self.allowed_tools = allowed_tools
        self.minimal_mcp = minimal_mcp

    def create_stream_parser(
        self, *, workspace: Path | None = None
    ) -> ClaudeStreamParser:
        """创建 Claude 增量解析器；参数为可选工作区，返回独立解析器，不抛异常。"""
        return ClaudeStreamParser(workspace=workspace)

    def build_invocation(
        self, *, worktree: Path, prompt: str, session_id: str | None
    ) -> Invocation:
        """构建 Claude argv。

        参数为 worktree、Prompt 和可选 Session；返回调用；Schema 读取失败时传播异常。
        """
        cwd = worktree.expanduser().resolve()
        effective_session_id = session_id or str(uuid4())
        schema_payload = json.loads(self.schema_path.read_text(encoding="utf-8"))
        schema_payload.pop("$schema", None)
        schema = json.dumps(
            schema_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        argv = [self.profile.command, "-p"]
        if session_id is None:
            argv.extend(["--session-id", effective_session_id])
        else:
            argv.extend(["--resume", effective_session_id])
        argv.extend(
            [
                "--permission-mode",
                "acceptEdits",
                "--tools",
                "Read,Edit,Write,Glob,Grep,Bash",
                "--allowedTools",
                ",".join(self.allowed_tools),
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--json-schema",
                schema,
            ]
        )
        if self.minimal_mcp:
            argv.extend(["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])
        if self.profile.model:
            argv.extend(["--model", self.profile.model])
        return Invocation(
            argv=tuple(argv),
            cwd=cwd,
            stdin=prompt,
            timeout_seconds=self.profile.effective_hard_timeout_seconds,
            startup_timeout_seconds=self.profile.startup_timeout_seconds,
            idle_timeout_seconds=self.profile.idle_timeout_seconds,
            hard_timeout_seconds=self.profile.effective_hard_timeout_seconds,
            heartbeat_interval_seconds=self.profile.heartbeat_interval_seconds,
            session_id=effective_session_id,
        )

    @staticmethod
    def _decode_response(response: dict[str, Any]) -> tuple[str | None, object]:
        """解码 Claude 终止对象。

        参数为 result 对象；返回 Session/协议；缺少终止字段时抛 KeyError。
        """
        if response.get("type") not in {None, "result"}:
            raise KeyError("Claude 未返回 result 终止事件")
        session_id = response["session_id"]
        if "structured_output" in response:
            content = response["structured_output"]
        else:
            content = response["result"]
        return session_id, content

    @classmethod
    def _decode_lines(cls, lines: Iterable[str]) -> tuple[str | None, object]:
        """流式解码 Claude JSONL；参数为文本行，返回最终 result，断流时抛错。"""
        terminal: dict[str, Any] | None = None
        for line in lines:
            if not line.strip():
                continue
            response = json.loads(line)
            if isinstance(response, dict) and response.get("type") == "result":
                terminal = response
        if terminal is None:
            raise KeyError("Claude 未返回 result 终止事件")
        return cls._decode_response(terminal)

    def _decode(self, stdout: str) -> tuple[str | None, object]:
        """解码 Claude 双格式输出；参数为 stdout，返回 Session/原文，断流时抛错。"""
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError:
            return self._decode_lines(stdout.splitlines())
        if not isinstance(response, dict):
            raise TypeError("Claude 响应不是对象")
        if response.get("type") == "result" or "session_id" in response:
            return self._decode_response(response)
        return self._decode_lines((stdout,))

    def _decode_result(self, result: ProcessResult) -> tuple[str | None, object]:
        """解码 Claude 结果；参数为结果，内存截断时读取完整 raw JSONL。"""
        if not result.stdout_truncated:
            return self._decode(result.stdout)
        if result.stdout_path is None or not result.stdout_path.is_file():
            raise AdapterOutputError("CLI 结构化输出被截断")
        with result.stdout_path.open(encoding="utf-8", errors="replace") as handle:
            return self._decode_lines(handle)

    def failure_detail(self, stdout: str) -> str | None:
        """提取 Claude 终止原因。

        参数为 stdout；返回截断后的错误摘要；解析失败时返回空。
        """
        try:
            response = json.loads(stdout.splitlines()[-1])
        except (IndexError, json.JSONDecodeError, TypeError):
            return None
        parts: list[str] = []
        terminal_reason = response.get("terminal_reason")
        if isinstance(terminal_reason, str) and terminal_reason:
            parts.append(terminal_reason)
        errors = response.get("errors")
        if isinstance(errors, list):
            parts.extend(str(item) for item in errors[:1] if item)
        elif not parts:
            result = response.get("result")
            if isinstance(result, str) and result:
                parts.append(result)
        if not parts:
            return None
        return ": ".join(parts).replace("\n", " ")[:300]
