"""把简化页面输入转换为安全运行配置，并提供只读运行摘要。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from debate_agent.agent_catalog import agent_definition, agent_definitions
from debate_agent.artifacts import ArtifactStore
from debate_agent.config import (
    AgentProfile,
    RunConfig,
    _default_profiles,
    infer_target_branch,
)
from debate_agent.events import RunEventStore
from debate_agent.invocation_events import (
    AgentInvocationProgress,
    InvocationEvent,
    InvocationEventStore,
    InvocationProgress,
    Redactor,
    read_redacted_tail,
)
from debate_agent.models import AgentName, ReviewProtocolVersion


def parse_command(value: str) -> tuple[str, ...]:
    """解析无 shell 命令；参数为页面文本，返回 argv，空值或引号错误时抛错。"""
    if not value.strip():
        raise ValueError("命令不能为空")
    parts = tuple(shlex.split(value, posix=True))
    if not parts:
        raise ValueError("命令不能为空")
    return parts


def build_profiles_from_form(
    *,
    commands: Mapping[AgentName, str],
    models: Mapping[AgentName, str],
    variants: Mapping[AgentName, str],
    native_profiles: Mapping[AgentName, str] | None = None,
    providers: Mapping[AgentName, str] | None = None,
) -> dict[AgentName, AgentProfile]:
    """按 Catalog 把页面输入映射为 Profile。

    参数为 Agent 命令、模型及 Catalog 声明支持的可选字段；返回完整 Profile 映射；
    缺少必需的 Catalog Agent 输入时抛 KeyError。
    """
    profile_values = native_profiles or {}
    provider_values = providers or {}
    return {
        definition.name: AgentProfile(
            command=commands[definition.name],
            model=models[definition.name].strip() or None,
            variant=(
                variants.get(definition.name, "").strip() or None
                if definition.supports_variant
                else None
            ),
            native_profile=(
                profile_values.get(definition.name, "").strip() or None
                if definition.supports_native_profile
                else None
            ),
            provider=(
                provider_values.get(definition.name, "").strip() or None
                if definition.supports_provider
                else None
            ),
        )
        for definition in agent_definitions()
    }


def _git(path: Path, *args: str) -> str:
    """执行只读 Git 查询；参数为路径/参数，返回 stdout，Git 失败时抛错。"""
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "Git 查询失败")
    return result.stdout.strip()


def detect_branch(workspace: Path) -> str:
    """识别当前 Git 分支；参数为工作空间，返回分支名，非仓库或 detached 时抛错。"""
    branch = _git(workspace.expanduser().resolve(), "branch", "--show-current")
    if not branch:
        raise ValueError("工作空间当前不在命名分支上")
    return branch


class RunDraft(BaseModel):
    """保存页面的简化启动输入，并负责构造完整 RunConfig。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: Path
    topic: str = Field(min_length=1)
    state_root: Path
    protocol_version: ReviewProtocolVersion = ReviewProtocolVersion.REVIEW_V1
    issue_ledger_enabled: bool = False
    counterexample_ballot: bool = False
    target_branch: str | None = None
    use_worktree: bool = False
    integrate_to_target: bool = True
    allowed_paths: tuple[str, ...] = (".",)
    protected_paths: tuple[str, ...] = (
        ".git",
        ".agent/debate",
        ".agent/discuss",
        ".env",
    )
    high_impact_paths: tuple[str, ...] = ()
    setup_command_lines: tuple[str, ...] = ()
    validation_command_lines: tuple[str, ...] = ()
    participants: tuple[AgentName, ...] = (
        AgentName.CODEX,
        AgentName.CLAUDE,
    )
    first_agent: AgentName = AgentName.CODEX
    max_turns: int = Field(default=8, gt=0)
    total_timeout_seconds: int = Field(default=7200, gt=0)
    max_process_failures: int = Field(default=2, gt=0)
    profiles: dict[AgentName, AgentProfile] = Field(default_factory=_default_profiles)
    startup_timeout_seconds: int = Field(default=300, gt=0)
    idle_timeout_seconds: int = Field(default=300, gt=0)
    hard_timeout_seconds: int = Field(default=3600, gt=0)
    heartbeat_interval_seconds: int = Field(default=5, gt=0)
    realtime_output_enabled: bool = True
    raw_output_tail_lines: int = Field(default=500, gt=0, le=5000)
    mcp_mode: Literal["minimal", "full"] = "minimal"
    proxy_environment_allowlist: tuple[str, ...] = ()
    facilitator_enabled: bool = True
    facilitator_model: str = "deepseek-v4-flash"
    allow_network: bool = False
    unrestricted_commands: bool = True

    def to_config(self) -> RunConfig:
        """生成冻结运行配置；无参数，返回 RunConfig，路径/Git/命令非法时抛错。"""
        workspace = self.workspace.expanduser().resolve()
        branch = self.target_branch or infer_target_branch(workspace)
        missing_profiles = tuple(
            agent for agent in self.participants if agent not in self.profiles
        )
        if missing_profiles:
            missing_names = "、".join(
                agent_definition(agent).display_name for agent in missing_profiles
            )
            raise ValueError(f"已选参与者 {missing_names} 缺少 Profile")
        profiles = {
            agent: profile.model_copy(
                update={
                    "timeout_seconds": self.hard_timeout_seconds,
                    "startup_timeout_seconds": self.startup_timeout_seconds,
                    "idle_timeout_seconds": self.idle_timeout_seconds,
                    "hard_timeout_seconds": self.hard_timeout_seconds,
                    "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
                }
            )
            for agent, profile in self.profiles.items()
        }
        return RunConfig(
            workspace=workspace,
            topic=self.topic.strip(),
            state_root=self.state_root.expanduser().resolve(),
            protocol_version=self.protocol_version,
            issue_ledger_enabled=self.issue_ledger_enabled,
            counterexample_ballot=self.counterexample_ballot,
            target_branch=branch,
            use_worktree=self.use_worktree,
            integrate_to_target=self.integrate_to_target,
            participants=self.participants,
            first_agent=self.first_agent,
            max_turns=self.max_turns,
            total_timeout_seconds=self.total_timeout_seconds,
            max_process_failures=self.max_process_failures,
            facilitator_enabled=self.facilitator_enabled,
            facilitator_model=self.facilitator_model,
            allow_network=self.allow_network,
            unrestricted_commands=self.unrestricted_commands,
            realtime_output_enabled=self.realtime_output_enabled,
            raw_output_tail_lines=self.raw_output_tail_lines,
            mcp_mode=self.mcp_mode,
            proxy_environment_allowlist=self.proxy_environment_allowlist,
            profiles=profiles,
            allowed_scope=self.allowed_paths,
            protected_scope=self.protected_paths,
            high_impact_scope=self.high_impact_paths,
            setup_commands=tuple(
                parse_command(line) for line in self.setup_command_lines if line.strip()
            ),
            validation_commands=tuple(
                parse_command(line)
                for line in self.validation_command_lines
                if line.strip()
            ),
        )


