"""路径与运行时配置。

一切落在 ``CAPGATE_HOME``（默认当前目录）下的 ``var/``：

    var/capgate.db      SQLite 账本（append-only 事件 + 聚合）
    var/artifacts/      内容寻址产物库
    var/logs/           运行日志

env / capability 的声明走仓库根的 ``envs/`` 与 ``capabilities/``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL_S = 900


def home() -> Path:
    return Path(os.environ.get("CAPGATE_HOME", Path.cwd())).resolve()


@dataclass(frozen=True)
class Paths:
    root: Path
    state: Path | None = None

    @property
    def var(self) -> Path:
        return self.state or (self.root / "var")

    @property
    def db(self) -> Path:
        return self.var / "capgate.db"

    @property
    def artifacts(self) -> Path:
        return self.var / "artifacts"

    @property
    def logs(self) -> Path:
        return self.var / "logs"

    @property
    def envs(self) -> Path:
        return self.root / "envs"

    @property
    def capabilities(self) -> Path:
        return self.root / "capabilities"

    @property
    def schemas(self) -> Path:
        return self.root / "schemas"

    def ensure(self) -> "Paths":
        for d in (self.var, self.artifacts, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        return self


def paths(root: Path | str | None = None, state: Path | str | None = None) -> Paths:
    resolved_root = Path(root).resolve() if root else home()
    resolved_state = Path(state).resolve() if state else None
    return Paths(resolved_root, resolved_state).ensure()
