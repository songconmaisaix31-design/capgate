"""仓库操作 MCP：让 capgate 用它自己构建自己。

只读工具（静态检查、跑测试、列 issue、看仓库状态）之外，issue #014 起又加了一组
**受限写入**工具（write_file / apply_patch）：默认 dry-run，显式 allow_write=true 才
落盘，路径只允许仓库内的 capgate/ tests/ docs/ envs/ capabilities/，越界直接拒绝。

放在 selfhost/ 里，不从主包导出——它是自举工作区的一部分，不是网关能力。
"""

from __future__ import annotations

import difflib
import hashlib
import re
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from tools.readme_check import check_readme, format_problems

ROOT = Path(__file__).resolve().parents[2]
mcp = FastMCP("capgate-repo")

# 写入白名单：只允许这些顶层目录。README.md / selfhost/ / .github/ 都不在列——
# 不能让网关改"检查它自己的检查"，也不能改引导它自己的 selfhost。
WRITE_WHITELIST = ("capgate", "tests", "docs", "envs", "capabilities")


def _run(args: list[str], timeout: int = 600) -> dict:
    proc = subprocess.run(
        args,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,  # 绝不能继承 MCP 的 stdio 管道
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _tail(text: str, lines: int = 6) -> str:
    rows = [ln.rstrip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(rows[-lines:])


@mcp.tool()
def run_lint() -> dict:
    """对仓库跑 ruff check，返回错误数与摘要。"""
    result = _run([sys.executable, "-m", "ruff", "check", ".", "--output-format", "concise"])
    output = result["stdout"] + result["stderr"]
    match = re.search(r"Found (\d+) error", output)
    return {
        "ok": result["exit_code"] == 0,
        "exit_code": result["exit_code"],
        "errors": int(match.group(1)) if match else 0,
        "fixed": "fixed" in output,
        "summary": _tail(output),
    }


@mcp.tool()
def run_tests(include_integration: bool = False) -> dict:
    """跑 pytest。默认跳过需要本机真实 MCP 的集成用例。"""
    args = [sys.executable, "-m", "pytest", "-q"]
    if not include_integration:
        args += ["-m", "not integration"]
    result = _run(args)
    output = result["stdout"] + result["stderr"]
    passed = re.search(r"(\d+) passed", output)
    failed = re.search(r"(\d+) failed", output)
    return {
        "ok": result["exit_code"] == 0,
        "exit_code": result["exit_code"],
        "passed": int(passed.group(1)) if passed else 0,
        "failed": int(failed.group(1)) if failed else 0,
        "integration": include_integration,
        "summary": _tail(output, 3),
    }


@mcp.tool()
def list_issues() -> dict:
    """列出 .github/issues 下的待办草稿（还没建到 GitHub 上的那些）。"""
    issues_dir = ROOT / ".github" / "issues"
    rows: list[dict] = []
    for path in sorted(issues_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        title = ""
        labels: list[str] = []
        for line in text.splitlines()[:12]:
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip().strip("\"'")
            elif line.startswith("labels:"):
                raw = line.split(":", 1)[1].strip().strip("[]")
                labels = [part.strip() for part in raw.split(",") if part.strip()]
        rows.append(
            {"id": path.stem.split("-")[0], "title": title, "labels": labels, "file": path.name}
        )
    by_label: dict[str, int] = {}
    for row in rows:
        for label in row["labels"]:
            by_label[label] = by_label.get(label, 0) + 1
    return {"count": len(rows), "by_label": by_label, "issues": rows}


@mcp.tool()
def ping() -> dict:
    """最简单的存活检查，用来区分"服务没起"和"工具卡住"。"""
    return {"pong": True, "root": str(ROOT)}


@mcp.tool()
def repo_status() -> dict:
    """当前分支、最近一条提交、未提交改动数。"""
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])["stdout"].strip()
    last = _run(["git", "log", "-1", "--pretty=%s"])["stdout"].strip()
    dirty = [
        ln for ln in _run(["git", "status", "--porcelain"])["stdout"].splitlines() if ln.strip()
    ]
    return {"branch": branch, "last_commit": last, "dirty_files": len(dirty), "root": str(ROOT)}


@mcp.tool()
def run_readme_check() -> dict:
    """校验 README 的双语覆盖、相对链接、图片存在与 demo.svg 是否最新。

    有任一问题就抛错（并列出具体到哪一节、哪个链接），否则返回完整报告。
    """
    report = check_readme(ROOT)
    if not report["ok"]:
        raise RuntimeError(format_problems(list(report["problems"])))
    return report


# ---------------------------------------------------------------- 受限写入


def _check_write_path(path: str) -> Path:
    """把写入路径解析到仓库内的白名单目录；越界、绝对路径、``..`` 逃逸一律拒绝。"""
    p = Path(path)
    if p.is_absolute():
        raise ValueError(f"写入被拒绝：{path!r} 是绝对路径，只接受仓库内的相对路径")
    resolved = (ROOT / p).resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError(f"写入被拒绝：{path!r} 越出仓库根目录")
    rel = resolved.relative_to(ROOT)
    if not rel.parts or rel.parts[0] not in WRITE_WHITELIST:
        raise ValueError(f"写入被拒绝：{path!r} 不在白名单目录（{'、'.join(WRITE_WHITELIST)}）")
    return resolved


def _unified_diff(rel: str, old: str, new: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""
        )
    )


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _apply_unified_diff(text: str, diff: str) -> str:
    """把 unified diff（difflib 生成的那种）应用到 text 上，返回新内容。

    任何 hunk 上下文对不上就抛 ValueError，绝不写出半截文件。
    """
    hunks: list[dict] = []
    current: dict | None = None
    for raw in diff.splitlines():
        m = _HUNK.match(raw)
        if m:
            current = {
                "old_start": int(m.group(1)),
                "old_len": int(m.group(2) or "1"),
                "new_start": int(m.group(3)),
                "new_len": int(m.group(4) or "1"),
                "ops": [],
            }
            hunks.append(current)
            continue
        if current is None:
            continue
        if raw.startswith(" "):
            current["ops"].append((" ", raw[1:]))
        elif raw.startswith("-"):
            current["ops"].append(("-", raw[1:]))
        elif raw.startswith("+"):
            current["ops"].append(("+", raw[1:]))
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"

    if not hunks:
        return text  # 空 diff = 无改动

    old_lines = text.splitlines()
    out: list[str] = []
    consumed = 0
    for hunk in hunks:
        start = max(int(hunk["old_start"]), 1)  # difflib 对空旧文件写 @@ -0,0 +1,N @@
        if start - 1 < consumed:
            raise ValueError(f"diff 无法应用：hunk 起始 {start} 与已消费 {consumed} 行重叠")
        out.extend(old_lines[consumed : start - 1])
        consumed = start - 1
        for op, line in hunk["ops"]:
            if op == " ":
                if consumed >= len(old_lines) or old_lines[consumed] != line:
                    raise ValueError(f"diff 无法应用：上下文不匹配 {line!r}")
                out.append(line)
                consumed += 1
            elif op == "-":
                if consumed >= len(old_lines) or old_lines[consumed] != line:
                    raise ValueError(f"diff 无法应用：待删除行不匹配 {line!r}")
                consumed += 1
            elif op == "+":
                out.append(line)
    out.extend(old_lines[consumed:])
    result = "\n".join(out)
    if text.endswith("\n") and out:
        result += "\n"
    return result