class PreflightCheck(BaseModel):
    """描述页面预检的一项可操作结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    label: str
    level: str
    detail: str


class WorkspaceInspection(BaseModel):
    """汇总工作空间、CLI 和 Facilitator 启动条件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: str
    branch: str | None
    checks: tuple[PreflightCheck, ...]
    can_start: bool

    def by_key(self, key: str) -> PreflightCheck:
        """按键读取检查项；参数为 key，返回检查，缺失时抛 KeyError。"""
        return next(check for check in self.checks if check.key == key)


def inspect_workspace(
    workspace: Path,
    *,
    expected_branch: str | None = None,
    profiles: Mapping[AgentName, AgentProfile] | None = None,
    participants: tuple[AgentName, ...] = (
        AgentName.CODEX,
        AgentName.CLAUDE,
    ),
    environment: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
) -> WorkspaceInspection:
    """执行只读启动预检。

    参数为路径、分支、Profile、参与者、环境和命令查询器；返回报告；Git 查询异常会转换为
    可操作检查项，不执行工作空间写入。
    """
    resolved = workspace.expanduser().resolve()
    checks: list[PreflightCheck] = []
    branch: str | None = None
    try:
        root = Path(_git(resolved, "rev-parse", "--show-toplevel")).resolve()
        checks.append(
            PreflightCheck(
                key="git_repo",
                label="Git 工作空间",
                level="success",
                detail=str(root),
            )
        )
        branch = detect_branch(root)
        branch_ok = expected_branch is None or branch == expected_branch
        checks.append(
            PreflightCheck(
                key="git_branch",
                label="目标分支",
                level="success" if branch_ok else "error",
                detail=(
                    branch
                    if branch_ok
                    else f"当前 {branch}，期望 {expected_branch}"
                ),
            )
        )
        status = _git(root, "status", "--porcelain", "--untracked-files=all")
        checks.append(
            PreflightCheck(
                key="git_clean",
                label="工作空间状态",
                level="success" if not status else "error",
                detail="干净" if not status else "存在未提交或未跟踪文件",
            )
        )
    except (OSError, ValueError) as exc:
        if resolved.is_dir() and os.access(resolved, os.R_OK | os.W_OK):
            branch = expected_branch or infer_target_branch(resolved)
            checks.extend(
                (
                    PreflightCheck(
                        key="git_repo",
                        label="Git 工作空间",
                        level="warning",
                        detail="尚未初始化；启动时将自动 git init",
                    ),
                    PreflightCheck(
                        key="git_branch",
                        label="目标分支",
                        level="success",
                        detail=branch,
                    ),
                    PreflightCheck(
                        key="git_clean",
                        label="工作空间状态",
                        level="warning",
                        detail="现有非保护文件将提交为初始基线",
                    ),
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    key="git_repo",
                    label="Git 工作空间",
                    level="error",
                    detail=str(exc)[:300],
                )
            )
    active_profiles = profiles or _default_profiles()
    for agent in participants:
        definition = agent_definition(agent)
        command = active_profiles[agent].command
        candidate = Path(command).expanduser()
        location = (
            str(candidate.resolve())
            if candidate.is_absolute()
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
            else which(command)
        )
        checks.append(
            PreflightCheck(
                key=agent.value,
                label=f"{definition.display_name} CLI",
                level="success" if location else "error",
                detail=location or "未找到可执行命令",
            )
        )
    key_ready = bool(environment.get("DEEPSEEK_API_KEY"))
    checks.append(
        PreflightCheck(
            key="deepseek",
            label="DeepSeek 语义服务",
            level="success" if key_ready else "warning",
            detail=(
                "已配置"
                if key_ready
                else "未配置；Facilitator 将降级，协议语义转码不可用"
            ),
        )
    )
    return WorkspaceInspection(
        workspace=str(resolved),
        branch=branch,
        checks=tuple(checks),
        can_start=not any(check.level == "error" for check in checks),
    )


