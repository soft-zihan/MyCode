"""上下文成本追踪 — OTel Metric 记录 Token 消耗、缓存命中率、压缩保留比例

Token 用量通过 OTel Span Attributes 自动采集（otel_exporter.otel_model_call）。
缓存命中率和压缩保留比例通过 OTel Metric 补充。
"""

from __future__ import annotations

from dataclasses import dataclass, field

_meter = None
_token_counter = None
_cache_hit_counter = None
_compression_ratio_histogram = None


def init_cost_metrics() -> None:
    global _meter, _token_counter, _cache_hit_counter, _compression_ratio_histogram

    try:
        from opentelemetry import metrics
    except ImportError:
        return

    _meter = metrics.get_meter("MyCode.cost")
    _token_counter = _meter.create_counter(
        "MyCode.tokens.total",
        description="Total token usage",
        unit="tokens",
    )
    _cache_hit_counter = _meter.create_counter(
        "MyCode.tokens.cached",
        description="Cached token count",
        unit="tokens",
    )
    _compression_ratio_histogram = _meter.create_histogram(
        "MyCode.context.compression_ratio",
        description="Context compression retention ratio",
        unit="ratio",
    )


def record_tokens(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0):
    if _token_counter:
        _token_counter.add(input_tokens, {"model": model, "direction": "input"})
        _token_counter.add(output_tokens, {"model": model, "direction": "output"})
    if _cache_hit_counter and cached_tokens > 0:
        _cache_hit_counter.add(cached_tokens, {"model": model})


def record_compression(before_tokens: int, after_tokens: int):
    if _compression_ratio_histogram and before_tokens > 0:
        ratio = after_tokens / before_tokens
        _compression_ratio_histogram.record(ratio, {
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "tokens_saved": before_tokens - after_tokens,
        })


@dataclass
class CostSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model_calls: int = 0
    compactions: int = 0
    compaction_before: int = 0
    compaction_after: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        return self.cached_tokens / self.input_tokens if self.input_tokens > 0 else 0.0

    @property
    def avg_tokens_per_call(self) -> float:
        return self.total_tokens / self.model_calls if self.model_calls > 0 else 0.0

    @property
    def compression_retention(self) -> float:
        return self.compaction_after / self.compaction_before if self.compaction_before > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "avg_tokens_per_call": round(self.avg_tokens_per_call, 1),
            "model_calls": self.model_calls,
            "compactions": self.compactions,
            "compression_retention": round(self.compression_retention, 4),
        }


_summary = CostSummary()


def get_cost_summary() -> CostSummary:
    return _summary


def reset_cost_summary() -> None:
    global _summary
    _summary = CostSummary()
