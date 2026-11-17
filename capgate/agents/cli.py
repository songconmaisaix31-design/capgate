"""cli adapter：把任意命令行 agent 当执行器。

这是"异构"最实用的一层：codex / dsh / claude / 自家脚本，只要能吃 stdin、
吐一段 JSON，就能挂进 env。网关不关心它内部是什么框架。

config:
    command: ["codex", "exec", "-"] 或 "bash -lc '...'"
    timeout_s: 300
    cwd: 可选
    output_mode: stdout_json | file
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any

from .base import AgentAdapter, AgentContext, AgentError, AgentOutput, register

PROMPT = """你是一个被能力网关调用的领域执行器。

任务目标：{goal}
任务类型：{task_type}
输入：{inputs}
可用输入产物：{artifacts}
本环境应当产出：{produces}

要求：只输出一个 JSON 对象 {{"artifacts": {{"<产物名>": <JSON>}}, "summary": "<一句话>"}}，
不要输出其它内容。"""


class CLIAdapter:
    name = "cli"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    async def run(self, ctx: AgentContext) -> AgentOutput:
        command = self.config.get("command")
        if not command:
            raise AgentError("cli adapter 需要 config.command")
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        timeout = float(self.config.get("timeout_s", 300))
        prompt = PROMPT.format(
            goal=ctx.task.goal,
            task_type=ctx.task.task_type,
            inputs=json.dumps(ctx.task.inputs, ensure_ascii=False, default=str),
            artifacts=json.dumps(list(ctx.artifacts), ensure_ascii=False),
            produces=ctx.env.artifacts.produces,
        )

        with tempfile.TemporaryDirectory(prefix="capgate-cli-") as tmp:
            out_file = Path(tmp) / "out.json"
            if self.config.get("output_mode") == "file":
                prompt = prompt + f"\n\n把结果写到文件：{out_file}"
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.get("cwd") or None,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=timeout
                )
            except TimeoutError as exc:
                proc.kill()
                raise AgentError(f"CLI agent 超时（{timeout}s）") from exc

            if proc.returncode != 0:
                raise AgentError(
                    f"CLI agent 退出码 {proc.returncode}：{stderr.decode('utf-8', 'replace')[:800]}"
                )
            if self.config.get("output_mode") == "file":
                if not out_file.exists():
                    raise AgentError("CLI agent 未写出结果文件")
                raw = out_file.read_text(encoding="utf-8")
            else:
                raw = stdout.decode("utf-8", "replace")

        data = _parse(raw)
        return AgentOutput(
            artifacts=data.get("artifacts") or {},
            text=str(data.get("summary", "")),
            trace=[{"adapter": "cli", "argv": argv}],
        )


def _parse(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 容忍前后有日志噪声：取第一个平衡的 JSON 对象
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise AgentError("CLI agent 输出不是 JSON") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AgentError(f"CLI agent 输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise AgentError("CLI agent 输出必须是 JSON 对象")
    return data


@register("cli")
def _factory(config: dict[str, Any]) -> AgentAdapter:
    return CLIAdapter(config)
