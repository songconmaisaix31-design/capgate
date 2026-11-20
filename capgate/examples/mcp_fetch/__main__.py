"""示例 MCP server：抓取 + 抽链接 + 抽正文。

使用官方 MCP Python SDK。
启动：``python -m capgate.examples.mcp_fetch``（stdio）
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx

from capgate.mcp.server import create_server

mcp = create_server("capgate-fetch")


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


_http_client: httpx.Client | None = None


def _client() -> httpx.Client:
    # 常驻连接池：httpx 每次新开 Client 在 Windows 上约 300ms 的开销，
    # 真实抓取器都会复用连接，这里也复用以贴近真实负载、避免每请求重新握手。
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "capgate/0.1"},
        )
    return _http_client


def _read(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        path = Path(url2pathname(parsed.path))
        if not path.exists():
            raise FileNotFoundError(f"本地文件不存在：{path}")
        return path.read_text(encoding="utf-8", errors="replace")
    if parsed.scheme in ("http", "https"):
        resp = _client().get(url)
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
