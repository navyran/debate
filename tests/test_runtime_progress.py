"""test_runtime.py 按职责拆分后的测试集合。"""

from __future__ import annotations

from pathlib import Path

import pytest
from git_helpers import create_repo
from runtime_test_support import (
    FakeAgentBackend,
)

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import RunConfig
from debate_agent.events import RunEventStore
from debate_agent.models import (
    RunStatus,
)
from debate_agent.runtime import DebateRuntime


def test_runtime_publishes_sanitized_node_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="进度事件测试",
        state_root=tmp_path / "state",
        max_turns=4,
    )
    artifacts = ArtifactStore.create(config, run_id="RUN-EVENTS")
    runtime = DebateRuntime(
        config=config,
        run_id="RUN-EVENTS",
        artifacts=artifacts,
        backend=FakeAgentBackend(),
    )
    monkeypatch.setattr(
        runtime,
        "_node_writer_turn",
        lambda state: {"current_head": "def456"},
    )

    update = runtime.run_node(
        "writer_turn",
        {
            "current_turn": 0,
            "max_turns": 4,
            "next_writer": "claude",
            "current_head": "abc123",
            "status": "running",
        },
    )

    assert update == {"current_head": "def456"}
    events = RunEventStore(
        state_root=config.state_root, run_id="RUN-EVENTS"
    ).read_events()
    assert [event.kind for event in events] == ["node_started", "node_completed"]
    assert all(event.phase == "debate" for event in events)
    assert all(event.actor == "claude" for event in events)
    assert events[0].current_turn == 1
    assert events[0].title == "Claude 开始写入"
    assert events[1].title == "Claude 完成写入"
    assert events[1].head == "def456"

def test_progress_timeout_counts_active_runtime_instead_of_offline_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证总时限累计活跃时间，且恢复间隔不会被计入。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="累计活跃时限测试",
        state_root=tmp_path / "state",
        total_timeout_seconds=100,
    )
    artifacts = ArtifactStore.create(config, run_id="RUN-ACTIVE-TIME")
    runtime = DebateRuntime(
        config=config,
        run_id="RUN-ACTIVE-TIME",
        artifacts=artifacts,
        backend=FakeAgentBackend(),
    )
    runtime._active_segment_started = 1_000.0
    monkeypatch.setattr("debate_agent.runtime_base.time.monotonic", lambda: 1_025.0)

    update = runtime.run_node(
        "progress_gate",
        {"active_elapsed_seconds": 50.0, "status": RunStatus.RUNNING.value},
    )

    assert update["active_elapsed_seconds"] == 75.0
    assert "status" not in update

def test_progress_timeout_stops_after_cumulative_active_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证跨恢复累计活跃时间达到预算后仍会终止。"""
    repo = create_repo(tmp_path)
    config = RunConfig(
        workspace=repo,
        topic="累计活跃时限终止测试",
        state_root=tmp_path / "state",
        total_timeout_seconds=100,
    )
    artifacts = ArtifactStore.create(config, run_id="RUN-ACTIVE-TIMEOUT")
    runtime = DebateRuntime(
        config=config,
        run_id="RUN-ACTIVE-TIMEOUT",
        artifacts=artifacts,
        backend=FakeAgentBackend(),
    )
    runtime._active_segment_started = 2_000.0
    monkeypatch.setattr("debate_agent.runtime_base.time.monotonic", lambda: 2_021.0)

    update = runtime.run_node(
        "progress_gate",
        {"active_elapsed_seconds": 80.0, "status": RunStatus.RUNNING.value},
    )

    assert update["active_elapsed_seconds"] == 101.0
    assert update["status"] == RunStatus.FAILED.value
    assert update["stop_reason"] == "达到累计活跃运行时限"
