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
    QoderAdapter,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import AgentName, TurnResult
from debate_agent.process import ProcessResult


def test_qoder_parses_terminal_stream_result(tmp_path: Path) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = QoderAdapter(AgentProfile(command="qodercli"), schema_path=schema)
    stdout = "\n".join(
        (
            json.dumps(
                {"type": "system", "subtype": "init", "session_id": "qoder-session"}
            ),
            json.dumps(
                {
                    "type": "result",
                    "session_id": "qoder-session",
                    "result": json.dumps(turn_payload("qoder")),
                }
            ),
        )
    )

    result = parse_adapter_result(
        adapter,
        process_result(stdout),
        response_model=TurnResult,
        turn_id="TURN-1",
        session_generation=1,
    )

    assert result.ok is True
    assert result.agent is AgentName.QODER
    assert result.session_id == "qoder-session"

def test_qoder_repairs_embedded_json_with_unescaped_quotes(tmp_path: Path) -> None:
    """验证 Qoder 在说明文字和非法内嵌引号下仍可提取合法协议。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = QoderAdapter(AgentProfile(command="qodercli"), schema_path=schema)
    payload = json.dumps(turn_payload("qoder"), ensure_ascii=False)
    payload = payload.replace(
        '"summary": "done"',
        '"summary": "对"中国AI视频创业公司"重要"',
    )
    stdout = json.dumps(
        {
            "type": "result",
            "session_id": "qoder-session",
            "result": f"已完成修改，返回 TurnResult。\n\n{payload}",
        },
        ensure_ascii=False,
    )

    result = parse_adapter_result(
        adapter,
        process_result(stdout),
        response_model=TurnResult,
        turn_id="TURN-1",
        session_generation=1,
    )

    assert result.ok is True
    assert result.structured_output is not None
    assert result.structured_output["summary"] == '对"中国AI视频创业公司"重要'

def test_adapters_parse_structured_output_and_session_ids(tmp_path: Path) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    codex = CodexAdapter(AgentProfile(command="codex"), schema_path=schema)
    codex_stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "codex-session"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(turn_payload()),
                    },
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    codex_result = parse_adapter_result(
        codex,
        process_result(codex_stdout),
        response_model=TurnResult,
        turn_id="TURN-1",
        session_generation=1,
    )

    claude = ClaudeAdapter(
        AgentProfile(command="claude"), schema_path=schema, allowed_tools=("Read",)
    )
    claude_result = parse_adapter_result(
        claude,
        process_result(
            json.dumps(
                {
                    "type": "result",
                    "session_id": "claude-session",
                    "result": "human-readable fallback",
                    "structured_output": turn_payload("claude"),
                }
            )
        ),
        response_model=TurnResult,
        turn_id="TURN-1",
        session_generation=2,
    )

    assert codex_result.ok is True
    assert codex_result.session_id == "codex-session"
    assert codex_result.structured_output == turn_payload()
    assert claude_result.ok is True
    assert claude_result.session_id == "claude-session"
    assert claude_result.session_generation == 2

def test_codex_parses_final_protocol_from_large_persisted_jsonl(
    tmp_path: Path,
) -> None:
    """验证 Codex 轨迹超过内存上限时仍从完整日志读取最终协议。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    stdout_path = tmp_path / "codex.stdout.log"
    events = [
        json.dumps({"type": "thread.started", "thread_id": "codex-session"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "aggregated_output": "x" * 1_100_000,
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(turn_payload()),
                },
            }
        ),
        json.dumps({"type": "turn.completed"}),
    ]
    stdout_path.write_text("\n".join(events), encoding="utf-8")
    process = replace(
        process_result(stdout_path.read_text(encoding="utf-8")[:1_048_576]),
        stdout_truncated=True,
        stdout_path=stdout_path,
    )
    adapter = CodexAdapter(AgentProfile(command="codex"), schema_path=schema)

    result = parse_adapter_result(
        adapter,
        process,
        response_model=TurnResult,
        turn_id="TURN-1",
        session_generation=1,
    )

    assert result.ok is True
    assert result.session_id == "codex-session"
    assert result.structured_output == turn_payload()

