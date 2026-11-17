from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from capgate.server import create_app


def test_read_endpoints(system):
    client = TestClient(create_app(system))

    health = client.get("/api/health").json()
    assert health["ok"] is True and health["errors"] == []

    envs = client.get("/api/envs").json()
    assert {"crawler", "analyst", "chain-watch", "doc-qa", "bca-chain", "bca-evm", "fit-env"} <= {
        e["id"] for e in envs
    }

    caps = client.get("/api/caps").json()
    assert {"mcp-fetch", "mcp-rag", "mcp-bca", "mcp-fit", "sdk-text", "sdk-chain"} <= {
        c["id"] for c in caps
    }

    stats = client.get("/api/stats").json()
    assert stats["ledger"]["tasks"] == 0

    assert "capgate" in client.get("/").text


async def test_create_task_registers_it(system, repo_root):
    app = create_app(system)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/api/tasks",
            json={
                "goal": "抓取仓库中的示例站点",
                "tags": ["爬虫"],
                "inputs": {"url": (repo_root / "fixtures" / "sample_site.html").as_uri()},
            },
        )
        assert response.status_code == 200
        task_id = response.json()["id"]
        assert system.ledger.task(task_id) is not None
        await asyncio.gather(*app.state.jobs)
        chain = (await client.get(f"/api/tasks/{task_id}")).raise_for_status().json()
    assert chain["task"]["status"] == "ok", chain["task"]["error"]
    assert any(artifact["name"] == "crawl_batch" for artifact in chain["artifacts"])


def test_outcome_endpoint_validates_task(system):
    client = TestClient(create_app(system))
    assert client.post("/api/tasks/task_missing/outcome", json={"success": True}).status_code == 404
