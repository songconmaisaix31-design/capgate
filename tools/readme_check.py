"""README 文档质量校验：双语覆盖 / 相对链接 / 图片存在 / demo.svg 最新。

对应 issue #013：把"文档质量没有门禁"补上。原先的双语检查是 skill 目录里的第三方
脚本，这里没有复制它，而是按本仓库自己的 README 约定从头实现，避免许可与来源问题。

结构解析用 ``markdown-it-py``（成熟 Markdown 解析器），不手写正则去猜标题 / 链接 /
图片；语言覆盖判定仍基于原文行。

校验的四件事：

1. **双语覆盖** —— 每个 ``##`` 小节标题与正文都要同时有中文与英文，缺哪边就报哪边；
2. **相对链接** —— ``[text](url)`` 里指向本地的 url 必须真实存在；
3. **图片存在** —— ``![alt](url)`` 与 ``<img src="...">`` 里的本地图片必须存在；
4. **demo.svg 最新** —— 跑 ``node tools/make-demo.mjs --check``。

用法：:

    python tools/readme_check.py        # 从仓库根跑，退出码 0=通过 / 1=有问题

这个模块被 ``selfhost.mcp_repo.run_readme_check`` 复用，不在这里放任何网关逻辑。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from markdown_it import MarkdownIt

_MD = MarkdownIt("commonmark")

# 中文用 CJK 统一表意文字区间；英文用 ASCII 字母。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")

# 带协议 / 锚点 / 协议相对引用的 url 不做本地存在性检查。
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|#|//)")

# 裸 ``<img src="...">``（块级或行内 HTML）里的 src。
_IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)

_DEMO_SVG = "assets/demo/demo.svg"

Problem = dict[str, object]


def _is_external(url: str) -> bool:
    return bool(_EXTERNAL.match(url))


def _strip_anchor(url: str) -> str:
    return url.split("#", 1)[0]


def _h2_sections(text: str) -> list[dict[str, object]]:
    """用 markdown-it 找 ``##`` 标题，按标题位置切出每节正文（0-based 行号）。"""
    tokens = _MD.parse(text)
    headings: list[tuple[int, int, str]] = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.tag == "h2":
            start = tok.map[0] if tok.map else 0
            end = tok.map[1] if tok.map else start + 1
            content = tokens[i + 1].content if i + 1 < len(tokens) else ""
            headings.append((start, end, content.strip()))

    lines = text.splitlines()
    sections: list[dict[str, object]] = []
    for idx, (start, end, heading) in enumerate(headings):
        nxt = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        sections.append(
            {
                "line": start + 1,
                "heading": heading,
                "body": "\n".join(lines[end:nxt]),
            }
        )
    return sections


def _refs(text: str) -> list[tuple[int, str, str]]:
    """用 markdown-it 提取链接与图片，返回 ``(行号, url, kind)``。"""
    tokens = _MD.parse(text)
    refs: list[tuple[int, str, str]] = []
    for tok in tokens:
        if tok.type == "inline":
            line = tok.map[0] + 1 if tok.map else 1
            for child in tok.children or []:
                if child.type == "link_open":
                    href = child.attrGet("href")
                    if href:
                        refs.append((line, href, "link"))
                elif child.type == "image":
                    src = child.attrGet("src")
                    if src:
                        refs.append((line, src, "image"))
                elif child.type == "html_inline":
                    for m in _IMG_SRC.finditer(child.content):
                        refs.append((line, m.group(1), "image"))
        elif tok.type == "html_block":
            line = tok.map[0] + 1 if tok.map else 1
            for m in _IMG_SRC.finditer(tok.content):
                refs.append((line, m.group(1), "image"))
    return refs


