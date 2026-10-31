from __future__ import annotations

from pathlib import Path

import pytest

from debate_agent.memory import MemoryProtocolError, MemoryStore
from debate_agent.models import (
    AgentName,
    ChangeAck,
    Decision,
    TurnResult,
)
from debate_agent.prompts import (
    build_peer_memory_prompt,
    build_session_bootstrap_prompt,
)


def turn_result() -> TurnResult:
    return TurnResult(
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit="abc",
        observed_head="abc",
        goal_contract_version=1,
        decision=Decision.REVISE,
        summary="实现会话交接",
        accepted_points=[],
        issues=[],
        requested_scope_additions=[],
        requested_validation_additions=[],
        completion_assessment="incomplete",
    )


def test_memory_builds_machine_bound_summary_and_pending_update(tmp_path: Path) -> None:
    store = MemoryStore(state_root=tmp_path, run_id="RUN-1")
    summary = store.build_change_summary(
        result=turn_result(),
        parent_commit="abc",
        commit_sha="def",
        changed_paths=["src/session.py"],
        resolved_issues=["ISSUE-0"],
        validation_status="passed",
        validation_report_ref="validation/TURN-1.json",
        requested_attention=["检查恢复幂等性"],
    )
    update = store.publish(summary, target_agent=AgentName.CLAUDE)

    assert summary.commit_sha == "def"
    assert summary.changed_paths == ["src/session.py"]
    assert summary.behavior_summary == ["实现会话交接"]
    assert update.target_agent is AgentName.CLAUDE
    assert update.changed_paths == ["src/session.py"]
    assert store.pending(AgentName.CLAUDE) == [update.update_id]


def test_memory_summary_accepts_canonical_ledger_projection_over_writer_issues(
    tmp_path: Path,
) -> None:
    """验证交接摘要可由唯一 Ledger 投影覆盖 Writer 自述 Issue。"""
    store = MemoryStore(state_root=tmp_path, run_id="RUN-LEDGER")
    result = turn_result().model_copy(
        update={
            "issues": [
                {
                    "id": "WRITER-SELF-ISSUE",
                    "severity": "high",
                    "claim": "Writer 自述问题",
                    "evidence": [],
                    "proposed_change": None,
                }
            ]
        }
    )
    summary = store.build_change_summary(
        result=result,
        parent_commit="abc",
        commit_sha="def",
        changed_paths=["src/session.py"],
        resolved_issues=["ISSUE-RESOLVED"],
        validation_status="passed",
        validation_report_ref="validation/TURN-1.json",
        requested_attention=["ISSUE-OPEN: 需要独立复核"],
        opened_issues=["ISSUE-OPEN"],
    )
    update = store.publish(summary, target_agent=AgentName.CLAUDE)

    assert update.opened_issues == ["ISSUE-OPEN"]
    assert update.resolved_issues == ["ISSUE-RESOLVED"]
    assert update.requested_attention == ["ISSUE-OPEN: 需要独立复核"]
    assert "WRITER-SELF-ISSUE" not in update.opened_issues
    assert store.pending(AgentName.CLAUDE) == [update.update_id]


def test_ack_must_bind_current_head_and_clear_pending_cursor(tmp_path: Path) -> None:
    store = MemoryStore(state_root=tmp_path, run_id="RUN-1")
    summary = store.build_change_summary(
        result=turn_result(),
        parent_commit="abc",
        commit_sha="def",
        changed_paths=["src/session.py"],
        resolved_issues=[],
        validation_status="failed-validation",
        validation_report_ref="validation/TURN-1.json",
        requested_attention=[],
    )
    update = store.publish(summary, target_agent=AgentName.CLAUDE)
    stale = ChangeAck(
        agent=AgentName.CLAUDE,
        commit_sha="abc",
        reviewed_paths=[],
        understood_changes=["理解更新"],
        risks_or_disputes=["需要修复测试"],
        decision=Decision.REVISE,
    )
    with pytest.raises(MemoryProtocolError, match="当前 HEAD"):
        store.acknowledge(update_id=update.update_id, ack=stale, current_head="def")

    ack = stale.model_copy(update={"commit_sha": "def"})
    store.acknowledge(update_id=update.update_id, ack=ack, current_head="def")

    assert store.pending(AgentName.CLAUDE) == []
    store.assert_synchronized(AgentName.CLAUDE, current_head="def")


