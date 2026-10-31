"""从 Archify HTML 提取带内嵌样式的双主题 SVG，供 GitHub README 展示。"""

from pathlib import Path
import re


def export_svg(path):
    """提取图形和主题；参数为HTML路径，写同名SVG；缺少预期结构时抛异常。"""
    content = path.read_text(encoding="utf-8")
    styles = re.search(r"<style>(.*?)</style>", content, re.S).group(1)
    light = re.search(r'\[data-theme="light"\]\s*\{([^}]+)\}', styles).group(1)
    svg = re.search(r"<svg\b.*?</svg>", content, re.S).group(0)
    svg = svg.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ', 1)
    injected = (
        "<style><![CDATA[" + styles
        + "\nsvg { font-family: ui-monospace, 'Noto Sans CJK SC', 'PingFang SC', monospace; }"
        + "\n@media (prefers-color-scheme: light) { :root { " + light + " } }"
        + "]]></style><rect width=\"100%\" height=\"100%\" fill=\"var(--bg)\"/>"
    )
    end = svg.index(">") + 1
    rendered = svg[:end] + injected + svg[end:]
    rendered = "\n".join(line.rstrip() for line in rendered.splitlines()) + "\n"
    path.with_suffix(".svg").write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    for name in ("architecture", "run"):
        export_svg(Path(__file__).parent / (name + ".html"))
