"""内置 SDK 能力：不经过 MCP、直接 import 的 python 函数。

这就是能力池里 ``kind: sdk`` / ``transport: python`` 的那一类。
它证明"能力"不必都是 MCP —— RAG、SDK、甚至一段本地算法都可以是能力，
env 只关心 id 和版本。

约定：能力函数一律接受关键字参数、返回 dict（见 docs/decisions.md）。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import UTC
from urllib.parse import urlparse

_WORD = re.compile(r"[A-Za-z][A-Za-z\-']+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def summarize_links(links: list[str] | None = None) -> dict:
    links = links or []
    hosts = Counter(urlparse(u).netloc or "(relative)" for u in links)
    return {
        "total": len(links),
        "unique": len(set(links)),
        "hosts": dict(hosts.most_common()),
        "unique_hosts": len(hosts),
    }


def word_freq(text: str = "", top: int = 8) -> dict:
    words = [w.lower() for w in _WORD.findall(text or "")]
    cjk = _CJK.findall(text or "")
    counter = Counter(words)
    if cjk:
        counter.update(cjk)
    return {"top": counter.most_common(int(top)), "tokens": sum(counter.values())}


def address_balance(address: str = "") -> dict:
    """演示用：确定性伪余额，**不是真实链上查询**。

    真接链时把这里换成 RPC / MCP 实现即可，env 与路由都不用动——
    这正是能力池存在的意义。
    """
    if not re.fullmatch(r"0x[0-9a-fA-F]{6,64}", address or ""):
        raise ValueError("address 需要是 0x 开头的十六进制地址")
    h = hashlib.sha256(address.lower().encode()).hexdigest()
    balance = int(h[:8], 16) / 1_000_000
    return {
        "address": address,
        "balance": round(balance, 6),
        "unit": "ETH",
        "source": "demo-deterministic",
        "note": "演示数据，非真实链上查询",
    }


def compact_text(text: str = "", limit: int = 600) -> dict:
    text = re.sub(r"\s+", " ", text or "").strip()
    return {"text": text[: int(limit)], "chars": len(text), "truncated": len(text) > int(limit)}


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds")


def compact_catalog(
    capabilities: list | None = None, workstations: list | None = None, sample: int = 12
) -> dict:
    """BCA 的能力/工作台清单压成可给人看的报告。

    原始清单有上百条、每条带描述，直接当产物会撑爆上下文，也会让下游只能全量读。
    """
    caps = [c for c in (capabilities or []) if isinstance(c, dict)]
    statuses: Counter[str] = Counter()
    missing: set[str] = set()
    for cap in caps:
        availability = cap.get("availability") or {}
        statuses[str(availability.get("status") or "unknown")] += 1
        missing.update(availability.get("missing") or [])
    stations = [w for w in (workstations or []) if isinstance(w, dict)]
    return {
        "capability_count": len(caps),
        "by_status": dict(statuses),
        "missing_requirements": sorted(missing),
        "workstations": [{"id": w.get("id"), "name": w.get("name")} for w in stations],
        "sample": [c.get("name") for c in caps[: int(sample)]],
        "generated_at": _now(),
    }


def compact_fit_report(
    doctor: dict | None = None, listing: dict | None = None, sample: int = 10
) -> dict:
    """FIT 注册表体检报告：多少端点、坏了几个、各类型分布。"""
    doctor = doctor or {}
    endpoints = (listing or {}).get("endpoints") or []
    kinds = Counter(str(e.get("kind") or "unknown") for e in endpoints)
    return {
        "root": doctor.get("root"),
        "total": doctor.get("total", len(endpoints)),
        "by_kind": dict(kinds or doctor.get("byKind") or {}),
        "agents": doctor.get("agents", 0),
        "broken": doctor.get("broken") or [],
        "sample": [f"{e.get('kind')}:{e.get('name')}" for e in endpoints[: int(sample)]],
        "updated_at": doctor.get("updatedAt"),
        "generated_at": _now(),
    }
