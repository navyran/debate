"""test_adapters.py 按职责拆分后的测试集合。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from adapter_test_support import (
    parse_adapter_result,
    process_result,
    turn_payload,
)

from debate_agent.adapters import (
    HERMES_ARGV_PROMPT_MAX_BYTES,
    AdapterOutputError,
    ClaudeAdapter,
    CodeBuddyAdapter,
    CodexAdapter,
    HermesAdapter,
    OpenClawAdapter,
    OpenCodeAdapter,
    QoderAdapter,
    SessionBoundaryError,
    sanitize_openclaw_stdout,
)
from debate_agent.config import AgentProfile
from debate_agent.health_models import CLI_DEFAULT_MODEL
from debate_agent.models import AgentName


def test_codex_invocation_uses_workspace_sandbox_schema_and_stdin(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = CodexAdapter(AgentProfile(command="codex"), schema_path=schema)

    invocation = adapter.build_invocation(
        worktree=tmp_path,
        prompt="private prompt",
        session_id=None,
    )

    assert invocation.argv == (
        "codex",
        "exec",
        "-C",
        str(tmp_path),
        "--sandbox",
        "workspace-write",
        "--ignore-user-config",
        "--json",
        "--output-schema",
        str(schema),
        "-",
    )
    assert invocation.stdin == "private prompt"
    assert invocation.audit_argv == invocation.argv
    assert invocation.prompt_transport.value == "stdin"
    assert invocation.temporary_inputs == ()
    assert not {
        "--ephemeral",
        "--last",
        "--dangerously-bypass-approvals-and-sandbox",
    }.intersection(invocation.argv)

def test_codex_resume_requires_verified_sandbox_boundary(tmp_path: Path) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    unsafe = CodexAdapter(AgentProfile(command="codex"), schema_path=schema)
    with pytest.raises(SessionBoundaryError):
        unsafe.build_invocation(
            worktree=tmp_path,
            prompt="continue",
            session_id="session-1",
        )

    safe = CodexAdapter(
        AgentProfile(command="codex"),
        schema_path=schema,
        resume_sandbox_verified=True,
    )
    invocation = safe.build_invocation(
        worktree=tmp_path,
        prompt="continue",
        session_id="session-1",
    )
    assert invocation.argv[:4] == ("codex", "exec", "resume", "session-1")
    assert invocation.argv[4:6] == ("-c", 'sandbox_mode="workspace-write"')
    assert "--ignore-user-config" in invocation.argv
    assert "--last" not in invocation.argv

def test_codex_invocation_uses_compatible_copy_for_conditional_schema(
    tmp_path: Path,
) -> None:
    """验证 Codex 仅接收删减条件关键字的传输 Schema，原始规则仍保留。"""
    schema = tmp_path / "review.schema.json"
    original = {
        "type": "object",
        "properties": {
            "state": {"const": "complete"},
            "decision": {"enum": ["accept", "revise"]},
        },
        "allOf": [
            {
                "if": {"properties": {"state": {"const": "complete"}}},
                "then": {"required": ["state"]},
            }
        ],
    }
    schema.write_text(json.dumps(original), encoding="utf-8")

    invocation = CodexAdapter(
        AgentProfile(command="codex"), schema_path=schema
    ).build_invocation(worktree=tmp_path, prompt="review", session_id=None)

    codex_schema = Path(
        invocation.argv[invocation.argv.index("--output-schema") + 1]
    )
    assert codex_schema != schema
    assert json.loads(codex_schema.read_text(encoding="utf-8")) == {
        "type": "object",
        "properties": {
            "state": {"const": "complete", "type": "string"},
            "decision": {
                "enum": ["accept", "revise"],
                "type": "string",
            },
        },
    }
    assert json.loads(schema.read_text(encoding="utf-8")) == original

def test_claude_invocation_uses_uuid_permissions_tools_schema_and_stdin(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
            }
        ),
        encoding="utf-8",
    )
    adapter = ClaudeAdapter(
        AgentProfile(command="claude"),
        schema_path=schema,
        allowed_tools=("Read", "Edit", "Bash(git status *)"),
    )

    invocation = adapter.build_invocation(
        worktree=tmp_path,
        prompt="private prompt",
        session_id=None,
    )

    assert invocation.argv[:2] == ("claude", "-p")
    assert "--session-id" in invocation.argv
    assert invocation.session_id is not None
    assert "--permission-mode" in invocation.argv
    assert "acceptEdits" in invocation.argv
    assert "--json-schema" in invocation.argv
    schema_arg = invocation.argv[invocation.argv.index("--json-schema") + 1]
    assert "$schema" not in json.loads(schema_arg)
    mcp_arg = invocation.argv[invocation.argv.index("--mcp-config") + 1]
    assert json.loads(mcp_arg) == {"mcpServers": {}}
    assert invocation.stdin == "private prompt"
    assert invocation.audit_argv == invocation.argv
    assert invocation.prompt_transport.value == "stdin"
    assert invocation.temporary_inputs == ()
    assert not {
        "--continue",
        "--no-session-persistence",
        "--bare",
        "--dangerously-skip-permissions",
    }.intersection(invocation.argv)

def test_qoder_invocation_defaults_to_yolo_and_reuses_session(tmp_path: Path) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    adapter = QoderAdapter(AgentProfile(command="qodercli"), schema_path=schema)

    first = adapter.build_invocation(
        worktree=tmp_path,
        prompt="private prompt",
        session_id=None,
    )
    resumed = adapter.build_invocation(
        worktree=tmp_path,
        prompt="continue",
        session_id="qoder-session",
    )

    assert first.argv[:3] == ("qodercli", "--yolo", "-p")
    assert "--output-format" in first.argv
    assert "stream-json" in first.argv
    assert "--session-id" in first.argv
    assert first.stdin.startswith("private prompt")
    assert first.audit_argv == first.argv
    assert first.prompt_transport.value == "stdin"
    assert first.temporary_inputs == ()
    assert "JSON Schema" in first.stdin
    assert resumed.argv[resumed.argv.index("--resume") + 1] == "qoder-session"

def test_codebuddy_invocation_uses_yolo_json_schema_stdin_and_explicit_resume(
    tmp_path: Path,
) -> None:
    """验证 CodeBuddy 首轮及恢复参数；参数为临时目录，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text(
        '{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        '"type":"object"}',
        encoding="utf-8",
    )
    adapter = CodeBuddyAdapter(
        AgentProfile(
            command="codebuddy",
            model="gpt-5",
            native_profile="reviewer",
        ),
        schema_path=schema,
        minimal_mcp=True,
    )

    first = adapter.build_invocation(
        worktree=tmp_path,
        prompt="private prompt",
        session_id=None,
    )
    resumed = adapter.build_invocation(
        worktree=tmp_path,
        prompt="continue",
        session_id="session-1",
    )

    assert first.argv[:3] == ("codebuddy", "-p", "-y")
    assert first.argv[first.argv.index("--output-format") + 1] == "stream-json"
    assert first.argv[first.argv.index("--model") + 1] == "gpt-5"
    assert first.argv[first.argv.index("--agent") + 1] == "reviewer"
    assert first.argv[first.argv.index("--setting-sources") + 1] == "project"
    assert first.argv[first.argv.index("--tools") + 1] == (
        "Bash,Read,Edit,Write,Glob,Grep"
    )
    assert json.loads(first.argv[first.argv.index("--mcp-config") + 1]) == {
        "mcpServers": {}
    }
    schema_arg = first.argv[first.argv.index("--json-schema") + 1]
    assert "$schema" not in json.loads(schema_arg)
    assert first.stdin == "private prompt"
    assert "private prompt" not in first.argv
    assert "--continue" not in first.argv + resumed.argv
    assert "--no-session-persistence" not in first.argv + resumed.argv
    assert "--resume" not in first.argv
    assert resumed.argv[resumed.argv.index("--resume") + 1] == "session-1"
    assert dict(first.environment_overrides) == {
        "CODEBUDDY_DISABLE_AUTO_MEMORY": "1",
        "CODEBUDDY_DISABLE_HOT_RELOAD": "1",
        "CODEBUDDY_PROMPT_SUGGESTION_DISABLED": "1",
        "CODEBUDDY_SKIP_BUILTIN_MARKETPLACE": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_TELEMETRY": "1",
    }


