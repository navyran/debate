"""CLI 调用级实时事件、增量存储与统一脱敏。"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from debate_agent.models import AgentName


class InvocationEventType(str, Enum):
    """定义页面允许展示的调用事件类型。"""

    STARTED = "started"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    FILE_CHANGED = "file_changed"
    MESSAGE = "message"
    PROGRESS = "progress"
    HEARTBEAT = "heartbeat"
    WARNING = "warning"
    ERROR = "error"
    TERMINATED = "terminated"


class InvocationSeverity(str, Enum):
    """定义调用事件的展示严重度。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class InvocationEvent(BaseModel):
    """保存一条可公开给本地页面的受限 CLI 调用事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(ge=1)
    timestamp: datetime
    run_id: str
    invocation_id: str
    agent: AgentName
    turn_id: str
    event_type: InvocationEventType
    title: str
    summary: str = Field(default="", max_length=500)
    severity: InvocationSeverity = InvocationSeverity.INFO
    elapsed_seconds: float = Field(ge=0)
    idle_seconds: float = Field(ge=0)
    changed_paths: tuple[str, ...] = ()
    terminal: bool = False

    @field_validator("summary", mode="before")
    @classmethod
    def truncate_summary(cls, value: object) -> str:
        """裁剪事件摘要；参数为任意摘要值，返回最多 500 字符文本，不抛异常。"""
        return str(value or "")[:500]

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """校验变更路径。

        参数为路径元组；返回规范去重的工作区相对路径；绝对或越界路径抛 ValueError。
        """
        normalized: list[str] = []
        for item in value:
            path = PurePosixPath(item)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"changed_paths 必须是工作区相对路径: {item}")
            text = path.as_posix()
            if not text:
                raise ValueError("changed_paths 必须是工作区相对路径")
            if text not in normalized:
                normalized.append(text)
        return tuple(normalized)


class AgentInvocationProgress(BaseModel):
    """保存单个 Agent 最近一次 CLI 调用的页面快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str
    turn_id: str
    started_at: datetime
    updated_at: datetime
    last_useful_activity_at: datetime | None = None
    elapsed_seconds: float = Field(ge=0)
    idle_seconds: float = Field(ge=0)
    event_count: int = Field(ge=1)
    changed_paths: tuple[str, ...] = ()
    changed_file_count: int = Field(ge=0)
    last_event_type: InvocationEventType
    last_title: str
    terminal: bool = False
    termination_category: str | None = None


