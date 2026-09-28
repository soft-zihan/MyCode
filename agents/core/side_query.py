"""边车 LLM 查询（标题生成 / skill 编译 / 折叠摘要等小任务）。

U0 从 Agent 拆出：Agent 保留薄门面（外部调用点不变），实现与
client 缓存收敛于 SideQueryFactory。行为与原 Agent._build_side_query
逐行等价：side endpoint 与主 endpoint 相同时回退主 client；
无可用 client 返回 None。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import openai

from agents.core.model_registry import resolve_side_endpoint
from agents.observability.trace import trace_span

if TYPE_CHECKING:
    from agents.agent import Agent


class SideQueryFactory:
    def __init__(self, agent: "Agent"):
        self._agent = agent
        self._client_cache: tuple[tuple, tuple] | None = None

    def get_client(self):
        a = self._agent
        endpoint = resolve_side_endpoint(primary=a._primary_endpoint())
        if (endpoint.model == a.model
                and endpoint.base_url == a._api_base):
            return None
        cache_key = (endpoint.model, endpoint.base_url, True)
        cached = self._client_cache
        if cached and cached[0] == cache_key:
            return cached[1]
        client = openai.AsyncOpenAI(base_url=endpoint.base_url, api_key=endpoint.api_key)
        result = (client, endpoint.model, True)
        self._client_cache = (cache_key, result)
        return result

    def build(self, *, max_tokens: int = 256):
        a = self._agent
        side = self.get_client()
        if side is not None:
            client, model, use_openai = side
        elif a._openai_client:
            client, model, use_openai = a._openai_client, a.model, True
        else:
            return None

        async def _sq_openai(system: str, user_message: str) -> str:

            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ]
            normalized_max_tokens = max(1, int(max_tokens))
            input_payload = json.dumps({
                "model": model,
                "max_tokens": normalized_max_tokens,
                "messages": messages,
            }, ensure_ascii=False, default=str)[:4000]

            with trace_span(
                "side_query",
                name=f"side_query.{model}",
                model=model,
                input=input_payload,
                metadata={"max_tokens": normalized_max_tokens},
            ) as span:
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=normalized_max_tokens,
                    messages=messages,
                )
                if not resp.choices:
                    logging.warning("side_query returned no OpenAI-compatible choices: model=%s", model)
                    span.update(output="")
                    span.record_error(RuntimeError("side_query returned no choices"))
                    return ""

                choice = resp.choices[0]
                content = choice.message.content or ""
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    total_tokens = int(getattr(usage, "total_tokens", 0) or 0) or (input_tokens + output_tokens)
                    cached_tokens = int(getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0)
                    cached_tokens = min(cached_tokens, input_tokens)
                    span.update(usage_details={
                        "input": input_tokens - cached_tokens,
                        "input_cached_tokens": cached_tokens,
                        "output": output_tokens,
                        "total": total_tokens,
                    })

                span.update(output=json.dumps({
                    "content": content[:4000],
                    "finish_reason": getattr(choice, "finish_reason", None),
                }, ensure_ascii=False, default=str))

                if not content.strip():
                    logging.warning(
                        "side_query returned empty OpenAI-compatible response: model=%s finish_reason=%s message=%s",
                        model,
                        getattr(choice, "finish_reason", ""),
                        choice.message,
                    )
                return content
        return _sq_openai
