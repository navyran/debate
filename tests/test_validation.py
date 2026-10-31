from __future__ import annotations

import sys
from pathlib import Path

import pytest

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import RunConfig
from debate_agent.policy import CommandPolicy
from debate_agent.process import ProcessRunner
from debate_agent.validation import (
    BlockingValidationError,
    ValidationPhase,
    ValidationRunner,
)


def build_runner(tmp_path: Path) -> ValidationRunner:
    process_runner = ProcessRunner(raw_root=tmp_path / "runs" / "RUN-1" / "raw")
    command_policy = CommandPolicy(approved_commands=[(sys.executable, "-c")])
    return ValidationRunner(
        state_root=tmp_path,
        run_id="RUN-1",
        process_runner=process_runner,
        command_policy=command_policy,
    )


def test_checkpoint_validation_records_authoritative_results_and_can_fail(
    tmp_path: Path,
) -> None:
    runner = build_runner(tmp_path)
    report = runner.run(
        report_id="TURN-1",
        phase=ValidationPhase.CHECKPOINT,
        commands=[
            [sys.executable, "-c", "print('ok')"],
            [sys.executable, "-c", "raise SystemExit(3)"],
        ],
        cwd=tmp_path,
        env={},
    )

    assert report.passed is False
    assert report.authoritative is True
    assert [item.return_code for item in report.commands] == [0, 3]
    assert report.report_ref == "validation/TURN-1.json"
    assert (tmp_path / "runs" / "RUN-1" / report.report_ref).exists()
    runner.assert_phase_success(report)


@pytest.mark.parametrize(
    "phase", [ValidationPhase.SETUP, ValidationPhase.BASELINE, ValidationPhase.FINAL]
)
def test_setup_baseline_and_final_failures_are_blocking(
    tmp_path: Path, phase: ValidationPhase
) -> None:
    runner = build_runner(tmp_path)
    report = runner.run(
        report_id=phase.value,
        phase=phase,
        commands=[[sys.executable, "-c", "raise SystemExit(2)"]],
        cwd=tmp_path,
        env={},
    )

    with pytest.raises(BlockingValidationError):
        runner.assert_phase_success(report)


def test_validation_report_redacts_sensitive_output(tmp_path: Path) -> None:
    runner = build_runner(tmp_path)
    report = runner.run(
        report_id="REDACT",
        phase=ValidationPhase.CHECKPOINT,
        commands=[
            [
                sys.executable,
                "-c",
                "print('TOKEN=top-secret'); print('normal line')",
            ]
        ],
        cwd=tmp_path,
        env={},
    )

    assert "top-secret" not in report.commands[0].stdout_summary
    assert "[REDACTED]" in report.commands[0].stdout_summary
    assert "normal line" in report.commands[0].stdout_summary


def test_validation_report_persists_optional_supervisor_head_binding(
    tmp_path: Path,
) -> None:
    """验证 Supervisor 可将报告绑定到当前 HEAD；参数为临时目录，无返回值。"""
    runner = build_runner(tmp_path)

    report = runner.run(
        report_id="HEAD-BOUND",
        phase=ValidationPhase.CHECKPOINT,
        commands=[[sys.executable, "-c", "print('ok')"]],
        cwd=tmp_path,
        env={},
        head_sha="abc123",
    )

    assert report.head_sha == "abc123"
    persisted = (tmp_path / "runs" / "RUN-1" / report.report_ref).read_text(
        encoding="utf-8"
    )
    assert '"head_sha": "abc123"' in persisted


def test_validation_runner_registers_immutable_supervisor_artifacts(
    tmp_path: Path,
) -> None:
    """验证验证器登记报告和原始结果来源；参数为临时目录，无返回值。"""
    artifacts = ArtifactStore.create(
        RunConfig(workspace=tmp_path, topic="验证", state_root=tmp_path),
        run_id="RUN-1",
    )
    runner = ValidationRunner(
        state_root=tmp_path,
        run_id="RUN-1",
        process_runner=ProcessRunner(raw_root=artifacts.run_dir / "raw"),
        command_policy=CommandPolicy(approved_commands=[(sys.executable, "-c")]),
        artifact_store=artifacts,
    )
    report = runner.run(
        report_id="ATTESTED",
        phase=ValidationPhase.CHECKPOINT,
        commands=[[sys.executable, "-c", "print('ok')"]],
        cwd=tmp_path,
        env={},
        head_sha="abc123",
    )

    artifacts.verify_supervisor_artifact(
        artifact_ref=report.report_ref,
        producer="validation_runner",
        head_sha="abc123",
    )
    artifacts.verify_supervisor_artifact(
        artifact_ref=report.commands[0].raw_output_ref,
        producer="validation_runner",
        head_sha="abc123",
    )
