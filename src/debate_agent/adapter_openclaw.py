"""OpenClaw CLI Adapter、输出清洗与实时流解析。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

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
    PromptTransport,
    TemporaryInput,
)


def sanitize_openclaw_stdout(stdout: str) -> str:
    """将 OpenClaw JSON 输出缩减为运行所需白名单字段。

    参数：stdout 为 CLI 完整标准输出。
    返回值：仅保留状态、最终正文、模型标识和规范 Session Key 的 JSON。
    异常：不传播解析错误；无效输出会转换成不含原文的协议错误标记。
    """
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return json.dumps(
            {"status": "invalid_json"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if not isinstance(payload, dict):
        return json.dumps(
            {"status": "invalid_payload"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    sanitized: dict[str, object] = {}
    status = payload.get("status")
    if isinstance(status, str):
        sanitized["status"] = status[:80]
    result = payload.get("result")
    if isinstance(result, dict):
        sanitized_result: dict[str, object] = {}
        raw_payloads = result.get("payloads")
        safe_payloads: list[dict[str, str]] = []
        if isinstance(raw_payloads, list):
            for item in raw_payloads:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    safe_payloads.append({"text": item["text"]})
        if safe_payloads:
            sanitized_result["payloads"] = safe_payloads
        meta = result.get("meta")
        if isinstance(meta, dict):
            safe_meta: dict[str, object] = {}
            agent_meta = meta.get("agentMeta")
            if isinstance(agent_meta, dict):
                safe_agent_meta = {
                    key: agent_meta[key]
                    for key in ("provider", "model")
                    if isinstance(agent_meta.get(key), str)
                }
                if safe_agent_meta:
                    safe_meta["agentMeta"] = safe_agent_meta
            prompt_report = meta.get("systemPromptReport")
            if isinstance(prompt_report, dict) and isinstance(
                prompt_report.get("sessionKey"), str
            ):
                safe_meta["systemPromptReport"] = {
                    "sessionKey": prompt_report["sessionKey"]
                }
            if safe_meta:
                sanitized_result["meta"] = safe_meta
        if sanitized_result:
            sanitized["result"] = sanitized_result
    return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))


class OpenClawStreamParser(_BaseStreamParser):
    """将 OpenClaw 的有限状态映射为安全实时事件。"""

    def __init__(self, *, workspace: Path | None = None) -> None:
        """创建解析器；参数为可选工作区，返回实例，不读取外部状态。"""
        super().__init__(workspace=workspace)
        self._started = False

    def feed(self, output: ProcessOutput) -> tuple[AdapterStreamEvent, ...]:
        """消费 OpenClaw 输出。

        参数：output 为一段 stdout/stderr。
        返回值：只包含有直接证据的启动或固定诊断事件。
        异常：不抛出解析异常，避免回显厂商任意文本。
        """
        if output.stream == "stderr":
            return (
                self._error_event("OpenClaw 写入了诊断输出")
                if output.text.strip()
                else ()
            )
        events: list[AdapterStreamEvent] = []
        if not self._started and output.text:
            self._started = True
            events.append(
                self._event(
                    InvocationEventType.STARTED,
                    "OpenClaw 已返回运行输出",
                    useful=False,
                )
            )
        try:
            payload = json.loads(output.text)
        except json.JSONDecodeError:
            return tuple(events)
        if isinstance(payload, dict) and payload.get("status") == "ok":
            events.append(
                self._event(
                    InvocationEventType.TERMINATED,
                    "OpenClaw 回合已完成",
                    useful=True,
                    terminal=True,
                )
            )
        return tuple(events)


class OpenClawAdapter(_BaseAdapter):
    """构建 OpenClaw agent 调用并解析脱敏后的 JSON 终止协议。"""

    agent = AgentName.OPENCLAW
    _PROMPT_MARKER = "__DEBATE_OPENCLAW_PROMPT_FILE__"

    def __init__(self, profile: AgentProfile, *, schema_path: Path) -> None:
        """创建 OpenClaw Adapter。

        参数：profile 为冻结配置，schema_path 为终局协议 Schema。
        返回值：实例。
        异常：Schema 不存在时抛 ValueError。
        """
        super().__init__(profile, schema_path=schema_path)
        self.expected_session_id: str = ""

    def create_stream_parser(
        self, *, workspace: Path | None = None
    ) -> OpenClawStreamParser:
        """创建安全流解析器；参数为可选工作区，返回独立解析器。"""
        return OpenClawStreamParser(workspace=workspace)

    def _session_boundary(self, session_id: str | None) -> tuple[str, str]:
        """计算 CLI Session Key 与规范标识。

        参数：session_id 为可选历史规范标识。
        返回值：传给 CLI 的相对 Key 与带 Agent 前缀的规范 Key。
        异常：历史 Session 不属于当前原生 Profile 时抛 SessionBoundaryError。
        """
        native_profile = (
            self.profile.native_profile.strip()
            if self.profile.native_profile
            else "main"
        )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", native_profile) is None:
            raise ValueError("OpenClaw Agent/Profile 格式无效")
        prefix = f"agent:{native_profile}:"
        if session_id is None:
            relative = f"debate:{uuid4().hex}"
            return relative, f"{prefix}{relative}"
        if not session_id.startswith(prefix):
            raise SessionBoundaryError("OpenClaw Session 不属于当前原生 Agent")
        relative = session_id.removeprefix(prefix)
        if not relative:
            raise SessionBoundaryError("OpenClaw Session Key 为空")
        return relative, session_id

    def build_invocation(
        self, *, worktree: Path, prompt: str, session_id: str | None
    ) -> Invocation:
        """构建 OpenClaw 安全调用。

        参数：worktree 为目标目录，prompt 为完整协议指令，session_id 为历史规范标识。
        返回值：使用 0600 临时 Prompt 文件的 Invocation。
        异常：Session 边界错误、Schema 读取错误向上抛出。
        """
        cwd = worktree.expanduser().resolve()
        relative_session, canonical_session = self._session_boundary(session_id)
        if self.profile.variant and self.profile.variant.strip() not in {
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "adaptive",
            "max",
        }:
            raise ValueError("OpenClaw Thinking 配置无效")
        argv = [self.profile.command, "agent"]
        if self.profile.native_profile:
            argv.extend(["--agent", self.profile.native_profile.strip()])
        argv.extend(["--session-key", relative_session])
        argv.extend(["--message-file", self._PROMPT_MARKER])
        if self.profile.model:
            argv.extend(["--model", self.profile.model.strip()])
        if self.profile.variant:
            argv.extend(["--thinking", self.profile.variant.strip()])
        argv.extend(
            [
                "--timeout",
                str(self.profile.effective_hard_timeout_seconds),
                "--json",
            ]
        )
        schema = self.schema_path.read_text(encoding="utf-8")
        protocol_prompt = (
            f"{prompt}\n\n目标工作空间的绝对路径是：{cwd}\n"
            "所有读取和写入必须限定在该目录。\n\n"
            "最终响应只能是满足以下 JSON Schema 的 JSON 对象，"
            f"不要使用 Markdown 代码块：\n{schema}"
        )
        self.expected_session_id = canonical_session
        return Invocation(
            argv=tuple(argv),
            cwd=cwd,
            stdin=None,
            timeout_seconds=self.profile.effective_hard_timeout_seconds,
            startup_timeout_seconds=self.profile.startup_timeout_seconds,
            idle_timeout_seconds=self.profile.idle_timeout_seconds,
            hard_timeout_seconds=self.profile.effective_hard_timeout_seconds,
            heartbeat_interval_seconds=self.profile.heartbeat_interval_seconds,
            session_id=canonical_session,
            prompt_transport=PromptTransport.TEMP_FILE,
            temporary_inputs=(
                TemporaryInput(
                    marker=self._PROMPT_MARKER,
                    content=protocol_prompt,
                    suffix=".md",
                ),
            ),
            stdout_persist_transform=sanitize_openclaw_stdout,
        )

    def _decode(self, stdout: str) -> tuple[str | None, object]:
        """解码脱敏 OpenClaw JSON。

        参数：stdout 为白名单化终止 JSON。
        返回值：规范 Session Key 和最后一段非空正文。
        异常：协议字段缺失时抛 AdapterOutputError，Session 不一致时抛边界错误。
        """
        payload = json.loads(stdout)
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise AdapterOutputError("OpenClaw 返回状态无效")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise AdapterOutputError("OpenClaw 响应缺少 result")
        meta = result.get("meta")
        report = meta.get("systemPromptReport") if isinstance(meta, dict) else None
        actual_session = report.get("sessionKey") if isinstance(report, dict) else None
        if not isinstance(actual_session, str) or not actual_session:
            raise AdapterOutputError("OpenClaw 响应缺少规范 Session Key")
        if actual_session != self.expected_session_id:
            raise SessionBoundaryError("OpenClaw 返回不同 Session Key")
        payloads = result.get("payloads")
        if not isinstance(payloads, list):
            raise AdapterOutputError("OpenClaw 响应缺少 payloads")
        texts = [
            item["text"]
            for item in payloads
            if isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and item["text"].strip()
        ]
        if not texts:
            raise AdapterOutputError("OpenClaw 响应缺少最终正文")
        return actual_session, texts[-1]

    def failure_detail(self, stdout: str) -> str | None:
        """返回固定失败摘要；参数为 stdout，返回不含厂商原文的安全文本。"""
        return "OpenClaw CLI 失败"
