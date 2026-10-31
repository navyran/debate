"""仓库内精简审计包与两阶段 final tree 确认。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from debate_agent.agent_catalog import agent_definition
from debate_agent.git_workspace import GitWorkspace, GitWorkspaceError
from debate_agent.goal_contract import GoalContract
from debate_agent.issue_ledger import IssueTransition, LedgerIssue
from debate_agent.models import (
    AgentName,
    Ballot,
    ChangeSummary,
    CompletionAssessment,
    Decision,
    FacilitatorRecord,
    TreeAck,
)
from debate_agent.paths import audit_relative_paths, select_audit_relative_path
from debate_agent.validation import ValidationReport


class FinalTreeError(RuntimeError):
    """表示审计 materialize、只读确认或最终候选绑定失败。"""


class DecisionRecord(BaseModel):
    """保存可审计的结构化决定，不包含隐藏推理。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: AgentName
    stage: str
    decision: Decision
    rationale: str
    commit_sha: str


class AgentAuditMetadata(BaseModel):
    """保存 CLI 的安全冻结身份摘要，不保存 Session 或运行配置原文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str | None = None
    command: str | None = None
    model: str | None = None
    variant: str | None = None
    native_profile: str | None = None
    provider: str | None = None
    experimental: bool = False
    prompt_transport: str | None = None
    session_generation: int | None = None
    preflight_status: str | None = None
    session_ref: str | None = None
    runtime_config_hash: str | None = None
    runtime_config_content_hash: str | None = None
    runtime_config_resolved_hash: str | None = None

    @field_validator(
        "runtime_config_hash",
        "runtime_config_content_hash",
        "runtime_config_resolved_hash",
    )
    @classmethod
    def validate_config_hash(cls, value: str | None) -> str | None:
        """校验配置摘要；参数为 SHA-256；返回原值，格式非法时抛 ValueError。"""
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("审计配置摘要必须是 SHA-256")
        return value

    @field_validator("session_ref")
    @classmethod
    def validate_session_ref(cls, value: str | None) -> str | None:
        """校验脱敏 Session 引用；参数为引用；返回原值，原始 ID 时抛 ValueError。"""
        if value is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError("审计 Session 引用必须是 sha256 摘要")
        return value


class AuditBundleInput(BaseModel):
    """限定审计生成器可读取的脱敏业务事实集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal_contract: GoalContract
    participants: tuple[AgentName, ...] = (
        AgentName.CODEX,
        AgentName.CLAUDE,
    )
    behavior_changes: tuple[str, ...]
    contributions: dict[AgentName, tuple[str, ...]]
    change_summaries: tuple[ChangeSummary, ...]
    decisions: tuple[DecisionRecord, ...]
    disputes: tuple[str, ...]
    limitations: tuple[str, ...]
    validation_reports: tuple[ValidationReport, ...]
    ballots: dict[AgentName, Ballot]
    facilitator_records: tuple[FacilitatorRecord, ...] = ()
    ledger_protocol_version: str | None = None
    ledger_issues: tuple[LedgerIssue, ...] = ()
    ledger_transitions: tuple[IssueTransition, ...] = ()
    agent_runtime_metadata: dict[AgentName, AgentAuditMetadata] = Field(
        default_factory=dict
    )


