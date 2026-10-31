"""Codex CLI Adapter 与实时流解析。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from debate_agent.adapter_core import (
    AdapterOutputError,
    AdapterStreamEvent,
    Invocation,
    SessionBoundaryError,
    _BaseAdapter,
    _BaseStreamParser,
    _codex_compatible_schema_path,
    _safe_changed_paths,
)
from debate_agent.config import AgentProfile
from debate_agent.invocation_events import InvocationEventType
from debate_agent.models import AgentName
from debate_agent.process import (
    ProcessOutput,
    ProcessResult,
)


class CodexStreamParser(_BaseStreamParser):
    """把 Codex JSONL 映射为受限实时事件。"""

    def feed(self, output: ProcessOutput) -> tuple[AdapterStreamEvent, ...]:
        """消费一条 Codex 输出。

        参数为增量输出；返回零或多条安全事件；不完整 JSON 暂忽略，未知字段丢弃。
        """
        if output.stream != "stdout":
            return self._error_event(output.text.strip()) if output.text.strip() else ()
        try:
            payload = json.loads(output.text)
        except json.JSONDecodeError:
            if not output.complete_line:
                return ()
            return self._error_event("Codex 输出了无效 JSONL")
        if not isinstance(payload, dict):
            return ()
        event_type = payload.get("type")
        if event_type == "thread.started":
            return (self._event(InvocationEventType.STARTED, "Codex Session 已建立"),)
        if event_type == "turn.started":
            return (self._event(InvocationEventType.STARTED, "Codex 回合已开始"),)
        if event_type == "turn.completed":
            return (
                self._event(
                    InvocationEventType.TERMINATED,
                    "Codex 回合已完成",
                    useful=True,
                    terminal=True,
                ),
            )
        if event_type == "error":
            return self._error_event(payload.get("message") or payload.get("error"))
        if event_type not in {"item.started", "item.completed"}:
            return ()
        item = payload.get("item")
        if not isinstance(item, dict):
            return ()
        item_type = str(item.get("type", ""))
        completed = event_type == "item.completed"
        if item_type in {"command_execution", "command", "tool_call"}:
            command = item.get("command") or item.get("cmd") or item_type
            return (
                self._event(
                    InvocationEventType.TOOL_COMPLETED
                    if completed
                    else InvocationEventType.TOOL_STARTED,
                    "Codex 命令完成" if completed else "Codex 执行命令",
                    summary=command,
                    useful=True,
                ),
            )
        if item_type in {"file_change", "file_changed", "file_edit"}:
            changes = item.get("changes", ())
            candidates: list[object] = []
            if isinstance(changes, list):
                for change in changes:
                    if isinstance(change, dict):
                        candidates.append(change.get("path"))
                    else:
                        candidates.append(change)
            candidates.extend((item.get("path"), item.get("file_path")))
            paths = _safe_changed_paths(candidates, workspace=self.workspace)
            return (
                self._event(
                    InvocationEventType.FILE_CHANGED,
                    "Codex 修改文件",
                    changed_paths=paths,
                    useful=True,
                ),
            )
        if item_type == "agent_message":
            return (
                self._event(
                    InvocationEventType.MESSAGE,
                    "Codex 更新结论",
                    summary=item.get("text", ""),
                    useful=True,
                ),
            )
        return ()


class CodexAdapter(_BaseAdapter):
    """构建 Codex exec 调用并解析 JSONL 事件。"""

    agent = AgentName.CODEX

    def __init__(
        self,
        profile: AgentProfile,
        *,
        schema_path: Path,
        resume_sandbox_verified: bool = False,
    ) -> None:
        """创建 Codex Adapter。

        参数为 Profile、Schema 和续接边界证明；返回实例；Schema 缺失时抛错。
        """
        super().__init__(profile, schema_path=schema_path)
        self.resume_sandbox_verified = resume_sandbox_verified

    def create_stream_parser(
        self, *, workspace: Path | None = None
    ) -> CodexStreamParser:
        """创建 Codex 增量解析器；参数为可选工作区，返回独立解析器，不抛异常。"""
        return CodexStreamParser(workspace=workspace)

    def build_invocation(
        self, *, worktree: Path, prompt: str, session_id: str | None
    ) -> Invocation:
        """构建 Codex argv。

        参数为 worktree、Prompt 和可选 Session；返回调用；不安全续接时抛边界错误。
        """
        cwd = worktree.expanduser().resolve()
        schema_path = _codex_compatible_schema_path(self.schema_path)
        if session_id is None:
            argv = [
                self.profile.command,
                "exec",
                "-C",
                str(cwd),
                "--sandbox",
                "workspace-write",
                "--ignore-user-config",
                "--json",
                "--output-schema",
                str(schema_path),
            ]
        else:
            if not self.resume_sandbox_verified:
                raise SessionBoundaryError("Codex resume 尚未证明 sandbox 写入边界")
            argv = [
                self.profile.command,
                "exec",
                "resume",
                session_id,
                "-c",
                'sandbox_mode="workspace-write"',
                "--ignore-user-config",
                "--json",
                "--output-schema",
                str(schema_path),
            ]
        if self.profile.model and self.profile.model != "__cli_default__":
            argv.extend(["--model", self.profile.model])
        argv.append("-")
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
        )

    @staticmethod
    def _decode_lines(lines: Iterable[str]) -> tuple[str | None, object]:
        """流式解码 Codex JSONL；参数为文本行，返回 Session/原文，缺事件时抛错。"""
        session_id: str | None = None
        content: object | None = None
        terminated = False
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "thread.started":
                session_id = event.get("thread_id")
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    content = item.get("text")
            if event.get("type") == "turn.completed":
                terminated = True
        if content is None or not terminated:
            raise KeyError("Codex 未返回 agent_message")
        return session_id, content

    def _decode(self, stdout: str) -> tuple[str | None, object]:
        """解码内存 Codex JSONL；参数为 stdout，返回 Session/原文，缺事件时抛错。"""
        return self._decode_lines(stdout.splitlines())

    def _decode_result(self, result: ProcessResult) -> tuple[str | None, object]:
        """解码 Codex 结果；参数为结果，超限时流式读取完整落盘 JSONL。"""
        if not result.stdout_truncated:
            return self._decode(result.stdout)
        if result.stdout_path is None or not result.stdout_path.is_file():
            raise AdapterOutputError("CLI 结构化输出被截断")
        with result.stdout_path.open(encoding="utf-8", errors="replace") as handle:
            return self._decode_lines(handle)
