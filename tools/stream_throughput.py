"""多页抓取分析的流式 vs 整块同负载基准（issue #6 验收）。

用户可执行命令：``python -m tools.stream_throughput``（在本机 Python 环境，
MCP1 用 ``../capgate/.venv``，MCP2 用 ``../capgate/.test-state/mcp-v2``，
运行时把 PYTHONPATH 指向本 worktree）。

它做四件事：
1. 下载一组固定公开技术文档（SQLite 官方文档）到 ``.test-state/corpus/``，只下载一次；
2. 用真实本地 HTTP 服务把它们提供出来（可重复执行，不伪造服务/数据）；
3. 同一组 URL、交替顺序跑 6 对整块/流式任务，记录全任务中位耗时；
4. 校验汇总一致、快照 SHA、截断与审计，打印原始结果。

两种模式处理完全相同的文档、依赖与输出，唯一差别是下游能否在来源收齐前开始。
"""

from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import statistics
import threading
from pathlib import Path

import httpx

from capgate import demo
from capgate.router import RouterConfig
from capgate.system import build

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".test-state"
CORPUS = STATE / "corpus"

# 固定公开文档（SQLite 官方技术文档，静态 HTML、互相交叉引用）。文件名即本地服务路径。
# 选较大的页面，让真实抓取（HTML 全量经 MCP stdio 序列化）有足够的数据传输工作量。
DOCS = [
    ("pragma.html", "https://www.sqlite.org/pragma.html"),
    ("fileformat2.html", "https://www.sqlite.org/fileformat2.html"),
    ("atomiccommit.html", "https://www.sqlite.org/atomiccommit.html"),
    ("howtocorrupt.html", "https://www.sqlite.org/howtocorrupt.html"),
]


def clear_proxy_env() -> None:
    # 本机 NO_PROXY 含 [::1]，httpx 解析代理环境会抛 InvalidURL；多页负载只走 127.0.0.1
    # 本地服务、不需要代理，因此清掉代理变量，让 mcp-fetch 子进程干净启动。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        os.environ.pop(key, None)


def ensure_corpus() -> list[tuple[str, str]]:
    """下载缺失的语料，返回 (文件名, 原始 URL) 列表。只下载一次，之后复用缓存。"""
    CORPUS.mkdir(parents=True, exist_ok=True)
    for filename, url in DOCS:
        dest = CORPUS / filename
        if dest.exists():
            continue
        resp = httpx.get(url, timeout=30, follow_redirects=True, trust_env=False)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return list(DOCS)


def start_http_server() -> tuple[threading.Thread, int]:
    """在 127.0.0.1 起一个真实 HTTP 服务，把语料目录提供出来。返回 (线程, 端口)。"""

    class Handler(http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # 允许 keep-alive，避免每请求重新握手

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(CORPUS), **kwargs)

        def log_message(self, *_args):  # 静默访问日志，避免刷屏
            pass

    class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    server = ThreadingServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread, server.server_address[1]


def local_urls(port: int) -> list[str]:
    return [f"http://127.0.0.1:{port}/{filename}" for filename, _ in DOCS]


def _load(system, result, name):
    artifact = next(a for a in result.artifacts if a.name == name)
    return system.store.load(artifact), artifact


async def run_series(system, urls, references, pairs: int = 6):
    """交替顺序跑 pairs 对整块/流式任务，返回 (block_ms, stream_ms, results)。"""
    block_ms: list[int] = []
    stream_ms: list[int] = []
    block_results = []
    stream_results = []
    for i in range(pairs):
        if i % 2 == 0:
            b = await system.orchestrator.run(
                demo.build_pages_task(urls, references, stream=False)
            )
            s = await system.orchestrator.run(
                demo.build_pages_task(urls, references, stream=True)
            )
        else:
            s = await system.orchestrator.run(
                demo.build_pages_task(urls, references, stream=True)
            )
            b = await system.orchestrator.run(
                demo.build_pages_task(urls, references, stream=False)
            )
        block_ms.append(b.latency_ms)
        stream_ms.append(s.latency_ms)
        block_results.append(b)
        stream_results.append(s)
    return block_ms, stream_ms, block_results, stream_results


def tool_calls(system, result) -> int:
    chain = system.ledger.chain(result.task.id)
    return sum(1 for e in chain["events"] if e["kind"] == "agent.tool")


