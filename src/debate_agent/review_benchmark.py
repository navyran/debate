"""对抗审查发布前基准的结构化摘要与脱敏报告生成。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from debate_agent.artifacts import ArtifactStore
from debate_agent.models import AgentName


class BenchmarkError(ValueError):
    """表示基准控制变量、摘要或报告路径不满足冻结约束。"""


class BenchmarkControls(BaseModel):
    """保存一次基准运行完整且可复核的控制变量。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: str
    participants: tuple[AgentName, ...]
    agent_profiles: dict[AgentName, dict[str, str]]
    initial_commit_sha: str
    goal_contract: dict[str, object]
    validation_commands: tuple[tuple[str, ...], ...]
    max_turns: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)
    facilitator: dict[str, object]
    random_seed: int | None = None
    repetitions: int = Field(gt=0)
    user_participation: str

    @field_validator("initial_commit_sha")
    @classmethod
    def validate_initial_commit_sha(cls, value: str) -> str:
        """校验初始提交 SHA；参数为 SHA，返回原值，非法格式时抛 ValueError。"""
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("initial_commit_sha 必须是 40 位小写 SHA")
        return value

    @field_validator("participants")
    @classmethod
    def validate_participants(
        cls, value: tuple[AgentName, ...]
    ) -> tuple[AgentName, ...]:
        """校验参与者；参数为 Agent 元组，返回原值，人数/重复非法时抛 ValueError。"""
        if len(value) < 2 or len(value) != len(set(value)):
            raise ValueError("基准参与者必须是至少两个互异 Agent")
        return value


class BenchmarkQuality(BaseModel):
    """保存发布前对抗审查质量指标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_blockers_found: int = Field(ge=0)
    known_blockers_total: int = Field(gt=0)
    missing_acceptance_found: int = Field(ge=0)
    unsupported_completion_blockers: int = Field(ge=0)
    style_false_positives: int = Field(ge=0)
    repeated_findings: int = Field(ge=0)
    ballot_overturns: int = Field(ge=0)
    clean_tasks_completed: int = Field(ge=0)
    clean_tasks_total: int = Field(gt=0)

    @property
    def known_blocker_detection_rate(self) -> float:
        """计算已知阻断发现率；无参数，返回 0 到 1 的比例。"""
        return self.known_blockers_found / self.known_blockers_total

    @property
    def clean_completion_rate(self) -> float:
        """计算干净任务完成率；无参数，返回 0 到 1 的比例。"""
        return self.clean_tasks_completed / self.clean_tasks_total


class BenchmarkCost(BaseModel):
    """保存基准成本与终止状态指标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    turns: int = Field(ge=0)
    total_cli_calls: int = Field(ge=0)
    max_turns_reached: bool
    stalled: bool
    user_participation_events: int = Field(ge=0)


class BenchmarkReliability(BaseModel):
    """保存协议与恢复的可靠性指标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    coverage_missing: int = Field(ge=0)
    invalid_evidence_rejected: int = Field(ge=0)
    output_repaired_or_degraded: int = Field(ge=0)
    session_context_lost: int = Field(ge=0)
    recovery_binding_errors: int = Field(ge=0)


class BenchmarkRunSummary(BaseModel):
    """保存单次基准 Run/Audit 的最小公开摘要，不包含隐藏推理或原始输出。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    comparison_group: Literal["current-prompt", "four-lens", "structured-v1"]
    controls: BenchmarkControls
    audit_refs: tuple[str, ...]
    quality: BenchmarkQuality
    cost: BenchmarkCost
    reliability: BenchmarkReliability
    terminal_status: str

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        """校验 Run ID；参数为文本，返回原值，含路径语义时抛 ValueError。"""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None:
            raise ValueError("基准 Run ID 必须是安全标识")
        return value

    @field_validator("audit_refs")
    @classmethod
    def validate_audit_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """校验审计引用；参数为相对路径元组，返回原值，越界时抛 ValueError。"""
        for reference in value:
            path = Path(reference)
            if not reference or path.is_absolute() or ".." in path.parts:
                raise ValueError("审计引用必须是安全相对路径")
        return value


