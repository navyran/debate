"""由 Supervisor 执行的权威 setup、基线、检查点和最终验证。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from debate_agent.artifacts import ArtifactStore
from debate_agent.policy import CommandPolicy
from debate_agent.process import ProcessRunner


class ValidationPhase(str, Enum):
    """标识验证发生的控制阶段。"""

    SETUP = "setup"
    BASELINE = "baseline"
    CHECKPOINT = "checkpoint"
    FINAL = "final"


class BlockingValidationError(RuntimeError):
    """表示 setup、基线或最终验证失败，不能继续相应阶段。"""


class ValidationCommandResult(BaseModel):
    """保存单条权威命令的脱敏结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...]
    return_code: int | None
    elapsed_seconds: float
    stdout_summary: str
    stderr_summary: str
    raw_output_ref: str
    timed_out: bool


class ValidationReport(BaseModel):
    """保存一个阶段的权威验证报告引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str
    phase: ValidationPhase
    passed: bool
    authoritative: bool = True
    commands: tuple[ValidationCommandResult, ...]
    report_ref: str
    head_sha: str | None = None


class ValidationRunner:
    """通过 ProcessRunner 串行执行已获策略批准的验证命令。"""

    def __init__(
        self,
        *,
        state_root: Path,
        run_id: str,
        process_runner: ProcessRunner,
        command_policy: CommandPolicy,
        artifact_store: ArtifactStore | None = None,
        summary_chars: int = 4000,
    ) -> None:
        """创建验证器。

        参数为状态/run、进程器、命令策略、可选产物 Store 和摘要上限；返回实例；
        Store 身份不匹配或上限非法时抛错。
        """
        if summary_chars <= 0:
            raise ValueError("summary_chars 必须为正数")
        self.run_dir = state_root.expanduser().resolve() / "runs" / run_id
        if artifact_store is not None and (
            artifact_store.run_id != run_id or artifact_store.run_dir != self.run_dir
        ):
            raise ValueError("ValidationRunner 与 ArtifactStore 的 run 身份不一致")
        self.validation_dir = self.run_dir / "validation"
        self.validation_dir.mkdir(parents=True, exist_ok=True)
        self.process_runner = process_runner
        self.command_policy = command_policy
        self.artifact_store = artifact_store
        self.summary_chars = summary_chars

    def _redact(self, text: str) -> str:
        """脱敏输出摘要；参数为文本，返回有限摘要，不抛异常。"""
        sensitive = re.compile(r"(?i)(token|password|secret|authorization|api[_-]?key)")
        lines = [
            "[REDACTED]" if sensitive.search(line) else line
            for line in text.splitlines()
        ]
        return "\n".join(lines)[: self.summary_chars]

    @staticmethod
    def _atomic_write(path: Path, payload: object) -> None:
        """原子写报告；参数为路径和 JSON 载荷，无返回值，文件错误时抛错。"""
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    def run(
        self,
        *,
        report_id: str,
        phase: ValidationPhase,
        commands: Sequence[Sequence[str]],
        cwd: Path,
        env: Mapping[str, str],
        head_sha: str | None = None,
    ) -> ValidationReport:
        """运行权威验证。

        参数为报告 ID、阶段、argv 计划、cwd、白名单环境和可选当前 HEAD；返回
        报告；策略违规时抛错。调用方未提供 HEAD 时保留旧报告兼容性，但该报告
        不能作为 ``EvidenceRef`` 的权威提交证据。
        """
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", report_id) is None:
            raise ValueError("report_id 必须是安全文件名标识")
        results: list[ValidationCommandResult] = []
        for index, command in enumerate(commands, start=1):
            argv = self.command_policy.validate(command)
            process = self.process_runner.run(
                invocation_id=f"VAL-{phase.value}-{report_id}-{index}",
                argv=argv,
                cwd=cwd,
                stdin="",
                timeout_seconds=1200,
                env=env,
            )
            results.append(
                ValidationCommandResult(
                    argv=argv,
                    return_code=process.return_code,
                    elapsed_seconds=process.elapsed_seconds,
                    stdout_summary=self._redact(process.stdout),
                    stderr_summary=self._redact(process.stderr),
                    raw_output_ref=process.raw_output_ref,
                    timed_out=process.timed_out,
                )
            )
        passed = all(
            result.return_code == 0 and not result.timed_out for result in results
        )
        report_ref = f"validation/{report_id}.json"
        report = ValidationReport(
            report_id=report_id,
            phase=phase,
            passed=passed,
            commands=tuple(results),
            report_ref=report_ref,
            head_sha=head_sha,
        )
        self._atomic_write(self.run_dir / report_ref, report.model_dump(mode="json"))
        if self.artifact_store is not None:
            self.artifact_store.register_supervisor_artifact(
                artifact_ref=report_ref,
                producer="validation_runner",
                head_sha=head_sha,
            )
            for result in results:
                self.artifact_store.register_supervisor_artifact(
                    artifact_ref=result.raw_output_ref,
                    producer="validation_runner",
                    head_sha=head_sha,
                )
        return report

    @staticmethod
    def assert_phase_success(report: ValidationReport) -> None:
        """执行阶段门禁。

        参数为报告；检查点失败允许继续；setup/基线/final 失败时抛阻断异常。
        """
        if report.passed or report.phase is ValidationPhase.CHECKPOINT:
            return
        raise BlockingValidationError(f"{report.phase.value} 权威验证失败")
