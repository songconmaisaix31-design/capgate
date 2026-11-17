"""openai 兼容 adapter：接任何 /v1/chat/completions 端点。

OpenAI、DeepSeek、通义、vLLM、Ollama、LiteLLM 都能用同一份代码，
所以"异构 agent"在这一层是可替换的，网关不需要知道具体是谁。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .base import AgentAdapter, AgentContext, AgentError, AgentOutput, register

SYSTEM = """你是能力网关里的领域执行器。你可以调用挂载的能力工具。
完成工作后，只输出一个 JSON 对象：
{"artifacts": {"<产物名>": <任意 JSON>}, "summary": "<一句话>"}
不要输出 markdown 代码块以外的任何解释。"""


class OpenAICompatAdapter:
    name = "openai"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    async def run(self, ctx: AgentContext) -> AgentOutput:
        base_url = self.config.get("base_url") or os.environ.get("CAPGATE_LLM_BASE_URL")
        if not base_url:
            raise AgentError("openai adapter 需要 config.base_url 或 CAPGATE_LLM_BASE_URL")
        api_key = self.config.get("api_key") or os.environ.get(
            self.config.get("api_key_env", "CAPGATE_LLM_API_KEY"), ""
        )
        max_turns = int(self.config.get("max_turns", 6))
        timeout = float(self.config.get("timeout_s", 120))

        tools = await ctx.toolbox.specs()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": self._prompt(ctx)},
        ]
        trace: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}

        async with httpx.AsyncClient(timeout=timeout) as client:
            for turn in range(max_turns):
                payload = {
                    "model": ctx.model,
                    "messages": messages,
                    "temperature": float(self.config.get("temperature", 0)),
                }
                if tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"
                resp = await client.post(
                    base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("usage"):
                    usage = data["usage"]
                choice = (data.get("choices") or [{}])[0].get("message") or {}
                messages.append(choice)

                calls = choice.get("tool_calls") or []
                if not calls:
                    text = choice.get("content") or ""
                    trace.append({"turn": turn, "finish": "text"})
                    return self._parse(text, trace, usage)

                for call in calls:
                    fn = call.get("function") or {}
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    capability, _, tool = name.partition("__")
                    try:
                        result = await ctx.toolbox.call(capability, tool, args)
                        payload_out: Any = result
                        ok = True
                    except Exception as exc:
                        payload_out, ok = {"error": str(exc)}, False
                    trace.append({"turn": turn, "tool": name, "ok": ok})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": json.dumps(payload_out, ensure_ascii=False, default=str)[
                                :20000
                            ],
                        }
                    )
        raise AgentError(f"超过最大轮数 {max_turns} 仍未收敛")

    # ------------------------------------------------------------------ 辅助

    def _prompt(self, ctx: AgentContext) -> str:
        return json.dumps(
            {
                "goal": ctx.task.goal,
                "task_type": ctx.task.task_type,
                "inputs": ctx.task.inputs,
                "artifacts": ctx.artifacts,
                "env": {"id": ctx.env.id, "domain": ctx.env.domain},
                "produces": ctx.env.artifacts.produces,
            },
            ensure_ascii=False,
            default=str,
        )

    def _parse(self, text: str, trace: list[dict[str, Any]], usage: dict[str, Any]) -> AgentOutput:
        raw = text.strip()
        fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
        if fence:
            raw = fence.group(1)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"artifacts": {}, "summary": raw[:2000]}
        artifacts = data.get("artifacts") or {}
        if not isinstance(artifacts, dict):
            raise AgentError("模型返回的 artifacts 必须是对象")
        return AgentOutput(
            artifacts=artifacts, text=str(data.get("summary", "")), trace=trace, usage=usage
        )


@register("openai")
def _factory(config: dict[str, Any]) -> AgentAdapter:
    return OpenAICompatAdapter(config)
