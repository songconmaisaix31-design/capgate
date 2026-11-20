"""演示场景：不依赖外网，跑通"多 env + 契约交接 + 账本"。

场景刻意覆盖不同的编排形状：
    pipeline  单链接力：采集 → 汇总
    branch    并行分叉：采集 ∥ 链上，再汇总（汇总只吃采集的产物）
    doc       单 env：知识问答（RAG 独立成一个环境）
    stream    流式接力：crawler-stream → analyst-stream（边爬边喂）
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
        "tags": ["爬虫", "演示链", "汇总"],
        "extra": {"address": "0x8f3a2b7c9d1e4f5a6b0c2d3e4f5a6b7c8d9e0f1a"},
        "hint": "并行分叉：crawler ∥ chain-watch → analyst",
    },
    "doc": {
        "goal": "能力网关里 env 和 MCP 是什么关系？",
        "tags": ["文档", "问答"],
        "extra": {},
        "hint": "单 env：doc-qa（RAG 能力）",
    },
    "stream": {
        "goal": "流式抓取示例站点并汇总其中的链接",
        "tags": ["流式"],
        "extra": {},
        "hint": "流式接力：crawler-stream → analyst-stream（边爬边喂）",
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


async def run_stream_comparison(system: System) -> tuple[TaskResult, TaskResult]:
    """同输入对比整块（crawler → analyst）与流式（crawler-stream → analyst-stream）。

    两者抓同一个 fixture、产出同样的 crawl_summary，可以对比结果一致性与耗时。
    """
    block = await run_demo(system, "pipeline")
    stream = await run_demo(system, "stream")
    return block, stream


# 多页抓取与分析（issue #6 真实负载）：两种模式处理完全相同的公开文档列表，
# 上游逐页 fetch_url，下游逐页统计链接/词频并校验交叉引用文档可用性。
# 整块与流式只有产物交接时机不同——流式每页一 chunk，下游可先于整块收齐开始分析。
_PAGES_GOAL = "抓取多份公开技术文档，逐页统计链接与词频并校验交叉引用文档可用性"
_PAGES_TAGS = ["爬虫", "汇总"]


def build_pages_task(
    urls: list[str],
    references: list[str],
    *,
    stream: bool = False,
    goal: str | None = None,
) -> TaskSpec:
    """构造多页抓取分析任务；hint_envs 明确指定整块或流式的上下游 env。"""
    envs = (
        ["crawler-pages-stream", "analyst-pages-stream"]
        if stream
        else ["crawler-pages", "analyst-pages"]
    )
    return TaskSpec(
        goal=goal or _PAGES_GOAL,
        tags=list(_PAGES_TAGS),
        inputs={"urls": list(urls), "references": list(references)},
        hint_envs=envs,
        budget_s=120,
    )


async def run_pages_comparison(
    system: System, urls: list[str], references: list[str]
) -> tuple[TaskResult, TaskResult]:
    """同输入对比多页整块（crawler-pages → analyst-pages）与流式（stream 版）。

    两者抓同一组 URL、产出同样的 crawl_pages 与 analysis_report，可对比一致性、SHA 与耗时。
    """
    block = await system.orchestrator.run(build_pages_task(urls, references, stream=False))
    stream = await system.orchestrator.run(build_pages_task(urls, references, stream=True))
    return block, stream
