"""双层路由。

第一层：网关 → 领域 env（按 tag 召回，按账本表现打分）
第二层：env → agent/model（域内自己的性能画像，探索也本地化）

为什么用 Thompson sampling 而不是 epsilon-greedy：它自带探索——
样本少的候选方差大，自然会被抽中去试。这正好治"第一个还行的组合被锁死"
那个病（"GPT 干文档还行 → 文档永远给 GPT"）。

LLM 只做兜底裁决，且在这里是可插拔的：默认关闭，开启后只在
"分数接近 / 低置信 / 新任务类型" 时调用，且必须留痕。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from .envs import EnvRegistry
from .ledger import Ledger
from .models import Candidate, Decision, EnvManifest, TaskSpec

LLMJudge = Callable[[TaskSpec, list[dict[str, Any]]], str]


@dataclass
class RouterConfig:
    strategy: str = "thompson"  # thompson | epsilon | static
    epsilon: float = 0.1
    latency_ref_ms: float = 4000.0  # 成本惩罚的参照值
    latency_weight: float = 0.25
    llm_fallback: bool = False
    llm_margin: float = 0.12  # 头名与次名差距小于它就问 LLM
    pins: dict[str, dict[str, str]] = field(default_factory=dict)


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
        self.rng = rng or random.Random()

    # ---------------------------------------------------------------- 第一层

    def route_domains(self, task: TaskSpec) -> Decision:
        task_type = task.task_type
        pinned = self.config.pins.get(task_type, {}).get("env")
        if pinned:
            env = self.registry.get(pinned)
            return Decision(
                stage="domain",
                strategy="pin",
                chosen=[env.id],
                candidates=[Candidate(id=env.id, score=1.0, detail="人工 pin")],
                reason=f"任务类型 {task_type!r} 被 pin 到 env {env.id!r}",
            )

        if task.hint_envs:
            envs = [self.registry.get(e) for e in task.hint_envs]
            return Decision(
                stage="domain",
                strategy="hint",
                chosen=[e.id for e in envs],
                candidates=[Candidate(id=e.id, score=1.0, detail="调用方指定") for e in envs],
                reason="调用方通过 hint_envs 指定了领域",
            )

        matched = self.registry.by_tag(task.tags)
        reason_prefix = ""
        if not matched:
            matched = self.registry.all()
            reason_prefix = "没有 tag 命中的 env，回退到全量候选（按先验排序）"

        scored = self._rank(matched, task_type)
        if not scored:
            return Decision(
                stage="domain",
                strategy="none",
                chosen=[],
                reason="能力池里没有任何 env",
            )

        chosen = [scored[0]["env"].id]
        strategy = self.config.strategy
        reason = reason_prefix or f"tag 命中 {len(matched)} 个 env，按 {strategy} 打分"

        if self.config.llm_fallback and len(scored) > 1 and self.judge:
            margin = scored[0]["score"] - scored[1]["score"]
            if margin < self.config.llm_margin:
                picked = self._ask_llm(task, scored)
                if picked:
                    chosen = [picked]
                    strategy = "llm-fallback"
                    reason = f"头名分差 {margin:.3f} < {self.config.llm_margin}，转 LLM 裁决"

        return Decision(
            stage="domain",
            strategy=strategy,
            chosen=chosen,
            candidates=[_candidate(row) for row in scored],
            reason=reason,
        )

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
            score = sample * self._cost_factor(rec.get("avg_ms", 0.0))
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
        rows = {r["env_id"]: r for r in self.ledger.perf(task_type=task_type)}
        scored = []
        for env in envs:
            rec = rows.get(env.id, {"n": 0, "wins": 0, "fails": 0, "avg_ms": 0.0})
            sample = self._sample(rec, env.prior)
            score = sample * self._cost_factor(rec.get("avg_ms", 0.0))
            scored.append({"env": env, "score": score, "sample": sample, "rec": rec})

        if self.config.strategy == "static":
            scored.sort(key=lambda r: r["env"].prior, reverse=True)
            for row in scored:
                row["score"] = row["env"].prior
            return scored

        scored.sort(key=lambda r: r["score"], reverse=True)

        if self.config.strategy == "epsilon" and self.rng.random() < self.config.epsilon:
            head, rest = scored[0], scored[1:]
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

    def _cost_factor(self, avg_ms: float) -> float:
        if avg_ms <= 0:
            return 1.0
        return 1.0 / (1.0 + self.config.latency_weight * (avg_ms / self.config.latency_ref_ms))

    # ---------------------------------------------------------------- LLM 兜底

    def _ask_llm(self, task: TaskSpec, scored: list[dict[str, Any]]) -> str | None:
        payload = [
            {
                "env": row["env"].id,
                "domain": row["env"].domain,
                "tags": row["env"].tags,
                "score": round(row["score"], 4),
                "n": row["rec"].get("n", 0),
            }
            for row in scored
        ]
        try:
            picked = self.judge(task, payload)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - 兜底失败就退回算法结果
            return None
        return picked or None


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
