"""能力池：共享、可复用、版本钉死。

一条能力 = 一个 id + 若干版本，每个版本声明它怎么被拉起
（stdio 子进程 / streamable HTTP / 直接 import 的 python 函数）。

env 只声明 ``id + 版本说明符``；解析成确切版本后写进账本，
这样半年后回看一次运行，还能知道当时用的是哪个版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import Capability, CapabilityRef, CapabilityVersion


@dataclass(frozen=True)
class Resolved:
    capability: Capability
    version: CapabilityVersion
    exact: str
    ref: CapabilityRef

    @property
    def key(self) -> str:
        """连接复用键：共享能力全池一份，非共享按 env 隔离由调用方加前缀。"""
        return f"{self.capability.id}@{self.exact}"


class Pool:
    def __init__(self, root: Path | str | None) -> None:
        self.root = Path(root) if root else None
        self._caps: dict[str, Capability] = {}
        self.errors: list[str] = []

    # ---------------------------------------------------------------- 载入

    def load(self) -> "Pool":
        self._caps.clear()
        self.errors.clear()
        if not self.root or not self.root.exists():
            return self
        for path in sorted(self.root.glob("*.y*ml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                cap = Capability.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - 单个文件坏掉不影响其它能力
                self.errors.append(f"{path.name}: {exc}")
                continue
            if cap.id in self._caps:
                self.errors.append(f"{path.name}: 能力 id 重复 {cap.id!r}")
                continue
            self._caps[cap.id] = cap
        return self

    # ---------------------------------------------------------------- 查询

    def __contains__(self, cap_id: str) -> bool:
        return cap_id in self._caps

    def all(self) -> list[Capability]:
        return list(self._caps.values())

    def get(self, cap_id: str) -> Capability:
        try:
            return self._caps[cap_id]
        except KeyError:
            raise KeyError(f"能力池里没有 {cap_id!r}（已注册：{sorted(self._caps)}）") from None

    def resolve(self, ref: CapabilityRef) -> Resolved:
        cap = self.get(ref.id)
        version = cap.pick(ref.version)
        return Resolved(capability=cap, version=version, exact=version.version, ref=ref)

    def resolve_env(self, env: Any) -> dict[str, Resolved]:
        out: dict[str, Resolved] = {}
        for ref in env.capabilities:
            out[ref.id] = self.resolve(ref)
        return out
