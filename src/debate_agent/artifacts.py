"""运行期目录、原子状态文件和幂等 invocation journal。"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from debate_agent.config import RunConfig
from debate_agent.models import AgentName, AgentResult, RunStatus
from debate_agent.review_models import ReviewResult


class ArtifactError(RuntimeError):
    """表示运行目录身份、journal 状态或持久化内容无效。"""


class ArtifactStore:
    """管理一个 run 的可恢复状态，不作为最终长期审计来源。"""

    directory_names = (
        "goal-contract",
        "updates",
        "acks",
        "leases",
        "invocations",
        "facilitator",
        "reviews",
        "issue-ledger",
        "validation",
        "raw",
        "supervisor-artifacts",
    )

    def __init__(self, *, state_root: Path, run_id: str) -> None:
        """打开运行目录；参数为状态根/run ID，返回实例，ID 非法时抛错。"""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None:
            raise ValueError("run_id 必须是安全路径标识")
        self.state_root = state_root.expanduser().resolve()
        self.run_id = run_id
        self.run_dir = self.state_root / "runs" / run_id
        self.config_path = self.run_dir / "config.json"
        self.status_path = self.run_dir / "status.json"
        self.state_path = self.run_dir / "state.json"
        self.repo_path = self.run_dir / "repo-lock.json"
        self.invocations_dir = self.run_dir / "invocations"
        self.reviews_dir = self.run_dir / "reviews"
        self.issue_ledger_path = self.run_dir / "issue-ledger" / "ledger.json"

    @staticmethod
    def _now() -> str:
        """生成 UTC 时间；无参数，返回 ISO 字符串，不抛异常。"""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _atomic_json(path: Path, payload: object) -> None:
        """原子写 JSON；参数为路径和载荷，无返回值，序列化/文件错误时抛异常。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    @classmethod
    def create(cls, config: RunConfig, *, run_id: str) -> ArtifactStore:
        """创建或幂等打开 run。

        参数为冻结配置和 run ID；返回 Store；已有不同配置时抛 ArtifactError。
        """
        store = cls(state_root=config.state_root, run_id=run_id)
        store.run_dir.mkdir(parents=True, exist_ok=True)
        for name in cls.directory_names:
            (store.run_dir / name).mkdir(parents=True, exist_ok=True)
        serialized = config.model_dump(mode="json")
        if store.config_path.exists():
            existing = json.loads(store.config_path.read_text(encoding="utf-8"))
            canonical_existing = RunConfig.model_validate(existing).model_dump(
                mode="json"
            )
            if canonical_existing != serialized:
                raise ArtifactError("已有 run ID 对应不同配置")
            if existing != canonical_existing:
                cls._atomic_json(store.config_path, canonical_existing)
        else:
            created_at = cls._now()
            cls._atomic_json(store.config_path, serialized)
            cls._atomic_json(store.state_path, {})
            cls._atomic_json(store.repo_path, {})
            cls._atomic_json(
                store.status_path,
                {
                    "run_id": run_id,
                    "status": RunStatus.RUNNING.value,
                    "stop_reason": None,
                    "created_at": created_at,
                    "updated_at": created_at,
                },
            )
        return store

    @classmethod
    def open(cls, *, state_root: Path, run_id: str) -> ArtifactStore:
        """打开已有 run；参数为状态根/run ID，返回 Store，不存在时抛 ArtifactError。"""
        store = cls(state_root=state_root, run_id=run_id)
        if not store.config_path.is_file():
            raise ArtifactError(f"运行不存在: {run_id}")
        return store

    def load_config(self) -> RunConfig:
        """读取冻结配置；无参数，返回 RunConfig，JSON/模型错误时传播异常。"""
        return RunConfig.model_validate_json(
            self.config_path.read_text(encoding="utf-8")
        )

    def replace_config_before_execution(
        self,
        *,
        expected: RunConfig,
        replacement: RunConfig,
    ) -> None:
        """在仓库副作用前替换健康预检冻结配置。

        参数为原配置和替换配置；无返回值；配置不匹配或已有 repo/invocation 副作用
        时抛 ArtifactError。
        """
        if self.load_config() != expected:
            raise ArtifactError("健康预检前的 RunConfig 已发生变化")
        if replacement.state_root != expected.state_root:
            raise ArtifactError("健康预检不得改变 state_root")
        if replacement.workspace != expected.workspace:
            raise ArtifactError("健康预检不得改变 workspace")
        repo_record = self.read_repo()
        invocations = (
            list(self.invocations_dir.glob("*.json"))
            if self.invocations_dir.exists()
            else []
        )
        if repo_record or invocations:
            raise ArtifactError("已有仓库或调用副作用，不能替换冻结配置")
        self._atomic_json(self.config_path, replacement.model_dump(mode="json"))

    def read_status(self) -> dict[str, Any]:
        """读取状态；无参数，返回 JSON 字典，损坏时抛 ArtifactError。"""
        payload = json.loads(self.status_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ArtifactError("status.json 顶层必须是对象")
        return payload

    def write_status(self, status: RunStatus | str, *, stop_reason: str | None) -> None:
        """更新状态；参数为状态和原因，无返回值，非 completed 缺原因时抛错。"""
        value = status.value if isinstance(status, RunStatus) else status
        if (
            value != RunStatus.COMPLETED.value
            and value != RunStatus.RUNNING.value
            and not stop_reason
        ):
            raise ValueError("非完成终止状态必须包含 stop_reason")
        previous = self.read_status() if self.status_path.exists() else {}
        self._atomic_json(
            self.status_path,
            {
                "run_id": self.run_id,
                "status": value,
                "stop_reason": stop_reason,
                "created_at": previous.get("created_at", self._now()),
                "updated_at": self._now(),
            },
        )

    def write_state(self, state: object) -> None:
        """保存小型图状态；参数为 JSON 可序列化对象，无返回值，失败时抛错。"""
        self._atomic_json(self.state_path, state)

    def write_diagnostic(self, *, phase: str, error: str) -> None:
        """保存脱敏诊断；参数为阶段/错误，无返回值，文件错误时抛异常。"""
        self._atomic_json(
            self.run_dir / "diagnostic.json",
            {
                "phase": phase,
                "error": error[:500],
                "updated_at": self._now(),
            },
        )

    def read_state(self) -> dict[str, Any]:
        """读取图状态；无参数，返回字典，损坏时传播异常。"""
        payload: dict[str, Any] = json.loads(
            self.state_path.read_text(encoding="utf-8")
        )
        return payload

    def write_repo(self, payload: object) -> None:
        """保存 Git 恢复记录；参数为记录，无返回值，序列化失败时抛错。"""
        self._atomic_json(self.repo_path, payload)

    def read_repo(self) -> dict[str, Any]:
        """读取 Git 恢复记录；无参数，返回字典，损坏时传播异常。"""
        payload: dict[str, Any] = json.loads(self.repo_path.read_text(encoding="utf-8"))
        return payload

    def _invocation_path(self, invocation_id: str) -> Path:
        """解析 journal 路径；参数为 invocation ID，返回路径，非法 ID 时抛错。"""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", invocation_id) is None:
            raise ValueError("invocation_id 必须是安全路径标识")
        return self.invocations_dir / f"{invocation_id}.json"

    def _facilitator_path(self, decision_id: str) -> Path:
        """解析 Facilitator 记录路径；参数为决定 ID，返回路径，非法 ID 时抛错。"""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", decision_id) is None:
            raise ValueError("decision_id 必须是安全路径标识")
        return self.run_dir / "facilitator" / f"{decision_id}.json"

    def write_facilitator_record(self, decision_id: str, payload: object) -> str:
        """原子保存 Facilitator 记录；参数为 ID/载荷，返回相对引用，失败时抛错。"""
        path = self._facilitator_path(decision_id)
        self._atomic_json(path, payload)
        return path.relative_to(self.run_dir).as_posix()

    def read_facilitator_record(self, decision_id: str) -> dict[str, Any] | None:
        """读取 Facilitator 记录；参数为 ID，返回字典或 None，损坏时传播异常。"""
        path = self._facilitator_path(decision_id)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ArtifactError("Facilitator 记录顶层必须是对象")
        return payload

    def read_issue_ledger_payload(self) -> dict[str, Any] | None:
        """读取唯一 Issue Ledger 原始载荷。

        无参数；返回 JSON 对象或 ``None``；文件损坏、符号链接或顶层类型错误时抛
        ``ArtifactError``。该方法不解释 Issue 的业务状态。
        """
        path = self.issue_ledger_path
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ArtifactError("Issue Ledger 必须是当前 run 内的普通文件")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ArtifactError("Issue Ledger 无法安全解析") from exc
        if not resolved.is_relative_to(self.run_dir.resolve()):
            raise ArtifactError("Issue Ledger 越出当前 run")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError("Issue Ledger 已损坏") from exc
        if not isinstance(payload, dict):
            raise ArtifactError("Issue Ledger 顶层必须是对象")
        return payload

    def write_issue_ledger_payload(self, payload: dict[str, Any]) -> str:
        """原子写入唯一 Issue Ledger 原始载荷。

        参数为 Supervisor 已校验的 JSON 对象；返回 run 相对引用；既有文件为符号
        链接、目录或写入失败时抛 ``ArtifactError``。本方法不执行状态转换。
        """
        path = self.issue_ledger_path
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ArtifactError("Issue Ledger 必须是当前 run 内的普通文件")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise ArtifactError("Issue Ledger 目录不能是符号链接")
        self._atomic_json(path, payload)
        return path.relative_to(self.run_dir).as_posix()

    @staticmethod
    def _safe_review_identity_part(value: str, *, field: str) -> str:
        """校验 Review 文件身份片段。

        参数为身份字符串和字段名；返回原字符串；值为空或含路径语义字符时抛
        ``ArtifactError``。该校验只保护本 run 的文件布局，不校验 Git 提交是否
        存在。
        """
        pattern = (
            r"[0-9A-Za-z]+"
            if field == "head_sha"
            else r"[A-Za-z0-9][A-Za-z0-9._-]*"
        )
        if re.fullmatch(pattern, value) is None:
            raise ArtifactError(f"Review {field} 必须是安全路径标识")
        return value

    def _review_path(
        self,
        *,
        head_sha: str,
        reviewer: AgentName,
        turn_id: str,
    ) -> Path:
        """计算唯一 Review 持久化路径。

        参数为 HEAD、Reviewer 与 turn ID；返回当前 run 下的文件路径；任一身份
        片段非法时抛 ``ArtifactError``。同一三元组始终映射到同一文件。
        """
        safe_head = self._safe_review_identity_part(head_sha, field="head_sha")
        safe_turn = self._safe_review_identity_part(turn_id, field="turn_id")
        return self.reviews_dir / f"{safe_head}.{reviewer.value}.{safe_turn}.json"

    def _secure_review_path(self, path: Path, *, require_exists: bool) -> Path:
        """验证 Review 路径未经符号链接越出当前 run。

        参数为 reviews 目录内的候选路径和存在性要求；返回原路径；reviews 父目录、
        最终文件或解析后的外部目标含符号链接、越出当前 run、不是普通目录/文件或
        目标缺失时抛 ``ArtifactError``。写入路径也会先验证其父目录。
        """
        if path.parent != self.reviews_dir:
            raise ArtifactError("Review 路径必须直接位于 reviews/ 目录")
        try:
            resolved_run = self.run_dir.resolve(strict=True)
            if self.reviews_dir.is_symlink():
                raise ArtifactError("reviews/ 目录不能是符号链接")
            resolved_reviews = self.reviews_dir.resolve(strict=True)
        except OSError as exc:
            raise ArtifactError("reviews/ 目录缺失或无法安全解析") from exc
        if (
            resolved_reviews.parent != resolved_run
            or not resolved_reviews.is_relative_to(resolved_run)
            or not resolved_reviews.is_dir()
        ):
            raise ArtifactError("reviews/ 目录越出当前 run 或不是目录")
        if path.is_symlink():
            raise ArtifactError("Review 文件不能是符号链接")
        if not path.exists():
            if require_exists:
                raise ArtifactError("Review 记录缺失")
            return path
        try:
            resolved_path = path.resolve(strict=True)
        except OSError as exc:
            raise ArtifactError("Review 文件无法安全解析") from exc
        if (
            not resolved_path.is_relative_to(resolved_run)
            or not resolved_path.is_file()
        ):
            raise ArtifactError("Review 文件越出当前 run 或不是普通文件")
        return path

    def _read_review_path(self, path: Path) -> ReviewResult:
        """读取并校验一个 Review 文件。

        参数为 reviews 目录下的预期文件路径；返回 ``ReviewResult``；文件缺失、
        JSON 损坏或协议字段无效时抛 ``ArtifactError``，避免恢复过程静默降级。
        """
        try:
            self._secure_review_path(path, require_exists=True)
            return ReviewResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ArtifactError("Review 记录缺失或损坏") from exc

    def write_review_result(self, review: ReviewResult) -> str:
        """原子、幂等保存结构化 Reviewer 结论。

        参数为已绑定的 ``ReviewResult``；返回相对引用；同一 HEAD/Reviewer/turn
        已有不同内容、已有文件损坏或文件写入失败时抛 ``ArtifactError``。本方法
        仅负责 Supervisor 可调用的持久化，不触发 Reviewer 运行时调用。
        """
        path = self._review_path(
            head_sha=review.head_sha,
            reviewer=review.agent,
            turn_id=review.turn_id,
        )
        self._secure_review_path(path, require_exists=False)
        if path.exists():
            existing = self._read_review_path(path)
            if existing != review:
                raise ArtifactError("同一 Review 身份已对应不同内容")
            return path.relative_to(self.run_dir).as_posix()
        self._atomic_json(path, review.model_dump(mode="json"))
        return path.relative_to(self.run_dir).as_posix()

    def read_review_result(
        self,
        *,
        head_sha: str,
        reviewer: AgentName,
        turn_id: str,
    ) -> ReviewResult:
        """按三元身份读取结构化审查结论。

        参数为 HEAD、Reviewer 和 turn ID；返回唯一 ``ReviewResult``；记录缺失、
        损坏或文件名与载荷身份不一致时抛 ``ArtifactError``。
        """
        path = self._review_path(
            head_sha=head_sha,
            reviewer=reviewer,
            turn_id=turn_id,
        )
        review = self._read_review_path(path)
        if (
            review.head_sha != head_sha
            or review.agent is not reviewer
            or review.turn_id != turn_id
        ):
            raise ArtifactError("Review 文件路径与载荷身份不一致")
        return review

    def find_review_result(
        self,
        *,
        head_sha: str,
        reviewer: AgentName,
        turn_id: str,
    ) -> ReviewResult | None:
        """按唯一身份查找已持久化 Review。

        参数为 HEAD、Reviewer 和 turn ID；返回已有 ``ReviewResult`` 或 ``None``；
        文件存在但损坏、越界或身份不一致时抛 ``ArtifactError``，以便节点重试复用
        已验证的只读审查而非重新调用 CLI。
        """
        path = self._review_path(
            head_sha=head_sha,
            reviewer=reviewer,
            turn_id=turn_id,
        )
        self._secure_review_path(path, require_exists=False)
        if not path.exists():
            return None
        return self.read_review_result(
            head_sha=head_sha,
            reviewer=reviewer,
            turn_id=turn_id,
        )

    def recover_review_result(self, review_ref: str | None) -> ReviewResult | None:
        """按冻结协议恢复图状态引用的 Review。

        参数为图状态中的相对 Review 引用；legacy-v0 直接返回 ``None`` 且绝不读取
        Review 文件；review-v1 缺失、越界或损坏时写入诊断并抛 ``ArtifactError``。
        当前模块只提供恢复边界，实际 Reviewer Runtime 接入由后续模块负责。
        """
        config = self.load_config()
        if config.protocol_version.value == "legacy-v0":
            return None
        if review_ref is None:
            error = "review-v1 恢复缺少 Review 引用"
            self.write_diagnostic(phase="review_recovery", error=error)
            raise ArtifactError(error)
        try:
            relative = self._safe_run_relative_ref(review_ref)
            if len(relative.parts) != 2 or relative.parts[0] != "reviews":
                raise ArtifactError("Review 引用必须位于 reviews/ 目录")
            candidate = self.run_dir / relative.as_posix()
            self._secure_review_path(candidate, require_exists=True)
            review = self._read_review_path(candidate)
            expected = self._review_path(
                head_sha=review.head_sha,
                reviewer=review.agent,
                turn_id=review.turn_id,
            )
            if candidate != expected:
                raise ArtifactError("Review 引用与载荷身份不一致")
            return review
        except ArtifactError as exc:
            error = f"review-v1 恢复失败: {exc}"
            self.write_diagnostic(phase="review_recovery", error=error)
            raise ArtifactError(error) from exc

    def prepare_invocation(
        self,
        *,
        invocation_id: str,
        agent: AgentName,
        turn_id: str,
        base_commit: str,
        goal_contract_version: int,
        prompt_hash: str,
    ) -> dict[str, Any]:
        """写入调用前日志。

        参数为调用身份、Agent/turn/base/契约和 Prompt hash；返回 journal；冲突时抛错。
        """
        path = self._invocation_path(invocation_id)
        identity = {
            "invocation_id": invocation_id,
            "run_id": self.run_id,
            "agent": agent.value,
            "turn_id": turn_id,
            "base_commit": base_commit,
            "goal_contract_version": goal_contract_version,
            "prompt_hash": prompt_hash,
        }
        if path.exists():
            existing = self.read_invocation(invocation_id)
            if any(existing.get(key) != value for key, value in identity.items()):
                raise ArtifactError("invocation ID 已对应不同外部副作用")
            return existing
        payload = {
            **identity,
            "status": "prepared",
            "prepared_at": self._now(),
        }
        self._atomic_json(path, payload)
        return payload

    def read_invocation(self, invocation_id: str) -> dict[str, Any]:
        """读取调用 journal；参数为 ID，返回字典，不存在或损坏时传播异常。"""
        payload: dict[str, Any] = json.loads(
            self._invocation_path(invocation_id).read_text(encoding="utf-8")
        )
        return payload

    def find_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        """查询调用 journal；参数为 ID，返回字典或 None，损坏时传播异常。"""
        path = self._invocation_path(invocation_id)
        return self.read_invocation(invocation_id) if path.exists() else None

    def _update_invocation(
        self, invocation_id: str, **updates: object
    ) -> dict[str, Any]:
        """原子更新 journal；参数为 ID/字段，返回新字典，文件缺失时抛错。"""
        payload = self.read_invocation(invocation_id)
        payload.update(updates)
        payload["updated_at"] = self._now()
        self._atomic_json(self._invocation_path(invocation_id), payload)
        return payload

    def mark_invocation_running(
        self, invocation_id: str, *, pid: int, process_group: int
    ) -> dict[str, Any]:
        """记录进程身份；参数为 ID/PID/进程组，返回 journal，无记录时抛错。"""
        return self._update_invocation(
            invocation_id,
            status="running",
            pid=pid,
            process_group=process_group,
        )

    def mark_invocation_result(
        self,
        invocation_id: str,
        *,
        result_ref: str,
        ok: bool,
        structured_ref: str | None = None,
    ) -> dict[str, Any]:
        """记录原始/结构化结果；参数为 ID/引用/成功标记，返回 journal。"""
        return self._update_invocation(
            invocation_id,
            status="result",
            result_ref=result_ref,
            ok=ok,
            structured_ref=structured_ref,
        )

    def write_agent_result(self, invocation_id: str, result: AgentResult) -> str:
        """原子保存统一 AgentResult；参数为 ID/结果，返回相对引用，文件失败时抛错。"""
        path = self.invocations_dir / f"{invocation_id}.result.json"
        self._atomic_json(path, result.model_dump(mode="json"))
        return path.relative_to(self.run_dir).as_posix()

    def read_agent_result(self, result_ref: str) -> AgentResult:
        """读取统一 AgentResult；参数为运行目录相对引用，返回模型，越界时抛错。"""
        candidate = (self.run_dir / result_ref).resolve()
        if not candidate.is_relative_to(self.run_dir):
            raise ArtifactError("AgentResult 引用越出运行目录")
        return AgentResult.model_validate_json(candidate.read_text(encoding="utf-8"))

    @staticmethod
    def _safe_run_relative_ref(reference: str) -> PurePosixPath:
        """校验当前 run 的相对产物引用。

        参数为产物引用；返回规范 POSIX 相对路径；空值、绝对路径、当前目录或
        ``..`` 越界路径抛 ``ArtifactError``。
        """
        relative = PurePosixPath(reference)
        if (
            not reference
            or relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ArtifactError("运行产物引用必须是安全相对路径")
        return relative

    def _supervisor_artifact_record_path(self, artifact_ref: str) -> Path:
        """计算单个 Supervisor 产物证明的持久化路径。

        参数为安全运行目录相对引用；返回证明记录路径；引用非法时抛
        ``ArtifactError``。路径使用 SHA-256 避免引用内容影响文件布局。
        """
        relative = self._safe_run_relative_ref(artifact_ref)
        digest = sha256(relative.as_posix().encode("utf-8")).hexdigest()
        return self.run_dir / "supervisor-artifacts" / f"{digest}.json"

    def register_supervisor_artifact(
        self,
        *,
        artifact_ref: str,
        producer: str,
        head_sha: str | None,
    ) -> None:
        """登记由 Supervisor 创建且内容冻结的运行产物。

        参数为当前 run 内产物引用、受控生成者标识和可选 HEAD；无返回值；产物
        不存在、越界、软链接、生成者非法或同一引用内容冲突时抛 ``ArtifactError``。
        """
        if re.fullmatch(r"[a-z][a-z0-9_-]*", producer) is None:
            raise ArtifactError("Supervisor 产物生成者标识非法")
        relative = self._safe_run_relative_ref(artifact_ref)
        candidate = self.run_dir / relative.as_posix()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ArtifactError("待登记 Supervisor 产物不存在") from exc
        if (
            not resolved.is_relative_to(self.run_dir)
            or candidate.is_symlink()
            or not resolved.is_file()
        ):
            raise ArtifactError("待登记 Supervisor 产物必须是当前 run 内的普通文件")
        payload = {
            "run_id": self.run_id,
            "artifact_ref": relative.as_posix(),
            "producer": producer,
            "head_sha": head_sha,
            "content_sha256": sha256(resolved.read_bytes()).hexdigest(),
        }
        record_path = self._supervisor_artifact_record_path(relative.as_posix())
        if record_path.exists():
            try:
                existing = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ArtifactError("Supervisor 产物证明已损坏") from exc
            if existing != payload:
                raise ArtifactError("Supervisor 产物证明与已有记录冲突")
            return
        self._atomic_json(record_path, payload)

    def verify_supervisor_artifact(
        self,
        *,
        artifact_ref: str,
        producer: str,
        head_sha: str,
    ) -> None:
        """验证当前 run 中受控生成的产物身份和内容摘要。

        参数为产物引用、期望生成者和当前 HEAD；无返回值；证明缺失、身份不符、
        内容被替换或路径非法时抛 ``ArtifactError``。
        """
        relative = self._safe_run_relative_ref(artifact_ref)
        record_path = self._supervisor_artifact_record_path(relative.as_posix())
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError("缺少有效的 Supervisor 产物证明") from exc
        expected = {
            "run_id": self.run_id,
            "artifact_ref": relative.as_posix(),
            "producer": producer,
            "head_sha": head_sha,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ArtifactError("Supervisor 产物证明身份不匹配")
        candidate = self.run_dir / relative.as_posix()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ArtifactError("已证明的 Supervisor 产物不存在") from exc
        if (
            not resolved.is_relative_to(self.run_dir)
            or candidate.is_symlink()
            or not resolved.is_file()
        ):
            raise ArtifactError("已证明的 Supervisor 产物路径非法")
        actual_digest = sha256(resolved.read_bytes()).hexdigest()
        if payload.get("content_sha256") != actual_digest:
            raise ArtifactError("Supervisor 产物内容已变化")

    def mark_invocation_commit(
        self, invocation_id: str, *, commit_sha: str
    ) -> dict[str, Any]:
        """记录检查点提交；参数为 ID/SHA，返回 journal，文件错误时抛错。"""
        return self._update_invocation(
            invocation_id,
            status="committed",
            commit_sha=commit_sha,
        )

    def mark_invocation_checkpointed(self, invocation_id: str) -> dict[str, Any]:
        """记录 LangGraph checkpoint 已落盘；参数为 ID，返回 journal。"""
        return self._update_invocation(invocation_id, status="checkpointed")

    def mark_invocation_failed(
        self, invocation_id: str, *, error: str
    ) -> dict[str, Any]:
        """记录调用不可安全重放；参数为 ID/脱敏错误，返回 journal。"""
        return self._update_invocation(
            invocation_id,
            status="failed",
            error=error[:500],
        )

    def mark_invocation_uncertain(
        self, invocation_id: str, *, error: str
    ) -> dict[str, Any]:
        """记录已产生副作用但协议结果不可信的调用。

        参数为调用 ID 和脱敏错误；返回 journal；记录不存在或持久化失败时传播异常。
        """
        return self._update_invocation(
            invocation_id,
            status="uncertain",
            error=error[:500],
        )

    def completed_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        """查询幂等完成结果；参数为 ID，返回 checkpointed journal 或 None。"""
        path = self._invocation_path(invocation_id)
        if not path.exists():
            return None
        payload = self.read_invocation(invocation_id)
        return payload if payload.get("status") == "checkpointed" else None

    def recoverable_invocations(self) -> list[dict[str, Any]]:
        """列出可能归属脏树的 journal；无参数，返回稳定列表，损坏时传播异常。"""
        candidates = []
        for path in sorted(self.invocations_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            status = payload.get("status")
            recoverable = status in {"prepared", "running", "uncertain"} or (
                status == "result" and payload.get("ok") is True
            )
            if payload.get("invocation_id") and recoverable:
                candidates.append(payload)
        return candidates

    @property
    def abort_path(self) -> Path:
        """返回跨进程中止标记路径；无参数，不执行 I/O。"""
        return self.run_dir / "abort.requested"

    def request_abort(self) -> None:
        """原子写入中止请求；无参数和返回值，文件失败时抛错。"""
        temporary = self.abort_path.with_name(
            f".{self.abort_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(self._now(), encoding="utf-8")
        os.replace(temporary, self.abort_path)

    def abort_requested(self) -> bool:
        """检查中止标记；无参数，返回布尔值，不抛异常。"""
        return self.abort_path.exists()

    def clear_abort_request(self) -> None:
        """清除本 run 中止标记；无参数和返回值，文件错误时抛异常。"""
        self.abort_path.unlink(missing_ok=True)

    def cleanup_sensitive(self) -> None:
        """清理成功 run 的敏感运行产物；无参数和返回值，文件错误时抛错。"""
        for name in ("raw", "leases", "invocations"):
            path = self.run_dir / name
            if path.exists():
                shutil.rmtree(path)
        for name in ("agent-sessions.json", "abort.requested"):
            (self.run_dir / name).unlink(missing_ok=True)