def test_codebuddy_invocation_uses_cli_default_without_model_argument(
    tmp_path: Path,
) -> None:
    """验证 CodeBuddy CLI 默认模型不会被内部占位符覆盖；参数为目录，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    adapter = CodeBuddyAdapter(
        AgentProfile(command="codebuddy", model=CLI_DEFAULT_MODEL),
        schema_path=schema,
    )

    invocation = adapter.build_invocation(
        worktree=tmp_path,
        prompt="private prompt",
        session_id=None,
    )

    assert "--model" not in invocation.argv
    assert CLI_DEFAULT_MODEL not in invocation.argv


def test_codebuddy_terminal_requires_structured_output_and_stable_session(
    tmp_path: Path,
) -> None:
    """验证 CodeBuddy 终止协议和恢复边界；参数为临时目录，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    adapter = CodeBuddyAdapter(
        AgentProfile(command="codebuddy", model="gpt-5"),
        schema_path=schema,
    )

    first = adapter.build_invocation(
        worktree=tmp_path,
        prompt="first",
        session_id=None,
    )
    assert first.session_id is None
    parsed = parse_adapter_result(
        adapter,
        process_result(
            json.dumps(
                {
                    "session_id": "codebuddy-session",
                    "structured_output": turn_payload("codebuddy"),
                }
            )
        ),
    )
    assert parsed.agent is AgentName.CODEBUDDY
    assert parsed.session_id == "codebuddy-session"

    current_output = json.dumps(
        [
            {"type": "message", "sessionId": "codebuddy-session"},
            {
                "type": "result",
                "subtype": "success",
                "session_id": "codebuddy-session",
                "result": (
                    "<StructuredOutput>\n"
                    f"{json.dumps(turn_payload('codebuddy'))}\n"
                    "</StructuredOutput>"
                ),
            },
        ]
    )
    current = parse_adapter_result(adapter, process_result(current_output))
    assert current.agent is AgentName.CODEBUDDY
    assert current.session_id == "codebuddy-session"

    tool_call_output = json.dumps(
        [
            {
                "type": "result",
                "subtype": "success",
                "session_id": "codebuddy-session",
                "result": (
                    "<tool_calls:abc123>\n"
                    "<tool_call:abc123>StructuredOutput\n"
                    f"{json.dumps(turn_payload('codebuddy'))}"
                ),
            }
        ]
    )
    tool_call = parse_adapter_result(adapter, process_result(tool_call_output))
    assert tool_call.agent is AgentName.CODEBUDDY
    assert tool_call.session_id == "codebuddy-session"
    raw_terminal = adapter.extract_terminal(
        process_result(
            json.dumps(
                {
                    "type": "result",
                    "session_id": "codebuddy-session",
                    "result": "需要由 ProtocolGateway 语义转码的原始内容",
                }
            )
        )
    )
    assert raw_terminal.content == "需要由 ProtocolGateway 语义转码的原始内容"

    adapter.build_invocation(
        worktree=tmp_path,
        prompt="resume",
        session_id="codebuddy-session",
    )
    with pytest.raises(SessionBoundaryError, match="CodeBuddy"):
        adapter.extract_terminal(
            process_result(
                json.dumps(
                    {
                        "session_id": "other-session",
                        "structured_output": turn_payload("codebuddy"),
                    }
                )
            )
        )
    with pytest.raises(AdapterOutputError, match="structured_output"):
        adapter.extract_terminal(
            process_result(
                json.dumps(
                    {
                        "session_id": "codebuddy-session",
                    }
                )
            )
        )

def test_opencode_invocation_uses_exact_argv_stdin_schema_and_resume_boundary(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "turn.schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    adapter = OpenCodeAdapter(
        AgentProfile(
            command="opencode",
            model="provider/model",
            variant="high",
        ),
        schema_path=schema,
    )

    first = adapter.build_invocation(
        worktree=tmp_path,
        prompt="private prompt",
        session_id=None,
    )
    resumed = adapter.build_invocation(
        worktree=tmp_path,
        prompt="continue prompt",
        session_id="session-1",
    )

    expected_prefix = (
        "opencode",
        "run",
        "--format",
        "json",
        "--dir",
        str(tmp_path.resolve()),
        "--model",
        "provider/model",
        "--agent",
        "build",
        "--auto",
        "--pure",
        "--variant",
        "high",
    )
    assert first.argv == expected_prefix
    assert resumed.argv == (*expected_prefix, "--session", "session-1")
    assert "private prompt" not in first.argv
    assert first.stdin.startswith("private prompt")
    assert first.audit_argv == first.argv
    assert first.prompt_transport.value == "stdin"
    assert first.temporary_inputs == ()
    assert "最终响应只能" in first.stdin
    assert schema.read_text(encoding="utf-8") in first.stdin
    assert not {"--continue", "--fork"}.intersection(first.argv + resumed.argv)
    assert first.argv[first.argv.index("--agent") + 1] == "build"
    assert resumed.argv[resumed.argv.index("--agent") + 1] == "build"

