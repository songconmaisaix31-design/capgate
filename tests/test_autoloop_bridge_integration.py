from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from capgate.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "integrations" / "agent-autoloop"
TASK = {
    "goal": "查询本机 FIT 注册表与环境情况",
    "tags": ["环境"],
    "hint_envs": ["fit-env"],
    "budget_s": 73,
    "allow_parallel": False,
}

pytestmark = pytest.mark.integration


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def running_server(args, env, url, log_path):
    with log_path.open("wb") as log:
        process = subprocess.Popen(args, env=env, cwd=ROOT, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    if httpx.get(url, timeout=1, trust_env=False).is_success:
                        break
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass
                time.sleep(0.1)
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail(log_path.read_text(encoding="utf-8", errors="replace"))
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


@pytest.fixture
def capgate_server(tmp_path):
    fit = Path(os.environ.get("CAPGATE_FIT_HOME", "D:/repos/fit-protocol")) / "bin" / "fit.mjs"
    if not fit.is_file():
        pytest.skip("需要本机已安装的 FIT MCP")
    root = tmp_path / "gateway"
    root.mkdir()
    for directory in ("envs", "capabilities", "schemas"):
        shutil.copytree(ROOT / directory, root / directory)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    command = [sys.executable, "-m", "capgate", "serve", "--root", str(root), "--port", str(port)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with running_server(command, env, f"{url}/api/health", tmp_path / "capgate.log"):
        yield url


@pytest.fixture
def live_autoloop(tmp_path, capgate_server):
    src = os.environ.get("CAPGATE_TEST_AUTOLOOP_DIR")
    if not src:
        pytest.skip("需设置 CAPGATE_TEST_AUTOLOOP_DIR 指向 agent-autoloop 源码目录")
    executable = Path(src).resolve() / "bin" / "agent-autoloop.ts"
    node = shutil.which("node")
    if not executable.is_file() or node is None:
        pytest.skip("需安装 Node.js 24 并提供 agent-autoloop 源码")

    state = tmp_path / "autoloop"
    state.mkdir()
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = dict(
        os.environ,
        AGENT_AUTOLOOP_HOME=str(state),
        AGENT_AUTOLOOP_RULES=str(state / "rules.json"),
        CAPGATE_AUTOLOOP_URL=url,
        CAPGATE_BRIDGE_URL=capgate_server,
    )
    cli = [node, str(executable)]
    subprocess.run(
        [
            *cli,
            "init",
            "--no-service",
            "--port",
            str(port),
            "--command",
            f'"{node}" "{BRIDGE / "command.mjs"}" rerun',
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    config_file = state / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    config.pop("channels", None)
    config_file.write_text(json.dumps(config), encoding="utf-8")
    rules = {
        "sources": {"capgate-operator": {"name": "capgate 操作者"}},
        "agents": {
            "capgate-rerun": {
                "name": "capgate 重跑",
                "worker": config["worker"]["name"],
                "use": "command",
                "command": [node, str(BRIDGE / "command.mjs"), "rerun"],
            }
        },
        "rules": [
            {
                "name": "capgate 重跑",
                "when": {"source": "capgate-operator", "types": ["capgate.rerun"]},
                "to": "capgate-rerun",
                "cwd": str(ROOT),
                "parallel": 1,
                "tell": "never",
                "prompt": "按保存的规范重跑 capgate 任务",
            }
        ],
    }
    (state / "rules.json").write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    subprocess.run([*cli, "check"], env=env, check=True, capture_output=True)
    env["CAPGATE_AUTOLOOP_OPERATOR_KEY"] = subprocess.check_output(
        [*cli, "key", "new", "source", "capgate-operator"],
        env=env,
        encoding="utf-8",
    ).strip()
    with running_server([*cli, "up"], env, f"{url}/healthz", tmp_path / "autoloop.log"):
        listed = subprocess.check_output([*cli, "key", "list"], env=env, encoding="utf-8")
        assert "source capgate-operator" in listed
        yield node, env


def completed_task(url, task_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        chain = httpx.get(f"{url}/api/tasks/{task_id}", trust_env=False).raise_for_status().json()
        if chain["task"]["status"] != "running":
            assert chain["task"]["status"] == "ok", chain
            return chain
        time.sleep(0.25)
    pytest.fail(f"FIT 任务 {task_id} 没有结束")


def test_api_preserves_budget(capgate_server):
    url = capgate_server
    task_id = (
        httpx.post(f"{url}/api/tasks", json=TASK, trust_env=False).raise_for_status().json()["id"]
    )
    chain = completed_task(url, task_id)
    assert json.loads(chain["task"]["spec"])["budget_s"] == TASK["budget_s"]
    assert "env_report" in {artifact["name"] for artifact in chain["artifacts"]}


def test_autoloop_source_is_available(live_autoloop):
    _, env = live_autoloop
    assert httpx.get(f"{env['CAPGATE_AUTOLOOP_URL']}/healthz", trust_env=False).json()["ok"]


def test_rerun_keeps_original_task_and_deduplicates(capgate_server, live_autoloop):
    assert hasattr(Ledger, "reserve_task"), "需合并 SQLite 的 reserve_task 实现"
    url = capgate_server
    task_id = (
        httpx.post(f"{url}/api/tasks", json=TASK, trust_env=False).raise_for_status().json()["id"]
    )
    before = completed_task(url, task_id)
    node, env = live_autoloop
    command = [node, str(BRIDGE / "rerun.mjs"), task_id, "inspect-fit-again"]
    first = subprocess.check_output(command, env=env, encoding="utf-8", timeout=45)
    second = subprocess.check_output(command, env=env, encoding="utf-8", timeout=45)
    assert first == second
    assert "成功" in first
    match = re.search(r"task_[0-9a-f]+", first)
    assert match is not None and match[0] != task_id
    after = completed_task(url, match[0])
    assert completed_task(url, task_id) == before
    spec = json.loads(after["task"]["spec"])
    assert {key: spec[key] for key in TASK} == TASK
    source = spec["inputs"]["_capgate_autoloop"]
    assert source["source"] == "capgate-operator" and source["rerun_of"] == task_id
    headers = {"Idempotency-Key": f"autoloop:rerun:{source['event_id']}"}
    same = (
        httpx.post(f"{url}/api/tasks", json=spec, headers=headers, trust_env=False)
        .raise_for_status()
        .json()
    )
    assert same["id"] == match[0]
    conflict = httpx.post(
        f"{url}/api/tasks", json={**spec, "budget_s": 90}, headers=headers, trust_env=False
    )
    assert conflict.status_code == 409
    assert len(httpx.get(f"{url}/api/tasks", trust_env=False).raise_for_status().json()) == 2
