"""双层路由。

第一层：网关 → 领域 env（按 tag 召回，按账本表现打分）
第二层：env → agent/model（域内自己的性能画像，探索也本地化）

为什么用 Thompson sampling 而不是 epsilon-greedy：它自带探索——
样本少的候选方差大，自然会被抽中去试。这正好治"第一个还行的组合被锁死"
那个病（"GPT 干文档还行 → 文档永远给 GPT"）。

LLM 只做兜底裁决，且在这里是可插拔的：默认关闭，开启后只在
"分数接近或无标签命中" 时调用，且必须留痕。
"""

from __future__ import annotations

import asyncio
import math
import os
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .agents.base import AgentError
from .envs import EnvRegistry
from .ledger import Ledger
from .llm_judge import JudgeResult, OpenAIJudge, TokenPricing
from .models import Candidate, Decision, EnvManifest, TaskSpec

LLMJudge = Callable[[TaskSpec, list[dict[str, Any]]], Awaitable[JudgeResult]]


@dataclass
class RouterConfig:
    strategy: str = "thompson"  # thompson | epsilon | static
    epsilon: float = 0.1
    latency_ref_ms: float = 4000.0  # 成本惩罚的参照值
    latency_weight: float = 0.25
    token_ref: float = 4000.0
    token_weight: float = 0.25
    llm_fallback: bool = False
    llm_margin: float = 0.12  # 头名与次名差距小于它就问 LLM
    llm_model: str | None = None
    llm_timeout_s: float = 10.0
    llm_max_tokens: int = 128
    llm_max_request_bytes: int = 16_384
    llm_reasoning_effort: str | None = None
    llm_price_usd_per_million: dict[str, float] | None = None
    pins: dict[str, dict[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(self.token_ref) or self.token_ref <= 0:
            raise ValueError("token_ref 须为正的有限数值")
        if not math.isfinite(self.token_weight) or self.token_weight < 0:
            raise ValueError("token_weight 须为非负的有限数值")
        if not math.isfinite(self.llm_timeout_s) or self.llm_timeout_s <= 0:
            raise ValueError("llm_timeout_s 须为正的有限数值")
        if self.llm_max_tokens <= 0 or self.llm_max_request_bytes <= 0:
            raise ValueError("llm_max_tokens 和 llm_max_request_bytes 须为正数")


class Router:
    def __init__(
        self,
        ledger: Ledger,
        registry: EnvRegistry,
        config: RouterConfig | None = None,
        judge: LLMJudge | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.ledger = ledger
        self.registry = registry
        self.config = config or RouterConfig()
        self.judge = judge
        if self.judge is None and self.config.llm_fallback:
            price = self.config.llm_price_usd_per_million
            self.judge = OpenAIJudge(
                model=self.config.llm_model,
                max_tokens=self.config.llm_max_tokens,
                max_request_bytes=self.config.llm_max_request_bytes,
                reasoning_effort=self.config.llm_reasoning_effort,
                pricing=TokenPricing.model_validate(price) if price is not None else None,
            )
        self.rng = rng or random.Random()

    # ---------------------------------------------------------------- 第一层

    def route_domains(self, task: TaskSpec) -> Decision:
        decision, _ = self._route_domains(task)
        return decision

    def _route_domains(self, task: TaskSpec) -> tuple[Decision, str | None]:
        task_type = task.task_type
        pinned = self.config.pins.get(task_type, {}).get("env")
        if pinned:
            env = self.registry.get(pinned)
            decision = Decision(
                stage="domain",
                strategy="pin",
                chosen=[env.id],
                candidates=[Candidate(id=env.id, score=1.0, detail="人工 pin")],
                reason=f"任务类型 {task_type!r} 被 pin 到 env {env.id!r}",
            )
            return decision, None

        if task.hint_envs:
            envs = [self.registry.get(e) for e in task.hint_envs]
            decision = Decision(
                stage="domain",
                strategy="hint",
                chosen=[e.id for e in envs],
                candidates=[Candidate(id=e.id, score=1.0, detail="调用方指定") for e in envs],
                reason="调用方通过 hint_envs 指定了领域",
            )
            return decision, None

        matched = self.registry.by_tag(task.tags)
        eligible = matched or [
            env for env in self.registry.all() if not env.agent.config.get("explicit_routing")
        ]
        scored = self._rank(eligible, task_type)
        if not scored:
            decision = Decision(
                stage="domain",
                strategy="none",
                chosen=[],
                reason="能力池里没有任何 env",
            )
            return decision, None

        strategy = self.config.strategy
        reason = (
            f"tag 命中 {len(matched)} 个 env，按 {strategy} 打分"
            if matched
            else f"没有 tag 命中的 env，回退到可自动选择的候选（按 {strategy} 打分）"
        )
        decision = Decision(
            stage="domain",
            strategy=strategy,
            chosen=[scored[0]["env"].id],
            candidates=[_candidate(row) for row in scored],
            reason=reason,
        )
        if not self.config.llm_fallback or len(scored) < 2:
            return decision, None
        if not matched:
            return decision, "没有标签命中"
        top, second = sorted((row["score"] for row in scored), reverse=True)[:2]
        margin = top - second
        trigger = f"头名分差 {margin:.3f} < {self.config.llm_margin}"
        return decision, trigger if margin < self.config.llm_margin else None

    async def route_domains_async(
        self, task: TaskSpec, *, on_judge: Callable[[dict[str, Any]], None] | None = None
    ) -> Decision:
        decision, trigger = self._route_domains(task)
        if trigger is None or self.judge is None:
            return decision

        timeout_s = min(self.config.llm_timeout_s, task.budget_s)
        if timeout_s <= 0:
            decision.reason += "；任务剩余预算不足，保留原候选"
            return decision

        candidates = {candidate.id: candidate for candidate in decision.candidates}
        payload = []
        for candidate in decision.candidates:
            env = self.registry.get(candidate.id)
            payload.append(
                {
                    "env_id": env.id,
                    "domain": env.domain,
                    "tags": env.tags,
                    "prior": env.prior,
                    "score": candidate.score,
                    "samples": candidate.success + candidate.fail,
                }
            )
        usage: dict[str, Any] = {}
        selected: str | None = None
        error: str | None = None
        try:
            async with asyncio.timeout(timeout_s):
                judgment = await self.judge(task, payload)
            usage = judgment.usage
            if judgment.env_id in candidates:
                selected = judgment.env_id
            else:
                error = "模型返回的环境不在候选列表中"
        except TimeoutError:
            error = f"裁决超时（{timeout_s} 秒）"
        except Exception as exc:
            # 裁决不改变领域执行的可用性，失败原因和已知用量保留在账本。
            if isinstance(exc, AgentError):
                usage = exc.usage
                error = str(exc)
            else:
                error = f"裁决失败（{type(exc).__name__}）"

        if on_judge is not None:
            on_judge(
                {
                    "model": self.config.llm_model or os.environ.get("CAPGATE_LLM_JUDGE_MODEL"),
                    "trigger": trigger,
                    "selected": selected,
                    "error": error,
                    "usage": usage,
                    "timeout_s": timeout_s,
                    "max_tokens": self.config.llm_max_tokens,
                }
            )
        if selected is None:
            decision.reason += f"；LLM 裁决（{trigger}）失败：{error}，保留原候选"
            return decision
        decision.strategy = "llm-fallback"
        decision.chosen = [selected]
        decision.candidates = [
            candidates[selected],
            *(candidate for candidate in decision.candidates if candidate.id != selected),
        ]
        decision.reason += f"；{trigger}，LLM 选中 {selected}"
        return decision

    # ---------------------------------------------------------------- 第二层

    def route_agent(self, env: EnvManifest, task_type: str) -> Decision:
        pinned = self.config.pins.get(task_type, {}).get("model")
        if pinned:
            return Decision(
                stage="agent",
                strategy="pin",
                chosen=[pinned],
                candidates=[Candidate(id=pinned, score=1.0, detail="人工 pin")],
                reason=f"任务类型 {task_type!r} 在 env {env.id!r} 被 pin 到模型 {pinned!r}",
            )

        models = list(env.agent.models)
        if len(models) == 1:
            return Decision(
                stage="agent",
                strategy="single",
                chosen=[models[0]],
                candidates=[Candidate(id=models[0], score=1.0, detail="env 只声明了一个模型")],
                reason="env 只有一个候选模型",
            )

        rows = self.ledger.perf(env_id=env.id, task_type=task_type, adapter=env.agent.adapter)
        by_model = {r["model"]: r for r in rows}
        scored = []
        for model in models:
            rec = by_model.get(model, {"n": 0, "wins": 0, "fails": 0, "avg_ms": 0.0})
            sample = self._sample(rec, env.prior)
            score = sample * self._cost_factor(rec.get("avg_ms", 0.0), rec.get("avg_tokens"))
            scored.append({"id": model, "score": score, "sample": sample, "rec": rec})
        scored.sort(key=lambda r: r["score"], reverse=True)

        strategy = self.config.strategy
        if strategy == "epsilon" and self.rng.random() < self.config.epsilon:
            pick = self.rng.choice(scored)
            strategy_used = "epsilon-explore"
        else:
            pick = scored[0]
            strategy_used = strategy if strategy != "epsilon" else "epsilon-greedy"

        return Decision(
            stage="agent",
            strategy=strategy_used,
            chosen=[pick["id"]],
            candidates=[
                Candidate(
                    id=row["id"],
                    score=round(row["score"], 4),
                    sample=round(row["sample"], 4) if row.get("sample") is not None else None,
                    success=int(row["rec"].get("wins", 0)),
                    fail=int(row["rec"].get("fails", 0)),
                    detail=f"n={row['rec'].get('n', 0)}",
                )
                for row in scored
            ],
            reason=f"env {env.id!r} 在任务类型 {task_type!r} 下按 {strategy_used} 选模型",
        )

    # ---------------------------------------------------------------- 打分

    def _rank(self, envs: list[EnvManifest], task_type: str) -> list[dict[str, Any]]:
        rows = {r["env_id"]: r for r in self.ledger.perf(task_type=task_type, by_env=True)}
        scored = []
        for env in envs:
            rec = rows.get(env.id, {"n": 0, "wins": 0, "fails": 0, "avg_ms": 0.0})
            sample = self._sample(rec, env.prior)
            score = sample * self._cost_factor(rec.get("avg_ms", 0.0), rec.get("avg_tokens"))
            scored.append({"env": env, "score": score, "sample": sample, "rec": rec})

        if self.config.strategy == "static":
            scored.sort(key=lambda r: r["env"].prior, reverse=True)
            for row in scored:
                row["score"] = row["env"].prior
            return scored

        scored.sort(key=lambda r: r["score"], reverse=True)

        if self.config.strategy == "epsilon" and self.rng.random() < self.config.epsilon:
            rest = scored[1:]
            if rest:
                swap = self.rng.choice(rest)
                scored = [swap] + [r for r in scored if r is not swap]
                for row in scored:
                    row["explored"] = row is swap
        return scored

    def _sample(self, rec: dict[str, Any], prior: float) -> float:
        """Beta 后验采样。先验用"一次成功当量 × env.prior"表示，可解释。"""
        wins = float(rec.get("wins", 0))
        fails = float(rec.get("fails", 0))
        if self.config.strategy == "static":
            return prior / (prior + 1.0)
        alpha = 1.0 + max(prior, 0.0) + wins
        beta = 1.0 + fails
        return self.rng.betavariate(alpha, beta)

    def _cost_factor(self, avg_ms: float, avg_tokens: float | None = None) -> float:
        latency = self.config.latency_weight * max(avg_ms, 0.0) / self.config.latency_ref_ms
        tokens = self.config.token_weight * (avg_tokens or 0.0) / self.config.token_ref
        return 1.0 / (1.0 + latency + tokens)


def _candidate(row: dict[str, Any]) -> Candidate:
    rec = row["rec"]
    detail = f"n={rec.get('n', 0)}"
    if row.get("explored"):
        detail += " · 探索"
    return Candidate(
        id=row["env"].id,
        score=round(row["score"], 4),
        sample=round(row["sample"], 4) if row.get("sample") is not None else None,
        success=int(rec.get("wins", 0)),
        fail=int(rec.get("fails", 0)),
        detail=detail,
    )
