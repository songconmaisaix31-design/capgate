"""本地 HTTP 服务 + 看板。

看板只做一件事：把决策链摊开给人看。路由再聪明，如果解释不了就是黑盒。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import __version__
from .models import TaskSpec
from .pins import PinError, set_pin
from .system import System
from .taxonomy import TaxonomyError

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
    budget_s: int = Field(default=300, gt=0)


class OutcomeIn(BaseModel):
    success: bool
    quality: float | None = None
    human_edits: int = 0
    note: str | None = None


class PinIn(BaseModel):
    env: str = Field(min_length=1)


class RerunReceipt(BaseModel):
    event: int = Field(gt=0)
    routed: list[str]


async def _request_rerun(task_id: str, request_id: str) -> RerunReceipt:
    hub = os.environ.get("CAPGATE_AUTOLOOP_URL")
    key = os.environ.get("CAPGATE_AUTOLOOP_OPERATOR_KEY")
    if not hub or not key:
        raise HTTPException(503, "重跑未启用，请设置 agent-autoloop 地址和操作者来源密钥")
    try:
        url = httpx.URL(hub)
    except httpx.InvalidURL as error:
        raise HTTPException(503, "agent-autoloop 地址无效") from error
    if (
        url.scheme not in {"http", "https"}
        or url.host not in {"127.0.0.1", "::1", "localhost"}
        or url.userinfo
    ):
        raise HTTPException(503, "agent-autoloop 地址须使用本机 HTTP 服务地址")

    encoded = json.dumps([task_id, request_id], separators=(",", ":")).encode()
    event_id = f"rerun:{hashlib.sha256(encoded).hexdigest()}"
    event = {"id": event_id, "type": "capgate.rerun", "subject": event_id, "detail": task_id}
    endpoint = url.copy_with(path="/v1/events", query=None, fragment=None)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(
                endpoint, json=event, headers={"Authorization": f"Bearer {key}"}
            )
            response.raise_for_status()
            receipt = RerunReceipt.model_validate(response.json())
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(502, "agent-autoloop 未确认重跑事件，请检查中台与来源密钥") from error
    if not receipt.routed:
        raise HTTPException(409, f"agent-autoloop 事件 {receipt.event} 未匹配重跑规则")
    return receipt


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
    async def create_task(
        body: TaskIn,
        idempotency_key: Annotated[
            str | None, Header(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")
        ] = None,
    ) -> dict[str, Any]:
        try:
            spec = system.orchestrator.prepare_task(TaskSpec(**body.model_dump()))
        except TaxonomyError as exc:
            raise HTTPException(422, str(exc)) from exc
        if idempotency_key is not None:
            spec.id = f"task_{uuid5(NAMESPACE_URL, f'capgate:tasks:{idempotency_key}').hex}"
        if not system.ledger.reserve_task(spec):
            existing = system.ledger.task(spec.id)
            if existing is None:
                raise HTTPException(409, "任务预留失败")
            previous = TaskSpec.model_validate_json(existing["spec"])
            if previous.model_dump(exclude={"taxonomy_version"}) != spec.model_dump(
                exclude={"taxonomy_version"}
            ):
                raise HTTPException(409, "此 Idempotency-Key 已用于另一份任务规格")
            return {"id": spec.id, "status": existing["status"]}

        async def runner() -> None:
            # 失败已经落账；这里只保证后台任务不把异常抛到事件循环外面
            with contextlib.suppress(Exception):
                await system.orchestrator.run(spec, reserved=True)

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

    @app.post("/api/tasks/{task_id}/pin")
    async def pin_env(task_id: str, body: PinIn) -> dict[str, Any]:
        task = system.ledger.task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        env = body.env.strip()
        if env not in system.registry or all(
            run["env_id"] != env for run in system.ledger.runs(task_id)
        ):
            raise HTTPException(422, "所选环境必须已注册并在该任务中运行过")
        task_type = task["task_type"]
        try:
            if system.taxonomy is not None:
                task_type = system.taxonomy.normalize_type(task_type)
            pins = set_pin(system.paths.root, task_type, env=env)
        except (PinError, TaxonomyError) as exc:
            raise HTTPException(422, str(exc)) from exc
        system.router.config.pins = pins
        return {"ok": True, "task_type": task_type, "env": env}

    @app.post("/api/tasks/{task_id}/rerun")
    async def request_rerun(
        task_id: str,
        idempotency_key: Annotated[
            str | None, Header(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
        ] = None,
    ) -> RerunReceipt:
        task = system.ledger.task(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        if task["status"] not in {"ok", "degraded", "failed"}:
            raise HTTPException(409, "任务尚未结束")
        return await _request_rerun(task_id, idempotency_key or str(uuid4()))

    return app
