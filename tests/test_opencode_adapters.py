"""test_adapters.py 按职责拆分后的测试集合。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from adapter_test_support import (
    parse_adapter_result,
    process_result,
    turn_payload,
)

from debate_agent.adapters import (
    AdapterOutputError,
    ClaudeAdapter,
    CodexAdapter,
    OpenCodeAdapter,
    QoderAdapter,
    SessionBoundaryError,
)
from debate_agent.config import AgentProfile
from debate_agent.models import AgentName


def test_opencode_invocation_requires_explicit_model(tmp_path: Path) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode"),
        schema_path=schema,
    )

    with pytest.raises(ValueError, match="OpenCode.*model|显式 model"):
        adapter.build_invocation(
            worktree=tmp_path,
            prompt="prompt",
            session_id=None,
        )

def test_opencode_invocation_has_only_three_stable_environment_overrides(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    opencode = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=schema,
    ).build_invocation(worktree=tmp_path, prompt="prompt", session_id=None)

    assert tuple(name for name, _value in opencode.environment_overrides) == (
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS",
    )
    config_content = dict(opencode.environment_overrides)[
        "OPENCODE_CONFIG_CONTENT"
    ]
    assert json.loads(config_content) == {
        "permission": "allow",
        "share": "disabled",
        "snapshot": False,
        "autoupdate": False,
    }
    assert dict(opencode.environment_overrides)[
        "OPENCODE_DISABLE_AUTOUPDATE"
    ] == "true"
    assert dict(opencode.environment_overrides)[
        "OPENCODE_DISABLE_DEFAULT_PLUGINS"
    ] == "true"

    codex = CodexAdapter(AgentProfile(command="codex"), schema_path=schema)
    claude = ClaudeAdapter(
        AgentProfile(command="claude"),
        schema_path=schema,
        allowed_tools=("Read",),
    )
    qoder = QoderAdapter(AgentProfile(command="qodercli"), schema_path=schema)
    for adapter in (codex, claude, qoder):
        invocation = adapter.build_invocation(
            worktree=tmp_path,
            prompt="prompt",
            session_id=None,
        )
        assert invocation.environment_overrides == ()

def test_opencode_terminal_uses_last_text_and_binds_unique_session(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=schema,
    )
    adapter.build_invocation(worktree=tmp_path, prompt="prompt", session_id=None)
    stdout = "\n".join(
        (
            json.dumps({"type": "step_start", "sessionID": "session-1"}),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "session-1",
                    "part": {"type": "text", "text": "earlier"},
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "session-1",
                    "part": {"type": "step-finish"},
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "session-1",
                    "part": {
                        "type": "text",
                        "text": json.dumps(turn_payload("opencode")),
                    },
                }
            ),
        )
    )

    parsed = parse_adapter_result(adapter, process_result(stdout))

    assert parsed.ok is True
    assert parsed.agent is AgentName.OPENCODE
    assert parsed.session_id == "session-1"
    assert parsed.structured_output == turn_payload("opencode")

@pytest.mark.parametrize(
    ("events", "error", "match"),
    [
        (
            [{"type": "text", "part": {"type": "text", "text": "{}"}}],
            AdapterOutputError,
            "session",
        ),
        (
            [
                {"type": "text", "sessionID": "one", "text": "{}"},
                {"type": "text", "sessionID": "two", "text": "{}"},
            ],
            SessionBoundaryError,
            "Session|session",
        ),
        (
            [{"type": "step_finish", "sessionID": "one"}],
            AdapterOutputError,
            "text|文本",
        ),
        (
            [
                {
                    "type": "error",
                    "sessionID": "one",
                    "error": {"code": "E_AUTH", "message": "secret-value"},
                },
                {"type": "text", "sessionID": "one", "text": "{}"},
            ],
            AdapterOutputError,
            "E_AUTH|错误",
        ),
    ],
)
def test_opencode_terminal_rejects_missing_conflicting_or_incomplete_boundary(
    tmp_path: Path,
    events: list[dict[str, object]],
    error: type[Exception],
    match: str,
) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=schema,
    )
    adapter.build_invocation(worktree=tmp_path, prompt="prompt", session_id=None)

    with pytest.raises(error, match=match) as captured:
        adapter.extract_terminal(
            process_result("\n".join(json.dumps(event) for event in events))
        )
    assert "secret-value" not in str(captured.value)

def test_opencode_resume_rejects_different_session(tmp_path: Path) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=schema,
    )
    adapter.build_invocation(
        worktree=tmp_path,
        prompt="prompt",
        session_id="expected-session",
    )
    stdout = json.dumps(
        {
            "type": "text",
            "sessionID": "other-session",
            "text": json.dumps(turn_payload("opencode")),
        }
    )

    with pytest.raises(SessionBoundaryError, match="Session|session"):
        adapter.extract_terminal(process_result(stdout))

def test_opencode_truncated_output_reads_complete_raw_spool(tmp_path: Path) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    raw = tmp_path / "opencode.stdout.log"
    lines = [
        json.dumps({"type": "step_start", "sessionID": "session-1"}),
        json.dumps(
            {
                "type": "text",
                "sessionID": "session-1",
                "text": json.dumps(turn_payload("opencode")),
            }
        ),
    ]
    raw.write_text("\n".join(lines), encoding="utf-8")
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=schema,
    )
    adapter.build_invocation(worktree=tmp_path, prompt="prompt", session_id=None)

    parsed = parse_adapter_result(
        adapter,
        replace(
            process_result(lines[0]),
            stdout_truncated=True,
            stdout_path=raw,
        ),
    )

    assert parsed.ok is True
    assert parsed.structured_output == turn_payload("opencode")
    parsed_from_empty_memory = parse_adapter_result(
        adapter,
        replace(
            process_result(""),
            stdout_truncated=True,
            stdout_path=raw,
        ),
    )
    assert parsed_from_empty_memory.structured_output == turn_payload("opencode")
    with pytest.raises(AdapterOutputError, match="截断"):
        parse_adapter_result(
            adapter,
            replace(process_result(lines[0]), stdout_truncated=True),
        )

def test_opencode_nonzero_failure_only_exposes_safe_error_fields(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=schema,
    )
    secret = "raw-secret-environment-value"
    stdout = json.dumps(
        {
            "type": "error",
            "error": {
                "code": "E_AUTH",
                "category": "authentication",
                "message": secret,
            },
        }
    )

    parsed = parse_adapter_result(
        adapter,
        process_result(stdout, return_code=1, stderr=secret),
    )

    assert parsed.ok is False
    assert parsed.error is not None
    assert "E_AUTH" in parsed.error
    assert secret not in parsed.error
