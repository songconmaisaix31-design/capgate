"""任务编排：一个任务拉多个 env，env 之间不直连，只经 artifact store 交换。

三条硬规矩（架构讨论第二节）在这里落地：
1. env 之间禁止直接通信 —— 编排层只把产物写进 store，再把需要的那几个读出来给下游；
2. 产物契约先于编排 —— 一个 env 能不能进流水线，取决于它的 consumes 能不能被满足；
3. 局部失败逐节点声明 —— abort / degrade / retry-alt 三种策略真实生效。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from .agents.base import AgentError
from .agents.openai_compat import _total_usage
from .artifacts import ArtifactStore, StreamReader, StreamSession, StreamWriter
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
from .taxonomy import Taxonomy

MAX_ENVS_PER_TASK = 8
MAX_ALT_RETRIES = 2


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
        *,
        taxonomy: Taxonomy | None = None,
    ) -> None:
        self.ledger = ledger
        self.store = store
        self.router = router
        self.supervisor = supervisor
        self.registry = registry
        self.taxonomy = taxonomy

    # ---------------------------------------------------------------- 主流程

    def prepare_task(self, task: TaskSpec) -> TaskSpec:
        if self.taxonomy is None:
            return task
        return task.model_copy(
            update={
                "tags": self.taxonomy.normalize_tags(task.tags, strict=True),
                "type": self.taxonomy.normalize_type(task.type),
                "taxonomy_version": self.taxonomy.version,
            }
        )

    async def run(self, task: TaskSpec, *, reserved: bool = False) -> TaskResult:
        if not reserved:
            task = self.prepare_task(task)
            if not self.ledger.reserve_task(task):
                raise ValueError(f"任务 {task.id!r} 已存在")
        started = time.monotonic()
        self.ledger.event(
            task.id,
            "task.start",
            {"goal": task.goal, "tags": task.tags, "type": task.task_type, "inputs": task.inputs},
        )
        result = TaskResult(task=task, status="ok")

        try:
            await self._route_and_plan(task, result)
            if not result.decisions[-1].chosen:
                raise RuntimeError("没有任何 env 能承接这个任务")

            pipeline = [self.registry.get(eid) for eid in result.decisions[-1].chosen]
            effective_runs = await self._execute(task, pipeline, result)

            if any(run.status == "failed" for run in effective_runs):
                result.status = "failed"
            elif any(run.status == "degraded" for run in effective_runs):
                result.status = "degraded"
        except Exception as exc:
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

    async def _route_and_plan(self, task: TaskSpec, result: TaskResult) -> None:
        domain = await self.router.route_domains_async(
            task,
            on_judge=lambda payload: self.ledger.event(task.id, "routing.llm_judge", payload),
        )
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
            reason=head_note
            + "；".join(f"{e.id}: {reasons.get(e.id, '主候选')}" for e in pipeline),
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
            gap = (
                wanted_tags() - set().union(*[covered(e) for e in chosen])
                if chosen
                else wanted_tags()
            )
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

    async def _execute(
        self, task: TaskSpec, pipeline: list[EnvManifest], result: TaskResult
    ) -> list[RunResult]:
        produced: set[str] = set()
        payloads: dict[str, Any] = {}
        payload_meta: dict[str, Artifact] = {}
        remaining = list(pipeline)
        blocked = False
        effective_runs: list[RunResult] = []

        while remaining and not blocked:
            attempted = {run.env_id for run in result.runs}
            remaining = [env for env in remaining if env.id not in attempted]
            if not remaining:
                break
            wave = [env for env in remaining if _names(env.artifacts.consumes) <= produced]
            if not wave:
                wave = remaining[:1]  # 契约成环或不可满足：先跑一个，让失败显性化
            remaining = [env for env in remaining if env not in wave]

            # 流式 producer 与其流式 consumer 并发跑（见 _run_streaming_link）。
            stream_producers = [
                env
                for env in wave
                if env.stream
                and _names(env.artifacts.produces)
                and any(
                    c.stream and _names(c.artifacts.consumes) & _names(env.artifacts.produces)
                    for c in remaining
                )
            ]
            normal_wave = [env for env in wave if env not in stream_producers]

            if normal_wave:
                runs = await self._run_wave(task, normal_wave, payloads, payload_meta, result)
                for run in runs:
                    stop, effective_run = await self._apply_failure_policy(
                        task, run, result, payloads, payload_meta
                    )
                    blocked = stop
                    if effective_run is not None:
                        effective_runs.append(effective_run)

            for producer in stream_producers:
                produces = _names(producer.artifacts.produces)
                consumers = [
                    env
                    for env in remaining
                    if env.stream and _names(env.artifacts.consumes) & produces
                ]
                remaining = [env for env in remaining if env not in consumers]
                runs = await self._run_streaming_link(
                    task, producer, consumers, payloads, payload_meta, result
                )
                for run in runs:
                    stop, effective_run = await self._apply_failure_policy(
                        task, run, result, payloads, payload_meta
                    )
                    blocked = stop
                    if effective_run is not None:
                        effective_runs.append(effective_run)

            produced = set(payloads)
        return effective_runs

    async def _run_streaming_link(
        self,
        task: TaskSpec,
        producer: EnvManifest,
        consumers: list[EnvManifest],
        payloads: dict[str, Any],
        payload_meta: dict[str, Artifact],
        result: TaskResult,
    ) -> list[RunResult]:
        """流式链接：producer 与它的流式 consumer 并发跑，chunk 经产物库流转。

        producer 每写一条 chunk 立即落盘到 ``.stream`` 并用内存事件通知，consumer
        增量读同一个文件——**chunk 数据走磁盘、通知走内存**，所以下游能在上游
        写出最终快照之前就消费到先到的 chunk。
        """
        writers: dict[str, StreamWriter] = {}
        for name in _names(producer.artifacts.produces):
            writers[name] = self.store.open_stream(task.id, name, env_id=producer.id)

        consumer_sessions: dict[str, StreamSession] = {}
        for consumer in consumers:
            readers: dict[str, StreamReader] = {}
            for ref in consumer.artifacts.consumes:
                name = ArtifactContract.parse(ref)[0]
                if name in writers:
                    readers[name] = self.store.read_stream(writers[name])
            consumer_sessions[consumer.id] = StreamSession(readers=readers)

        coros = [
            self._run_env(
                task,
                producer,
                payloads,
                payload_meta,
                result,
                stream=StreamSession(writers=writers),
            )
        ]
        for consumer in consumers:
            coros.append(
                self._run_env(
                    task,
                    consumer,
                    payloads,
                    payload_meta,
                    result,
                    stream=consumer_sessions[consumer.id],
                )
            )
        runs = await asyncio.gather(*coros, return_exceptions=False)

        # 引用计数补记：流式产物的 sha 要到 producer seal 后才已知，此时再登记
        # consumer 的消费，喂给 GC 判断"被引用则不能删"。
        for consumer, run in zip(consumers, runs[1:], strict=False):
            for ref in consumer.artifacts.consumes:
                name = ArtifactContract.parse(ref)[0]
                if name in payload_meta:
                    self.ledger.add_artifact_ref(
                        task.id, payload_meta[name].sha256, consumer.id, run.run_id
                    )

        # 收尾：关掉流式文件句柄，删掉临时的 .stream（最终快照是 .json）。
        for session in consumer_sessions.values():
            for reader in session.readers.values():
                reader.close()
        for writer in writers.values():
            writer.close()
            with contextlib.suppress(OSError):
                writer.stream_path.unlink(missing_ok=True)
        return list(runs)

    async def _run_wave(
        self,
        task: TaskSpec,
        wave: list[EnvManifest],
        payloads: dict[str, Any],
        payload_meta: dict[str, Artifact],
        result: TaskResult,
    ) -> list[RunResult]:
        if len(wave) == 1 or not task.allow_parallel:
            runs = []
            for env in wave:
                runs.append(await self._run_env(task, env, payloads, payload_meta, result))
            return runs
        runs = await asyncio.gather(
            *[self._run_env(task, env, payloads, payload_meta, result) for env in wave],
            return_exceptions=False,
        )
        return list(runs)

    async def _run_env(
        self,
        task: TaskSpec,
        env: EnvManifest,
        payloads: dict[str, Any],
        payload_meta: dict[str, Artifact],
        result: TaskResult,
        stream: StreamSession | None = None,
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
        # 引用计数：把这次 env 消费了哪些产物登记下来，供 GC 判断"被引用则不能删"。
        for ref in env.artifacts.consumes:
            name = ArtifactContract.parse(ref)[0]
            if name in payload_meta:
                self.ledger.add_artifact_ref(task.id, payload_meta[name].sha256, env.id, run_id)
        self.ledger.event(
            task.id,
            "env.start",
            {"env": env.id, "adapter": env.agent.adapter, "model": model, "inputs": sorted(inputs)},
            env_id=env.id,
        )

        rec = self.ledger.perf(env_id=env.id, task_type=task.task_type)
        rate = (sum(r["wins"] for r in rec) / sum(r["n"] for r in rec)) if rec else 0.0
        samples = sum(r["n"] for r in rec)

        requests: list[dict[str, Any]] = []

        def on_event(kind: str, payload: dict[str, Any]) -> None:
            self.ledger.event(task.id, kind, payload, env_id=env.id)
            if kind == "llm.usage":
                requests.append(
                    {
                        field: value
                        for field, value in payload.items()
                        if field not in {"run_id", "turn", "model"}
                    }
                )

        started = time.monotonic()
        execution = None
        artifacts: list[Artifact] = []
        outputs: dict[str, Any] = {}
        mode = "stream" if env.stream else "block"
        try:
            execution = await self.supervisor.execute(
                env=env,
                task=task,
                model=model,
                artifacts=inputs,
                run_id=run_id,
                on_event=on_event,
                stream=stream,
            )
            if stream is not None and stream.writers:
                for name, writer in stream.writers.items():
                    artifact = writer.seal()
                    self.ledger.add_artifact(task.id, artifact)
                    artifacts.append(artifact)
                    outputs[name] = self.store.load(artifact)
            else:
                for name, payload in execution.output.artifacts.items():
                    artifact = self.store.put(
                        task.id, name, payload, env_id=env.id, run_id=run_id, stream=env.stream
                    )
                    self.ledger.add_artifact(task.id, artifact)
                    artifacts.append(artifact)
                    outputs[name] = payload
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if execution is not None:
                usage = execution.output.usage
            elif isinstance(exc, AgentError) and exc.usage:
                usage = exc.usage
            else:
                usage = _total_usage(requests)
            if stream is not None and stream.writers:
                for name, writer in stream.writers.items():
                    if writer.sealed:
                        continue
                    writer.mark_truncated()
                    artifact = writer.seal()
                    self.ledger.add_artifact(task.id, artifact)
                    artifacts.append(artifact)
                    payloads[name] = self.store.load(artifact)
                    payload_meta[name] = artifact
            run = RunResult(
                task_id=task.id,
                env_id=env.id,
                run_id=run_id,
                status="failed",
                adapter=env.agent.adapter,
                model=model,
                artifacts=artifacts,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
                trace=execution.output.trace if execution is not None else [],
            )
            self.ledger.finish_run(
                run,
                meta={
                    "error": str(exc),
                    "resolved": (
                        execution.resolved_versions if execution is not None else _versions(env)
                    ),
                    "usage": usage,
                    "summary": execution.output.text if execution is not None else "",
                    "mode": mode,
                },
            )
            self.ledger.event(task.id, "env.failed", {"error": str(exc)}, env_id=env.id)
            result.runs.append(run)
            result.artifacts.extend(artifacts)
            if execution is not None:
                raise
            return run

        declared = _names(env.artifacts.produces)
        payloads.update(outputs)
        payload_meta.update((artifact.name, artifact) for artifact in artifacts)
        missing = declared - outputs.keys()
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
                "summary": execution.output.text,
                "instance": execution.instance_id,
                "warm": should_keep_warm(env, rate, samples, status == "ok"),
                "mode": mode,
            },
        )
        self.ledger.event(
            task.id,
            "env.finish",
            {
                "status": status,
                "ms": execution.latency_ms,
                "artifacts": [a.name for a in artifacts],
                "mode": mode,
            },
            env_id=env.id,
        )
        result.runs.append(run)
        result.artifacts.extend(artifacts)
        return run

    async def _apply_failure_policy(
        self,
        task: TaskSpec,
        run: RunResult,
        result: TaskResult,
        payloads: dict[str, Any],
        payload_meta: dict[str, Artifact],
    ) -> tuple[bool, RunResult | None]:
        if run.status in ("ok", "degraded"):
            return False, run
        env = self.registry.get(run.env_id)
        if env.failure_policy == "abort":
            result.error = result.error or run.error or f"env {env.id} 失败，策略 abort"
            return True, run
        if env.failure_policy == "degrade":
            result.status = "degraded"
            result.error = result.error or run.error
            return False, run
        return False, await self._retry_alternatives(task, env, run, result, payloads, payload_meta)

    async def _retry_alternatives(
        self,
        task: TaskSpec,
        original: EnvManifest,
        run: RunResult,
        result: TaskResult,
        payloads: dict[str, Any],
        payload_meta: dict[str, Artifact],
    ) -> RunResult | None:
        domain = next(decision for decision in result.decisions if decision.stage == "domain")
        failures = [run]
        for attempt in range(1, MAX_ALT_RETRIES + 1):
            alternative, candidates = self._retry_candidates(original, domain, result, payloads)
            reason = f"env {run.env_id} 执行失败：{run.error or '未提供失败原因'}；"
            if alternative is None:
                reason += "领域候选中没有未尝试且满足输入与产出契约的 env，按 degrade 处理"
            else:
                reason += (
                    f"按领域候选顺序换到 {alternative.id}，当前产物满足其 consumes，"
                    f"其 produces 覆盖 {original.id} 的声明，"
                    f"执行第 {attempt}/{MAX_ALT_RETRIES} 次替代重试"
                )
            decision = Decision(
                stage="retry",
                strategy="retry-alt",
                chosen=[alternative.id] if alternative else [],
                candidates=candidates,
                reason=reason,
            )
            self.ledger.decide(task.id, decision)
            result.decisions.append(decision)
            if alternative is None:
                break
            self.ledger.event(
                task.id,
                "env.retry_alt",
                {"failed_env": run.env_id, "replacement_env": alternative.id, "attempt": attempt},
                env_id=run.env_id,
            )
            run = await self._run_env(task, alternative, payloads, payload_meta, result)
            if run.status in ("ok", "degraded"):
                return run
            failures.append(run)

        result.status = "degraded"
        error = "替代重试未成功，按 degrade 处理；已尝试：" + "；".join(
            f"env {failed.env_id}：{failed.error or '未提供失败原因'}" for failed in failures
        )
        result.error = f"{result.error}；{error}" if result.error else error
        return None

    def _retry_candidates(
        self,
        original: EnvManifest,
        domain: Decision,
        result: TaskResult,
        payloads: dict[str, Any],
    ) -> tuple[EnvManifest | None, list[Candidate]]:
        attempted = {run.env_id for run in result.runs}
        required = _names(original.artifacts.produces)
        produced = set(payloads)
        chosen: EnvManifest | None = None
        candidates = []
        for candidate in domain.candidates:
            exclusions = []
            if candidate.id in attempted:
                exclusions.append("已经尝试过")
            if candidate.id not in self.registry:
                exclusions.append("env 未注册")
            else:
                env = self.registry.get(candidate.id)
                missing_inputs = _names(env.artifacts.consumes) - produced
                missing_outputs = required - _names(env.artifacts.produces)
                if missing_inputs:
                    exclusions.append(f"缺少已产出的输入产物：{sorted(missing_inputs)}")
                if missing_outputs:
                    exclusions.append(f"未声明原 env 所需的产物：{sorted(missing_outputs)}")
            if exclusions:
                detail = "排除：" + "；".join(exclusions)
            elif chosen is None:
                chosen = self.registry.get(candidate.id)
                detail = "输入与产出契约满足，按领域候选顺序选中"
            else:
                detail = "输入与产出契约满足，领域候选顺序靠后"
            if candidate.detail:
                detail = f"{candidate.detail}；{detail}"
            candidates.append(candidate.model_copy(update={"detail": detail}))
        return chosen, candidates


def _versions(env: EnvManifest) -> dict[str, str]:
    return {ref.id: ref.version for ref in env.capabilities}
