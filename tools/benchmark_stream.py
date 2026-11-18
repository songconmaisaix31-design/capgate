from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import tempfile
from collections import Counter
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from statistics import median
from threading import Thread
from time import perf_counter
from urllib.parse import urlsplit
from zipfile import ZipFile

import yaml

from capgate.models import TaskSpec
from capgate.router import RouterConfig
from capgate.system import System, build

ARCHIVE_URL = "https://www.sqlite.org/2026/sqlite-doc-3510300.zip"
ARCHIVE_SHA256 = "ec053e66a33b0479b4b4c88133aeef4d02214d63e5a2d59ed062caf5061ea22c"
ARCHIVE_PREFIX = "sqlite-doc-3510300/"
WORKLOAD = (
    ("wal", "wal.html", "pragma.html"),
    ("foreignkeys", "foreignkeys.html", "lang_createtable.html"),
    ("queryplanner", "queryplanner.html", "optoverview.html"),
    ("json", "json1.html", "lang_aggfunc.html"),
    ("fts", "fts5.html", "fts3.html"),
)
MODES = ("block", "stream")
PAGES = "benchmark_pages"
SUMMARY = "benchmark_summary"
ROOT = Path(__file__).resolve().parent.parent


class ArchiveServer(ThreadingHTTPServer):
    def __init__(self, directory: Path) -> None:
        self.requests: list[tuple[str, str, int]] = []
        super().__init__(("127.0.0.1", 0), partial(ArchiveHandler, directory=str(directory)))


class ArchiveHandler(SimpleHTTPRequestHandler):
    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        self.server.requests.append((self.command, self.path, int(code)))


def extract_pages(archive: Path, directory: Path) -> dict[str, int]:
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("SQLite 档案 SHA-256 不符，请取得指定版本的官方发布文件")
    directory.mkdir()
    names = [name for _, source, reference in WORKLOAD for name in (source, reference)]
    sizes = {}
    with ZipFile(archive) as documents:
        for name in names:
            content = documents.read(ARCHIVE_PREFIX + name)
            (directory / name).write_bytes(content)
            sizes[name] = len(content)
    return sizes


def ref(path: str) -> str:
    return "{{ " + path + " }}"


def invoke(capability: str, tool: str, alias: str, **args: str | int) -> dict:
    return {"capability": capability, "tool": tool, "as": alias, "args": args}


def recipes(stream: bool) -> tuple[list[dict], list[dict]]:
    crawl = []
    analyze = []
    pages = {}
    summary = {}
    for key, source, reference in WORKLOAD:
        page = f"page_{key}"
        links = f"links_{key}"
        text = f"text_{key}"
        crawl.extend(
            [
                invoke("mcp-fetch", "fetch_url", page, url=ref(f"input.sources.{key}")),
                invoke("mcp-fetch", "extract_links", links, html=ref(f"{page}.html")),
                invoke("mcp-fetch", "extract_text", text, html=ref(f"{page}.html")),
            ]
        )
        pages[key] = {
            "file": source,
            "title": ref(f"{page}.title"),
            "links": ref(f"{links}.links"),
            "text": ref(f"{text}.text"),
        }
        if stream:
            crawl.append({"stream_emit": {"name": PAGES, "body": {key: pages[key]}}})
            analyze.append({"stream_collect": {"name": PAGES, "until": [key]}, "as": "batch"})
        batch = f"batch.{key}" if stream else f"artifacts.{PAGES}.{key}"
        linked = f"reference_{key}"
        linked_text = f"reference_text_{key}"
        counts = f"counts_{key}"
        terms = f"terms_{key}"
        analyze.extend(
            [
                invoke(
                    "mcp-fetch-reference", "fetch_url", linked, url=ref(f"input.references.{key}")
                ),
                invoke(
                    "mcp-fetch-reference", "extract_text", linked_text, html=ref(f"{linked}.html")
                ),
                invoke("sdk-text", "summarize_links", counts, links=ref(f"{batch}.links")),
                invoke("sdk-text", "word_freq", terms, text=ref(f"{linked_text}.text"), top=8),
            ]
        )
        summary[key] = {
            "source": ref(f"{batch}.file"),
            "source_title": ref(f"{batch}.title"),
            "source_links": ref(f"{counts}.total"),
            "reference": reference,
            "reference_title": ref(f"{linked}.title"),
            "reference_chars": ref(f"{linked_text}.chars"),
            "reference_terms": ref(f"{terms}.top"),
        }
    if not stream:
        crawl.append({"emit": {"name": PAGES, "body": pages}})
    analyze.append({"emit": {"name": SUMMARY, "body": summary}})
    return crawl, analyze


