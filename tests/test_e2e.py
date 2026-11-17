from __future__ import annotations

import pytest

from capgate import demo
from capgate.models import TaskSpec


async def test_pipeline_demo_end_to_end(system):
    result = await demo.run_demo(system, "pipeline")

    assert result.status == "ok", result.error
    names = {a.name for a in result.artifacts}
    assert names == {"crawl_batch", "crawl_summary"}

    batch = system.store.load(next(a for a in result.artifacts if a.name == "crawl_batch"))
    assert batch["url"].startswith("file://")
    assert len(batch["links"]) >= 4
    assert "能力" in batch["text"]

    summary = system.store.load(next(a for a in result.artifacts if a.name == "crawl_summary"))
    assert summary["total_links"] >= 4
    assert summary["source"] == batch["url"]

    chain = system.ledger.chain(result.task.id)
    stages = [d["stage"] for d in chain["decisions"]]
    assert stages[0] == "domain"
    assert stages[1] == "pipeline"
    assert stages.count("agent") == 2
    assert all("理由" not in d["reason"] for d in chain["decisions"])  # 理由必须是真的写了
    assert [r["env_id"] for r in chain["runs"]] == ["crawler", "analyst"]


async def test_branch_demo_runs_envs_in_parallel(system):
    result = await demo.run_demo(system, "branch")

    assert result.status == "ok", result.error
    names = {a.name for a in result.artifacts}
    assert names == {"crawl_batch", "chain_balance", "crawl_summary"}
    assert len(result.runs) == 3

    balance = system.store.load(next(a for a in result.artifacts if a.name == "chain_balance"))
    assert balance["address"].startswith("0x")
    assert "demo-deterministic" == balance["source"]


async def test_doc_demo_uses_rag_env(system):
    result = await demo.run_demo(system, "doc")

    assert result.status == "ok", result.error
    answer = system.store.load(next(a for a in result.artifacts if a.name == "doc_answer"))
    assert answer["hits"], "检索不该空手而归"
    assert answer["hits"][0]["doc"].endswith(".md")


async def test_missing_input_fails_honestly(system):
    task = TaskSpec(goal="查余额但没给地址", tags=["链上"], inputs={}, budget_s=60)
    result = await system.orchestrator.run(task)

    assert result.status == "failed"
    assert result.runs and result.runs[0].status == "failed"
    assert "address" in (result.runs[0].error or "")


async def test_human_label_feeds_back_into_routing(system):
    result = await demo.run_demo(system, "pipeline")
    system.ledger.outcome(result.task.id, success=False, human_edits=2, note="结果不能用")

    perf = system.ledger.perf(env_id="crawler")
    assert perf and perf[0]["wins"] == 0, "人工差评必须压过自动判定"
