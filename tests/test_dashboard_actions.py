"""看板操作（人工验收 + 按任务类型 pin）的测试。

pin 的真源是 router.yaml，本文件重点覆盖 pins 模块的读写、幂等、注释保留，
以及「写回 → load_router_config → Router 决策」这条闭环——对应验收标准
"pin 之后同一 task_type 的决策链显示 pin"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capgate.pins import PinError, load_pins, remove_pin, set_pin

ROUTER = """\
# 路由策略
strategy: thompson
epsilon: 0.1

# 人工 pin：任务类型 -> {env, model}。优先级最高，用于线上救火与 A/B 基线。
pins: {}
"""


def _write(root: Path, text: str = ROUTER) -> Path:
    path = root / "router.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_pins_missing_file_returns_empty(tmp_path):
    assert load_pins(tmp_path) == {}


def test_load_pins_empty(tmp_path):
    _write(tmp_path)
    assert load_pins(tmp_path) == {}


def test_set_pin_writes_and_roundtrips(tmp_path):
    _write(tmp_path)
    pins = set_pin(tmp_path, "crawl", env="crawler")
    assert pins == {"crawl": {"env": "crawler"}}
    assert load_pins(tmp_path) == {"crawl": {"env": "crawler"}}


def test_set_pin_merges_env_then_model(tmp_path):
    _write(tmp_path)
    set_pin(tmp_path, "crawl", env="crawler")
    set_pin(tmp_path, "crawl", model="m1")
    assert load_pins(tmp_path) == {"crawl": {"env": "crawler", "model": "m1"}}


def test_set_pin_preserves_other_fields_and_comments(tmp_path):
    path = _write(tmp_path)
    set_pin(tmp_path, "crawl", env="crawler")
    text = path.read_text(encoding="utf-8")
    assert "strategy: thompson" in text
    assert "epsilon: 0.1" in text
    assert "人工 pin" in text


def test_set_pin_keeps_fields_after_pins(tmp_path):
    """round-trip 不应要求 pins 是最后一个键：其后的字段也要原样保留。"""
    _write(tmp_path, "strategy: thompson\npins: {}\nllm_fallback: false\n# 尾注\n")
    set_pin(tmp_path, "crawl", env="crawler")
    text = (tmp_path / "router.yaml").read_text(encoding="utf-8")
    assert "llm_fallback: false" in text
    assert "尾注" in text
    assert load_pins(tmp_path) == {"crawl": {"env": "crawler"}}


def test_set_pin_overwrites_existing_env(tmp_path):
    _write(tmp_path)
    set_pin(tmp_path, "crawl", env="crawler")
    set_pin(tmp_path, "crawl", env="analyst")
    assert load_pins(tmp_path) == {"crawl": {"env": "analyst"}}


def test_set_pin_requires_env_or_model(tmp_path):
    _write(tmp_path)
    with pytest.raises(PinError, match="env 或 model"):
        set_pin(tmp_path, "crawl")


def test_set_pin_rejects_blank_task_type(tmp_path):
    _write(tmp_path)
    with pytest.raises(PinError, match="task_type"):
        set_pin(tmp_path, "   ", env="crawler")


def test_set_pin_model_only(tmp_path):
    _write(tmp_path)
    set_pin(tmp_path, "crawl", model="m2")
    assert load_pins(tmp_path) == {"crawl": {"model": "m2"}}


def test_remove_pin_whole_type(tmp_path):
    _write(tmp_path)
    set_pin(tmp_path, "crawl", env="crawler", model="m1")
    pins = remove_pin(tmp_path, "crawl")
    assert pins == {}
    assert load_pins(tmp_path) == {}


def test_remove_pin_single_key_keeps_the_other(tmp_path):
    _write(tmp_path)
    set_pin(tmp_path, "crawl", env="crawler", model="m1")
    remove_pin(tmp_path, "crawl", key="env")
    assert load_pins(tmp_path) == {"crawl": {"model": "m1"}}


def test_remove_pin_missing_type_is_noop(tmp_path):
    _write(tmp_path)
    set_pin(tmp_path, "crawl", env="crawler")
    pins = remove_pin(tmp_path, "doc")
    assert pins == {"crawl": {"env": "crawler"}}
    assert load_pins(tmp_path) == {"crawl": {"env": "crawler"}}


def test_unicode_task_type_roundtrips(tmp_path):
    _write(tmp_path)
    set_pin(tmp_path, "爬虫", env="crawler")
    assert load_pins(tmp_path) == {"爬虫": {"env": "crawler"}}


def test_load_pins_invalid_shape_raises(tmp_path):
    _write(tmp_path, "strategy: thompson\npins: [1, 2]\n")
    with pytest.raises(PinError, match="pins"):
        load_pins(tmp_path)


def test_load_pins_ignores_none_value(tmp_path):
    _write(tmp_path, "strategy: thompson\npins:\n")
    assert load_pins(tmp_path) == {}


def test_pin_roundtrip_drives_router_decision(tmp_path):
    """验收标准：pin 之后同一 task_type 的领域决策链显示 strategy=pin。"""
    from capgate.envs import EnvRegistry
    from capgate.ledger import Ledger
    from capgate.models import EnvManifest, TaskSpec
    from capgate.router import Router
    from capgate.system import load_router_config

    _write(tmp_path)
    set_pin(tmp_path, "crawl", env="crawler")
    cfg = load_router_config(tmp_path)
    assert cfg.pins == {"crawl": {"env": "crawler"}}

    reg = EnvRegistry(None)
    reg.envs = {"crawler": EnvManifest(id="crawler", domain="d", tags=["crawl"])}
    led = Ledger(tmp_path / "l.db")
    router = Router(led, reg, cfg)
    decision = router.route_domains(TaskSpec(goal="g", tags=["crawl"]))
    assert decision.strategy == "pin"
    assert decision.chosen == ["crawler"]
    led.close()


def test_dashboard_html_wires_the_action_endpoints():
    """看板与 server 端点的契约：按钮齐、端点名对得上，防止两路 Agent 各写各的。"""
    html = (
        Path(__file__).resolve().parents[1] / "capgate" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert "验收：好" in html and "验收：差" in html
    assert "pin 当前 env" in html
    assert "重跑" in html
    assert "/api/tasks/" in html and "/outcome" in html
    assert "/api/pins" in html
    assert "/rerun" in html
