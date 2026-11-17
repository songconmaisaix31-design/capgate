"""终端渲染：把决策链打印成人能读的样子。

决策链是这套系统的一等产物——"为什么走了这条路"必须能当场回答，
而不是事后靠猜。
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .envs import EnvRegistry
from .models import TaskResult
from .pool import Pool


def result_panel(result: TaskResult, console: Console) -> None:
    color = {"ok": "green", "degraded": "yellow", "failed": "red"}[result.status]
    console.print(
        Panel(
            f"[bold]{result.task.goal}[/bold]\n"
            f"task={result.task.id}  type={result.task.task_type}  status=[{color}]{result.status}[/{color}]  "
            f"{result.latency_ms}ms",
            title="任务",
            border_style=color,
        )
    )

    for decision in result.decisions:
        console.print(f"[bold cyan]▸ {decision.stage}[/bold cyan]  ({decision.strategy})  → {decision.chosen}")
        console.print(f"  [dim]{decision.reason}[/dim]")
        if decision.candidates:
            table = Table(show_header=True, header_style="dim", box=None, pad_edge=False)
            table.add_column("候选")
            table.add_column("分数", justify="right")
            table.add_column("样本", justify="right")
            table.add_column("说明")
            for cand in decision.candidates:
                table.add_row(
                    cand.id,
                    f"{cand.score:.3f}",
                    f"{cand.success}/{cand.success + cand.fail}",
                    cand.detail,
                )
            console.print(table, no_wrap=False)

    runs = Table(title="执行", header_style="dim")
    for col in ("env", "adapter", "model", "status", "ms", "artifacts"):
        runs.add_column(col)
    for run in result.runs:
        mark = {"ok": "green", "degraded": "yellow", "failed": "red"}.get(run.status, "white")
        runs.add_row(
            run.env_id,
            run.adapter,
            run.model,
            f"[{mark}]{run.status}[/{mark}]",
            str(run.latency_ms),
            ", ".join(a.name for a in run.artifacts) or "—",
        )
    if result.runs:
        console.print(runs)

    if result.error:
        console.print(f"[red]error:[/red] {result.error}")


def artifacts_view(result: TaskResult, console: Console) -> None:
    if not result.artifacts:
        return
    table = Table(title="产物", header_style="dim")
    for col in ("name", "sha256", "bytes", "path"):
        table.add_column(col)
    for art in result.artifacts:
        table.add_row(art.name, art.sha256[:12], str(art.bytes), art.path)
    console.print(table)


def chain_view(chain: dict[str, Any], console: Console) -> None:
    task = chain.get("task") or {}
    console.print(
        Panel(
            json.dumps(task, ensure_ascii=False, indent=2),
            title="task",
            border_style="cyan",
        )
    )
    for decision in chain.get("decisions", []):
        console.print(
            f"[bold]{decision['stage']}[/bold] ({decision['strategy']}) → {decision['chosen']}"
        )
        console.print(f"  [dim]{decision['reason']}[/dim]")
        for cand in decision["candidates"]:
            console.print(f"    · {cand['id']}  {cand['score']:.3f}  n={cand['success'] + cand['fail']}")
    for run in chain.get("runs", []):
        console.print(f"[green]run[/green] {run['env_id']} {run['status']} {run['latency_ms']}ms")
    for art in chain.get("artifacts", []):
        console.print(f"[blue]artifact[/blue] {art['name']} {art['sha256'][:12]} {art['path']}")


def envs_table(registry: EnvRegistry, console: Console) -> None:
    table = Table(title="领域环境", header_style="dim")
    for col in ("id", "domain", "tags", "capabilities", "agent", "models", "prior", "warm"):
        table.add_column(col)
    for env in registry.all():
        caps = ", ".join(f"{c.id}@{c.version}" for c in env.capabilities)
        table.add_row(
            env.id,
            env.domain,
            ", ".join(env.tags),
            caps,
            env.agent.adapter,
            ", ".join(env.agent.models),
            f"{env.prior:.1f}",
            "✓" if env.resources.warm else "",
        )
    console.print(table)


def caps_table(pool: Pool, console: Console) -> None:
    table = Table(title="能力池", header_style="dim")
    for col in ("id", "kind", "title", "versions", "transport"):
        table.add_column(col)
    for cap in pool.all():
        table.add_row(
            cap.id,
            cap.kind,
            cap.title,
            ", ".join(v.version for v in cap.versions),
            ", ".join(sorted({v.transport for v in cap.versions})),
        )
    console.print(table)


def stats_panel(stats: dict[str, Any], perf: list[dict[str, Any]], console: Console) -> None:
    console.print(
        Panel(
            f"tasks={stats['tasks']}  ok={stats['tasks_ok']}  runs={stats['runs']}  "
            f"failed_runs={stats['runs_failed']}  artifacts={stats['artifacts']}\n"
            f"执行成功率={stats['success_rate']:.1%}（只看运行状态）  "
            f"人工验收={stats.get('rated', 0)} 条，其中差评 {stats.get('rated_bad', 0)} 条",
            title="账本",
            border_style="cyan",
        )
    )
    if not perf:
        console.print("[dim]还没有足量样本，路由目前完全靠先验。[/dim]")
        return
    table = Table(title="按 (env, adapter, model) 聚合", header_style="dim")
    for col in ("env", "adapter", "model", "n", "wins", "rate", "avg_ms"):
        table.add_column(col)
    for row in perf:
        table.add_row(
            row["env_id"],
            row["adapter"],
            row["model"],
            str(row["n"]),
            str(row["wins"]),
            f"{row['rate']:.1%}",
            f"{row['avg_ms']:.0f}",
        )
    console.print(table)
