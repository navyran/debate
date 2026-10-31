"""跨 Agent 的结构化摘要、增量更新和 Ack 游标。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from debate_agent.issue_ledger import IssueStatus, LedgerIssue
from debate_agent.models import (
    AgentName,
    ChangeAck,
    ChangeSummary,
    PeerMemoryUpdate,
    TurnResult,
)


class MemoryProtocolError(RuntimeError):
    """表示 PeerMemoryUpdate、ChangeAck 或同步游标不一致。"""


class MemoryStore:
    """持久化有界语义摘要，不保存隐藏推理和原始终端输出。"""

    def __init__(
        self,
        *,
        state_root: Path,
        run_id: str,
        participants: tuple[AgentName, ...] = (
            AgentName.CODEX,
            AgentName.CLAUDE,
        ),
    ) -> None:
        """创建记忆存储；参数为状态根、run ID 和参与者，返回实例，目录错误时抛错。"""
        self.participants = participants
        self.run_dir = state_root.expanduser().resolve() / "runs" / run_id
        self.updates_dir = self.run_dir / "updates"
        self.acks_dir = self.run_dir / "acks"
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        self.acks_dir.mkdir(parents=True, exist_ok=True)
        self.cursor_path = self.run_dir / "memory-cursors.json"
        if not self.cursor_path.exists():
            self._write_json(
                self.cursor_path,
                {
                    "pending": {agent.value: [] for agent in participants},
                    "last_ack_commit": {agent.value: None for agent in participants},
                    "last_update_id": {agent.value: None for agent in participants},
                },
            )
        else:
            cursors = self._read_cursors()
            changed = False
            for field, default in (
                ("pending", []),
                ("last_ack_commit", None),
                ("last_update_id", None),
            ):
                values = cursors.setdefault(field, {})
                for agent in participants:
                    if agent.value not in values:
                        values[agent.value] = (
                            list(default) if isinstance(default, list) else default
                        )
                        changed = True
            if changed:
                self._write_json(self.cursor_path, cursors)

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        """原子写 JSON；参数为路径和载荷，无返回值，序列化或文件错误时抛异常。"""
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _read_cursors(self) -> dict[str, Any]:
        """读取游标；无参数，返回可变 JSON 对象，文件损坏时传播异常。"""
        payload: dict[str, Any] = json.loads(
            self.cursor_path.read_text(encoding="utf-8")
        )
        return payload

    def build_change_summary(
        self,
        *,
        result: TurnResult,
        parent_commit: str,
        commit_sha: str,
        changed_paths: list[str],
        resolved_issues: list[str],
        validation_status: str,
        validation_report_ref: str,
        requested_attention: list[str],
        opened_issues: list[str] | None = None,
    ) -> ChangeSummary:
        """合成机器绑定摘要。

        参数为结构化回合、Git/验证机器事实、可选的权威账本未决项和关注点；返回
        摘要；模型不合法时抛错。未提供账本投影时沿用 legacy Writer 协议中的 Issue。
        """
        return ChangeSummary(
            update_id=f"UPD-{result.turn_id}-{result.agent.value}",
            source_agent=result.agent,
            turn_id=result.turn_id,
            parent_commit=parent_commit,
            commit_sha=commit_sha,
            goal_contract_version=result.goal_contract_version,
            changed_paths=sorted(set(changed_paths)),
            behavior_summary=[result.summary],
            opened_issues=(
                sorted(set(opened_issues))
                if opened_issues is not None
                else [issue.id for issue in result.issues]
            ),
            resolved_issues=sorted(set(resolved_issues)),
            validation_status=validation_status,
            validation_report_ref=validation_report_ref,
            requested_attention=requested_attention,
        )

    @staticmethod
    def project_ledger_for_peer_memory(
        issues: tuple[LedgerIssue, ...],
    ) -> tuple[list[str], list[str]]:
        """从唯一 Ledger 派生交接中的已关闭与未决问题。

        参数为 Ledger Issue 元组；返回 ``(resolved_issue_ids, attention)``；不写入
        独立状态，不判断业务 Claim，输入模型非法时由调用方在上游拦截。
        """
        resolved = [
            issue.issue_id
            for issue in issues
            if issue.status in {IssueStatus.RESOLVED, IssueStatus.DISMISSED}
        ]
        attention = [
            f"{issue.issue_id}: {issue.claim}"[:1000]
            for issue in issues
            if issue.status is IssueStatus.OPEN and issue.blocking
        ]
        return resolved, attention

    def publish(
        self, summary: ChangeSummary, *, target_agent: AgentName
    ) -> PeerMemoryUpdate:
        """发布同伴增量。

        参数为摘要与目标 Agent；返回更新；向来源自身发布或同 ID 内容冲突时抛错。
        """
        if target_agent is summary.source_agent:
            raise MemoryProtocolError("PeerMemoryUpdate 目标不能是来源 Agent")
        update = PeerMemoryUpdate.from_change_summary(
            summary, target_agent=target_agent
        )
        path = self.updates_dir / f"{update.update_id}.json"
        serialized = update.model_dump(mode="json")
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != serialized:
                raise MemoryProtocolError("相同 update ID 的内容发生冲突")
        else:
            self._write_json(path, serialized)
        cursors = self._read_cursors()
        pending: list[str] = cursors["pending"][target_agent.value]
        if update.update_id not in pending:
            pending.append(update.update_id)
            self._write_json(self.cursor_path, cursors)
        return update

    def save_change_summary(self, summary: ChangeSummary) -> str:
        """保存 ChangeSummary；参数为摘要，返回运行目录相对引用，文件失败时抛错。"""
        path = self.updates_dir / f"{summary.update_id}.summary.json"
        self._write_json(path, summary.model_dump(mode="json"))
        return path.relative_to(self.run_dir).as_posix()

    def load_change_summaries(self) -> list[ChangeSummary]:
        """读取全部交接摘要；无参数，返回按文件名排序列表，模型错误时传播异常。"""
        return [
            ChangeSummary.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.updates_dir.glob("*.summary.json"))
        ]

    def get_update(self, update_id: str) -> PeerMemoryUpdate:
        """读取 PeerMemoryUpdate；参数为 ID，返回模型，不存在或损坏时传播异常。"""
        path = self.updates_dir / f"{update_id}.json"
        return PeerMemoryUpdate.model_validate_json(path.read_text(encoding="utf-8"))

    def pending(self, agent: AgentName) -> list[str]:
        """读取待同步 ID；参数为 Agent，返回副本，文件损坏时传播异常。"""
        cursors = self._read_cursors()
        return list(cursors["pending"][agent.value])

    def acknowledge(self, *, update_id: str, ack: ChangeAck, current_head: str) -> Path:
        """确认一个增量。

        参数为 update、Ack 和当前 HEAD；返回 Ack 路径；身份、HEAD 或理解缺失时抛错。
        """
        update_path = self.updates_dir / f"{update_id}.json"
        if not update_path.exists():
            raise MemoryProtocolError(f"更新不存在: {update_id}")
        update = PeerMemoryUpdate.model_validate_json(
            update_path.read_text(encoding="utf-8")
        )
        if ack.agent is not update.target_agent:
            raise MemoryProtocolError("Ack Agent 与更新目标不一致")
        if ack.commit_sha != current_head:
            raise MemoryProtocolError("Ack 必须绑定当前 HEAD")
        if not ack.understood_changes or not any(
            item.strip() for item in ack.understood_changes
        ):
            raise MemoryProtocolError("Ack 必须包含实际理解")
        cursors = self._read_cursors()
        pending: list[str] = cursors["pending"][ack.agent.value]
        if update_id not in pending:
            raise MemoryProtocolError("更新不在 Agent 的待同步游标中")
        ack_path = self.acks_dir / f"{update_id}-{ack.agent.value}.json"
        self._write_json(ack_path, ack.model_dump(mode="json"))
        pending.remove(update_id)
        cursors["last_ack_commit"][ack.agent.value] = ack.commit_sha
        cursors["last_update_id"][ack.agent.value] = update_id
        self._write_json(self.cursor_path, cursors)
        return ack_path

    def assert_synchronized(self, agent: AgentName, *, current_head: str) -> None:
        """检查正式回合资格。

        参数为 Agent 和当前 HEAD；无返回值；仍有更新或 Ack 不是当前 HEAD 时抛错。
        """
        cursors = self._read_cursors()
        if cursors["pending"][agent.value]:
            raise MemoryProtocolError("Agent 仍有未同步 PeerMemoryUpdate")
        if cursors["last_ack_commit"][agent.value] != current_head:
            raise MemoryProtocolError("Agent 尚未确认当前 HEAD")
