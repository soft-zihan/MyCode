"""模型调用关注点（U0 执行层重构：与执行调度分离）。

AgentLoop 只负责 turn/step 编排与工具执行；LLM 请求-响应细节
（请求组装 / token breakdown / 流式消费 / 结果构建 / trace 上报 /
超时重试外壳）收敛于本模块。这是 U3 后台化与 U6 崩溃恢复的
结构前提之一（调度与调用分离后各自演化）。

行为与原 agent_loop.call_model_stream 逐行等价（U0 纪律：不夹带
功能变更），仅删除两处死代码：`if not a.is_sub_agent: pass` no-op
与 TITLE-DEBUG stderr 调试残留。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import TYPE_CHECKING, Any

from agents.core.text_sanitization import safe_utf8_text, sanitize_for_utf8
from agents.tools.registry import get_active_tool_definitions

if TYPE_CHECKING:
    from agents.agent import Agent


_TRACE_TEXT_LIMIT = int(os.environ.get("MYCODE_TRACE_TEXT_LIMIT", "2000"))
_TRACE_MESSAGE_LIMIT = int(os.environ.get("MYCODE_TRACE_MESSAGE_LIMIT", "50"))
_TRACE_PAYLOAD_LIMIT = int(os.environ.get("MYCODE_TRACE_PAYLOAD_LIMIT", "100000"))


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """tool_definitions → OpenAI function schema。

    D7：agent 工具的 type enum 每次请求现场重解析（含自定义代理），
    与 get_sub_agent_config 的可用类型始终一致，不做静态快照。
    """
    dynamic_agent_types: list[str] | None = None
    out = []
    for t in tools:
        schema = t["input_schema"]
        if t["name"] == "agent":
            if dynamic_agent_types is None:
                from agents.core.subagent import get_available_agent_types
                dynamic_agent_types = [n["name"] for n in get_available_agent_types() if n.get("name")]
            if dynamic_agent_types and "type" in schema.get("properties", {}):
                import copy
                schema = copy.deepcopy(schema)
                schema["properties"]["type"]["enum"] = dynamic_agent_types
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": schema,
            },
        })
    return out


def _truncate_trace_text(value: Any, limit: int = _TRACE_TEXT_LIMIT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    return value[:limit]


def _compact_message_for_trace(message: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"role": message.get("role")}
    content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(_truncate_trace_text(item.get("text") or item, 500))
            else:
                parts.append(_truncate_trace_text(item, 500))
        compact["content"] = "\n".join(parts)[:_TRACE_TEXT_LIMIT]
    else:
        compact["content"] = _truncate_trace_text(content)

    if message.get("tool_calls"):
        compact["tool_calls"] = [
            {
                "id": tc.get("id"),
                "name": (tc.get("function") or {}).get("name"),
                "arguments": _truncate_trace_text((tc.get("function") or {}).get("arguments"), 500),
            }
            for tc in message["tool_calls"]
            if isinstance(tc, dict)
        ]
    if message.get("tool_call_id"):
        compact["tool_call_id"] = message.get("tool_call_id")
    return compact


def _model_input_for_trace(messages: list[dict[str, Any]], tool_defs: list[dict[str, Any]]) -> str:
    recent = messages[-_TRACE_MESSAGE_LIMIT:]
    payload = {
        "message_count": len(messages),
        "omitted_message_count": max(0, len(messages) - len(recent)),
        "messages": [_compact_message_for_trace(msg) for msg in recent if isinstance(msg, dict)],
        "tools": [tool.get("name") for tool in tool_defs],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)[:_TRACE_PAYLOAD_LIMIT]


def _model_output_for_trace(result: dict[str, Any]) -> str:
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    payload = {
        "finish_reason": choice.get("finish_reason"),
        "content": _truncate_trace_text(message.get("content"), 4000),
        "thinking": _truncate_trace_text(message.get("thinking"), 2000),
        "tool_calls": [
            {
                "id": tc.get("id"),
                "name": (tc.get("function") or {}).get("name"),
                "arguments": _truncate_trace_text((tc.get("function") or {}).get("arguments"), 1000),
            }
            for tc in tool_calls
            if isinstance(tc, dict)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, default=str)[:_TRACE_PAYLOAD_LIMIT]


def _usage_details_for_trace(usage: dict[str, Any]) -> dict[str, int]:
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    cached_tokens = int(usage.get("cached_tokens") or 0)
    cached_tokens = min(cached_tokens, input_tokens)
    return {
        "input": input_tokens - cached_tokens,
        "input_cached_tokens": cached_tokens,
        "output": output_tokens,
        "total": total_tokens or (input_tokens + output_tokens),
    }


class ModelCaller:
    """单次流式模型调用的完整生命周期。"""

    def __init__(self, agent: "Agent"):
        self._agent = agent

    async def call(self, *, tools_enabled: bool = True) -> dict:
        """流式模型调用（trace 外壳 + 超时 + 重试）。"""
        from agents.observability.trace import trace_span
        from agents.agent import _with_retry

        a = self._agent
        _model_t0 = time.time()

        with trace_span(
            "model_call",
            model=a.model,
            metadata={
                "provider": "openai",
                "turn_number": a._current_turn,
                "step_number": a._current_step,
                "is_sub_agent": a.is_sub_agent,
            },
        ) as span:

            async def _attempt():
                await a.check_and_compact()
                create_params, raw_messages, metrics = self._assemble_request(span, tools_enabled)
                metrics.update(self._compute_token_breakdown(raw_messages))
                stream = await a.openai_client.chat.completions.create(**create_params)
                consumed = await self._consume_stream(stream)
                return self._build_result(consumed, metrics)

            try:
                model_timeout = int(os.environ.get("MYCODE_MODEL_TIMEOUT", "120"))
                result = await asyncio.wait_for(_with_retry(_attempt), timeout=model_timeout)
                usage = result.get("usage", {}) if isinstance(result, dict) else {}
                input_tokens = int(usage.get("prompt_tokens", 0) or 0)
                output_tokens = int(usage.get("completion_tokens", 0) or 0)
                total_tokens = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
                cached_tokens = int(usage.get("cached_tokens", 0) or 0)
                duration_s = round(time.time() - _model_t0, 2)
                span.update(
                    output=_model_output_for_trace(result),
                    usage_details=_usage_details_for_trace(usage),
                    metadata={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                        "cached_tokens": cached_tokens,
                        "cache_hit_rate": round(cached_tokens / input_tokens, 3) if input_tokens else 0.0,
                        "duration_s": duration_s,
                        "success": True,
                    },
                )

                asm = result.get("_assembly_metrics", None) if isinstance(result, dict) else None
                if asm:
                    span.add_metadata(assembly_metrics=asm)

                return result
            except asyncio.TimeoutError:
                duration_s = round(time.time() - _model_t0, 2)
                span.add_metadata(timeout_s=model_timeout, duration_s=duration_s)
                span.record_error(TimeoutError(f"Model call timed out after {model_timeout}s"))
                raise TimeoutError(f"Model call timed out after {model_timeout}s")
            except Exception as e:
                span.add_metadata(duration_s=round(time.time() - _model_t0, 2))
                span.record_error(e)
                raise

    def _assemble_request(self, span, tools_enabled: bool) -> tuple[dict, list[dict], dict]:
        """组装 create 参数 + 上报 trace input，返回 (params, raw_messages, 组装指标)。"""
        a = self._agent
        _asm_t0 = time.perf_counter()

        _t1 = time.perf_counter()
        tool_defs = get_active_tool_definitions(a.tools) if tools_enabled else []
        _tool_defs_ms = (time.perf_counter() - _t1) * 1000

        _t2 = time.perf_counter()
        raw_messages = a.messages
        _msg_history_ms = (time.perf_counter() - _t2) * 1000

        _t3 = time.perf_counter()
        sanitized_messages = sanitize_for_utf8(raw_messages)
        _sanitize_msgs_ms = (time.perf_counter() - _t3) * 1000

        # 运行时易变状态尾部注入（prefix cache 保护：主 system prompt 会话内不变，
        # 尾部消息每步变化不影响其前全部历史的缓存命中）
        _guidance = a.build_runtime_guidance()
        if _guidance:
            sanitized_messages = [*sanitized_messages, {"role": "system", "content": _guidance}]

        create_params = {
            "model": a.model,
            "messages": sanitized_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if a.thinking is not None and "qwen" in a.model.lower():
            create_params["extra_body"] = {"enable_thinking": bool(a.thinking)}
        if tool_defs:
            _t4 = time.perf_counter()
            openai_tools = _to_openai_tools(tool_defs)
            _convert_tools_ms = (time.perf_counter() - _t4) * 1000

            _t5 = time.perf_counter()
            create_params["tools"] = sanitize_for_utf8(openai_tools)
            _sanitize_tools_ms = (time.perf_counter() - _t5) * 1000
        else:
            _convert_tools_ms = 0
            _sanitize_tools_ms = 0

        _assembly_ms = (time.perf_counter() - _asm_t0) * 1000

        _msg_count = len(raw_messages)
        _tool_count = len(tool_defs)

        span.update(
            input=_model_input_for_trace(sanitized_messages, tool_defs),
            model_parameters={
                "model": a.model,
                "stream": True,
                "message_count": _msg_count,
                "tool_count": _tool_count,
            },
        )

        metrics = {
            "assembly_ms": round(_assembly_ms, 2),
            "tool_defs_ms": round(_tool_defs_ms, 2),
            "msg_history_ms": round(_msg_history_ms, 2),
            "sanitize_msgs_ms": round(_sanitize_msgs_ms, 2),
            "convert_tools_ms": round(_convert_tools_ms, 2),
            "sanitize_tools_ms": round(_sanitize_tools_ms, 2),
            "msg_count": _msg_count,
            "msg_chars": sum(len(str(m.get("content", ""))) for m in raw_messages),
            "tool_count": _tool_count,
        }
        return create_params, raw_messages, metrics

    def _compute_token_breakdown(self, raw_messages: list[dict]) -> dict:
        """细粒度 token（字符数）breakdown：按角色 + system prompt 分部 + plan mode。"""
        a = self._agent
        _system_chars = 0
        _user_chars = 0
        _assistant_chars = 0
        _tool_result_chars = 0

        for msg in raw_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str):
                chars = len(content)
            elif isinstance(content, list):
                chars = sum(len(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text")
            else:
                chars = 0
            if role == "system":
                _system_chars = chars
            elif role == "user":
                _user_chars += chars
            elif role == "assistant":
                _assistant_chars += chars
            elif role == "tool":
                _tool_result_chars += chars

        # System prompt 各部分（重新构建以获取各部分大小）
        _system_base_chars = 0
        _system_claude_md_chars = 0
        _system_agents_md_chars = 0
        _system_skills_chars = 0
        _system_wiki_chars = 0
        _system_agents_chars = 0
        _system_workspace_chars = 0
        try:
            from agents.core.prompt import (
                load_claude_md, load_agents_md, build_skill_descriptions,
                build_wiki_prompt_section,
                build_agent_descriptions, build_workspace_structure
            )
            _system_claude_md_chars = len(load_claude_md())
            _system_agents_md_chars = len(load_agents_md())
            _system_skills_chars = len(build_skill_descriptions())
            _system_wiki_chars = len(build_wiki_prompt_section())
            _system_agents_chars = len(build_agent_descriptions())
            _system_workspace_chars = len(build_workspace_structure())
            _system_base_chars = _system_chars - _system_claude_md_chars - _system_agents_md_chars - _system_skills_chars - _system_wiki_chars - _system_agents_chars - _system_workspace_chars
        except Exception:
            _system_base_chars = _system_chars

        # plan mode 的额外 token 占用
        _plan_mode_chars = 0
        _is_plan_mode = a.permission_mode == "plan"
        if _is_plan_mode and hasattr(a, '_plan_mode_manager') and a._plan_mode_manager:
            try:
                _plan_mode_chars = len(a._plan_mode_manager.build_plan_mode_prompt())
            except Exception:
                _plan_mode_chars = 0

        return {
            "system_chars": _system_chars,
            "user_chars": _user_chars,
            "assistant_chars": _assistant_chars,
            "tool_result_chars": _tool_result_chars,
            # Tool result breakdown by name
            "tool_result_by_name": dict(getattr(a, '_tool_result_chars', {})),
            # System prompt 细粒度
            "system_base_chars": _system_base_chars,
            "system_claude_md_chars": _system_claude_md_chars,
            "system_agents_md_chars": _system_agents_md_chars,
            "system_skills_chars": _system_skills_chars,
            "system_wiki_chars": _system_wiki_chars,
            "system_agents_chars": _system_agents_chars,
            "system_workspace_chars": _system_workspace_chars,
            # Plan mode
            "is_plan_mode": _is_plan_mode,
            "plan_mode_chars": _plan_mode_chars,
        }

    async def _consume_stream(self, stream) -> dict:
        """消费 SSE 流：thinking/text/tool_calls 增量、usage、abort、空闲超时。"""
        from agents.agent import ContentLevelError
        from agents.wiki.citation import CitationStripper

        a = self._agent
        _citation_stripper = CitationStripper()
        acc: dict = {"content": "", "tool_calls": {}, "finish_reason": "", "usage": None}

        stream_idle_timeout = int(os.environ.get("MYCODE_STREAM_IDLE_TIMEOUT", "60"))
        chunk_iter = stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(chunk_iter.__anext__(), timeout=stream_idle_timeout)
            except asyncio.TimeoutError:
                raise ContentLevelError(f"stream_idle_timeout after {stream_idle_timeout}s")
            except StopAsyncIteration:
                break

            if a.abort_requested():
                a.mark_aborted()
                break

            self._process_chunk(chunk, _citation_stripper, acc)

        _citation_tail = _citation_stripper.flush()
        if _citation_tail:
            a.emit_text(_citation_tail)

        tool_calls = acc["tool_calls"]
        assembled = None
        if tool_calls:
            assembled = [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for _, tc in sorted(tool_calls.items())
            ]
            assembled = [tc for tc in assembled if tc["id"] and tc["function"]["name"]]

        return {
            "content": acc["content"],
            "tool_calls": assembled,
            "finish_reason": acc["finish_reason"],
            "usage": acc["usage"],
        }

    def _process_chunk(self, chunk, stripper, acc: dict) -> None:
        """单 chunk 处理：usage 快照 / thinking 增量 / 文本增量（citation 剥离）/ tool_calls 组装 / finish_reason。"""
        a = self._agent
        if chunk.usage:
            prompt_tokens = int(getattr(chunk.usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(chunk.usage, "completion_tokens", 0) or 0)
            total_tokens = int(getattr(chunk.usage, "total_tokens", 0) or 0)
            acc["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens or (prompt_tokens + completion_tokens),
                "cached_tokens": chunk.usage.prompt_tokens_details.cached_tokens if chunk.usage.prompt_tokens_details else 0,
            }

        if not chunk.choices:
            return
        delta = chunk.choices[0].delta

        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            a.append_thinking_text(reasoning)
            # 流式事件只发送 SSE，不持久化到事件日志
            event_data = {"content": reasoning, "turn": a._current_turn, "step": a._current_step}
            if a.current_sub_agent_id:
                event_data["sub_agent_id"] = a.current_sub_agent_id
            a.session.append("thinking", event_data)

        if delta and delta.content:
            raw_delta = safe_utf8_text(delta.content)
            visible = stripper.feed(raw_delta)
            if visible:
                a.emit_text(visible)
            acc["content"] += raw_delta

        if delta and delta.tool_calls:
            for tc in delta.tool_calls:
                existing = acc["tool_calls"].get(tc.index)
                if existing:
                    if tc.function and tc.function.arguments:
                        existing["arguments"] += safe_utf8_text(tc.function.arguments)
                else:
                    acc["tool_calls"][tc.index] = {
                        "id": safe_utf8_text(tc.id or ""),
                        "name": safe_utf8_text((tc.function.name if tc.function else "") or ""),
                        "arguments": safe_utf8_text((tc.function.arguments if tc.function else "") or ""),
                    }

        if chunk.choices[0].finish_reason:
            acc["finish_reason"] = chunk.choices[0].finish_reason

    def _build_result(self, consumed: dict, metrics: dict) -> dict:
        """组装 OpenAI 兼容 result：空响应检查 + citation 剥离 + usage 计数。"""
        from agents.agent import ContentLevelError
        from agents.wiki.citation import strip_citations

        a = self._agent
        content = consumed["content"]
        assembled = consumed["tool_calls"]
        finish_reason = consumed["finish_reason"]
        usage = consumed["usage"]
        thinking_content = a.get_thinking_content()

        if not content and not assembled and not finish_reason:
            raise ContentLevelError("empty_response")
        if not content and not assembled and finish_reason == "length":
            raise ContentLevelError("truncated_response")

        # 4.1：落盘前剥离 citation（防回灌），命中路径计 usage
        content, _cited_paths = strip_citations(content)
        for _p in _cited_paths:
            try:
                from agents.wiki.wiki_manager import increment_usage
                increment_usage(_p)
            except Exception as _e:
                print(f"[wiki_citation] usage update failed for {_p}: {type(_e).__name__}: {_e}")

        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": assembled,
                    "thinking": thinking_content,
                },
                "finish_reason": finish_reason or "stop",
            }],
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "_assembly_metrics": metrics,
        }
