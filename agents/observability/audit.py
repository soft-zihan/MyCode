"""操作审计 — 通过 OTel Span 记录权限决策、危险操作、文件修改

以 Langfuse guardrail observation 类型上报（langfuse.observation.type=guardrail），
可在 Langfuse UI 按类型过滤审计事件。
"""

from __future__ import annotations

from .tracer import tracer


class AuditLogger:

    def log_permission_decision(self, tool: str, decision: str, risk_level: str = "low", reason: str = ""):
        with tracer.span(f"audit.{tool}", {
            "langfuse.observation.type": "guardrail",
            "mycode.audit.decision": decision,
            "mycode.audit.risk_level": risk_level,
            "langfuse.observation.metadata.audit_reason": reason[:500],
        }) as span:
            if span:
                span.set_attribute("mycode.audit.reason", reason)

    def log_dangerous_operation(self, tool: str, input_summary: str, risk_level: str):
        with tracer.span(f"audit.dangerous.{tool}", {
            "mycode.audit.risk_level": risk_level,
            "mycode.audit.requires_approval": risk_level in ("high", "critical"),
            "tool.input_summary": input_summary[:500],
        }) as span:
            pass

    def log_file_modification(self, file_path: str, operation: str, diff_summary: str = ""):
        with tracer.span(f"audit.file.{operation}", {
            "mycode.audit.file_path": file_path,
            "mycode.audit.operation": operation,
            "mycode.audit.diff_summary": diff_summary[:1000],
        }) as span:
            pass


_audit_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger
