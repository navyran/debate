"""test_opencode_health.py 按职责拆分后的测试集合。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from opencode_health_support import (
    OpenCodeExecutor,
    check_opencode,
    command_tails,
)

from debate_agent.cli_health import (
    FailureCategory,
)


def test_opencode_minimal_disables_all_discovered_mcp_and_persists_only_hashes(
    tmp_path: Path,
) -> None:
    names = ("local-alpha", "local-beta")
    initial = {
        "model": "provider-x/model-y",
        "mcp": {name: {"type": "local"} for name in names},
    }
    final = {
        "permission": "allow",
        "share": "disabled",
        "snapshot": False,
        "autoupdate": False,
        "mcp": {name: {"enabled": False} for name in names},
    }
    executor = OpenCodeExecutor(initial_config=initial, final_config=final)

    report = check_opencode(tmp_path, executor)

    assert report.can_start is True
    assert report.mcp_configured_count == 2
    assert report.mcp_enabled_count == 0
    assert report.mcp_names_hash == hashlib.sha256(
        json.dumps(sorted(names), separators=(",", ":")).encode()
    ).hexdigest()
    debug_calls = [
        call for call in executor.calls if call[0][1:] == ("debug", "config", "--pure")
    ]
    first_content = json.loads(debug_calls[0][3]["OPENCODE_CONFIG_CONTENT"])
    final_content = json.loads(debug_calls[1][3]["OPENCODE_CONFIG_CONTENT"])
    assert "mcp" not in first_content
    assert final_content["mcp"] == {
        name: {"enabled": False} for name in names
    }
    assert not any(
        command[:2] == ("mcp", "list") for command in command_tails(executor)
    )
    serialized = report.model_dump_json()
    assert all(name not in serialized for name in names)

@pytest.mark.parametrize(
    "mcp",
    [
        {"local-alpha": {}},
        {"local-alpha": {"enabled": True}},
    ],
)
def test_opencode_minimal_blocks_any_final_enabled_mcp(
    tmp_path: Path, mcp: Mapping[str, object]
) -> None:
    executor = OpenCodeExecutor(
        initial_config={"model": "provider-x/model-y", "mcp": {"local-alpha": {}}},
        final_config={
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": mcp,
        },
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("mcp").category is FailureCategory.CONFIGURATION

@pytest.mark.parametrize("invalid_name", ["", "   "])
def test_opencode_rejects_empty_mcp_names(
    tmp_path: Path, invalid_name: str
) -> None:
    executor = OpenCodeExecutor(
        initial_config={"model": "provider-x/model-y", "mcp": {invalid_name: {}}}
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("mcp").category is FailureCategory.CONFIGURATION

def test_opencode_rejects_mcp_count_over_limit(tmp_path: Path) -> None:
    executor = OpenCodeExecutor(
        initial_config={
            "model": "provider-x/model-y",
            "mcp": {f"entry-{index}": {} for index in range(257)},
        }
    )

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("mcp").category is FailureCategory.CONFIGURATION
    assert not any(
        command[:2] == ("auth", "list") for command in command_tails(executor)
    )

def test_opencode_rejects_resolved_config_over_128_kib_without_leak(
    tmp_path: Path,
) -> None:
    marker = "oversize-sensitive-marker"
    raw = json.dumps(
        {"model": "provider-x/model-y", "mcp": {}, "padding": marker * 7000}
    )
    assert len(raw.encode()) > 128 * 1024
    executor = OpenCodeExecutor(initial_raw=raw)

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key("resolved_config_initial").category is (
        FailureCategory.CONFIGURATION
    )
    assert marker not in report.model_dump_json()

def test_opencode_accepts_resolved_config_at_exact_128_kib_boundary(
    tmp_path: Path,
) -> None:
    base = {"model": "provider-x/model-y", "mcp": {}, "padding": ""}
    empty = json.dumps(base, separators=(",", ":"))
    base["padding"] = "x" * (128 * 1024 - len(empty.encode()))
    raw = json.dumps(base, separators=(",", ":"))
    assert len(raw.encode()) == 128 * 1024

    report = check_opencode(tmp_path, OpenCodeExecutor(initial_raw=raw))

    assert report.can_start is True

def test_opencode_rejects_invalid_resolved_json_without_raw_leak(
    tmp_path: Path,
) -> None:
    raw = "invalid-sensitive-config"
    report = check_opencode(tmp_path, OpenCodeExecutor(initial_raw=raw))

    assert report.can_start is False
    assert raw not in report.model_dump_json()

def test_opencode_rejects_initial_resolved_config_with_surrogate_mcp_name(
    tmp_path: Path,
) -> None:
    """验证初始配置的孤立 surrogate 会稳定归为配置失败；参数为临时目录；无返回值。"""
    raw = '{"model":"provider-x/model-y","mcp":{"\\ud800":{}}}'

    report = check_opencode(tmp_path, OpenCodeExecutor(initial_raw=raw))

    assert report.can_start is False
    assert report.by_key("resolved_config_initial").category is (
        FailureCategory.CONFIGURATION
    )
    assert "\\ud800" not in report.model_dump_json()

def test_opencode_rejects_final_resolved_config_with_surrogate_mcp_name(
    tmp_path: Path,
) -> None:
    """验证最终配置的孤立 surrogate 会稳定归为配置失败；参数为临时目录；无返回值。"""
    raw = (
        '{"permission":"allow","share":"disabled","snapshot":false,'
        '"autoupdate":false,"mcp":{"\\ud800":{"enabled":false}}}'
    )

    report = check_opencode(
        tmp_path,
        OpenCodeExecutor(final_raw=raw),
    )

    assert report.can_start is False
    assert report.by_key("resolved_config_final").category is (
        FailureCategory.CONFIGURATION
    )
    assert "\\ud800" not in report.model_dump_json()

def test_opencode_full_preserves_mcp_and_optional_list_failure_is_warning(
    tmp_path: Path,
) -> None:
    names = ("full-alpha", "full-beta")
    initial = {
        "model": "provider-x/model-y",
        "mcp": {name: {} for name in names},
    }
    final = {
        "permission": "allow",
        "share": "disabled",
        "snapshot": False,
        "autoupdate": False,
        "mcp": {names[0]: {"enabled": False}, names[1]: {}},
    }
    executor = OpenCodeExecutor(
        initial_config=initial,
        final_config=final,
        mcp_ok=False,
    )

    report = check_opencode(tmp_path, executor, mcp_mode="full")

    assert report.can_start is True
    assert report.by_key("mcp").level == "warning"
    assert report.mcp_configured_count == 2
    assert report.mcp_enabled_count == 1
    assert report.mcp_list_status == "unavailable"
    assert report.mcp_list_total_count is None
    final_debug = [
        call for call in executor.calls if call[0][1:] == ("debug", "config", "--pure")
    ][1]
    assert "mcp" not in json.loads(final_debug[3]["OPENCODE_CONFIG_CONTENT"])
    assert ("mcp", "list", "--pure") in command_tails(executor)
    serialized = report.model_dump_json()
    assert all(name not in serialized for name in names)

def test_opencode_full_records_only_mcp_list_aggregate_status(
    tmp_path: Path,
) -> None:
    """验证 full MCP list 只记录总数、启用数和健康状态；参数为临时目录；无返回值。"""
    names = ("list-safe-alpha", "list-safe-beta")
    initial = {
        "model": "provider-x/model-y",
        "mcp": {name: {} for name in names},
    }
    final = {
        "permission": "allow",
        "share": "disabled",
        "snapshot": False,
        "autoupdate": False,
        "mcp": {names[0]: {"enabled": False}, names[1]: {}},
    }

    report = check_opencode(
        tmp_path,
        OpenCodeExecutor(initial_config=initial, final_config=final),
        mcp_mode="full",
    )

    assert report.can_start is True
    assert report.by_key("mcp").level == "success"
    assert report.mcp_list_total_count == 2
    assert report.mcp_list_enabled_count == 1
    assert report.mcp_list_healthy_count == 1
    assert report.mcp_list_status == "healthy"
    assert "总数 2，启用 1，健康 1" in report.by_key("mcp").detail
    serialized = report.model_dump_json()
    assert all(name not in serialized for name in names)

def test_opencode_full_mcp_list_unhealthy_status_warns_without_blocking(
    tmp_path: Path,
) -> None:
    """验证 full MCP 的失败状态降级为 warning；参数为临时目录；无返回值。"""
    name = "unhealthy-safe-name"
    mcp_stdout = "│  ℹ  ✗ unhealthy-safe-name failed\n└  1 server(s)"
    executor = OpenCodeExecutor(
        initial_config={"model": "provider-x/model-y", "mcp": {name: {}}},
        final_config={
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {name: {}},
        },
        mcp_stdout=mcp_stdout,
    )

    report = check_opencode(tmp_path, executor, mcp_mode="full")

    assert report.can_start is True
    assert report.by_key("mcp").level == "warning"
    assert report.by_key("mcp").category is FailureCategory.MCP_OPTIONAL_ERROR
    assert report.mcp_list_total_count == 1
    assert report.mcp_list_enabled_count == 1
    assert report.mcp_list_healthy_count == 0
    assert report.mcp_list_status == "degraded"
    assert name not in report.model_dump_json()

def test_opencode_full_mcp_list_count_mismatch_warns_without_blocking(
    tmp_path: Path,
) -> None:
    """验证 list 与最终配置计数不一致时不会伪称正常；参数为临时目录；无返回值。"""
    names = ("count-safe-alpha", "count-safe-beta")
    mcp_stdout = "│  ℹ  ✓ count-safe-alpha connected\n└  1 server(s)"
    executor = OpenCodeExecutor(
        initial_config={
            "model": "provider-x/model-y",
            "mcp": {name: {} for name in names},
        },
        final_config={
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {name: {} for name in names},
        },
        mcp_stdout=mcp_stdout,
    )

    report = check_opencode(tmp_path, executor, mcp_mode="full")

    assert report.can_start is True
    assert report.by_key("mcp").level == "warning"
    assert report.mcp_list_total_count == 1
    assert report.mcp_list_enabled_count == 1
    assert report.mcp_list_status == "inconsistent"
    serialized = report.model_dump_json()
    assert all(name not in serialized for name in names)

@pytest.mark.parametrize(
    ("case", "mcp_stdout"),
    [
        ("malformed", "malformed-sensitive-mcp-output"),
        ("count-mismatch", "│  ℹ  ✓ malformed-safe-name connected\n└  2 server(s)"),
        ("oversize", "oversize-sensitive-mcp-output" * 20_000),
    ],
)
def test_opencode_full_mcp_list_invalid_or_oversize_output_warns_without_leak(
    tmp_path: Path,
    case: str,
    mcp_stdout: str,
) -> None:
    """验证非法或超大 MCP list 输出只产生脱敏 warning；参数为目录/输出；无返回值。"""
    marker = "sensitive-mcp-output"
    executor = OpenCodeExecutor(mcp_stdout=mcp_stdout)

    report = check_opencode(tmp_path, executor, mcp_mode="full")

    assert report.can_start is True
    assert report.by_key("mcp").level == "warning"
    assert report.by_key("mcp").category is FailureCategory.MCP_OPTIONAL_ERROR
    assert report.mcp_list_status == "invalid"
    assert marker not in report.model_dump_json()
    assert case in {"malformed", "count-mismatch", "oversize"}

def test_opencode_full_mcp_launch_exception_warns_and_continues_probe(
    tmp_path: Path,
) -> None:
    executor = OpenCodeExecutor(raise_stage="mcp", stage_error=OSError)

    report = check_opencode(tmp_path, executor, mcp_mode="full")

    assert report.can_start is True
    assert report.by_key("mcp").level == "warning"
    assert report.by_key("mcp").category is FailureCategory.MCP_OPTIONAL_ERROR
    assert report.by_key("probe").level == "success"
    assert "sensitive mcp" not in report.model_dump_json()

@pytest.mark.parametrize(
    ("stage", "check_key", "category"),
    [
        ("version", "version", FailureCategory.CONFIGURATION),
        ("help", "features", FailureCategory.CACHE_INCOMPATIBLE),
        ("initial_debug", "resolved_config_initial", FailureCategory.CONFIGURATION),
        ("final_debug", "resolved_config_final", FailureCategory.CONFIGURATION),
        ("auth", "authentication", FailureCategory.AUTHENTICATION_ERROR),
        ("probe", "probe", FailureCategory.PROCESS_ERROR),
    ],
)
def test_opencode_expected_stage_launch_exceptions_become_stable_checks(
    tmp_path: Path,
    stage: str,
    check_key: str,
    category: FailureCategory,
) -> None:
    executor = OpenCodeExecutor(raise_stage=stage, stage_error=OSError)

    report = check_opencode(tmp_path, executor)

    assert report.can_start is False
    assert report.by_key(check_key).category is category
    assert "sensitive" not in report.model_dump_json()

@pytest.mark.parametrize(
    "stage",
    ["help", "initial_debug", "final_debug", "auth", "mcp", "probe"],
)
def test_opencode_executor_value_error_is_not_disguised_as_health_failure(
    tmp_path: Path,
    stage: str,
) -> None:
    executor = OpenCodeExecutor(raise_stage=stage, stage_error=ValueError)

    with pytest.raises(ValueError, match="sensitive"):
        check_opencode(
            tmp_path,
            executor,
            mcp_mode="full" if stage == "mcp" else "minimal",
        )
