from __future__ import annotations

import json
from pathlib import Path

import pytest

from debate_agent.openclaw_recovery import (
    OpenClawSessionOutcome,
    inspect_openclaw_session,
)


@pytest.mark.parametrize(
    ("sessions", "expected"),
    (
        ([], OpenClawSessionOutcome.NOT_RECEIVED),
        (
            [{"key": "agent:main:debate:test", "status": "running"}],
            OpenClawSessionOutcome.RUNNING,
        ),
        (
            [{"key": "agent:main:debate:test", "status": "killed"}],
            OpenClawSessionOutcome.UNKNOWN,
        ),
    ),
)
def test_openclaw_session_inspection_distinguishes_non_completed_states(
    tmp_path: Path,
    sessions: list[dict[str, str]],
    expected: OpenClawSessionOutcome,
) -> None:
    """验证未接收/运行中/未知三态；参数为目录/Session/预期，无返回值。"""
    inspection = inspect_openclaw_session(
        json.dumps({"sessions": sessions}),
        expected_session_key="agent:main:debate:test",
        allowed_sessions_root=tmp_path,
    )

    assert inspection.outcome is expected
    assert inspection.content is None


def test_openclaw_session_inspection_recovers_only_allowed_completed_terminal(
    tmp_path: Path,
) -> None:
    """验证完成态只读取允许目录中的最终助手正文；参数为目录，无返回值。"""
    root = tmp_path / "sessions"
    root.mkdir()
    session_file = root / "session.jsonl"
    session_file.write_text("{}\n", encoding="utf-8")
    stdout = json.dumps(
        {
            "sessions": [
                {
                    "key": "agent:main:debate:test",
                    "status": "done",
                    "sessionFile": str(session_file),
                }
            ]
        }
    )

    inspection = inspect_openclaw_session(
        stdout,
        expected_session_key="agent:main:debate:test",
        allowed_sessions_root=root,
        read_terminal=lambda path: '{"decision":"accept"}'
        if path == session_file
        else None,
    )

    assert inspection.outcome is OpenClawSessionOutcome.COMPLETED
    assert inspection.content == '{"decision":"accept"}'

    outside = json.loads(stdout)
    outside["sessions"][0]["sessionFile"] = str(tmp_path / "outside.jsonl")
    (tmp_path / "outside.jsonl").write_text("secret", encoding="utf-8")
    blocked = inspect_openclaw_session(
        json.dumps(outside),
        expected_session_key="agent:main:debate:test",
        allowed_sessions_root=root,
        read_terminal=lambda _path: "must-not-run",
    )
    assert blocked.outcome is OpenClawSessionOutcome.UNKNOWN
