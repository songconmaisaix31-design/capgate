"""多页抓取分析：流式 vs 整块 同负载一致性（issue #6）。

覆盖真实多页负载的机制回归：整块与流式处理同一组 URL 产出相同快照，
流式下游在快照前消费，运行模式正确记录，截断标记为假。真实 HTTP 与
6 对交替耗时在 ``tools/stream_throughput.py`` 里验收，这里只验机制。
"""

from __future__ import annotations

from pathlib import Path

from capgate import demo

ROOT = Path(__file__).resolve().parents[1]


def _urls() -> list[str]:
    # 用仓库内固定 fixture 构造四页（file:// 即可覆盖机制，真实 HTTP 由基准工具覆盖）。
    uri = (ROOT / "fixtures" / "sample_site.html").resolve().as_uri()
    return [uri, uri, uri, uri]


def _load(system, result, name):
    artifact = next(a for a in result.artifacts if a.name == name)
    return system.store.load(artifact), artifact


async def test_pages_stream_matches_block_result(system):
    urls = _urls()
    block, stream = await demo.run_pages_comparison(system, urls, urls)
    assert block.status == "ok", block.error
    assert stream.status == "ok", stream.error

    _, block_pages = _load(system, block, "crawl_pages")
    _, stream_pages = _load(system, stream, "crawl_pages")
    block_report, _ = _load(system, block, "analysis_report")
    stream_report, _ = _load(system, stream, "analysis_report")

    # 同输入同输出：快照 SHA 一致、汇总一致、截断为假。
    assert block_pages.sha256 == stream_pages.sha256, "整块与流式最终快照应字节一致"
    assert stream_pages.truncated is False, "正常流式不应截断"
    assert block_report == stream_report, "两种模式汇总报告应一致"


async def test_pages_stream_consumer_reads_before_seal(system):
    urls = _urls()
    stream = await system.orchestrator.run(demo.build_pages_task(urls, urls, stream=True))
    chain = system.ledger.chain(stream.task.id)
    collects = [e for e in chain["events"] if e["kind"] == "agent.stream_collect"]
    assert collects, "流式 consumer 应有 stream_collect 事件"
    assert any(e["payload"].get("before_seal") for e in collects), "下游应在快照前消费"


async def test_pages_stream_records_stream_mode(system):
    urls = _urls()
    block, stream = await demo.run_pages_comparison(system, urls, urls)
    chain_b = system.ledger.chain(block.task.id)
    chain_s = system.ledger.chain(stream.task.id)
    modes_b = {e["payload"].get("mode") for e in chain_b["events"] if e["kind"] == "env.finish"}
    modes_s = {e["payload"].get("mode") for e in chain_s["events"] if e["kind"] == "env.finish"}
    assert modes_b == {"block"}
    assert modes_s == {"stream"}
