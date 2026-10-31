from __future__ import annotations

import json
from pathlib import Path

import pytest

from debate_agent.models import AgentName, CompletionAssessment, LeaseStatus
from debate_agent.supervisor import LeaseConflictError, Supervisor


def test_supervisor_allows_only_one_active_writer_and_persists_lease(
    tmp_path: Path,
) -> None:
    supervisor = Supervisor(state_root=tmp_path, run_id="RUN-1")

    lease = supervisor.grant_write_lease(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit="abc",
        goal_contract_version=1,
    )

    assert lease.status is LeaseStatus.ACTIVE
    prepared_path = supervisor.invocations_dir / "TURN-1.json"
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared"
    assert prepared["agent"] == "codex"
    assert supervisor.assert_writer(AgentName.CODEX) == lease
    with pytest.raises(LeaseConflictError):
        supervisor.grant_write_lease(
            agent=AgentName.CLAUDE,
            turn_id="TURN-2",
            base_commit="abc",
            goal_contract_version=1,
        )
    payload = json.loads(supervisor.active_lease_path.read_text())
    assert payload["agent"] == "codex"


def test_revoke_lease_clears_active_writer_and_archives_state(tmp_path: Path) -> None:
    supervisor = Supervisor(state_root=tmp_path, run_id="RUN-1")
    supervisor.grant_write_lease(
        agent=AgentName.CLAUDE,
        turn_id="TURN-2",
        base_commit="abc",
        goal_contract_version=2,
    )

    revoked = supervisor.revoke_write_lease("TURN-2")

    assert revoked.status is LeaseStatus.REVOKED
    assert not supervisor.active_lease_path.exists()
    assert (supervisor.leases_dir / "TURN-2.json").exists()
    with pytest.raises(LeaseConflictError):
        supervisor.assert_writer(AgentName.CLAUDE)


def test_incomplete_agent_owns_next_revision(tmp_path: Path) -> None:
    supervisor = Supervisor(state_root=tmp_path, run_id="RUN-1")

    assert (
        supervisor.choose_next_writer(
            current_writer=AgentName.CODEX,
            assessments={
                AgentName.CODEX: CompletionAssessment.COMPLETE,
                AgentName.CLAUDE: CompletionAssessment.INCOMPLETE,
            },
        )
        is AgentName.CLAUDE
    )
    assert (
        supervisor.choose_next_writer(
            current_writer=AgentName.CLAUDE,
            assessments={
                AgentName.CODEX: CompletionAssessment.INCOMPLETE,
                AgentName.CLAUDE: CompletionAssessment.INCOMPLETE,
            },
        )
        is AgentName.CODEX
    )


def test_read_only_phase_rejects_any_workspace_change(tmp_path: Path) -> None:
    supervisor = Supervisor(state_root=tmp_path, run_id="RUN-1")

    supervisor.assert_read_only_phase(
        before_fingerprint="same",
        after_fingerprint="same",
        phase="review",
    )
    with pytest.raises(LeaseConflictError, match="review"):
        supervisor.assert_read_only_phase(
            before_fingerprint="before",
            after_fingerprint="after",
            phase="review",
        )
