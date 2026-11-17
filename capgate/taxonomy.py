"""受控词表：tag 的规范名、别名、版本与人工 pin。

解决的问题（issue #9）：tag 由人随手写，"爬虫 / 抓取 / 采集"会被当成三个不同
的 ``task_type``，历史样本被劈成几份，Thompson 后验永远攒不够。本模块提供一层
"别名 → 标准词"的规范化，任务入库前调用一次，账本只存标准词。

设计约束：

- **纯数据 + 纯函数**：本模块不依赖账本 / 路由 / 编排，只依赖 ``yaml`` 与标准库，
  可独立测试。接入方（system / orchestrator / cli / server）只需 ``Taxonomy.load(...)``
  一次，之后用 ``normalize`` / ``normalize_tags`` / ``task_types`` 三个入口。
- **词表带版本号**：``version`` 每次变更必须递增；历史任务已固化的 ``task_type``
  不追溯改写（规范化只在入库那一刻发生，账本 append-only）。旧 ``Taxonomy`` 实例
  与新实例互不干扰，各自保留自己的映射快照。
- **人工 pin 优先级最高**：``normalize`` 先查 pin，再查别名表；pin 永远不被别名表
  覆盖，也不会被自动改写。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Term:
    """词表里的一条标准词。``id`` 是规范化后的 task_type 取值。"""

    id: str
    aliases: tuple[str, ...] = ()
    desc: str = ""
    owner: str = ""


@dataclass(frozen=True)
class TagOverlap:
    """两个 env 之间的 tag 重叠报告，供 ``capgate envs`` 提示用。"""

    a: str
    b: str
    shared: tuple[str, ...]  # 规范化后共同的标准词
    jaccard: float


class TaxonomyError(ValueError):
    """词表文件本身的问题：重复 id、别名冲突、非法 pin 等。"""


class Taxonomy:
    """受控词表容器。

    只读的词条（terms）+ 可变的 pin 层。规范化逻辑是无副作用的：同一个实例对
    同一个输入永远返回同一个结果，直到有人显式 ``pin`` / ``unpin``。
    """

    def __init__(
        self,
        terms: Iterable[Term | dict[str, Any]],
        version: str | int = "0",
        pins: dict[str, str] | None = None,
    ) -> None:
        self.version = str(version)
        self._terms: dict[str, Term] = {}  # 标准词 id -> Term（保持定义顺序）
        self._by_lower: dict[str, Term] = {}  # lower(id) -> Term
        self._index: dict[str, str] = {}  # lower(别名或标准词) -> 标准词 id
        self._pins: dict[str, str] = {}  # lower(输入) -> 标准词 id

        for raw in terms:
            term = raw if isinstance(raw, Term) else _coerce(raw)
            tid_lower = term.id.strip().lower()
            if not tid_lower:
                raise TaxonomyError("标准词 id 不能为空")
            if tid_lower in self._by_lower:
                raise TaxonomyError(f"标准词 id 重复：{term.id!r}")
            self._terms[term.id] = term
            self._by_lower[tid_lower] = term
            self._register(term.id, term.id)
            for alias in term.aliases:
                self._register(alias, term.id)

        for alias, canonical in (pins or {}).items():
            target = self._resolve_id(canonical)
            if target is None:
                raise TaxonomyError(
                    f"pin 目标 {canonical!r} 不是词表里的标准词（已登记：{list(self._terms)}）"
                )
            self._pins[alias.strip().lower()] = target

    # ---------------------------------------------------------------- 装载

    @classmethod
    def load(cls, path: Path | str) -> Taxonomy:
        """从 ``taxonomy.yaml`` 装载。文件缺失或损坏都抛 :class:`TaxonomyError`。"""
        path = Path(path)
        if not path.exists():
            raise TaxonomyError(f"词表文件不存在：{path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise TaxonomyError(f"词表文件解析失败：{path}：{exc}") from exc
        if not isinstance(raw, dict):
            raise TaxonomyError(f"词表文件顶层必须是映射：{path}")
        return cls(
            terms=raw.get("terms") or [],
            version=raw.get("version", "0"),
            pins=raw.get("pins") or {},
        )

    @classmethod
    def empty(cls) -> Taxonomy:
        """空词表兜底：所有 tag 原样通过，``task_types()`` 为空。

        只应在"确实没有词表"时使用——静默空词表等于关闭规范化，正是 issue #9
        要治的病。
        """
        return cls(terms=[], version="0")

    # ---------------------------------------------------------------- 规范化

    def normalize(self, tag: str) -> str:
        """单个 tag → 标准词。

        查找顺序：人工 pin → 标准词自身 → 别名。未知 tag 原样返回（不抛错），
        由 ``strict=True`` 的批量入口决定要不要拒绝。
        """
        t = tag.strip()
        if not t:
            return ""
        key = t.lower()
        if key in self._pins:
            return self._pins[key]
        return self._index.get(key, t)

    def normalize_tags(self, tags: Iterable[str], *, strict: bool = False) -> list[str]:
        """批量规范化 + 去重，保持首次出现顺序；空 tag 被丢弃。

        ``strict=True`` 时遇到词表里没有的 tag 抛 :class:`TaxonomyError`，
        用于入库时把 ``task_type`` 锁死在可枚举集合里。
        """
        out: list[str] = []
        seen: set[str] = set()
        for tag in tags:
            canonical = self.normalize(tag)
            if not canonical:
                continue
            if strict and not self.is_canonical(canonical):
                raise TaxonomyError(f"未知 tag {tag!r}，不在词表里")
            if canonical not in seen:
                seen.add(canonical)
                out.append(canonical)
        return out

    # ---------------------------------------------------------------- 查询

    def task_types(self) -> list[str]:
        """``task_type`` 的合法取值：有限、可枚举，按词表定义顺序返回。"""
        return list(self._terms)

    def terms(self) -> list[Term]:
        """全部词条，按定义顺序。"""
        return list(self._terms.values())

    def get(self, term_id: str) -> Term | None:
        """按 id（大小写不敏感）取词条。"""
        return self._by_lower.get(term_id.strip().lower())

    def aliases_of(self, term_id: str) -> list[str]:
        """某个标准词的别名列表。"""
        term = self.get(term_id)
        return list(term.aliases) if term else []

    def is_canonical(self, tag: str) -> bool:
        """``tag`` 是否就是某个标准词（大小写不敏感）。"""
        return tag.strip().lower() in self._by_lower

    def is_known(self, tag: str) -> bool:
        """``tag`` 是否落在词表内（标准词、别名或 pin 都算）。"""
        return self.is_canonical(self.normalize(tag))

    # ---------------------------------------------------------------- pin

    @property
    def pins(self) -> dict[str, str]:
        """当前 pin 映射的副本（未 lowercase 的原始键值）。"""
        return dict(self._pins)

    def pin(self, alias: str, canonical: str) -> None:
        """人工钉死一个映射，优先级高于别名表。目标必须是标准词。"""
        target = self._resolve_id(canonical)
        if target is None:
            raise TaxonomyError(f"pin 目标 {canonical!r} 不是词表里的标准词")
        self._pins[alias.strip().lower()] = target

    def unpin(self, alias: str) -> None:
        """取消某个人工 pin。"""
        self._pins.pop(alias.strip().lower(), None)

    # ---------------------------------------------------------------- 内部

    def _register(self, surface: str, canonical: str) -> None:
        key = surface.strip().lower()
        if not key:
            return
        prev = self._index.get(key)
        if prev is not None and prev != canonical:
            raise TaxonomyError(f"词表冲突：{surface!r} 同时映射到 {prev!r} 和 {canonical!r}")
        self._index[key] = canonical

    def _resolve_id(self, canonical: str) -> str | None:
        term = self._by_lower.get(canonical.strip().lower())
        return term.id if term else None


def _coerce(raw: dict[str, Any]) -> Term:
    return Term(
        id=str(raw.get("id", "")),
        aliases=tuple(str(a) for a in (raw.get("aliases") or ())),
        desc=str(raw.get("desc") or ""),
        owner=str(raw.get("owner") or ""),
    )


# ---------------------------------------------------------------- env 重叠提示


def overlapping_pairs(
    envs: Iterable[Any],
    taxonomy: Taxonomy,
    *,
    threshold: float = 0.0,
) -> list[TagOverlap]:
    """找出 tag 高度重叠的 env 对，供 ``capgate envs`` 提示"这两个 env 会抢路由"。

    ``envs`` 是任意带 ``id`` 与 ``tags`` 属性的对象（如 :class:`~capgate.models.EnvManifest`）。
    先规范化到标准词，再按 Jaccard 相似度比较；只返回至少共享一个标准词、且
    Jaccard ``>= threshold`` 的对，按相似度降序排列。
    """
    entries: list[tuple[str, set[str]]] = []
    for env in envs:
        tags = set(taxonomy.normalize_tags(getattr(env, "tags", [])))
        entries.append((str(getattr(env, "id", "?")), tags))

    out: list[TagOverlap] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a_id, a = entries[i]
            b_id, b = entries[j]
            shared = a & b
            if not shared:
                continue
            union = a | b
            jaccard = len(shared) / len(union) if union else 0.0
            if jaccard >= threshold:
                out.append(TagOverlap(a_id, b_id, tuple(sorted(shared)), round(jaccard, 4)))
    out.sort(key=lambda o: (-o.jaccard, o.a, o.b))
    return out


__all__ = [
    "TagOverlap",
    "Taxonomy",
    "TaxonomyError",
    "Term",
    "overlapping_pairs",
]
