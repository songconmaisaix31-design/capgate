"""人工 pin：把「任务类型 → {env, model}」写回 router.yaml。

pin 是路由里优先级最高的一层（线上救火、A/B 基线），但只有落回
``router.yaml`` 才扛得住重启。这个模块只管读写 ``router.yaml`` 里的
``pins`` 这一节：不动其它字段、不丢注释、不引入新依赖。

运行中的 ``Router`` 是否立即生效，由调用方（server 装配处）负责——这里只保证
磁盘上的真源正确。写回采用「只替换 pins 节」的方式，``pins`` 约定为
``router.yaml`` 的最后一个顶层键，其之前的所有内容（含其它字段与注释）原样保留。
"""

from __future__ import annotations

from pathlib import Path

import yaml


class PinError(Exception):
    """pin 操作失败，供调用方翻译成 HTTP 错误。"""


def load_pins(root: Path) -> dict[str, dict[str, str]]:
    """读 router.yaml 的 pins 节；文件或字段缺失时返回空 dict。"""
    path = root / "router.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    pins = raw.get("pins")
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

    已存在的 pin 只更新指定的键，不会把另一边冲掉：先 pin env 再 pin model，
    结果同时保留两者。
    """
    task_type = task_type.strip()
    if not task_type:
        raise PinError("task_type 不能为空")
    if not env and not model:
        raise PinError("pin 至少要指定 env 或 model 之一")
    pins = load_pins(root)
    entry = dict(pins.get(task_type) or {})
    if env:
        entry["env"] = env.strip()
    if model:
        entry["model"] = model.strip()
    pins[task_type] = entry
    _write_pins(root / "router.yaml", pins)
    return pins


def remove_pin(
    root: Path,
    task_type: str,
    *,
    key: str | None = None,
) -> dict[str, dict[str, str]]:
    """解除 pin：key 省略则整类解除，否则只删 env 或 model 那一项。"""
    pins = load_pins(root)
    if task_type not in pins:
        return pins
    if key is None:
        del pins[task_type]
    else:
        entry = dict(pins[task_type])
        entry.pop(key, None)
        if entry:
            pins[task_type] = entry
        else:
            del pins[task_type]
    _write_pins(root / "router.yaml", pins)
    return pins


def _write_pins(path: Path, pins: dict[str, dict[str, str]]) -> None:
    """把 pins 写回 router.yaml，只替换 pins 节，其余内容原样保留。"""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    idx = _pins_line_index(lines)

    rendered = yaml.safe_dump({"pins": pins}, sort_keys=False, allow_unicode=True).rstrip("\n")

    if idx is None:
        comment = "# 人工 pin：任务类型 -> {env, model}。优先级最高，用于线上救火与 A/B 基线。"
        head = lines + ([""] if lines and lines[-1] != "" else [])
        new_lines = [*head, comment, rendered]
    else:
        head = lines[:idx]
        new_lines = [*head, rendered]

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _pins_line_index(lines: list[str]) -> int | None:
    """定位顶层 ``pins:`` 键所在行；不存在返回 None。"""
    for i, line in enumerate(lines):
        if line.startswith("pins:") or line == "pins":
            return i
    return None
