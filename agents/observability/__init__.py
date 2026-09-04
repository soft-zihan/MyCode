"""MyCode 可观测性模块

子模块：
- trace: JSONL 兜底日志（零依赖）
- otel_exporter: OTel Span 导出到 Phoenix
- audit: 操作审计
- cost_tracker: Token/缓存/压缩成本追踪
- tool_quality: 工具调用质量追踪
- evals: Phoenix LLM-as-Judge 评估
"""

from __future__ import annotations

import os


def init_tracing() -> None:
    """初始化可观测性系统（在应用启动时调用）"""
    from .trace import set_trace_enabled

    if os.environ.get("MYCODE_TRACE", "1") not in ("", "0"):
        set_trace_enabled(True)

    if os.environ.get("MYCODE_OTEL", "").strip() not in ("", "0"):
        try:
            from .otel_exporter import init_otel
            init_otel()
        except ImportError:
            pass

    if os.environ.get("MYCODE_PHOENIX", "").strip() not in ("", "0"):
        try:
            from openinference.instrumentation.openai import OpenAIInstrumentor
            OpenAIInstrumentor().instrument()
        except ImportError:
            pass
