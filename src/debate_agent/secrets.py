"""从固定本地文件加载 Facilitator 密钥。"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import MutableMapping
from pathlib import Path

DEFAULT_SECRETS_PATH = Path("~/.config/debate-agent/secrets.env")
LEGACY_SECRETS_PATH = Path("~/.config/discuss-agent/secrets.env")
DEEPSEEK_KEY_NAME = "DEEPSEEK_API_KEY"
_KEY_PATTERN = re.compile(
    rf"^(?:export\s+)?{DEEPSEEK_KEY_NAME}\s*=\s*(?P<value>.*)$"
)


def _parse_value(raw_value: str, *, line_number: int) -> str:
    """解析单个 Key 值；参数为原始文本和行号，返回值，格式非法时抛错。"""
    lexer = shlex.shlex(raw_value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        parts = list(lexer)
    except ValueError as exc:
        raise ValueError(f"secrets.env 第 {line_number} 行引号不完整") from exc
    if len(parts) != 1 or not parts[0]:
        raise ValueError(f"secrets.env 第 {line_number} 行的 DeepSeek Key 无效")
    return parts[0]


def load_default_deepseek_key(
    *,
    environment: MutableMapping[str, str] | None = None,
    secrets_path: Path | None = None,
) -> bool:
    """加载默认 DeepSeek Key。

    参数为可选环境映射和文件路径；成功写入环境时返回 True，显式变量或文件缺失时
    返回 False；文件读取失败或 Key 格式非法时抛错。只解析目标赋值，不执行 shell。
    """
    target = environment if environment is not None else os.environ
    if DEEPSEEK_KEY_NAME in target:
        return False
    path = (secrets_path or DEFAULT_SECRETS_PATH).expanduser()
    if secrets_path is None and not path.is_file():
        path = LEGACY_SECRETS_PATH.expanduser()
    if not path.is_file():
        return False
    value: str | None = None
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = _KEY_PATTERN.fullmatch(line.strip())
        if match is None:
            continue
        value = _parse_value(match.group("value"), line_number=line_number)
    if value is None:
        return False
    target[DEEPSEEK_KEY_NAME] = value
    return True
