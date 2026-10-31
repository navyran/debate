"""命令行参数与基础状态查询。"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Callable, MutableMapping, Sequence
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from debate_agent.agent_catalog import agent_definitions
from debate_agent.config import AgentProfile, RunConfig, infer_target_branch
from debate_agent.models import AgentName, ReviewProtocolVersion, RunStatus
from debate_agent.secrets import load_default_deepseek_key

DEFAULT_CLI_DIRECTORIES = (
    Path("/Applications/ServBay/package/node/current/bin"),
    Path("/Applications/ChatGPT.app/Contents/Resources"),
    Path("~/.local/bin"),
    Path("~/.volta/bin"),
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
)


def _extend_cli_path(
    *,
    environment: MutableMapping[str, str] | None = None,
    candidate_directories: Sequence[Path] = DEFAULT_CLI_DIRECTORIES,
) -> tuple[str, ...]:
    """补充 CLI 搜索路径。

    参数为可选环境和候选目录；返回实际追加的目录；目录无可执行 Agent 时跳过，
    不执行 shell，也不覆盖已有 PATH。
    """
    target = environment if environment is not None else os.environ
    path_entries = [
        item
        for item in target.get("PATH", os.defpath).split(os.pathsep)
        if item
    ]
    appended: list[str] = []
    for candidate in candidate_directories:
        directory = candidate.expanduser()
        text = str(directory)
        if text in path_entries or not directory.is_dir():
            continue
        has_cli = any(
            (directory / command).is_file()
            and os.access(directory / command, os.X_OK)
            for command in (
                definition.default_command for definition in agent_definitions()
            )
        )
        if not has_cli:
            continue
        path_entries.append(text)
        appended.append(text)
    target["PATH"] = os.pathsep.join(path_entries)
    return tuple(appended)


class LifecycleService:
    """声明 CLI 使用的 run/resume/abort/cleanup 生命周期接口。"""

    def run(self, config: RunConfig) -> dict[str, object]:
        """启动 run；参数为冻结配置，返回状态字典，失败时抛运行时异常。"""
        raise RuntimeError("LifecycleService 未实现 run")

    def resume(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        """恢复 run；参数为状态根/ID，返回状态字典，失败时抛运行时异常。"""
        raise RuntimeError("LifecycleService 未实现 resume")

    def abort(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        """中止 run；参数为状态根/ID，返回状态字典，失败时抛运行时异常。"""
        raise RuntimeError("LifecycleService 未实现 abort")

    def cleanup(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        """清理 run；参数为状态根/ID，返回状态字典，失败时抛运行时异常。"""
        raise RuntimeError("LifecycleService 未实现 cleanup")


def _add_state_root(
    parser: argparse.ArgumentParser, *, default: Path = Path("agent/debate/.runtime")
) -> None:
    """向子命令加入状态目录参数；参数为 parser/默认路径，无返回值。"""
    parser.add_argument(
        "--state-root",
        type=Path,
        default=default,
        help="运行状态根目录",
    )


def build_parser() -> argparse.ArgumentParser:
    """构造完整 CLI parser；无参数，返回 parser，配置错误时由 argparse 抛错。"""
    definitions = agent_definitions()
    parser = argparse.ArgumentParser(prog="debate-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="启动新的 Debate Run")
    run_parser.add_argument("--workspace", type=Path, required=True)
    topic_group = run_parser.add_mutually_exclusive_group(required=True)
    topic_group.add_argument("--topic")
    topic_group.add_argument("--topic-file", type=Path)
    run_parser.add_argument(
        "--target-branch",
        help="目标分支；留空时自动使用工作空间当前分支",
    )
    run_parser.add_argument("--base-ref")
    worktree_group = run_parser.add_mutually_exclusive_group()
    worktree_group.add_argument(
        "--worktree",
        action="store_true",
        dest="use_worktree",
        help="创建独立 worktree 运行",
    )
    worktree_group.add_argument(
        "--no-worktree",
        action="store_false",
        dest="use_worktree",
        help="在原工作空间的临时 debate 分支中执行（默认）",
    )
    run_parser.set_defaults(use_worktree=False)
    run_parser.add_argument(
        "--participant",
        action="append",
        choices=tuple(definition.name.value for definition in definitions),
        help="参与讨论的 CLI Agent；可重复，默认 codex + claude",
    )
    run_parser.add_argument(
        "--first-agent",
        choices=tuple(definition.name.value for definition in definitions),
        default=definitions[0].name.value,
    )
    run_parser.add_argument("--max-turns", type=int, default=8)
    run_parser.add_argument("--total-timeout-seconds", type=int, default=7200)
    run_parser.add_argument("--max-process-failures", type=int, default=2)
    run_parser.add_argument(
        "--protocol-version",
        choices=tuple(version.value for version in ReviewProtocolVersion),
        default=ReviewProtocolVersion.REVIEW_V1.value,
        help="新 Run 固定使用的审查协议版本（默认 review-v1）",
    )
    run_parser.add_argument(
        "--issue-ledger",
        action="store_true",
        help="仅在 review-v1 中显式启用三状态 Issue Ledger（默认关闭）",
    )
    run_parser.add_argument(
        "--counterexample-ballot",
        action="store_true",
        help=(
            "仅在 review-v1 中显式要求最终 Blind Ballot 给出最强反例及处理说明"
            "（默认关闭）"
        ),
    )
    run_parser.add_argument("--format-retries", type=int, choices=(0, 1), default=1)
    run_parser.add_argument("--stall-turns", type=int, default=2)
    run_parser.add_argument(
        "--no-facilitator",
        action="store_false",
        dest="facilitator_enabled",
        help="关闭 DeepSeek Facilitator 并使用确定性路由",
    )
    run_parser.add_argument("--facilitator-model", default="deepseek-v4-flash")
    run_parser.add_argument(
        "--facilitator-base-url", default="https://api.deepseek.com"
    )
    run_parser.add_argument("--facilitator-timeout-seconds", type=int, default=120)
    for definition in definitions:
        agent = definition.name.value
        run_parser.add_argument(
            f"--{agent}-command", default=definition.default_command
        )
        run_parser.add_argument(f"--{agent}-model")
        if definition.supports_variant:
            run_parser.add_argument(
                f"--{agent}-variant",
                help=f"{definition.display_name} 的可选执行 variant",
            )
        if definition.supports_native_profile:
            run_parser.add_argument(
                f"--{agent}-native-profile",
                help=f"{definition.display_name} 的原生 Agent/Profile",
            )
        if definition.supports_provider:
            run_parser.add_argument(
                f"--{agent}-provider",
                help=f"{definition.display_name} 的显式 Provider",
            )
        run_parser.add_argument(
            f"--{agent}-timeout-seconds",
            type=int,
            help="历史单次硬上限参数；指定时覆盖新的 hard timeout",
        )
        run_parser.add_argument(
            f"--{agent}-startup-timeout-seconds", type=int, default=300
        )
        run_parser.add_argument(
            f"--{agent}-idle-timeout-seconds", type=int, default=300
        )
        run_parser.add_argument(
            f"--{agent}-hard-timeout-seconds", type=int, default=3600
        )
        run_parser.add_argument(
            f"--{agent}-heartbeat-interval-seconds", type=int, default=5
        )
    run_parser.add_argument(
        "--mcp-mode", choices=("minimal", "full"), default="minimal"
    )
    run_parser.add_argument(
        "--proxy-env",
        action="append",
        choices=(
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        ),
        default=[],
    )
    run_parser.add_argument(
        "--realtime-output",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    run_parser.add_argument("--raw-output-tail-lines", type=int, default=500)
    run_parser.add_argument("--setup-command-json", action="append", default=[])
    run_parser.add_argument("--validation-command-json", action="append", default=[])
    run_parser.add_argument("--allow-network", action="store_true")
    run_parser.add_argument(
        "--unrestricted-commands",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="允许可信 Agent 执行任意命令（默认开启）",
    )
    run_parser.add_argument(
        "--codex-resume-sandbox-verified",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="复用本次 run 首次创建的 Codex Session（默认开启）",
    )
    run_parser.add_argument("--env", action="append", default=[])
    run_parser.add_argument("--allow-path", action="append", default=[])
    run_parser.add_argument("--protect-path", action="append", default=[])
    run_parser.add_argument("--high-impact-path", action="append", default=[])
    _add_state_root(run_parser)

    for command in ("resume", "status", "abort", "cleanup"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--run-id", required=True)
        _add_state_root(command_parser)
    ui_parser = subparsers.add_parser("ui", help="启动本地 Streamlit 控制中心")
    ui_parser.add_argument("--port", type=int, default=8502)
    _add_state_root(
        ui_parser, default=Path(__file__).resolve().parents[2] / ".runtime"
    )
    return parser


def launch_ui(host: str, port: int, state_root: Path) -> int:
    """启动本地 Streamlit；参数为地址/端口/状态根，返回退出码，依赖缺失时抛错。"""
    if not 1 <= port <= 65535:
        raise ValueError("UI 端口必须在 1 到 65535 之间")
    try:
        streamlit_cli = importlib.import_module("streamlit.web.cli")
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 UI 依赖，请安装 debate-agent[ui]") from exc
    app_path = Path(__file__).resolve().parent / "ui" / "app.py"
    os.environ["DISCUSS_AGENT_STATE_ROOT"] = str(state_root.expanduser().resolve())
    original_argv = sys.argv[:]
    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        host,
        "--server.port",
        str(port),
        "--server.headless",
        "true",
    ]
    try:
        return int(streamlit_cli.main())
    finally:
        sys.argv = original_argv


def _load_status(state_root: Path, run_id: str) -> dict[str, object]:
    """读取持久化状态。

    参数为状态根目录和 run ID，返回 JSON 字典，文件缺失或非法时抛异常。
    """
    status_path = state_root.expanduser().resolve() / "runs" / run_id / "status.json"
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("status.json 顶层必须是对象")
    return cast(dict[str, object], payload)


def _validate_run(args: argparse.Namespace) -> RunConfig:
    """把 run 参数转换为冻结配置。

    参数为 argparse 结果，返回 RunConfig，输入非法时抛错。
    """

    def parse_commands(
        values: Sequence[str], option: str
    ) -> tuple[tuple[str, ...], ...]:
        """解析 JSON argv；参数为文本和选项名，返回命令元组，非字符串数组时抛错。"""
        commands: list[tuple[str, ...]] = []
        for value in values:
            payload = json.loads(value)
            if (
                not isinstance(payload, list)
                or not payload
                or not all(isinstance(part, str) and part for part in payload)
            ):
                raise ValueError(f"{option} 必须是非空 JSON 字符串数组")
            commands.append(tuple(payload))
        return tuple(commands)

    target_branch = args.target_branch
    if target_branch is None:
        target_branch = infer_target_branch(args.workspace)

    definitions = agent_definitions()
    participants = tuple(
        AgentName(value)
        for value in (
            args.participant
            or [definition.name.value for definition in definitions[:2]]
        )
    )
    profiles = {}
    for definition in definitions:
        option_prefix = definition.name.value.replace("-", "_")
        legacy_timeout = getattr(args, f"{option_prefix}_timeout_seconds")
        hard_timeout = getattr(args, f"{option_prefix}_hard_timeout_seconds")
        effective_hard_timeout = legacy_timeout or hard_timeout
        profiles[definition.name] = AgentProfile(
            command=getattr(args, f"{option_prefix}_command"),
            model=getattr(args, f"{option_prefix}_model"),
            variant=(
                getattr(args, f"{option_prefix}_variant")
                if definition.supports_variant
                else None
            ),
            native_profile=(
                getattr(args, f"{option_prefix}_native_profile")
                if definition.supports_native_profile
                else None
            ),
            provider=(
                getattr(args, f"{option_prefix}_provider")
                if definition.supports_provider
                else None
            ),
            timeout_seconds=effective_hard_timeout,
            startup_timeout_seconds=getattr(
                args, f"{option_prefix}_startup_timeout_seconds"
            ),
            idle_timeout_seconds=getattr(
                args, f"{option_prefix}_idle_timeout_seconds"
            ),
            hard_timeout_seconds=effective_hard_timeout,
            heartbeat_interval_seconds=getattr(
                args, f"{option_prefix}_heartbeat_interval_seconds"
            ),
        )
    overrides: dict[str, object] = {
        "target_branch": target_branch,
        "base_ref": args.base_ref,
        "use_worktree": args.use_worktree,
        "participants": participants,
        "first_agent": args.first_agent,
        "max_turns": args.max_turns,
        "total_timeout_seconds": args.total_timeout_seconds,
        "max_process_failures": args.max_process_failures,
        "protocol_version": ReviewProtocolVersion(args.protocol_version),
        "issue_ledger_enabled": args.issue_ledger,
        "counterexample_ballot": args.counterexample_ballot,
        "format_retries": args.format_retries,
        "stall_turns": args.stall_turns,
        "facilitator_enabled": args.facilitator_enabled,
        "facilitator_model": args.facilitator_model,
        "facilitator_base_url": args.facilitator_base_url,
        "facilitator_timeout_seconds": args.facilitator_timeout_seconds,
        "state_root": args.state_root,
        "setup_commands": parse_commands(
            args.setup_command_json, "--setup-command-json"
        ),
        "validation_commands": parse_commands(
            args.validation_command_json, "--validation-command-json"
        ),
        "allow_network": args.allow_network,
        "unrestricted_commands": args.unrestricted_commands,
        "realtime_output_enabled": args.realtime_output,
        "raw_output_tail_lines": args.raw_output_tail_lines,
        "mcp_mode": args.mcp_mode,
        "proxy_environment_allowlist": tuple(args.proxy_env),
        "codex_resume_sandbox_verified": args.codex_resume_sandbox_verified,
        "profiles": profiles,
    }
    if args.env:
        defaults = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
        overrides["environment_allowlist"] = tuple(
            dict.fromkeys((*defaults, *args.env))
        )
    if args.allow_path:
        overrides["allowed_scope"] = tuple(args.allow_path)
    if args.protect_path:
        overrides["protected_scope"] = tuple(args.protect_path)
    if args.high_impact_path:
        overrides["high_impact_scope"] = tuple(args.high_impact_path)
    return RunConfig.from_sources(
        workspace=args.workspace,
        topic=args.topic,
        topic_file=args.topic_file,
        **overrides,
    )


def _default_service() -> LifecycleService:
    """延迟创建真实生命周期服务；无参数，返回服务，导入失败时传播异常。"""
    from debate_agent.runtime import DebateLifecycleService

    return DebateLifecycleService()


def _exit_code(payload: dict[str, object], *, lifecycle_command: str) -> int:
    """映射生命周期结果；参数为状态和命令，返回进程退出码，不抛异常。"""
    if lifecycle_command in {"abort", "cleanup"}:
        return 0
    return 0 if payload.get("status") == RunStatus.COMPLETED.value else 3


def main(
    argv: Sequence[str] | None = None,
    *,
    service: LifecycleService | None = None,
    ui_launcher: Callable[[str, int, Path], int] = launch_ui,
) -> int:
    """执行 CLI；参数为 argv/可注入服务，返回退出码，输入和运行错误写 stderr。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "ui":
            return ui_launcher(
                "127.0.0.1", args.port, args.state_root.expanduser().resolve()
            )
        if args.command == "status":
            payload = _load_status(args.state_root, args.run_id)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "run":
            lifecycle = service or _default_service()
            payload = lifecycle.run(_validate_run(args))
        else:
            lifecycle = service or _default_service()
            method = getattr(lifecycle, args.command)
            payload = method(
                state_root=args.state_root.expanduser().resolve(), run_id=args.run_id
            )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return _exit_code(payload, lifecycle_command=args.command)
    except (
        OSError,
        RuntimeError,
        ValueError,
        ValidationError,
        json.JSONDecodeError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def entrypoint() -> None:
    """作为 console script 退出进程；无参数和返回值，使用 main 的退出码终止。"""
    try:
        load_default_deepseek_key()
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"默认 DeepSeek Key 加载失败: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    _extend_cli_path()
    raise SystemExit(main())
