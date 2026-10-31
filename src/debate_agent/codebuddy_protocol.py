"""CodeBuddy 终止输出的确定性归一化。"""

from __future__ import annotations

import json
import re

_SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_STRUCTURED_OUTPUT_PATTERN = re.compile(
    r"\s*<StructuredOutput>\s*(?P<payload>\{.*\})\s*</StructuredOutput>\s*",
    re.DOTALL,
)
_STRUCTURED_TOOL_CALL_PATTERN = re.compile(
    r"\s*<tool_calls:[A-Za-z0-9_-]+>\s*"
    r"<tool_call:[A-Za-z0-9_-]+>StructuredOutput\s*"
    r"(?P<payload>\{.*\})\s*",
    re.DOTALL,
)


def decode_codebuddy_output(content: str) -> object | None:
    """解码 CodeBuddy 单 JSON 或 JSONL 输出。

    参数：content 为 CLI stdout 全文。
    返回值：单个 JSON 值，或按顺序组成的 JSONL 事件列表；内容不完整时返回 None。
    异常：不传播 JSON 解码异常。
    """
    try:
        decoded: object = json.loads(content)
        return decoded
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    values: list[object] = []
    cursor = 0
    while cursor < len(content):
        while cursor < len(content) and content[cursor].isspace():
            cursor += 1
        if cursor >= len(content):
            break
        try:
            value, cursor = decoder.raw_decode(content, cursor)
        except json.JSONDecodeError:
            return None
        values.append(value)
    return values or None


def parse_codebuddy_terminal(payload: object) -> tuple[str, object] | None:
    """解析 CodeBuddy 终止输出。

    参数：payload 为 JSON 解码后的单对象或完整事件数组。
    返回值：合法 Session ID 与结构化内容；协议不完整时返回 None。
    异常：不抛出 JSON 或类型异常，非法厂商输出统一返回 None。
    """
    terminal: object = payload
    if isinstance(payload, list):
        terminal = next(
            (
                item
                for item in reversed(payload)
                if isinstance(item, dict) and item.get("type") == "result"
            ),
            None,
        )
    if not isinstance(terminal, dict):
        return None
    session_id = terminal.get("session_id")
    if (
        not isinstance(session_id, str)
        or _SESSION_PATTERN.fullmatch(session_id) is None
    ):
        return None
    if "structured_output" in terminal:
        return session_id, terminal["structured_output"]
    result = terminal.get("result")
    if not isinstance(result, str):
        return None
    match = _STRUCTURED_OUTPUT_PATTERN.fullmatch(result)
    if match is None:
        match = _STRUCTURED_TOOL_CALL_PATTERN.fullmatch(result)
    if match is None:
        return session_id, result
    try:
        structured_output = json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return None
    return session_id, structured_output
