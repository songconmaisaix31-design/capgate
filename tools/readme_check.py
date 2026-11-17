"""README 文档质量校验：双语覆盖 / 相对链接 / 图片存在 / demo.svg 最新。

对应 issue #013：把"文档质量没有门禁"补上。原先的双语检查是 skill 目录里的第三方
脚本，这里没有复制它，而是按本仓库自己的 README 约定从头实现，避免许可与来源问题。

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

# 中文用 CJK 统一表意文字区间；英文用 ASCII 字母。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")

# README 的顶级小节用 ``## `` 起头；标题约定 ``英文 · 中文``。
_SECTION = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# markdown 图片 ``![alt](url)`` 与链接 ``[text](url)``（负向后瞻排除图片）。
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
_MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
# 裸 ``<img src="...">``。
_IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)

# 带协议 / 锚点 / 协议相对引用的 url 不做本地存在性检查。
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|#|//)")

_DEMO_SVG = "assets/demo/demo.svg"

Problem = dict[str, object]


def _is_external(url: str) -> bool:
    return bool(_EXTERNAL.match(url))


def _strip_anchor(url: str) -> str:
    return url.split("#", 1)[0]


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _sections(text: str) -> list[dict[str, object]]:
    """按 ``##`` 切分 README，返回标题、正文与起始行号。"""
    matches = list(_SECTION.finditer(text))
    sections: list[dict[str, object]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append(
            {
                "heading": m.group(1).strip(),
                "body": text[m.end() : end],
                "line": _line_of(text, m.start()),
            }
        )
    return sections


def check_bilingual(readme_text: str) -> list[Problem]:
    """每个 ``##`` 小节都要同时有中文与英文，缺哪边报哪边。"""
    problems: list[Problem] = []
    for sec in _sections(readme_text):
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
    """检查 README 里指向本地的引用（链接或图片）是否真实存在。"""
    problems: list[Problem] = []
    seen: set[Path] = set()
    for line_no, line in enumerate(readme_text.splitlines(), start=1):
        urls: list[str] = []
        if kind == "link":
            urls = [m.group(1) for m in _MD_LINK.finditer(line)]
        else:
            urls = [m.group(1) for m in _MD_IMAGE.finditer(line)]
            urls += [m.group(1) for m in _IMG_SRC.finditer(line)]
        for url in urls:
            if _is_external(url):
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
                        "line": line_no,
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

    checks: dict[str, object] = {
        "sections": len(_sections(text)),
        "links": len(_MD_LINK.findall(text)),
        "images": len(_MD_IMAGE.findall(text)) + len(_IMG_SRC.findall(text)),
    }

    if run_demo_check:
        demo = check_demo(root)
        checks["demo"] = demo["status"]
        if demo["status"] != "fresh":
            problems.append(
                {
                    "kind": "demo",
                    "file": _DEMO_SVG,
                    "line": None,
                    "message": str(demo["message"]),
                }
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
