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
    freeze_opencode_profile,
)
from pydantic import ValidationError

from debate_agent.adapters import OpenCodeAdapter
from debate_agent.artifacts import ArtifactStore
from debate_agent.cli_health import (
    CircuitBreakerStore,
    CliHealthReport,
    CliHealthStrategyResult,
    CliIdentity,
    FailureCategory,
    freeze_run_config,
)
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import AgentName


def test_opencode_freezes_variant_hash_and_private_content_for_adapter(
    tmp_path: Path,
) -> None:
    mcp_name = "freeze-local-entry"
    executor = OpenCodeExecutor(
        initial_config={
            "model": "provider-x/model-y",
            "mcp": {mcp_name: {}},
        },
        final_config={
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {mcp_name: {"enabled": False}},
        },
    )
    opencode_report = check_opencode(
        tmp_path,
        executor,
        model="provider-x/model-y",
        variant="quality",
    )
    codex_identity = CliIdentity(
        agent=AgentName.CODEX,
        executable="/opt/tools/codex",
        version="1.2.3",
        model="gpt-test",
    )
    codex_report = CliHealthReport(
        agent=AgentName.CODEX,
        checked_at=opencode_report.checked_at,
        identity=codex_identity,
        checks=(),
        can_start=True,
        enabled_environment_names=("PATH",),
    )
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        profiles={
            AgentName.CODEX: AgentProfile(command="codex", model="gpt-test"),
            AgentName.OPENCODE: AgentProfile(
                command="opencode",
                model="provider-x/model-y",
                variant="quality",
            ),
        },
    )

    frozen = freeze_run_config(
        config,
        reports={
            AgentName.CODEX: codex_report,
            AgentName.OPENCODE: opencode_report,
        },
    )
    profile = frozen.profiles[AgentName.OPENCODE]
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    adapter = OpenCodeAdapter(profile, schema_path=schema)
    first = adapter.build_invocation(
        worktree=tmp_path, prompt="prompt", session_id=None
    )
    resumed = adapter.build_invocation(
        worktree=tmp_path, prompt="prompt", session_id="resume-test"
    )
    final_debug_content = [
        call[3]["OPENCODE_CONFIG_CONTENT"]
        for call in executor.calls
        if call[0][1:] == ("debug", "config", "--pure")
    ][1]

    assert profile.command == "/opt/tools/opencode"
    assert profile.version == "1.18.3"
    assert profile.model == "provider-x/model-y"
    assert profile.variant == "quality"
    assert profile.runtime_config_hash == opencode_report.runtime_config_hash
    assert profile.runtime_config_content_hash == (
        opencode_report.runtime_config_content_hash
    )
    assert profile.runtime_config_resolved_hash == (
        opencode_report.runtime_config_resolved_hash
    )
    assert dict(first.environment_overrides)["OPENCODE_CONFIG_CONTENT"] == (
        final_debug_content
    )
    assert first.environment_overrides == resumed.environment_overrides
    assert hashlib.sha256(final_debug_content.encode()).hexdigest() == (
        profile.runtime_config_content_hash
    )
    assert profile.runtime_config_hash != profile.runtime_config_content_hash
    public = frozen.model_dump_json()
    assert final_debug_content not in public
    assert mcp_name not in public
    assert final_debug_content not in repr(frozen)
    assert final_debug_content not in repr(first)
    assert final_debug_content not in repr(
        CliHealthStrategyResult(
            identity=opencode_report.identity,
            checks=(),
            runtime_config_content=final_debug_content,
        )
    )

