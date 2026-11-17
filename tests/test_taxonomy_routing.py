from __future__ import annotations

import random

from capgate.envs import EnvRegistry
from capgate.ledger import Ledger
from capgate.models import TaskSpec
from capgate.router import Router, RouterConfig
from capgate.taxonomy import Taxonomy


def _taxonomy_with_envs(repo_root, registry) -> Taxonomy:
    """按集成代码的真实形态装载：根词表 + 环境 tag 自动登记。"""
    return Taxonomy.load(repo_root / "taxonomy.yaml", env_tags=registry.all())


def test_alias_routes_to_same_env_as_canonical(repo_root):
    """验收 #1：别名输入（"抓取"）能路由到与标准词（"爬虫"）相同的 env。"""
    registry = EnvRegistry(repo_root / "envs").load()
    tax = _taxonomy_with_envs(repo_root, registry)

    assert tax.normalize("抓取") == "爬虫"

    by_alias = registry.by_tag(["抓取"])
    by_canonical = registry.by_tag(["爬虫"])
    assert by_alias == by_canonical
    assert [e.id for e in by_alias] == ["crawler"]


def test_normalized_task_type_unifies_samples(repo_root):
    """验收 #2：不同写法收敛到同一个 task_type，学习单位不再被劈开。"""
    registry = EnvRegistry(repo_root / "envs").load()
    tax = _taxonomy_with_envs(repo_root, registry)
    by_alias = TaskSpec(goal="g", tags=tax.normalize_tags(["抓取"]))
    by_canonical = TaskSpec(goal="g", tags=tax.normalize_tags(["爬虫"]))
    assert by_alias.task_type == "爬虫"
    assert by_canonical.task_type == "爬虫"
    assert by_alias.task_type == by_canonical.task_type


def test_alias_and_canonical_choose_same_env_via_router(repo_root, tmp_path):
    """端到端：规范化后走路由，别名与标准词选中同一个 env。"""
    registry = EnvRegistry(repo_root / "envs").load()
    tax = _taxonomy_with_envs(repo_root, registry)
    ledger = Ledger(tmp_path / "l.db")
    router = Router(ledger, registry, RouterConfig(strategy="static"), rng=random.Random(1))

    def route(tags: list[str]) -> list[str]:
        task = TaskSpec(goal="g", tags=tax.normalize_tags(tags))
        return router.route_domains(task).chosen

    assert route(["抓取"]) == route(["爬虫"]) == ["crawler"]
    ledger.close()


def test_new_env_yaml_tag_routes_to_its_env(repo_root, tmp_path):
    """新增业务域只写 env YAML：新 tag 自动登记为标准词，并能路由到新 env。"""
    (tmp_path / "new-domain.yaml").write_text(
        "id: new-crawler\ndomain: 新采集\ntags: [新爬取]\nprior: 1.0\n",
        encoding="utf-8",
    )
    registry = EnvRegistry(tmp_path).load()
    tax = _taxonomy_with_envs(repo_root, registry)

    assert tax.normalize("新爬取") == "新爬取"

    ledger = Ledger(tmp_path / "l.db")
    router = Router(ledger, registry, RouterConfig(strategy="static"), rng=random.Random(1))
    task = TaskSpec(goal="g", tags=tax.normalize_tags(["新爬取"]))
    decision = router.route_domains(task)
    assert decision.chosen == ["new-crawler"]
    assert task.task_type == "新爬取"
    ledger.close()
