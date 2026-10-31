from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from debate_agent.artifacts import ArtifactError, ArtifactStore
from debate_agent.config import AgentProfile, RunConfig
from debate_agent.models import (
    AgentName,
    CompletionAssessment,
    Decision,
    ReviewProtocolVersion,
)
from debate_agent.review_models import (
    ReviewCoverageStatus,
    ReviewDimension,
    ReviewResult,
)


def review_result(*, summary: str = "审查尚未完成") -> ReviewResult:
    """构造可持久化的最小 ReviewResult。

    参数为摘要；返回绑定固定身份的未完成 Review；构造数据非法时传播 Pydantic
    校验错误。
    """
    return ReviewResult.model_validate(
        {
            "agent": "claude",
            "turn_id": "REVIEW-TURN-1",
            "head_sha": "abc",
            "goal_contract_version": 1,
            "protocol_version": "review-v1",
            "coverage": [
                {
                    "dimension": dimension.value,
                    "status": ReviewCoverageStatus.UNKNOWN.value,
                    "summary": f"待核查 {dimension.value}",
                    "evidence_refs": [],
                }
                for dimension in ReviewDimension
            ],
            "findings": [],
            "issue_dispositions": [],
            "strongest_counterexample": None,
            "counterexample_resolution": None,
            "completion_assessment": CompletionAssessment.INCOMPLETE.value,
            "decision": Decision.REVISE.value,
            "summary": summary,
        }
    )


def test_artifact_store_writes_and_reads_atomic_facilitator_record(
    tmp_path: Path,
) -> None:
    config = RunConfig(workspace=tmp_path, topic="goal", state_root=tmp_path / "state")
    store = ArtifactStore.create(config, run_id="RUN-1")
    payload = {"decision_id": "FAC-1", "status": "decision"}

    reference = store.write_facilitator_record("FAC-1", payload)

    assert reference == "facilitator/FAC-1.json"
    assert store.read_facilitator_record("FAC-1") == payload
    with pytest.raises(ValueError):
        store.write_facilitator_record("../escape", payload)


def test_artifact_store_persists_review_idempotently_by_bound_identity(
    tmp_path: Path,
) -> None:
    """验证 Review 以 HEAD/Reviewer/turn 幂等存储；参数为临时目录，无返回值。"""
    config = RunConfig(workspace=tmp_path, topic="goal", state_root=tmp_path / "state")
    store = ArtifactStore.create(config, run_id="RUN-REVIEW")
    review = review_result()

    reference = store.write_review_result(review)

    assert reference == "reviews/abc.claude.REVIEW-TURN-1.json"
    assert store.write_review_result(review) == reference
    assert store.read_review_result(
        head_sha="abc",
        reviewer=AgentName.CLAUDE,
        turn_id="REVIEW-TURN-1",
    ) == review
    assert store.find_review_result(
        head_sha="abc",
        reviewer=AgentName.CLAUDE,
        turn_id="REVIEW-TURN-1",
    ) == review
    assert store.find_review_result(
        head_sha="abc",
        reviewer=AgentName.CODEX,
        turn_id="REVIEW-TURN-1",
    ) is None
    with pytest.raises(ArtifactError, match="不同内容"):
        store.write_review_result(review_result(summary="同一身份的不同结论"))
    invalid_head = review.model_copy(update={"head_sha": "abc.claude"})
    with pytest.raises(ArtifactError, match="head_sha"):
        store.write_review_result(invalid_head)