def test_opencode_internal_content_cannot_be_injected_or_persisted(
    tmp_path: Path,
) -> None:
    secret_content = '{"permission":"allow","private":"must-not-persist"}'
    with pytest.raises(ValidationError):
        AgentProfile.model_validate(
            {
                "command": "opencode",
                "model": "provider-x/model-y",
                "runtime_config_content": secret_content,
            }
        )

    executor = OpenCodeExecutor()
    report = check_opencode(tmp_path, executor)
    codex_identity = CliIdentity(
        agent=AgentName.CODEX,
        executable="/opt/tools/codex",
        version="1.2.3",
        model="gpt-test",
    )
    codex_report = CliHealthReport(
        agent=AgentName.CODEX,
        checked_at=report.checked_at,
        identity=codex_identity,
        checks=(),
        can_start=True,
        enabled_environment_names=("PATH",),
    )
    config = RunConfig(
        workspace=tmp_path,
        topic="目标",
        state_root=tmp_path / "state",
        participants=(AgentName.CODEX, AgentName.OPENCODE),
        profiles={
            AgentName.CODEX: AgentProfile(command="codex", model="gpt-test"),
            AgentName.OPENCODE: AgentProfile(command="opencode"),
        },
    )
    frozen = freeze_run_config(
        config,
        reports={AgentName.CODEX: codex_report, AgentName.OPENCODE: report},
    )
    content = [
        call[3]["OPENCODE_CONFIG_CONTENT"]
        for call in executor.calls
        if call[0][1:] == ("debug", "config", "--pure")
    ][1]

    store = ArtifactStore.create(frozen, run_id="RUN-PRIVATE-CONTENT")

    assert content not in store.config_path.read_text(encoding="utf-8")
    assert content not in report.model_dump_json()
    assert content not in repr(report)
    loaded = store.load_config().profiles[AgentName.OPENCODE]
    assert loaded.runtime_config_hash == report.runtime_config_hash
    assert loaded.runtime_config_content_hash == report.runtime_config_content_hash
    assert loaded.runtime_config_resolved_hash == report.runtime_config_resolved_hash
    schema = tmp_path / "loaded.schema.json"
    schema.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="预检|运行配置"):
        OpenCodeAdapter(loaded, schema_path=schema).build_invocation(
            worktree=tmp_path,
            prompt="prompt",
            session_id=None,
        )

