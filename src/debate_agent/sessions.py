"""原生 CLI Session 的身份绑定、持久化与重建。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from debate_agent.config import AgentProfile
from debate_agent.models import (
    AgentName,
    AgentSessionState,
    ChangeAck,
    SessionHealth,
)


class SessionIdentityError(RuntimeError):
    """表示持久化 Session 与当前 run、workspace 或 Profile 不一致。"""


class SessionManager:
    """为一个 run 管理选定且不可跨边界复用的 CLI Session。"""

    def __init__(
        self,
        *,
        path: Path,
        run_id: str,
        workspace: Path,
        profiles: dict[AgentName, AgentProfile],
        sessions: dict[AgentName, AgentSessionState],
        rebuild_history: list[dict[str, Any]],
    ) -> None:
        """创建已验证的 Manager；参数为身份与状态，返回实例，不执行额外校验。"""
        self.path = path
        self.run_id = run_id
        self.workspace = workspace
        self.profiles = profiles
        self.sessions = sessions
        self.rebuild_history = rebuild_history

    @staticmethod
    def _profile_payload(
        profiles: dict[AgentName, AgentProfile],
    ) -> dict[str, dict[str, object]]:
        """序列化冻结 Profile；参数为配置映射，返回稳定 JSON 对象，不抛异常。"""
        return {
            agent.value: profile.model_dump(mode="json")
            for agent, profile in sorted(
                profiles.items(), key=lambda item: item[0].value
            )
        }

    @classmethod
    def _normalize_stored_profiles(
        cls, value: object
    ) -> dict[str, dict[str, object]]:
        """规范历史 Profile 快照。

        参数为持久化载荷；返回按当前模型补齐默认值的快照；格式非法时抛身份错误。
        """
        if not isinstance(value, dict):
            raise SessionIdentityError("Session Profile 快照格式非法")
        try:
            profiles = {
                AgentName(str(agent)): AgentProfile.model_validate(profile)
                for agent, profile in value.items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionIdentityError("Session Profile 快照格式非法") from exc
        return cls._profile_payload(profiles)

    @classmethod
    def open(
        cls,
        *,
        state_root: Path,
        run_id: str,
        workspace: Path,
        profiles: dict[AgentName, AgentProfile],
    ) -> SessionManager:
        """打开或创建 Session 存储。

        参数为状态根、run、workspace 和 Profile；返回 Manager；身份不符时抛错。
        """
        resolved_workspace = workspace.expanduser().resolve()
        path = (
            state_root.expanduser().resolve() / "runs" / run_id / "agent-sessions.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        expected_profiles = cls._profile_payload(profiles)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            stored_profiles = cls._normalize_stored_profiles(payload.get("profiles"))
            if (
                payload.get("run_id") != run_id
                or payload.get("workspace") != str(resolved_workspace)
                or stored_profiles != expected_profiles
            ):
                raise SessionIdentityError(
                    "Session 身份与 run/workspace/Profile 不一致"
                )
            sessions = {
                agent: AgentSessionState.model_validate(
                    payload["sessions"][agent.value]
                )
                for agent in profiles
            }
            rebuild_history = list(payload.get("rebuild_history", []))
        else:
            sessions = {agent: AgentSessionState() for agent in profiles}
            rebuild_history = []
        manager = cls(
            path=path,
            run_id=run_id,
            workspace=resolved_workspace,
            profiles=profiles,
            sessions=sessions,
            rebuild_history=rebuild_history,
        )
        if not path.exists():
            manager._save()
        return manager

    def _save(self) -> None:
        """原子保存 Session；无参数和返回值，文件错误时抛 OSError。"""
        payload = {
            "run_id": self.run_id,
            "workspace": str(self.workspace),
            "profiles": self._profile_payload(self.profiles),
            "sessions": {
                agent.value: state.model_dump(mode="json")
                for agent, state in self.sessions.items()
            },
            "rebuild_history": self.rebuild_history,
        }
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    def get(self, agent: AgentName) -> AgentSessionState:
        """读取 Agent Session；参数为 Agent，返回冻结状态，不抛异常。"""
        return self.sessions[agent]

    def bind(self, agent: AgentName, session_id: str) -> AgentSessionState:
        """绑定明确 Session ID；参数为 Agent/ID，返回新状态，空 ID 时抛错。"""
        if not session_id.strip():
            raise ValueError("session_id 不能为空")
        state = self.sessions[agent].model_copy(
            update={"session_id": session_id, "health": SessionHealth.ACTIVE}
        )
        self.sessions[agent] = state
        self._save()
        return state

    def record_ack(self, ack: ChangeAck, *, update_id: str) -> AgentSessionState:
        """记录同步游标；参数为 Ack/update，返回新状态，Session 未激活时抛错。"""
        current = self.sessions[ack.agent]
        if current.health is not SessionHealth.ACTIVE:
            raise SessionIdentityError("未激活 Session 不能记录 ChangeAck")
        state = current.model_copy(
            update={
                "last_ack_commit": ack.commit_sha,
                "last_update_id": update_id,
            }
        )
        self.sessions[ack.agent] = state
        self._save()
        return state

    def rebuild(self, agent: AgentName, *, health: SessionHealth) -> AgentSessionState:
        """重建不可信 Session。

        参数为 Agent 与旧健康原因；返回新代次 NEW 状态；ACTIVE 原因时抛错。
        """
        if health not in {SessionHealth.LOST, SessionHealth.UNCERTAIN}:
            raise ValueError("只有 lost/uncertain Session 可以重建")
        current = self.sessions[agent]
        self.rebuild_history.append(
            {
                "agent": agent.value,
                "generation": current.generation,
                "health": health.value,
                "session_id": current.session_id,
            }
        )
        state = AgentSessionState(generation=current.generation + 1)
        self.sessions[agent] = state
        self._save()
        return state