def test_artifact_store_rejects_corrupt_or_identity_mismatched_review(
    tmp_path: Path,
) -> None:
    """验证 Review 损坏或身份不一致不会静默读取；参数为临时目录，无返回值。"""
    config = RunConfig(workspace=tmp_path, topic="goal", state_root=tmp_path / "state")
    store = ArtifactStore.create(config, run_id="RUN-BAD-REVIEW")
    reference = store.write_review_result(review_result())
    review_path = store.run_dir / reference
    review_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ArtifactError, match="损坏"):
        store.read_review_result(
            head_sha="abc",
            reviewer=AgentName.CLAUDE,
            turn_id="REVIEW-TURN-1",
        )

    payload = review_result().model_dump(mode="json")
    payload["turn_id"] = "REVIEW-TURN-2"
    review_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="身份不一致"):
        store.read_review_result(
            head_sha="abc",
            reviewer=AgentName.CLAUDE,
            turn_id="REVIEW-TURN-1",
        )
    payload = review_result().model_dump(mode="json")
    payload["agent"] = AgentName.CODEX.value
    review_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="身份不一致"):
        store.read_review_result(
            head_sha="abc",
            reviewer=AgentName.CLAUDE,
            turn_id="REVIEW-TURN-1",
        )


def test_artifact_store_rejects_symlinked_reviews_parent_for_write_and_read(
    tmp_path: Path,
) -> None:
    """验证 reviews 父目录为符号链接时，读写都不能越出 run。"""
    config = RunConfig(workspace=tmp_path, topic="goal", state_root=tmp_path / "state")
    store = ArtifactStore.create(config, run_id="RUN-REVIEW-SYMLINK")
    outside = tmp_path / "outside"
    outside.mkdir()
    store.reviews_dir.rmdir()
    store.reviews_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactError, match="符号链接"):
        store.write_review_result(review_result())
    with pytest.raises(ArtifactError, match="符号链接"):
        store.read_review_result(
            head_sha="abc",
            reviewer=AgentName.CLAUDE,
            turn_id="REVIEW-TURN-1",
        )
    assert list(outside.iterdir()) == []


def test_artifact_store_rejects_symlinked_or_directory_review_file(
    tmp_path: Path,
) -> None:
    """验证最终 Review 文件为符号链接或目录时，读写均不能越出或覆盖目标。"""
    config = RunConfig(workspace=tmp_path, topic="goal", state_root=tmp_path / "state")
    store = ArtifactStore.create(config, run_id="RUN-REVIEW-FILE-SYMLINK")
    review_path = store.reviews_dir / "abc.claude.REVIEW-TURN-1.json"
    outside = tmp_path / "outside.json"
    outside.write_text("outside remains", encoding="utf-8")
    review_path.symlink_to(outside)

    with pytest.raises(ArtifactError, match="符号链接"):
        store.write_review_result(review_result())
    with pytest.raises(ArtifactError, match="符号链接"):
        store.read_review_result(
            head_sha="abc",
            reviewer=AgentName.CLAUDE,
            turn_id="REVIEW-TURN-1",
        )
    assert outside.read_text(encoding="utf-8") == "outside remains"

    review_path.unlink()
    review_path.mkdir()
    with pytest.raises(ArtifactError, match="不是普通文件"):
        store.write_review_result(review_result())
    with pytest.raises(ArtifactError, match="不是普通文件"):
        store.read_review_result(
            head_sha="abc",
            reviewer=AgentName.CLAUDE,
            turn_id="REVIEW-TURN-1",
        )


def test_review_recovery_branches_by_frozen_protocol_and_records_diagnostic(
    tmp_path: Path,
) -> None:
    """验证 legacy 不读取 Review，review-v1 的缺失或损坏会阻断恢复。"""
    legacy = RunConfig(
        workspace=tmp_path,
        topic="legacy",
        state_root=tmp_path / "state",
    )
    legacy_store = ArtifactStore.create(legacy, run_id="RUN-LEGACY-REVIEW")
    (legacy_store.reviews_dir / "broken.json").write_text("{broken", encoding="utf-8")

    assert legacy_store.recover_review_result("reviews/broken.json") is None
    assert not (legacy_store.run_dir / "diagnostic.json").exists()

    review_v1 = legacy.model_copy(
        update={"protocol_version": ReviewProtocolVersion.REVIEW_V1}
    )
    store = ArtifactStore.create(review_v1, run_id="RUN-REVIEW-RECOVERY")
    with pytest.raises(ArtifactError, match="缺少 Review 引用"):
        store.recover_review_result(None)
    diagnostic = json.loads(
        (store.run_dir / "diagnostic.json").read_text(encoding="utf-8")
    )
    assert diagnostic["phase"] == "review_recovery"
    assert "review-v1" in diagnostic["error"]

    reference = store.write_review_result(review_result())
    assert store.recover_review_result(reference) == review_result()
    (store.run_dir / reference).write_text("{broken", encoding="utf-8")
    with pytest.raises(ArtifactError, match="恢复失败"):
        store.recover_review_result(reference)