def test_opencode_adapter_baseline_content_only_applies_without_preflight_hash(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    baseline = OpenCodeAdapter(
        AgentProfile(command="opencode", model="provider-x/model-y"),
        schema_path=schema,
    ).build_invocation(worktree=tmp_path, prompt="prompt", session_id=None)
    hashed_without_content = AgentProfile(
        command="opencode",
        model="provider-x/model-y",
        runtime_config_hash="a" * 64,
    )

    assert json.loads(
        dict(baseline.environment_overrides)["OPENCODE_CONFIG_CONTENT"]
    ) == {
        "permission": "allow",
        "share": "disabled",
        "snapshot": False,
        "autoupdate": False,
    }
    with pytest.raises(ValueError, match="预检|运行配置"):
        OpenCodeAdapter(hashed_without_content, schema_path=schema).build_invocation(
            worktree=tmp_path,
            prompt="prompt",
            session_id=None,
        )

def test_circuit_breaker_isolates_opencode_runtime_config_hash(tmp_path: Path) -> None:
    store = CircuitBreakerStore(
        state_root=tmp_path,
        threshold=1,
        window_seconds=60,
        cooldown_seconds=60,
    )
    identity = CliIdentity(
        agent=AgentName.OPENCODE,
        executable="/opt/tools/opencode",
        version="1.18.3",
        model="provider-x/model-y",
        variant="quality",
        runtime_config_hash="a" * 64,
    )
    changed = identity.model_copy(update={"runtime_config_hash": "b" * 64})

    store.record_failure(
        identity,
        category=FailureCategory.NETWORK_ERROR,
        signature="network",
    )

    assert store.active(identity) is not None
    assert store.active(changed) is None

def test_opencode_runtime_summary_hash_includes_mode_and_resolved_mcp_safety(
    tmp_path: Path,
) -> None:
    first = "summary-entry-a"
    second = "summary-entry-b"
    full_a_executor = OpenCodeExecutor(
        initial_config={"model": "provider-x/model-y", "mcp": {first: {}}},
        final_config={
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {first: {}},
        },
    )
    full_b_executor = OpenCodeExecutor(
        initial_config={"model": "provider-x/model-y", "mcp": {second: {}}},
        final_config={
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {second: {}},
        },
    )
    minimal_executor = OpenCodeExecutor(
        initial_config={"model": "provider-x/model-y", "mcp": {first: {}}},
        final_config={
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {first: {"enabled": False}},
        },
    )

    full_a = check_opencode(tmp_path / "full-a", full_a_executor, mcp_mode="full")
    full_b = check_opencode(tmp_path / "full-b", full_b_executor, mcp_mode="full")
    minimal = check_opencode(
        tmp_path / "minimal", minimal_executor, mcp_mode="minimal"
    )

    assert full_a.can_start and full_b.can_start and minimal.can_start
    assert full_a.runtime_config_hash != full_b.runtime_config_hash
    assert full_a.runtime_config_hash != minimal.runtime_config_hash
    assert full_a.runtime_config_content_hash == full_b.runtime_config_content_hash
    assert full_a.runtime_config_content_hash != minimal.runtime_config_content_hash
    full_content = [
        call[3]["OPENCODE_CONFIG_CONTENT"]
        for call in full_a_executor.calls
        if call[0][1:] == ("debug", "config", "--pure")
    ][1]
    assert hashlib.sha256(full_content.encode()).hexdigest() == (
        full_a.runtime_config_content_hash
    )
    assert full_a.runtime_config_hash != full_a.runtime_config_content_hash

def test_opencode_adapter_rejects_forged_internal_runtime_content(
    tmp_path: Path,
) -> None:
    name = "safe-entry"
    deny_content = json.dumps(
        {
            "permission": "deny",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    profile, _report = freeze_opencode_profile(
        tmp_path,
        OpenCodeExecutor(
            initial_config={"model": "provider-x/model-y", "mcp": {name: {}}},
            final_config={
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "mcp": {name: {"enabled": False}},
            },
        ),
    )
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")

    direct = profile.model_copy()
    object.__setattr__(direct, "_runtime_config_content", deny_content)
    copied = profile.model_copy(update={"_runtime_config_content": deny_content})
    missing_hash = profile.model_copy(update={"runtime_config_content_hash": None})
    wrong_hash = profile.model_copy(update={"runtime_config_content_hash": "b" * 64})

    for forged in (direct, copied, missing_hash, wrong_hash):
        with pytest.raises(ValueError, match="预检|运行配置|安全"):
            OpenCodeAdapter(forged, schema_path=schema).build_invocation(
                worktree=tmp_path,
                prompt="prompt",
                session_id=None,
            )

@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "mcp": {"entry-one": {"enabled": False}},
            },
            True,
        ),
        (
            {
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
            },
            False,
        ),
        (
            {
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "plugin": [],
                "mcp": {"entry-one": {"enabled": False}},
            },
            False,
        ),
        (
            {
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "agent": {"build": {"permission": "deny"}},
                "mcp": {"entry-one": {"enabled": False}},
            },
            False,
        ),
        (
            {
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "mcp": {"entry-two": {"enabled": False}},
            },
            False,
        ),
        (
            {
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "mcp": {
                    "entry-one": {"enabled": False, "command": ["unsafe"]}
                },
            },
            False,
        ),
    ],
)
def test_opencode_minimal_overlay_requires_exact_capability_semantics(
    payload: Mapping[str, object],
    expected: bool,
) -> None:
    names = ("entry-one",)
    names_hash = hashlib.sha256(
        json.dumps(list(names), separators=(",", ":")).encode()
    ).hexdigest()
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    assert OpenCodeAdapter._runtime_config_is_safe(
        content,
        mcp_mode="minimal",
        mcp_names=names,
        mcp_configured_count=1,
        mcp_names_hash=names_hash,
    ) is expected

