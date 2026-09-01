"""StatsCollector - Agent 运行统计收集器。

收集 LLM 调用、工具执行、Token 使用等统计数据，用于前端可视化。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class StatsCollector:
    """收集 Agent 运行统计数据。
    
    用于前端 StatsBar 组件展示实时统计信息。
    
    Attributes:
        turns: 完成的对话轮次
        steps: 工具执行步骤数
        llm_ms: LLM 调用累计耗时（毫秒）
        tool_ms: 工具执行累计耗时（毫秒）
        ttft_ms: 首 Token 延迟（毫秒）
        input_tokens: 输入 Token 总数
        output_tokens: 输出 Token 总数
        cache_hits: 缓存命中次数
        cache_total: 缓存检查总次数
    """
    
    turns: int = 0
    steps: int = 0
    llm_ms: float = 0
    tool_ms: float = 0
    ttft_ms: float = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hits: int = 0
    cache_total: int = 0
    
    _llm_start: float = field(default=0, init=False, repr=False)
    _tool_start: float = field(default=0, init=False, repr=False)
    _request_start: float = field(default=0, init=False, repr=False)
    
    def start_request(self) -> None:
        """标记请求开始。"""
        self._request_start = time.time()
    
    def start_llm_call(self) -> None:
        """标记 LLM 调用开始。"""
        self._llm_start = time.time()
    
    def end_llm_call(
        self,
        input_tokens: int,
        output_tokens: int,
        ttft_ms: float,
        cache_hit: bool = False,
    ) -> None:
        """标记 LLM 调用结束。
        
        Args:
            input_tokens: 输入 Token 数
            output_tokens: 输出 Token 数
            ttft_ms: 首 Token 延迟（毫秒）
            cache_hit: 是否缓存命中
        """
        self.llm_ms += (time.time() - self._llm_start) * 1000
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.ttft_ms = ttft_ms
        if cache_hit:
            self.cache_hits += 1
        self.cache_total += 1
    
    def start_tool_execution(self) -> None:
        """标记工具执行开始。"""
        self._tool_start = time.time()
    
    def end_tool_execution(self) -> None:
        """标记工具执行结束。"""
        self.tool_ms += (time.time() - self._tool_start) * 1000
        self.steps += 1
    
    def end_turn(self) -> None:
        """标记一轮对话结束。"""
        self.turns += 1
    
    @property
    def tokens_per_sec(self) -> float:
        """计算输出 Token 速率（tokens/sec）。"""
        decode_ms = self.llm_ms - self.ttft_ms
        return (self.output_tokens / (decode_ms / 1000)) if decode_ms > 0 else 0
    
    @property
    def cache_hit_percent(self) -> float:
        """计算缓存命中率（百分比）。"""
        return (self.cache_hits / self.cache_total * 100) if self.cache_total > 0 else 0
    
    def to_dict(self) -> dict:
        """转换为字典，用于 SSE 事件传输。"""
        return {
            "turns": self.turns,
            "steps": self.steps,
            "llm_ms": round(self.llm_ms, 1),
            "tool_ms": round(self.tool_ms, 1),
            "ttft_ms": round(self.ttft_ms, 1),
            "tokens_per_sec": round(self.tokens_per_sec, 1),
            "cache_hit_percent": round(self.cache_hit_percent, 1),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
    
    def reset(self) -> None:
        """重置所有统计数据。"""
        self.turns = 0
        self.steps = 0
        self.llm_ms = 0
        self.tool_ms = 0
        self.ttft_ms = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_hits = 0
        self.cache_total = 0
