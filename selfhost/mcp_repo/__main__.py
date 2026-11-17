"""仓库操作 MCP：让 capgate 用它自己构建自己。

只读工具（静态检查、跑测试、列 issue、看仓库状态）之外，issue #014 起又加了一组
**受限写入**工具（write_file / apply_patch）。写入是双保险：

1. 工具侧：默认 dry-run，显式 ``allow_write=true`` 才考虑落盘；路径只允许仓库内的
   ``capgate/ tests/ docs/ envs/ capabilities/``，越界 / 绝对路径 / 符号链接一律拒绝。
2. 操作者批准侧：进程只在启动时通过 ``CAPGATE_SELFHOST_APPROVED_PATCH`` 收到一个
   **已审 patch 产物路径**；正式写入要求 allow_write、且当前动作的 path / diff /
   修改前后的文件哈希都与该产物一致。进程没收到批准就拒绝落盘——模型自己点
   ``allow_write=true`` 没用。

原始执行理由（reason）随 patch 产物与账本保留。放在 selfhost/ 里，不从主包导出。
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import unidiff
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tools.readme_check import check_readme, format_problems

from capgate.mcp.server import create_server
from capgate.mcp.transport import sdk_major

if sdk_major() == 2:
    from mcp.server.mcpserver.exceptions import ToolError
else:
    from mcp.server.fastmcp.exceptions import ToolError

_default_root = Path(__file__).resolve().parents[2]
_root_env = os.environ.get("CAPGATE_SELFHOST_ROOT", "").strip()
ROOT = Path(_root_env).resolve() if _root_env else _default_root

mcp = create_server("capgate-repo")

# 写入白名单：只允许这些顶层目录。README.md / selfhost/ / .github/ 都不在列——
# 不能让网关改"检查它自己的检查"，也不能改引导它自己的 selfhost。
WRITE_WHITELIST = ("capgate", "tests", "docs", "envs", "capabilities")


def _run(args: list[str], timeout: int = 600) -> dict:
    try:
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
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"仓库子命令执行超时（{timeout}s）") from exc
    return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _tail(text: str, lines: int = 6) -> str:
    rows = [ln.rstrip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(rows[-lines:])


@mcp.tool()
def run_lint() -> dict:
    """对仓库跑 ruff check，返回错误数与摘要。"""
    result = _run([sys.executable, "-m", "ruff", "check", ".", "--output-format", "concise"])
    output = result["stdout"] + result["stderr"]
    if result["exit_code"] != 0:
        raise ToolError(f"Ruff 退出码 {result['exit_code']}\n{_tail(output, 20)}")
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
    state = _default_root / "var"
    state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=state, prefix="selfhost-tests-") as scratch:
        result = _run([*args, "--basetemp", str(Path(scratch) / "pytest")])
    output = result["stdout"] + result["stderr"]
    if result["exit_code"] != 0:
        raise ToolError(f"pytest 退出码 {result['exit_code']}\n{_tail(output, 20)}")
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
        raise ToolError(format_problems(list(report["problems"])))
    return report


# ---------------------------------------------------------------- 受限写入


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_write_path(path: str) -> Path:
    """把写入路径解析到仓库内的白名单目录；越界、绝对路径、符号链接一律拒绝。"""
    p = Path(path)
    if p.is_absolute():
        raise ToolError(f"写入被拒绝：{path!r} 是绝对路径，只接受仓库内的相对路径")
    # 路径里任何已存在的组件是符号链接都拒绝——写入不能穿过未授权的跳转。
    probe = ROOT
    for part in p.parts:
        probe = probe / part
        if probe.is_symlink():
            raise ToolError(f"写入被拒绝：{path!r} 含符号链接组件 {part!r}")
    resolved = probe.resolve()
    if not resolved.is_relative_to(ROOT):
        raise ToolError(f"写入被拒绝：{path!r} 越出仓库根目录")
    rel = resolved.relative_to(ROOT)
    if not rel.parts or rel.parts[0] not in WRITE_WHITELIST:
        raise ToolError(f"写入被拒绝：{path!r} 不在白名单目录（{'、'.join(WRITE_WHITELIST)}）")
    return resolved


def _unified_diff(rel: str, old: str, new: str) -> str:
    """用标准库 difflib 生成统一 diff，保留文件的换行变化。"""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
    )
    return "".join(
        line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n" for line in lines
    )


def _apply_patch_safely(rel: str, old: str, diff: str) -> str:
    """用 unidiff 校验 diff 只改一个文件，再用 git apply 在隔离目录生成新内容。

    不手写 unified diff parser；任何多文件 / 路径不一致 / 上下文对不上都拒绝。
    """
    try:
        patch = unidiff.PatchSet.from_string(diff)
    except Exception as exc:
        raise ValueError(f"diff 无法解析：{exc}") from exc
    files = [f for f in patch if f.is_added_file or f.is_modified_file or f.is_removed_file]
    if len(files) != 1:
        raise ValueError(f"diff 必须只改一个文件，实际 {len(files)} 个")
    declared = files[0].path
    if declared != rel:
        raise ValueError(f"diff 声明的文件 {declared!r} 与目标 {rel!r} 不一致")

    scratch = _default_root / "var" / "selfhost-patches"
    scratch.mkdir(parents=True, exist_ok=True)
    git_env = {
        key: value for key, value in os.environ.items() if key not in {"GIT_DIR", "GIT_WORK_TREE"}
    }
    git_env["GIT_CEILING_DIRECTORIES"] = str(scratch)
    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        tmpdir = Path(tmp)
        work = tmpdir / rel
        work.parent.mkdir(parents=True, exist_ok=True)
        work.write_bytes(old.encode("utf-8"))
        proc = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn"],
            cwd=tmpdir,
            env=git_env,
            input=diff.encode("utf-8"),
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
            raise ValueError(f"git apply 失败：{detail or 'diff 无法应用'}")
        return work.read_bytes().decode("utf-8")


def _atomic_write(target: Path, content: str) -> None:
    """同目录临时文件 + 原子替换，避免写一半的落盘。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class PatchApproval(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    path: str = Field(min_length=1)
    diff: str
    old_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _load_approval() -> PatchApproval | None:
    raw = os.environ.get("CAPGATE_SELFHOST_APPROVED_PATCH", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    try:
        return PatchApproval.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ValueError(f"批准产物无法读取或不符合格式：{path}：{exc}") from exc


_APPROVED = _load_approval()


def _require_approval(rel: str, diff: str, old_sha: str, new_sha: str) -> None:
    """正式写入必须匹配进程持有的已审 patch 产物，否则拒绝。"""
    if _APPROVED is None:
        raise ToolError(
            "写入被拒绝：进程没有收到已审 patch 批准"
            "（设置 CAPGATE_SELFHOST_APPROVED_PATCH 指向已审产物）"
        )
    if _APPROVED.path != rel:
        raise ToolError(
            f"写入被拒绝：批准产物的 path 与当前动作不一致（批准 {_APPROVED.path!r}，当前 {rel!r}）"
        )
    if _APPROVED.old_sha256 != old_sha:
        raise ToolError("写入被拒绝：文件前置状态已变化，旧 patch 已过期")
    if _APPROVED.diff != diff or _APPROVED.new_sha256 != new_sha:
        raise ToolError("写入被拒绝：批准产物的 diff 与当前动作不一致")


def _patch_result(
    rel: str, old: str, new: str, reason: str, allow_write: bool, wrote: bool
) -> dict:
    return {
        "path": rel,
        "wrote": wrote,
        "changed": old != new,
        "diff": _unified_diff(rel, old, new),
        "old_sha256": _sha256(old),
        "new_sha256": _sha256(new),
        "reason": reason,
        "allow_write": allow_write,
    }


@mcp.tool()
def write_file(path: str, content: str, allow_write: bool = False, reason: str = "") -> dict:
    """写一个文件。默认 dry-run（只产出 diff）；allow_write=true 且进程持有匹配批准才落盘。

    路径只允许仓库内的 capgate/ tests/ docs/ envs/ capabilities/，越界抛错。
    """
    target = _check_write_path(path)
    rel = target.relative_to(ROOT).as_posix()
    old = target.read_bytes().decode("utf-8") if target.exists() else ""
    new = content
    changed = old != new
    wrote = False
    if allow_write:
        _require_approval(rel, _unified_diff(rel, old, new), _sha256(old), _sha256(new))
        if changed:
            _atomic_write(target, new)
            wrote = True
    return _patch_result(rel, old, new, reason, allow_write, wrote)


@mcp.tool()
def apply_patch(path: str, diff: str, allow_write: bool = False, reason: str = "") -> dict:
    """把一个 unified diff 应用到文件上。默认 dry-run；allow_write=true 且持有匹配批准才落盘。

    路径白名单同 write_file；diff 只许改这一个文件，应用失败 / 过期一律拒绝且不改文件。
    """
    target = _check_write_path(path)
    rel = target.relative_to(ROOT).as_posix()
    old = target.read_bytes().decode("utf-8") if target.exists() else ""
    try:
        new = _apply_patch_safely(rel, old, diff)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    unified = _unified_diff(rel, old, new)
    changed = old != new
    wrote = False
    if allow_write:
        _require_approval(rel, unified, _sha256(old), _sha256(new))
        if changed:
            _atomic_write(target, new)
            wrote = True
    return _patch_result(rel, old, new, reason, allow_write, wrote)


def main() -> None:
    import logging

    logging.getLogger("mcp").setLevel(logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