@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
            },
            True,
        ),
        (
            {
                "permission": "allow",
                "share": "disabled",
                "snapshot": False,
                "autoupdate": False,
                "mcp": {},
            },
            False,
        ),
    ],
)
def test_opencode_full_overlay_forbids_mcp_key(
    payload: Mapping[str, object],
    expected: bool,
) -> None:
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    assert OpenCodeAdapter._runtime_config_is_safe(
        content,
        mcp_mode="full",
        mcp_names=(),
        mcp_configured_count=0,
        mcp_names_hash=hashlib.sha256(b"[]").hexdigest(),
    ) is expected

def test_opencode_profile_copy_cannot_reseal_or_mutate_minimal_capability(
    tmp_path: Path,
) -> None:
    name = "sealed-entry"
    executor = OpenCodeExecutor(
        initial_config={"model": "provider-x/model-y", "mcp": {name: {}}},
        final_config={
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
            "mcp": {name: {"enabled": False}},
        },
    )
    profile, _report = freeze_opencode_profile(tmp_path, executor)
    schema = tmp_path / "sealed.schema.json"
    schema.write_text("{}", encoding="utf-8")
    forged_content = json.dumps(
        {
            "permission": "allow",
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    forged_hash = hashlib.sha256(forged_content.encode()).hexdigest()

    capability = profile.__pydantic_private__["_runtime_config_capability"]
    object.__setattr__(
        capability,
        "_RuntimeConfigCapability__content",
        forged_content,
    )
    mutated = profile.model_copy(
        update={"runtime_config_content_hash": forged_hash}
    )
    copied = profile.model_copy(
        update={"_runtime_config_capability": object()}
    )
    directly_injected = profile.model_copy(deep=True)
    directly_injected._runtime_config_capability = object()  # type: ignore[assignment]

    for candidate in (mutated, copied, directly_injected):
        with pytest.raises(ValueError, match="能力|预检|运行配置|安全"):
            OpenCodeAdapter(candidate, schema_path=schema).build_invocation(
                worktree=tmp_path,
                prompt="prompt",
                session_id=None,
            )
    with pytest.raises((TypeError, ValueError)):
        profile.model_copy(
            update={"runtime_config_content_hash": forged_hash}
        ).with_internal_runtime_config(forged_content)

def test_opencode_full_identity_hashes_canonical_resolved_definitions(
    tmp_path: Path,
) -> None:
    name = "same-name"
    first_final = {
        "permission": "allow",
        "share": "disabled",
        "snapshot": False,
        "autoupdate": False,
        "mcp": {name: {"type": "local", "command": ["first-command"]}},
    }
    second_final = {
        "permission": "allow",
        "share": "disabled",
        "snapshot": False,
        "autoupdate": False,
        "mcp": {name: {"type": "remote", "url": "https://example.invalid"}},
    }
    first = check_opencode(
        tmp_path / "first",
        OpenCodeExecutor(
            initial_config={"model": "provider-x/model-y", "mcp": {name: {}}},
            final_config=first_final,
        ),
        mcp_mode="full",
    )
    second = check_opencode(
        tmp_path / "second",
        OpenCodeExecutor(
            initial_config={"model": "provider-x/model-y", "mcp": {name: {}}},
            final_config=second_final,
        ),
        mcp_mode="full",
    )
    canonical = json.dumps(
        first_final,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert first.can_start and second.can_start
    assert first.mcp_names_hash == second.mcp_names_hash
    assert first.runtime_config_content_hash == second.runtime_config_content_hash
    assert first.runtime_config_resolved_hash == hashlib.sha256(
        canonical.encode()
    ).hexdigest()
    assert first.runtime_config_resolved_hash != second.runtime_config_resolved_hash
    assert first.runtime_config_hash != second.runtime_config_hash
    serialized = first.model_dump_json()
    assert "first-command" not in serialized
