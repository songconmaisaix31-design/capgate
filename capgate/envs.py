"""env 清单的载入与校验。"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import EnvManifest


class EnvRegistry:
    def __init__(self, root: Path | str | None) -> None:
        self.root = Path(root) if root else None
        self.envs: dict[str, EnvManifest] = {}
        self.errors: list[str] = []

    def load(self) -> EnvRegistry:
        self.envs.clear()
        self.errors.clear()
        if not self.root or not self.root.exists():
            return self
        for path in sorted(self.root.glob("*.y*ml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                manifest = EnvManifest.model_validate(raw)
            except Exception as exc:
                self.errors.append(f"{path.name}: {exc}")
                continue
            if manifest.id in self.envs:
                self.errors.append(f"{path.name}: env id 重复 {manifest.id!r}")
                continue
            self.envs[manifest.id] = manifest
        return self

    def __contains__(self, env_id: str) -> bool:
        return env_id in self.envs

    def get(self, env_id: str) -> EnvManifest:
        try:
            return self.envs[env_id]
        except KeyError:
            raise KeyError(f"没有 env {env_id!r}（已注册：{sorted(self.envs)}）") from None

    def all(self) -> list[EnvManifest]:
        return list(self.envs.values())

    def by_tag(self, tags: list[str]) -> list[EnvManifest]:
        wanted = {t.strip().lower() for t in tags if t.strip()}
        if not wanted:
            return []
        hits = []
        for env in self.envs.values():
            if wanted & {t.lower() for t in env.tags}:
                hits.append(env)
        return hits