@pytest.mark.parametrize("stdout", ["", "not json", "{}"])
def test_adapter_rejects_empty_or_invalid_structured_output(
    tmp_path: Path, stdout: str
) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = ClaudeAdapter(
        AgentProfile(command="claude"), schema_path=schema, allowed_tools=("Read",)
    )

    with pytest.raises(AdapterOutputError):
        parse_adapter_result(
            adapter,
            process_result(stdout),
            response_model=TurnResult,
            turn_id="TURN-1",
            session_generation=1,
        )

def test_nonzero_process_result_returns_explicit_agent_failure(tmp_path: Path) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = CodexAdapter(AgentProfile(command="codex"), schema_path=schema)

    result = parse_adapter_result(
        adapter,
        process_result("", return_code=7),
        response_model=TurnResult,
        turn_id="TURN-1",
        session_generation=1,
    )

    assert result.ok is False
    assert result.return_code == 7
    assert result.error == "CLI 退出码 7"
    assert result.agent is AgentName.CODEX

def test_claude_failure_preserves_terminal_reason(tmp_path: Path) -> None:
    """验证 Claude 非零退出时保留结构化终止原因。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = ClaudeAdapter(
        AgentProfile(command="claude"), schema_path=schema, allowed_tools=("Read",)
    )
    stdout = json.dumps(
        {
            "terminal_reason": "structured_output_retry_exhausted",
            "errors": ["Failed to provide valid structured output after 5 attempts"],
        }
    )

    result = parse_adapter_result(
        adapter,
        process_result(stdout, return_code=1),
        response_model=TurnResult,
        turn_id="TURN-1",
        session_generation=1,
    )

    assert result.error == (
        "CLI 退出码 1: structured_output_retry_exhausted: "
        "Failed to provide valid structured output after 5 attempts"
    )

def test_qoder_failure_with_empty_stdout_preserves_stderr(tmp_path: Path) -> None:
    """验证 Qoder 失败仅写 stderr 时返回失败结果而不是触发解析异常。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = QoderAdapter(AgentProfile(command="qodercli"), schema_path=schema)

    result = parse_adapter_result(
        adapter,
        process_result(
            "",
            return_code=42,
            stderr='Error resuming session: Invalid session identifier "session-1".',
        ),
        response_model=TurnResult,
        turn_id="TURN-1",
        session_generation=1,
    )

    assert result.ok is False
    assert result.return_code == 42
    assert result.error == (
        'CLI 退出码 42: Error resuming session: Invalid session identifier "session-1".'
    )

def test_truncated_output_is_rejected_and_format_retry_runs_at_most_once(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = ClaudeAdapter(
        AgentProfile(command="claude"), schema_path=schema, allowed_tools=("Read",)
    )
    truncated = replace(process_result("{}"), stdout_truncated=True)
    with pytest.raises(AdapterOutputError, match="截断"):
        parse_adapter_result(
            adapter,
            truncated,
            response_model=TurnResult,
            turn_id="TURN-1",
            session_generation=1,
        )

    attempts = 0

    def retry_once() -> ProcessResult:
        nonlocal attempts
        attempts += 1
        return process_result(
            json.dumps(
                {
                    "type": "result",
                    "session_id": "claude-session",
                    "structured_output": turn_payload("claude"),
                }
            )
        )

    with pytest.raises(AdapterOutputError):
        parse_adapter_result(adapter, process_result("not-json"))
    result = parse_adapter_result(adapter, retry_once())

    assert result.ok is True
    assert attempts == 1

def test_format_retry_budget_is_configurable_but_never_exceeds_one(
    tmp_path: Path,
) -> None:
    assert RunConfig(workspace=tmp_path, topic="goal").format_retries == 1
    disabled = RunConfig(workspace=tmp_path, topic="goal", format_retries=0)
    assert disabled.format_retries == 0
    with pytest.raises(ValueError):
        RunConfig(workspace=tmp_path, topic="goal", format_retries=2)
