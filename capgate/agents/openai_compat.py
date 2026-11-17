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
from pydantic import BaseModel, Field

from .base import AgentAdapter, AgentContext, AgentError, AgentOutput, register

SYSTEM = """你是能力网关里的领域执行器。你可以调用挂载的能力工具。
完成工作后，只输出一个 JSON 对象：
{"artifacts": {"<产物名>": <任意 JSON>}, "summary": "<一句话>"}
如任务依赖工具，先调用工具，再使用它返回的实际内容。"""


class TokenPricing(BaseModel):
    input: float = Field(ge=0, allow_inf_nan=False)
    output: float = Field(ge=0, allow_inf_nan=False)
    cached_input: float | None = Field(default=None, ge=0, allow_inf_nan=False)


def completion_usage(data: dict[str, Any], pricing: TokenPricing | None = None) -> dict[str, Any]:
    raw = data.get("usage") or {}
    if not isinstance(raw, dict):
        return {}
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    usage = {
        field: raw[field] for field in fields if type(raw.get(field)) is int and raw[field] >= 0
    }
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    details = raw.get("prompt_tokens_details")
    cached = raw.get("prompt_cache_hit_tokens")
    if isinstance(details, dict):
        cached = details.get("cached_tokens", cached)
    if prompt is not None and type(cached) is int and 0 <= cached <= prompt:
        usage["cached_prompt_tokens"] = cached
    if prompt is not None and completion is not None:
        usage.setdefault("total_tokens", prompt + completion)
        if pricing and pricing.cached_input is None:
            usage["cost_usd"] = (prompt * pricing.input + completion * pricing.output) / 1_000_000
        elif pricing and "cached_prompt_tokens" in usage:
            usage["cost_usd"] = (
                (prompt - cached) * pricing.input
                + cached * pricing.cached_input
                + completion * pricing.output
            ) / 1_000_000
    return usage


async def request_completion(
    client: httpx.AsyncClient, url: str, api_key: str, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = await client.post(
            url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
            json=payload,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise AgentError(f"模型 API 返回 HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise AgentError(f"模型 API 请求失败（{type(exc).__name__}）") from exc
    data = response.json()
    if not isinstance(data, dict):
        raise AgentError("模型 API 响应须为 JSON 对象")
    return data


def response_message(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AgentError("模型 API 没有返回有效消息")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AgentError("模型 API 没有返回有效消息")
    finish = choice.get("finish_reason")
    if finish not in (None, "stop", "tool_calls"):
        raise AgentError(f"模型 API 未完成生成（{finish}）")
    return message


def _total_usage(requests: list[dict[str, Any]]) -> dict[str, Any]:
    if not requests:
        return {}
    known_fields = set(requests[0]).intersection(*requests[1:])
    return {
        **{field: sum(request[field] for request in requests) for field in sorted(known_fields)},
        "requests": len(requests),
    }


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
        timeout_s = float(self.config.get("timeout_s", 120))
        configured_price = self.config.get("prices_usd_per_million", {}).get(ctx.model)
        pricing = (
            TokenPricing.model_validate(configured_price) if configured_price is not None else None
        )
        instructions = self.config.get("instructions")

        tools = await ctx.toolbox.specs()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{SYSTEM}\n{instructions}" if instructions else SYSTEM},
            {"role": "user", "content": self._prompt(ctx)},
        ]
        trace: list[dict[str, Any]] = []
        requests: list[dict[str, Any]] = []

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                for turn in range(max_turns):
                    payload = {
                        "model": ctx.model,
                        "messages": messages,
                        "temperature": float(self.config.get("temperature", 0)),
                    }
                    for option in ("reasoning_effort", "max_tokens"):
                        if option in self.config:
                            payload[option] = self.config[option]
                    if tools:
                        payload.update(tools=tools, tool_choice="auto")
                    data = await request_completion(client, base_url, api_key, payload)
                    usage = completion_usage(data, pricing)
                    requests.append(usage)
                    ctx.log(
                        "llm.usage",
                        {"run_id": ctx.run_id, "turn": turn + 1, "model": ctx.model, **usage},
                    )
                    message = response_message(data)
                    calls = message.get("tool_calls") or []
                    if not calls:
                        trace.append({"turn": turn, "finish": "text"})
                        return self._parse(
                            message.get("content") or "", trace, _total_usage(requests)
                        )
                    messages.append(message)

                    for call in calls:
                        name = call["function"]["name"]
                        args = json.loads(call["function"]["arguments"])
                        if not isinstance(args, dict):
                            raise AgentError(f"工具 {name!r} 参数须为 JSON 对象")
                        result = await ctx.toolbox.call_advertised(name, args)
                        content = json.dumps(result, ensure_ascii=False, default=str)
                        if len(content) > 20_000:
                            raise AgentError(f"工具 {name!r} 的结果过长，请缩小查询范围")
                        trace.append({"turn": turn, "tool": name, "ok": True})
                        messages.append(
                            {"role": "tool", "tool_call_id": call["id"], "content": content}
                        )
            raise AgentError(f"超过最大轮数 {max_turns} 仍未收敛")
        except AgentError as exc:
            raise AgentError(str(exc), usage=_total_usage(requests)) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentError("模型消息或工具参数格式无效", usage=_total_usage(requests)) from exc
        except Exception as exc:
            raise AgentError(str(exc), usage=_total_usage(requests)) from exc

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
        if not isinstance(data, dict):
            raise AgentError("模型输出须为 JSON 对象")
        artifacts = data.get("artifacts") or {}
        if not isinstance(artifacts, dict):
            raise AgentError("模型返回的 artifacts 必须是对象")
        return AgentOutput(
            artifacts=artifacts, text=str(data.get("summary", "")), trace=trace, usage=usage
        )


@register("openai")
def _factory(config: dict[str, Any]) -> AgentAdapter:
    return OpenAICompatAdapter(config)
