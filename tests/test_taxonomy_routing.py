from __future__ import annotations

import random

from capgate.envs import EnvRegistry
from capgate.ledger import Ledger
from capgate.models import TaskSpec
from capgate.router import Router, RouterConfig
from capgate.taxonomy import Taxonomy


def test_alias_routes_to_same_env_as_canonical(repo_root):
    """验收 #1：别名输入（"抓取"）能路由到与标准词（"爬虫"）相同的 env。"""
    tax = Taxonomy.load(repo_root / "taxonomy.yaml")
    registry = EnvRegistry(repo_root / "envs").load()

    assert tax.normalize("抓取") == "爬虫"

    by_alias = registry.by_tag(["抓取"])
    by_canonical = registry.by_tag(["爬虫"])
    assert by_alias == by_canonical
    assert [e.id for e in by_alias] == ["crawler"]


def test_normalized_task_type_unifies_samples(repo_root):
    """验收 #2：不同写法收敛到同一个 task_type，学习单位不再被劈开。"""
    tax = Taxonomy.load(repo_root / "taxonomy.yaml")
    by_alias = TaskSpec(goal="g", tags=tax.normalize_tags(["抓取"]))
    by_canonical = TaskSpec(goal="g", tags=tax.normalize_tags(["爬虫"]))
    assert by_alias.task_type == "爬虫"
    assert by_canonical.task_type == "爬虫"
    assert by_alias.task_type == by_canonical.task_type


def test_alias_and_canonical_choose_same_env_via_router(repo_root, tmp_path):
    """端到端：规范化后走路由，别名与标准词选中同一个 env。"""
    tax = Taxonomy.load(repo_root / "taxonomy.yaml")
    registry = EnvRegistry(repo_root / "envs").load()
    ledger = Ledger(tmp_path / "l.db")
    router = Router(ledger, registry, RouterConfig(strategy="static"), rng=random.Random(1))

    def route(tags: list[str]) -> list[str]:
        task = TaskSpec(goal="g", tags=tax.normalize_tags(tags))
        return router.route_domains(task).chosen

    assert route(["抓取"]) == route(["爬虫"]) == ["crawler"]
    ledger.close()
