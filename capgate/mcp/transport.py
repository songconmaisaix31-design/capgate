from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol

from mcp import StdioServerParameters
from mcp.types import CallToolResult, ListToolsResult

from ..models import CapabilityVersion


class MCPTransport(Protocol):
    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult: ...


def sdk_major() -> int:
    major = int(version("mcp").split(".", 1)[0])
    requested = os.environ.get("CAPGATE_MCP_SDK")
    if major not in (1, 2):
        raise ValueError(f"不支持 MCP SDK {major}.x，请安装 capgate[mcp] 或 capgate[mcp-v2]")
    if requested is not None and requested != str(major):
        raise ValueError(f"CAPGATE_MCP_SDK={requested!r} 与已安装的 MCP SDK {major}.x 不一致")
    return major


@asynccontextmanager
async def open_transport(spec: CapabilityVersion) -> AsyncIterator[MCPTransport]:
    target = _target(spec)
    if sdk_major() == 2:
        from mcp import Client

        async with Client(target) as client:
            yield client
        return

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    async with AsyncExitStack() as stack:
        if isinstance(target, StdioServerParameters):
            read, write = await stack.enter_async_context(stdio_client(target))
        else:
            read, write, _ = await stack.enter_async_context(streamablehttp_client(target))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        yield session


def _target(spec: CapabilityVersion) -> StdioServerParameters | str:
    if spec.transport == "http":
        if not spec.url:
            raise ValueError("MCP 的 HTTP 传输缺少 url")
        return spec.url
    if spec.transport != "stdio":
        raise ValueError(f"不支持的 MCP 传输：{spec.transport}")
    if not spec.command:
        raise ValueError("MCP 的 stdio 传输缺少 command")
    command = sys.executable if spec.command in ("python", "python3", "py") else spec.command
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    # 示例服务可以从任意工作目录启动，仍需找到 capgate 包。
    package_root = str(Path(__file__).resolve().parents[2])
    python_paths = [package_root, *filter(None, env.get("PYTHONPATH", "").split(os.pathsep))]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))
    env.update(spec.env)
    return StdioServerParameters(command=command, args=spec.args, env=env, cwd=spec.cwd or None)
