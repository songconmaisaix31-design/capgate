"""scripted adapter：确定性快路径。

这是架构里"不经过 LLM 的路径"：env 把一套已验证的步骤写成 recipe，
网关直接照做。价值在于——同样的输入永远同样的结果，可测、可审计、可缓存。

recipe 的每一步：
    - capability: 能力 id
      tool: 工具名
      args: {...}          # 支持 {{ input.x }} 模板
      as: 变量名            # 结果存进命名空间
      on_error: fail|ignore
    - emit:
        name: 产物名
        body: {...}
"""

from __future__ import annotations

from typing import Any

from .base import AgentAdapter, AgentContext, AgentError, AgentOutput, register, render


class ScriptedAdapter:
    name = "scripted"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    async def run(self, ctx: AgentContext) -> AgentOutput:
        steps = ctx.env.agent.recipe
        if not steps:
            raise AgentError(f"env {ctx.env.id} 用 scripted adapter 却没有 recipe")
        ns = ctx.namespace()
        produced: dict[str, Any] = {}
        trace: list[dict[str, Any]] = []

        for index, step in enumerate(steps):
            if "emit" in step:
                emitted = render(step["emit"], ns)
                name = emitted.get("name")
                if not name:
                    raise AgentError(f"第 {index} 步 emit 缺少 name")
                body = emitted.get("body")
                produced[name] = body
                ns[name] = body
                trace.append({"step": index, "emit": name, "ok": True})
                ctx.log("agent.emit", {"name": name, "bytes": len(str(body))})
                continue

            capability = step.get("capability")
            tool = step.get("tool")
            if not capability or not tool:
                raise AgentError(f"第 {index} 步缺少 capability/tool")
            args = render(step.get("args", {}), ns)
            on_error = step.get("on_error", "fail")
            try:
                result = await ctx.toolbox.call(capability, tool, args)
            except Exception as exc:
                trace.append(
                    {"step": index, "tool": f"{capability}.{tool}", "ok": False, "error": str(exc)}
                )
                if on_error == "ignore":
                    ns[step.get("as", f"step{index}")] = None
                    continue
                raise
            alias = step.get("as") or f"step{index}"
            ns[alias] = result
            trace.append({"step": index, "tool": f"{capability}.{tool}", "ok": True, "as": alias})
            ctx.log("agent.tool", {"capability": capability, "tool": tool, "as": alias})

        return AgentOutput(artifacts=produced, trace=trace)


@register("scripted")
def _factory(config: dict[str, Any]) -> AgentAdapter:
    return ScriptedAdapter(config)
