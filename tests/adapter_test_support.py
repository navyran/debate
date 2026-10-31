"""test_adapters.py 的共享测试桩与断言辅助函数。"""

from __future__ import annotations

from debate_agent.models import AgentResult, TurnResult
from debate_agent.process import ProcessResult
from debate_agent.protocol_gateway import (
    ProtocolBinding,
    ProtocolGateway,
    VendorAdapter,
)


def process_result(
    stdout: str, *, return_code: int = 0, stderr: str = ""
) -> ProcessResult:
    """构造 Adapter 进程结果；参数为输出/退出码，返回结果，不抛异常。"""
    return ProcessResult(
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        elapsed_seconds=0.2,
        raw_output_ref="raw/INV.json",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        cancelled=False,
    )

def parse_adapter_result(
    adapter: VendorAdapter,
    result: ProcessResult,
    *,
    response_model: type[TurnResult] = TurnResult,
    turn_id: str = "TURN-1",
    session_generation: int = 1,
) -> AgentResult:
    """经 ProtocolGateway 解析 Adapter 终止结果；参数为调用上下文，返回统一结果。"""
    return ProtocolGateway().parse_result(
        adapter=adapter,
        result=result,
        response_model=response_model,
        binding=ProtocolBinding(
            agent=adapter.agent,
            turn_id=turn_id,
            head_sha="abc",
            goal_contract_version=1,
        ),
        session_generation=session_generation,
    )

def turn_payload(agent: str = "codex") -> dict[str, object]:
    return {
        "agent": agent,
        "turn_id": "TURN-1",
        "base_commit": "abc",
        "observed_head": "abc",
        "goal_contract_version": 1,
        "decision": "accept",
        "summary": "done",
        "accepted_points": [],
        "issues": [],
        "requested_scope_additions": [],
        "requested_validation_additions": [],
        "completion_assessment": "complete",
    }
