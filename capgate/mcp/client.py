"""MCP 客户端封装。

要点：MCP 的 stdio 客户端上下文必须在**同一个 task** 里进出，所以每次连接
开一个常驻 host task，业务侧只拿 session 调工具。这样能力可以被多个 env
共享复用一个进程（``shared: true``），也支持每个 env 一份隔离实例。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.types import CallToolResult, Tool

from ..models import CapabilityVersion
from .transport import MCPTransport, open_transport

DEFAULT_TIMEOUT = 60.0


class MCPError(RuntimeError):
    pass


def _normalize(result: CallToolResult) -> Any:
    # BCA 的 JSON 文本沿用原字段，结构化内容可能额外包一层 result。
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if not result.content:
        return structured
    texts: list[str] = []
    extra: list[dict[str, str]] = []
    for item in result.content:
        if item.type == "text":
            texts.append(item.text)
        else:
            extra.append({"type": item.type})
    merged = "\n".join(texts)
    if extra:
        return {"text": merged, "extra": extra}
    try:
        return json.loads(merged)
    except json.JSONDecodeError:
        return structured if structured is not None else merged


class MCPConnection:
    def __init__(self, spec: CapabilityVersion, label: str = "") -> None:
        self.spec = spec
        self.label = label or spec.version
        self._session: MCPTransport | None = None
        self._tools: dict[str, Tool] = {}
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._sem = asyncio.Semaphore(8)

    # ---------------------------------------------------------------- 生命周期

    @classmethod
    async def open(cls, spec: CapabilityVersion, label: str = "") -> MCPConnection:
        conn = cls(spec, label=label)
        await conn.start()
        return conn

    async def start(self) -> None:
        self._task = asyncio.create_task(self._host(), name=f"mcp-host:{self.label}")
        timeout = float(getattr(self.spec, "startup_timeout", 60.0) or 60.0)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError:
            self._stop.set()
            if self._task:
                self._task.cancel()
            raise MCPError(f"MCP 启动超时（{self.label}，{timeout}s）") from None
        if self._error:
            raise MCPError(f"MCP 连接失败（{self.label}）：{self._error}") from self._error

    async def _host(self) -> None:
        try:
            async with open_transport(self.spec) as session:
                self._tools = await self._list_tools(session)
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            self._session = None

    async def _list_tools(self, session: MCPTransport) -> dict[str, Tool]:
        tools = {}
        seen = set()
        cursor = None
        while True:
            page = await session.list_tools(cursor=cursor)
            tools.update((tool.name, tool) for tool in page.tools)
            cursor = getattr(page, "next_cursor", getattr(page, "nextCursor", None))
            if cursor is None:
                return tools
            if cursor in seen:
                raise MCPError(f"{self.label} 返回了重复的工具分页游标")
            seen.add(cursor)

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None

    # ---------------------------------------------------------------- 调用

    @property
    def tools(self) -> dict[str, Tool]:
        return self._tools

    def tool_specs(self) -> list[dict[str, Any]]:
        out = []
        for name, tool in self._tools.items():
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
            out.append(
                {
                    "name": name,
                    "description": tool.description or "",
                    "input_schema": schema or {"type": "object"},
                }
            )
        return out

    async def call(
        self, name: str, arguments: dict[str, Any], timeout_s: float = DEFAULT_TIMEOUT
    ) -> Any:
        if not self._session:
            raise MCPError(f"MCP 未就绪（{self.label}）")
        if name not in self._tools:
            raise MCPError(f"{self.label} 没有工具 {name!r}（可用：{sorted(self._tools)}）")
        async with self._sem:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments or {}), timeout=timeout_s
            )
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            raise MCPError(f"{self.label}.{name} 返回错误：{_normalize(result)}")
        return _normalize(result)


class MCPHub:
    """共享连接池：同一个 (能力, 版本) 只起一个进程。"""

    def __init__(self) -> None:
        self._conns: dict[str, MCPConnection] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, factory) -> MCPConnection:
        async with self._lock:
            conn = self._conns.get(key)
            if conn is not None and conn._session is not None:
                return conn
            conn = await factory()
            self._conns[key] = conn
            return conn

    async def drop_prefix(self, prefix: str) -> None:
        async with self._lock:
            keys = [k for k in self._conns if k.startswith(prefix)]
            conns = [self._conns.pop(k) for k in keys]
        for conn in conns:
            await conn.close()

    async def aclose(self) -> None:
        async with self._lock:
            conns, self._conns = list(self._conns.values()), {}
        for conn in conns:
            await conn.close()
