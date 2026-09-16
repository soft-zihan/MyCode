"""Trace 结构信号检测模块。

从 Langfuse Trace 数据检测 bad case 信号：
- Span 错误检测
- 异常耗时检测
- Token 异常检测
- 工具失败率检测
"""

from __future__ import annotations

import time
from typing import Any

from agents.observability.evals import get_client


def detect_span_errors(trace_id: str) -> list[dict[str, Any]]:
    """检测 Span 错误。
    
    从 Langfuse Trace 获取 observations，检测 status=ERROR 的 span。
    
    Returns:
        错误列表，每项包含 span_id, span_name, error_message, turn_id
    """
    try:
        client = get_client()
        trace = client.fetch_trace(trace_id)
    except Exception:
        return []
    
    errors = []
    observations = trace.get("observations", [])
    
    for obs in observations:
        if obs.get("statusMessage") or obs.get("level") == "ERROR":
            errors.append({
                "span_id": obs.get("id"),
                "span_name": obs.get("name"),
                "error_message": obs.get("statusMessage", ""),
                "turn_id": obs.get("parentObservationId"),
            })
    
    return errors


def detect_abnormal_duration(
    trace_id: str, 
    threshold_sigma: float = 3.0
) -> list[dict[str, Any]]:
    """检测异常耗时。
    
    从 Langfuse Trace 获取 turn span 的耗时，检测超过 mean + threshold_sigma * std 的。
    
    Returns:
        异常耗时列表，每项包含 span_id, duration, turn_number
    """
    try:
        client = get_client()
        trace = client.fetch_trace(trace_id)
    except Exception:
        return []
    
    observations = trace.get("observations", [])
    
    # 提取所有 turn span 的耗时
    durations = []
    turn_spans = []
    
    for obs in observations:
        if obs.get("name") == "turn" or obs.get("type") == "chain":
            start_time = obs.get("startTime")
            end_time = obs.get("endTime")
            if start_time and end_time:
                # 解析 ISO 时间字符串
                from datetime import datetime
                try:
                    start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                    end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                    duration = (end - start).total_seconds()
                    durations.append(duration)
                    turn_spans.append({
                        "span_id": obs.get("id"),
                        "duration": duration,
                        "turn_number": obs.get("metadata", {}).get("turn_number"),
                    })
                except (ValueError, TypeError):
                    continue
    
    if len(durations) < 3:
        return []
    
    # 计算均值和标准差
    mean = sum(durations) / len(durations)
    variance = sum((d - mean) ** 2 for d in durations) / len(durations)
    std = variance ** 0.5
    
    # 检测异常
    threshold = mean + threshold_sigma * std
    abnormal = [
        span for span in turn_spans 
        if span["duration"] > threshold
    ]
    
    return abnormal


def detect_abnormal_tokens(
    trace_id: str,
    threshold_sigma: float = 3.0
) -> list[dict[str, Any]]:
    """检测 Token 异常。
    
    从 Langfuse Trace 获取 LLM span 的 token 使用，检测超过 mean + threshold_sigma * std 的。
    
    Returns:
        异常 token 列表，每项包含 span_id, tokens, turn_number
    """
    try:
        client = get_client()
        trace = client.fetch_trace(trace_id)
    except Exception:
        return []
    
    observations = trace.get("observations", [])
    
    # 提取所有 LLM span 的 token 使用
    tokens_list = []
    llm_spans = []
    
    for obs in observations:
        if obs.get("type") == "generation":
            usage = obs.get("usage") or obs.get("usageDetails", {})
            total_tokens = 0
            
            if isinstance(usage, dict):
                total_tokens = (
                    usage.get("total", 0) or
                    usage.get("totalTokens", 0) or
                    usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                )
            
            if total_tokens > 0:
                tokens_list.append(total_tokens)
                llm_spans.append({
                    "span_id": obs.get("id"),
                    "tokens": total_tokens,
                    "turn_number": obs.get("metadata", {}).get("turn_number"),
                })
    
    if len(tokens_list) < 3:
        return []
    
    # 计算均值和标准差
    mean = sum(tokens_list) / len(tokens_list)
    variance = sum((t - mean) ** 2 for t in tokens_list) / len(tokens_list)
    std = variance ** 0.5
    
    # 检测异常
    threshold = mean + threshold_sigma * std
    abnormal = [
        span for span in llm_spans 
        if span["tokens"] > threshold
    ]
    
    return abnormal


def detect_high_tool_failure_rate(
    trace_id: str,
    threshold: float = 0.3
) -> dict[str, Any]:
    """检测工具失败率过高。
    
    从 Langfuse Trace 获取 tool span，计算失败率。
    
    Returns:
        失败率信息，包含 total_calls, failed_calls, failure_rate, is_high
    """
    try:
        client = get_client()
        trace = client.fetch_trace(trace_id)
    except Exception:
        return {"total_calls": 0, "failed_calls": 0, "failure_rate": 0.0, "is_high": False}
    
    observations = trace.get("observations", [])
    
    # 统计工具调用
    tool_calls = 0
    tool_errors = 0
    
    for obs in observations:
        if obs.get("type") == "tool" or obs.get("name", "").startswith("tool."):
            tool_calls += 1
            if obs.get("statusMessage") or obs.get("level") == "ERROR":
                tool_errors += 1
    
    if tool_calls == 0:
        return {"total_calls": 0, "failed_calls": 0, "failure_rate": 0.0, "is_high": False}
    
    failure_rate = tool_errors / tool_calls
    
    return {
        "total_calls": tool_calls,
        "failed_calls": tool_errors,
        "failure_rate": failure_rate,
        "is_high": failure_rate > threshold,
    }


def detect_all_trace_signals(trace_id: str) -> dict[str, Any]:
    """检测所有 trace 结构信号。
    
    Returns:
        包含所有检测结果的字典
    """
    return {
        "span_errors": detect_span_errors(trace_id),
        "abnormal_duration": detect_abnormal_duration(trace_id),
        "abnormal_tokens": detect_abnormal_tokens(trace_id),
        "tool_failure_rate": detect_high_tool_failure_rate(trace_id),
    }
