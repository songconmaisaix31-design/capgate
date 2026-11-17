"""示例 MCP server：本地语料检索（RAG 的最小可用形态）。

它演示另一类环境：知识型能力。检索算法刻意做得很土——因为这里要证明的是
"RAG 可以作为一个独立环境被拉起"，而不是"检索做得好"。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from capgate.mcp.server import create_server

mcp = create_server("capgate-rag")

_CORPUS_ENV = "CAPGATE_CORPUS"
_DEFAULT = Path(__file__).resolve().parents[3] / "fixtures" / "corpus"


def _corpus() -> Path:
    return Path(os.environ.get(_CORPUS_ENV, _DEFAULT))


def _terms(text: str) -> list[str]:
    latin = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-']+", text)]
    cjk = re.findall(r"[\u4e00-\u9fff]{2,4}", text)
    return latin + cjk


def _snippet(text: str, term: str, width: int = 120) -> str:
    idx = text.lower().find(term.lower())
    if idx < 0:
        return text[:width].replace("\n", " ")
    start = max(0, idx - width // 2)
    return text[start : start + width].replace("\n", " ")


@mcp.tool()
def search(query: str, k: int = 3) -> dict:
    """在本地语料里按关键词打分检索，返回命中片段。"""
    root = _corpus()
    if not root.exists():
        raise FileNotFoundError(f"语料目录不存在：{root}")
    terms = _terms(query) or [query]
    scored = []
    for doc in sorted(root.glob("**/*")):
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        score = sum(low.count(t.lower()) for t in terms)
        if score:
            scored.append((score, doc, text))
    scored.sort(key=lambda row: row[0], reverse=True)
    hits = [
        {
            "doc": str(doc.relative_to(root)).replace("\\", "/"),
            "score": score,
            "snippet": _snippet(text, terms[0]),
        }
        for score, doc, text in scored[: int(k)]
    ]
    return {"query": query, "terms": terms, "hits": hits, "corpus": str(root.name)}


@mcp.tool()
def list_docs() -> dict:
    """列出语料里的文档。"""
    root = _corpus()
    docs = [
        str(p.relative_to(root)).replace("\\", "/")
        for p in sorted(root.glob("**/*"))
        if p.is_file()
    ]
    return {"docs": docs, "count": len(docs)}


def main() -> None:
    import logging

    logging.getLogger("mcp").setLevel(logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
