from __future__ import annotations

import json
import os

import pytest

from capgate.models import TaskSpec
from capgate.router import Router, RouterConfig
from capgate.system import System

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("CAPGATE_LLM_API_KEY") or not os.environ.get("CAPGATE_LLM_BASE_URL"),
        reason="需要真实模型 API",
    ),
]


async def test_public_routing_ab_with_real_judge(system: System) -> None:
    baseline = Router(system.ledger, system.registry, RouterConfig(strategy="static"))
    judge = Router(
        system.ledger,
        system.registry,
        RouterConfig(
            strategy="static",
            llm_fallback=True,
            llm_model="deepseek-flash",
            llm_reasoning_effort="none",
            llm_timeout_s=20,
            llm_price_usd_per_million={"input": 0.3, "output": 1.2, "cached_input": 0.006},
        ),
    )
    cases = [
        (TaskSpec(goal="通过 SWA 读取当前注意力焦点"), "swa-attention", "没有标签命中"),
        (
            TaskSpec(goal="检查 FIT 的工具注册表和版本", tags=["环境", "焦点"]),
            "fit-env",
            "头名分差",
        ),
        (
            TaskSpec(goal="用仓库内规则型 RAG 文档问答环境检索引用段落"),
            "doc-qa",
            "没有标签命中",
        ),
    ]
    report = {
        "baseline_correct": 0,
        "judge_correct": 0,
        "baseline_tokens": 0,
        "judge_tokens": 0,
        "baseline_estimated_usd": 0.0,
        "judge_estimated_usd": 0.0,
    }
    for spec, expected, trigger in cases:
        task = system.orchestrator.prepare_task(spec)
        system.ledger.add_task(task)
        control = await baseline.route_domains_async(task)
        system.ledger.decide(task.id, control)
        result = await judge.route_domains_async(
            task,
            on_judge=lambda payload, task_id=task.id: system.ledger.event(
                task_id, "routing.llm_judge", payload
            ),
        )
        system.ledger.decide(task.id, result)
        events = [
            event for event in system.ledger.events(task.id) if event["kind"] == "routing.llm_judge"
        ]
        assert len(events) == 1
        event = events[0]["payload"]
        assert event["usage"]["requests"] == 1
        assert event["usage"]["total_tokens"] > 0
        assert event["usage"]["cost_usd"] > 0
        assert set(result.chosen) <= {candidate.id for candidate in control.candidates}
        if event["selected"] is not None:
            assert result.strategy == "llm-fallback" and result.chosen == [event["selected"]]
        else:
            assert result.chosen == control.chosen
        assert trigger in result.reason
        assert result.candidates[0].id == result.chosen[0]
        report["baseline_correct"] += control.chosen == [expected]
        report["judge_correct"] += result.chosen == [expected]
        report["judge_tokens"] += event["usage"]["total_tokens"]
        report["judge_estimated_usd"] += event["usage"]["cost_usd"]
    print(f"真实路由裁决对照，任务数 {len(cases)}，{json.dumps(report, ensure_ascii=False)}")


async def test_judge_input_budget_falls_back(system: System) -> None:
    router = Router(
        system.ledger,
        system.registry,
        RouterConfig(
            strategy="static",
            llm_fallback=True,
            llm_model="deepseek-flash",
            llm_max_request_bytes=1,
        ),
    )
    task = TaskSpec(goal="读取 FIT 工具注册表")
    system.ledger.add_task(task)
    result = await router.route_domains_async(
        task, on_judge=lambda payload: system.ledger.event(task.id, "routing.llm_judge", payload)
    )
    assert result.chosen == router.route_domains(task).chosen
    assert result.strategy == "static" and "请求超过 1 字节预算" in result.reason
    (record,) = system.ledger.events(task.id)
    assert record["payload"]["usage"] == {}
