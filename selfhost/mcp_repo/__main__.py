"""仓库操作 MCP：让 capgate 用它自己构建自己。

工具是只读的（除了跑命令这件事本身）：静态检查、跑测试、列 issue、看仓库状态。
放在 selfhost/ 里，不从主包导出——它是自举工作区的一部分，不是网关能力。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]
mcp = FastMCP("capgate-repo")


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


def main() -> None:
    import logging

    logging.getLogger("mcp").setLevel(logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
