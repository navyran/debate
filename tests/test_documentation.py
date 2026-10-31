from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def read_document(name: str) -> str:
    """读取项目文档；参数为文件名，返回 UTF-8 文本，文件错误时传播异常。"""
    return (ROOT / name).read_text(encoding="utf-8")


def test_reliability_defaults_are_consistent_across_user_documents() -> None:
    """验证稳定性默认值在用户文档中一致；无参数和返回值，缺失事实时断言失败。"""
    documents = {
        name: read_document(name)
        for name in (
            "README.md",
            "USER_GUIDE.md",
            "CLI_AGENT_EXECUTION_DEFAULTS.md",
        )
    }

    for name, content in documents.items():
        assert "300" in content, f"{name} 缺少启动/空闲默认值"
        assert "3600" in content, f"{name} 缺少单次硬上限"
        assert "7200" in content, f"{name} 缺少 Run 总时限"
        assert "1200 秒" not in content, f"{name} 仍记录旧固定超时"


def test_user_documents_cover_live_output_health_and_current_adapter_modes() -> None:
    """验证文档覆盖实时输出和当前 CLI 模式；无参数和返回值，过期说明会断言失败。"""
    readme = read_document("README.md")
    guide = read_document("USER_GUIDE.md")
    defaults = read_document("CLI_AGENT_EXECUTION_DEFAULTS.md")

    for content in (readme, guide, defaults):
        assert "健康预检" in content
        assert "startup" in content
        assert "idle" in content
        assert "hard" in content
        assert "MCP" in content
        assert "熔断" in content
    assert "Codex 实时" in readme
    assert "原始输出" in guide
    assert "network_error" in guide
    assert "--ignore-user-config" in defaults
    assert "stream-json" in defaults
    assert "CodeBuddy" in defaults
    assert "--output-format json" in defaults
    assert "当前 Streamlit 页面尚未提供" not in defaults


def test_opencode_documents_match_current_entrypoints_and_safe_smoke_scope() -> None:
    """验证四份用户文档完整说明 OpenCode 入口、边界和组合回归；无参数，无返回值。"""
    readme = read_document("README.md")
    guide = read_document("USER_GUIDE.md")
    defaults = read_document("CLI_AGENT_EXECUTION_DEFAULTS.md")
    smoke = read_document("SMOKE_TEST.md")

    assert "OpenCode" in readme
    assert "默认不选" in readme
    assert "--opencode-variant" in readme
    assert "Supervisor" in readme
    assert "不 push" in readme
    assert "opencode auth" in guide
    assert "--variant" in guide
    assert "minimal" in guide and "full" in guide
    assert "OpenCode CLI" in defaults
    assert "opencode run" in defaults
    assert "--auto" in defaults and "--pure" in defaults
    assert "OPENCODE_CONFIG_CONTENT" in defaults
    assert "OpenCode" in smoke
    assert "非写入" in smoke
    for combination in (
        "Codex + OpenCode",
        "Claude + OpenCode",
        "Qoder + OpenCode",
        "四方",
    ):
        assert combination in smoke


def test_user_documents_describe_dynamic_participant_consensus() -> None:
    """验证用户文档不把动态共识门禁写成固定双 Agent；无参数，无返回值。"""
    readme = read_document("README.md")
    guide = read_document("USER_GUIDE.md")
    defaults = read_document("CLI_AGENT_EXECUTION_DEFAULTS.md")

    for content in (readme, guide, defaults):
        assert "所有已选 Agent" in content
    assert "Codex 和 Claude 相互不可见地独立判断是否完成" not in guide
    assert "双方只读确认同一个最终 tree" not in guide
    assert "双方都明确判断 GoalContract 已完成" not in guide
    assert "双 complete ballot" not in defaults
    assert "双方完成判断" not in readme
    assert "双方对固定 tree 的 Ack" not in readme


def test_codebuddy_documents_match_headless_session_and_security_contract() -> None:
    """验证 CodeBuddy 文档覆盖安装、Session、密钥和真实 E2E 门禁。"""
    readme = read_document("README.md")
    guide = read_document("USER_GUIDE.md")
    defaults = read_document("CLI_AGENT_EXECUTION_DEFAULTS.md")
    smoke = read_document("SMOKE_TEST.md")
    integration = read_document("CODEBUDDY_INTEGRATION.md")

    for content in (readme, guide, defaults, smoke, integration):
        assert "CodeBuddy" in content
    assert "--codebuddy-native-profile" in readme
    assert "--codebuddy-model" in guide
    assert "codebuddy -p -y" in defaults
    assert "--output-format json" in defaults
    assert "--json-schema" in defaults
    assert "--resume" in defaults
    assert "structured_output" in integration
    assert "CODEBUDDY_API_KEY" in integration
    assert "DEBATE_AGENT_E2E_CODEBUDDY" in smoke
    assert "默认不选" in integration
