"""人工 pin：把「任务类型 → {env, model}」写回 router.yaml。

pin 是路由里优先级最高的一层（线上救火、A/B 基线），但只有落回
``router.yaml`` 才扛得住重启。本模块用 ``ruamel.yaml`` 的 round-trip 模式读写
``router.yaml``：完整载入 → 改 ``pins`` → 完整保存，注释、其它字段、以及已有的
model pin 原样保留，不手写 YAML 解析。

写回走「同目录临时文件 + 原子替换」，写到一半进程被中断也不会留下半截文件。

依赖：``ruamel.yaml``（PyYAML 的 safe_load/safe_dump 会丢注释，round-trip 是
保留注释的唯一成熟做法）。本模块不碰依赖清单，由集成侧统一加入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


class PinError(Exception):
    """pin 操作失败，供调用方翻译成 HTTP 错误。"""


def load_pins(root: Path) -> dict[str, dict[str, str]]:
    """读 router.yaml 的 pins 节；文件或字段缺失时返回空 dict。"""
    data = _load(root / "router.yaml")
    pins = data.get("pins")
    if pins is None:
        return {}
    if not isinstance(pins, dict):
        raise PinError("router.yaml 的 pins 必须是「任务类型 → {env, model}」的映射")
    result: dict[str, dict[str, str]] = {}
    for task_type, entry in pins.items():
        if not isinstance(entry, dict):
            raise PinError(
                f"pins.{task_type!r} 必须是 {{env, model}} 映射，不是 {type(entry).__name__}"
            )
        result[str(task_type)] = {str(k): str(v) for k, v in entry.items()}
    return result


def set_pin(
    root: Path,
    task_type: str,
    *,
    env: str | None = None,
    model: str | None = None,
) -> dict[str, dict[str, str]]:
    """给某个任务类型设置 env / model pin（至少其一），写回并返回全部 pins。

    已存在的 pin 只更新指定的键：先 pin env 再 pin model，两者并存。
    """
    task_type = task_type.strip()
    if not task_type:
        raise PinError("task_type 不能为空")
    if not env and not model:
        raise PinError("pin 至少要指定 env 或 model 之一")

    path = root / "router.yaml"
    data = _load(path)
    pins = _ensure_pins(data)
    entry = pins.get(task_type)
    if entry is None:
        entry = {}
        pins[task_type] = entry
    elif not isinstance(entry, dict):
        raise PinError(
            f"pins.{task_type!r} 必须是 {{env, model}} 映射，不是 {type(entry).__name__}"
        )
    if env:
        entry["env"] = env.strip()
    if model:
        entry["model"] = model.strip()

    _dump(path, data)
    return load_pins(root)


def remove_pin(
    root: Path,
    task_type: str,
    *,
    key: str | None = None,
) -> dict[str, dict[str, str]]:
    """解除 pin：key 省略则整类解除，否则只删 env 或 model 那一项。"""
    path = root / "router.yaml"
    if not path.exists():
        return {}
    data = _load(path)
    pins = _ensure_pins(data)
    if task_type in pins:
        if key is None:
            del pins[task_type]
        else:
            entry = pins[task_type]
            if isinstance(entry, dict) and key in entry:
                del entry[key]
                if not entry:
                    del pins[task_type]
        _dump(path, data)
    return load_pins(root)


def _load(path: Path) -> Any:
    """round-trip 载入；显式 UTF-8，因为 ruamel 直接吃 Path 会用系统默认编码。"""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return YAML(typ="rt").load(fh) or {}


def _ensure_pins(data: Any) -> Any:
    """取回 pins 映射；缺失则新建并挂回 data（已有 pin 的注释与排版继续保留）。"""
    pins = data.get("pins")
    if pins is None:
        pins = {}
        data["pins"] = pins
        return pins
    if not isinstance(pins, dict):
        raise PinError("router.yaml 的 pins 必须是「任务类型 → {env, model}」的映射")
    return pins


def _dump(path: Path, data: Any) -> None:
    """同目录临时文件 + 原子替换：写盘中断不会留下半截 router.yaml。"""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        YAML(typ="rt").dump(data, fh)
    tmp.replace(path)
