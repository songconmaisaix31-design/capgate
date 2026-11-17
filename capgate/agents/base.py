"""Agent 适配器契约。

网关只认这一份契约，异构因此不会变成 N×M 适配矩阵：
新增一个框架 = 新增一个 adapter + 一个 env 声明，网关零改动。

契约刻意做小：invoke 一次，返回产物。流式、取消走 task 取消 + 超时；
用量与轨迹塞进 AgentOutput，供账本记账。
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import EnvManifest, TaskSpec, utcnow
from ..pool import Pool, Resolved


class AgentError(RuntimeError):
    def __init__(self, message: str, *, usage: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.usage = usage or {}


@dataclass
class AgentOutput:
    artifacts: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentContext:
    task: TaskSpec
    env: EnvManifest
    model: str
    artifacts: dict[str, Any]
    toolbox: ToolBox
    budget_s: int = 300
    run_id: str = ""
    log: Callable[[str, dict[str, Any]], None] = lambda kind, payload: None

    def namespace(self) -> dict[str, Any]:
        return {
            "input": self.task.inputs,
            "artifacts": self.artifacts,
            "task": {
                "id": self.task.id,
                "goal": self.task.goal,
                "tags": self.task.tags,
                "type": self.task.task_type,
            },
            "env": {"id": self.env.id, "domain": self.env.domain},
            "now": utcnow(),
        }


class AgentAdapter(Protocol):
    name: str

    async def run(self, ctx: AgentContext) -> AgentOutput: ...


# ---------------------------------------------------------------- 工具盒


@dataclass
class ToolCall:
    capability: str
    tool: str
    args: dict[str, Any]
    result: Any = None
    error: str | None = None
    ms: int = 0


class ToolBox:
    """env 内部可见的能力集合。

    共享能力走全池单例连接；非共享能力按 env 实例隔离。
    工具名对外加 ``{cap}__`` 前缀，避免不同 MCP 的同名工具互相覆盖。
    """

    def __init__(
        self,
        pool: Pool,
        hub: Any,
        resolved: dict[str, Resolved],
        instance_id: str = "adhoc",
    ) -> None:
        self.pool = pool
        self.hub = hub
        self.resolved = resolved
        self.instance_id = instance_id
        self.calls: list[ToolCall] = []
        self._advertised_tools: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------ 描述

    async def _connection(self, res: Resolved):
        from ..mcp.client import MCPConnection

        key = res.key if res.ref.shared else f"{self.instance_id}:{res.key}"

        async def factory():
            return await MCPConnection.open(res.version, label=res.key)

        return await self.hub.get(key, factory)

    async def specs(self) -> list[dict[str, Any]]:
        """OpenAI function-calling 兼容的工具描述。"""
        out: list[dict[str, Any]] = []
        advertised: dict[str, tuple[str, str]] = {}
        for cap_id, res in self.resolved.items():
            if res.version.transport == "python":
                specs = [
                    {
                        "name": tool,
                        "description": "",
                        "input_schema": {"type": "object", "additionalProperties": True},
                    }
                    for tool in self._python_tools(res)
                ]
            else:
                conn = await self._connection(res)
                specs = conn.tool_specs()
            for spec in specs:
                tool = spec["name"]
                if res.version.tools and tool not in res.version.tools:
                    continue
                name = re.sub(r"[^A-Za-z0-9_]", "_", f"{cap_id}__{tool}")
                if len(name) > 64:
                    raise AgentError(f"工具名 {name!r} 超过 API 的 64 字符上限")
                if name in advertised:
                    raise AgentError(f"工具名 {name!r} 重复，请检查能力和工具名称")
                advertised[name] = (cap_id, tool)
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": (
                                spec["description"] or f"{res.capability.title or cap_id} / {tool}"
                            )[:1024],
                            "parameters": spec["input_schema"],
                        },
                    }
                )
        self._advertised_tools = advertised
        return out

    async def call_advertised(self, name: str, args: dict[str, Any]) -> Any:
        choice = self._advertised_tools.get(name)
        if choice is None:
            raise AgentError(f"模型请求了未公开的工具 {name!r}")
        return await self.call(*choice, args)

    def _python_tools(self, res: Resolved) -> list[str]:
        if res.version.entries:
            return list(res.version.entries)
        if res.version.entry:
            return [res.version.entry.rsplit(":", 1)[-1]]
        return list(res.version.tools)

    # ------------------------------------------------------------ 调用

    async def call(self, capability: str, tool: str, args: dict[str, Any] | None = None) -> Any:
        args = args or {}
        if capability not in self.resolved:
            raise AgentError(f"env 未挂载能力 {capability!r}（已挂载：{sorted(self.resolved)}）")
        res = self.resolved[capability]
        started = _now_ms()
        call = ToolCall(capability=capability, tool=tool, args=args)
        try:
            if res.version.transport == "python":
                result = await self._call_python(res, tool, args)
            else:
                if res.version.tools and tool not in res.version.tools:
                    raise AgentError(f"{capability} 未开放工具 {tool!r}")
                conn = await self._connection(res)
                result = await conn.call(tool, args)
            call.result = result
            return result
        except Exception as exc:
            call.error = str(exc)
            raise
        finally:
            call.ms = _now_ms() - started
            self.calls.append(call)

    async def _call_python(self, res: Resolved, tool: str, args: dict[str, Any]) -> Any:
        target = res.version.entries.get(tool) or res.version.entry
        if not target or ":" not in target:
            raise AgentError(f"{res.capability.id} 没有可调用的 python 入口 {tool!r}")
        module_name, _, func_name = target.partition(":")
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
        if inspect.iscoroutinefunction(func):
            return await func(**args)
        return await asyncio.to_thread(func, **args)


# ---------------------------------------------------------------- 注册表


_ADAPTERS: dict[str, Callable[[dict[str, Any]], AgentAdapter]] = {}


def register(name: str):
    def deco(
        factory: Callable[[dict[str, Any]], AgentAdapter],
    ) -> Callable[[dict[str, Any]], AgentAdapter]:
        _ADAPTERS[name] = factory
        return factory

    return deco


def build(name: str, config: dict[str, Any] | None = None) -> AgentAdapter:
    if name not in _ADAPTERS:
        raise AgentError(f"未知 agent adapter {name!r}（可用：{sorted(_ADAPTERS)}）")
    return _ADAPTERS[name](config or {})


def adapters() -> list[str]:
    return sorted(_ADAPTERS)


# ---------------------------------------------------------------- 模板渲染

_TEMPLATE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def render(obj: Any, ns: dict[str, Any]) -> Any:
    """把 ``{{ input.url }}`` / ``{{ artifacts.foo.bar }}`` / ``{{ page }}`` 求值。

    整串就是一个占位符时保留原始类型（dict/list），混在文本里时转成字符串。
    """
    if isinstance(obj, str):
        whole = _TEMPLATE.fullmatch(obj.strip())
        if whole:
            return lookup(ns, whole.group(1))
        return _TEMPLATE.sub(lambda m: _stringify(lookup(ns, m.group(1))), obj)
    if isinstance(obj, list):
        return [render(x, ns) for x in obj]
    if isinstance(obj, dict):
        return {k: render(v, ns) for k, v in obj.items()}
    return obj


def lookup(ns: dict[str, Any], path: str) -> Any:
    cur: Any = ns
    for part in path.split("."):
        part = part.strip()
        if isinstance(cur, dict):
            if part not in cur:
                raise AgentError(f"模板变量不存在：{path!r}（断在 {part!r}）")
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError) as exc:
                raise AgentError(f"模板变量下标非法：{path!r}") from exc
        else:
            raise AgentError(f"模板变量无法继续下钻：{path!r}（断在 {part!r}）")
    return cur


def _stringify(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