def check_bilingual(readme_text: str) -> list[Problem]:
    """每个 ``##`` 小节都要同时有中文与英文，缺哪边报哪边。"""
    problems: list[Problem] = []
    for sec in _h2_sections(readme_text):
        heading = str(sec["heading"])
        body = str(sec["body"])
        line = int(sec["line"])

        head_cn = bool(_CJK.search(heading))
        head_en = bool(_LATIN.search(heading))
        if not (head_cn and head_en):
            side = "缺英文" if head_cn else "缺中文"
            problems.append(
                {
                    "kind": "bilingual",
                    "file": "README.md",
                    "line": line,
                    "message": f"小节标题「{heading}」{side}",
                }
            )

        body_cn = bool(_CJK.search(body))
        body_en = bool(_LATIN.search(body))
        if body_cn and not body_en:
            problems.append(
                {
                    "kind": "bilingual",
                    "file": "README.md",
                    "line": line,
                    "message": f"小节「{heading}」正文缺英文",
                }
            )
        elif body_en and not body_cn:
            problems.append(
                {
                    "kind": "bilingual",
                    "file": "README.md",
                    "line": line,
                    "message": f"小节「{heading}」正文缺中文",
                }
            )
    return problems


def _check_local_refs(root: Path, readme_text: str, kind: str) -> list[Problem]:
    seen: set[Path] = set()
    problems: list[Problem] = []
    for line, url, ref_kind in _refs(readme_text):
        if ref_kind != kind or _is_external(url):
            continue
        target = (root / _strip_anchor(url)).resolve()
        if target in seen:
            continue
        seen.add(target)
        if not target.exists():
            label = "链接" if kind == "link" else "图片"
            problems.append(
                {
                    "kind": kind,
                    "file": "README.md",
                    "line": line,
                    "message": f"{label}失效：{url}（目标不存在）",
                }
            )
    return problems


def check_links(root: Path, readme_text: str) -> list[Problem]:
    return _check_local_refs(root, readme_text, "link")


def check_images(root: Path, readme_text: str) -> list[Problem]:
    return _check_local_refs(root, readme_text, "image")


def check_demo(root: Path) -> dict[str, object]:
    """跑 ``node tools/make-demo.mjs --check``，判断 demo.svg 是否最新。"""
    script = root / "tools" / "make-demo.mjs"
    try:
        proc = subprocess.run(
            ["node", str(script), "--check"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except FileNotFoundError:
        return {"status": "skipped", "message": "本机没有 node，无法校验 demo.svg 是否最新"}
    if proc.returncode == 0:
        return {"status": "fresh", "message": None}
    return {"status": "stale", "message": "已过期，请跑：node tools/make-demo.mjs"}


def check_readme(root: Path, *, run_demo_check: bool = True) -> dict[str, object]:
    """汇总四项检查。``run_demo_check=False`` 给单测用，跳过 node 子进程。"""
    readme = root / "README.md"
    if not readme.exists():
        return {
            "ok": False,
            "problems": [
                {"kind": "link", "file": "README.md", "line": None, "message": "README.md 不存在"}
            ],
            "checks": {},
        }
    text = readme.read_text(encoding="utf-8")

    problems: list[Problem] = []
    problems += check_bilingual(text)
    problems += check_links(root, text)
    problems += check_images(root, text)

    refs = _refs(text)
    checks: dict[str, object] = {
        "sections": len(_h2_sections(text)),
        "links": sum(1 for r in refs if r[2] == "link"),
        "images": sum(1 for r in refs if r[2] == "image"),
    }

    if run_demo_check:
        demo = check_demo(root)
        checks["demo"] = demo["status"]
        if demo["status"] != "fresh":
            problems.append(
                {"kind": "demo", "file": _DEMO_SVG, "line": None, "message": str(demo["message"])}
            )
    else:
        checks["demo"] = "skipped"

    return {"ok": not problems, "problems": problems, "checks": checks}


def format_problems(problems: list[Problem]) -> str:
    """把问题列表排成给人看的多行文本，供 CLI 与 MCP 工具共用。"""
    lines = ["README 文档校验失败："]
    for p in problems:
        file = str(p["file"])
        line = p.get("line")
        loc = f"{file}:{line}" if line else file
        lines.append(f"- {loc} {p['message']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    report = check_readme(root)
    if report["ok"]:
        checks = report["checks"]
        print(f"README 校验通过：{checks.get('sections')} 节，demo={checks.get('demo')}")
        return 0
    print(format_problems(list(report["problems"])))
    return 1


if __name__ == "__main__":
    sys.exit(main())
