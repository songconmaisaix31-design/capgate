"""本地 HTTP 服务 + 看板。

看板只做一件事：把决策链摊开给人看。路由再聪明，如果解释不了就是黑盒。
"""

from __future__ import annotations

import asyncio
import contextlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import __version__
from .models import TaskSpec
from .system import System

DASHBOARD = Path(__file__).with_name("static") / "dashboard.html"


@lru_cache(maxsize=1)
def dashboard_html() -> str:
    """看板是静态资源，不塞进 Python 字符串里——否则改一行 CSS 也要重启解释器。"""
    return DASHBOARD.read_text(encoding="utf-8")


class TaskIn(BaseModel):
    goal: str
    tags: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    hint_envs: list[str] = Field(default_factory=list)
    type: str | None = None
    allow_parallel: bool = True


class OutcomeIn(BaseModel):
    success: bool
    quality: float | None = None
    human_edits: int = 0
    note: str | None = None


def create_app(system: System) -> FastAPI:
    app = FastAPI(title="capgate", version=__version__)
    app.state.system = system
    app.state.jobs = set()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return dashboard_html()

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "errors": system.errors}

    @app.get("/api/envs")
    async def list_envs() -> list[dict[str, Any]]:
        return [
            {
                "id": env.id,
                "domain": env.domain,
                "tags": env.tags,
                "capabilities": [f"{c.id}@{c.version}" for c in env.capabilities],
                "adapter": env.agent.adapter,
                "models": env.agent.models,
                "prior": env.prior,
                "warm": env.resources.warm,
            }
            for env in system.registry.all()
        ]

    @app.get("/api/caps")
    async def list_caps() -> list[dict[str, Any]]:
        return [
            {
                "id": cap.id,
                "kind": cap.kind,
                "title": cap.title,
                "versions": [v.version for v in cap.versions],
            }
            for cap in system.pool.all()
        ]

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        return {
            "ledger": system.ledger.stats(),
            "perf": system.ledger.perf(),
            "warm": system.warm(),
        }

    @app.get("/api/tasks")
    async def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
        return system.ledger.list_tasks(limit=limit)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        chain = system.ledger.chain(task_id)
        if not chain.get("task"):
            raise HTTPException(404, "task not found")
        return chain

    @app.post("/api/tasks")
    async def create_task(body: TaskIn) -> dict[str, Any]:
        spec = TaskSpec(
            goal=body.goal,
            tags=body.tags,
            inputs=body.inputs,
            hint_envs=body.hint_envs,
            type=body.type,
            allow_parallel=body.allow_parallel,
        )
        system.ledger.add_task(spec)

        async def runner() -> None:
            # 失败已经落账；这里只保证后台任务不把异常抛到事件循环外面
            with contextlib.suppress(Exception):
                await system.orchestrator.run(spec)

        job = asyncio.create_task(runner())
        app.state.jobs.add(job)
        job.add_done_callback(app.state.jobs.discard)
        return {"id": spec.id, "status": "running"}

    @app.post("/api/tasks/{task_id}/outcome")
    async def record_outcome(task_id: str, body: OutcomeIn) -> dict[str, Any]:
        if not system.ledger.task(task_id):
            raise HTTPException(404, "task not found")
        system.ledger.outcome(
            task_id,
            success=body.success,
            quality=body.quality,
            human_edits=body.human_edits,
            note=body.note,
        )
        return {"ok": True}

    return app
