"""真实 MCP 的连通性验证。

CAPGATE_MCP_SDK=1 或 2 指定安装环境应使用的客户端版本。
本机存在 BCA 与 FIT 时同时验证这两个真实服务。
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import pytest
import uvicorn

from capgate.mcp import MCPConnection
from capgate.models import CapabilityRef, CapabilityVersion

pytestmark = pytest.mark.integration

BCA_HOME = Path(os.environ.get("CAPGATE_BCA_HOME", "C:/Users/DW/orca/blockchain-anything"))
FIT_HOME = Path(os.environ.get("CAPGATE_FIT_HOME", "D:/repos/fit-protocol"))

requires_bca = pytest.mark.skipif(
    not (BCA_HOME / "dist" / "mcp" / "main.js").exists(),
    reason=f"BCA 未安装或未构建：{BCA_HOME}",
)
requires_fit = pytest.mark.skipif(
    not (FIT_HOME / "bin" / "fit.mjs").exists(), reason=f"FIT 未安装：{FIT_HOME}"
)


async def _connect(system, capability: str) -> MCPConnection:
    spec = system.pool.resolve(CapabilityRef(id=capability)).version
    return await MCPConnection.open(spec, label=capability)


@requires_bca
async def test_bca_exposes_contract_tools(system):
    conn = await _connect(system, "mcp-bca")
    try:
        tools = {tool["name"]: tool for tool in conn.tool_specs()}
        assert len(tools) > 100
        assert {"capability_list", "workstation_list", "evm_balance"} <= set(tools)
        assert "address" in tools["evm_balance"]["input_schema"]["properties"]
        capabilities = await conn.call("capability_list", {})
        assert isinstance(capabilities, list) and capabilities
        assert all(capability["name"] for capability in capabilities)
    finally:
        await conn.close()


@requires_fit
async def test_fit_exposes_registry_tools(system):
    conn = await _connect(system, "mcp-fit")
    try:
        assert set(conn.tools) == {
            "fit_list",
            "fit_resolve",
            "fit_register",
            "fit_register_agent",
            "fit_probe",
            "fit_doctor",
        }
    finally:
        await conn.close()


async def test_capgate_server_returns_structured_tool_data(system, repo_root):
    conn = await _connect(system, "mcp-fetch")
    try:
        uri = (repo_root / "fixtures" / "sample_site.html").as_uri()
        result = await conn.call("fetch_url", {"url": uri})
        assert result["url"] == uri
        assert result["status"] == 200
        assert result["html"] == (repo_root / "fixtures" / "sample_site.html").read_text("utf-8")
    finally:
        await conn.close()


async def test_capgate_server_over_streamable_http():
    from capgate.examples.mcp_rag.__main__ import mcp

    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="error"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        await serving
                        pytest.fail("MCP HTTP 服务启动前已退出")
                    await asyncio.sleep(0.02)

            spec = CapabilityVersion(
                version="1.0.0", transport="http", url=f"http://127.0.0.1:{port}/mcp"
            )
            conn = await MCPConnection.open(spec)
            try:
                assert "list_docs" in conn.tools
                result = await conn.call("list_docs", {})
                assert "routing.md" in result["docs"]
            finally:
                await conn.close()
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=15)


@requires_fit
async def test_fit_env_produces_report(system):
    from capgate.models import TaskSpec

    task = TaskSpec(goal="看一下环境注册表", tags=["环境", "工具链"], budget_s=120)
    result = await system.orchestrator.run(task)

    assert result.status == "ok", result.error
    artifact = next(a for a in result.artifacts if a.name == "env_report")
    payload = system.store.load(artifact)
    assert payload["total"] > 0
    assert "by_kind" in payload
