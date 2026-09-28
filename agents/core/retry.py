"""LLM 调用重试原语（M4 解环：从 agent.py 提取的叶子模块）。

model_caller 依赖 with_retry/ContentLevelError，agent 依赖 model_caller——
留在 agent.py 会形成 model_caller↔agent 环（只能靠函数内延迟 import 绕过）。
本模块只依赖 logging 工具与 circuit_breaker（均为叶子），环消失。
"""

from __future__ import annotations

import asyncio
import time

from agents.core.circuit_breaker import llm_circuit_breaker
from agents.logging import print_retry


class ContentLevelError(Exception):
    """模型返回内容级错误（空响应、截断、畸形 tool_call 等）。"""
    pass


def is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    if isinstance(error, ContentLevelError):
        return True
    return False


async def with_retry(fn, max_retries: int = 3):
    if not llm_circuit_breaker.can_execute():
        raise RuntimeError("LLM API circuit breaker is open, retry later")

    for attempt in range(max_retries + 1):
        try:
            result = await fn()
            llm_circuit_breaker.record_success()
            return result
        except Exception as error:
            llm_circuit_breaker.record_failure()
            if attempt >= max_retries or not is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)
