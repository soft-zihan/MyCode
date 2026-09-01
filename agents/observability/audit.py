"""操作审计 — 通过 OTel Span 记录权限决策、危险操作、文件修改

Phoenix 无原生 Audit 支持，通过自定义 Span 实现。
"""

from __future__ import annotations

from .otel_exporter import otel_audit, otel_span


class AuditLogger:

    def log_permission_decision(self, tool: str, decision: str, risk_level: str = "low", reason: str = ""):
        with otel_audit(decision, tool, risk_level) as span:
            if span:
                span.set_attribute("bearcode.audit.reason", reason)

    def log_dangerous_operation(self, tool: str, input_summary: str, risk_level: str):
        with otel_span(f"audit.dangerous.{tool}", {
            "bearcode.audit.risk_level": risk_level,
            "bearcode.audit.requires_approval": risk_level in ("high", "critical"),
            "tool.input_summary": input_summary[:500],
        }) as span:
            pass

    def log_file_modification(self, file_path: str, operation: str, diff_summary: str = ""):
        with otel_span(f"audit.file.{operation}", {
            "bearcode.audit.file_path": file_path,
            "bearcode.audit.operation": operation,
            "bearcode.audit.diff_summary": diff_summary[:1000],
        }) as span:
            pass


_audit_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
