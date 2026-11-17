"""cli adapter：把任意命令行 agent 当执行器。

这是"异构"最实用的一层：codex / dsh / claude / 自家脚本，只要能吃 stdin、
吐一段 JSON，就能挂进 env。网关不关心它内部是什么框架。

config:
    command: ["codex", "exec", "-"] 或 "bash -lc '...'"
    timeout_s: 300
    cwd: 可选
    output_mode: stdout_json | file | codex_jsonl
    codex_text_replacements: true 时，从完整文本替换生成 codex_change
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import signal
import tempfile
from contextlib import suppress
from difflib import unified_diff
from io import StringIO
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

CODEX_TEXT_PROMPT = """你是能力网关的代码修改执行器。只使用随任务提供的文件文本。

任务目标：{goal}
任务类型：{task_type}
任务输入 JSON：{inputs}
可用输入产物：{artifacts}

使用任务输入中 files 字段提供的原始路径和文本。只为确有修改的文件返回完整的新文本。
保留 LF 换行，非空文本以换行结尾。每个文件只出现一次。
仅输出符合指定结构的 JSON，包含 replacements（每项含 path 和 text）和 reason。
网关生成统一 diff，请在 text 中返回完整文件。"""

CODEX_TEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "replacements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                "required": ["path", "text"],
                "additionalProperties": False,
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["replacements", "reason"],
    "additionalProperties": False,
}

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
        task_inputs = ctx.task.inputs
        source_files = None
        prompt_template = PROMPT
        text_replacements = self.config.get("codex_text_replacements", False)
        if not isinstance(text_replacements, bool):
            raise AgentError("codex_text_replacements 须为布尔值")
        if text_replacements:
            if output_mode != "codex_jsonl":
                raise AgentError("codex_text_replacements 需要 codex_jsonl 输出模式")
            source_files = _codex_source_files(task_inputs.get("files"))
            task_inputs = {**task_inputs, "files": source_files}
            prompt_template = CODEX_TEXT_PROMPT
        prompt = prompt_template.format(
            goal=ctx.task.goal,
            task_type=ctx.task.task_type,
            inputs=json.dumps(task_inputs, ensure_ascii=False, default=str),
            artifacts=json.dumps(ctx.artifacts, ensure_ascii=False),
            produces=ctx.env.artifacts.produces,
        )

        if output_mode == "codex_jsonl":
            return await _run_codex(argv, prompt, ctx, timeout, source_files=source_files)

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
    input_bytes = prompt.encode("utf-8")
    try:
        if os.name == "nt":
            from ._windows_job import run_windows_job

            code, stdout, stderr = await run_windows_job(argv, input_bytes, timeout_s, cwd)
        else:
            code, stdout, stderr = await _run_posix_command(argv, input_bytes, timeout_s, cwd)
    except TimeoutError as exc:
        raise AgentError(f"CLI agent 超时（{timeout_s}s）") from exc
    if code != 0:
        detail = stderr.decode("utf-8", "replace")[:800]
        raise AgentError(f"CLI agent 退出码 {code}：{detail}")
    return stdout.decode("utf-8", "replace")


async def _run_posix_command(
    argv: list[str], input_bytes: bytes, timeout_s: float, cwd: str | Path | None
) -> tuple[int | None, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or None,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await proc.communicate(input_bytes)
    except (TimeoutError, asyncio.CancelledError):
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    return proc.returncode, stdout, stderr


async def _run_codex(
    argv: list[str],
    prompt: str,
    ctx: AgentContext,
    timeout_s: float,
    *,
    source_files: dict[str, str] | None = None,
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
        ]
        if source_files is not None:
            schema_path = cwd / "output.json"
            schema_path.write_text(json.dumps(CODEX_TEXT_SCHEMA), encoding="utf-8")
            command.extend(("--output-schema", str(schema_path)))
        command.append("-")
        raw = await run_command(command, prompt, timeout_s, cwd)
    return _parse_codex_events(raw, ctx, source_files=source_files)


def _parse_codex_events(
    raw: str, ctx: AgentContext, *, source_files: dict[str, str] | None = None
) -> AgentOutput:
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
            if item["type"] in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                raise AgentError(f"Codex 产生了受限执行中禁止的事件：{item['type']}")
            if kind == "item.completed" and item["type"] == "agent_message":
                last_message = item["text"]
        if kind == "turn.completed":
            usage = event["usage"]
    if not thread_id or not last_message or usage is None:
        raise AgentError("Codex 缺少会话 ID、最终消息或回合用量")
    try:
        data = (
            _parse(last_message)
            if source_files is None
            else _codex_patch(last_message, source_files)
        )
    except AgentError as exc:
        raise AgentError(str(exc), usage=usage) from exc
    return AgentOutput(
        artifacts=data["artifacts"],
        text=data["summary"],
        trace=[{"adapter": "cli", "controller": "codex", "thread_id": thread_id}],
        usage=usage,
    )


def _codex_source_files(files: Any) -> dict[str, str]:
    if isinstance(files, str):
        try:
            files = json.loads(files, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError as exc:
            raise AgentError("Codex 的 inputs.files 须为有效 JSON 对象") from exc
    if not isinstance(files, dict) or not files:
        raise AgentError("Codex 文本替换需要 inputs.files 提供非空的路径到原文映射")
    for path, content in files.items():
        if (
            not isinstance(path, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path) is None
            or any(part.casefold() == ".git" or part.endswith(".") for part in path.split("/"))
        ):
            raise AgentError(f"Codex 文件路径须为规范相对路径，且仅含英数字和 ._-/: {path!r}")
        _codex_lf_text(path, content)
    return files


def _codex_lf_text(path: str, content: Any) -> str:
    if (
        not isinstance(content, str)
        or "\r" in content
        or "\0" in content
        or (content and not content.endswith("\n"))
    ):
        raise AgentError(f"Codex 文件 {path!r} 须为 UTF-8 LF 文本，非空文件以 LF 结尾")
    try:
        content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AgentError(f"Codex 文件 {path!r} 包含无效的 UTF-8 字符") from exc
    return content


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise AgentError(f"Codex JSON 含重复字段 {key!r}")
        obj[key] = value
    return obj


def _codex_patch(raw: str, source_files: dict[str, str]) -> dict[str, Any]:
    try:
        output = json.loads(raw, object_pairs_hook=_unique_json_object)
    except json.JSONDecodeError as exc:
        raise AgentError("Codex 文本替换须为有效 JSON") from exc
    if not isinstance(output, dict) or set(output) != {"replacements", "reason"}:
        raise AgentError("Codex 文本替换须包含 replacements 和 reason")
    reason = output["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise AgentError("Codex 文本替换缺少修改理由")
    replacements = output["replacements"]
    if not isinstance(replacements, list) or not replacements:
        raise AgentError("Codex 未返回任何文件修改")
    patches: dict[str, str] = {}
    for replacement in replacements:
        if not isinstance(replacement, dict) or set(replacement) != {"path", "text"}:
            raise AgentError("Codex 的每项替换须包含 path 和完整 text")
        path = replacement["path"]
        if not isinstance(path, str) or path not in source_files:
            raise AgentError(f"Codex 返回了任务未提供的文件 {path!r}")
        if path in patches:
            raise AgentError(f"Codex 对文件 {path!r} 返回了重复替换")
        original = source_files[path]
        updated = _codex_lf_text(path, replacement["text"])
        if original == updated:
            raise AgentError(f"Codex 返回的文件 {path!r} 没有修改")
        # Git 只按 LF 计行。splitlines 会把其他 Unicode 分隔符也算作换行。
        patches[path] = "".join(
            unified_diff(
                StringIO(original).readlines(),
                StringIO(updated).readlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    diff = "".join(patches[path] for path in sorted(patches))
    return {"artifacts": {"codex_change": {"diff": diff, "reason": reason}}, "summary": reason}


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
