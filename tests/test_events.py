from __future__ import annotations

from pathlib import Path

import pytest

from debate_agent.events import RunEventStore


def test_event_store_publishes_monotonic_events_and_latest_progress(
    tmp_path: Path,
) -> None:
    store = RunEventStore(state_root=tmp_path, run_id="RUN-1")

    first = store.publish(
        kind="node_started",
        phase="debate",
        node="writer_turn",
        actor="codex",
        title="Codex 开始写入",
        current_turn=1,
        max_turns=8,
        head="abc123",
    )
    second = store.publish(
        kind="node_completed",
        phase="validation",
        node="run_checkpoint_validation",
        actor="supervisor",
        title="检查点验证通过",
        summary="3 项验证全部通过",
        current_turn=1,
        max_turns=8,
        head="def456",
        validation_status="passed",
    )

    assert first.seq == 1
    assert second.seq == 2
    assert [event.seq for event in store.read_events()] == [1, 2]
    progress = store.read_progress()
    assert progress is not None
    assert progress.phase == "validation"
    assert progress.node == "run_checkpoint_validation"
    assert progress.title == "检查点验证通过"
    assert progress.current_turn == 1
    assert progress.max_turns == 8
    assert progress.validation_status == "passed"
    assert progress.last_event_seq == 2


def test_event_store_limits_history_and_rejects_unsafe_run_id(
    tmp_path: Path,
) -> None:
    store = RunEventStore(state_root=tmp_path, run_id="RUN-1")
    for index in range(3):
        store.publish(
            kind="notice",
            phase="preflight",
            title=f"事件 {index}",
        )

    assert [event.seq for event in store.read_events(limit=2)] == [2, 3]
    with pytest.raises(ValueError, match="run_id"):
        RunEventStore(state_root=tmp_path, run_id="../escape")
