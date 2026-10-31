from __future__ import annotations

from pathlib import Path

import pytest

import debate_agent.secrets as secrets_module
from debate_agent.secrets import load_default_deepseek_key


def test_loads_exported_key_without_executing_other_lines(tmp_path: Path) -> None:
    """验证只解析目标 Key；参数为临时目录，无返回值。"""
    secrets = tmp_path / "secrets.env"
    marker = tmp_path / "must-not-exist"
    secrets.write_text(
        "# local secrets\n"
        f"touch {marker}\n"
        "export DEEPSEEK_API_KEY=\"secret-value#inside\" # comment\n",
        encoding="utf-8",
    )
    environment: dict[str, str] = {}

    loaded = load_default_deepseek_key(
        environment=environment, secrets_path=secrets
    )

    assert loaded is True
    assert environment == {"DEEPSEEK_API_KEY": "secret-value#inside"}
    assert marker.exists() is False


def test_accepts_plain_assignment_and_preserves_explicit_environment(
    tmp_path: Path,
) -> None:
    """验证兼容普通赋值且环境优先；参数为临时目录，无返回值。"""
    secrets = tmp_path / "secrets.env"
    secrets.write_text("DEEPSEEK_API_KEY=file-value\n", encoding="utf-8")
    loaded_environment: dict[str, str] = {}

    assert (
        load_default_deepseek_key(
            environment=loaded_environment, secrets_path=secrets
        )
        is True
    )
    assert loaded_environment["DEEPSEEK_API_KEY"] == "file-value"

    explicit_environment = {"DEEPSEEK_API_KEY": "explicit-value"}
    assert (
        load_default_deepseek_key(
            environment=explicit_environment, secrets_path=secrets
        )
        is False
    )
    assert explicit_environment["DEEPSEEK_API_KEY"] == "explicit-value"


def test_missing_file_or_key_is_optional(tmp_path: Path) -> None:
    """验证文件或 Key 缺失不阻断；参数为临时目录，无返回值。"""
    environment: dict[str, str] = {}
    assert (
        load_default_deepseek_key(
            environment=environment, secrets_path=tmp_path / "missing.env"
        )
        is False
    )
    empty = tmp_path / "secrets.env"
    empty.write_text("OTHER_KEY=value\n", encoding="utf-8")
    assert (
        load_default_deepseek_key(environment=environment, secrets_path=empty)
        is False
    )
    assert environment == {}


def test_invalid_key_value_reports_line_without_leaking_content(
    tmp_path: Path,
) -> None:
    """验证非法值产生脱敏错误；参数为临时目录，无返回值。"""
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "export DEEPSEEK_API_KEY=\"unterminated\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="第 1 行引号不完整") as captured:
        load_default_deepseek_key(environment={}, secrets_path=secrets)

    assert "unterminated" not in str(captured.value)


def test_default_key_path_falls_back_to_legacy_discuss_agent_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证新密钥文件缺失时读取旧版路径；参数为目录/补丁夹具，无返回值。"""
    current = tmp_path / "debate-agent" / "secrets.env"
    legacy = tmp_path / "discuss-agent" / "secrets.env"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("DEEPSEEK_API_KEY=legacy-value\n", encoding="utf-8")
    monkeypatch.setattr(secrets_module, "DEFAULT_SECRETS_PATH", current)
    monkeypatch.setattr(secrets_module, "LEGACY_SECRETS_PATH", legacy)
    environment: dict[str, str] = {}

    loaded = load_default_deepseek_key(environment=environment)

    assert loaded is True
    assert environment["DEEPSEEK_API_KEY"] == "legacy-value"
