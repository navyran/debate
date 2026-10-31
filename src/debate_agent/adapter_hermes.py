"""Hermes CLI Adapter 与实时流解析。"""

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
from debate_agent.config import AgentProfile
from debate_agent.invocation_events import InvocationEventType
from debate_agent.models import AgentName
from debate_agent.process import (
    ProcessOutput,
    ProcessResult,
    PromptTransport,
    validate_argv_prompt,
)

HERMES_ARGV_PROMPT_MAX_BYTES = 65_536
_HERMES_SESSION_LINE = re.compile(
    r"^\s*session_id:\s*(?P<session>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\s*$"
)


class HermesStreamParser(_BaseStreamParser):
    """将 Hermes Quiet 输出映射为安全生命周期事件。"""

    def feed(self, output: ProcessOutput) -> tuple[AdapterStreamEvent, ...]:
        """消费 Hermes 输出。

        参数：output 为增量 stdout/stderr。
        返回值：只含 Session 建立或固定诊断事件。
        异常：不抛出异常，不回显任意原始正文。
        """
        if output.stream == "stderr":
            return (
                self._error_event("Hermes 写入了诊断输出")
                if output.text.strip()
                else ()
            )
        if _HERMES_SESSION_LINE.fullmatch(output.text.strip()):
            return (
                self._event(
                    InvocationEventType.STARTED,
                    "Hermes Session 已确认",
                    useful=False,
                ),
            )
        try:
            payload = json.loads(output.text)
        except json.JSONDecodeError:
            return ()
        if isinstance(payload, dict):
            return (
                self._event(
                    InvocationEventType.TERMINATED,
                    "Hermes 回合已完成",
                    useful=True,
                    terminal=True,
                ),
            )
        return ()


class HermesAdapter(_BaseAdapter):
    """构建 Hermes 实验性调用并分离 Quiet 正文与 Session。"""

    agent = AgentName.HERMES

    def __init__(self, profile: AgentProfile, *, schema_path: Path) -> None:
        """创建 Hermes Adapter；参数为冻结 Profile/Schema，返回实例。"""
        super().__init__(profile, schema_path=schema_path)
        self._expected_session_id: str | None = None

    def create_stream_parser(
        self, *, workspace: Path | None = None
    ) -> HermesStreamParser:
        """创建安全流解析器；参数为可选工作区，返回独立解析器。"""
        return HermesStreamParser(workspace=workspace)

    def build_invocation(
        self, *, worktree: Path, prompt: str, session_id: str | None
    ) -> Invocation:
        """构建 Hermes argv Prompt 调用。

        参数：worktree 为目标目录，prompt 为完整协议，session_id 为可选历史 Session。
        返回值：Prompt 仅存在真实 argv、审计 argv 使用占位符的 Invocation。
        异常：Provider/Model 缺失或 Prompt 超限时在启动前抛 ValueError。
        """
        provider = self.profile.provider.strip() if self.profile.provider else ""
        model = self.profile.model.strip() if self.profile.model else ""
        if not provider:
            raise ValueError("Hermes 启动前必须配置显式 provider")
        if not model:
            raise ValueError("Hermes 启动前必须配置显式 model")
        cwd = worktree.expanduser().resolve()
        schema = self.schema_path.read_text(encoding="utf-8")
        protocol_prompt = (
            f"{prompt}\n\n目标工作空间的绝对路径是：{cwd}\n"
            "所有读取和写入必须限定在该目录。\n\n"
            "最终响应只能是满足以下 JSON Schema 的 JSON 对象，"
            f"不要使用 Markdown 代码块：\n{schema}"
        )
        validate_argv_prompt(
            protocol_prompt,
            max_bytes=HERMES_ARGV_PROMPT_MAX_BYTES,
        )
        argv = [
            self.profile.command,
            "chat",
            "--quiet",
            "--yolo",
            "--no-restore-cwd",
            "--provider",
            provider,
            "--model",
            model,
        ]
        if session_id is not None:
            argv.extend(["--resume", session_id])
        argv.extend(["--query", protocol_prompt])
        audit_argv = tuple(
            "<redacted>" if index == len(argv) - 1 else value
            for index, value in enumerate(argv)
        )
        self._expected_session_id = session_id
        return Invocation(
            argv=tuple(argv),
            audit_argv=audit_argv,
            cwd=cwd,
            stdin=None,
            timeout_seconds=self.profile.effective_hard_timeout_seconds,
            startup_timeout_seconds=self.profile.startup_timeout_seconds,
            idle_timeout_seconds=self.profile.idle_timeout_seconds,
            hard_timeout_seconds=self.profile.effective_hard_timeout_seconds,
            heartbeat_interval_seconds=self.profile.heartbeat_interval_seconds,
            session_id=session_id,
            prompt_transport=PromptTransport.ARGV,
        )

    def _decode(self, stdout: str, stderr: str = "") -> tuple[str | None, object]:
        """解码 Hermes Quiet 输出。

        参数：stdout 为业务正文，stderr 为 Hermes v0.19 Session 状态。
        返回值：唯一 Session ID 和去除状态行后的正文。
        异常：Session 缺失/冲突、续接不一致或正文缺失时抛明确错误。
        """
        session_ids: list[str] = []
        content_lines: list[str] = []
        for output in (stdout, stderr):
            for line in output.splitlines():
                session_match = _HERMES_SESSION_LINE.fullmatch(line)
                if session_match:
                    value = session_match.group("session")
                    if value not in session_ids:
                        session_ids.append(value)
        for line in stdout.splitlines():
            if _HERMES_SESSION_LINE.fullmatch(line):
                continue
            if line.lstrip().startswith("↻ Resumed session "):
                continue
            if line.strip():
                content_lines.append(line)
        if len(session_ids) != 1:
            raise AdapterOutputError("Hermes 响应缺少唯一 Session ID")
        actual_session = session_ids[0]
        if (
            self._expected_session_id is not None
            and actual_session != self._expected_session_id
        ):
            raise SessionBoundaryError("Hermes resume 返回不同 Session ID")
        content = "\n".join(content_lines).strip()
        if not content or re.match(r"^HTTP\s+[45]\d\d:", content):
            raise AdapterOutputError("Hermes 响应缺少有效最终正文")
        return actual_session, content

    def _decode_result(self, result: ProcessResult) -> tuple[str | None, object]:
        """解码 Hermes 双流结果。

        参数：result 为受控进程结果。
        返回值：从 stdout 正文和 stderr Session 状态提取的边界。
        异常：任一所需流被截断且缺少完整 spool 时抛协议错误。
        """
        stdout = result.stdout
        stderr = result.stderr
        if result.stdout_truncated:
            if result.stdout_path is None or not result.stdout_path.is_file():
                raise AdapterOutputError("CLI 结构化输出被截断")
            stdout = result.stdout_path.read_text(encoding="utf-8", errors="replace")
        if result.stderr_truncated:
            if result.stderr_path is None or not result.stderr_path.is_file():
                raise AdapterOutputError("CLI Session 输出被截断")
            stderr = result.stderr_path.read_text(encoding="utf-8", errors="replace")
        return self._decode(stdout, stderr)

    def failure_detail(self, stdout: str) -> str | None:
        """返回固定失败摘要；参数为 stdout，返回不含任意厂商原文的文本。"""
        return "Hermes CLI 失败"
