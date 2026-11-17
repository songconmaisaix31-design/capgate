from __future__ import annotations

import pytest

from capgate.taxonomy import (
    Taxonomy,
    TaxonomyError,
    Term,
    overlapping_pairs,
)


@pytest.fixture
def tax(repo_root) -> Taxonomy:
    return Taxonomy.load(repo_root / "taxonomy.yaml")


def test_load_reads_version_and_terms(tax):
    assert tax.version == "1"
    assert set(tax.task_types()) == {
        "爬虫",
        "汇总",
        "链上",
        "EVM",
        "地址",
        "余额",
        "环境",
        "文档",
        "演示链",
        "代码修改",
        "注意力",
    }


def test_alias_normalizes_to_canonical(tax):
    # issue #9 的核心例子：三种写法收敛到同一个标准词
    assert tax.normalize("抓取") == "爬虫"
    assert tax.normalize("采集") == "爬虫"
    assert tax.normalize("爬虫") == "爬虫"
    assert tax.normalize("分析") == "汇总"
    assert tax.normalize("问答") == "文档"


def test_normalize_is_case_insensitive(tax):
    assert tax.normalize("evm") == "EVM"
    assert tax.normalize("Evm") == "EVM"
    assert tax.normalize("EVM") == "EVM"


def test_normalize_tags_dedup_and_preserves_order(tax):
    assert tax.normalize_tags(["抓取", "爬虫", "采集", "余额"]) == ["爬虫", "余额"]
    assert tax.normalize_tags(["文档", "问答", "检索"]) == ["文档"]


def test_unknown_tag_passes_through_by_default(tax):
    assert tax.normalize("还没有的词") == "还没有的词"
    assert not tax.is_known("还没有的词")


def test_strict_mode_rejects_unknown(tax):
    with pytest.raises(TaxonomyError):
        tax.normalize_tags(["抓取", "还没有的词"], strict=True)


def test_task_types_are_finite_and_enumerable(tax):
    # 验收：task_type 取值集合有限、可枚举
    assert tax.task_types() == list(dict.fromkeys(tax.task_types()))
    assert all(isinstance(t, str) for t in tax.task_types())


def test_every_registered_env_tag_is_covered(tax, repo_root):
    """词表必须覆盖所有 env 当前声明的 tag，否则路由会悄悄漏掉。"""
    from capgate.envs import EnvRegistry

    registry = EnvRegistry(repo_root / "envs").load()
    for env in registry.all():
        for tag in env.tags:
            assert tax.is_known(tag), f"env {env.id} 的 tag {tag!r} 不在词表里"


def test_term_metadata_is_exposed(tax):
    term = tax.get("爬虫")
    assert term is not None
    assert term.owner == "数据采集"
    assert "抓取" in term.aliases
    assert tax.get("不存在") is None


def test_pin_overrides_alias_table(tax):
    # 人工把"分析"钉到"文档"，即使词表里"分析"是"汇总"的别名
    tax.pin("分析", "文档")
    assert tax.normalize("分析") == "文档"
    assert tax.normalize("汇总") == "汇总"  # 标准词自身不受影响
    tax.unpin("分析")
    assert tax.normalize("分析") == "汇总"


def test_pin_target_must_be_canonical():
    tax = Taxonomy(terms=[Term(id="爬虫", aliases=("抓取",))])
    with pytest.raises(TaxonomyError):
        tax.pin("抓取", "不存在的标准词")


def test_duplicate_id_raises():
    with pytest.raises(TaxonomyError):
        Taxonomy(terms=[Term(id="爬虫"), Term(id="爬虫")])


def test_alias_collision_raises():
    with pytest.raises(TaxonomyError):
        Taxonomy(
            terms=[
                Term(id="爬虫", aliases=("抓取",)),
                Term(id="汇总", aliases=("抓取",)),
            ]
        )


def test_version_change_does_not_rewrite_old_snapshot():
    # 验收：词表变更走版本号，旧实例保持旧映射，历史 task_type 不被追溯改写
    v1 = Taxonomy(terms=[Term(id="爬虫", aliases=("抓取",))], version="1")
    v2 = Taxonomy(terms=[Term(id="采集", aliases=("抓取",))], version="2")
    assert v1.normalize("抓取") == "爬虫"
    assert v2.normalize("抓取") == "采集"
    assert v1.version == "1" and v2.version == "2"


def test_empty_taxonomy_passes_everything_through():
    empty = Taxonomy.empty()
    assert empty.normalize("任意词") == "任意词"
    assert empty.task_types() == []


class _Env:
    def __init__(self, id, tags):
        self.id = id
        self.tags = tags


def test_overlapping_pairs_flags_shared_tags():
    tax = Taxonomy(terms=[Term(id="爬虫", aliases=("抓取", "采集")), Term(id="余额")])
    envs = [
        _Env("crawler", ["爬虫", "抓取"]),
        _Env("spider", ["采集"]),
        _Env("balance", ["余额"]),
    ]
    pairs = overlapping_pairs(envs, tax)
    assert [(p.a, p.b) for p in pairs] == [("crawler", "spider")]
    assert pairs[0].shared == ("爬虫",)
    assert pairs[0].jaccard == 1.0


def test_overlapping_pairs_respects_threshold():
    tax = Taxonomy(terms=[Term(id="爬虫", aliases=("抓取",)), Term(id="余额")])
    envs = [_Env("a", ["爬虫", "余额"]), _Env("b", ["爬虫"])]
    # a 与 b 只共享"爬虫"，Jaccard = 1/2 = 0.5
    assert len(overlapping_pairs(envs, tax, threshold=0.6)) == 0
    assert len(overlapping_pairs(envs, tax, threshold=0.5)) == 1
