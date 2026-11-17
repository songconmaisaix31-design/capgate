from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import selfhost.mcp_repo.__main__ as repo

from capgate.models import TaskSpec
from capgate.router import RouterConfig
from capgate.system import build

REPO_ROOT = Path(__file__).resolve().parents[1]
SELF = REPO_ROOT / "selfhost"

INITIAL = "a\nb\nc\n"
NEXT = "a\nb\nc\nd\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture
def isolated_repo(tmp_path: Path, monkeypatch) -> Path:
    """一个真实、干净的 Git 仓库，带一个白名单目录下的文件。"""
    root = tmp_path / "repo"
    (root / "capgate").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "capgate" / "foo.py").write_text(INITIAL, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    monkeypatch.setenv("CAPGATE_SELFHOST_ROOT", str(root))
    return root


@pytest.fixture
async def selfhost_system(tmp_path: Path):
    s = build(SELF, RouterConfig(strategy="static"), state=tmp_path / "state")
    try:
        yield s
    finally:
        await s.aclose()


async def _run_fix(system, path: str, content: str, allow_write: bool, goal: str = "测试写入"):
    task = TaskSpec(
        goal=goal,
        tags=["修复"],
        inputs={"path": path, "content": content, "allow_write": allow_write},
        budget_s=120,
    )
    return await system.orchestrator.run(task)


def _patch_payload(system, result) -> dict:
    patch = next(a for a in result.artifacts if a.name == "patch")
    return system.store.load(patch)


# ---------------------------------------------------------------- write_file


async def test_dry_run_does_not_write(selfhost_system, isolated_repo):
    target = isolated_repo / "capgate" / "foo.py"
    result = await _run_fix(selfhost_system, "capgate/foo.py", NEXT, allow_write=False)
    assert result.status == "ok", result.error
    payload = _patch_payload(selfhost_system, result)
    assert payload["wrote"] is False
    assert "capgate/foo.py" in payload["diff"]
    assert target.read_text(encoding="utf-8") == INITIAL


async def test_allow_write_without_approval_rejected(selfhost_system, isolated_repo):
    target = isolated_repo / "capgate" / "foo.py"
    result = await _run_fix(selfhost_system, "capgate/foo.py", NEXT, allow_write=True)
    assert result.status == "failed"
    assert "没有收到已审 patch 批准" in (result.error or "")
    assert target.read_text(encoding="utf-8") == INITIAL


@pytest.mark.parametrize("configured_file", ["absent", "schema"])
def test_invalid_configured_approval_fails_at_startup(tmp_path, configured_file):
    approval = (
        SELF / "schemas" / "patch.schema.json"
        if configured_file == "schema"
        else tmp_path / "unavailable-approval.json"
    )
    process = subprocess.run(
        [sys.executable, "-m", "selfhost.mcp_repo"],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "CAPGATE_SELFHOST_APPROVED_PATCH": str(approval),
            "PYTHONIOENCODING": "utf-8",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert process.returncode != 0
    assert "批准产物无法读取或不符合格式" in process.stderr


async def test_approved_write_executes_and_is_traceable(tmp_path, monkeypatch, isolated_repo):
    # 1. dry-run 产出 patch 产物（path / diff / old_sha256）
    s1 = build(SELF, RouterConfig(strategy="static"), state=tmp_path / "s1")
    try:
        r1 = await _run_fix(s1, "capgate/foo.py", NEXT, allow_write=False)
        assert r1.status == "ok", r1.error
        payload = _patch_payload(s1, r1)
    finally:
        await s1.aclose()

    # 2. 操作者审阅后批准：把产物写成批准文件，通过环境变量指给进程
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CAPGATE_SELFHOST_APPROVED_PATCH", str(approval))

    # 3. 新进程（新 system → 新 MCP 子进程）读到批准，正式写入
    s2 = build(SELF, RouterConfig(strategy="static"), state=tmp_path / "s2")
    try:
        r2 = await _run_fix(s2, "capgate/foo.py", NEXT, allow_write=True, goal="修复 foo 增加 d 行")
        assert r2.status == "ok", r2.error
        p2 = _patch_payload(s2, r2)
        assert p2["wrote"] is True
        # 回查：决策链里能追到改了哪个文件、为什么
        chain = s2.ledger.chain(r2.task.id)
        assert [r["env_id"] for r in chain["runs"]] == ["repo-fix"]
        assert {a["name"] for a in chain["artifacts"]} == {"patch"}
        assert "capgate/foo.py" in p2["diff"]
        assert p2["reason"] == "修复 foo 增加 d 行"
    finally:
        await s2.aclose()

    assert (isolated_repo / "capgate" / "foo.py").read_text(encoding="utf-8") == NEXT


async def test_stale_approval_rejected(tmp_path, monkeypatch, isolated_repo):
    # dry-run 产出批准
    s1 = build(SELF, RouterConfig(strategy="static"), state=tmp_path / "s1")
    try:
        r1 = await _run_fix(s1, "capgate/foo.py", NEXT, allow_write=False)
        payload = _patch_payload(s1, r1)
    finally:
        await s1.aclose()
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CAPGATE_SELFHOST_APPROVED_PATCH", str(approval))

    # 批准后文件被别人改过（前置状态变了），旧 patch 过期
    target = isolated_repo / "capgate" / "foo.py"
    target.write_text("a\nb\nc\nX\n", encoding="utf-8")

    s2 = build(SELF, RouterConfig(strategy="static"), state=tmp_path / "s2")
    try:
        r2 = await _run_fix(s2, "capgate/foo.py", NEXT, allow_write=True)
        assert r2.status == "failed"
        assert "前置状态已变化" in (r2.error or "")
    finally:
        await s2.aclose()
    assert target.read_text(encoding="utf-8") == "a\nb\nc\nX\n"


async def test_out_of_bounds_rejected(selfhost_system, isolated_repo):
    # 越界：../ 逃逸（先于批准检查就被拒绝）
    r = await _run_fix(selfhost_system, "../evil.py", "x", allow_write=True)
    assert r.status == "failed"
    assert "越出仓库根目录" in (r.error or "")
    assert not (isolated_repo.parent / "evil.py").exists()

    # 绝对路径
    r2 = await _run_fix(
        selfhost_system, str(isolated_repo / "capgate" / "foo.py"), "x", allow_write=False
    )
    assert r2.status == "failed"
    assert "绝对路径" in (r2.error or "")

    # 非白名单目录（README.md 在根，不在白名单）
    r3 = await _run_fix(selfhost_system, "README.md", "x", allow_write=False)
    assert r3.status == "failed"
    assert "白名单" in (r3.error or "")


# ---------------------------------------------------------------- apply_patch 的 diff 应用


def test_apply_patch_safely_single_file_roundtrip():
    diff = "--- a/capgate/foo.py\n+++ b/capgate/foo.py\n@@ -1,3 +1,4 @@\n a\n b\n c\n+d\n"
    assert repo._apply_patch_safely("capgate/foo.py", INITIAL, diff) == NEXT


def test_apply_patch_safely_preserves_final_newline_change():
    target = "capgate/foo.py"
    changed = INITIAL.rstrip("\n")
    diff = repo._unified_diff(target, INITIAL, changed)
    assert repo._apply_patch_safely(target, INITIAL, diff) == changed


def test_apply_patch_safely_rejects_multi_file():
    diff = (
        "--- a/capgate/foo.py\n+++ b/capgate/foo.py\n@@ -1 +1 @@\n-a\n+b\n"
        "--- a/capgate/bar.py\n+++ b/capgate/bar.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    with pytest.raises(ValueError, match="一个文件"):
        repo._apply_patch_safely("capgate/foo.py", "a\n", diff)


def test_apply_patch_safely_rejects_path_mismatch():
    diff = "--- a/capgate/other.py\n+++ b/capgate/other.py\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(ValueError, match="不一致"):
        repo._apply_patch_safely("capgate/foo.py", "a\n", diff)


def test_apply_patch_safely_rejects_context_mismatch():
    diff = "--- a/capgate/foo.py\n+++ b/capgate/foo.py\n@@ -1,2 +1,2 @@\n a\n ZZZ\n"
    with pytest.raises(ValueError, match="git apply 失败"):
        repo._apply_patch_safely("capgate/foo.py", "a\nb\n", diff)
