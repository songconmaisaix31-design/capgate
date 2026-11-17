"""环境监管者：拉起、复用、限流、回收、取消。

"表现好的留下" 在这里落成三条互不相同的机制（架构讨论第四节）：
    常驻热实例   —— 本模块的 warm pool（保留下面的 EnvInstance）
    默认路由意图 —— 由账本里的成绩驱动 router（不在这里）
    可复现快照   —— 能力池的版本钉死 + 账本里记录的 exact version

热实例只在 env 声明 warm、或该 env 在账本里成绩够好时才保留；
否则用完即回收，避免只进不出的僵尸池。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .agents import AgentContext, AgentError, AgentOutput, ToolBox, build
from .artifacts import StreamSession, current_stream
from .models import EnvManifest, TaskSpec, new_id
from .pool import Pool, Resolved

WARM_MIN_RATE = 0.8
WARM_MIN_SAMPLES = 3
MAX_WARM_PER_ENV = 2


@dataclass
class EnvInstance:
    env_id: str
    instance_id: str
    resolved: dict[str, Resolved]
    toolbox: ToolBox
    created_at: float
    last_used: float
    uses: int = 0


@dataclass
class Execution:
    output: AgentOutput
    latency_ms: int
    instance_id: str
    resolved_versions: dict[str, str] = field(default_factory=dict)


class Supervisor:
    def __init__(self, pool: Pool, hub: Any, ttl_default: int = 900) -> None:
        self.pool = pool
        self.hub = hub
        self.ttl_default = ttl_default
        self._pool: dict[str, list[EnvInstance]] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    # ---------------------------------------------------------------- 实例

    def _sem(self, env: EnvManifest) -> asyncio.Semaphore:
        sem = self._sems.get(env.id)
        if sem is None:
            sem = asyncio.Semaphore(env.resources.max_parallel)
            self._sems[env.id] = sem
        return sem

    async def acquire(self, env: EnvManifest) -> EnvInstance:
        async with self._lock:
            bucket = self._pool.get(env.id) or []
            if bucket:
                inst = bucket.pop()
                inst.last_used = time.monotonic()
                return inst
        resolved = self.pool.resolve_env(env)
        instance_id = new_id("inst")
        toolbox = ToolBox(self.pool, self.hub, resolved, instance_id=instance_id)
        return EnvInstance(
            env_id=env.id,
            instance_id=instance_id,
            resolved=resolved,
            toolbox=toolbox,
            created_at=time.monotonic(),
            last_used=time.monotonic(),
        )

    async def release(self, inst: EnvInstance, env: EnvManifest, keep: bool) -> None:
        inst.uses += 1
        if keep and not self._closed:
            bucket = self._pool.setdefault(inst.env_id, [])
            if len(bucket) < MAX_WARM_PER_ENV:
                bucket.append(inst)
                return
        await self._dispose(inst)

    async def _dispose(self, inst: EnvInstance) -> None:
        # 非共享能力是 per-instance 连接，随实例一起关掉；回收失败不能影响主流程
        with contextlib.suppress(Exception):
            await self.hub.drop_prefix(f"{inst.instance_id}:")

    # ---------------------------------------------------------------- 执行

    async def execute(
        self,
        *,
        env: EnvManifest,
        task: TaskSpec,
        model: str,
        artifacts: dict[str, Any],
        run_id: str,
        budget_s: int | None = None,
        keep_warm: bool = False,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        stream: StreamSession | None = None,
    ) -> Execution:
        adapter = build(env.agent.adapter, env.agent.config)
        sem = self._sem(env)
        async with sem:
            inst = await self.acquire(env)
            ctx = AgentContext(
                task=task,
                env=env,
                model=model,
                artifacts=artifacts,
                toolbox=inst.toolbox,
                budget_s=budget_s or task.budget_s,
                run_id=run_id,
                log=on_event or (lambda kind, payload: None),
            )
            started = time.monotonic()
            token = current_stream.set(stream)
            deadline = asyncio.timeout(ctx.budget_s)
            try:
                async with deadline:
                    output = await adapter.run(ctx)
            except TimeoutError as exc:
                await self.release(inst, env, keep=False)
                if not deadline.expired():
                    raise
                raise AgentError(f"env {env.id} 执行超时（{ctx.budget_s}s）") from exc
            except BaseException:
                await self.release(inst, env, keep=False)
                raise
            finally:
                current_stream.reset(token)
            latency_ms = int((time.monotonic() - started) * 1000)
            await self.release(inst, env, keep=keep_warm)
            return Execution(
                output=output,
                latency_ms=latency_ms,
                instance_id=inst.instance_id,
                resolved_versions={k: r.exact for k, r in inst.resolved.items()},
            )

    # ---------------------------------------------------------------- 回收

    async def reap(self) -> int:
        now = time.monotonic()
        reaped = 0
        async with self._lock:
            for env_id, bucket in list(self._pool.items()):
                alive = []
                for inst in bucket:
                    if now - inst.last_used > self.ttl_default:
                        await self._dispose(inst)
                        reaped += 1
                    else:
                        alive.append(inst)
                if alive:
                    self._pool[env_id] = alive
                else:
                    self._pool.pop(env_id, None)
        return reaped

    def warm_stats(self) -> dict[str, int]:
        return {env_id: len(bucket) for env_id, bucket in self._pool.items() if bucket}

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            buckets = list(self._pool.values())
            self._pool.clear()
        for bucket in buckets:
            for inst in bucket:
                await self._dispose(inst)


def should_keep_warm(env: EnvManifest, rate: float, samples: int, success: bool) -> bool:
    """只在"表现好"时留：env 声明 warm，或成绩稳定达标。"""
    if not success:
        return False
    if env.resources.warm:
        return True
    return samples >= WARM_MIN_SAMPLES and rate >= WARM_MIN_RATE
