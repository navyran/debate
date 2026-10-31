from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from debate_agent.adapters import (
    AdapterOutputError,
    ClaudeAdapter,
    CodeBuddyAdapter,
    CodexAdapter,
    HermesAdapter,
    OpenClawAdapter,
    OpenCodeAdapter,
)
from debate_agent.config import AgentProfile
from debate_agent.invocation_events import InvocationEventType
from debate_agent.models import AgentResult, TurnResult
from debate_agent.process import ProcessOutput, ProcessResult
from debate_agent.protocol_gateway import (
    ProtocolBinding,
    ProtocolGateway,
    VendorAdapter,
)


def turn_payload(agent: str) -> dict[str, object]:
    """构造终局协议；参数为 Agent 名，返回协议字典，不抛异常。"""
    return {
        "agent": agent,
        "turn_id": "TURN-1",
        "base_commit": "abc",
        "observed_head": "abc",
        "goal_contract_version": 1,
        "decision": "accept",
        "summary": "done",
        "accepted_points": [],
        "issues": [],
        "requested_scope_additions": [],
        "requested_validation_additions": [],
        "completion_assessment": "complete",
    }


def output(payload: object, *, stream: str = "stdout") -> ProcessOutput:
    """构造单行增量输出；参数为 JSON 载荷和流名，返回输出事件，不抛异常。"""
    return ProcessOutput(
        stream=stream,
        text=json.dumps(payload, ensure_ascii=False) + "\n",
        elapsed_seconds=1.0,
        complete_line=True,
    )


def result(stdout: str, *, stdout_path: Path | None = None) -> ProcessResult:
    """构造成功进程结果；参数为输出和可选 raw 路径，返回结果，不抛异常。"""
    return ProcessResult(
        return_code=0,
        stdout=stdout,
        stderr="",
        elapsed_seconds=1.0,
        raw_output_ref="raw/INV.json",
        stdout_truncated=stdout_path is not None,
        stderr_truncated=False,
        timed_out=False,
        cancelled=False,
        stdout_path=stdout_path,
    )


