from __future__ import annotations

import json
from pathlib import Path

import pytest

from debate_agent.config import AgentProfile
from debate_agent.models import AgentName, ChangeAck, Decision, SessionHealth
from debate_agent.sessions import SessionIdentityError, SessionManager


def profiles() -> dict[AgentName, AgentProfile]:
    return {
        AgentName.CODEX: AgentProfile(command="codex", model="gpt-5"),
        AgentName.CLAUDE: AgentProfile(command="claude", model="opus"),
    }


def test_session_manager_persists_run_workspace_and_frozen_profiles(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.open(
        state_root=tmp_path / "state",
        run_id="RUN-1",
        workspace=workspace,
        profiles=profiles(),
    )
    manager.bind(AgentName.CODEX, "session-1")

    reopened = SessionManager.open(
        state_root=tmp_path / "state",
        run_id="RUN-1",
        workspace=workspace,
        profiles=profiles(),
    )

    assert reopened.get(AgentName.CODEX).session_id == "session-1"
    assert reopened.get(AgentName.CODEX).health is SessionHealth.ACTIVE
    with pytest.raises(SessionIdentityError):
        SessionManager.open(
            state_root=tmp_path / "state",
            run_id="RUN-1",
            workspace=tmp_path,
            profiles=profiles(),
        )
    changed_profiles = profiles()
    changed_profiles[AgentName.CODEX] = AgentProfile(command="codex", model="other")
    with pytest.raises(SessionIdentityError):
        SessionManager.open(
            state_root=tmp_path / "state",
            run_id="RUN-1",
            workspace=workspace,
            profiles=changed_profiles,
        )


def test_session_manager_accepts_legacy_profile_snapshot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.open(
        state_root=tmp_path / "state",
        run_id="RUN-1",
        workspace=workspace,
        profiles=profiles(),
    )
    payload = json.loads(manager.path.read_text(encoding="utf-8"))
    payload["profiles"] = {
        agent: {
            key: value
            for key, value in profile.items()
            if key in {"command", "model", "timeout_seconds"}
        }
        for agent, profile in payload["profiles"].items()
    }
    manager.path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = SessionManager.open(
        state_root=tmp_path / "state",
        run_id="RUN-1",
        workspace=workspace,
        profiles=profiles(),
    )

    assert reopened.profiles == profiles()


def test_lost_session_rebuilds_generation_and_requires_fresh_ack(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager.open(
        state_root=tmp_path / "state",
        run_id="RUN-1",
        workspace=workspace,
        profiles=profiles(),
    )
    manager.bind(AgentName.CLAUDE, "old")
    manager.record_ack(
        ChangeAck(
            agent=AgentName.CLAUDE,
            commit_sha="abc",
            reviewed_paths=[],
            understood_changes=["old"],
            risks_or_disputes=[],
            decision=Decision.ACCEPT,
        ),
        update_id="UPD-1",
    )

    rebuilt = manager.rebuild(AgentName.CLAUDE, health=SessionHealth.LOST)

    assert rebuilt.generation == 2
    assert rebuilt.session_id is None
    assert rebuilt.health is SessionHealth.NEW
    assert rebuilt.last_ack_commit is None
    assert rebuilt.last_update_id is None
    payload = json.loads(manager.path.read_text(encoding="utf-8"))
    assert payload["rebuild_history"] == [
        {
            "agent": "claude",
            "generation": 1,
            "health": "lost",
            "session_id": "old",
        }
    ]
