"""MCP 客户端封装。

要点：MCP 的 stdio 客户端上下文必须在**同一个 task** 里进出，所以每次连接
开一个常驻 host task，业务侧只拿 session 调工具。这样能力可以被多个 env
共享复用一个进程（``shared: true``），也支持每个 env 一份隔离实例。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from ..models import CapabilityVersion

DEFAULT_TIMEOUT = 60.0


class MCPError(RuntimeError):
    pass


def _normalize(result: Any) -> Any:
    """把 CallToolResult 压成 Python 值：单个文本能 JSON 化就 JSON 化。"""
    content = getattr(result, "content", None)
    if not content:
        return None
    texts: list[str] = []
    extra: list[dict[str, Any]] = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            texts.append(text)
        else:
            extra.append({"type": getattr(item, "type", "unknown")})
    if len(texts) == 1 and not extra:
        text = texts[0]
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text
    if texts and not extra:
        merged = "\n".join(texts)
        try:
            return json.loads(merged)
        except (TypeError, ValueError):
            return merged
    return {"text": "\n".join(texts), "extra": extra}


class MCPConnection:
    def __init__(self, spec: CapabilityVersion, label: str = "") -> None:
        self.spec = spec
        self.label = label or spec.version
        self._session: Any = None
        self._tools: dict[str, Any] = {}
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._sem = asyncio.Semaphore(8)

    # ---------------------------------------------------------------- 生命周期

    @classmethod
    async def open(cls, spec: CapabilityVersion, label: str = "") -> "MCPConnection":
        conn = cls(spec, label=label)
        await conn.start()
        return conn

    async def start(self) -> None:
        self._task = asyncio.create_task(self._host(), name=f"mcp-host:{self.label}")
        await self._ready.wait()
        if self._error:
            raise MCPError(f"MCP 连接失败（{self.label}）：{self._error}") from self._error

    async def _host(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        try:
            async with AsyncExitStack() as stack:
                if self.spec.transport == "stdio":
                    params = StdioServerParameters(
                        command=_command(self.spec.command),
                        args=list(self.spec.args),
                        env=_env(self.spec.env),
                        cwd=self.spec.cwd or None,
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                elif self.spec.transport == "http":
                    from mcp.client.streamable_http import streamablehttp_client

                    if not self.spec.url:
                        raise MCPError("http transport 缺少 url")
                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(self.spec.url)
                    )
                else:
                    raise MCPError(f"不支持的 transport：{self.spec.transport}")

                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                self._tools = {t.name: t for t in listed.tools}
                self._session = session
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - 全部转成 ready 上的错误
            self._error = exc
            self._ready.set()
        finally:
            self._session = None

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
                self._task = None

    # ---------------------------------------------------------------- 调用

    @property
    def tools(self) -> dict[str, Any]:
        return self._tools

    def tool_specs(self) -> list[dict[str, Any]]:
        out = []
        for name, tool in self._tools.items():
            out.append(
                {
                    "name": name,
                    "description": getattr(tool, "description", "") or "",
                    "input_schema": getattr(tool, "inputSchema", None) or {"type": "object"},
                }
            )
        return out

    async def call(self, name: str, arguments: dict[str, Any], timeout: float = DEFAULT_TIMEOUT) -> Any:
        if not self._session:
            raise MCPError(f"MCP 未就绪（{self.label}）")
        if name not in self._tools:
            raise MCPError(f"{self.label} 没有工具 {name!r}（可用：{sorted(self._tools)}）")
        async with self._sem:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments or {}), timeout=timeout
            )
        if getattr(result, "isError", False):
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


def _command(command: str | None) -> str:
    if not command:
        raise MCPError("stdio transport 缺少 command")
    if command in ("python", "python3", "py"):
        return sys.executable
    return command


def _env(extra: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    # 让子进程能 import capgate（示例 MCP server 就在包里），
    # 不依赖调用方从哪个目录启动网关。
    import capgate

    package_root = str(Path(capgate.__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    parts = [package_root] + [p for p in existing.split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    env.update(extra or {})
    return env