class BenchmarkReport(BaseModel):
    """保存基准完整结果及各阶段继续门禁结论。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runs: tuple[BenchmarkRunSummary, ...]
    phase_decisions: dict[str, str]
    real_cli_decisions: dict[str, str]

    @field_validator("runs")
    @classmethod
    def validate_runs(
        cls, value: tuple[BenchmarkRunSummary, ...]
    ) -> tuple[BenchmarkRunSummary, ...]:
        """校验摘要集合；参数为运行元组，返回原值，空集/ID 重复时抛 ValueError。"""
        if not value:
            raise ValueError("基准报告至少需要一次运行")
        ids = [run.run_id for run in value]
        if len(ids) != len(set(ids)):
            raise ValueError("基准 Run ID 不能重复")
        return value


def write_run_summary(
    *, artifacts: ArtifactStore, summary: BenchmarkRunSummary, head_sha: str | None
) -> str:
    """写入并登记单次基准摘要。

    参数为当前 Run 的产物库、脱敏摘要和可选 HEAD；返回相对引用；路径越界、已有
    摘要冲突或写入失败时抛 ``BenchmarkError``/``ArtifactError``。
    """
    directory = artifacts.run_dir / "benchmark"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise BenchmarkError("benchmark 目录必须是当前 run 内普通目录")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{summary.run_id}.json"
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise BenchmarkError("基准摘要必须是当前 run 内普通文件")
    payload = summary.model_dump(mode="json")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != rendered:
            raise BenchmarkError("同一基准 Run ID 已对应不同摘要")
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(rendered, encoding="utf-8")
        os.replace(temporary, path)
    reference = path.relative_to(artifacts.run_dir).as_posix()
    artifacts.register_supervisor_artifact(
        artifact_ref=reference,
        producer="review_benchmark",
        head_sha=head_sha,
    )
    return reference


def render_final_benchmark_report(report: BenchmarkReport) -> str:
    """渲染最终基准 Markdown。

    参数为脱敏结构化报告；返回 Markdown；不读取 CLI 原始输出、不抛异常。
    """
    lines = [
        "# 对抗审查最终基准报告",
        "",
        "本报告只汇总冻结控制变量、Run/Audit 结构化摘要和可复核门禁结果；"
        "不保存隐藏推理、Session ID 或完整 CLI 原始输出。",
        "",
        "## 逐次固定基准结果",
        "",
        "| Run | 对照组 | Agent | 已知阻断 | 遗漏验收 | 无证据阻断 | 风格误报 | "
        "重复 | Ballot 推翻 | 输入/输出 Token | 秒/轮/调用 | 状态 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run in report.runs:
        quality = run.quality
        cost = run.cost
        lines.append(
            "| {run_id} | {group} | {agents} | {found}/{total} | {missing} | "
            "{unsupported} | {style} | {repeated} | {overturns} | {input}/{output} | "
            "{seconds:.2f}/{turns}/{calls} | {status} |".format(
                run_id=run.run_id,
                group=run.comparison_group,
                agents=" + ".join(agent.value for agent in run.controls.participants),
                found=quality.known_blockers_found,
                total=quality.known_blockers_total,
                missing=quality.missing_acceptance_found,
                unsupported=quality.unsupported_completion_blockers,
                style=quality.style_false_positives,
                repeated=quality.repeated_findings,
                overturns=quality.ballot_overturns,
                input=cost.input_tokens,
                output=cost.output_tokens,
                seconds=cost.elapsed_seconds,
                turns=cost.turns,
                calls=cost.total_cli_calls,
                status=run.terminal_status,
            )
        )
    lines.extend(["", "## 可靠性汇总", ""])
    for run in report.runs:
        reliability = run.reliability
        lines.append(
            f"- {run.run_id}: Coverage 缺失={reliability.coverage_missing}，"
            f"非法 Evidence 拒绝={reliability.invalid_evidence_rejected}，"
            f"Output 修复/降级={reliability.output_repaired_or_degraded}，"
            f"Session 上下文丢失={reliability.session_context_lost}，"
            f"恢复绑定错误={reliability.recovery_binding_errors}；"
            f"审计引用={', '.join(run.audit_refs) or '无'}。"
        )
    lines.extend(["", "## 阶段门禁", ""])
    for phase, decision in report.phase_decisions.items():
        lines.append(f"- {phase}: {decision}")
    lines.extend(["", "## 真实 CLI 验收", ""])
    for combination, decision in report.real_cli_decisions.items():
        lines.append(f"- {combination}: {decision}")
    return "\n".join(lines) + "\n"


def write_final_benchmark_report(*, path: Path, report: BenchmarkReport) -> None:
    """原子写最终 Markdown 报告。

    参数为目标路径和结构化报告；无返回值；目标父目录非法或文件写入失败时抛异常。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_final_benchmark_report(report)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
