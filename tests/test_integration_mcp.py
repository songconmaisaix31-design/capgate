"""真实 MCP 的连通性验证。

这些测试会拉起本机真实的 Node 侧 MCP（BCA / FIT），因此按可用性跳过：
路径不在就不跑，而不是假装通过。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from capgate.mcp import MCPConnection
from capgate.models import CapabilityRef

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


async def _tools(system, capability: str) -> dict[str, dict]:
    spec = system.pool.resolve(CapabilityRef(id=capability)).version
    conn = await MCPConnection.open(spec, label=capability)
    try:
        return {t["name"]: t for t in conn.tool_specs()}
    finally:
        await conn.close()


@requires_bca
async def test_bca_exposes_contract_tools(system):
    tools = await _tools(system, "mcp-bca")
    assert len(tools) > 100, "BCA 是一个工具箱，工具数应该远多于示例 MCP"
    assert {"capability_list", "workstation_list", "evm_balance"} <= set(tools)


@requires_fit
async def test_fit_exposes_registry_tools(system):
    tools = await _tools(system, "mcp-fit")
    assert set(tools) == {
        "fit_list",
        "fit_resolve",
        "fit_register",
        "fit_register_agent",
        "fit_probe",
        "fit_doctor",
    }


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
