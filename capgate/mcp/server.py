from __future__ import annotations

from typing import TYPE_CHECKING

from .transport import sdk_major

if TYPE_CHECKING:
    from mcp.server import MCPServer
    from mcp.server.fastmcp import FastMCP


def create_server(name: str) -> FastMCP | MCPServer:
    if sdk_major() == 2:
        from mcp.server import MCPServer

        return MCPServer(name)

    from mcp.server.fastmcp import FastMCP

    return FastMCP(name)
