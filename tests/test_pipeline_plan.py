from __future__ import annotations

from capgate.demo import build_task
from capgate.models import EnvManifest


def _ranked(system, ids: list[str]) -> list[EnvManifest]:
    return [system.registry.get(i) for i in ids]


def test_single_chain_orders_crawler_before_analyst(system):
    task = build_task("pipeline", system.paths.root)
    pipeline, reasons, _ = system.orchestrator._plan_pipeline(
        task, _ranked(system, ["crawler", "analyst"])
    )
    assert [env.id for env in pipeline] == ["crawler", "analyst"]
    assert "接在后面" in reasons["analyst"], "有依赖的 env 不能被描述成并行"
    assert "并行" not in reasons["analyst"]


def test_head_never_needs_artifacts_it_cannot_have(system):
    task = build_task("pipeline", system.paths.root)
    # analyst 排第一也当不了起点：它要 crawl_batch
    pipeline, _, _ = system.orchestrator._plan_pipeline(
        task, _ranked(system, ["analyst", "crawler"])
    )
    assert pipeline[0].id == "crawler"
    assert [env.id for env in pipeline] == ["crawler", "analyst"]


def test_branch_keeps_independent_envs_parallel(system):
    task = build_task("branch", system.paths.root)
    pipeline, _, _ = system.orchestrator._plan_pipeline(
        task, _ranked(system, ["crawler", "analyst", "chain-watch"])
    )
    ids = [env.id for env in pipeline]
    assert ids[0] == "crawler"
    assert "chain-watch" in ids, "覆盖不同 tag 且无依赖的 env 应该被并行拉进来"
    assert ids.index("analyst") > ids.index("crawler"), "有契约依赖的下游必须排在后面"


def test_unmatched_consumers_are_skipped_with_reason(system):
    task = build_task("pipeline", system.paths.root)
    pipeline, _, skipped = system.orchestrator._plan_pipeline(task, _ranked(system, ["crawler"]))
    assert [env.id for env in pipeline] == ["crawler"]
