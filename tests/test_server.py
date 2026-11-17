from __future__ import annotations

from fastapi.testclient import TestClient

from capgate.server import create_app


def test_read_endpoints(system):
    client = TestClient(create_app(system))

    health = client.get("/api/health").json()
    assert health["ok"] is True and health["errors"] == []

    envs = client.get("/api/envs").json()
    assert {e["id"] for e in envs} == {"crawler", "analyst", "chain-watch", "doc-qa"}

    caps = client.get("/api/caps").json()
    assert {c["id"] for c in caps} == {"mcp-fetch", "mcp-rag", "sdk-text", "sdk-chain"}

    stats = client.get("/api/stats").json()
    assert stats["ledger"]["tasks"] == 0

    assert "capgate" in client.get("/").text


def test_create_task_registers_it(system):
    client = TestClient(create_app(system))
    resp = client.post(
        "/api/tasks",
        json={"goal": "抓一下示例站点", "tags": ["爬虫"], "inputs": {"url": "file:///nope"}},
    )
    assert resp.status_code == 200
    task_id = resp.json()["id"]
    assert system.ledger.task(task_id) is not None


def test_outcome_endpoint_validates_task(system):
    client = TestClient(create_app(system))
    assert client.post("/api/tasks/task_missing/outcome", json={"success": True}).status_code == 404