def test_lagging_agent_can_ack_older_update_at_current_head(tmp_path: Path) -> None:
    store = MemoryStore(
        state_root=tmp_path,
        run_id="RUN-1",
        participants=(AgentName.CODEX, AgentName.CLAUDE, AgentName.QODER),
    )
    summary = store.build_change_summary(
        result=turn_result(),
        parent_commit="abc",
        commit_sha="older",
        changed_paths=["src/session.py"],
        resolved_issues=[],
        validation_status="passed",
        validation_report_ref="validation/TURN-1.json",
        requested_attention=[],
    )
    update = store.publish(summary, target_agent=AgentName.QODER)
    ack = ChangeAck(
        agent=AgentName.QODER,
        commit_sha="current",
        reviewed_paths=["src/session.py"],
        understood_changes=["已读取 older 到 current 的完整 Git 变化"],
        risks_or_disputes=[],
        decision=Decision.ACCEPT,
    )

    store.acknowledge(
        update_id=update.update_id,
        ack=ack,
        current_head="current",
    )

    store.assert_synchronized(AgentName.QODER, current_head="current")


def test_opencode_checkpoint_fans_out_independent_updates_to_every_peer(
    tmp_path: Path,
) -> None:
    """验证 OpenCode 提交后每个其他参与者均有独立待确认更新。"""
    participants = (
        AgentName.CODEX,
        AgentName.CLAUDE,
        AgentName.QODER,
        AgentName.OPENCODE,
    )
    store = MemoryStore(
        state_root=tmp_path,
        run_id="RUN-OPENCODE",
        participants=participants,
    )
    summary = store.build_change_summary(
        result=turn_result().model_copy(update={"agent": AgentName.OPENCODE}),
        parent_commit="base",
        commit_sha="head",
        changed_paths=["result.txt"],
        resolved_issues=[],
        validation_status="passed",
        validation_report_ref="validation/TURN-1.json",
        requested_attention=[],
    )

    updates = {
        peer: store.publish(summary, target_agent=peer)
        for peer in participants
        if peer is not AgentName.OPENCODE
    }

    assert {update.target_agent for update in updates.values()} == {
        AgentName.CODEX,
        AgentName.CLAUDE,
        AgentName.QODER,
    }
    assert len({update.update_id for update in updates.values()}) == 3
    for peer, update in updates.items():
        assert store.pending(peer) == [update.update_id]
        store.acknowledge(
            update_id=update.update_id,
            ack=ChangeAck(
                agent=peer,
                commit_sha="head",
                reviewed_paths=["result.txt"],
                understood_changes=["已读取当前 HEAD 与 OpenCode 完整 Git 变更"],
                risks_or_disputes=[],
                decision=Decision.ACCEPT,
            ),
            current_head="head",
        )
        store.assert_synchronized(peer, current_head="head")


def test_memory_prompts_are_bounded_and_do_not_include_raw_or_hidden_reasoning(
    tmp_path: Path,
) -> None:
    store = MemoryStore(state_root=tmp_path, run_id="RUN-1")
    summary = store.build_change_summary(
        result=turn_result(),
        parent_commit="abc",
        commit_sha="def",
        changed_paths=["src/session.py"],
        resolved_issues=[],
        validation_status="passed",
        validation_report_ref="validation/TURN-1.json",
        requested_attention=[],
    )
    update = store.publish(summary, target_agent=AgentName.CLAUDE)

    peer_prompt = build_peer_memory_prompt(update, current_head="def")
    bootstrap = build_session_bootstrap_prompt(
        goal_contract="目标契约摘要",
        current_head="def",
        shared_summaries=[summary],
        issue_summary=["ISSUE-1"],
        validation_summary="passed",
    )

    assert "自主读取 Git" in peer_prompt
    assert "raw_output" not in peer_prompt
    assert "hidden_reasoning" not in peer_prompt
    assert "目标契约摘要" in bootstrap
    assert "def" in bootstrap