def _write_result(rel: str, old: str, new: str, allow_write: bool, wrote: bool) -> dict:
    return {
        "path": rel,
        "wrote": wrote,
        "changed": old != new,
        "diff": _unified_diff(rel, old, new),
        "old_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
        "new_sha256": hashlib.sha256(new.encode("utf-8")).hexdigest(),
        "allow_write": allow_write,
    }


@mcp.tool()
def write_file(path: str, content: str, allow_write: bool = False) -> dict:
    """写一个文件。默认 dry-run（只产出 diff，不落盘）；allow_write=true 才真正写。

    路径只允许仓库内的 capgate/ tests/ docs/ envs/ capabilities/，越界抛错。
    """
    target = _check_write_path(path)
    rel = target.relative_to(ROOT).as_posix()
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    changed = old != content
    if allow_write and changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return _write_result(rel, old, content, allow_write, allow_write and changed)


@mcp.tool()
def apply_patch(path: str, diff: str, allow_write: bool = False) -> dict:
    """把一个 unified diff 应用到文件上。默认 dry-run；allow_write=true 才落盘。

    路径白名单同 write_file；diff 应用失败（上下文对不上）抛错且不改任何文件。
    """
    target = _check_write_path(path)
    rel = target.relative_to(ROOT).as_posix()
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    new = _apply_unified_diff(old, diff)
    changed = old != new
    if allow_write and changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding="utf-8")
    return _write_result(rel, old, new, allow_write, allow_write and changed)


def main() -> None:
    import logging

    logging.getLogger("mcp").setLevel(logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
