"""为 Streamlit 和其他观察者提供脱敏运行事件与最新进度快照。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RunEvent(BaseModel):
    """描述一条可公开给本地控制台的脱敏运行事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(ge=1)
    timestamp: datetime
    run_id: str
    kind: str
    phase: str
    title: str
    summary: str = ""
    node: str | None = None
    actor: str | None = None
    current_turn: int | None = Field(default=None, ge=0)
    max_turns: int | None = Field(default=None, gt=0)
    head: str | None = None
    validation_status: str | None = None
    status: str = "running"


class RunProgress(BaseModel):
    """保存页面轮询所需的最新运行进度，不包含模型原始内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    phase: str
    title: str
    updated_at: datetime
    last_event_seq: int = Field(ge=1)
    status: str = "running"
    node: str | None = None
    actor: str | None = None
    current_turn: int | None = Field(default=None, ge=0)
    max_turns: int | None = Field(default=None, gt=0)
    head: str | None = None
    validation_status: str | None = None


class RunEventStore:
    """以 JSONL 和原子 JSON 快照持久化单个 run 的可观察进度。"""

    def __init__(self, *, state_root: Path, run_id: str) -> None:
        """创建事件存储；参数为状态根和 run ID，返回实例，ID 非法时抛错。"""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None:
            raise ValueError("run_id 必须是安全路径标识")
        self.run_id = run_id
        self.run_dir = state_root.expanduser().resolve() / "runs" / run_id
        self.events_path = self.run_dir / "events.jsonl"
        self.progress_path = self.run_dir / "progress.json"

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        """原子写入 JSON；参数为目标路径和载荷，无返回值，I/O 失败时抛错。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    def read_events(self, *, limit: int | None = None) -> list[RunEvent]:
        """读取按序事件；参数为可选尾部条数，返回事件列表，内容损坏时抛错。"""
        if limit is not None and limit <= 0:
            raise ValueError("limit 必须大于 0")
        if not self.events_path.exists():
            return []
        events = [
            RunEvent.model_validate_json(line)
            for line in self.events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return events[-limit:] if limit is not None else events

    def read_progress(self) -> RunProgress | None:
        """读取最新进度；无参数，返回快照或 None，内容损坏时抛错。"""
        if not self.progress_path.exists():
            return None
        return RunProgress.model_validate_json(
            self.progress_path.read_text(encoding="utf-8")
        )

    def publish(
        self,
        *,
        kind: str,
        phase: str,
        title: str,
        summary: str = "",
        node: str | None = None,
        actor: str | None = None,
        current_turn: int | None = None,
        max_turns: int | None = None,
        head: str | None = None,
        validation_status: str | None = None,
        status: str = "running",
    ) -> RunEvent:
        """追加事件并刷新快照；参数为脱敏进度字段，返回事件，I/O 失败时抛错。"""
        previous = self.read_progress()
        last_seq = previous.last_event_seq if previous is not None else 0
        timestamp = datetime.now(timezone.utc)
        event = RunEvent(
            seq=last_seq + 1,
            timestamp=timestamp,
            run_id=self.run_id,
            kind=kind,
            phase=phase,
            title=title,
            summary=summary[:500],
            node=node,
            actor=actor,
            current_turn=current_turn,
            max_turns=max_turns,
            head=head,
            validation_status=validation_status,
            status=status,
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        progress = RunProgress(
            run_id=self.run_id,
            phase=phase,
            title=title,
            updated_at=timestamp,
            last_event_seq=event.seq,
            status=status,
            node=node,
            actor=actor,
            current_turn=(
                current_turn
                if current_turn is not None
                else previous.current_turn if previous is not None else None
            ),
            max_turns=(
                max_turns
                if max_turns is not None
                else previous.max_turns if previous is not None else None
            ),
            head=head if head is not None else previous.head if previous else None,
            validation_status=(
                validation_status
                if validation_status is not None
                else previous.validation_status if previous is not None else None
            ),
        )
        self._atomic_json(self.progress_path, progress.model_dump(mode="json"))
        return event