def environments(root: Path) -> None:
    for directory in ("envs", "capabilities", "schemas"):
        (root / directory).mkdir()
    fetch = yaml.safe_load((ROOT / "capabilities/mcp-fetch.yaml").read_text(encoding="utf-8"))
    for cap_id in ("mcp-fetch", "mcp-fetch-reference"):
        fetch["id"] = cap_id
        for transport in fetch["versions"]:
            transport["env"] = {**transport.get("env", {}), "NO_PROXY": "127.0.0.1"}
        (root / "capabilities" / f"{cap_id}.yaml").write_text(
            yaml.safe_dump(fetch), encoding="utf-8"
        )
    text = (ROOT / "capabilities/sdk-text.yaml").read_bytes()
    (root / "capabilities/sdk-text.yaml").write_bytes(text)

    for mode in MODES:
        crawl, analyze = recipes(mode == "stream")
        steps = (
            ("crawler", crawl, ["mcp-fetch"], [], PAGES),
            ("analyst", analyze, ["mcp-fetch-reference", "sdk-text"], [PAGES], SUMMARY),
        )
        for role, recipe, caps, consumes, produces in steps:
            env_id = f"benchmark-{role}-{mode}"
            manifest = {
                "id": env_id,
                "domain": role,
                "tags": ["benchmark"],
                "stream": mode == "stream",
                "capabilities": [
                    {"id": cap, "kind": "sdk" if cap == "sdk-text" else "mcp"} for cap in caps
                ],
                "agent": {"adapter": "scripted", "models": ["rule-v1"], "recipe": recipe},
                "artifacts": {"consumes": consumes, "produces": [produces]},
                "resources": {"max_parallel": 1},
            }
            (root / "envs" / f"{env_id}.yaml").write_text(
                yaml.safe_dump(manifest), encoding="utf-8"
            )

    fields = {
        PAGES: {"file": "string", "title": "string", "links": "array", "text": "string"},
        SUMMARY: {
            "source": "string",
            "source_title": "string",
            "source_links": "integer",
            "reference": "string",
            "reference_title": "string",
            "reference_chars": "integer",
            "reference_terms": "array",
        },
    }
    for name, contents in fields.items():
        entry = {
            "type": "object",
            "properties": {key: {"type": kind} for key, kind in contents.items()},
            "required": list(contents),
            "additionalProperties": False,
        }
        keys = [key for key, _, _ in WORKLOAD]
        schema = {
            "type": "object",
            "properties": dict.fromkeys(keys, entry),
            "required": keys,
            "additionalProperties": False,
        }
        (root / "schemas" / f"{name}.schema.json").write_text(json.dumps(schema), encoding="utf-8")


