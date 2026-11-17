"""受限写入能力（issue #014）的单测。

写工具的 ROOT 用 monkeypatch 指到临时目录，绝不碰真实仓库；dry-run / 白名单 /
apply_patch 的冲突回退都在这里验。真正跑通"决策链可追"的集成路径单独一条，用
dry-run 走网关（不落盘），保证测试安全。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import selfhost.mcp_repo.__main__ as repo

from capgate.models import TaskSpec

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    (root / "capgate").mkdir(parents=True)
    monkeypatch.setattr(repo, "ROOT", root)
    return root


# ---------------------------------------------------------------- write_file


def test_write_file_dry_run_does_not_touch_disk(fake_root: Path):
    result = repo.write_file("capgate/x.py", "print('hi')\n")
    assert result["wrote"] is False
    assert result["changed"] is True
    assert "capgate/x.py" in result["diff"]
    assert not (fake_root / "capgate" / "x.py").exists()


def test_write_file_allow_write_writes(fake_root: Path):
    result = repo.write_file("capgate/x.py", "print('hi')\n", allow_write=True)
    assert result["wrote"] is True
    assert (fake_root / "capgate" / "x.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_write_file_noop_when_content_unchanged(fake_root: Path):
    (fake_root / "capgate" / "x.py").write_text("same\n", encoding="utf-8")
    result = repo.write_file("capgate/x.py", "same\n", allow_write=True)
    assert result["wrote"] is False
    assert result["changed"] is False
    assert result["diff"] == ""


def test_write_file_rejects_parent_traversal(fake_root: Path):
    with pytest.raises(ValueError, match="越出"):
        repo.write_file("../evil.py", "x")
    assert not (fake_root.parent / "evil.py").exists()


def test_write_file_rejects_absolute_path(fake_root: Path):
    with pytest.raises(ValueError, match="绝对路径"):
        repo.write_file(str(fake_root / "capgate" / "x.py"), "x")


def test_write_file_rejects_non_whitelisted_dir(fake_root: Path):
    with pytest.raises(ValueError, match="白名单"):
        repo.write_file("README.md", "x")


def test_write_file_rejects_selfhost(fake_root: Path):
    with pytest.raises(ValueError, match="白名单"):
        repo.write_file("selfhost/foo.py", "x")
    assert not (fake_root / "selfhost" / "foo.py").exists()


# ---------------------------------------------------------------- apply_patch


def test_apply_patch_roundtrip(fake_root: Path):
    old = "a\nb\nc\n"
    new = "a\nb\nc\nd\n"
    target = fake_root / "capgate" / "x.py"
    target.write_text(old, encoding="utf-8")
    diff = repo.write_file("capgate/x.py", new)["diff"]  # dry-run 生成统一 diff
    result = repo.apply_patch("capgate/x.py", diff, allow_write=True)
    assert result["wrote"] is True
    assert target.read_text(encoding="utf-8") == new


def test_apply_patch_dry_run_keeps_file(fake_root: Path):
    target = fake_root / "capgate" / "x.py"
    target.write_text("a\n", encoding="utf-8")
    diff = repo.write_file("capgate/x.py", "a\nb\n")["diff"]
    result = repo.apply_patch("capgate/x.py", diff)
    assert result["wrote"] is False
    assert target.read_text(encoding="utf-8") == "a\n"


def test_apply_patch_rejects_on_context_mismatch(fake_root: Path):
    target = fake_root / "capgate" / "x.py"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    bad_diff = "--- a/capgate/x.py\n+++ b/capgate/x.py\n@@ -1,2 +1,2 @@\n a\n ZZZ\n"
    with pytest.raises(ValueError, match="不匹配"):
        repo.apply_patch("capgate/x.py", bad_diff)
    assert target.read_text(encoding="utf-8") == "a\nb\nc\n"  # 不留半截文件


def test_apply_patch_empty_diff_is_noop(fake_root: Path):
    target = fake_root / "capgate" / "x.py"
    target.write_text("a\n", encoding="utf-8")
    result = repo.apply_patch("capgate/x.py", "")
    assert result["changed"] is False
    assert target.read_text(encoding="utf-8") == "a\n"


# ---------------------------------------------------------------- 集成：决策链可追


@pytest.fixture
async def selfhost_system(tmp_path: Path):
    from capgate.router import RouterConfig
    from capgate.system import build

    s = build(REPO_ROOT / "selfhost", RouterConfig(strategy="static"), state=tmp_path / "state")
    try:
        yield s
    finally:
        await s.aclose()


async def test_repo_fix_dry_run_lands_patch_artifact_in_chain(selfhost_system):
    """一次写入（即便 dry-run）也要在决策链里追到改了哪个文件、为什么。"""
    target = "capgate/__init__.py"
    original = (REPO_ROOT / target).read_text(encoding="utf-8")
    content = original + "\n# selfhost 写入测试（dry-run，不落盘）\n"
    task = TaskSpec(
        goal="演示一次受限写入的可追溯性",
        tags=["修复"],
        inputs={"path": target, "content": content, "allow_write": False},
        budget_s=120,
    )
    result = await selfhost_system.orchestrator.run(task)

    assert result.status == "ok", result.error
    patch = next(a for a in result.artifacts if a.name == "patch")
    payload = selfhost_system.store.load(patch)
    assert payload["wrote"] is False
    assert target in payload["diff"]
    # dry-run 不改任何文件
    assert (REPO_ROOT / target).read_text(encoding="utf-8") == original
    # 决策链：任务目标（为什么）+ 运行 env（谁）+ 产物（改了哪个文件）
    chain = selfhost_system.ledger.chain(task.id)
    assert [r["env_id"] for r in chain["runs"]] == ["repo-fix"]
    assert {a["name"] for a in chain["artifacts"]} == {"patch"}
