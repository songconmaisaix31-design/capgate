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

from capgate.agents.base import AgentError
from capgate.agents.cli import _codex_patch, _codex_source_files

PUBLIC_REPO = "https://github.com/songconmaisaix31-design/astrocyte-s3-public-stats"
PUBLIC_REVISION = "dbce190be32f52eb93a6fd3909fe4d05213c9ba7"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("CAPGATE_CODEX_PUBLIC_REPO"),
        reason="需要 CAPGATE_CODEX_PUBLIC_REPO 指向公开验证仓库的工作目录",
    ),
]


@pytest.fixture(scope="module")
def public_source() -> tuple[Path, str]:
    public_repo = Path(os.environ["CAPGATE_CODEX_PUBLIC_REPO"]).resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=public_repo, text=True)
    assert revision.strip() == PUBLIC_REVISION
    source = subprocess.check_output(
        ["git", "show", f"{PUBLIC_REVISION}:stats.go"], cwd=public_repo
    ).decode("utf-8")
    return public_repo, source


def test_codex_public_task_through_http(
    repo_root: Path, tmp_path: Path, public_source: tuple[Path, str]
) -> None:
    public_repo, source = public_source
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
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
            ) as client:
                chain = _run_public_task(client, process, source)
                perf = client.get("/api/stats").raise_for_status().json()["perf"]
        finally:
            process.terminate()
            process.wait(timeout=10)

    artifact = next(
        artifact for artifact in chain["artifacts"] if artifact["name"] == "codex_change"
    )
    payload = json.loads((tmp_path / "var" / "artifacts" / artifact["path"]).read_bytes())
    patch = payload["diff"]
    assert "func Median(" in patch and payload["reason"].strip()
    # 使用 LF 检出，保持应用检查的文件字节与提供给模型的提交正文一致。
    check_repo = tmp_path / "public-lf"
    subprocess.run(
        [
            "git",
            "clone",
            "--no-hardlinks",
            "-c",
            "core.autocrlf=false",
            str(public_repo),
            str(check_repo),
        ],
        check=True,
        capture_output=True,
    )
    assert (check_repo / "stats.go").read_bytes() == source.encode("utf-8")
    subprocess.run(
        ["git", "apply", "--check", "-"],
        input=patch.encode("utf-8"),
        cwd=check_repo,
        check=True,
    )
    run = chain["runs"][0]
    assert (run["adapter"], run["model"], run["status"]) == ("cli", "gpt-6.1-sol", "ok")
    usage = json.loads(run["meta"])["usage"]
    (record,) = perf
    assert record["avg_tokens"] == usage["input_tokens"] + usage["output_tokens"] > 0
    assert record["avg_cost"] is None
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
        outputs = [
            content["text"]
            for line in lines
            if (event := json.loads(line))["type"] == "response_item"
            and event["payload"].get("role") == "assistant"
            for content in event["payload"]["content"]
            if content.get("type") == "output_text"
        ]
    native_result = json.loads(outputs[-1])
    assert set(native_result) == {"replacements", "reason"}
    assert [item["path"] for item in native_result["replacements"]] == ["stats.go"]
    assert native_result["reason"] == payload["reason"]
    assert not {"command_execution", "file_change", "mcp_tool_call", "web_search"} & {
        event["payload"].get("item_type") for event in events
    }
    print(f"thread_id={thread_id} event_ids={[event['id'] for event in chain['events']]}")


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("unknown", "未提供的文件"),
        ("duplicate", "重复替换"),
        ("unchanged", "没有修改"),
        ("crlf", "UTF-8 LF"),
    ],
)
def test_codex_rejects_invalid_replacements(
    public_source: tuple[Path, str], case: str, message: str
) -> None:
    _, source = public_source
    source_files = _codex_source_files({"stats.go": source})
    changed = {"path": "stats.go", "text": source + "\n"}
    replacements = {
        "unknown": [{**changed, "path": "../stats.go"}],
        "duplicate": [changed, changed],
        "unchanged": [{**changed, "text": source}],
        "crlf": [{**changed, "text": source.replace("\n", "\r\n")}],
    }
    output = json.dumps({"replacements": replacements[case], "reason": "为统计包保留原输入。"})
    with pytest.raises(AgentError, match=message):
        _codex_patch(output, source_files)


def test_codex_rejects_nonrelative_source_path(public_source: tuple[Path, str]) -> None:
    _, source = public_source
    with pytest.raises(AgentError, match="规范相对路径"):
        _codex_source_files({"../stats.go": source})


def test_codex_decodes_cli_files_without_duplicate_keys(
    public_source: tuple[Path, str],
) -> None:
    _, source = public_source
    assert _codex_source_files(json.dumps({"stats.go": source})) == {"stats.go": source}
    item = f'"stats.go": {json.dumps(source)}'
    with pytest.raises(AgentError, match="重复字段"):
        _codex_source_files(f"{{{item}, {item}}}")


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
                "避免两个最大有限中间值求平均时溢出。只返回 stats.go 完整新文本和修改理由。"
            ),
            "inputs": {
                "repo": PUBLIC_REPO,
                "commit": PUBLIC_REVISION,
                "files": {"stats.go": source},
            },
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