class AuditBundleWriter:
    """由 Supervisor 独占调用，机械生成六个仓库内审计文件。"""

    expected_files = frozenset(
        {
            "goal-contract.md",
            "final-summary.md",
            "change-summary.md",
            "decisions.jsonl",
            "facilitator-decisions.jsonl",
            "issue-ledger.json",
            "validation-report.md",
        }
    )

    def __init__(self, *, worktree: Path, run_id: str) -> None:
        """创建审计写入器。

        参数为 worktree 和 run ID；返回实例；路径或 run ID 非法时抛 ValueError。
        """
        root = worktree.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"worktree 不是目录: {root}")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None:
            raise ValueError("run_id 必须是安全路径标识")
        self.worktree = root
        self.run_id = run_id
        self.relative_path = select_audit_relative_path(root, run_id)
        self.audit_path = root / self.relative_path
        self.audit_path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sanitize(value: str, *, limit: int = 4000) -> str:
        """脱敏并限制审计文本。

        参数为文本和上限；返回不含本机路径/凭据/运行期字段的文本；不抛异常。
        """
        text = value
        text = re.sub(
            r"(?i)\b(token|password|secret|authorization|api[_-]?key|session[_-]?id)\s*[:=]\s*\S+",
            r"\1=[REDACTED]",
            text,
        )
        text = re.sub(
            r"(?i)\b(raw[_-]?output|hidden[_-]?reasoning|session[_-]?id)\b",
            "[REDACTED_FIELD]",
            text,
        )
        text = re.sub(
            r"(?<![\w.])/(?:[^\s/]+/)+[^\s,;:)}\]]+",
            "<redacted-path>",
            text,
        )
        text = re.sub(
            r"\b[A-Za-z]:\\(?:[^\s\\]+\\)+[^\s,;:)}\]]+",
            "<redacted-path>",
            text,
        )
        text = re.sub(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
            r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b",
            "[REDACTED_ID]",
            text,
        )
        return text[:limit]

    @classmethod
    def _items(cls, values: tuple[str, ...]) -> str:
        """渲染 Markdown 列表；参数为文本元组，返回脱敏列表，不抛异常。"""
        if not values:
            return "- 无"
        return "\n".join(f"- {cls._sanitize(value)}" for value in values)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """原子写审计文件；参数为路径和内容，无返回值，文件失败时抛 OSError。"""
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def _goal_contract_markdown(self, data: AuditBundleInput) -> str:
        """渲染契约；参数为审计输入，返回 Markdown，不抛异常。"""
        contract = data.goal_contract
        commands = tuple(
            " ".join(command) for command in contract.all_validation_commands
        )
        return "\n".join(
            [
                f"# GoalContract v{contract.version}",
                "",
                "## 目标",
                self._sanitize(contract.objective),
                "",
                "## 交付物",
                self._items(contract.deliverables),
                "",
                "## 验收标准",
                self._items(contract.acceptance_criteria),
                "",
                "## 有效范围",
                self._items(contract.effective_scope),
                "",
                "## 权威验证",
                self._items(commands),
                "",
                "## 不可弱化约束",
                self._items(contract.immutable_constraints),
            ]
        )

    def _final_summary_markdown(
        self,
        data: AuditBundleInput,
        *,
        status: str = "completed",
        stop_reason: str | None = None,
    ) -> str:
        """渲染最终/终止摘要；参数为输入、状态和原因，返回 Markdown。"""
        lines = [
            "# Debate Agent 结果摘要",
            "",
            f"- Run: {self.run_id}",
            f"- Status: {self._sanitize(status)}",
        ]
        if status == "ready-for-final-tree-ack":
            lines.extend(
                [
                    "- Final-Tree-Ack: 本文件属于待确认的固定 tree；参与者确认结果"
                    "只写入最终提交的 Final-Tree-Ack trailer，避免反向修改已确认"
                    " tree。",
                    "- Delivery: 只有该 tree 获得双 Ack、成功集成并校验后，运行状态"
                    "才会成为 completed。",
                ]
            )
        if stop_reason:
            lines.append(f"- Stop reason: {self._sanitize(stop_reason)}")
        lines.extend(["", "## 最终行为", self._items(data.behavior_changes)])
        for agent in data.participants:
            display = agent_definition(agent).display_name
            metadata = data.agent_runtime_metadata.get(agent)
            lines.extend(
                [
                    "",
                    f"## {display} 贡献（检查点）",
                    self._items(data.contributions.get(agent, ())),
                ]
            )
            if metadata is not None:
                lines.extend(
                    [
                        "- CLI 冻结摘要: "
                        + "; ".join(
                            part
                            for part in (
                                (
                                    f"version={self._sanitize(metadata.version)}"
                                    if metadata.version
                                    else None
                                ),
                                (
                                    f"command={self._sanitize(metadata.command)}"
                                    if metadata.command
                                    else None
                                ),
                                (
                                    f"model={self._sanitize(metadata.model)}"
                                    if metadata.model
                                    else None
                                ),
                                (
                                    f"variant={self._sanitize(metadata.variant)}"
                                    if metadata.variant
                                    else None
                                ),
                                (
                                    "native-profile="
                                    f"{self._sanitize(metadata.native_profile)}"
                                    if metadata.native_profile
                                    else None
                                ),
                                (
                                    f"provider={self._sanitize(metadata.provider)}"
                                    if metadata.provider
                                    else None
                                ),
                                (
                                    "experimental=true"
                                    if metadata.experimental
                                    else None
                                ),
                                (
                                    "prompt-transport="
                                    f"{self._sanitize(metadata.prompt_transport)}"
                                    if metadata.prompt_transport
                                    else None
                                ),
                                (
                                    "session-generation="
                                    f"{metadata.session_generation}"
                                    if metadata.session_generation is not None
                                    else None
                                ),
                                (
                                    "preflight="
                                    f"{self._sanitize(metadata.preflight_status)}"
                                    if metadata.preflight_status
                                    else None
                                ),
                                (
                                    f"session-ref={metadata.session_ref}"
                                    if metadata.session_ref
                                    else None
                                ),
                                (
                                    f"runtime-config={metadata.runtime_config_hash}"
                                    if metadata.runtime_config_hash
                                    else None
                                ),
                                (
                                    "runtime-config-content="
                                    f"{metadata.runtime_config_content_hash}"
                                    if metadata.runtime_config_content_hash
                                    else None
                                ),
                                (
                                    "runtime-config-resolved="
                                    f"{metadata.runtime_config_resolved_hash}"
                                    if metadata.runtime_config_resolved_hash
                                    else None
                                ),
                            )
                            if part is not None
                        )
                    ]
                )
        lines.extend(
            [
                "",
                "## 关键争议（历史记录）",
                self._items(data.disputes),
                "",
                "## 已知限制",
                self._items(data.limitations),
                "",
                "## 所有参与者终审",
            ]
        )
        for agent in data.participants:
            ballot = data.ballots.get(agent)
            if ballot is not None:
                lines.append(
                    f"- {agent.value}: {ballot.completion.value}; "
                    f"{self._sanitize(ballot.rationale)}"
                )
                if ballot.strongest_counterexample is not None:
                    lines.extend(
                        (
                            "  - 最强反例: "
                            f"{self._sanitize(ballot.strongest_counterexample)}",
                            "  - 处理与证据: "
                            f"{self._sanitize(ballot.counterexample_resolution or '')}",
                        )
                    )
        return "\n".join(lines)

    def _change_summary_markdown(self, data: AuditBundleInput) -> str:
        """渲染检查点摘要；参数为审计输入，返回 Markdown，不包含完整 diff。"""
        lines = ["# 变更摘要"]
        for summary in data.change_summaries:
            lines.extend(
                [
                    "",
                    f"## {summary.turn_id} · {summary.source_agent.value}",
                    f"- Commit: {summary.commit_sha}",
                    f"- Paths: {', '.join(summary.changed_paths) or '无'}",
                    f"- Validation: {self._sanitize(summary.validation_status)}",
                    self._items(tuple(summary.behavior_summary)),
                ]
            )
        return "\n".join(lines)

    def _decisions_jsonl(self, data: AuditBundleInput) -> str:
        """渲染结构化决定；参数为审计输入，返回脱敏 JSONL。"""
        rows = []
        for decision in data.decisions:
            payload = decision.model_dump(mode="json")
            payload["rationale"] = self._sanitize(decision.rationale)
            rows.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return "\n".join(rows)

    def _facilitator_decisions_jsonl(self, data: AuditBundleInput) -> str:
        """渲染精简语义决定；参数为审计输入，返回脱敏 JSONL。"""
        rows = []
        for record in data.facilitator_records:
            payload: dict[str, object] = {
                "decision_id": record.decision_id,
                "purpose": record.purpose.value,
                "head_sha": record.head_sha,
                "goal_contract_version": record.goal_contract_version,
                "mode": record.mode.value,
                "status": record.status.value,
                "error_category": (
                    self._sanitize(record.error_category)
                    if record.error_category
                    else None
                ),
                "deterministic_route": (
                    self._sanitize(record.deterministic_route)
                    if record.deterministic_route
                    else None
                ),
            }
            if record.decision is not None:
                payload.update(
                    {
                        "action": record.decision.action.value,
                        "recommended_next_writer": (
                            record.decision.recommended_next_writer.value
                            if record.decision.recommended_next_writer
                            else None
                        ),
                        "unresolved_criteria": [
                            self._sanitize(item)
                            for item in record.decision.unresolved_criteria
                        ],
                        "risk_flags": [
                            self._sanitize(item) for item in record.decision.risk_flags
                        ],
                        "evidence_refs": [
                            self._sanitize(item)
                            for item in record.decision.evidence_refs
                        ],
                    }
                )
            rows.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return "\n".join(rows)

    def _validation_markdown(self, data: AuditBundleInput) -> str:
        """渲染权威验证；参数为审计输入，返回脱敏 Markdown，省略 raw 引用。"""
        lines = ["# 权威验证报告"]
        for report in data.validation_reports:
            lines.extend(
                [
                    "",
                    f"## {report.phase.value} · {report.report_id}",
                    f"- Passed: {str(report.passed).lower()}",
                ]
            )
            for command in report.commands:
                argv = " ".join(self._sanitize(part) for part in command.argv)
                lines.extend(
                    [
                        f"- Command: `{argv}`",
                        f"  - Exit: {command.return_code}",
                        f"  - Elapsed: {command.elapsed_seconds:.3f}s",
                        (
                            f"  - Stdout: {self._sanitize(command.stdout_summary)}"
                        ).rstrip(),
                        (
                            f"  - Stderr: {self._sanitize(command.stderr_summary)}"
                        ).rstrip(),
                    ]
                )
        return "\n".join(lines)

    def _issue_ledger_json(self, data: AuditBundleInput) -> str:
        """渲染 Ledger 生命周期投影。

        参数为审计输入；返回脱敏 JSON；账本未启用时返回空 Issue 列表，不补造任何
        状态或证据。
        """
        payload: dict[str, object] = {
            "issues": [issue.model_dump(mode="json") for issue in data.ledger_issues],
            "transitions": [
                transition.model_dump(mode="json")
                for transition in data.ledger_transitions
            ],
        }
        if data.ledger_protocol_version is not None:
            payload["protocol_version"] = data.ledger_protocol_version
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    def _write(
        self,
        data: AuditBundleInput,
        *,
        status: str,
        stop_reason: str | None,
    ) -> Path:
        """写完整审计包；参数为输入/状态/原因，返回目录，文件失败时抛错。"""
        documents = {
            "goal-contract.md": self._goal_contract_markdown(data),
            "final-summary.md": self._final_summary_markdown(
                data, status=status, stop_reason=stop_reason
            ),
            "change-summary.md": self._change_summary_markdown(data),
            "decisions.jsonl": self._decisions_jsonl(data),
            "facilitator-decisions.jsonl": self._facilitator_decisions_jsonl(data),
            "issue-ledger.json": self._issue_ledger_json(data),
            "validation-report.md": self._validation_markdown(data),
        }
        for name, content in documents.items():
            self._atomic_write(self.audit_path / name, content)
        return self.audit_path

    def write_completed(self, data: AuditBundleInput) -> Path:
        """写完成候选审计；参数为审计输入，返回目录，文件失败时抛错。"""
        return self._write(
            data,
            status="ready-for-final-tree-ack",
            stop_reason=None,
        )

    def write_non_success(
        self, data: AuditBundleInput, *, status: str, stop_reason: str
    ) -> Path:
        """写非完成审计；参数为输入/状态/原因，返回目录，不读取违规文件内容。"""
        if status == "completed":
            raise ValueError("非完成审计不能使用 completed 状态")
        return self._write(data, status=status, stop_reason=stop_reason)


