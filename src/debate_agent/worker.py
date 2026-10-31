"""独立执行 Debate Run 的后台 Worker 入口。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from debate_agent.artifacts import ArtifactStore
from debate_agent.config import RunConfig
from debate_agent.events import RunEventStore
from debate_agent.models import RunStatus
from debate_agent.runtime import DebateLifecycleService


class WorkerLifecycle(Protocol):
    """声明 Worker 所需的 Debate 生命周期接口。"""

    def run_with_id(
        self, config: RunConfig, *, run_id: str
    ) -> dict[str, object]:
        """执行预分配 run；参数为配置/run，返回终态。"""
        raise RuntimeError("WorkerLifecycle 未实现 run_with_id")

    def resume(self, *, state_root: Path, run_id: str) -> dict[str, object]:
        """恢复 run；参数为状态根/run，返回终态。"""
        raise RuntimeError("WorkerLifecycle 未实现 resume")


def build_parser() -> argparse.ArgumentParser:
    """创建 Worker 参数解析器；无参数，返回 parser，不执行运行。"""
    parser = argparse.ArgumentParser(prog="debate-agent-worker")
    parser.add_argument("mode", choices=("run", "resume"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    lifecycle: WorkerLifecycle | None = None,
) -> int:
    """执行后台 run；参数为 argv/生命周期，返回退出码，异常会写入脱敏状态。"""
    args = build_parser().parse_args(argv)
    state_root = args.state_root.expanduser().resolve()
    store = ArtifactStore.open(state_root=state_root, run_id=args.run_id)
    service = lifecycle or DebateLifecycleService()
    try:
        if args.mode == "run":
            result = service.run_with_id(store.load_config(), run_id=args.run_id)
        else:
            result = service.resume(state_root=state_root, run_id=args.run_id)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:500]}"
        store.write_diagnostic(phase="worker", error=reason)
        store.write_status(RunStatus.FAILED, stop_reason=reason)
        RunEventStore(state_root=state_root, run_id=args.run_id).publish(
            kind="worker_failed",
            phase="stopped",
            title="后台执行失败",
            summary=reason,
            status=RunStatus.FAILED.value,
        )
        return 2
    status = str(result.get("status", RunStatus.FAILED.value))
    completed = status == RunStatus.COMPLETED.value
    RunEventStore(state_root=state_root, run_id=args.run_id).publish(
        kind="worker_finished",
        phase="completed" if completed else "stopped",
        title="讨论运行完成" if completed else "讨论运行已停止",
        summary=str(result.get("stop_reason") or ""),
        status=status,
    )
    return 0 if completed else 3


if __name__ == "__main__":
    raise SystemExit(main())