def test_review_recovery_rejects_symlinked_reviews_parent(tmp_path: Path) -> None:
    """验证 review-v1 恢复不会经符号链接读取外部 reviews 目录。"""
    config = RunConfig(
        workspace=tmp_path,
        topic="recover",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        state_root=tmp_path / "state",
    )
    store = ArtifactStore.create(config, run_id="RUN-RECOVERY-SYMLINK")
    reference = store.write_review_result(review_result())
    (store.run_dir / reference).unlink()
    store.reviews_dir.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store.reviews_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactError, match="恢复失败: reviews/ 目录不能是符号链接"):
        store.recover_review_result(reference)
    diagnostic = json.loads(
        (store.run_dir / "diagnostic.json").read_text(encoding="utf-8")
    )
    assert diagnostic["phase"] == "review_recovery"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("target_kind", ("symlink", "directory"))
def test_review_recovery_rejects_non_regular_review_file(
    tmp_path: Path,
    target_kind: str,
) -> None:
    """验证 review-v1 恢复拒绝最终链接或目录，并写入诊断。"""
    config = RunConfig(
        workspace=tmp_path,
        topic="recover",
        protocol_version=ReviewProtocolVersion.REVIEW_V1,
        state_root=tmp_path / "state",
    )
    store = ArtifactStore.create(
        config,
        run_id=f"RUN-RECOVERY-FILE-{target_kind}",
    )
    reference = store.write_review_result(review_result())
    review_path = store.run_dir / reference
    review_path.unlink()
    outside = tmp_path / f"outside-{target_kind}.json"
    outside.write_text("outside remains", encoding="utf-8")
    if target_kind == "symlink":
        review_path.symlink_to(outside)
    else:
        review_path.mkdir()

    with pytest.raises(ArtifactError, match="review-v1 恢复失败"):
        store.recover_review_result(reference)
    diagnostic = json.loads(
        (store.run_dir / "diagnostic.json").read_text(encoding="utf-8")
    )
    assert diagnostic["phase"] == "review_recovery"
    assert outside.read_text(encoding="utf-8") == "outside remains"


def test_artifact_store_only_freezes_health_identity_before_side_effects(
    tmp_path: Path,
) -> None:
    config = RunConfig(workspace=tmp_path, topic="goal", state_root=tmp_path / "state")
    store = ArtifactStore.create(config, run_id="RUN-FREEZE")
    profiles = dict(config.profiles)
    profiles[AgentName.CODEX] = AgentProfile(
        command="/opt/codex",
        model="gpt-test",
        version="0.144.5",
    )
    replacement = config.model_copy(update={"profiles": profiles})

    store.replace_config_before_execution(
        expected=config,
        replacement=replacement,
    )

    assert store.load_config() == replacement
    store.prepare_invocation(
        invocation_id="INV-1",
        agent=AgentName.CODEX,
        turn_id="TURN-1",
        base_commit="abc",
        goal_contract_version=1,
        prompt_hash="sha256:test",
    )
    with pytest.raises(ArtifactError, match="副作用"):
        store.replace_config_before_execution(
            expected=replacement,
            replacement=config,
        )