def test_openclaw_invocation_uses_prompt_file_and_stable_explicit_session(
    tmp_path: Path,
) -> None:
    """验证 OpenClaw 首轮/续轮参数和 Prompt 边界；参数为临时目录，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    adapter = OpenClawAdapter(
        AgentProfile(
            command="openclaw",
            model="deepseek/deepseek-v4-pro",
            variant="off",
            native_profile="research",
            hard_timeout_seconds=180,
        ),
        schema_path=schema,
    )

    first = adapter.build_invocation(
        worktree=tmp_path,
        prompt="TOP_SECRET_PROMPT",
        session_id=None,
    )
    canonical_session = adapter.expected_session_id
    resumed = adapter.build_invocation(
        worktree=tmp_path,
        prompt="continue",
        session_id=canonical_session,
    )

    assert first.argv[:3] == ("openclaw", "agent", "--agent")
    assert first.argv[first.argv.index("--agent") + 1] == "research"
    assert first.argv[first.argv.index("--model") + 1] == (
        "deepseek/deepseek-v4-pro"
    )
    assert first.argv[first.argv.index("--thinking") + 1] == "off"
    assert first.argv[first.argv.index("--timeout") + 1] == "180"
    assert "--json" in first.argv
    assert first.argv[first.argv.index("--message-file") + 1] == (
        "__DEBATE_OPENCLAW_PROMPT_FILE__"
    )
    assert first.argv[first.argv.index("--session-key") + 1].startswith(
        "debate:"
    )
    assert canonical_session.startswith("agent:research:debate:")
    assert resumed.argv[resumed.argv.index("--session-key") + 1] == (
        first.argv[first.argv.index("--session-key") + 1]
    )
    assert first.stdin is None
    assert first.prompt_transport.value == "temp_file"
    assert len(first.temporary_inputs) == 1
    assert "TOP_SECRET_PROMPT" in first.temporary_inputs[0].content
    assert str(tmp_path.resolve()) in first.temporary_inputs[0].content
    assert schema.read_text(encoding="utf-8") in first.temporary_inputs[0].content
    assert "TOP_SECRET_PROMPT" not in repr(first)
    assert "TOP_SECRET_PROMPT" not in first.argv
    assert first.audit_argv[-1] != "TOP_SECRET_PROMPT"
    assert not {"--local", "--deliver", "--channel", "--to"}.intersection(
        first.argv
    )

def test_openclaw_terminal_extracts_text_and_verifies_canonical_session(
    tmp_path: Path,
) -> None:
    """验证 OpenClaw 终止解析和 Session 一致性；参数为临时目录，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = OpenClawAdapter(
        AgentProfile(command="openclaw", native_profile="main"),
        schema_path=schema,
    )
    adapter.build_invocation(worktree=tmp_path, prompt="prompt", session_id=None)
    payload = turn_payload("openclaw")
    stdout = json.dumps(
        {
            "status": "ok",
            "result": {
                "payloads": [{"text": json.dumps(payload)}],
                "meta": {
                    "systemPromptReport": {
                        "sessionKey": adapter.expected_session_id,
                    },
                    "finalPromptText": "must-not-be-used",
                },
            },
        }
    )

    parsed = parse_adapter_result(adapter, process_result(stdout))

    assert parsed.ok is True
    assert parsed.agent is AgentName.OPENCLAW
    assert parsed.session_id == adapter.expected_session_id
    assert parsed.structured_output == payload

    mismatched = json.loads(stdout)
    mismatched["result"]["meta"]["systemPromptReport"]["sessionKey"] = (
        "agent:main:debate:other"
    )
    with pytest.raises(SessionBoundaryError, match="Session|session"):
        adapter.extract_terminal(process_result(json.dumps(mismatched)))

