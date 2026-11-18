"""产物库的 GC、流式通道、原子写入与多进程并发假设。

对应 issue #5（TTL 与引用计数）、#6（流式产物）、#7（多写者）。单测不碰网络、
不启子进程：并发用线程 + 两个独立 Ledger 实例逼近"CLI 与 serve 同时跑"。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from capgate import demo
from capgate.artifacts import ArtifactStore, StreamClosed
from capgate.ledger import Ledger
from capgate.models import TaskSpec

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "art", ROOT / "schemas")


# ---------------------------------------------------------------- #5 GC


async def test_gc_dry_run_then_execute_keeps_ledger_record(system):
    result = await demo.run_demo(system, "pipeline")
    assert result.status == "ok", result.error
    future = datetime.now(UTC) + timedelta(days=40)

    dry = system.gc(ttl_days=30, dry_run=True, now=future)
    assert dry.expired == 2, "crawl_batch + crawl_summary 都过期且未被引用"
    assert dry.deleted == 0, "dry-run 不该真删"
    for art in result.artifacts:
        assert (system.store.root / art.path).exists(), "dry-run 之后文件仍在"

    real = system.gc(ttl_days=30, dry_run=False, now=future)
    assert real.deleted == 2
    assert real.reclaimed_bytes > 0
    for art in result.artifacts:
        assert not (system.store.root / art.path).exists(), "执行后文件已删"

    # 账本记录仍在：chain 依然能列出产物，历史路径允许 404
    chain = system.ledger.chain(result.task.id)
    assert len(chain["artifacts"]) == 2
    kinds = [e["kind"] for e in chain["events"]]
    assert kinds.count("artifact.gc") == 2, "删除动作记入 artifact.gc 事件，可审计"


def test_unfinished_task_artifacts_are_protected(tmp_path):
    led = Ledger(tmp_path / "l.db")
    store = ArtifactStore(tmp_path / "art")
    task = TaskSpec(goal="g", tags=["t"])
    led.add_task(task)  # status=running，未完成
    art = store.put(task.id, "free_form", {"x": 1})
    led.add_artifact(task.id, art)

    future = datetime.now(UTC) + timedelta(days=40)
    candidates, protected, _ = led.gc_candidates(ttl_days=30, now=future)
    assert candidates == [], "未完成任务（running）的产物不能删"
    assert protected == 1
    led.close()


def test_reference_count_protects_consumed_artifact(tmp_path):
    led = Ledger(tmp_path / "l.db")
    store = ArtifactStore(tmp_path / "art")

    # 任务 A 已结束，产出 X
    task_a = TaskSpec(goal="g", tags=["t"])
    led.add_task(task_a)
    art = store.put(task_a.id, "free_form", {"x": 1})
    led.add_artifact(task_a.id, art)
    led.finish_task(task_a.id, "ok")

    # 任务 B 仍在运行，消费了 X
    task_b = TaskSpec(goal="g2", tags=["t2"])
    led.add_task(task_b)
    led.add_artifact_ref(task_b.id, art.sha256, "consumer", "run_b")

    future = datetime.now(UTC) + timedelta(days=40)
    candidates, protected, _ = led.gc_candidates(ttl_days=30, now=future)
    assert candidates == [], "被未完成任务 consume 的产物即使超 TTL 也保留"
    assert protected == 1
    assert led.artifact_ref_count(art.sha256) == 1

    # 任务 B 结束 → 引用不再保护，X 变回可清理
    led.finish_task(task_b.id, "ok")
    candidates, protected, _ = led.gc_candidates(ttl_days=30, now=future)
    assert len(candidates) == 1 and protected == 0
    assert store.delete(candidates[0].path) is True
    assert not (store.root / art.path).exists()
    led.close()


def test_gc_ignores_unexpired_artifacts(tmp_path):
    led = Ledger(tmp_path / "l.db")
    store = ArtifactStore(tmp_path / "art")
    task = TaskSpec(goal="g", tags=["t"])
    led.add_task(task)
    led.finish_task(task.id, "ok")
    art = store.put(task.id, "free_form", {"x": 1})
    led.add_artifact(task.id, art)

    candidates, protected, _ = led.gc_candidates(ttl_days=30)  # now = 真实当前时间
    assert candidates == [] and protected == 0, "刚写入的产物不该过期"
    led.close()


def test_reserve_task_is_idempotent(tmp_path):
    led = Ledger(tmp_path / "l.db")
    task = TaskSpec(goal="g", tags=["t"])
    assert led.reserve_task(task) is True, "首次保留成功"
    assert led.reserve_task(task) is False, "同一 id 的重复请求不重复入账"
    with sqlite3.connect(tmp_path / "l.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    led.close()


# ---------------------------------------------------------------- #6 流式


def test_stream_seal_merges_dict_chunks(store):
    writer = store.open_stream("task_1", "free_form")
    writer.write_chunk({"url": "u", "title": "t"})
    writer.write_chunk({"links": ["a"]})
    writer.write_chunk({"text": "x"})
    art = writer.seal()

    assert art.stream is True and art.truncated is False
    # 最终快照 = dict chunk 浅合并，可重放
    assert store.load(art) == {"url": "u", "title": "t", "links": ["a"], "text": "x"}


def test_stream_sha256_is_final_snapshot_hash(store):
    a = store.open_stream("task_1", "free_form")
    a.write_chunk({"p": 1})
    a.write_chunk({"q": 2})
    art_a = a.seal()

    b = store.open_stream("task_2", "free_form")
    b.write_chunk({"p": 1})
    b.write_chunk({"q": 2})
    art_b = b.seal()

    assert art_a.sha256 == art_b.sha256, "相同最终快照 → 相同 sha"
    assert art_a.path != art_b.path, "目录布局按 task_id 分，不按 sha256 分"


def test_stream_truncated_is_explicit_and_persisted(store):
    writer = store.open_stream("task_1", "free_form")
    writer.write_chunk({"p": 1})
    writer.mark_truncated()
    art = writer.seal()

    assert art.truncated is True, "断流要打明确标记，而不是静默半截"
    assert store.load(art) == {"p": 1}, "半截数据也落盘成最终快照，可重放"


def test_stream_cannot_write_after_seal(store):
    writer = store.open_stream("task_1", "free_form")
    writer.write_chunk(1)
    writer.seal()
    with pytest.raises(StreamClosed):
        writer.write_chunk(2)


def test_reader_consumes_chunk_before_seal(store):
    writer = store.open_stream("task_1", "free_form")
    reader = store.read_stream(writer)
    writer.write_chunk({"url": "u"})
    assert reader.read_available() == [{"url": "u"}]
    assert reader.first_chunk_read_at is not None
    assert writer.sealed is False, "还没 seal，下游已经读到第一个 chunk"
    writer.write_chunk({"text": "x"})
    writer.seal()
    assert reader.first_chunk_before_seal is True


async def test_stream_demo_matches_block_result(system):
    block, stream = await demo.run_stream_comparison(system)
    assert block.status == "ok", block.error
    assert stream.status == "ok", stream.error

    def load(result, name):
        return system.store.load(next(a for a in result.artifacts if a.name == name))

    # 同输入，流式与整块产出同样的 crawl_summary
    assert load(stream, "crawl_summary") == load(block, "crawl_summary")

    # crawl_batch 语义一致；fetched_at 是墙钟时间戳，两次运行必然不同，排除掉
    stream_batch = load(stream, "crawl_batch")
    block_batch = load(block, "crawl_batch")
    stream_batch.pop("fetched_at", None)
    block_batch.pop("fetched_at", None)
    assert stream_batch == block_batch


async def test_stream_demo_consumer_reads_before_seal(system):
    stream = await demo.run_demo(system, "stream")
    assert stream.status == "ok", stream.error
    chain = system.ledger.chain(stream.task.id)
    collects = [e for e in chain["events"] if e["kind"] == "agent.stream_collect"]
    assert collects, "consumer 应有 stream_collect 事件"
    assert any(e["payload"].get("before_seal") for e in collects), (
        "下游要在上游写出最终快照前消费到第一个 chunk"
    )


async def test_stream_demo_records_stream_mode(system):
    stream = await demo.run_demo(system, "stream")
    assert stream.status == "ok", stream.error
    chain = system.ledger.chain(stream.task.id)
    finishes = [e for e in chain["events"] if e["kind"] == "env.finish"]
    assert finishes and all(e["payload"]["mode"] == "stream" for e in finishes)


async def test_stream_consumer_artifact_is_not_marked_streamed(system):
    stream = await demo.run_demo(system, "stream")
    assert stream.status == "ok", stream.error
    batch = next(a for a in stream.artifacts if a.name == "crawl_batch")
    summary = next(a for a in stream.artifacts if a.name == "crawl_summary")
    assert batch.stream is True, "producer 经 seal 的产物要标 stream"
    assert batch.producer_run, "流式产物要回填 producer_run"
    assert summary.stream is False, "consumer 用 emit 的普通整块产物不标 stream"
    # 运行模式与产物模式分开：consumer 的 run 模式是 stream，产物却不是流式
    chain = system.ledger.chain(stream.task.id)
    finishes = {e["env_id"]: e for e in chain["events"] if e["kind"] == "env.finish"}
    assert finishes["analyst-stream"]["payload"]["mode"] == "stream"


async def test_stream_producer_failure_marks_truncated(system):
    from capgate.models import AgentSpec, ArtifactContract, EnvManifest

    producer = EnvManifest(
        id="sp",
        domain="流式",
        tags=["流式"],
        stream=True,
        artifacts=ArtifactContract(produces=["report"]),
        agent=AgentSpec(
            recipe=[
                {"stream_emit": {"name": "report", "body": {"part": 1}}},
                # 模板变量缺失 → render 抛 AgentError，producer 在第一条 chunk 之后断流
                {
                    "capability": "sdk-text",
                    "tool": "summarize_links",
                    "args": {"links": "{{ input.missing }}"},
                },
            ]
        ),
    )
    consumer = EnvManifest(
        id="sc",
        domain="流式",
        tags=["流式"],
        stream=True,
        artifacts=ArtifactContract(consumes=["report"], produces=["summary"]),
        agent=AgentSpec(
            recipe=[
                # 等到 extra 出现或 seal：extra 永远不来，consumer 在 seal 时拿到 truncated
                {"stream_collect": {"name": "report", "until": ["part", "extra"]}, "as": "rep"},
                {"emit": {"name": "summary", "body": {"got": "{{ rep.part }}"}}},
            ]
        ),
    )
    system.registry.envs = {"sp": producer, "sc": consumer}
    result = await system.orchestrator.run(
        TaskSpec(goal="断流", tags=["流式"], inputs={"text": "x"})
    )

    # producer 失败，但断流状态在持久化与下游都可观察
    report = next(a for a in result.artifacts if a.name == "report")
    assert report.truncated is True, "截断快照要落盘且带 truncated 标记"
    assert report.producer_run, "截断快照也要回填 producer_run"
    chain = system.ledger.chain(result.task.id)
    collects = [e for e in chain["events"] if e["kind"] == "agent.stream_collect"]
    assert any(e["payload"].get("truncated") for e in collects), "下游要观察到断流，而非挂死"
    # consumer 收到 truncated 却缺 extra，诊断必须点出 truncated
    consumer_run = next(r for r in chain["runs"] if r["env_id"] == "sc")
    assert "truncated" in (consumer_run["error"] or ""), "缺 key 时错误要明确出现 truncated"


# ---------------------------------------------------------------- #7 原子写与并发


def test_put_is_atomic_no_temp_leftover(store):
    art = store.put("task_1", "free_form", {"ok": True})
    files = list((store.root / "task_1").iterdir())
    assert len(files) == 1
    assert not any(f.name.startswith(".") for f in files), "不能留下半截临时文件"
    assert store.load(art) == {"ok": True}


def test_put_uses_full_sha256_and_repeated_write_is_safe(store):
    art = store.put("task_1", "free_form", {"x": 1})
    assert len(art.sha256) == 64
    assert art.path.endswith(f"-{art.sha256}.json"), "文件名用完整 SHA-256，不是八位摘要"

    # 重复写同一内容：内容寻址去重，硬链接安装不报错，仍只有一份文件
    art2 = store.put("task_1", "free_form", {"x": 1})
    assert art2.sha256 == art.sha256 and art2.path == art.path
    files = list((store.root / "task_1").iterdir())
    assert len(files) == 1
    assert not any(f.name.startswith(".") for f in files)
    assert store.load(art2) == {"x": 1}


def test_concurrent_put_same_content_stays_valid(store):
    payload = {"ok": True, "v": [1, 2, 3]}

    def put_many() -> None:
        for _ in range(50):
            store.put("task_1", "free_form", payload)

    threads = [threading.Thread(target=put_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    files = list((store.root / "task_1").iterdir())
    assert len(files) == 1, "相同内容 → 相同完整 sha256 → 同一文件，不该堆积"
    assert json.loads(files[0].read_text(encoding="utf-8")) == payload
    assert not any(f.name.startswith(".") for f in files)


def test_concurrent_ledger_writes_lose_nothing(tmp_path):
    """两个独立 Ledger 实例（模拟两个进程）并发写：不丢事件、不报 database is locked。"""
    db = tmp_path / "l.db"
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            led = Ledger(db)
            for i in range(20):
                task = TaskSpec(goal=f"g-{n}-{i}", tags=["t"])
                led.add_task(task)
                led.event(task.id, "task.start", {"n": n, "i": i})
            led.close()
        except Exception as exc:  # pragma: no cover - 只想收集，不中断
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"并发写账本不该报错：{errors}"
    with sqlite3.connect(db) as conn:
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert tasks == 80, "4 个写者 × 20 个任务，一个都不能少"
    assert events == 80, "每个任务一条事件，一个都不能丢"


def test_file_lock_is_mutually_exclusive(tmp_path):
    from capgate.db import FileLock, concurrent_writer_detected, lock_path_for

    path = lock_path_for(tmp_path / "l.db")
    held = FileLock(path, timeout=10.0)
    held.acquire()
    try:
        probe = FileLock(path, timeout=0.0)
        with pytest.raises(TimeoutError):
            probe.acquire()
        assert concurrent_writer_detected(tmp_path / "l.db") is True, "doctor 要能看出持锁者"
    finally:
        held.release()
    assert concurrent_writer_detected(tmp_path / "l.db") is False