def stream_before_seal(system, result) -> bool:
    chain = system.ledger.chain(result.task.id)
    collects = [e for e in chain["events"] if e["kind"] == "agent.stream_collect"]
    return any(e["payload"].get("before_seal") for e in collects)


def modes(system, result) -> set[str]:
    chain = system.ledger.chain(result.task.id)
    return {e["payload"].get("mode") for e in chain["events"] if e["kind"] == "env.finish"}


async def main() -> None:
    parser = argparse.ArgumentParser(description="多页抓取分析 流式 vs 整块 同负载基准")
    parser.add_argument("--pairs", type=int, default=6, help="对比对数（默认 6）")
    args = parser.parse_args()

    clear_proxy_env()
    docs = ensure_corpus()
    _thread, port = start_http_server()
    urls = local_urls(port)
    references = list(urls)  # 分析侧校验交叉引用文档可用性：同一组公开文档

    system = build(ROOT, RouterConfig(strategy="static"), state=STATE / "bench")
    try:
        # 同等预热：两种模式先各跑一次，让 MCP 子进程与 warm 实例就绪。
        await demo.run_pages_comparison(system, urls, references)

        block_ms, stream_ms, block_results, stream_results = await run_series(
            system, urls, references, pairs=args.pairs
        )

        bmed = statistics.median(block_ms)
        smed = statistics.median(stream_ms)

        # 工作次数一致性：两种模式每个任务真实工具调用数应相同。
        block_work = {tool_calls(system, r) for r in block_results}
        stream_work = {tool_calls(system, r) for r in stream_results}

        # 校验：汇总一致、快照 SHA、截断、审计（跨最后一对，两模式各取一次）。
        b = block_results[-1]
        s = stream_results[-1]
        _, b_pages_art = _load(system, b, "crawl_pages")
        _, s_pages_art = _load(system, s, "crawl_pages")
        b_report, _ = _load(system, b, "analysis_report")
        s_report, _ = _load(system, s, "analysis_report")
        b_before_seal = stream_before_seal(system, s)
        b_modes = modes(system, b)
        s_modes = modes(system, s)

        sha_match = b_pages_art.sha256 == s_pages_art.sha256
        report_match = b_report == s_report
        truncated = s_pages_art.truncated
        ok = (
            report_match
            and sha_match
            and not truncated
            and b_before_seal
            and b_modes == {"block"}
            and s_modes == {"stream"}
            and block_work == stream_work
            and len(block_work) == 1
        )
    finally:
        await system.aclose()

    print("=== 多页抓取分析：流式 vs 整块（同负载） ===")
    print("语料（真实公开文档，经本地 HTTP 服务提供）：")
    for filename, url in docs:
        size = (CORPUS / filename).stat().st_size
        print(f"  {filename}  <-  {url}  ({size} bytes)")
    print(f"pairs={len(block_ms)} 交替顺序（偶数对先整块后流式，奇数对反之）")
    print(f"整块 中位={bmed:.1f}ms  原始={block_ms}")
    print(f"流式 中位={smed:.1f}ms  原始={stream_ms}")
    print(f"加速比（整块中位 / 流式中位）={bmed / smed:.3f}")

    print("=== 校验 ===")
    print(f"任务哈希：整块={b.task.id} 流式={s.task.id}")
    print(
        f"工作次数（每任务 agent.tool 调用数）：整块={sorted(block_work)} "
        f"流式={sorted(stream_work)}"
    )
    print(f"汇总一致（analysis_report 相同）：{report_match}")
    print(
        f"快照 SHA 一致（crawl_pages）：整块={b_pages_art.sha256[:16]}… "
        f"流式={s_pages_art.sha256[:16]}… 相同={sha_match}"
    )
    print(f"流式截断标记（应 False）：{truncated}")
    print(f"流式下游在快照前消费（before_seal，应 True）：{b_before_seal}")
    print(f"运行模式（整块应 {{'block'}}）：{sorted(b_modes)}")
    print(f"运行模式（流式应 {{'stream'}}）：{sorted(s_modes)}")
    print(f"验收：{'PASS' if ok else 'FAIL'}")
    print(f"流式中位低于整块中位：{smed < bmed}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
