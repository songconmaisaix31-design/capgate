"""装配：把能力池、env 注册表、账本、产物库、路由、监管者接起来。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .artifacts import ArtifactStore
from .config import Paths
from .config import paths as resolve_paths
from .envs import EnvRegistry
from .ledger import Ledger
from .mcp import MCPHub
from .models import GcReport
from .orchestrator import Orchestrator
from .pool import Pool
from .router import LLMJudge, Router, RouterConfig
from .supervisor import Supervisor


@dataclass
class System:
    paths: Paths
    pool: Pool
    registry: EnvRegistry
    ledger: Ledger
    store: ArtifactStore
    hub: MCPHub
    router: Router
    supervisor: Supervisor
    orchestrator: Orchestrator
    errors: list[str] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.supervisor.aclose()
        await self.hub.aclose()
        self.ledger.close()

    async def reap_forever(self, interval_s: float = 60.0) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                await self.supervisor.reap()
        except asyncio.CancelledError:
            return

    def warm(self) -> dict[str, int]:
        return self.supervisor.warm_stats()

    def gc(
        self, ttl_days: float = 30.0, dry_run: bool = False, now: datetime | None = None
    ) -> GcReport:
        """清理过期产物：引用计数护住未完成任务与被消费产物，删除动作记入账本。"""
        now = now or datetime.now(UTC)
        candidates, protected, scanned = self.ledger.gc_candidates(ttl_days, now)
        report = GcReport(
            dry_run=dry_run,
            scanned=scanned,
            expired=len(candidates),
            protected=protected,
            items=candidates,
        )
        deleted = 0
        reclaimed = 0
        if not dry_run:
            for item in candidates:
                if self.store.delete(item.path):
                    self.ledger.record_gc(item.task_id, item.sha256, item.path, item.bytes)
                    deleted += 1
                    reclaimed += item.bytes
            report.deleted = deleted
            report.reclaimed_bytes = reclaimed
        return report


def load_router_config(root: Path) -> RouterConfig:
    path = root / "router.yaml"
    if not path.exists():
        return RouterConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = set(RouterConfig.__dataclass_fields__)
    return RouterConfig(**{k: v for k, v in raw.items() if k in known})


def build(
    root: Path | str | None = None,
    router_config: RouterConfig | None = None,
    judge: LLMJudge | None = None,
    state: Path | str | None = None,
) -> System:
    p = resolve_paths(root, state)
    pool = Pool(p.capabilities).load()
    registry = EnvRegistry(p.envs).load()
    ledger = Ledger(p.db)
    store = ArtifactStore(p.artifacts, p.schemas)
    hub = MCPHub()
    config = router_config or load_router_config(p.root)
    router = Router(ledger, registry, config, judge=judge)
    supervisor = Supervisor(pool, hub)
    orchestrator = Orchestrator(ledger, store, router, supervisor, registry)
    return System(
        paths=p,
        pool=pool,
        registry=registry,
        ledger=ledger,
        store=store,
        hub=hub,
        router=router,
        supervisor=supervisor,
        orchestrator=orchestrator,
        errors=list(pool.errors) + list(registry.errors),
    )
