"""capgate CLI。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from typing import Any

import typer
from rich.console import Console

from . import __version__, demo, render
from .config import paths as resolve_paths
from .models import TaskSpec
from .system import System, build, load_router_config

app = typer.Typer(add_completion=False, help="capgate —— 本地能力网关")
console = Console()


def emit_json(obj: Any) -> None:
    """机器可读输出走裸 stdout：rich 的软换行会把 JSON 字符串切断。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _system(root: str | None = None, strategy: str | None = None) -> System:
    p = resolve_paths(root)
    cfg = load_router_config(p.root)
    if strategy:
        cfg.strategy = strategy
    return build(p.root, cfg)


@app.command()
def version() -> None:
    """打印版本。"""
    console.print(f"capgate {__version__}")


@app.command()
def doctor(root: str = typer.Option(None, "--root", help="仓根目录（默认当前目录）")) -> None:
    """自检：依赖、SQLite、能力与 env 的载入问题。"""
    from .db import wal_is_unsafe

    p = resolve_paths(root)
    console.print(f"[bold]root[/bold] {p.root}")
    console.print(f"[bold]python[/bold] {sys.version.split()[0]}")
    ver = sqlite3.sqlite_version
    if wal_is_unsafe():
        console.print(
            f"[yellow]sqlite {ver}[/yellow] 落在 WAL-reset 竞态区间（< 3.51.3）。"
            "单进程单连接使用风险低；若要多进程并发写，设 CAPGATE_SQLITE_JOURNAL=delete。"
        )
    else:
        console.print(f"[bold]sqlite[/bold] {ver}")
    try:
        import importlib.metadata as md

        console.print(f"[bold]mcp[/bold] {md.version('mcp')}")
    except Exception:
        console.print("[red]mcp 未安装[/red]（pip install 'mcp>=1.2,<2'）")

    system = build(p.root)
    console.print(
        f"[bold]envs[/bold] {len(system.registry.envs)}  "
        f"[bold]capabilities[/bold] {len(system.pool.all())}"
    )
    if system.errors:
        console.print("[red]载入问题：[/red]")
        for err in system.errors:
            console.print(f"  · {err}")
    else:
        console.print("[green]载入无问题[/green]")
    for env in system.registry.all():
        for ref in env.capabilities:
            try:
                system.pool.resolve(ref)
            except Exception as exc:
                console.print(f"[red]{env.id} → {ref.id}@{ref.version} 无法解析：{exc}[/red]")
    system.ledger.close()


@app.command()
def envs(root: str = typer.Option(None, "--root")) -> None:
    """列出领域环境。"""
    system = _system(root)
    render.envs_table(system.registry, console)
    system.ledger.close()


@app.command()
def caps(root: str = typer.Option(None, "--root")) -> None:
    """列出能力池。"""
    system = _system(root)
    render.caps_table(system.pool, console)
    system.ledger.close()