class InvocationProgress(BaseModel):
    """保存一个 Run 的调用级最新进度和递增游标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    updated_at: datetime
    last_event_seq: int = Field(ge=1)
    agents: dict[AgentName, AgentInvocationProgress]


class Redactor:
    """统一遮罩实时事件和本机诊断尾部中的敏感文本。"""

    _assignment_pattern = re.compile(
        r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|COOKIE|PROXY)|PROMPT)"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    )
    _authorization_pattern = re.compile(
        r"(?i)\b(Authorization)\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+"
    )
    _cookie_pattern = re.compile(r"(?i)\b(Cookie)\s*[:=]\s*[^\s,;]+")
    _session_pattern = re.compile(
        r"(?i)([\"']?(?:session|session_id|thread_id)[\"']?\s*[:=]\s*[\"']?)"
        r"[A-Za-z0-9._:-]{8,}([\"']?)"
    )
    _uuid_pattern = re.compile(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
    )
    _url_credentials_pattern = re.compile(
        r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"
    )
    _external_path_pattern = re.compile(
        r"(?<![:\w])/(?:Users|home|tmp|private|var|etc)/[^\s,;]+"
    )

    def __init__(
        self,
        *,
        workspace: Path | None,
        secret_values: tuple[str, ...] = (),
        session_ids: tuple[str, ...] = (),
    ) -> None:
        """创建脱敏器。

        参数为可选工作区、已知密钥值和 Session ID；返回实例；不读取环境变量。
        """
        self.workspace = workspace.expanduser().resolve() if workspace else None
        self.secret_values = tuple(value for value in secret_values if value)
        self.session_ids = tuple(value for value in session_ids if value)

    def redact(self, text: str, *, max_chars: int | None = None) -> str:
        """脱敏一段文本。

        参数为原文和可选字符上限；返回遮罩文本；上限非正数时抛 ValueError。
        """
        if max_chars is not None and max_chars <= 0:
            raise ValueError("max_chars 必须为正数")
        sanitized = str(text)
        for value in (*self.secret_values, *self.session_ids):
            sanitized = sanitized.replace(value, "<redacted>")
        sanitized = self._url_credentials_pattern.sub(r"\1<redacted>@", sanitized)
        sanitized = self._authorization_pattern.sub(r"\1: <redacted>", sanitized)
        sanitized = self._cookie_pattern.sub(r"\1: <redacted>", sanitized)
        sanitized = self._assignment_pattern.sub(r"\1=<redacted>", sanitized)
        sanitized = self._session_pattern.sub(r"\1<redacted>\2", sanitized)
        sanitized = self._uuid_pattern.sub("<redacted>", sanitized)
        if self.workspace is not None:
            workspace_text = str(self.workspace)
            sanitized = sanitized.replace(f"{workspace_text}/", "")
            sanitized = sanitized.replace(workspace_text, ".")
        sanitized = self._external_path_pattern.sub("<external-path>", sanitized)
        return sanitized[:max_chars] if max_chars is not None else sanitized


class InvocationEventStore:
    """持久化单个 Run 的调用级 JSONL 事件和原子进度快照。"""

    def __init__(
        self,
        *,
        state_root: Path,
        run_id: str,
        redactor: Redactor | None = None,
    ) -> None:
        """创建事件存储。

        参数为状态根、Run ID 和可选脱敏器；返回实例；Run ID 非法时抛 ValueError。
        """
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None:
            raise ValueError("run_id 必须是安全路径标识")
        self.run_id = run_id
        self.run_dir = state_root.expanduser().resolve() / "runs" / run_id
        self.events_path = self.run_dir / "invocation-events.jsonl"
        self.progress_path = self.run_dir / "invocation-progress.json"
        self.redactor = redactor or Redactor(workspace=None)

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        """原子写 JSON；参数为目标路径和载荷，无返回值，I/O 错误时抛 OSError。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    def read_progress(self) -> InvocationProgress | None:
        """读取最新调用快照；无参数，返回快照或 None，内容损坏时抛错。"""
        if not self.progress_path.exists():
            return None
        return InvocationProgress.model_validate_json(
            self.progress_path.read_text(encoding="utf-8")
        )

    def read_events(
        self,
        *,
        after_seq: int = 0,
        limit: int | None = None,
        agent: AgentName | None = None,
    ) -> list[InvocationEvent]:
        """增量读取事件。

        参数为已消费序号、尾部上限和 Agent 过滤；返回有序事件；完整损坏行抛错，
        未完成尾行暂时忽略。
        """
        if after_seq < 0:
            raise ValueError("after_seq 不能为负数")
        if limit is not None and limit <= 0:
            raise ValueError("limit 必须为正数")
        if not self.events_path.exists():
            return []
        selected: list[InvocationEvent] | deque[InvocationEvent]
        selected = deque(maxlen=limit) if limit is not None else []
        with self.events_path.open("rb") as handle:
            for raw_line in handle:
                complete = raw_line.endswith(b"\n")
                try:
                    event = InvocationEvent.model_validate_json(raw_line)
                except (UnicodeDecodeError, ValueError):
                    if not complete:
                        break
                    raise
                if event.seq <= after_seq or (
                    agent is not None and event.agent != agent
                ):
                    continue
                selected.append(event)
        return list(selected)

    def _safe_changed_paths(
        self, changed_paths: tuple[str, ...]
    ) -> tuple[str, ...]:
        """过滤公开变更路径。

        参数为 Adapter 路径；返回不含调用级敏感值的路径；被脱敏器改变的路径整条丢弃。
        """
        return tuple(
            path
            for path in changed_paths
            if self.redactor.redact(path) == path
        )

    def publish(
        self,
        *,
        invocation_id: str,
        agent: AgentName,
        turn_id: str,
        event_type: InvocationEventType,
        title: str,
        elapsed_seconds: float,
        idle_seconds: float,
        summary: str = "",
        severity: InvocationSeverity = InvocationSeverity.INFO,
        changed_paths: tuple[str, ...] = (),
        useful: bool,
        terminal: bool = False,
        termination_category: str | None = None,
    ) -> InvocationEvent:
        """追加脱敏事件并刷新快照。

        参数为调用身份、展示字段、活动/终止状态；返回新事件；模型或 I/O 错误时抛错。
        """
        previous = self.read_progress()
        sequence = previous.last_event_seq + 1 if previous is not None else 1
        timestamp = datetime.now(timezone.utc)
        event = InvocationEvent(
            seq=sequence,
            timestamp=timestamp,
            run_id=self.run_id,
            invocation_id=invocation_id,
            agent=agent,
            turn_id=turn_id,
            event_type=event_type,
            title=self.redactor.redact(title, max_chars=160),
            summary=self.redactor.redact(summary, max_chars=500),
            severity=severity,
            elapsed_seconds=elapsed_seconds,
            idle_seconds=idle_seconds,
            changed_paths=self._safe_changed_paths(changed_paths),
            terminal=terminal,
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()

        agents = dict(previous.agents) if previous is not None else {}
        prior_agent = agents.get(agent)
        active_prior = prior_agent if (
            prior_agent is not None
            and prior_agent.invocation_id == invocation_id
        ) else None
        existing_paths = active_prior.changed_paths if active_prior is not None else ()
        merged_paths = tuple(dict.fromkeys((*existing_paths, *event.changed_paths)))
        started_at = (
            active_prior.started_at if active_prior is not None else event.timestamp
        )
        last_useful = (
            event.timestamp
            if useful
            else (
                active_prior.last_useful_activity_at
                if active_prior is not None
                else None
            )
        )
        agents[agent] = AgentInvocationProgress(
            invocation_id=invocation_id,
            turn_id=turn_id,
            started_at=started_at,
            updated_at=event.timestamp,
            last_useful_activity_at=last_useful,
            elapsed_seconds=elapsed_seconds,
            idle_seconds=idle_seconds,
            event_count=(
                active_prior.event_count + 1 if active_prior is not None else 1
            ),
            changed_paths=merged_paths,
            changed_file_count=len(merged_paths),
            last_event_type=event_type,
            last_title=event.title,
            terminal=terminal,
            termination_category=termination_category,
        )
        progress = InvocationProgress(
            run_id=self.run_id,
            updated_at=timestamp,
            last_event_seq=event.seq,
            agents=agents,
        )
        self._atomic_json(self.progress_path, progress.model_dump(mode="json"))
        return event


def read_redacted_tail(
    path: Path,
    *,
    redactor: Redactor,
    lines: int,
) -> str:
    """读取脱敏日志尾部。

    参数为文件、脱敏器和最大行数；返回尾部文本；行数非正或文件读取失败时抛错。
    """
    if lines <= 0:
        raise ValueError("lines 必须为正数")
    with path.open("rb") as handle:
        tail = deque(handle, maxlen=lines)
    text = b"".join(tail).decode("utf-8", errors="replace").rstrip("\r\n")
    return redactor.redact(text)