class RunSummary(BaseModel):
    """描述历史列表所需的单个运行摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    workspace: str
    target_branch: str
    integrate_to_target: bool
    topic: str
    status: str
    stop_reason: str | None = None
    phase: str | None = None
    current_turn: int | None = None
    max_turns: int
    created_at: str | None = None
    updated_at: str | None = None


class AgentLiveTrack(BaseModel):
    """描述一个 CLI Agent 的实时展示轨道。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent: AgentName
    events: tuple[InvocationEvent, ...] = ()
    elapsed_seconds: float = Field(default=0, ge=0)
    idle_seconds: float = Field(default=0, ge=0)
    event_count: int = Field(default=0, ge=0)
    changed_file_count: int = Field(default=0, ge=0)
    last_title: str = "等待调用"
    terminal: bool = False
    termination_category: str | None = None


class InvocationLiveView(BaseModel):
    """汇总一个 Run 的增量调用事件和参与 Agent 快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    after_seq: int = Field(ge=0)
    last_seq: int = Field(ge=0)
    events: tuple[InvocationEvent, ...] = ()
    tracks: dict[AgentName, AgentLiveTrack]
    warning: str | None = None


class RawOutputTail(BaseModel):
    """保存一个 raw 输出文件可公开展示的脱敏尾部。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_name: str
    stream: Literal["stdout", "stderr"]
    text: str
    warning: str | None = None


def _live_track(
    agent: AgentName,
    *,
    events: tuple[InvocationEvent, ...],
    progress: InvocationProgress | None,
) -> AgentLiveTrack:
    """构建单 Agent 轨道；参数为 Agent/事件/快照，返回展示模型，不抛异常。"""
    agent_events = tuple(event for event in events if event.agent is agent)
    snapshot: AgentInvocationProgress | None = (
        progress.agents.get(agent) if progress is not None else None
    )
    if snapshot is None:
        return AgentLiveTrack(agent=agent, events=agent_events)
    return AgentLiveTrack(
        agent=agent,
        events=agent_events,
        elapsed_seconds=snapshot.elapsed_seconds,
        idle_seconds=snapshot.idle_seconds,
        event_count=snapshot.event_count,
        changed_file_count=snapshot.changed_file_count,
        last_title=snapshot.last_title,
        terminal=snapshot.terminal,
        termination_category=snapshot.termination_category,
    )


