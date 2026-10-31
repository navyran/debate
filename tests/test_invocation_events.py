from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from debate_agent.invocation_events import (
    InvocationEvent,
    InvocationEventStore,
    InvocationEventType,
    InvocationSeverity,
    Redactor,
    read_redacted_tail,
)
from debate_agent.models import AgentName


def event_payload(**updates: object) -> dict[str, object]:
    """构造实时事件测试载荷；参数为覆盖字段，返回字典，不抛异常。"""
    payload: dict[str, object] = {
        "seq": 1,
        "timestamp": datetime.now(timezone.utc),
        "run_id": "RUN-1",
        "invocation_id": "INV-1",
        "agent": AgentName.CODEX,
        "turn_id": "TURN-1",
        "event_type": InvocationEventType.TOOL_STARTED,
        "title": "执行 pytest",
        "summary": "开始测试",
        "severity": InvocationSeverity.INFO,
        "elapsed_seconds": 1.5,
        "idle_seconds": 0.0,
        "changed_paths": ("src/a.py",),
        "terminal": False,
    }
    payload.update(updates)
    return payload


def test_invocation_event_rejects_unsafe_paths_and_truncates_summary() -> None:
    event = InvocationEvent.model_validate(
        event_payload(
            summary="x" * 700,
            changed_paths=(".env", ".github/workflows/ci.yml"),
        )
    )

    assert len(event.summary) == 500
    assert event.changed_paths == (".env", ".github/workflows/ci.yml")
    with pytest.raises(ValidationError, match="工作区相对路径"):
        InvocationEvent.model_validate(event_payload(changed_paths=("../secret",)))
    with pytest.raises(ValidationError, match="工作区相对路径"):
        InvocationEvent.model_validate(event_payload(changed_paths=("/tmp/a",)))


def test_store_publishes_progress_and_reads_incrementally_by_agent(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(state_root=tmp_path, run_id="RUN-1")
    first = store.publish(
        invocation_id="INV-CODEX",
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        event_type=InvocationEventType.TOOL_STARTED,
        title="运行测试",
        summary="pytest",
        elapsed_seconds=1.0,
        idle_seconds=0.0,
        changed_paths=("src/a.py",),
        useful=True,
    )
    second = store.publish(
        invocation_id="INV-CLAUDE",
        agent=AgentName.CLAUDE,
        turn_id="TURN-1",
        event_type=InvocationEventType.HEARTBEAT,
        title="仍在运行",
        elapsed_seconds=2.0,
        idle_seconds=1.0,
        useful=False,
    )

    assert (first.seq, second.seq) == (1, 2)
    assert store.read_events(after_seq=1) == [second]
    assert store.read_events(agent=AgentName.CODEX) == [first]
    progress = store.read_progress()
    assert progress is not None
    assert progress.last_event_seq == 2
    assert progress.agents[AgentName.CODEX].event_count == 1
    assert progress.agents[AgentName.CODEX].changed_file_count == 1
    assert progress.agents[AgentName.CODEX].last_useful_activity_at is not None
    assert progress.agents[AgentName.CLAUDE].last_useful_activity_at is None


def test_store_drops_changed_paths_changed_by_invocation_redactor(
    tmp_path: Path,
) -> None:
    """验证含调用密钥的路径不进入公开事件；参数为临时目录，无返回值。"""
    secret = (
        '{"autoupdate":false,"permission":"allow","share":"disabled",'
        '"snapshot":false}'
    )
    store = InvocationEventStore(
        state_root=tmp_path,
        run_id="RUN-SECRET-PATH",
        redactor=Redactor(workspace=tmp_path, secret_values=(secret,)),
    )

    event = store.publish(
        invocation_id="INV-OPEN",
        agent=AgentName.OPENCODE,
        turn_id="TURN-1",
        event_type=InvocationEventType.FILE_CHANGED,
        title="OpenCode 修改文件",
        elapsed_seconds=1.0,
        idle_seconds=0.0,
        changed_paths=("src/safe.py", f"tmp/{secret}.json"),
        useful=True,
    )

    assert event.changed_paths == ("src/safe.py",)
    assert store.read_events()[0].changed_paths == ("src/safe.py",)
    progress = store.read_progress()
    assert progress is not None
    assert progress.agents[AgentName.OPENCODE].changed_paths == ("src/safe.py",)
    public_content = (
        store.events_path.read_text(encoding="utf-8")
        + store.progress_path.read_text(encoding="utf-8")
    )
    assert secret not in public_content
    assert "tmp/<redacted>.json" not in public_content


def test_store_tolerates_partial_tail_and_bounds_incremental_window(
    tmp_path: Path,
) -> None:
    store = InvocationEventStore(state_root=tmp_path, run_id="RUN-1")
    for index in range(1_005):
        store.publish(
            invocation_id="INV-1",
            agent=AgentName.CODEX,
            turn_id="TURN-1",
            event_type=InvocationEventType.MESSAGE,
            title=f"事件 {index}",
            elapsed_seconds=float(index),
            idle_seconds=0.0,
            useful=True,
        )
    with store.events_path.open("ab") as handle:
        handle.write(b'{"seq":1006,"broken"')

    tail = store.read_events(after_seq=1_000, limit=3)

    assert [event.seq for event in tail] == [1003, 1004, 1005]
    assert store.read_events(after_seq=1_005) == []


def test_redactor_masks_secrets_sessions_credentials_and_external_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    redactor = Redactor(
        workspace=workspace,
        secret_values=("deepseek-secret", "proxy-secret"),
        session_ids=("session-0123456789abcdef",),
    )
    text = (
        "Prompt=不要泄露 Authorization: Bearer bearer-secret "
        "DEEPSEEK_API_KEY=deepseek-secret Cookie: sid=cookie-secret "
        "HTTPS_PROXY=https://user:proxy-secret@example.com "
        "session-0123456789abcdef /Users/test/private/key.pem "
        f"{workspace}/src/a.py"
    )

    sanitized = redactor.redact(text)

    for secret in (
        "不要泄露",
        "bearer-secret",
        "deepseek-secret",
        "cookie-secret",
        "proxy-secret",
        "session-0123456789abcdef",
        "/Users/test/private/key.pem",
    ):
        assert secret not in sanitized
    assert "<redacted>" in sanitized
    assert "src/a.py" in sanitized


def test_read_redacted_tail_does_not_return_earlier_lines_or_secrets(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw.log"
    raw.write_text(
        "old line\nkeep one\nTOKEN=top-secret\nkeep two\n",
        encoding="utf-8",
    )
    redactor = Redactor(workspace=tmp_path, secret_values=("top-secret",))

    tail = read_redacted_tail(raw, redactor=redactor, lines=3)

    assert "old line" not in tail
    assert "keep one" in tail
    assert "top-secret" not in tail
    assert tail.endswith("keep two")
