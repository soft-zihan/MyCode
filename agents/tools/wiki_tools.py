"""Wiki 工具 — 供 Agent 调用的 Wiki 操作。"""

from __future__ import annotations

from agents.wiki.wiki_manager import write_workflow_pattern as _write_workflow_pattern


def write_workflow_pattern(inp: dict) -> str:
    """创建一个 workflow_pattern 条目。

    Args:
        inp: {
            "name": str,          # 模式名称（如 "db-connection-timeout"）
            "symptom": str,       # 症状描述
            "root_cause": str,    # 根本原因
            "workaround": str,    # 解决方案/排查步骤
            "description": str,   # 可选，简短描述
        }

    Returns:
        创建结果消息
    """
    try:
        name = inp.get("name", "")
        symptom = inp.get("symptom", "")
        root_cause = inp.get("root_cause", "")
        workaround = inp.get("workaround", "")
        description = inp.get("description", "")

        if not name:
            return "Error: name is required"
        if not symptom:
            return "Error: symptom is required"
        if not root_cause:
            return "Error: root_cause is required"
        if not workaround:
            return "Error: workaround is required"

        path = _write_workflow_pattern(
            name=name,
            symptom=symptom,
            root_cause=root_cause,
            workaround=workaround,
            description=description or symptom[:80],
        )
        return f"Created workflow_pattern: {path}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"
