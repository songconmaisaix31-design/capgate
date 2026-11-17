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
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from contextlib import suppress
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

CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "plugins",
    "apps",
    "browser_use",
    "computer_use",
    "view_image",
    "image_generation",
    "multi_agent",
    "multi_agent_v2",
    "memories",
    "hooks",
    "code_mode",
    "code_mode_host",
    "skill_search",
    "tool_suggest",
    "workspace_dependencies",
)


class CLIAdapter:
    name = "cli"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    async def run(self, ctx: AgentContext) -> AgentOutput:
        command = self.config.get("command")
        if not command:
            raise AgentError("cli adapter 需要 config.command")
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        argv = _resolve_argv([ctx.model if arg == "{model}" else arg for arg in argv])
        timeout = float(self.config.get("timeout_s", 300))
        output_mode = self.config.get("output_mode", "stdout_json")
        prompt = PROMPT.format(
            goal=ctx.task.goal,
            task_type=ctx.task.task_type,
            inputs=json.dumps(ctx.task.inputs, ensure_ascii=False, default=str),
            artifacts=json.dumps(ctx.artifacts, ensure_ascii=False),
            produces=ctx.env.artifacts.produces,
        )

        if output_mode == "codex_jsonl":
            return await _run_codex(argv, prompt, ctx, timeout)

        if output_mode == "stdout_json":
            raw = await run_command(argv, prompt, timeout, self.config.get("cwd"))
        elif output_mode == "file":
            raw = await self._read_output_file(argv, prompt, timeout)
        else:
            raise AgentError(f"CLI adapter 不支持 output_mode {output_mode!r}")

        data = _parse(raw)
        return AgentOutput(
            artifacts=data.get("artifacts") or {},
            text=str(data.get("summary", "")),
            trace=[{"adapter": "cli", "argv": argv}],
        )

    async def _read_output_file(self, argv: list[str], prompt: str, timeout_s: float) -> str:
        scratch = Path("var") / "cli"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="agent-", dir=scratch.resolve()) as tmp:
            out_file = Path(tmp) / "out.json"
            await run_command(
                argv, f"{prompt}\n\n把结果写到文件：{out_file}", timeout_s, self.config.get("cwd")
            )
            if not out_file.is_file():
                raise AgentError("CLI agent 未写出结果文件")
            return out_file.read_text(encoding="utf-8")


async def run_command(
    argv: list[str], prompt: str, timeout_s: float, cwd: str | Path | None = None
) -> str:
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or None,
        **options,
    )
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await proc.communicate(prompt.encode("utf-8"))
    except TimeoutError as exc:
        await _terminate_tree(proc)
        raise AgentError(f"CLI agent 超时（{timeout_s}s）") from exc
    except asyncio.CancelledError:
        await _terminate_tree(proc)
        raise
    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace")[:800]
        raise AgentError(f"CLI agent 退出码 {proc.returncode}：{detail}")
    return stdout.decode("utf-8", "replace")


async def _terminate_tree(proc: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        if proc.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/T",
                "/F",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await killer.communicate()
            if killer.returncode != 0 and proc.returncode is None:
                detail = output.decode(errors="replace").strip()
                raise AgentError(f"CLI 子进程树 {proc.pid} 未能终止：{detail}")
    else:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


async def _run_codex(
    argv: list[str], prompt: str, ctx: AgentContext, timeout_s: float
) -> AgentOutput:
    scratch = Path("var") / "cli"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-", dir=scratch.resolve()) as tmp:
        cwd = Path(tmp)
        raw = await run_command([*argv, "debug", "models"], "", min(timeout_s, 30), cwd)
        models = json.loads(raw)["models"]
        model = next((model for model in models if model["slug"] == ctx.model), None)
        if model is None:
            raise AgentError(f"本机 Codex 模型目录没有 {ctx.model!r}")

        # apply_patch 由模型目录单独控制，关闭 shell 不能移除它。
        model.update(shell_type="disabled", apply_patch_tool_type=None)
        model["experimental_supported_tools"] = []
        catalog = cwd / "models.json"
        catalog.write_text(json.dumps({"models": [model]}, ensure_ascii=False), encoding="utf-8")
        catalog_setting = f"model_catalog_json='{catalog.as_posix()}'"
        verified = await run_command(
            [*argv, "debug", "-c", catalog_setting, "models"], "", min(timeout_s, 30), cwd
        )
        loaded = json.loads(verified)["models"]
        if (
            len(loaded) != 1
            or loaded[0]["slug"] != ctx.model
            or loaded[0]["shell_type"] != "disabled"
            or loaded[0].get("apply_patch_tool_type") is not None
            or loaded[0].get("experimental_supported_tools")
        ):
            raise AgentError("Codex 未加载关闭原生文件和命令工具的模型配置")

        disabled = [arg for name in CODEX_DISABLED_FEATURES for arg in ("--disable", name)]
        settings = [
            catalog_setting,
            "approval_policy='never'",
            "project_doc_max_bytes=0",
            "web_search='disabled'",
            "mcp_servers={}",
            "agents.enabled=false",
            "skills.include_instructions=false",
            "include_apps_instructions=false",
        ]
        command = [
            *argv,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--json",
            "-m",
            ctx.model,
            "--enable",
            "skip_host_skill_discovery",
            *(arg for setting in settings for arg in ("-c", setting)),
            *disabled,
            "-",
        ]
        raw = await run_command(command, prompt, timeout_s, cwd)
    return _parse_codex_events(raw, ctx)


def _parse_codex_events(raw: str, ctx: AgentContext) -> AgentOutput:
    thread_id = ""
    last_message = ""
    usage = None
    for sequence, line in enumerate(raw.splitlines(), start=1):
        event = json.loads(line)
        kind = event["type"]
        if kind == "thread.started":
            thread_id = event["thread_id"]
        payload = {"run_id": ctx.run_id, "thread_id": thread_id, "sequence": sequence}
        item = event.get("item")
        if item is not None:
            payload.update(item_id=item["id"], item_type=item["type"])
        ctx.log(f"codex.{kind}", payload)
        if item is not None:
            if item["type"] in {"command_execution", "file_change", "mcp_tool_call"}:
                raise AgentError(f"Codex 产生了受限执行中禁止的事件：{item['type']}")
            if kind == "item.completed" and item["type"] == "agent_message":
                last_message = item["text"]
        if kind == "turn.completed":
            usage = event["usage"]
    if not thread_id or not last_message or usage is None:
        raise AgentError("Codex 缺少会话 ID、最终消息或回合用量")
    data = _parse(last_message)
    return AgentOutput(
        artifacts=data["artifacts"],
        text=data["summary"],
        trace=[{"adapter": "cli", "controller": "codex", "thread_id": thread_id}],
        usage=usage,
    )


def _resolve_argv(argv: list[str]) -> list[str]:
    """把裸命令名解析成真实可执行文件。

    Windows 上 npm / pip 安装的 CLI 通常是 ``codex.cmd`` 这类批处理垫片，
    ``create_subprocess_exec`` 不会按 PATHEXT 补全，直接调用会报 WinError 2。
    这里统一解析：找到真实路径，遇到 .cmd/.bat 就用 cmd.exe 起。
    """
    if not argv:
        return argv
    exe = shutil.which(argv[0]) or argv[0]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", exe, *argv[1:]]
    return [exe, *argv[1:]]


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