def test_artifact_store_migrates_persisted_legacy_protocol_without_drift(
    tmp_path: Path,
) -> None:
    """验证真实旧 config.json 在恢复时固定为 legacy-v0；参数为临时目录，无返回值。"""
    config = RunConfig(
        workspace=tmp_path,
        topic="恢复旧 Run",
        state_root=tmp_path / "state",
    )
    store = ArtifactStore.create(config, run_id="RUN-LEGACY-PROTOCOL")
    legacy_payload = json.loads(store.config_path.read_text(encoding="utf-8"))
    legacy_payload.pop("protocol_version")
    store.config_path.write_text(
        json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8"
    )

    recovered = ArtifactStore.create(config, run_id="RUN-LEGACY-PROTOCOL")
    migrated_payload = json.loads(recovered.config_path.read_text(encoding="utf-8"))

    assert recovered.load_config().protocol_version is ReviewProtocolVersion.LEGACY_V0
    assert migrated_payload["protocol_version"] == "legacy-v0"
    assert "review_protocol_version" not in migrated_payload
    repeated = ArtifactStore.create(config, run_id="RUN-LEGACY-PROTOCOL")
    assert repeated.load_config() == config

    review_v1 = config.model_copy(
        update={"protocol_version": ReviewProtocolVersion.REVIEW_V1}
    )
    with pytest.raises(ArtifactError, match="不同配置"):
        ArtifactStore.create(review_v1, run_id="RUN-LEGACY-PROTOCOL")
    persisted_after_rejection = json.loads(
        recovered.config_path.read_text(encoding="utf-8")
    )
    assert persisted_after_rejection == migrated_payload


def test_artifact_store_rejects_invalid_legacy_protocol_alias_on_disk(
    tmp_path: Path,
) -> None:
    """验证存储层不静默接受旧协议别名；参数为临时目录，无返回值。"""
    config = RunConfig(
        workspace=tmp_path,
        topic="拒绝双协议字段",
        state_root=tmp_path / "state",
    )
    store = ArtifactStore.create(config, run_id="RUN-INVALID-PROTOCOL")
    invalid_payload = json.loads(store.config_path.read_text(encoding="utf-8"))
    invalid_payload["review_protocol_version"] = "legacy-v0"
    store.config_path.write_text(
        json.dumps(invalid_payload, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(ValidationError):
        ArtifactStore.create(config, run_id="RUN-INVALID-PROTOCOL")


def test_recoverable_invocations_excludes_persisted_failed_result(
    tmp_path: Path,
) -> None:
    """验证已确认失败且无副作用的结果不会与后续不确定 Writer 争抢恢复归属。"""
    config = RunConfig(workspace=tmp_path, topic="goal", state_root=tmp_path / "state")
    store = ArtifactStore.create(config, run_id="RUN-RECOVERABLE")
    for invocation_id in ("INV-FAILED", "INV-UNCERTAIN"):
        store.prepare_invocation(
            invocation_id=invocation_id,
            agent=AgentName.QODER,
            turn_id="TURN-2",
            base_commit="abc",
            goal_contract_version=1,
            prompt_hash=f"sha256:{invocation_id}",
        )
    store.mark_invocation_result(
        "INV-FAILED",
        result_ref="raw/INV-FAILED.json",
        ok=False,
    )
    store.mark_invocation_uncertain(
        "INV-UNCERTAIN",
        error="structured output invalid after workspace changes",
    )

    recoverable = store.recoverable_invocations()

    assert [item["invocation_id"] for item in recoverable] == ["INV-UNCERTAIN"]


def test_supervisor_artifact_proof_binds_run_identity_and_content(
    tmp_path: Path,
) -> None:
    """验证 Supervisor 产物证明绑定内容；参数为临时目录，无返回值。"""
    config = RunConfig(workspace=tmp_path, topic="验证产物", state_root=tmp_path)
    store = ArtifactStore.create(config, run_id="RUN-PROOF")
    artifact = store.run_dir / "raw" / "result.json"
    artifact.write_text('{"ok": true}', encoding="utf-8")

    store.register_supervisor_artifact(
        artifact_ref="raw/result.json",
        producer="validation_runner",
        head_sha="abc123",
    )
    store.verify_supervisor_artifact(
        artifact_ref="raw/result.json",
        producer="validation_runner",
        head_sha="abc123",
    )
    artifact.write_text('{"ok": false}', encoding="utf-8")

    with pytest.raises(ArtifactError, match="内容已变化"):
        store.verify_supervisor_artifact(
            artifact_ref="raw/result.json",
            producer="validation_runner",
            head_sha="abc123",
        )