def adapter_schema(tmp_path: Path) -> Path:
    """创建测试 Schema；参数为临时目录，返回文件路径，写入失败时抛 OSError。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    return schema


def parse_adapter_result(
    adapter: VendorAdapter,
    process: ProcessResult,
) -> AgentResult:
    """经 Gateway 解析终止结果；参数为 Adapter/进程结果，返回统一结果。"""
    return ProtocolGateway().parse_result(
        adapter=adapter,
        result=process,
        response_model=TurnResult,
        binding=ProtocolBinding(
            agent=adapter.agent,
            turn_id="TURN-1",
            head_sha="abc",
            goal_contract_version=1,
        ),
        session_generation=1,
    )


def test_codex_stream_parser_maps_lifecycle_tools_files_messages_and_errors(
    tmp_path: Path,
) -> None:
    adapter = CodexAdapter(
        AgentProfile(command="codex", model="gpt-test"),
        schema_path=adapter_schema(tmp_path),
    )
    parser = adapter.create_stream_parser()

    started = parser.feed(output({"type": "thread.started", "thread_id": "secret"}))
    command = parser.feed(
        output(
            {
                "type": "item.started",
                "item": {"type": "command_execution", "command": "pytest -q"},
            }
        )
    )
    changed = parser.feed(
        output(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [{"path": "src/a.py"}, {"path": "../outside"}],
                },
            }
        )
    )
    message = parser.feed(
        output(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "阶段结论"},
            }
        )
    )
    first_error = parser.feed(
        output({"type": "error", "message": "model cache incompatible"})
    )
    duplicate_error = parser.feed(
        output({"type": "error", "message": "model cache incompatible"})
    )
    completed = parser.feed(output({"type": "turn.completed"}))

    assert started[0].event_type is InvocationEventType.STARTED
    assert started[0].useful is False
    assert "secret" not in started[0].summary
    assert command[0].event_type is InvocationEventType.TOOL_STARTED
    assert command[0].useful is True
    assert changed[0].event_type is InvocationEventType.FILE_CHANGED
    assert changed[0].changed_paths == ("src/a.py",)
    assert message[0].event_type is InvocationEventType.MESSAGE
    assert message[0].useful is True
    assert first_error[0].useful is False
    assert duplicate_error == ()
    assert completed[0].terminal is True
    assert completed[0].useful is True


def test_claude_invocation_and_stream_parser_support_partial_tool_and_result(
    tmp_path: Path,
) -> None:
    adapter = ClaudeAdapter(
        AgentProfile(command="claude", model="sonnet"),
        schema_path=adapter_schema(tmp_path),
        allowed_tools=("Read", "Edit"),
    )
    invocation = adapter.build_invocation(
        worktree=tmp_path,
        prompt="secret prompt",
        session_id=None,
    )
    parser = adapter.create_stream_parser(workspace=tmp_path)

    assert invocation.argv[invocation.argv.index("--output-format") + 1] == (
        "stream-json"
    )
    assert "--include-partial-messages" in invocation.argv
    assert "--verbose" in invocation.argv
    assert "--strict-mcp-config" in invocation.argv
    init = parser.feed(
        output({"type": "system", "subtype": "init", "session_id": "secret"})
    )
    tool = parser.feed(
        output(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "src/a.py"},
                        }
                    ]
                },
            }
        )
    )
    root_tool = parser.feed(
        output(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": str(tmp_path)},
                        }
                    ]
                },
            }
        )
    )
    partial = parser.feed(
        output(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "正在修复"},
                },
            }
        )
    )
    terminal = parser.feed(
        output(
            {
                "type": "result",
                "session_id": "secret",
                "structured_output": turn_payload("claude"),
            }
        )
    )

    assert init[0].useful is False
    assert "secret" not in init[0].summary
    assert tool[0].event_type is InvocationEventType.FILE_CHANGED
    assert tool[0].changed_paths == ("src/a.py",)
    assert root_tool[0].event_type is InvocationEventType.TOOL_STARTED
    assert root_tool[0].changed_paths == ()
    assert partial[0].event_type is InvocationEventType.MESSAGE
    assert partial[0].useful is True
    assert terminal[0].event_type is InvocationEventType.TERMINATED
    assert terminal[0].terminal is True


def test_claude_stream_result_uses_only_terminal_structured_output(
    tmp_path: Path,
) -> None:
    adapter = ClaudeAdapter(
        AgentProfile(command="claude"),
        schema_path=adapter_schema(tmp_path),
        allowed_tools=("Read",),
    )
    lines = [
        {"type": "system", "subtype": "init", "session_id": "claude-session"},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {
                    "type": "text_delta",
                    "text": json.dumps(turn_payload("codex")),
                },
            },
        },
        {
            "type": "result",
            "session_id": "claude-session",
            "structured_output": turn_payload("claude"),
        },
    ]

    parsed = parse_adapter_result(
        adapter,
        result("\n".join(json.dumps(line) for line in lines)),
    )

    assert parsed.ok is True
    assert parsed.session_id == "claude-session"
    assert parsed.structured_output == turn_payload("claude")


def test_codebuddy_json_parser_emits_only_safe_terminal_event(tmp_path: Path) -> None:
    """验证 CodeBuddy 单 JSON 输出映射为安全终止事件；参数为临时目录，无返回值。"""
    adapter = CodeBuddyAdapter(
        AgentProfile(command="codebuddy", model="gpt-5"),
        schema_path=adapter_schema(tmp_path),
    )
    parser = adapter.create_stream_parser(workspace=tmp_path)

    terminal = parser.feed(
        output(
            {
                "session_id": "secret-session",
                "structured_output": turn_payload("codebuddy"),
            }
        )
    )

    assert len(terminal) == 1
    assert terminal[0].event_type is InvocationEventType.TERMINATED
    assert terminal[0].terminal is True
    assert terminal[0].useful is True
    assert "secret-session" not in terminal[0].summary


def test_claude_partial_without_terminal_result_is_not_accepted(
    tmp_path: Path,
) -> None:
    adapter = ClaudeAdapter(
        AgentProfile(command="claude"),
        schema_path=adapter_schema(tmp_path),
        allowed_tools=("Read",),
    )
    partial = json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {
                    "type": "text_delta",
                    "text": json.dumps(turn_payload("claude")),
                },
            },
        }
    )

    with pytest.raises(AdapterOutputError, match="厂商终止输出无效"):
        parse_adapter_result(adapter, result(partial))


def test_codex_agent_message_without_turn_completed_is_not_accepted(
    tmp_path: Path,
) -> None:
    adapter = CodexAdapter(
        AgentProfile(command="codex"), schema_path=adapter_schema(tmp_path)
    )
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "codex-session"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": json.dumps(turn_payload("codex")),
                },
            }
        ),
    ]

    with pytest.raises(AdapterOutputError, match="厂商终止输出无效"):
        parse_adapter_result(adapter, result("\n".join(lines)))


def test_claude_truncated_memory_output_reads_terminal_result_from_raw_file(
    tmp_path: Path,
) -> None:
    adapter = ClaudeAdapter(
        AgentProfile(command="claude"),
        schema_path=adapter_schema(tmp_path),
        allowed_tools=("Read",),
    )
    raw = tmp_path / "claude.stdout.log"
    lines = [
        json.dumps({"type": "assistant", "message": {"content": "x" * 2000}}),
        json.dumps(
            {
                "type": "result",
                "session_id": "claude-session",
                "structured_output": turn_payload("claude"),
            }
        ),
    ]
    raw.write_text("\n".join(lines), encoding="utf-8")

    parsed = parse_adapter_result(
        adapter,
        replace(result(lines[0]), stdout_truncated=True, stdout_path=raw),
    )

    assert parsed.ok is True
    assert parsed.structured_output == turn_payload("claude")


def test_opencode_stream_maps_lifecycle_part_tools_text_error_and_progress(
    tmp_path: Path,
) -> None:
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=adapter_schema(tmp_path),
    )
    parser = adapter.create_stream_parser(workspace=tmp_path)
    secret = "do-not-expose-this-message"

    started = parser.feed(output({"type": "step_start", "sessionID": "secret"}))
    tool = parser.feed(
        output(
            {
                "type": "part",
                "part": {
                    "type": "tool",
                    "tool": "write",
                    "state": {"input": {"filePath": "src/a.py"}},
                },
            }
        )
    )
    message = parser.feed(
        output(
            {
                "type": "text",
                "part": {"type": "text", "text": "x" * 600},
            }
        )
    )
    error = parser.feed(
        output(
            {
                "type": "error",
                "error": {"code": "E_TEST", "message": secret},
            }
        )
    )
    progress = parser.feed(output({"type": "step_finish"}))

    assert started[0].event_type is InvocationEventType.STARTED
    assert tool[0].event_type is InvocationEventType.FILE_CHANGED
    assert tool[0].changed_paths == ("src/a.py",)
    assert message[0].event_type is InvocationEventType.MESSAGE
    assert len(message[0].summary) == 500
    assert error[0].event_type is InvocationEventType.ERROR
    assert "E_TEST" in error[0].summary
    assert secret not in error[0].summary
    assert progress[0].event_type is InvocationEventType.PROGRESS
    assert progress[0].terminal is False


def test_opencode_stream_rejects_unsafe_paths_and_caps_deduplicated_hints(
    tmp_path: Path,
) -> None:
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=adapter_schema(tmp_path),
    )
    parser = adapter.create_stream_parser(workspace=tmp_path)
    inside = tmp_path / "inside.py"
    long_prefix = "z" * 512
    paths = [
        "src/a.py",
        "src/a.py",
        ".env",
        ".github/workflows/ci.yml",
        "../outside.py",
        "/outside.py",
        "~debate-agent-user-that-does-not-exist/private.py",
        "",
        str(inside),
        f"{long_prefix}a",
        f"{long_prefix}b",
        *(f"src/{index}.py" for index in range(30)),
    ]

    events = parser.feed(
        output(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool_use",
                    "name": "edit",
                    "input": {"path": paths},
                },
            }
        )
    )

    assert events[0].event_type is InvocationEventType.FILE_CHANGED
    assert events[0].changed_paths[:4] == (
        "src/a.py",
        ".env",
        ".github/workflows/ci.yml",
        "inside.py",
    )
    assert len(events[0].changed_paths) == 20
    assert all(len(path) <= 512 for path in events[0].changed_paths)
    assert all(".." not in Path(path).parts for path in events[0].changed_paths)
    assert len(set(events[0].changed_paths)) == len(events[0].changed_paths)
    assert events[0].changed_paths.count(long_prefix) == 1


def test_opencode_stream_bounds_recursive_path_candidate_scanning(
    tmp_path: Path,
) -> None:
    """验证路径提取限制深度和扫描量；参数为临时目录，无返回值。"""
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=adapter_schema(tmp_path),
    )
    parser = adapter.create_stream_parser(workspace=tmp_path)
    deeply_nested: dict[str, object] = {"path": "too-deep.py"}
    for _ in range(32):
        deeply_nested = {"nested": deeply_nested}
    broad_candidates = [
        {"ignored": index} for index in range(300)
    ] + [{"path": "too-late.py"}]

    deep_event = parser.feed(
        output(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool_use",
                    "name": "edit",
                    "input": deeply_nested,
                },
            }
        )
    )
    broad_event = parser.feed(
        output(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool_use",
                    "name": "edit",
                    "input": broad_candidates,
                },
            }
        )
    )

    assert deep_event[0].event_type is InvocationEventType.TOOL_STARTED
    assert deep_event[0].changed_paths == ()
    assert broad_event[0].event_type is InvocationEventType.TOOL_STARTED
    assert broad_event[0].changed_paths == ()


def test_opencode_stream_ignores_partial_and_deduplicates_safe_parse_errors(
    tmp_path: Path,
) -> None:
    adapter = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider/model"),
        schema_path=adapter_schema(tmp_path),
    )
    parser = adapter.create_stream_parser(workspace=tmp_path)
    partial = ProcessOutput(
        stream="stdout",
        text='{"type":"text"',
        elapsed_seconds=1.0,
        complete_line=False,
    )
    invalid = ProcessOutput(
        stream="stdout",
        text="not-json\n",
        elapsed_seconds=1.0,
        complete_line=True,
    )
    stderr = ProcessOutput(
        stream="stderr",
        text="OPENCODE_CONFIG_CONTENT=raw-secret\n",
        elapsed_seconds=1.0,
        complete_line=True,
    )

    assert parser.feed(partial) == ()
    first = parser.feed(invalid)
    assert first[0].event_type is InvocationEventType.ERROR
    assert "not-json" not in first[0].summary
    assert parser.feed(invalid) == ()
    stderr_event = parser.feed(stderr)
    assert stderr_event[0].event_type is InvocationEventType.ERROR
    assert "raw-secret" not in stderr_event[0].summary
    assert parser.feed(stderr) == ()


def test_openclaw_stream_emits_only_evidenced_safe_lifecycle(
    tmp_path: Path,
) -> None:
    """验证 OpenClaw 流只发布启动、固定诊断和终止；参数为目录，无返回值。"""
    adapter = OpenClawAdapter(
        AgentProfile(command="openclaw"),
        schema_path=adapter_schema(tmp_path),
    )
    parser = adapter.create_stream_parser(workspace=tmp_path)
    secret = "PROMPT_SECRET"

    events = parser.feed(
        output(
            {
                "status": "ok",
                "result": {"meta": {"finalPromptText": secret}},
            }
        )
    )
    diagnostic = parser.feed(
        ProcessOutput(
            stream="stderr",
            text=secret,
            elapsed_seconds=1,
            complete_line=True,
        )
    )

    assert [event.event_type for event in events] == [
        InvocationEventType.STARTED,
        InvocationEventType.TERMINATED,
    ]
    assert events[-1].terminal is True
    assert diagnostic[0].event_type is InvocationEventType.ERROR
    assert secret not in repr(events + diagnostic)


def test_codebuddy_stream_parser_accepts_concatenated_jsonl_events(
    tmp_path: Path,
) -> None:
    """验证 CodeBuddy JSONL 同块多事件可增量识别终局；参数为目录，无返回值。"""
    adapter = CodeBuddyAdapter(
        AgentProfile(command="codebuddy", model="gpt-5"),
        schema_path=adapter_schema(tmp_path),
    )
    parser = adapter.create_stream_parser(workspace=tmp_path)
    payload = (
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "codebuddy-session",
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "session_id": "codebuddy-session",
                "result": "待 ProtocolGateway 处理",
            }
        )
        + "\n"
    )

    events = parser.feed(
        ProcessOutput(
            stream="stdout",
            text=payload,
            elapsed_seconds=1,
            complete_line=True,
        )
    )

    assert [event.event_type for event in events] == [
        InvocationEventType.STARTED,
        InvocationEventType.TERMINATED,
    ]
    assert events[-1].terminal is True


def test_hermes_stream_emits_safe_session_diagnostic_and_json_terminal(
    tmp_path: Path,
) -> None:
    """验证 Hermes 流不回显 Session/Prompt 且识别 JSON 终止；参数为目录，无返回值。"""
    parser = HermesAdapter(
        AgentProfile(
            command="hermes",
            provider="deepseek",
            model="deepseek-v4-flash",
        ),
        schema_path=adapter_schema(tmp_path),
    ).create_stream_parser(workspace=tmp_path)
    session = parser.feed(
        ProcessOutput(
            stream="stdout",
            text="session_id: secret-session\n",
            elapsed_seconds=1,
            complete_line=True,
        )
    )
    terminal = parser.feed(output({"decision": "accept"}))

    assert session[0].event_type is InvocationEventType.STARTED
    assert "secret-session" not in repr(session)
    assert terminal[0].event_type is InvocationEventType.TERMINATED
    assert terminal[0].terminal is True
