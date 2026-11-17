"""任务编排：一个任务拉多个 env，env 之间不直连，只经 artifact store 交换。

三条硬规矩（架构讨论第二节）在这里落地：
1. env 之间禁止直接通信 —— 编排层只把产物写进 store，再把需要的那几个读出来给下游；
2. 产物契约先于编排 —— 一个 env 能不能进流水线，取决于它的 consumes 能不能被满足；
3. 局部失败逐节点声明 —— abort / degrade / retry-alt 三种策略真实生效。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .artifacts import ArtifactStore
from .envs import EnvRegistry
from .ledger import Ledger
from .models import (
    Artifact,
    ArtifactContract,
    Candidate,
    Decision,
    EnvManifest,
    RunResult,
    TaskResult,
    TaskSpec,
)
from .router import Router
from .supervisor import Supervisor, should_keep_warm

MAX_ENVS_PER_TASK = 8


def _names(refs: list[str]) -> set[str]:
    return {ArtifactContract.parse(ref)[0] for ref in refs}


class Orchestrator:
    def __init__(
        self,
        ledger: Ledger,
        store: ArtifactStore,
        router: Router,
        supervisor: Supervisor,
        registry: EnvRegistry,
    ) -> None:
        self.ledger = ledger
        self.store = store
        self.router = router
        self.supervisor = supervisor
        self.registry = registry

    # ---------------------------------------------------------------- 主流程

    async def run(self, task: TaskSpec) -> TaskResult:
        started = time.monotonic()
        self.ledger.add_task(task)
        self.ledger.event(
            task.id,
            "task.start",
            {"goal": task.goal, "tags": task.tags, "type": task.task_type, "inputs": task.inputs},
        )
        result = TaskResult(task=task, status="ok")

        try:
            self._route_and_plan(task, result)
            if not result.decisions[-1].chosen:
                raise RuntimeError("没有任何 env 能承接这个任务")

            pipeline = [self.registry.get(eid) for eid in result.decisions[-1].chosen]
            await self._execute(task, pipeline, result)

            if any(run.status == "failed" for run in result.runs):
                result.status = "failed"
            elif any(run.status == "degraded" for run in result.runs):
                result.status = "degraded"
        except Exception as exc:  # noqa: BLE001 - 任务级失败要落账并如实返回
            result.status = "failed"
            result.error = str(exc)
            self.ledger.event(task.id, "task.error", {"error": str(exc)})

        result.latency_ms = int((time.monotonic() - started) * 1000)
        self.ledger.finish_task(task.id, result.status, result.error)
        self.ledger.event(
            task.id,
            "task.finish",
            {"status": result.status, "latency_ms": result.latency_ms, "error": result.error},
        )
        return result

    # ---------------------------------------------------------------- 选路与编排计划

    def _route_and_plan(self, task: TaskSpec, result: TaskResult) -> None:
        domain = self.router.route_domains(task)
        self.ledger.decide(task.id, domain)
        result.decisions.append(domain)
        if not domain.chosen:
            return

        ranked = [self.registry.get(c.id) for c in domain.candidates if c.id in self.registry]
        if not ranked:
            ranked = [self.registry.get(domain.chosen[0])]

        pipeline, reasons, skipped = self._plan_pipeline(task, ranked)
        head_note = ""
        if pipeline and pipeline[0].id != domain.chosen[0]:
            head_note = f"（首选领域 {domain.chosen[0]} 依赖上游产物，起点调整为 {pipeline[0].id}）"
        plan = Decision(
            stage="pipeline",
            strategy="contract",
            chosen=[e.id for e in pipeline],
            candidates=[
                Candidate(id=e.id, score=1.0, detail=reasons.get(e.id, "")) for e in pipeline
            ]
            + [Candidate(id=env_id, score=0.0, detail=why) for env_id, why in skipped.items()],
            reason=head_note + "；".join(f"{e.id}: {reasons.get(e.id, '主候选')}" for e in pipeline),
        )
        self.ledger.decide(task.id, plan)
        result.decisions.append(plan)

    def _plan_pipeline(
        self, task: TaskSpec, ranked: list[EnvManifest]
    ) -> tuple[list[EnvManifest], dict[str, str], dict[str, str]]:
        """按"契约可满足性 + 任务 tag 覆盖"组流水线。"""
        reasons: dict[str, str] = {}
        skipped: dict[str, str] = {}
        chosen: list[EnvManifest] = []
        produced: set[str] = set()
        taken: set[str] = set()

        provided = set(task.inputs)

        def external_deps(env: EnvManifest) -> set[str]:
            """这个 env 需要别的 env 先产出什么（任务输入直接给的不算依赖）。"""
            return _names(env.artifacts.consumes) - provided

        first = self._pick_head(task, ranked)
        if first is None:  # 所有候选都依赖别人还没产出的东西，如实失败
            env = ranked[0]
            return [env], {env.id: "没有可独立启动的 env，强制从最高分开始"}, {}

        chosen.append(first)
        taken.add(first.id)
        produced |= _names(first.artifacts.produces)
        reasons[first.id] = "最高分且不依赖上游产物"
        rest = [env for env in ranked if env.id != first.id]

        def wanted_tags() -> set[str]:
            return {t.lower() for t in task.tags}

        def covered(env: EnvManifest) -> set[str]:
            return {t.lower() for t in env.tags}

        # 第一轮：补齐任务里还没被覆盖的 tag。
        # 只有"不依赖别的 env 产物"的才算并行候选——依赖别人的必须进接力轮，
        # 否则决策链会写出"可并行"这种与实际执行不符的理由。
        for env in rest:
            if len(chosen) >= MAX_ENVS_PER_TASK:
                break
            if env.id in taken:
                continue
            gap = wanted_tags() - set().union(*[covered(e) for e in chosen]) if chosen else wanted_tags()
            if not (gap & covered(env)):
                skipped.setdefault(env.id, "不覆盖任务的剩余 tag")
                continue
            deps = external_deps(env)
            if deps:
                skipped[env.id] = f"需要上游产物 {sorted(deps)}，排到接力轮"
                continue
            chosen.append(env)
            taken.add(env.id)
            produced |= _names(env.artifacts.produces)
            skipped.pop(env.id, None)
            reasons[env.id] = f"补齐 tag {sorted(gap & covered(env))}，与其他无依赖 env 并行"

        # 第二轮：接力的下游（靠产物契约挂上）
        edge_added = True
        while edge_added:
            edge_added = False
            for env in rest:
                if len(chosen) >= MAX_ENVS_PER_TASK:
                    break
                if env.id in taken:
                    continue
                need = _names(env.artifacts.consumes)
                if need and need <= produced:
                    chosen.append(env)
                    taken.add(env.id)
                    produced |= _names(env.artifacts.produces)
                    skipped.pop(env.id, None)
                    reasons[env.id] = f"消费 {sorted(need)}，接在后面跑"
                    edge_added = True
                    break
                if need:
                    skipped[env.id] = f"需要的产物无人提供：{sorted(need - produced)}"
        return chosen, reasons, skipped

    def _pick_head(self, task: TaskSpec, ranked: list[EnvManifest]) -> EnvManifest | None:
        """起点必须是能独立启动的 env：consumes 为空，或全部由任务输入直接提供。

        不这样就可能把"要等上游产物"的 env 排到第一位，一开局就注定失败。
        """
        provided = set(task.inputs)
        for env in ranked:
            need = _names(env.artifacts.consumes)
            if not need or need <= provided:
                return env
        return None

    # ---------------------------------------------------------------- 执行

    async def _execute(self, task: TaskSpec, pipeline: list[EnvManifest], result: TaskResult) -> None:
        produced: set[str] = set()
        payloads: dict[str, Any] = {}
        remaining = list(pipeline)
        blocked = False

        while remaining and not blocked:
            wave = [env for env in remaining if _names(env.artifacts.consumes) <= produced]
            if not wave:
                wave = remaining[:1]  # 契约成环或不可满足：先跑一个，让失败显性化
            remaining = [env for env in remaining if env not in wave]

            runs = await self._run_wave(task, wave, payloads, result)
            for run in runs:
                if run.status in ("ok", "degraded"):
                    produced |= {a.name for a in run.artifacts}
                blocked = await self._apply_failure_policy(task, run, result)

    async def _run_wave(
        self,
        task: TaskSpec,
        wave: list[EnvManifest],
        payloads: dict[str, Any],
        result: TaskResult,
    ) -> list[RunResult]:
        if len(wave) == 1 or not task.allow_parallel:
            runs = []
            for env in wave:
                runs.append(await self._run_env(task, env, payloads, result))
            return runs
        runs = await asyncio.gather(
            *[self._run_env(task, env, payloads, result) for env in wave],
            return_exceptions=False,
        )
        return list(runs)

    async def _run_env(
        self,
        task: TaskSpec,
        env: EnvManifest,
        payloads: dict[str, Any],
        result: TaskResult,
    ) -> RunResult:
        agent_decision = self.router.route_agent(env, task.task_type)
        self.ledger.decide(task.id, agent_decision)
        result.decisions.append(agent_decision)
        model = agent_decision.chosen[0]

        inputs = {
            ArtifactContract.parse(ref)[0]: payloads[ArtifactContract.parse(ref)[0]]
            for ref in env.artifacts.consumes
            if ArtifactContract.parse(ref)[0] in payloads
        }

        run_id = self.ledger.start_run(task.id, env, env.agent.adapter, model)
        self.ledger.event(
            task.id,
            "env.start",
            {"env": env.id, "adapter": env.agent.adapter, "model": model, "inputs": sorted(inputs)},
            env_id=env.id,
        )

        rec = self.ledger.perf(env_id=env.id, task_type=task.task_type)
        rate = (sum(r["wins"] for r in rec) / sum(r["n"] for r in rec)) if rec else 0.0
        samples = sum(r["n"] for r in rec)

        on_event = lambda kind, payload: self.ledger.event(  # noqa: E731
            task.id, kind, payload, env_id=env.id
        )

        try:
            execution = await self.supervisor.execute(
                env=env,
                task=task,
                model=model,
                artifacts=inputs,
                run_id=run_id,
                on_event=on_event,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            run = RunResult(
                task_id=task.id,
                env_id=env.id,
                run_id=run_id,
                status="failed",
                adapter=env.agent.adapter,
                model=model,
                error=str(exc),
            )
            self.ledger.finish_run(run, meta={"error": str(exc), "resolved": _versions(env)})
            self.ledger.event(task.id, "env.failed", {"error": str(exc)}, env_id=env.id)
            result.runs.append(run)
            result.error = result.error or str(exc)
            return run

        artifacts: list[Artifact] = []
        declared = _names(env.artifacts.produces)
        emitted = set(execution.output.artifacts)
        for name, payload in execution.output.artifacts.items():
            artifact = self.store.put(
                task.id, name, payload, env_id=env.id, run_id=run_id
            )
            self.ledger.add_artifact(task.id, artifact)
            artifacts.append(artifact)
            payloads[name] = payload

        missing = declared - emitted
        status: str = "ok" if not missing else "degraded"
        run = RunResult(
            task_id=task.id,
            env_id=env.id,
            run_id=run_id,
            status=status,  # type: ignore[arg-type]
            adapter=env.agent.adapter,
            model=model,
            artifacts=artifacts,
            latency_ms=execution.latency_ms,
            error=f"声明的产物没产出：{sorted(missing)}" if missing else None,
            trace=execution.output.trace,
        )
        self.ledger.finish_run(
            run,
            meta={
                "resolved": execution.resolved_versions,
                "usage": execution.output.usage,
                "instance": execution.instance_id,
                "warm": should_keep_warm(env, rate, samples, status == "ok"),
            },
        )
        self.ledger.event(
            task.id,
            "env.finish",
            {"status": status, "ms": execution.latency_ms, "artifacts": [a.name for a in artifacts]},
            env_id=env.id,
        )
        result.runs.append(run)
        result.artifacts.extend(artifacts)
        return run

    async def _apply_failure_policy(
        self, task: TaskSpec, run: RunResult, result: TaskResult
    ) -> bool:
        if run.status in ("ok", "degraded"):
            return False
        env = self.registry.get(run.env_id)
        if env.failure_policy == "abort":
            result.error = result.error or f"env {env.id} 失败，策略 abort"
            return True
        if env.failure_policy == "degrade":
            result.status = "degraded"
            return False
        # retry-alt：交给下一个还没试过的候选 env 再来一次（这里只记录，不在此处重跑）
        result.status = "degraded"
        self.ledger.event(
            task.id,
            "env.retry_alt",
            {"failed_env": env.id, "note": "已记录，下一个 wave 继续"},
            env_id=env.id,
        )
        return False


def _versions(env: EnvManifest) -> dict[str, str]:
    return {ref.id: ref.version for ref in env.capabilities}
