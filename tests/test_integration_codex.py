from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

PUBLIC_REPO = "https://github.com/songconmaisaix31-design/astrocyte-s3-public-stats"
PUBLIC_REVISION = "dbce190be32f52eb93a6fd3909fe4d05213c9ba7"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("CAPGATE_CODEX_PUBLIC_REPO"),
        reason="需要 CAPGATE_CODEX_PUBLIC_REPO 指向公开验证仓库的工作目录",
    ),
]


def test_codex_public_task_through_http(repo_root: Path, tmp_path: Path) -> None:
    public_repo = Path(os.environ["CAPGATE_CODEX_PUBLIC_REPO"]).resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=public_repo, text=True)
    assert revision.strip() == PUBLIC_REVISION
    source = subprocess.check_output(
        ["git", "show", f"{PUBLIC_REVISION}:stats.go"], cwd=public_repo, encoding="utf-8"
    )
    for directory in ("envs", "capabilities", "schemas"):
        shutil.copytree(repo_root / directory, tmp_path / directory)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (tmp_path / "server.log").open("wb") as server_log:
        process = subprocess.Popen(
            [sys.executable, "-m", "capgate", "serve", "--port", str(port)],
            cwd=tmp_path,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "CAPGATE_HOME": str(tmp_path)},
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
                chain = _run_public_task(client, process, source)
        finally:
            process.terminate()
            process.wait(timeout=10)

    artifact = next(
        artifact for artifact in chain["artifacts"] if artifact["name"] == "codex_change"
    )
    patch = json.loads((tmp_path / "var" / "artifacts" / artifact["path"]).read_bytes())["diff"]
    assert "Median" in patch
    subprocess.run(
        ["git", "apply", "--check", "-"],
        input=patch.rstrip() + "\n",
        cwd=public_repo,
        encoding="utf-8",
        check=True,
    )
    run = chain["runs"][0]
    assert (run["adapter"], run["model"], run["status"]) == ("cli", "gpt-6.1-sol", "ok")
    events = [event for event in chain["events"] if event["kind"].startswith("codex.")]
    thread_id = next(event for event in events if event["kind"] == "codex.thread.started")[
        "payload"
    ]["thread_id"]
    assert any(event["kind"] == "codex.turn.completed" for event in events)
    assert all(event["payload"]["thread_id"] == thread_id for event in events)

    with sqlite3.connect(tmp_path / "var" / "capgate.db") as database:
        for event in chain["events"]:
            row = database.execute(
                "SELECT task_id, kind, payload FROM events WHERE id=?", (event["id"],)
            ).fetchone()
            assert row is not None
            assert (row[0], row[1], json.loads(row[2])) == (
                chain["task"]["id"],
                event["kind"],
                event["payload"],
            )

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    (session,) = (codex_home / "sessions").rglob(f"*{thread_id}.jsonl")
    with session.open(encoding="utf-8") as lines:
        meta = json.loads(next(lines))
        assert meta["type"] == "session_meta" and meta["payload"]["id"] == thread_id
        assert any("codex_change" in line and '"role":"assistant"' in line for line in lines)
    print(f"thread_id={thread_id} event_ids={[event['id'] for event in chain['events']]}")


def _run_public_task(client: httpx.Client, process: subprocess.Popen, source: str) -> dict:
    for _ in range(50):
        assert process.poll() is None, "capgate HTTP 服务提前退出"
        try:
            health = client.get("/api/health").raise_for_status().json()
        except httpx.ConnectError:
            time.sleep(0.2)
        else:
            assert health["ok"] and health["errors"] == []
            break
    else:
        pytest.fail("capgate HTTP 服务没有就绪")

    response = client.post(
        "/api/tasks",
        json={
            "goal": (
                "只根据给出的公开 stats.go 为 Go 包添加 Median 函数。"
                "保留输入顺序，支持奇偶数和负数，拒绝空值和非有限数。"
                "避免两个最大有限中间值求平均时溢出。codex_change 产物须包含 diff 和 reason。"
                "diff 只改 stats.go，必须能用 git apply 应用。"
                "以标准 JSON 返回完整的结构化结果。"
            ),
            "inputs": {"repo": PUBLIC_REPO, "commit": PUBLIC_REVISION, "stats.go": source},
            "tags": ["代码修改"],
            "hint_envs": ["codex-dev"],
        },
    ).raise_for_status()
    task_id = response.json()["id"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        chain = client.get(f"/api/tasks/{task_id}").raise_for_status().json()
        if chain["task"]["status"] != "running":
            assert chain["task"]["status"] == "ok", (chain["task"]["error"], chain["runs"])
            return chain
        time.sleep(0.3)
    pytest.fail("公开 Codex 任务超时")
