"""test_opencode_health.py 按职责拆分后的测试集合。"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from opencode_health_support import (
    OpenCodeExecutor,
    check_opencode,
)

from debate_agent.adapters import OpenCodeAdapter, scan_opencode_jsonl
from debate_agent.cli_health import (
    FailureCategory,
    OpenCodeHealthStrategy,
)


def test_opencode_probe_uses_ephemeral_git_cwd_safe_stdin_and_runtime_flags(
    tmp_path: Path,
) -> None:
    workspace_before = set(tmp_path.iterdir())
    executor = OpenCodeExecutor()

    report = check_opencode(
        tmp_path,
        executor,
        model="provider-x/model-y",
        variant="quality",
    )

    assert report.can_start is True
    probe = next(call for call in executor.calls if call[2])
    argv, cwd, stdin, environment = probe
    assert executor.probe_git_observed is True
    assert cwd != tmp_path
    assert not cwd.exists()
    assert argv[argv.index("--dir") + 1] == str(cwd)
    assert argv[argv.index("--model") + 1] == "provider-x/model-y"
    assert argv[argv.index("--agent") + 1] == "build"
    assert argv[argv.index("--variant") + 1] == "quality"
    assert {"--format", "--auto", "--pure"}.issubset(argv)
    assert not {"--continue", "--fork", "--session"}.intersection(argv)
    assert '{"ok":true}' in stdin
    assert "provider-x/model-y" not in stdin
    assert environment["OPENCODE_DISABLE_AUTOUPDATE"] == "true"
    assert environment["OPENCODE_DISABLE_DEFAULT_PLUGINS"] == "true"
    assert set(tmp_path.iterdir()) - workspace_before == {tmp_path / "probes"}
    assert not any((tmp_path / "probes").iterdir())

@pytest.mark.parametrize(
    ("probe_stdout", "category"),
    [
        (
            json.dumps(
                {
                    "type": "error",
                    "sessionID": "error-session",
                    "error": {"code": "TEST"},
                }
            ),
            FailureCategory.PROTOCOL_ERROR,
        ),
        (
            json.dumps({"type": "text", "sessionID": "empty-session", "text": " "}),
            FailureCategory.PROTOCOL_ERROR,
        ),
        (
            json.dumps({"type": "text", "text": '{"ok":true}'}),
            FailureCategory.PROTOCOL_ERROR,
        ),
        (
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "text",
                            "sessionID": "conflict-one",
                            "text": "first",
                        }
                    ),
                    json.dumps(
                        {
                            "type": "text",
                            "sessionID": "conflict-two",
                            "text": "second",
                        }
                    ),
                ]
            ),
            FailureCategory.PROTOCOL_ERROR,
        ),
        (
            "\n".join(
                [
                    json.dumps(
                        {"type": "permission.requested", "sessionID": "wait-session"}
                    ),
                    json.dumps(
                        {
                            "type": "text",
                            "sessionID": "wait-session",
                            "text": '{"ok":true}',
                        }
                    ),
                ]
            ),
            FailureCategory.POLICY_VIOLATION,
        ),
        ("not-json", FailureCategory.PROTOCOL_ERROR),
    ],
)
def test_opencode_probe_protocol_failures_are_blocking_and_cleanup_obtained_sessions(
    tmp_path: Path,
    probe_stdout: str,
    category: FailureCategory,
) -> None:
    executor = OpenCodeExecutor(probe_stdout=probe_stdout)

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("probe").category is category
    discovered = {
        json.loads(line).get("sessionID")
        for line in probe_stdout.splitlines()
        if line.startswith("{") and isinstance(json.loads(line), dict)
    } - {None}
    cleanup_ids = {
        argv[3]
        for argv, _cwd, _stdin, _env in executor.calls
        if argv[1:3] == ("session", "delete")
    }
    assert cleanup_ids == discovered

def test_opencode_nonzero_probe_classifies_network_and_still_cleans_session(
    tmp_path: Path,
) -> None:
    executor = OpenCodeExecutor(
        probe_stdout=json.dumps(
            {
                "type": "text",
                "sessionID": "network-session",
                "text": '{"ok":true}',
            }
        ),
        probe_return_code=1,
        probe_stderr="TLS connection reset",
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("probe").category is FailureCategory.NETWORK_ERROR
    assert any(
        argv[1:3] == ("session", "delete")
        for argv, _cwd, _stdin, _env in executor.calls
    )

def test_opencode_cleanup_failure_is_warning_and_session_is_redacted(
    tmp_path: Path,
) -> None:
    session_id = "cleanup-sensitive-session"
    executor = OpenCodeExecutor(
        probe_stdout=json.dumps(
            {
                "type": "text",
                "sessionID": session_id,
                "part": {"type": "text", "text": '{"ok":true}'},
            }
        ),
        cleanup_ok=False,
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is True
    assert report.by_key("session_cleanup").level == "warning"
    serialized = report.model_dump_json()
    assert session_id not in serialized
    cleanup_evidence = next(
        argv for argv, _stdin, _env in report.command_evidence if "session" in argv
    )
    assert cleanup_evidence[3] == "<redacted-session>"

def test_opencode_cleanup_warning_does_not_replace_original_probe_failure(
    tmp_path: Path,
) -> None:
    executor = OpenCodeExecutor(
        probe_stdout=json.dumps(
            {"type": "error", "sessionID": "failed-cleanup-session"}
        ),
        cleanup_ok=False,
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("probe").category is FailureCategory.PROTOCOL_ERROR
    assert report.by_key("session_cleanup").level == "warning"

def test_opencode_cleanup_launch_error_is_only_redacted_warning(
    tmp_path: Path,
) -> None:
    session_id = "launch-error-session"
    executor = OpenCodeExecutor(
        probe_stdout=json.dumps(
            {"type": "text", "sessionID": session_id, "text": '{"ok":true}'}
        ),
        cleanup_raises=True,
        cleanup_error=OSError,
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is True
    assert report.by_key("session_cleanup").level == "warning"
    assert session_id not in report.model_dump_json()
    cleanup_evidence = next(
        argv for argv, _stdin, _env in report.command_evidence if "session" in argv
    )
    assert cleanup_evidence[3] == "<redacted-session>"

def test_opencode_cleanup_value_error_propagates(tmp_path: Path) -> None:
    executor = OpenCodeExecutor(
        probe_stdout=json.dumps(
            {"type": "text", "sessionID": "value-error-session", "text": "ok"}
        ),
        cleanup_raises=True,
        cleanup_error=ValueError,
    )

    with pytest.raises(ValueError, match="sensitive cleanup"):
        check_opencode(tmp_path, executor)

def test_opencode_invalid_session_token_is_not_executed_or_leaked(
    tmp_path: Path,
) -> None:
    session_id = "invalid/session-sensitive"
    executor = OpenCodeExecutor(
        probe_stdout=json.dumps(
            {"type": "text", "sessionID": session_id, "text": '{"ok":true}'}
        )
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("probe").category is FailureCategory.PROTOCOL_ERROR
    assert report.by_key("session_cleanup").level == "warning"
    assert not any(
        argv[1:3] == ("session", "delete")
        for argv, _cwd, _stdin, _env in executor.calls
    )
    assert session_id not in report.model_dump_json()
    assert session_id not in repr(report.command_evidence)

def test_opencode_health_and_adapter_share_safe_jsonl_scanner() -> None:
    state = scan_opencode_jsonl(
        [
            json.dumps(
                {
                    "type": "permission.requested",
                    "sessionID": "shared-session",
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "shared-session",
                    "part": {"type": "text", "text": "final"},
                }
            ),
        ]
    )

    assert state.session_ids == ("shared-session",)
    assert state.last_text == "final"
    assert state.interaction_wait is True
    assert "scan_opencode_jsonl" in inspect.getsource(
        OpenCodeHealthStrategy._probe_protocol
    )
    assert "scan_opencode_jsonl" in inspect.getsource(OpenCodeAdapter._decode_lines)
