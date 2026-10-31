"""CLI 终止内容的严格解析、确定性修复、语义转码与不变量绑定。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from debate_agent.adapters import AdapterOutputError, VendorTerminal
from debate_agent.models import AgentName, AgentResult, Ballot
from debate_agent.process import ProcessResult
from debate_agent.review_models import ReviewResult

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class ProtocolMethod(str, Enum):
    """标识协议载荷采用的恢复方式。"""

    STRICT = "strict"
    DETERMINISTIC = "deterministic"
    DEEPSEEK = "deepseek"


@dataclass(frozen=True, slots=True)
class ProtocolBinding:
    """保存 Supervisor 掌握的不可变协议事实。"""

    agent: AgentName
    turn_id: str
    head_sha: str
    goal_contract_version: int

    def as_fields(self) -> dict[str, object]:
        """展开可绑定字段；无参数，返回字段映射，不抛异常。"""
        return {
            "agent": self.agent.value,
            "turn_id": self.turn_id,
            "head_sha": self.head_sha,
            "base_commit": self.head_sha,
            "observed_head": self.head_sha,
            "commit_sha": self.head_sha,
            "goal_contract_version": self.goal_contract_version,
        }


class ProtocolNormalizer(Protocol):
    """声明只读语义转码器的最小接口。"""

    def normalize(
        self,
        *,
        raw_text: str,
        response_model: type[ResponseModel],
        binding: ProtocolBinding,
        preserved_claims: Mapping[str, object],
    ) -> Mapping[str, Any]:
        """转码原始文本；参数为文本/Schema/绑定，返回候选载荷，失败时抛错。"""
        raise RuntimeError("ProtocolNormalizer 未实现 normalize")


class VendorAdapter(Protocol):
    """声明 ProtocolGateway 依赖的 Vendor Adapter 边界。"""

    agent: AgentName

    def extract_terminal(self, result: ProcessResult) -> VendorTerminal:
        """提取终止内容；参数为进程结果，返回终止对象，厂商协议错误时抛错。"""
        raise AdapterOutputError("VendorAdapter 未实现 extract_terminal")

    def failure_detail(self, stdout: str) -> str | None:
        """提取失败详情；参数为 stdout，返回摘要或空，不抛异常。"""
        return None


class ProtocolInvariantError(ValueError):
    """表示候选协议改写了 Supervisor 或原 Agent 的不可变事实。"""


class ProtocolNormalizationError(RuntimeError):
    """表示语义转码器不可用或没有返回可绑定的协议。"""


class InvariantBinder:
    """绑定 Supervisor 事实并保护 Agent 的业务结论不被转码器改写。"""

    semantic_fields = (
        "decision",
        "completion_assessment",
        "completion",
        "accepted",
    )

    @classmethod
    def preserved_claims(
        cls,
        raw_text: str,
        response_model: type[BaseModel],
    ) -> dict[str, object]:
        """提取原输出中的业务结论。

        参数为原文和目标模型；返回可明确识别的枚举/布尔字段，不推断缺失值。
        """
        claims: dict[str, object] = {}
        for field in cls.semantic_fields:
            if field not in response_model.model_fields:
                continue
            matches = re.finditer(
                rf'"{re.escape(field)}"\s*:\s*'
                r'(?P<value>"(?:\\.|[^"\\])*"|true|false|null)',
                raw_text,
            )
            values: list[object] = []
            for match in matches:
                try:
                    value = json.loads(match.group("value"))
                except json.JSONDecodeError:
                    continue
                if value not in values:
                    values.append(value)
            if len(values) == 1:
                claims[field] = values[0]
        return claims

    @classmethod
    def bind(
        cls,
        payload: Mapping[str, Any],
        *,
        response_model: type[BaseModel],
        binding: ProtocolBinding,
        preserved_claims: Mapping[str, object],
        require_semantic_claims: bool,
    ) -> dict[str, Any]:
        """校验并复制候选协议。

        参数为载荷/Schema/绑定/原结论；返回普通字典；事实不一致或语义缺失时抛错。
        """
        fields = response_model.model_fields
        candidate = dict(payload)
        for field, expected in binding.as_fields().items():
            if field not in fields:
                continue
            if field in candidate:
                actual = candidate[field]
                actual_value = getattr(actual, "value", actual)
                if actual_value != expected:
                    raise ProtocolInvariantError(
                        f"协议字段 {field} 与 Supervisor 事实不一致"
                    )
            candidate[field] = expected
        for field in cls.semantic_fields:
            if field not in fields:
                continue
            if field not in preserved_claims:
                if require_semantic_claims:
                    raise ProtocolInvariantError(f"原输出缺少明确业务结论字段 {field}")
                continue
            actual = candidate.get(field)
            actual_value = getattr(actual, "value", actual)
            if actual_value != preserved_claims[field]:
                raise ProtocolInvariantError(f"转码结果改写了业务结论字段 {field}")
        return candidate


class ProtocolGateway:
    """把 Vendor 终止内容转换为经绑定和 Schema 校验的 AgentResult。"""

    def __init__(
        self,
        *,
        normalizer: ProtocolNormalizer | None = None,
        counterexample_ballot_enabled: bool = False,
    ) -> None:
        """创建 Gateway；参数为可选转码器和冻结反例投票开关，返回实例。

        反例投票启用时，Ballot 的反例字段必须来自原始 CLI 的严格或确定性 JSON
        解析，不能由语义转码器补造；实例化不执行模型调用。
        """
        self.normalizer = normalizer
        self.counterexample_ballot_enabled = counterexample_ballot_enabled

    @staticmethod
    def _strict_payload(content: object) -> dict[str, Any]:
        """严格解析内容；参数为终止内容，返回对象，非严格 JSON 时抛错。"""
        payload = json.loads(content) if isinstance(content, str) else content
        if not isinstance(payload, dict):
            raise TypeError("CLI result 不是对象")
        return payload

    @staticmethod
    def _quote_closes_json_string(payload: str, index: int) -> bool:
        """判断引号是否结束 JSON 字符串；参数为文本/位置，返回布尔值。"""
        cursor = index + 1
        while cursor < len(payload) and payload[cursor].isspace():
            cursor += 1
        if cursor >= len(payload) or payload[cursor] in ":}]":
            return True
        if payload[cursor] != ",":
            return False
        cursor += 1
        while cursor < len(payload) and payload[cursor].isspace():
            cursor += 1
        if cursor >= len(payload):
            return True
        return payload[cursor] in '"{[-0123456789tfn'

    @classmethod
    def _repair_unescaped_quotes(cls, payload: str) -> str:
        """修复文本字段中的裸双引号；参数为候选 JSON，返回有限语法修复结果。"""
        repaired: list[str] = []
        in_string = False
        escaped = False
        for index, char in enumerate(payload):
            if escaped:
                repaired.append(char)
                escaped = False
                continue
            if in_string and char == "\\":
                repaired.append(char)
                escaped = True
                continue
            if char != '"':
                repaired.append(char)
                continue
            if not in_string:
                in_string = True
                repaired.append(char)
            elif cls._quote_closes_json_string(payload, index):
                in_string = False
                repaired.append(char)
            else:
                repaired.append('\\"')
        return "".join(repaired)

    @classmethod
    def _deterministic_payload(cls, content: object) -> dict[str, Any]:
        """确定性修复终止内容；参数为内容，返回对象，无法修复时抛解码错误。"""
        if not isinstance(content, str):
            raise TypeError("非文本内容不适用确定性修复")
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < start:
            raise json.JSONDecodeError("未找到 JSON 对象", content, 0)
        candidate = content[start : end + 1]
        try:
            return cls._strict_payload(candidate)
        except json.JSONDecodeError:
            return cls._strict_payload(cls._repair_unescaped_quotes(candidate))

    @staticmethod
    def _raw_text(content: object) -> str:
        """序列化终止内容；参数为任意内容，返回稳定文本，不抛异常。"""
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _validated(
        payload: Mapping[str, Any],
        *,
        response_model: type[ResponseModel],
        binding: ProtocolBinding,
        preserved_claims: Mapping[str, object],
        require_semantic_claims: bool,
    ) -> ResponseModel:
        """绑定并校验载荷；参数为候选和约束，返回模型，冲突或 Schema 错误时抛错。"""
        bound = InvariantBinder.bind(
            payload,
            response_model=response_model,
            binding=binding,
            preserved_claims=preserved_claims,
            require_semantic_claims=require_semantic_claims,
        )
        return response_model.model_validate(bound)

    def _decode_payload(
        self,
        content: object,
        *,
        response_model: type[ResponseModel],
        binding: ProtocolBinding,
        normalizer_text: Callable[[str], str] | None,
    ) -> tuple[ResponseModel, ProtocolMethod]:
        """执行三层协议恢复；参数为内容/Schema/绑定/脱敏器，返回模型和方式，失败时抛错。"""
        raw_text = self._raw_text(content)
        claims = InvariantBinder.preserved_claims(raw_text, response_model)
        last_error: Exception | None = None
        for method, decoder in (
            (ProtocolMethod.STRICT, self._strict_payload),
            (ProtocolMethod.DETERMINISTIC, self._deterministic_payload),
        ):
            try:
                payload = decoder(content)
                return (
                    self._validated(
                        payload,
                        response_model=response_model,
                        binding=binding,
                        preserved_claims=claims,
                        require_semantic_claims=False,
                    ),
                    method,
                )
            except ProtocolInvariantError:
                raise
            except (json.JSONDecodeError, TypeError, ValidationError) as exc:
                last_error = exc
        if self.normalizer is not None and response_model is ReviewResult:
            # Review 的 Coverage、Finding 和 EvidenceRef 是协议的权威业务内容。
            # 确定性 JSON 修复之外的 LLM 转码无法证明其没有补造这些字段，因此
            # review-v1 在这里明确失败，不能用“格式恢复”掩盖语义缺失。
            last_error = ProtocolNormalizationError(
                "review_semantics_require_deterministic_parse"
            )
        elif (
            self.normalizer is not None
            and response_model is Ballot
            and self.counterexample_ballot_enabled
        ):
            # 反例字段只在显式 opt-in 的 review-v1 Run 中成为必填项。若让
            # DeepSeek 转码补齐它们，损坏的原始终端输出可能被伪装为合格投票。
            # 因此此处拒绝，让 Runtime 走已有格式重试/新 Session 路径。
            last_error = ProtocolNormalizationError(
                "counterexample_ballot_requires_deterministic_parse"
            )
        elif self.normalizer is not None:
            try:
                normalized_input = (
                    normalizer_text(raw_text)
                    if normalizer_text is not None
                    else raw_text
                )
                payload = dict(
                    self.normalizer.normalize(
                        raw_text=normalized_input,
                        response_model=response_model,
                        binding=binding,
                        preserved_claims=claims,
                    )
                )
                return (
                    self._validated(
                        payload,
                        response_model=response_model,
                        binding=binding,
                        preserved_claims=claims,
                        require_semantic_claims=True,
                    ),
                    ProtocolMethod.DEEPSEEK,
                )
            except ProtocolInvariantError:
                raise
            except Exception as exc:
                last_error = exc
        if last_error is None:
            raise AdapterOutputError("CLI 结构化输出无效")
        if isinstance(last_error, ProtocolNormalizationError):
            raise AdapterOutputError(
                f"CLI 结构化输出无效；语义转码失败: {last_error}"
            ) from last_error
        raise AdapterOutputError("CLI 结构化输出无效") from last_error

    def parse_result(
        self,
        *,
        adapter: VendorAdapter,
        result: ProcessResult,
        response_model: type[ResponseModel],
        binding: ProtocolBinding,
        session_generation: int,
        normalizer_text: Callable[[str], str] | None = None,
    ) -> AgentResult:
        """解析统一结果。

        参数为 Adapter/进程结果/Schema/绑定/代次/脱敏器；返回 AgentResult；
        协议错误时抛错。
        """
        if result.return_code != 0 or result.timed_out or result.cancelled:
            if result.timed_out:
                error = "CLI 调用超时"
            elif result.cancelled:
                error = "CLI 调用已取消"
            else:
                error = f"CLI 退出码 {result.return_code}"
                detail = adapter.failure_detail(result.stdout)
                if not detail:
                    detail = " ".join(result.stderr.split())[:300] or None
                if detail:
                    error = f"{error}: {detail}"
            return AgentResult(
                ok=False,
                agent=adapter.agent,
                session_generation=session_generation,
                turn_id=binding.turn_id,
                return_code=result.return_code,
                elapsed_seconds=result.elapsed_seconds,
                raw_output_ref=result.raw_output_ref,
                error=error,
            )
        terminal = adapter.extract_terminal(result)
        if not terminal.session_id:
            raise AdapterOutputError("CLI 响应缺少明确 session ID")
        try:
            validated, method = self._decode_payload(
                terminal.content,
                response_model=response_model,
                binding=binding,
                normalizer_text=normalizer_text,
            )
        except ProtocolInvariantError as exc:
            raise AdapterOutputError("CLI 结构化输出违反运行绑定") from exc
        observed_head = getattr(validated, "observed_head", None)
        return AgentResult(
            ok=True,
            agent=adapter.agent,
            session_id=terminal.session_id,
            session_generation=session_generation,
            turn_id=binding.turn_id,
            observed_head=observed_head,
            structured_output=validated.model_dump(mode="json"),
            return_code=result.return_code,
            elapsed_seconds=result.elapsed_seconds,
            raw_output_ref=result.raw_output_ref,
            protocol_method=method.value,
        )