async def run_one(
    system: System, server: ArchiveServer, mode: str, expected: dict[str, bytes]
) -> dict:
    base = f"http://127.0.0.1:{server.server_port}/"
    crawler = f"benchmark-crawler-{mode}"
    analyst = f"benchmark-analyst-{mode}"
    task = TaskSpec(
        goal="检索指定 SQLite 文档，统计实际链接与引用文档关键词",
        tags=["benchmark"],
        hint_envs=[crawler, analyst],
        inputs={
            "sources": {key: base + source for key, source, _ in WORKLOAD},
            "references": {key: base + reference for key, _, reference in WORKLOAD},
        },
    )
    offset = len(server.requests)
    started = perf_counter()
    result = await system.orchestrator.run(task)
    elapsed_ms = (perf_counter() - started) * 1000
    if result.status != "ok" or any(run.status != "ok" for run in result.runs):
        errors = [f"{run.env_id}: {run.error}" for run in result.runs if run.error]
        raise RuntimeError(f"{mode} 任务失败: {result.error}; {errors}")

    chain = system.ledger.chain(task.id)
    events = chain["events"]
    request_counts = Counter(server.requests[offset:])
    expected_requests = Counter(
        ("GET", "/" + name, 200)
        for _, source, reference in WORKLOAD
        for name in (source, reference)
    )
    if request_counts != expected_requests:
        raise RuntimeError(f"{mode} 实际 HTTP 请求与固定清单不符: {request_counts}")

    calls = {}
    for env_id, expected_calls in ((crawler, len(WORKLOAD) * 3), (analyst, len(WORKLOAD) * 4)):
        calls[env_id] = [
            event for event in events if event["kind"] == "agent.tool" and event["env_id"] == env_id
        ]
        if len(calls[env_id]) != expected_calls:
            raise RuntimeError(f"{env_id} 的实际工具调用数与工作量不符")
    if {json.loads(run["meta"])["mode"] for run in chain["runs"]} != {mode}:
        raise RuntimeError(f"{mode} 的账本执行模式记录不符")
    overlapping_calls = sum(event["id"] < calls[crawler][-1]["id"] for event in calls[analyst])
    collects = [event["payload"] for event in events if event["kind"] == "agent.stream_collect"]
    if mode == "stream":
        if (
            not overlapping_calls
            or len(collects) != len(WORKLOAD)
            or not collects[0]["before_seal"]
            or any(item["truncated"] for item in collects)
        ):
            raise RuntimeError("流式任务未在抓取结束前执行真实分析，或流式审计不完整")
    elif overlapping_calls or collects:
        raise RuntimeError("整批任务在抓取完成前执行了分析")

    hashes = {}
    for artifact in result.artifacts:
        contents = (system.store.root / artifact.path).read_bytes()
        if artifact.truncated or hashlib.sha256(contents).hexdigest() != artifact.sha256:
            raise RuntimeError(f"产物 {artifact.name} 未完整通过 SHA-256 校验")
        if expected.setdefault(artifact.name, contents) != contents:
            raise RuntimeError(f"产物 {artifact.name} 与整批预热结果的字节不一致")
        hashes[artifact.name] = artifact.sha256
        if artifact.name == PAGES:
            pages = json.loads(contents)
            for key, _, reference in WORKLOAD:
                if reference not in {urlsplit(link).path for link in pages[key]["links"]}:
                    raise RuntimeError(f"官方文档 {pages[key]['file']} 缺少链接 {reference}")
    if set(hashes) != {PAGES, SUMMARY}:
        raise RuntimeError("任务未产出完整的页面快照和分析汇总")
    return {
        "task_id": task.id,
        "ms": round(elapsed_ms, 3),
        "sha256": hashes,
        "http_requests": sum(request_counts.values()),
        "tools": sum(map(len, calls.values())),
        "overlapping_analysis_calls": overlapping_calls,
    }


async def measure(root: Path, server: ArchiveServer) -> dict:
    system = build(root, RouterConfig(strategy="static"))
    try:
        if system.errors:
            raise RuntimeError(f"无法加载基准环境: {system.errors}")
        expected: dict[str, bytes] = {}
        warmup = {mode: await run_one(system, server, mode, expected) for mode in MODES}
        pairs = []
        for index in range(6):
            order = MODES if index % 2 == 0 else MODES[::-1]
            pair = {mode: await run_one(system, server, mode, expected) for mode in order}
            if any(pair[mode]["sha256"] != warmup["block"]["sha256"] for mode in MODES):
                raise RuntimeError("整批与流式的最终快照或分析汇总 SHA-256 不一致")
            pairs.append({"order": order, **pair})
        medians = {mode: median(pair[mode]["ms"] for pair in pairs) for mode in MODES}
        savings = (medians["block"] - medians["stream"]) / medians["block"]
        return {"warmup": warmup, "pairs": pairs, "median_ms": medians, "reduction": savings}
    finally:
        await system.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="比较同一份官方 SQLite 文档的整批与流式处理")
    parser.add_argument("archive", type=Path, help="官方 sqlite-doc-3510300.zip 的本地路径")
    args = parser.parse_args()
    scratch = ROOT / ".test-state"
    scratch.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="stream-benchmark-", dir=scratch))
    public = root / "public"
    sizes = extract_pages(args.archive, public)
    environments(root)
    with ArchiveServer(public) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            report = asyncio.run(measure(root, server))
        finally:
            server.shutdown()
            thread.join()
    result = {
        "archive": ARCHIVE_URL,
        "archive_sha256": ARCHIVE_SHA256,
        "system": platform.system(),
        "python": platform.python_version(),
        "mcp": version("mcp"),
        "workspace": str(root),
        "workload": WORKLOAD,
        "document_bytes": sizes,
        **report,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if report["reduction"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
