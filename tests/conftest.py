from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from capgate.router import RouterConfig
from capgate.system import build

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture
async def system(tmp_path):
    s = build(ROOT, RouterConfig(strategy="static"), state=tmp_path / "state")
    try:
        yield s
    finally:
        await s.aclose()