@dataclass(frozen=True, slots=True)
class FinalTreeCandidate:
    """绑定代码 HEAD、审计路径、index tree 与工作树指纹。"""

    code_commit: str
    final_tree_sha: str
    workspace_fingerprint: str
    audit_path: str


class FinalTreeCoordinator:
    """协调审计 materialize、双 tree ack 和聚焦提交创建。"""

    def __init__(self, workspace: GitWorkspace) -> None:
        """创建协调器；参数为持锁 GitWorkspace，返回实例，不产生 I/O。"""
        self.workspace = workspace

    def materialize_final_tree(self, audit_path: str) -> FinalTreeCandidate:
        """物化 final tree。

        参数为当前 run 审计路径；返回固定候选；混入产品变更或 Git 失败时抛错。
        """
        expected_path = select_audit_relative_path(
            self.workspace.worktree_path, self.workspace.run_id
        )
        if audit_path != expected_path:
            raise FinalTreeError("final tree 审计路径不属于当前 run")
        audit_root = self.workspace.worktree_path / audit_path
        actual_files = (
            {path.name for path in audit_root.iterdir() if path.is_file()}
            if audit_root.is_dir()
            else set()
        )
        if actual_files != AuditBundleWriter.expected_files:
            raise FinalTreeError("审计目录文件集合不完整或包含额外文件")
        try:
            final_tree = self.workspace.stage_audit_tree(
                audit_path, require_only_audit=True
            )
        except GitWorkspaceError as exc:
            raise FinalTreeError(str(exc)) from exc
        snapshot = self.workspace.snapshot()
        return FinalTreeCandidate(
            code_commit=snapshot.head,
            final_tree_sha=final_tree,
            workspace_fingerprint=snapshot.fingerprint,
            audit_path=audit_path,
        )

    def collect_tree_acks(
        self,
        candidate: FinalTreeCandidate,
        *,
        collector: Callable[[AgentName, FinalTreeCandidate], TreeAck],
        participants: tuple[AgentName, ...] = (
            AgentName.CODEX,
            AgentName.CLAUDE,
        ),
    ) -> dict[AgentName, TreeAck]:
        """串行收集只读 tree ack。

        参数为固定候选和收集器；返回双方 Ack；写入或 tree/身份不符时抛错。
        """
        acks: dict[AgentName, TreeAck] = {}
        for agent in participants:
            if self.workspace.snapshot().fingerprint != candidate.workspace_fingerprint:
                raise FinalTreeError("tree ack 前只读候选已变化")
            ack = collector(agent, candidate)
            if self.workspace.snapshot().fingerprint != candidate.workspace_fingerprint:
                raise FinalTreeError("tree ack 只读阶段检测到写入")
            if ack.agent is not agent or ack.final_tree_sha != candidate.final_tree_sha:
                raise FinalTreeError("tree ack 未绑定固定 final tree")
            acks[agent] = ack
        return acks

    def create_integration_commit(
        self,
        candidate: FinalTreeCandidate,
        *,
        tree_acks: Mapping[AgentName, TreeAck],
        ballots: Mapping[AgentName, Ballot],
        subject: str,
        goal_contract_version: int,
        validation_status: str,
        participants: tuple[AgentName, ...] = (
            AgentName.CODEX,
            AgentName.CLAUDE,
        ),
    ) -> str:
        """创建聚焦集成提交。

        参数为候选、双 Ack/ballot、主题、契约和验证；返回 SHA；任一门禁失败时抛错。
        """
        if self.workspace.snapshot().fingerprint != candidate.workspace_fingerprint:
            raise FinalTreeError("final tree 确认后工作树发生变化")
        if self.workspace.head() != candidate.code_commit:
            raise FinalTreeError("代码 HEAD 与已确认候选不一致")
        if candidate.audit_path not in audit_relative_paths(self.workspace.run_id):
            raise FinalTreeError("已确认审计路径不属于当前 run")
        if self.workspace.index_tree() != candidate.final_tree_sha:
            raise FinalTreeError("当前 index tree 与已确认 final tree 不一致")
        required_agents = set(participants)
        if set(tree_acks) != required_agents or any(
            not ack.accepted
            or ack.agent is not agent
            or ack.final_tree_sha != candidate.final_tree_sha
            for agent, ack in tree_acks.items()
        ):
            raise FinalTreeError("双方尚未确认；所有参与者必须确认同一 final tree")
        if set(ballots) != required_agents or any(
            ballot.agent is not agent
            or ballot.commit_sha != candidate.code_commit
            or ballot.goal_contract_version != goal_contract_version
            or ballot.completion is not CompletionAssessment.COMPLETE
            for agent, ballot in ballots.items()
        ):
            raise FinalTreeError("所有参与者尚未对同一代码候选判断 complete")
        if validation_status != "passed":
            raise FinalTreeError("最终权威验证未通过")
        headline = (
            subject.strip().splitlines()[0] if subject.strip() else "debated result"
        )
        agent_names = ", ".join(agent.value for agent in participants)
        message = (
            f"{headline}\n\n"
            f"Debate-Run: {self.workspace.run_id}\n"
            f"Debate-Agents: {agent_names}\n"
            f"Goal-Contract: v{goal_contract_version}\n"
            f"Final-Tree: {candidate.final_tree_sha}\n"
            f"Final-Tree-Ack: {agent_names}\n"
            f"Validation-Status: {validation_status}\n"
            f"Audit-Path: {candidate.audit_path}"
        )
        return self.workspace.create_integration_commit(
            candidate.final_tree_sha, message
        )
