"""演示场景：不依赖外网，跑通"多 env + 契约交接 + 账本"。

三个场景刻意覆盖不同的编排形状：
    pipeline  单链接力：采集 → 汇总
    branch    并行分叉：采集 ∥ 链上，再汇总（汇总只吃采集的产物）
    doc       单 env：知识问答（RAG 独立成一个环境）
"""

from __future__ import annotations

from pathlib import Path

from .models import TaskResult, TaskSpec
from .system import System

SCENARIOS: dict[str, dict] = {
    "pipeline": {
        "goal": "抓取示例站点并汇总其中的链接",
        "tags": ["爬虫", "汇总"],
        "extra": {},
        "hint": "单链接力：crawler → analyst",
    },
    "branch": {
        "goal": "抓取示例站点、汇总链接，同时查一个地址的余额",
        "tags": ["爬虫", "链上", "汇总"],
        "extra": {"address": "0x8f3a2b7c9d1e4f5a6b0c2d3e4f5a6b7c8d9e0f1a"},
        "hint": "并行分叉：crawler ∥ chain-watch → analyst",
    },
    "doc": {
        "goal": "能力网关里 env 和 MCP 是什么关系？",
        "tags": ["文档", "问答"],
        "extra": {},
        "hint": "单 env：doc-qa（RAG 能力）",
    },
}


def fixture_url(root: Path) -> str:
    return (root / "fixtures" / "sample_site.html").resolve().as_uri()


def build_task(name: str, root: Path, goal: str | None = None) -> TaskSpec:
    if name not in SCENARIOS:
        raise KeyError(f"未知场景 {name!r}（可用：{sorted(SCENARIOS)}）")
    spec = SCENARIOS[name]
    inputs = {"url": fixture_url(root), "question": spec["goal"], **spec["extra"]}
    return TaskSpec(
        goal=goal or spec["goal"],
        tags=list(spec["tags"]),
        inputs=inputs,
        budget_s=120,
    )


async def run_demo(system: System, name: str, goal: str | None = None) -> TaskResult:
    task = build_task(name, system.paths.root, goal=goal)
    return await system.orchestrator.run(task)
