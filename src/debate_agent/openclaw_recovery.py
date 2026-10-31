"""OpenClaw 模糊传输失败的只读 Session 判定。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class OpenClawSessionOutcome(str, Enum):
    """表示 Session 查询能证明的传输结果。"""

    COMPLETED = "completed"
    RUNNING = "running"
    NOT_RECEIVED = "not_received"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OpenClawSessionInspection:
    """保存不含原始轨迹的 Session 判定。"""

    outcome: OpenClawSessionOutcome
    content: str | None = field(default=None, repr=False)


def _last_assistant_text(session_file: Path) -> str | None:
    """读取轨迹最后一段助手正文。

    参数：session_file 为已验证位于 OpenClaw Session 目录的 JSONL。
    返回值：最后一条 assistant text，缺失时返回 None。
    异常：文件读取错误向上抛出。
    """
    last_text: str | None = None
    with session_file.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message") if isinstance(event, dict) else None
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            texts = [
                item["text"]
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
                and item["text"].strip()
            ]
            if texts:
                last_text = texts[-1]
    return last_text


def inspect_openclaw_session(
    stdout: str,
    *,
    expected_session_key: str,
    allowed_sessions_root: Path,
    read_terminal: Callable[[Path], str | None] = _last_assistant_text,
) -> OpenClawSessionInspection:
    """判定 OpenClaw Session 是否接收并完成了模糊调用。

    参数：stdout 为 ``sessions --json`` 输出，expected_session_key 为规范 Key，
    allowed_sessions_root 为允许读取轨迹的根目录，read_terminal 为可注入读取器。
    返回值：完成、运行中、未接收或未知四态判定。
    异常：不传播 JSON、路径或文件错误，无法证明时统一返回 UNKNOWN。
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return OpenClawSessionInspection(OpenClawSessionOutcome.UNKNOWN)
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        return OpenClawSessionInspection(OpenClawSessionOutcome.UNKNOWN)
    matched = [
        item
        for item in sessions
        if isinstance(item, dict) and item.get("key") == expected_session_key
    ]
    if not matched:
        return OpenClawSessionInspection(OpenClawSessionOutcome.NOT_RECEIVED)
    if len(matched) != 1:
        return OpenClawSessionInspection(OpenClawSessionOutcome.UNKNOWN)
    session = matched[0]
    status = session.get("status")
    if status in {"running", "active", "pending"}:
        return OpenClawSessionInspection(OpenClawSessionOutcome.RUNNING)
    if status != "done":
        return OpenClawSessionInspection(OpenClawSessionOutcome.UNKNOWN)
    session_file = session.get("sessionFile")
    if not isinstance(session_file, str) or not session_file:
        return OpenClawSessionInspection(OpenClawSessionOutcome.UNKNOWN)
    try:
        root = allowed_sessions_root.expanduser().resolve()
        path = Path(session_file).expanduser().resolve()
        path.relative_to(root)
        if not path.is_file():
            return OpenClawSessionInspection(OpenClawSessionOutcome.UNKNOWN)
        content = read_terminal(path)
    except (OSError, RuntimeError, ValueError):
        return OpenClawSessionInspection(OpenClawSessionOutcome.UNKNOWN)
    if not content:
        return OpenClawSessionInspection(OpenClawSessionOutcome.UNKNOWN)
    return OpenClawSessionInspection(
        OpenClawSessionOutcome.COMPLETED,
        content=content,
    )
