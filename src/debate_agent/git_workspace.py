"""目标 Git 仓库、共享 worktree 和提交事务。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from debate_agent.models import AgentName, LeaseState, LeaseStatus
from debate_agent.paths import audit_relative_paths, select_audit_relative_path
from debate_agent.policy import WritePolicy


class GitWorkspaceError(RuntimeError):
    """表示 Git 命令或仓库结构不满足运行要求。"""


class DirtyWorkspaceError(GitWorkspaceError):
    """表示源工作空间存在未提交或未跟踪文件。"""


class RepositoryLockError(GitWorkspaceError):
    """表示仓库目标分支已被其他 run 锁定。"""


class IntegrationBlockedError(GitWorkspaceError):
    """表示最终提交不能安全快进目标分支。"""


class WorkspaceRaceError(GitWorkspaceError):
    """表示租约撤销或验证后工作树又发生变化。"""


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """保存某一静默点的 HEAD、tree、路径和内容指纹。"""

    head: str
    tree: str
    changed_paths: tuple[str, ...]
    fingerprint: str


@dataclass(slots=True)
class GitWorkspace:
    """封装一个 run 对目标仓库的确定性 Git 操作。"""

    repo_root: Path
    target_branch: str
    base_commit: str
    state_root: Path
    run_id: str
    debate_branch: str
    worktree_path: Path
    use_worktree: bool = True

    @staticmethod
    def _run_git(
        repo: Path,
        args: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> str:
        """执行 Git。

        参数为仓库、argv 和可选 stdin；返回 stdout；失败时抛 GitWorkspaceError。
        """
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise GitWorkspaceError(message or f"Git 命令失败: {' '.join(args)}")
        return result.stdout.strip()

    @classmethod
    def preflight(
        cls,
        workspace: Path,
        *,
        target_branch: str,
        base_ref: str,
        state_root: Path,
        run_id: str,
        use_worktree: bool = True,
        excluded_paths: Sequence[str] = (),
    ) -> GitWorkspace:
        """校验并冻结源仓库；参数为路径/分支/基线/状态/run，返回实例，无效或脏时抛错。"""
        candidate = workspace.expanduser().resolve()
        try:
            root_text = cls._run_git(candidate, ["rev-parse", "--show-toplevel"])
        except GitWorkspaceError as exc:
            if (candidate / ".git").exists():
                raise GitWorkspaceError(
                    f"workspace Git 元数据不可用: {candidate}"
                ) from exc
            cls._initialize_repository(
                candidate,
                target_branch=target_branch,
                state_root=state_root,
                excluded_paths=excluded_paths,
            )
            root_text = cls._run_git(candidate, ["rev-parse", "--show-toplevel"])
        except OSError as exc:
            raise GitWorkspaceError("无法执行 git，请先安装 Git") from exc
        repo_root = Path(root_text).resolve()
        if not os.access(repo_root, os.R_OK | os.W_OK):
            raise GitWorkspaceError(f"workspace 必须可读写: {repo_root}")
        current_branch = cls._run_git(repo_root, ["branch", "--show-current"])
        if current_branch != target_branch:
            raise GitWorkspaceError(
                f"当前分支 {current_branch} 与目标分支 {target_branch} 不一致"
            )
        try:
            cls._run_git(repo_root, ["rev-parse", "--verify", "HEAD"])
        except GitWorkspaceError:
            cls._create_initial_baseline(
                repo_root,
                state_root=state_root,
                excluded_paths=excluded_paths,
            )
        status = cls._run_git(
            repo_root, ["status", "--porcelain", "--untracked-files=all"]
        )
        if status:
            raise DirtyWorkspaceError("源工作空间必须保持干净")
        base_commit = cls._run_git(repo_root, ["rev-parse", base_ref])
        target_commit = cls._run_git(
            repo_root, ["rev-parse", f"refs/heads/{target_branch}"]
        )
        if base_commit != target_commit:
            raise GitWorkspaceError("base_ref 必须等于目标分支当前提交")
        resolved_state = state_root.expanduser().resolve()
        resolved_state.mkdir(parents=True, exist_ok=True)
        probe = resolved_state / f".write-probe-{run_id}"
        try:
            descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        except OSError as exc:
            raise GitWorkspaceError(f"状态目录不可写: {resolved_state}") from exc
        finally:
            probe.unlink(missing_ok=True)
        worktree_path = (
            resolved_state / "worktrees" / run_id / "workspace"
            if use_worktree
            else repo_root
        )
        return cls(
            repo_root=repo_root,
            target_branch=target_branch,
            base_commit=base_commit,
            state_root=resolved_state,
            run_id=run_id,
            debate_branch=f"debate/{run_id}",
            worktree_path=worktree_path,
            use_worktree=use_worktree,
        )

    @classmethod
    def _initialize_repository(
        cls,
        workspace: Path,
        *,
        target_branch: str,
        state_root: Path,
        excluded_paths: Sequence[str],
    ) -> None:
        """初始化 Git 基线；参数为目录/分支/状态与保护路径，无返回值，失败时抛错。"""
        if not workspace.is_dir() or not os.access(workspace, os.R_OK | os.W_OK):
            raise GitWorkspaceError(f"workspace 必须是可读写目录: {workspace}")
        cls._run_git(workspace, ["init", "-b", target_branch])
        cls._create_initial_baseline(
            workspace,
            state_root=state_root,
            excluded_paths=excluded_paths,
        )

    @classmethod
    def _create_initial_baseline(
        cls,
        workspace: Path,
        *,
        state_root: Path,
        excluded_paths: Sequence[str],
    ) -> None:
        """创建首个 Git 提交；参数为仓库/状态与保护路径，无返回值，失败时抛错。"""
        author_defaults = {
            "user.name": "Discuss Agent",
            "user.email": "debate-agent@local",
        }
        for key, default in author_defaults.items():
            try:
                cls._run_git(workspace, ["config", "--get", key])
            except GitWorkspaceError:
                cls._run_git(workspace, ["config", key, default])
        exclusions = {
            PurePosixPath(path).as_posix().rstrip("/")
            for path in excluded_paths
            if path and path != ".git"
        }
        resolved_state = state_root.expanduser().resolve()
        if resolved_state.is_relative_to(workspace):
            exclusions.add(resolved_state.relative_to(workspace).as_posix())
        exclude_file = workspace / ".git" / "info" / "exclude"
        existing = exclude_file.read_text(encoding="utf-8")
        additions = [
            f"/{path}"
            for path in sorted(exclusions)
            if f"/{path}" not in existing
        ]
        if additions:
            with exclude_file.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write("\n".join(additions) + "\n")
        cls._run_git(workspace, ["add", "-A", "--", "."])
        cls._run_git(
            workspace,
            [
                "commit",
                "--allow-empty",
                "-m",
                "[debate][supervisor] initialize workspace",
            ],
        )

    @property
    def lock_path(self) -> Path:
        """计算仓库分支锁路径；无参数，返回稳定路径，不执行 I/O。"""
        identity = f"{self.repo_root}\0{self.target_branch}".encode()
        digest = hashlib.sha256(identity).hexdigest()
        return self.state_root / "locks" / f"{digest}.json"

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        """检查进程是否存活；参数为 PID，返回布尔值，不因权限错误误判死亡。"""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def acquire_lock(
        self,
        *,
        stale_recovery: Callable[[Mapping[str, object]], bool] | None = None,
    ) -> Path:
        """原子获取仓库锁。

        参数为可选遗留状态恢复判定；返回锁路径；活锁或未获恢复许可时抛错。
        """
        path = self.lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                pid = int(existing["pid"])
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ) as exc:
                raise RepositoryLockError("仓库锁损坏，拒绝自动接管") from exc
            if self._pid_is_alive(pid):
                raise RepositoryLockError("目标仓库分支已有 active run")
            if stale_recovery is None or not stale_recovery(existing):
                raise RepositoryLockError("遗留锁状态未经确认，不允许接管")
            archive = path.with_name(
                f"{path.stem}.stale-{existing.get('run_id', 'unknown')}.json"
            )
            try:
                path.replace(archive)
            except FileNotFoundError as exc:
                raise RepositoryLockError("遗留锁接管发生竞态") from exc
        payload = json.dumps(
            {
                "run_id": self.run_id,
                "pid": os.getpid(),
                "repo_root": str(self.repo_root),
                "target_branch": self.target_branch,
                "base_commit": self.base_commit,
                "worktree": str(self.worktree_path),
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise RepositoryLockError("目标仓库分支已有 active run") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return path

    def to_record(self) -> dict[str, object]:
        """序列化恢复记录；无参数，返回不含凭据的路径/分支/基线字典。"""
        return {
            "repo_root": str(self.repo_root),
            "target_branch": self.target_branch,
            "base_commit": self.base_commit,
            "state_root": str(self.state_root),
            "run_id": self.run_id,
            "debate_branch": self.debate_branch,
            "worktree_path": str(self.worktree_path),
            "use_worktree": self.use_worktree,
        }

    @classmethod
    def from_record(
        cls,
        payload: Mapping[str, object],
        *,
        expected_state_root: Path,
        expected_run_id: str,
        require_worktree: bool = True,
    ) -> GitWorkspace:
        """从运行记录恢复 GitWorkspace。

        参数为记录映射；返回实例；字段缺失、路径失效或分支身份不符时抛错。
        """
        try:
            workspace = cls(
                repo_root=Path(str(payload["repo_root"])).resolve(),
                target_branch=str(payload["target_branch"]),
                base_commit=str(payload["base_commit"]),
                state_root=Path(str(payload["state_root"])).resolve(),
                run_id=str(payload["run_id"]),
                debate_branch=str(payload["debate_branch"]),
                worktree_path=Path(str(payload["worktree_path"])).resolve(),
                use_worktree=bool(payload.get("use_worktree", True)),
            )
        except KeyError as exc:
            raise GitWorkspaceError(f"Git 恢复记录缺少字段: {exc.args[0]}") from exc
        expected_root = expected_state_root.expanduser().resolve()
        if workspace.run_id != expected_run_id or workspace.state_root != expected_root:
            raise GitWorkspaceError("Git 恢复记录与请求的 run/state root 不一致")
        expected_worktree = (
            expected_root / "worktrees" / expected_run_id / "workspace"
            if workspace.use_worktree
            else workspace.repo_root
        )
        if workspace.worktree_path != expected_worktree:
            raise GitWorkspaceError("Git 恢复记录中的 worktree 路径不符合 run 布局")
        if not workspace.repo_root.is_dir() or (
            require_worktree and not workspace.worktree_path.is_dir()
        ):
            raise GitWorkspaceError("Git 恢复记录中的仓库或 worktree 不存在")
        actual_root = cls._run_git(
            workspace.repo_root,
            ["rev-parse", "--show-toplevel"],
        )
        if Path(actual_root).resolve() != workspace.repo_root:
            raise GitWorkspaceError("Git 恢复记录中的仓库根不一致")
        if workspace.debate_branch != f"debate/{workspace.run_id}":
            raise GitWorkspaceError("Git 恢复记录中的临时分支身份不一致")
        if require_worktree:
            actual_branch = cls._run_git(
                workspace.worktree_path, ["branch", "--show-current"]
            )
            valid_branches = (
                {workspace.debate_branch}
                if workspace.use_worktree
                else {workspace.target_branch, workspace.debate_branch}
            )
            if actual_branch not in valid_branches:
                raise GitWorkspaceError("共享 worktree 当前分支与恢复记录不一致")
        return workspace

    def release_lock(self) -> None:
        """释放自己持有的仓库锁；无参数和返回值，锁属于其他 run 时抛错。"""
        path = self.lock_path
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("run_id") != self.run_id:
            raise RepositoryLockError("不能释放其他 run 的仓库锁")
        path.unlink()

    def assert_lock_owner(self) -> None:
        """确认仓库锁仍属于当前 run；无参数和返回值，丢失或易主时抛锁错误。"""
        try:
            payload = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryLockError("仓库锁不存在或已损坏") from exc
        if payload.get("run_id") != self.run_id:
            raise RepositoryLockError("仓库锁已不属于当前 run")

    def create_worktree(self) -> Path:
        """创建临时分支和共享 worktree；无参数，返回路径，未持锁或 Git 失败时抛错。"""
        if not self.lock_path.exists():
            raise RepositoryLockError("创建 worktree 前必须持有仓库锁")
        if not self.use_worktree:
            current_branch = self._run_git(
                self.repo_root, ["branch", "--show-current"]
            )
            if current_branch != self.target_branch:
                raise GitWorkspaceError("原工作空间已不在目标分支")
            if self.changed_paths():
                raise DirtyWorkspaceError("原工作空间必须保持干净")
            self._run_git(
                self.repo_root,
                ["switch", "-c", self.debate_branch, self.base_commit],
            )
            return self.worktree_path
        self.worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._run_git(
            self.repo_root,
            [
                "worktree",
                "add",
                str(self.worktree_path),
                "-b",
                self.debate_branch,
                self.base_commit,
            ],
        )
        return self.worktree_path

    def changed_paths(self) -> list[str]:
        """读取 worktree 全部变更路径；无参数，返回排序列表，worktree 不存在时抛错。"""
        if not self.worktree_path.exists():
            raise GitWorkspaceError("共享 worktree 不存在")
        commands = (
            ["diff", "--name-only", "-z"],
            ["diff", "--cached", "--name-only", "-z"],
            ["ls-files", "--others", "--exclude-standard", "-z"],
        )
        paths: set[str] = set()
        for command in commands:
            output = self._run_git(self.worktree_path, command)
            paths.update(path for path in output.split("\0") if path)
        return sorted(paths)

    def head(self) -> str:
        """读取共享 worktree 的 HEAD；无参数，返回 commit SHA，不存在时抛 Git 错误。"""
        return self._run_git(self.worktree_path, ["rev-parse", "HEAD"])

    def path_exists_at_head(self, path: str) -> bool:
        """检查仓库相对文件或目录是否存在于当前 HEAD。

        参数为已经过调用方路径策略校验的仓库相对路径；返回该路径是否由当前
        HEAD 跟踪。Git 无法读取当前 HEAD 时抛 ``GitWorkspaceError``。
        """
        output = self._run_git(
            self.worktree_path,
            ["ls-tree", "-r", "--name-only", "-z", "HEAD", "--", path],
        )
        tracked = {item for item in output.split("\0") if item}
        directory_prefix = f"{path.rstrip('/')}/"
        return path in tracked or any(
            item.startswith(directory_prefix) for item in tracked
        )

    def path_changed_at_head(self, path: str) -> bool:
        """检查仓库相对路径是否出现在 Run 基线到当前 HEAD 的累计差异中。

        参数为已经过调用方路径策略校验的仓库相对路径；返回本 Run 是否修改、新增
        或删除该路径。Git 无法读取基线或当前 HEAD 时抛 ``GitWorkspaceError``。
        """
        output = self._run_git(
            self.worktree_path,
            [
                "diff",
                "--name-only",
                "-z",
                self.base_commit,
                "HEAD",
                "--",
                path,
            ],
        )
        return bool(output)

    def tree(self) -> str:
        """读取共享 worktree 的 HEAD tree；无参数，返回 tree SHA，Git 失败时抛错。"""
        return self._run_git(self.worktree_path, ["rev-parse", "HEAD^{tree}"])

    def target_tree(self) -> str:
        """读取目标分支当前 tree；无参数，返回 tree SHA，Git 失败时抛错。"""
        return self._run_git(
            self.repo_root,
            ["rev-parse", f"refs/heads/{self.target_branch}^{{tree}}"],
        )

    def index_tree(self) -> str:
        """读取当前 index tree；无参数，返回 tree SHA，Git 失败时抛错。"""
        return self._run_git(self.worktree_path, ["write-tree"])

    def stage_audit_tree(self, audit_path: str, *, require_only_audit: bool) -> str:
        """仅暂存 Supervisor 审计子树。

        参数为审计相对路径和是否拒绝其他变更；返回 index tree；
        越界或混入产品代码时抛错。
        """
        path = PurePosixPath(audit_path)
        expected = {
            PurePosixPath(candidate)
            for candidate in audit_relative_paths(self.run_id)
        }
        if path.is_absolute() or ".." in path.parts or path not in expected:
            raise GitWorkspaceError("审计路径必须属于当前 run")
        audit_root = self.worktree_path / path.as_posix()
        if not audit_root.is_dir():
            raise GitWorkspaceError("当前 run 的审计目录不存在")
        changed = self.changed_paths()
        prefix = path.as_posix() + "/"
        outside = [item for item in changed if not item.startswith(prefix)]
        if require_only_audit and outside:
            raise GitWorkspaceError(f"final tree 前仍有产品代码变更: {outside}")
        self._run_git(
            self.worktree_path,
            ["add", "-f", "-A", "--", path.as_posix()],
        )
        return self.index_tree()

    def discard_supervisor_audit(
        self,
        expected_files: Collection[str],
        *,
        expected_head: str | None = None,
    ) -> bool:
        """撤销当前 run 的 Supervisor 审计。

        参数为允许的固定文件名和可选的恢复点；返回是否清理；
        路径异常、混入额外文件或无法证明已提交内容仅为审计时抛错。
        """
        expected = set(expected_files)
        if not expected or any(
            PurePosixPath(name).name != name or "/" in name for name in expected
        ):
            raise GitWorkspaceError("Supervisor 审计文件集合非法")
        relative_root = PurePosixPath(
            select_audit_relative_path(self.worktree_path, self.run_id)
        )
        prefix = relative_root.as_posix() + "/"
        audit_root = self.worktree_path / relative_root.as_posix()
        changed = self.changed_paths()
        audit_changes = [path for path in changed if path.startswith(prefix)]
        if not audit_root.exists() and not audit_changes:
            return False
        if audit_root.is_symlink() or not audit_root.is_dir():
            raise GitWorkspaceError("Supervisor 审计目录类型异常")
        actual_files: set[str] = set()
        for path in audit_root.rglob("*"):
            if path.is_symlink() or not path.is_file() or path.parent != audit_root:
                raise GitWorkspaceError("Supervisor 审计目录包含异常条目")
            actual_files.add(path.name)
        if not actual_files.issubset(expected):
            raise GitWorkspaceError("Supervisor 审计目录包含额外文件")
        allowed_paths = {f"{prefix}{name}" for name in expected}
        if any(path not in allowed_paths for path in audit_changes):
            raise GitWorkspaceError("Supervisor 审计变更范围异常")
        tracked = self._run_git(
            self.worktree_path,
            ["ls-tree", "-r", "--name-only", "HEAD", "--", relative_root.as_posix()],
        )
        if tracked:
            current_head = self.head()
            if not expected_head or current_head == expected_head:
                raise GitWorkspaceError("Supervisor 审计已经进入 HEAD，拒绝自动撤销")
            resolved_expected = self._run_git(
                self.worktree_path,
                ["rev-parse", "--verify", f"{expected_head}^{{commit}}"],
            )
            if resolved_expected != expected_head:
                raise GitWorkspaceError("Supervisor 审计恢复点非法")
            try:
                self._run_git(
                    self.worktree_path,
                    ["merge-base", "--is-ancestor", expected_head, current_head],
                )
            except GitWorkspaceError as exc:
                raise GitWorkspaceError("Supervisor 审计提交不在恢复点之后") from exc
            committed_paths = set(
                filter(
                    None,
                    self._run_git(
                        self.worktree_path,
                        [
                            "diff",
                            "--name-only",
                            expected_head,
                            current_head,
                            "--",
                        ],
                    ).splitlines(),
                )
            )
            if not committed_paths or not committed_paths.issubset(allowed_paths):
                raise GitWorkspaceError("恢复点之后包含非 Supervisor 审计提交")
            self._run_git(
                self.worktree_path,
                ["reset", "--soft", expected_head],
            )
        self._run_git(
            self.worktree_path,
            ["reset", "-q", "HEAD", "--", relative_root.as_posix()],
        )
        for name in actual_files:
            (audit_root / name).unlink()
        audit_root.rmdir()
        if any(path.startswith(prefix) for path in self.changed_paths()):
            raise GitWorkspaceError("Supervisor 审计撤销后仍存在残留变更")
        return True

    def snapshot(self) -> WorkspaceSnapshot:
        """生成工作树静默快照。

        无参数；返回 HEAD、tree、路径和内容指纹；读取文件或 Git 失败时抛异常。
        """
        paths = tuple(self.changed_paths())
        digest = hashlib.sha256()
        head = self.head()
        tree = self.tree()
        digest.update(head.encode())
        digest.update(
            self._run_git(self.worktree_path, ["diff", "--binary", "HEAD"]).encode()
        )
        for relative in paths:
            digest.update(relative.encode())
            candidate = self.worktree_path / relative
            if candidate.is_symlink():
                digest.update(b"symlink\0")
                digest.update(os.readlink(candidate).encode())
            elif candidate.is_file():
                digest.update(b"file\0")
                with candidate.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif candidate.exists():
                digest.update(b"other\0")
            else:
                digest.update(b"deleted\0")
        return WorkspaceSnapshot(
            head=head,
            tree=tree,
            changed_paths=paths,
            fingerprint=digest.hexdigest(),
        )

    def commit_checkpoint(
        self,
        *,
        agent: AgentName,
        turn_id: str,
        goal_contract_version: int,
        validation_status: str,
        summary: str,
        lease: LeaseState,
        validated_snapshot: WorkspaceSnapshot,
        validated_paths: Sequence[str],
        write_policy: WritePolicy,
    ) -> str | None:
        """提交合法回合变更。

        参数为归属、已撤销租约、验证快照与路径；返回 SHA 或 None；竞态时抛错。
        """
        if lease.status is not LeaseStatus.REVOKED:
            raise WorkspaceRaceError("提交前必须先撤销写入租约")
        if (
            lease.run_id != self.run_id
            or lease.agent is not agent
            or lease.turn_id != turn_id
            or lease.goal_contract_version != goal_contract_version
        ):
            raise WorkspaceRaceError("检查点归属与租约不一致")
        current = self.snapshot()
        if current != validated_snapshot:
            raise WorkspaceRaceError("验证后工作树发生变化")
        expected_paths = tuple(sorted(set(validated_paths)))
        if current.changed_paths != expected_paths:
            raise WorkspaceRaceError("提交路径与已验证范围不一致")
        policy_paths = tuple(write_policy.validate_paths(current.changed_paths))
        if policy_paths != expected_paths:
            raise WorkspaceRaceError("路径策略结果与验证记录不一致")
        if current.head != lease.base_commit:
            raise WorkspaceRaceError("回合 HEAD 与租约基线不一致")
        if not current.changed_paths:
            return None
        self._run_git(self.worktree_path, ["add", "-A", "--", "."])
        subject = summary.strip().splitlines()[0] if summary.strip() else "checkpoint"
        message = (
            f"[debate][{agent.value}][{turn_id}][{validation_status}] {subject}\n\n"
            f"Debate-Run: {self.run_id}\n"
            f"Debate-Agent: {agent.value}\n"
            f"Debate-Turn: {turn_id}\n"
            f"Goal-Contract: v{goal_contract_version}\n"
            f"Validation-Status: {validation_status}"
        )
        self._run_git(self.worktree_path, ["commit", "-m", message])
        committed = self._run_git(self.worktree_path, ["rev-parse", "HEAD"])
        if self.changed_paths():
            raise WorkspaceRaceError("提交后工作树仍不干净")
        return committed

    def create_integration_commit(self, final_tree_sha: str, message: str) -> str:
        """从 final tree 创建单 parent 提交。

        参数为 tree 和消息；返回 SHA；Git 失败时抛 GitWorkspaceError。
        """
        commit = self._run_git(
            self.repo_root,
            ["commit-tree", final_tree_sha, "-p", self.base_commit],
            input_text=message,
        )
        if not self.use_worktree and self.changed_paths():
            if self.index_tree() != final_tree_sha:
                raise GitWorkspaceError("原工作空间 index 与 final tree 不一致")
            self._run_git(
                self.repo_root,
                ["commit", "-m", f"[debate][supervisor][{self.run_id}] final audit"],
            )
        return commit

    def _integration_commit_for_target(
        self,
        *,
        final_commit: str,
        final_tree_sha: str,
        target_head: str,
    ) -> tuple[str, str]:
        """把 Run 聚焦提交安全重放到当前目标 HEAD。

        参数为原聚焦提交/tree 和当前目标 HEAD；返回可快进提交及其 tree；目标历史
        改写、Run 提交损坏或三方合并冲突时抛 ``IntegrationBlockedError``。
        """
        commit_tree = self._run_git(
            self.repo_root, ["rev-parse", f"{final_commit}^{{tree}}"]
        )
        if commit_tree != final_tree_sha:
            raise IntegrationBlockedError("聚焦提交与已确认 final tree 不一致")
        if target_head == self.base_commit:
            return final_commit, final_tree_sha
        try:
            self._run_git(
                self.repo_root,
                ["merge-base", "--is-ancestor", self.base_commit, target_head],
            )
        except GitWorkspaceError as exc:
            raise IntegrationBlockedError("目标分支历史已改写") from exc
        try:
            merged_tree = self._run_git(
                self.repo_root,
                ["merge-tree", "--write-tree", target_head, final_commit],
            )
        except GitWorkspaceError as exc:
            raise IntegrationBlockedError("目标分支漂移与 Run 结果存在冲突") from exc
        message = self._run_git(
            self.repo_root, ["show", "-s", "--format=%B", final_commit]
        )
        rebased_message = (
            f"{message.rstrip()}\n\n"
            f"Integrated-Onto: {target_head}\n"
            f"Integrated-Tree: {merged_tree}"
        )
        rebased_commit = self._run_git(
            self.repo_root,
            ["commit-tree", merged_tree, "-p", target_head],
            input_text=rebased_message,
        )
        return rebased_commit, merged_tree

    def _drop_owned_stash(self, stash_commit: str) -> None:
        """删除本次集成创建的临时 stash。

        参数为 stash 提交；无返回值；stash 顶部已被并发改写时抛集成阻断异常。
        """
        current = self._run_git(self.repo_root, ["rev-parse", "refs/stash"])
        if current != stash_commit:
            raise IntegrationBlockedError("临时 stash 已被并发改写，拒绝自动删除")
        self._run_git(self.repo_root, ["stash", "drop", "stash@{0}"])

    def _restore_owned_stash(self, stash_commit: str) -> None:
        """恢复并删除本次集成创建的临时 stash。

        参数为 stash 提交；无返回值；恢复冲突时保留 stash 并抛集成阻断异常。
        """
        try:
            self._run_git(
                self.repo_root,
                ["stash", "apply", "--index", stash_commit],
            )
        except GitWorkspaceError as exc:
            raise IntegrationBlockedError(
                f"本地改动自动恢复失败，已保留在 stash {stash_commit}"
            ) from exc
        self._drop_owned_stash(stash_commit)

    def _merge_preserving_dirty_target(
        self,
        *,
        target_head: str,
        integrated_commit: str,
    ) -> None:
        """临时收纳并三方恢复目标工作区的本地改动。

        参数为目标 HEAD 和待快进提交；无返回值；本地改动冲突或 Git 失败时抛
        ``IntegrationBlockedError``，并在目标 HEAD 未移动时恢复原改动。
        """
        try:
            previous_stash = self._run_git(
                self.repo_root, ["rev-parse", "--verify", "refs/stash"]
            )
        except GitWorkspaceError:
            previous_stash = None
        try:
            self._run_git(
                self.repo_root,
                [
                    "stash",
                    "push",
                    "--include-untracked",
                    "--message",
                    f"[debate][supervisor][{self.run_id}] integration guard",
                ],
            )
            stash_commit = self._run_git(
                self.repo_root, ["rev-parse", "--verify", "refs/stash"]
            )
        except GitWorkspaceError as exc:
            raise IntegrationBlockedError("目标工作区本地改动无法安全收纳") from exc
        if stash_commit == previous_stash:
            raise IntegrationBlockedError("目标工作区本地改动未被完整收纳")
        try:
            self._run_git(
                self.repo_root,
                ["merge-tree", "--write-tree", integrated_commit, stash_commit],
            )
        except GitWorkspaceError as exc:
            self._restore_owned_stash(stash_commit)
            raise IntegrationBlockedError(
                "目标工作区本地改动与 Run 结果存在冲突"
            ) from exc
        try:
            self._run_git(self.repo_root, ["merge", "--ff-only", integrated_commit])
        except GitWorkspaceError as exc:
            if self._run_git(self.repo_root, ["rev-parse", "HEAD"]) == target_head:
                self._restore_owned_stash(stash_commit)
            raise IntegrationBlockedError("目标分支无法 fast-forward") from exc
        self._restore_owned_stash(stash_commit)

    def integrate_target(
        self, final_commit: str, final_tree_sha: str
    ) -> tuple[str, str]:
        """安全集成目标分支。

        参数为已确认的 Run 聚焦提交/tree；返回实际集成提交/tree；目标漂移冲突、
        当前分支错误或本地改动会被覆盖时抛 ``IntegrationBlockedError``。
        """
        try:
            self.assert_lock_owner()
        except RepositoryLockError as exc:
            raise IntegrationBlockedError("最终集成前仓库锁校验失败") from exc
        target_head = self._run_git(
            self.repo_root, ["rev-parse", f"refs/heads/{self.target_branch}"]
        )
        if target_head == final_commit:
            if self.target_tree() != final_tree_sha:
                raise IntegrationBlockedError("已集成提交的 tree 与 final tree 不一致")
            return final_commit, final_tree_sha
        status = self._run_git(
            self.repo_root, ["status", "--porcelain", "--untracked-files=all"]
        )
        if status and not self.use_worktree:
            raise IntegrationBlockedError("源工作空间不干净")
        current_branch = self._run_git(
            self.repo_root,
            ["branch", "--show-current"],
        )
        valid_branches = (
            {self.target_branch}
            if self.use_worktree
            else {self.target_branch, self.debate_branch}
        )
        if current_branch not in valid_branches:
            raise IntegrationBlockedError("源工作空间已不在目标分支")
        integrated_commit, integrated_tree = self._integration_commit_for_target(
            final_commit=final_commit,
            final_tree_sha=final_tree_sha,
            target_head=target_head,
        )
        if not self.use_worktree and current_branch == self.debate_branch:
            self._run_git(self.repo_root, ["switch", self.target_branch])
        if status:
            self._merge_preserving_dirty_target(
                target_head=target_head,
                integrated_commit=integrated_commit,
            )
        else:
            try:
                self._run_git(
                    self.repo_root, ["merge", "--ff-only", integrated_commit]
                )
            except GitWorkspaceError as exc:
                raise IntegrationBlockedError("目标分支无法 fast-forward") from exc
        actual_tree = self._run_git(self.repo_root, ["rev-parse", "HEAD^{tree}"])
        if actual_tree != integrated_tree:
            raise IntegrationBlockedError("集成后的 tree 与 final tree 不一致")
        return integrated_commit, integrated_tree

    def preserve_result(self, final_commit: str, final_tree_sha: str) -> None:
        """保留未集成结果。

        参数为最终提交和 tree；将 debate 分支固定到聚焦提交并释放锁；
        分支、可选 worktree 或文件树不一致时抛集成阻断错误。
        """
        try:
            self.assert_lock_owner()
        except RepositoryLockError as exc:
            raise IntegrationBlockedError("保留结果前仓库锁校验失败") from exc
        current_branch = self._run_git(
            self.worktree_path, ["branch", "--show-current"]
        )
        if current_branch != self.debate_branch:
            raise IntegrationBlockedError("结果工作区已不在本次 debate 分支")
        if self.index_tree() != final_tree_sha:
            raise IntegrationBlockedError("待保留的 index 与 final tree 不一致")
        self._run_git(self.worktree_path, ["reset", "--hard", final_commit])
        if self.tree() != final_tree_sha or self.changed_paths():
            raise IntegrationBlockedError("结果分支未固定到干净的 final tree")
        self.release_lock()

    def cleanup_success(self) -> None:
        """清理成功运行的临时 Git 资源；无参数和返回值，Git 或锁错误时抛异常。"""
        if self.use_worktree and self.worktree_path.exists():
            self._run_git(
                self.repo_root,
                ["worktree", "remove", "--force", str(self.worktree_path)],
            )
        if not self.use_worktree:
            current_branch = self._run_git(
                self.repo_root, ["branch", "--show-current"]
            )
            if current_branch == self.debate_branch:
                if self.changed_paths():
                    raise DirtyWorkspaceError("原工作空间仍有未提交变更，拒绝清理")
                self._run_git(self.repo_root, ["switch", self.target_branch])
            elif current_branch != self.target_branch:
                raise GitWorkspaceError("原工作空间位于非本次运行分支，拒绝清理")
        branches = self._run_git(
            self.repo_root, ["branch", "--list", self.debate_branch]
        )
        if branches:
            self._run_git(self.repo_root, ["branch", "-D", self.debate_branch])
        self.release_lock()
