"""Qoder CLI Adapter 与实时流解析。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from debate_agent.adapter_claude import ClaudeAdapter, ClaudeStreamParser
from debate_agent.adapter_core import (
    AdapterStreamEvent,
    Invocation,
    _BaseAdapter,
)
from debate_agent.config import AgentProfile
from debate_agent.models import AgentName
from debate_agent.process import (
    ProcessOutput,
)


class QoderStreamParser(ClaudeStreamParser):
    """把 Qoder stream-json 映射为统一实时事件。"""

    def feed(self, output: ProcessOutput) -> tuple[AdapterStreamEvent, ...]:
        """消费一条 Qoder 输出。

        参数为增量输出；返回使用 Qoder 展示名的安全事件；未知事件由父解析器忽略。
        """
        events = super().feed(output)
        return tuple(
            AdapterStreamEvent(
                event_type=event.event_type,
                title=event.title.replace("Claude", "Qoder"),
                summary=event.summary.replace("Claude", "Qoder"),
                severity=event.severity,
                changed_paths=event.changed_paths,
                useful=event.useful,
                terminal=event.terminal,
            )
            for event in events
        )


class QoderAdapter(ClaudeAdapter):
    """构建 Qoder CLI 调用并解析其 stream-json 终止结果。"""

    agent = AgentName.QODER

    def __init__(
        self,
        profile: AgentProfile,
        *,
        schema_path: Path,
        minimal_mcp: bool = True,
        yolo: bool = True,
    ) -> None:
        """创建 Qoder Adapter。

        参数为 Profile、协议 Schema、MCP 模式和 yolo 开关；返回实例；Schema 缺失时抛错。
        """
        _BaseAdapter.__init__(self, profile, schema_path=schema_path)
        self.minimal_mcp = minimal_mcp
        self.yolo = yolo

    def create_stream_parser(
        self, *, workspace: Path | None = None
    ) -> QoderStreamParser:
        """创建 Qoder 增量解析器；参数为可选工作区，返回独立解析器，不抛异常。"""
        return QoderStreamParser(workspace=workspace)

    def build_invocation(
        self, *, worktree: Path, prompt: str, session_id: str | None
    ) -> Invocation:
        """构建 Qoder argv。

        参数为 worktree、Prompt 和可选 Session；返回默认 yolo 调用；
        Schema 读取失败时抛错。
        """
        cwd = worktree.expanduser().resolve()
        effective_session_id = session_id or str(uuid4())
        argv = [self.profile.command]
        if self.yolo:
            argv.append("--yolo")
        argv.extend(["-p", "--cwd", str(cwd)])
        if session_id is None:
            argv.extend(["--session-id", effective_session_id])
        else:
            argv.extend(["--resume", effective_session_id])
        argv.extend(["--output-format", "stream-json"])
        if self.minimal_mcp:
            argv.extend(["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])
        if self.profile.model and self.profile.model != "__cli_default__":
            argv.extend(["--model", self.profile.model])
        schema = self.schema_path.read_text(encoding="utf-8")
        protocol_prompt = (
            f"{prompt}\n\n最终响应只能是满足以下 JSON Schema 的 JSON 对象，"
            f"不要使用 Markdown 代码块：\n{schema}"
        )
        return Invocation(
            argv=tuple(argv),
            cwd=cwd,
            stdin=protocol_prompt,
            timeout_seconds=self.profile.effective_hard_timeout_seconds,
            startup_timeout_seconds=self.profile.startup_timeout_seconds,
            idle_timeout_seconds=self.profile.idle_timeout_seconds,
            hard_timeout_seconds=self.profile.effective_hard_timeout_seconds,
            heartbeat_interval_seconds=self.profile.heartbeat_interval_seconds,
            session_id=effective_session_id,
        )
