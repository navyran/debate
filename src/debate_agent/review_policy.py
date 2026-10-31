"""Reviewer 机器证据的来源、路径和提交绑定校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from debate_agent.artifacts import ArtifactError, ArtifactStore
from debate_agent.git_workspace import GitWorkspace
from debate_agent.review_models import EvidenceKind, EvidenceRef, ReviewResult
from debate_agent.validation import ValidationReport


class ReviewPolicyError(ValueError):
    """表示 EvidenceRef 的来源、路径或 HEAD 绑定不满足审查协议。"""


@dataclass(frozen=True, slots=True)
class ResolvedEvidence:
    """保存已通过机器校验的 EvidenceRef 和其权威来源。

    字段为原始 EvidenceRef 与在仓库或运行目录中的相对来源；该对象不携带、
    不推断任何业务结论。
    """

    evidence: EvidenceRef
    source_ref: str


class EvidenceResolver:
    """以当前 Git HEAD 和 Supervisor 运行产物解析机器证据。"""

    def __init__(self, *, workspace: GitWorkspace, artifacts: ArtifactStore) -> None:
        """创建证据解析器。

        参数为当前共享 GitWorkspace 和同一 run 的 ArtifactStore；返回实例；运行
        目录与工作区 run 身份不一致或不可访问时抛 ``ReviewPolicyError``。
        """
        expected_run_dir = workspace.state_root / "runs" / workspace.run_id
        resolved_run_dir = expected_run_dir.resolve()
        if (
            artifacts.run_id != workspace.run_id
            or artifacts.run_dir != resolved_run_dir
        ):
            raise ReviewPolicyError(
                "EvidenceResolver 的 ArtifactStore 不属于当前工作区 run"
            )
        if not resolved_run_dir.is_dir():
            raise ReviewPolicyError("运行产物目录不存在")
        self.workspace = workspace
        self.artifacts = artifacts
        self.run_dir = resolved_run_dir

    @staticmethod
    def _safe_relative_ref(reference: str | None, *, field: str) -> PurePosixPath:
        """校验仓库或运行目录相对引用。

        参数为待校验引用和字段名；返回规范 POSIX 相对路径；绝对路径、空段或包含
        ``..`` 的路径抛 ``ReviewPolicyError``。
        """
        if not reference:
            raise ReviewPolicyError(f"{field} 不能为空")
        path = PurePosixPath(reference)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ReviewPolicyError(f"{field} 必须是安全相对路径")
        return path

    def _artifact_path(self, artifact_ref: str) -> Path:
        """解析并校验运行产物实际路径。

        参数为运行目录相对引用；返回存在的普通文件路径；越界、软链接、目录或不
        存在产物抛 ``ReviewPolicyError``。
        """
        relative = self._safe_relative_ref(artifact_ref, field="artifact_ref")
        candidate = self.run_dir / relative.as_posix()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ReviewPolicyError("运行产物不存在") from exc
        if not resolved.is_relative_to(self.run_dir):
            raise ReviewPolicyError("运行产物引用越出当前 run 目录")
        if candidate.is_symlink() or not resolved.is_file():
            raise ReviewPolicyError("运行产物必须是当前 run 内的普通文件")
        return resolved

    def _current_head(self, evidence: EvidenceRef) -> str:
        """核对证据与当前共享工作区 HEAD 的绑定。

        参数为证据引用；返回当前 HEAD；引用 HEAD 与工作区不一致时抛
        ``ReviewPolicyError``。
        """
        current_head = self.workspace.head()
        if evidence.head_sha != current_head:
            raise ReviewPolicyError("EvidenceRef 的 head_sha 不是当前 HEAD")
        return current_head

    def _load_validation_report(self, artifact_ref: str) -> ValidationReport:
        """读取 Supervisor 验证报告并校验其文件身份。

        参数为报告引用；返回结构化 ValidationReport；非 ``validation/`` 路径、
        损坏内容或声明引用不一致时抛 ``ReviewPolicyError``。
        """
        relative = self._safe_relative_ref(artifact_ref, field="artifact_ref")
        if not relative.parts or relative.parts[0] != "validation":
            raise ReviewPolicyError("validation_report 必须引用 validation/ 产物")
        artifact_path = self._artifact_path(relative.as_posix())
        try:
            report = ValidationReport.model_validate_json(
                artifact_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, json.JSONDecodeError) as exc:
            raise ReviewPolicyError(
                "validation_report 不是有效的 Supervisor 报告"
            ) from exc
        if report.report_ref != relative.as_posix() or not report.authoritative:
            raise ReviewPolicyError("validation_report 不是权威 Supervisor 产物")
        return report

    def _resolve_validation_report(self, evidence: EvidenceRef) -> ResolvedEvidence:
        """解析完整的 Supervisor 验证报告证据。

        参数为 ``validation_report`` 类型证据；返回已解析来源；报告没有同 HEAD
        绑定时抛 ``ReviewPolicyError``。
        """
        assert evidence.artifact_ref is not None
        current_head = self._current_head(evidence)
        report = self._load_validation_report(evidence.artifact_ref)
        if report.head_sha != current_head:
            raise ReviewPolicyError("validation_report 已过期或未绑定当前 HEAD")
        try:
            self.artifacts.verify_supervisor_artifact(
                artifact_ref=report.report_ref,
                producer="validation_runner",
                head_sha=current_head,
            )
        except ArtifactError as exc:
            raise ReviewPolicyError(
                "validation_report 缺少可信 Supervisor 来源"
            ) from exc
        return ResolvedEvidence(evidence=evidence, source_ref=report.report_ref)

    def _resolve_test_result(self, evidence: EvidenceRef) -> ResolvedEvidence:
        """解析由权威验证报告索引的单条原始测试结果。

        参数为 ``test_result`` 类型证据；返回已解析来源；产物不是报告中登记的原始
        输出、报告不权威或 HEAD 过期时抛 ``ReviewPolicyError``。
        """
        assert evidence.artifact_ref is not None
        current_head = self._current_head(evidence)
        relative = self._safe_relative_ref(evidence.artifact_ref, field="artifact_ref")
        self._artifact_path(relative.as_posix())
        try:
            self.artifacts.verify_supervisor_artifact(
                artifact_ref=relative.as_posix(),
                producer="validation_runner",
                head_sha=current_head,
            )
        except ArtifactError as exc:
            raise ReviewPolicyError("test_result 缺少可信 Supervisor 来源") from exc
        reports_dir = self.run_dir / "validation"
        if not reports_dir.is_dir():
            raise ReviewPolicyError("未找到 Supervisor 验证报告目录")
        for report_path in sorted(reports_dir.glob("*.json")):
            if report_path.is_symlink() or not report_path.is_file():
                continue
            report_ref = report_path.relative_to(self.run_dir).as_posix()
            try:
                report = self._load_validation_report(report_ref)
            except ReviewPolicyError:
                continue
            if report.head_sha != current_head:
                continue
            if any(
                command.raw_output_ref == relative.as_posix()
                for command in report.commands
            ):
                return ResolvedEvidence(evidence=evidence, source_ref=report_ref)
        raise ReviewPolicyError("test_result 未由当前 HEAD 的 Supervisor 报告生成")

    def resolve(self, evidence: EvidenceRef) -> ResolvedEvidence:
        """解析一条 EvidenceRef 的机器权威来源。

        参数为结构已校验的 EvidenceRef；返回已解析证据；路径越界、Git 路径不存在、
        当前 HEAD 不匹配、非 Supervisor 产物或过期报告时抛 ``ReviewPolicyError``。
        本方法不判断证据是否足以证明任何业务 Claim。
        """
        self._current_head(evidence)
        if evidence.kind is EvidenceKind.GIT_PATH:
            relative = self._safe_relative_ref(evidence.path, field="path")
            if not self.workspace.path_exists_at_head(relative.as_posix()):
                raise ReviewPolicyError("git_path 不存在于当前 HEAD")
            return ResolvedEvidence(evidence=evidence, source_ref=relative.as_posix())
        if evidence.kind is EvidenceKind.GIT_DIFF:
            relative = self._safe_relative_ref(evidence.path, field="path")
            if not self.workspace.path_changed_at_head(relative.as_posix()):
                raise ReviewPolicyError(
                    "git_diff 不存在于 Run 基线到当前 HEAD 的累计差异"
                )
            return ResolvedEvidence(evidence=evidence, source_ref=relative.as_posix())
        if evidence.kind is EvidenceKind.VALIDATION_REPORT:
            return self._resolve_validation_report(evidence)
        if evidence.kind is EvidenceKind.TEST_RESULT:
            return self._resolve_test_result(evidence)
        raise ReviewPolicyError(f"不支持的 EvidenceRef 类型: {evidence.kind}")


class ReviewPolicy:
    """批量校验一个 Reviewer 结果中的机器证据，不裁决业务正确性。"""

    def __init__(self, *, resolver: EvidenceResolver) -> None:
        """创建 ReviewPolicy；参数为证据解析器，返回实例，不抛异常。"""
        self.resolver = resolver

    def validate_review(self, review: ReviewResult) -> tuple[ResolvedEvidence, ...]:
        """校验 ReviewResult 的 HEAD 与全部 Coverage/Finding/处置证据。

        参数为已经过模型结构校验的 ReviewResult；返回按输入顺序解析的证据元组；
        HEAD 不一致或任一证据不合法时抛 ``ReviewPolicyError``。该方法不判断
        Finding 的业务语义、影响或修复方案。
        """
        current_head = self.resolver.workspace.head()
        if review.head_sha != current_head:
            raise ReviewPolicyError("ReviewResult 的 head_sha 不是当前 HEAD")
        references = [
            reference
            for coverage in review.coverage
            for reference in coverage.evidence_refs
        ]
        references.extend(
            reference
            for finding in review.findings
            for reference in finding.evidence_refs
        )
        references.extend(
            reference
            for disposition in review.issue_dispositions
            for reference in disposition.evidence_refs
        )
        return tuple(self.resolver.resolve(reference) for reference in references)