@app.command()
def probe(
    capability: str = typer.Argument(..., help="能力 id"),
    version: str = typer.Option("*", "--version", help="版本说明符"),
    root: str = typer.Option(None, "--root"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """真连一次某个能力，打印它实际暴露的工具。接新 MCP 时先用它验。"""

    from rich.table import Table

    from .mcp import MCPConnection
    from .models import CapabilityRef

    system = _system(root)
    spec = system.pool.resolve(CapabilityRef(id=capability, version=version)).version

    async def main() -> int:
        conn = await MCPConnection.open(spec, label=capability)
        try:
            tools = conn.tool_specs()
        finally:
            await conn.close()
        if as_json:
            emit_json(tools)
            return 0
        table = Table(title=f"{capability}@{spec.version} · {len(tools)} tools", header_style="dim")
        for col in ("tool", "说明", "参数"):
            table.add_column(col)
        for tool in tools:
            props = list((tool.get("input_schema") or {}).get("properties", {}))
            table.add_row(tool["name"], (tool["description"] or "")[:70], ", ".join(props[:6]))
        console.print(table)
        return 0

    try:
        code = asyncio.run(main())
    finally:
        system.ledger.close()
    raise typer.Exit(code)


@app.command(name="call")
def call_tool(
    capability: str = typer.Argument(..., help="能力 id"),
    tool: str = typer.Argument(..., help="工具名"),
    args: str = typer.Option("{}", "--args", help="JSON 参数"),
    version: str = typer.Option("*", "--version"),
    root: str = typer.Option(None, "--root"),
) -> None:
    """直接调一次某个能力上的工具，用来核对真实输出形状。"""
    from .mcp import MCPConnection
    from .models import CapabilityRef

    payload = json.loads(args)
    if not isinstance(payload, dict):
        raise typer.BadParameter("--args 必须是 JSON 对象")
    system = _system(root)
    spec = system.pool.resolve(CapabilityRef(id=capability, version=version)).version

    async def main():
        conn = await MCPConnection.open(spec, label=capability)
        try:
            return await conn.call(tool, payload)
        finally:
            await conn.close()

    try:
        emit_json(asyncio.run(main()))
    finally:
        system.ledger.close()


@app.command(name="demo")
def demo_cmd(
    scenario: str = typer.Option("all", "--scenario", "-s", help="pipeline | branch | doc | all"),
    root: str = typer.Option(None, "--root"),
    strategy: str = typer.Option(None, "--strategy", help="thompson | epsilon | static"),
) -> None:
    """跑演示场景（离线，不依赖外网）。"""
    system = _system(root, strategy)

    async def main() -> int:
        names = list(demo.SCENARIOS) if scenario == "all" else [scenario]
        failed = 0
        try:
            for name in names:
                console.rule(f"[bold]{name}[/bold] · {demo.SCENARIOS[name]['hint']}")
                result = await demo.run_demo(system, name)
                render.result_panel(result, console)
                render.artifacts_view(result, console)
                failed += 0 if result.status == "ok" else 1
        finally:
            await system.aclose()
        return failed

    raise typer.Exit(asyncio.run(main()))


@app.command()
def solve(
    goal: str = typer.Argument(..., help="任务目标"),
    tag: list[str] = typer.Option([], "--tag", "-t", help="可重复：任务标签"),
    inp: list[str] = typer.Option([], "--input", "-i", help="可重复：k=v 输入"),
    hint: list[str] = typer.Option([], "--hint", help="可重复：指定 env"),
    root: str = typer.Option(None, "--root"),
    strategy: str = typer.Option(None, "--strategy"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """提交一个任务并打印决策链。"""
    inputs: dict[str, Any] = {}
    for pair in inp:
        key, _, value = pair.partition("=")
        inputs[key.strip()] = value.strip()
    system = _system(root, strategy)

    async def main() -> int:
        try:
            spec = TaskSpec(goal=goal, tags=list(tag), inputs=inputs, hint_envs=list(hint))
            result = await system.orchestrator.run(spec)
            if as_json:
                emit_json(json.loads(result.model_dump_json()))
            else:
                render.result_panel(result, console)
                render.artifacts_view(result, console)
            return 0 if result.status == "ok" else 1
        finally:
            await system.aclose()

    raise typer.Exit(asyncio.run(main()))


@app.command()
def chain(task_id: str = typer.Argument(...), root: str = typer.Option(None, "--root")) -> None:
    """查看某个任务的决策链。"""
    system = _system(root)
    render.chain_view(system.ledger.chain(task_id), console)
    system.ledger.close()


@app.command()
def rate(
    task_id: str = typer.Argument(...),
    good: bool = typer.Option(False, "--good", help="人工验收：好"),
    bad: bool = typer.Option(False, "--bad", help="人工验收：差"),
    edits: int = typer.Option(0, "--edits", help="人工改动次数"),
    note: str = typer.Option(None, "--note"),
    root: str = typer.Option(None, "--root"),
) -> None:
    """给任务打人工验收标签（优先于自动判定，直接喂给路由）。"""
    if good == bad:
        raise typer.BadParameter("必须且只能给一个 --good 或 --bad")
    system = _system(root)
    system.ledger.outcome(task_id, success=good, human_edits=edits, note=note)
    console.print(f"[green]已记录[/green] {task_id} success={good}")
    system.ledger.close()


@app.command()
def stats(
    root: str = typer.Option(None, "--root"), task_type: str = typer.Option(None, "--type")
) -> None:
    """账本聚合。"""
    system = _system(root)
    render.stats_panel(system.ledger.stats(), system.ledger.perf(task_type=task_type), console)
    system.ledger.close()


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8787, "--port"),
    root: str = typer.Option(None, "--root"),
) -> None:
    """起本地 HTTP 服务 + 看板。"""
    import uvicorn

    from .server import create_app

    p = resolve_paths(root)
    app_ = create_app(build(p.root))
    console.print(f"看板 → [bold]http://{host}:{port}[/bold]")
    uvicorn.run(app_, host=host, port=port, log_level="warning")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
