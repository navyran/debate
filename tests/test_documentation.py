"""验证公开文档可导航且配图作为仓库资源存在。"""

from pathlib import Path
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).parents[1]


def test_readme_local_links_and_images_exist():
    """检查首页相对链接与配图；无参数返回值，缺失资源时断言失败。"""
    content = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+)\)", content):
        if not target.startswith(("https://", "http://", "#")):
            assert (ROOT / target.split("#")[0]).is_file(), target
    for name in ("architecture", "run"):
        ET.parse(ROOT / "docs" / "diagrams" / (name + ".svg"))


def test_repository_root_contains_only_public_entry_documents():
    """根目录只保留公开入口文档；无参数返回值，混入内部材料时断言失败。"""
    assert {p.name for p in ROOT.glob("*.md")} == {"README.md", "SECURITY.md"}
