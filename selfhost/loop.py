"""自举驱动：读 ``selfhost/tasks/*.md``，用 capgate 跑本仓库自己的质量门。

用法::

    python -m selfhost.loop                # 跑全部任务
    python -m selfhost.loop --only T01     # 只跑一条
    python -m selfhost.loop --list         # 只列任务

退出码：全部 ok 才是 0。任何一条失败都返回 1，可以直接当 CI 门禁。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

from capgate.models import TaskResult, TaskSpec
from capgate.system import build

SELF = Path(__file__).resolve().parent
ROOT = SELF.parent


def _front_matter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path.name} 缺少 front-matter")
    _, raw, _body = text.split("---", 2)
    return yaml.safe_load(raw) or {}


def load_tasks(only: list[str] | None = None) -> list[TaskSpec]:
    tasks: list[TaskSpec] = []
    for path in sorted((SELF / "tasks").glob("*.md")):
        meta = _front_matter(path)
        task_id = str(meta.get("id") or path.stem)
        if only and task_id not in only:
            continue
        tasks.append(
            TaskSpec(
                goal=str(meta.get("goal") or meta.get("title") or task_id),
                type=str(meta.get("type") or "general"),
                tags=[str(t) for t in meta.get("tags") or []],
                inputs={"task_file": path.name},
                budget_s=600,
            )
        )
    return tasks


def _print_result(result: TaskResult) -> None:
    mark = {"ok": "✓", "degraded": "!", "failed": "✗"}[result.status]
    print(f"\n{mark} {result.task.type:<8} {result.task.goal}")
    for decision in result.decisions:
        if decision.stage in ("domain", "pipeline"):
            print(f"    {decision.stage:<9} → {', '.join(decision.chosen) or '(无)'}")
    for run in result.runs:
        names = ", ".join(a.name for a in run.artifacts) or "—"
        print(f"    {run.env_id:<10} {run.status:<7} {names}")
    print(f"    status={result.status}  artifacts={len(result.artifacts)}  {result.latency_ms}ms")
    if result.error:
        print(f"    error={result.error}")


async def _run(only: list[str] | None) -> int:
    tasks = load_tasks(only)
    if not tasks:
        print("没有匹配的任务。")
        return 1

    system = build(SELF)
    print(f"selfhost · root={system.paths.root}")
    print(f"envs={len(system.registry.envs)}  capabilities={len(system.pool.all())}")
    if system.errors:
        for err in system.errors:
            print(f"!! {err}")

    failed = 0
    try:
        for spec in tasks:
            result = await system.orchestrator.run(spec)
            _print_result(result)
            if result.status != "ok":
                failed += 1
    finally:
        await system.aclose()

    print(f"\n完成 {len(tasks)} 条，失败 {failed} 条。账本：{system.paths.db}")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="selfhost.loop", description="用 capgate 构建 capgate")
    parser.add_argument("--only", nargs="*", help="只跑指定任务 id，例如 --only T01 T03")
    parser.add_argument("--list", action="store_true", help="只列出任务，不执行")
    args = parser.parse_args(argv)

    if args.list:
        for spec in load_tasks(args.only):
            print(f"{spec.type:<8} {spec.goal}   tags={spec.tags}")
        return 0

    try:
        return asyncio.run(_run(args.only))
    except KeyboardInterrupt:  # pragma: no cover - 手工中断
        return 130


if __name__ == "__main__":
    sys.exit(main())
