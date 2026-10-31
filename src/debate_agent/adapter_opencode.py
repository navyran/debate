"""OpenCode CLI Adapter、JSONL 扫描与实时流解析。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from debate_agent.adapter_core import (
    AdapterOutputError,
    AdapterStreamEvent,
    Invocation,
    SessionBoundaryError,
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


def _opencode_part(payload: dict[str, Any]) -> dict[str, Any]:
    """读取 OpenCode part；参数为事件对象；返回 part 对象或空字典；不抛异常。"""
    part = payload.get("part")
    return part if isinstance(part, dict) else {}


def _opencode_error_detail(payload: dict[str, Any]) -> str:
    """提取 OpenCode 安全错误字段。

    参数为事件对象；返回仅含 code/type/category 的有限摘要；任意原始消息均被忽略。
    """
    fields: list[str] = []

    def visit(value: object) -> None:
        """递归查找安全字段；参数为任意节点；无返回值；异常节点被忽略。"""
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in {"code", "type", "category"} and isinstance(
                    nested, (str, int)
                ):
                    text = str(nested).strip()
                    if re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", text):
                        item = f"{key}={text}"
                        if item not in fields:
                            fields.append(item)
                elif key in {"error", "part"}:
                    visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return ("; ".join(fields) or "OpenCode 报告错误")[:300]


@dataclass(frozen=True, slots=True)
class OpenCodeJsonlState:
    """保存一次 OpenCode JSONL 安全扫描的有限协议状态。"""

    session_ids: tuple[str, ...]
    last_text: str | None
    error_detail: str | None
    interaction_wait: bool
    invalid_json: bool


def scan_opencode_jsonl(lines: Iterable[str]) -> OpenCodeJsonlState:
    """扫描 OpenCode JSONL。

    参数为文本行；返回有限 Session/text/error/wait 状态；原始行和任意错误消息
    不进入返回值，输入迭代错误会传播。
    """
    session_ids: list[str] = []
    last_text: str | None = None
    error_detail: str | None = None
    interaction_wait = False
    invalid_json = False
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            invalid_json = True
            continue
        if not isinstance(payload, dict):
            continue
        part = _opencode_part(payload)
        for source in (payload, part):
            value = source.get("sessionID")
            if isinstance(value, str) and value.strip():
                normalized = value.strip()
                if normalized not in session_ids:
                    session_ids.append(normalized)
        event_type = str(payload.get("type", "")).casefold()
        part_type = str(part.get("type", "")).casefold()
        if event_type == "error" or part_type == "error":
            error_detail = _opencode_error_detail(payload)
        interaction_wait = interaction_wait or any(
            marker in event_type or marker in part_type
            for marker in ("permission", "question")
        )
        if event_type == "text" or part_type == "text":
            text = part.get("text") if "text" in part else payload.get("text")
            if isinstance(text, str) and text.strip():
                last_text = text
    return OpenCodeJsonlState(
        session_ids=tuple(session_ids),
        last_text=last_text,
        error_detail=error_detail,
        interaction_wait=interaction_wait,
        invalid_json=invalid_json,
    )


_OPENCODE_PATH_MAX_DEPTH = 12
_OPENCODE_PATH_SCAN_LIMIT = 256
_OPENCODE_PATH_CANDIDATE_LIMIT = 64


def _opencode_path_values(payload: dict[str, Any]) -> tuple[object, ...]:
    """读取 OpenCode 显式路径字段。

    参数为事件对象；返回 path/file_path/filePath 下的候选值；深度、扫描节点和
    候选数均受固定上限约束，不推断命令或文本内容。
    """
    candidates: list[object] = []
    pending: list[tuple[object, int]] = [(payload, 0)]
    scanned = 0
    path_keys = {"path", "file_path", "filePath"}
    while (
        pending
        and scanned < _OPENCODE_PATH_SCAN_LIMIT
        and len(candidates) < _OPENCODE_PATH_CANDIDATE_LIMIT
    ):
        value, depth = pending.pop()
        if depth > _OPENCODE_PATH_MAX_DEPTH:
            continue
        children: list[tuple[object, int]] = []
        if isinstance(value, dict):
            for key, nested in value.items():
                if scanned >= _OPENCODE_PATH_SCAN_LIMIT:
                    break
                scanned += 1
                if key in path_keys:
                    path_values = nested if isinstance(nested, list) else [nested]
                    for item in path_values:
                        if (
                            scanned >= _OPENCODE_PATH_SCAN_LIMIT
                            or len(candidates) >= _OPENCODE_PATH_CANDIDATE_LIMIT
                        ):
                            break
                        scanned += 1
                        candidates.append(item)
                elif depth < _OPENCODE_PATH_MAX_DEPTH and isinstance(
                    nested, (dict, list)
                ):
                    children.append((nested, depth + 1))
        elif isinstance(value, list) and depth < _OPENCODE_PATH_MAX_DEPTH:
            for nested in value:
                if scanned >= _OPENCODE_PATH_SCAN_LIMIT:
                    break
                scanned += 1
                if isinstance(nested, (dict, list)):
                    children.append((nested, depth + 1))
        pending.extend(reversed(children))
    return tuple(candidates)


class OpenCodeStreamParser(_BaseStreamParser):
    """把 OpenCode JSONL 和常见 part 载荷映射为受限实时事件。"""

    def _safe_error_event(
        self, payload: dict[str, Any]
    ) -> tuple[AdapterStreamEvent, ...]:
        """生成安全错误事件；参数为厂商事件；返回去重事件；不回显任意消息。"""
        return self._error_event(_opencode_error_detail(payload))

    def feed(self, output: ProcessOutput) -> tuple[AdapterStreamEvent, ...]:
        """消费一条 OpenCode 输出。

        参数为增量输出；返回零或多条安全事件；不完整 JSON 等待 raw 完整内容，
        stderr 与完整非法 JSON 只生成去重的统一错误。
        """
        if output.stream != "stdout":
            return (
                self._error_event("OpenCode stderr 输出") if output.text.strip() else ()
            )
        try:
            payload = json.loads(output.text)
        except json.JSONDecodeError:
            if not output.complete_line:
                return ()
            return self._error_event("OpenCode 输出了无效 JSONL")
        if not isinstance(payload, dict):
            return ()
        part = _opencode_part(payload)
        event_type = str(payload.get("type", ""))
        part_type = str(part.get("type", ""))
        if event_type == "step_start" or part_type in {"step_start", "step-start"}:
            return (self._event(InvocationEventType.STARTED, "OpenCode 回合已开始"),)
        if event_type == "error" or part_type == "error":
            return self._safe_error_event(payload)
        if event_type == "step_finish" or part_type in {
            "step_finish",
            "step-finish",
        }:
            return (
                self._event(
                    InvocationEventType.PROGRESS,
                    "OpenCode 步骤已完成",
                    useful=True,
                    terminal=False,
                ),
            )
        if event_type == "text" or part_type == "text":
            text = part.get("text") if "text" in part else payload.get("text", "")
            return (
                self._event(
                    InvocationEventType.MESSAGE,
                    "OpenCode 更新结论",
                    summary=text,
                    useful=True,
                ),
            )
        if event_type not in {"tool_use", "tool"} and part_type not in {
            "tool_use",
            "tool",
        }:
            return ()
        paths = _safe_changed_paths(
            _opencode_path_values(payload), workspace=self.workspace
        )[:20]
        tool_name = part.get("name") or part.get("tool") or payload.get("name")
        return (
            self._event(
                InvocationEventType.FILE_CHANGED
                if paths
                else InvocationEventType.TOOL_STARTED,
                f"OpenCode 调用 {str(tool_name or 'tool')[:80]}",
                changed_paths=paths,
                useful=True,
            ),
        )


class OpenCodeAdapter(_BaseAdapter):
    """构建 OpenCode run 调用并解析 JSONL 终止协议。"""

    agent = AgentName.OPENCODE

    def __init__(self, profile: AgentProfile, *, schema_path: Path) -> None:
        """创建 OpenCode Adapter。

        参数为 Profile 与 Schema；返回实例；Schema 缺失时抛错；Session 边界在每次
        build_invocation 时重新记录。
        """
        super().__init__(profile, schema_path=schema_path)
        self._expected_session_id: str | None = None

    def create_stream_parser(
        self, *, workspace: Path | None = None
    ) -> OpenCodeStreamParser:
        """创建 OpenCode 增量解析器；参数为可选工作区；返回独立解析器。"""
        return OpenCodeStreamParser(workspace=workspace)

    @staticmethod
    def _runtime_config_is_safe(
        content: str,
        *,
        mcp_mode: str,
        mcp_names: tuple[str, ...],
        mcp_configured_count: int,
        mcp_names_hash: str,
    ) -> bool:
        """校验冻结配置精确语义；参数为内容/MCP 元数据；返回布尔值。"""
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict):
            return False
        serialized_names = json.dumps(
            sorted(mcp_names), ensure_ascii=False, separators=(",", ":")
        )
        if (
            mcp_mode not in {"minimal", "full"}
            or mcp_configured_count != len(mcp_names)
            or hashlib.sha256(serialized_names.encode("utf-8")).hexdigest()
            != mcp_names_hash
            or payload.get("permission") != "allow"
            or payload.get("share") != "disabled"
            or payload.get("snapshot") is not False
            or payload.get("autoupdate") is not False
        ):
            return False
        expected_keys = {"permission", "share", "snapshot", "autoupdate"}
        if mcp_mode == "full":
            return set(payload) == expected_keys
        if set(payload) != expected_keys | {"mcp"}:
            return False
        mcp = payload["mcp"]
        return (
            isinstance(mcp, dict)
            and len(mcp) == mcp_configured_count
            and set(mcp) == set(mcp_names)
            and all(
                type(value) is dict and value == {"enabled": False}
                for value in mcp.values()
            )
        )

    def build_invocation(
        self, *, worktree: Path, prompt: str, session_id: str | None
    ) -> Invocation:
        """构建 OpenCode argv。

        参数为工作树、Prompt 和可选 Session；返回 stdin 协议调用；未配置显式 model
        时抛 ValueError，Schema 读取失败时传播异常。
        """
        model = self.profile.model.strip() if self.profile.model else ""
        if not model:
            raise ValueError("OpenCode 启动前必须配置显式 model")
        cwd = worktree.expanduser().resolve()
        argv = [
            self.profile.command,
            "run",
            "--format",
            "json",
            "--dir",
            str(cwd),
            "--model",
            model,
            "--agent",
            "build",
            "--auto",
            "--pure",
        ]
        if self.profile.variant:
            argv.extend(["--variant", self.profile.variant])
        if session_id is not None:
            argv.extend(["--session", session_id])
        schema = self.schema_path.read_text(encoding="utf-8")
        protocol_prompt = (
            f"{prompt}\n\n最终响应只能是满足以下 JSON Schema 的 JSON 对象，"
            f"不要使用 Markdown 代码块：\n{schema}"
        )
        capability = self.profile.internal_runtime_config_capability()
        if capability is None:
            if (
                self.profile.runtime_config_hash is not None
                or self.profile.runtime_config_content_hash is not None
                or self.profile.runtime_config_resolved_hash is not None
            ):
                raise ValueError("OpenCode 预检运行配置内容不可用")
            config_content = json.dumps(
                {
                    "permission": "allow",
                    "share": "disabled",
                    "snapshot": False,
                    "autoupdate": False,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            mcp_mode = "full"
            mcp_names: tuple[str, ...] = ()
            mcp_configured_count = 0
            mcp_names_hash = hashlib.sha256(b"[]").hexdigest()
        else:
            config_content = capability.content()
            content_hash = self.profile.runtime_config_content_hash
            if (
                self.profile.runtime_config_hash is None
                or content_hash is None
                or self.profile.runtime_config_resolved_hash is None
            ):
                raise ValueError("OpenCode 预检运行配置摘要不完整")
            actual_hash = hashlib.sha256(config_content.encode()).hexdigest()
            if actual_hash != content_hash:
                raise ValueError("OpenCode 预检运行配置完整性校验失败")
            (
                mcp_mode,
                mcp_names,
                mcp_configured_count,
                mcp_names_hash,
            ) = capability.metadata()
        if not self._runtime_config_is_safe(
            config_content,
            mcp_mode=mcp_mode,
            mcp_names=mcp_names,
            mcp_configured_count=mcp_configured_count,
            mcp_names_hash=mcp_names_hash,
        ):
            raise ValueError("OpenCode 预检运行配置未通过安全校验")
        self._expected_session_id = session_id
        return Invocation(
            argv=tuple(argv),
            cwd=cwd,
            stdin=protocol_prompt,
            timeout_seconds=self.profile.effective_hard_timeout_seconds,
            startup_timeout_seconds=self.profile.startup_timeout_seconds,
            idle_timeout_seconds=self.profile.idle_timeout_seconds,
            hard_timeout_seconds=self.profile.effective_hard_timeout_seconds,
            heartbeat_interval_seconds=self.profile.heartbeat_interval_seconds,
            session_id=session_id,
            environment_overrides=(
                ("OPENCODE_CONFIG_CONTENT", config_content),
                ("OPENCODE_DISABLE_AUTOUPDATE", "true"),
                ("OPENCODE_DISABLE_DEFAULT_PLUGINS", "true"),
            ),
        )

    def _decode_lines(self, lines: Iterable[str]) -> tuple[str | None, object]:
        """流式解码 OpenCode JSONL。

        参数为文本行；返回唯一 Session 与最后非空 text；非法 JSON、错误事件、
        Session 缺失/冲突/续接不一致或文本缺失时抛协议或 Session 边界错误。
        """
        state = scan_opencode_jsonl(lines)
        if state.invalid_json:
            raise AdapterOutputError("OpenCode 输出了无效 JSONL")
        if state.error_detail is not None:
            raise AdapterOutputError(f"OpenCode 报告错误: {state.error_detail}"[:300])
        if not state.session_ids:
            raise AdapterOutputError("OpenCode 响应缺少明确 sessionID")
        if len(state.session_ids) != 1:
            raise SessionBoundaryError("OpenCode 流中出现多个 Session ID")
        actual_session_id = state.session_ids[0]
        if (
            self._expected_session_id is not None
            and actual_session_id != self._expected_session_id
        ):
            raise SessionBoundaryError("OpenCode resume 返回不同 Session ID")
        if state.last_text is None:
            raise AdapterOutputError("OpenCode 响应缺少最终非空 text")
        return actual_session_id, state.last_text

    def _decode(self, stdout: str) -> tuple[str | None, object]:
        """解码内存 OpenCode JSONL；参数为 stdout；返回 Session/最终 text。"""
        return self._decode_lines(stdout.splitlines())

    def _decode_result(self, result: ProcessResult) -> tuple[str | None, object]:
        """解码 OpenCode 结果。

        参数为进程结果；返回 Session/最终 text；内存截断时流式读取 raw spool，
        raw 不存在时抛结构化输出截断错误。
        """
        if not result.stdout_truncated:
            return self._decode(result.stdout)
        if result.stdout_path is None or not result.stdout_path.is_file():
            raise AdapterOutputError("CLI 结构化输出被截断")
        with result.stdout_path.open(encoding="utf-8", errors="replace") as handle:
            return self._decode_lines(handle)

    def failure_detail(self, stdout: str) -> str | None:
        """提取 OpenCode 安全失败详情。

        参数为 stdout；返回仅含 code/type/category 的摘要或统一文本；无论输入内容
        如何都不回显任意原始消息、Prompt 或环境值。
        """
        for line in reversed(stdout.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            part = _opencode_part(payload)
            if payload.get("type") == "error" or part.get("type") == "error":
                return _opencode_error_detail(payload)
        return "OpenCode CLI 失败"
