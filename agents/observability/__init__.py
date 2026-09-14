"""MyCode 可观测性模块

子模块：
- trace: JSONL 兜底日志（零依赖）+ trace_span/trace_event 业务入口
- tracer: OTel Span 导出到 Langfuse（OTLP/HTTP）
- otel_exporter: 导出器初始化封装
- audit: 操作审计（guardrail span）
- cost_tracker: Token/缓存/压缩成本追踪
- tool_quality: 工具调用质量追踪
"""

from __future__ import annotations

import os


def init_tracing() -> None:
    """初始化可观测性系统（在应用启动时调用）。

    OTel/Langfuse 初始化失败不阻塞主流程（观测是旁路，不能拖垮 Agent）。
    """
    from .trace import set_trace_enabled

    if os.environ.get("MYCODE_TRACE", "1") not in ("", "0"):
        set_trace_enabled(True)

    if os.environ.get("MYCODE_OTEL", "").strip() not in ("", "0"):
        try:
            from .otel_exporter import init_otel
            init_otel()
        except ImportError:
            print("[observability] opentelemetry 未安装，跳过 OTel 导出")
        except RuntimeError as e:
            print(f"[observability] OTel 初始化失败（{e}），Span 导出已禁用")

    try:
        from .cost_tracker import init_cost_metrics
        init_cost_metrics()
    except ImportError:
        pass


def shutdown_tracing() -> None:
    """flush 并关闭 OTel 导出（在应用退出时调用）。"""
    try:
        from .tracer import shutdown_tracer
        shutdown_tracer()
    except ImportError:
        pass