def read_invocation_live_view(
    *,
    state_root: Path,
    run_id: str,
    after_seq: int,
    limit: int,
) -> InvocationLiveView:
    """增量读取调用级页面数据。

    参数为状态根、Run ID、已消费游标和窗口上限；返回 Catalog 轨道展示模型；文件尚未
    生成、尾行未写完时安全返回，完整损坏记录以 warning 降级。
    """
    store = InvocationEventStore(state_root=state_root, run_id=run_id)
    warning: str | None = None
    try:
        progress = store.read_progress()
    except (OSError, ValueError) as exc:
        progress = None
        warning = f"调用进度暂不可读: {str(exc)[:200]}"
    try:
        events = tuple(store.read_events(after_seq=after_seq, limit=limit))
    except (OSError, ValueError) as exc:
        events = ()
        warning = f"调用事件暂不可读: {str(exc)[:200]}"
    last_seq = max((event.seq for event in events), default=after_seq)
    return InvocationLiveView(
        run_id=run_id,
        after_seq=after_seq,
        last_seq=last_seq,
        events=events,
        tracks={
            definition.name: _live_track(
                definition.name, events=events, progress=progress
            )
            for definition in agent_definitions()
        },
        warning=warning,
    )


def read_raw_output_tails(
    *,
    state_root: Path,
    run_id: str,
    redactor: Redactor,
    lines: int,
    file_limit: int = 8,
) -> tuple[RawOutputTail, ...]:
    """读取最近 raw 输出的脱敏尾部。

    参数为状态根、Run ID、脱敏器、行数和文件上限；返回按新到旧排列的尾部；
    文件缺失或读取中消失时安全跳过，参数非法时抛 ValueError。
    """
    if lines <= 0:
        raise ValueError("lines 必须为正数")
    if file_limit <= 0:
        raise ValueError("file_limit 必须为正数")
    store = InvocationEventStore(state_root=state_root, run_id=run_id)
    raw_dir = store.run_dir / "raw"
    if not raw_dir.is_dir():
        return ()
    try:
        candidates = [
            path
            for path in raw_dir.iterdir()
            if path.is_file()
            and (
                path.name.endswith(".stdout.log")
                or path.name.endswith(".stderr.log")
            )
        ]
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return ()
    tails: list[RawOutputTail] = []
    for path in candidates[:file_limit]:
        stream: Literal["stdout", "stderr"] = (
            "stdout" if path.name.endswith(".stdout.log") else "stderr"
        )
        try:
            content = read_redacted_tail(path, redactor=redactor, lines=lines)
        except OSError:
            continue
        tails.append(
            RawOutputTail(
                file_name=path.name,
                stream=stream,
                text=content,
            )
        )
    return tuple(tails)


def list_run_summaries(state_root: Path) -> list[RunSummary]:
    """读取历史运行；参数为状态根，返回新到旧摘要，损坏目录会被跳过。"""
    runs_root = state_root.expanduser().resolve() / "runs"
    if not runs_root.exists():
        return []
    summaries: list[RunSummary] = []
    run_dirs = sorted(
        runs_root.iterdir(), key=lambda path: path.name, reverse=True
    )
    for run_dir in run_dirs:
        if not run_dir.is_dir():
            continue
        try:
            store = ArtifactStore.open(state_root=state_root, run_id=run_dir.name)
            config = store.load_config()
            status = store.read_status()
            progress = RunEventStore(
                state_root=state_root, run_id=run_dir.name
            ).read_progress()
            summaries.append(
                RunSummary(
                    run_id=run_dir.name,
                    workspace=str(config.workspace),
                    target_branch=config.target_branch,
                    integrate_to_target=config.integrate_to_target,
                    topic=config.topic,
                    status=str(status.get("status", "unknown")),
                    stop_reason=(
                        str(status["stop_reason"])
                        if status.get("stop_reason") is not None
                        else None
                    ),
                    phase=progress.phase if progress else None,
                    current_turn=progress.current_turn if progress else None,
                    max_turns=config.max_turns,
                    created_at=(
                        str(status["created_at"])
                        if status.get("created_at") is not None
                        else None
                    ),
                    updated_at=(
                        str(status["updated_at"])
                        if status.get("updated_at") is not None
                        else None
                    ),
                )
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return summaries