def test_openclaw_stdout_sanitizer_allowlists_protocol_fields() -> None:
    """验证 OpenClaw 原始元数据不会进入持久化输出；无参数，无返回值。"""
    secret = "PROMPT_AND_GATEWAY_SECRET"
    sanitized = sanitize_openclaw_stdout(
        json.dumps(
            {
                "runId": secret,
                "status": "ok",
                "result": {
                    "payloads": [{"text": '{"ok":true}', "mediaUrl": secret}],
                    "meta": {
                        "agentMeta": {
                            "sessionId": secret,
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                        },
                        "systemPromptReport": {
                            "sessionKey": "agent:main:debate:safe",
                            "workspaceDir": secret,
                            "skills": [secret],
                        },
                        "finalPromptText": secret,
                        "finalAssistantRawText": secret,
                    },
                },
            }
        )
    )

    assert secret not in sanitized
    assert json.loads(sanitized) == {
        "status": "ok",
        "result": {
            "payloads": [{"text": '{"ok":true}'}],
            "meta": {
                "agentMeta": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                },
                "systemPromptReport": {
                    "sessionKey": "agent:main:debate:safe"
                },
            },
        },
    }

@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"status": "ok", "result": {"payloads": []}},
        {"status": "ok", "result": {"payloads": [{"text": ""}]}},
    ),
)
def test_openclaw_terminal_rejects_invalid_or_missing_text(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    """验证 OpenClaw 拒绝缺失边界字段；参数为目录/载荷，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = OpenClawAdapter(
        AgentProfile(command="openclaw"),
        schema_path=schema,
    )
    adapter.build_invocation(worktree=tmp_path, prompt="prompt", session_id=None)

    with pytest.raises(AdapterOutputError):
        adapter.extract_terminal(process_result(json.dumps(payload)))

@pytest.mark.parametrize(
    ("native_profile", "variant", "match"),
    (
        ("../outside", "off", "Profile"),
        ("main", "unsupported", "Thinking"),
    ),
)
def test_openclaw_invocation_rejects_invalid_profile_or_thinking(
    tmp_path: Path,
    native_profile: str,
    variant: str,
    match: str,
) -> None:
    """验证 OpenClaw 原生配置在执行前被拒绝；参数为配置场景，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = OpenClawAdapter(
        AgentProfile(
            command="openclaw",
            native_profile=native_profile,
            variant=variant,
        ),
        schema_path=schema,
    )

    with pytest.raises(ValueError, match=match):
        adapter.build_invocation(
            worktree=tmp_path,
            prompt="prompt",
            session_id=None,
        )

def test_hermes_invocation_uses_redacted_argv_and_reuses_session(
    tmp_path: Path,
) -> None:
    """验证 Hermes 首轮/续轮参数、脱敏和工作区边界；参数为目录，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    adapter = HermesAdapter(
        AgentProfile(
            command="hermes",
            provider="deepseek",
            model="deepseek-v4-flash",
        ),
        schema_path=schema,
    )

    first = adapter.build_invocation(
        worktree=tmp_path,
        prompt="HERMES_PRIVATE_PROMPT",
        session_id=None,
    )
    resumed = adapter.build_invocation(
        worktree=tmp_path,
        prompt="continue",
        session_id="20260726_130800_45de97",
    )

    assert first.argv[:5] == (
        "hermes",
        "chat",
        "--quiet",
        "--yolo",
        "--no-restore-cwd",
    )
    assert first.argv[first.argv.index("--provider") + 1] == "deepseek"
    assert first.argv[first.argv.index("--model") + 1] == "deepseek-v4-flash"
    assert "--resume" not in first.argv
    assert resumed.argv[resumed.argv.index("--resume") + 1] == (
        "20260726_130800_45de97"
    )
    assert first.argv[-2] == "--query"
    assert "HERMES_PRIVATE_PROMPT" in first.argv[-1]
    assert str(tmp_path.resolve()) in first.argv[-1]
    assert first.audit_argv[-1] == "<redacted>"
    assert "HERMES_PRIVATE_PROMPT" not in repr(first)
    assert first.stdin is None
    assert first.prompt_transport.value == "argv"
    assert not {
        "--worktree",
        "--ignore-rules",
        "--safe-mode",
        "gateway",
        "kanban",
    }.intersection(first.argv)

def test_hermes_terminal_separates_session_resume_banner_and_body(
    tmp_path: Path,
) -> None:
    """验证 Hermes Quiet 输出分离 Session 与正文；参数为目录，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = HermesAdapter(
        AgentProfile(
            command="hermes",
            provider="deepseek",
            model="deepseek-v4-flash",
        ),
        schema_path=schema,
    )
    adapter.build_invocation(
        worktree=tmp_path,
        prompt="prompt",
        session_id="20260726_130800_45de97",
    )
    payload = turn_payload("hermes")
    stdout = (
        "↻ Resumed session 20260726_130800_45de97 "
        "(1 user message, 2 total messages)\n\n"
        "session_id: 20260726_130800_45de97\n"
        + json.dumps(payload)
    )

    parsed = parse_adapter_result(adapter, process_result(stdout))

    assert parsed.ok is True
    assert parsed.agent is AgentName.HERMES
    assert parsed.session_id == "20260726_130800_45de97"
    assert parsed.structured_output == payload

def test_hermes_terminal_reads_session_status_from_stderr(
    tmp_path: Path,
) -> None:
    """验证 Hermes v0.19 的 stderr Session 边界；参数为临时目录，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = HermesAdapter(
        AgentProfile(
            command="hermes",
            provider="deepseek",
            model="deepseek-v4-flash",
        ),
        schema_path=schema,
    )
    adapter.build_invocation(
        worktree=tmp_path,
        prompt="prompt",
        session_id=None,
    )
    payload = turn_payload("hermes")

    parsed = parse_adapter_result(
        adapter,
        process_result(
            json.dumps(payload),
            stderr="\nsession_id: 20260726_130800_45de97\n",
        ),
    )

    assert parsed.session_id == "20260726_130800_45de97"
    assert parsed.structured_output == payload

def test_hermes_rejects_missing_session_mismatch_and_argv_overflow(
    tmp_path: Path,
) -> None:
    """验证 Hermes Session 边界和 UTF-8 长度门禁；参数为目录，无返回值。"""
    schema = tmp_path / "turn.schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = HermesAdapter(
        AgentProfile(
            command="hermes",
            provider="deepseek",
            model="deepseek-v4-flash",
        ),
        schema_path=schema,
    )
    adapter.build_invocation(
        worktree=tmp_path,
        prompt="prompt",
        session_id="expected",
    )
    with pytest.raises(AdapterOutputError, match="Session"):
        adapter.extract_terminal(process_result('{"ok":true}'))
    with pytest.raises(SessionBoundaryError, match="Session"):
        adapter.extract_terminal(
            process_result("session_id: other\n" + json.dumps(turn_payload("hermes")))
        )
    with pytest.raises(ValueError, match="上限"):
        adapter.build_invocation(
            worktree=tmp_path,
            prompt="中" * HERMES_ARGV_PROMPT_MAX_BYTES,
            session_id=None,
        )
