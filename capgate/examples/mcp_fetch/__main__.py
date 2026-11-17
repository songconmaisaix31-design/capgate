"""示例 MCP server：抓取 + 抽链接 + 抽正文。

用官方 SDK 的 FastMCP 写，几十行就是一个真 MCP。
启动：``python -m capgate.examples.mcp_fetch``（stdio）

约定：工具一律返回 dict —— FastMCP 会把 dict 序列化成单个 JSON 文本块，
客户端一行 json.loads 就能拿回结构；返回 list 会被拆成多个文本块。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("capgate-fetch")


class _Extract(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip = 0
        self.links: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            chunk = data.strip()
            if chunk:
                self.text.append(chunk)


def _read(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = Path(url2pathname(parsed.path))
        if not path.exists():
            raise FileNotFoundError(f"本地文件不存在：{path}")
        return path.read_text(encoding="utf-8", errors="replace")
    if parsed.scheme in ("http", "https"):
        import httpx

        resp = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "capgate/0.1"})
        resp.raise_for_status()
        return resp.text
    raise ValueError(f"不支持的协议：{parsed.scheme or '(空)'}")


@mcp.tool()
def fetch_url(url: str) -> dict:
    """抓取一个 URL（http/https/file），返回标题、链接列表和正文摘要。"""
    html = _read(url)
    parser = _Extract()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.text)).strip()
    return {
        "url": url,
        "status": 200,
        "title": parser.title.strip(),
        "html": html,
        "html_bytes": len(html),
        "links": parser.links,
        "text": text[:8000],
    }


@mcp.tool()
def extract_links(html: str, base: str = "") -> dict:
    """从 HTML 片段里抽出链接。base 非空时把相对链接补成绝对链接。"""
    parser = _Extract()
    parser.feed(html or "")
    links = parser.links
    if base:
        from urllib.parse import urljoin

        links = [urljoin(base, link) for link in links]
    return {"links": links, "count": len(links)}


@mcp.tool()
def extract_text(html: str) -> dict:
    """抽出可见正文并按空白折叠。"""
    parser = _Extract()
    parser.feed(html or "")
    text = re.sub(r"\s+", " ", " ".join(parser.text)).strip()
    return {"text": text, "chars": len(text)}


def main() -> None:
    import logging

    logging.getLogger("mcp").setLevel(logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
