"""Tool Permissions - 工具权限检查。

提供工具级别的权限检查，包括：
- 危险命令检测
- 权限规则加载和匹配
- 权限模式检查
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agents.core.workspace import get_workspace
from agents.tools.paths import resolve_tool_path
from agents.tools.registry import READ_TOOLS, EDIT_TOOLS


DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s"),
    re.compile(r"\bgit\s+(push|reset|clean|checkout\s+\.)"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s"),
    re.compile(r">\s*/dev/"),
    re.compile(r"\bkill\b"),
    re.compile(r"\bpkill\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\bdel\s", re.IGNORECASE),
    re.compile(r"\brmdir\s", re.IGNORECASE),
    re.compile(r"\bformat\s", re.IGNORECASE),
    re.compile(r"\btaskkill\s", re.IGNORECASE),
    re.compile(r"\bRemove-Item\s", re.IGNORECASE),
    re.compile(r"\bStop-Process\s", re.IGNORECASE),
]


def is_dangerous(command: str) -> bool:
    return any(p.search(command) for p in DANGEROUS_PATTERNS)


def _parse_rule(rule: str) -> dict:
    m = re.match(r"^([a-z_]+)\((.+)\)$", rule)
    if m:
        return {"tool": m.group(1), "pattern": m.group(2)}
    return {"tool": rule, "pattern": None}


def _load_settings(file_path: Path) -> dict | None:
    if not file_path.exists():
        return None
    try:
        return json.loads(file_path.read_text())
    except Exception:
        return None


_cached_rules: dict | None = None


def load_permission_rules() -> dict:
    global _cached_rules
    if _cached_rules is not None:
        return _cached_rules

    allow: list[dict] = []
    deny: list[dict] = []

    user_settings = _load_settings(Path.home() / ".mycode" / "settings.json")
    project_settings = _load_settings(get_workspace() / ".mycode" / "settings.json")

    for settings in [user_settings, project_settings]:
        if not settings or "permissions" not in settings:
            continue
        perms = settings["permissions"]
        for r in perms.get("allow", []):
            allow.append(_parse_rule(r))
        for r in perms.get("deny", []):
            deny.append(_parse_rule(r))

    _cached_rules = {"allow": allow, "deny": deny}
    return _cached_rules


def reset_permission_cache() -> None:
    global _cached_rules
    _cached_rules = None


def _matches_rule(rule: dict, tool_name: str, inp: dict) -> bool:
    if rule["tool"] != tool_name:
        return False
    if rule["pattern"] is None:
        return True

    value = ""
    if tool_name == "run_shell":
        value = inp.get("command", "")
    elif "file_path" in inp:
        value = inp["file_path"]
    else:
        return True

    pattern = rule["pattern"]
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


def _check_permission_rules(tool_name: str, inp: dict) -> str | None:
    rules = load_permission_rules()
    for rule in rules["deny"]:
        if _matches_rule(rule, tool_name, inp):
            return "deny"
    for rule in rules["allow"]:
        if _matches_rule(rule, tool_name, inp):
            return "allow"
    return None


def check_permission(
    tool_name: str,
    inp: dict,
    mode: str = "default",
    plan_file_path: str | None = None,
    plan_dir: str | None = None,
    sub_agent_type: str | None = None,
    allowed_commands: list[str] | None = None,
    plan_execution_active: bool = False,
) -> dict:
    """Returns {"action": "allow"|"deny"|"confirm", "message": ...}"""
    from agents.observability.tracer import tracer
    
    with tracer.span("permission.check", {
        "langfuse.observation.type": "guardrail",
        "mycode.permission.tool_name": tool_name,
        "mycode.permission.mode": mode,
        "mycode.permission.sub_agent_type": sub_agent_type or "",
        "mycode.permission.plan_execution_active": plan_execution_active,
    }) as span:
        result = _check_permission_inner(tool_name, inp, mode, plan_file_path, plan_dir, sub_agent_type, allowed_commands, plan_execution_active)
        if span:
            span.set_attribute("mycode.permission.action", result["action"])
            if result.get("message"):
                span.set_attribute("mycode.permission.message", result["message"][:200])
        return result


def _check_permission_inner(
    tool_name: str,
    inp: dict,
    mode: str = "default",
    plan_file_path: str | None = None,
    plan_dir: str | None = None,
    sub_agent_type: str | None = None,
    allowed_commands: list[str] | None = None,
    plan_execution_active: bool = False,
) -> dict:
    """Internal permission check logic."""
    if mode == "bypassPermissions":
        return {"action": "allow"}

    # 执行阶段禁止直接编辑 tasks.md
    if plan_execution_active and tool_name in EDIT_TOOLS:
        file_path = inp.get("file_path") or inp.get("path") or ""
        if "tasks.md" in file_path and ".mycode/plans" in file_path:
            return {"action": "deny", "message": "Direct editing of tasks.md is forbidden during plan execution. Use mark_task_done/mark_task_failed instead."}

    # Reviewer 子 Agent 的 run_shell 白名单验证
    if sub_agent_type == "reviewer" and tool_name == "run_shell":
        command = inp.get("command", "")
        if allowed_commands:
            # 检查命令是否在白名单中
            command_allowed = False
            for allowed in allowed_commands:
                if allowed.strip() and allowed.strip() in command:
                    command_allowed = True
                    break
            if not command_allowed:
                return {"action": "deny", "message": f"Reviewer run_shell: command not in whitelist. Allowed: {allowed_commands}"}
        else:
            return {"action": "deny", "message": "Reviewer run_shell: no allowed_commands provided"}

    rule_result = _check_permission_rules(tool_name, inp)
    if rule_result == "deny":
        return {"action": "deny", "message": f"Denied by permission rule for {tool_name}"}
    if rule_result == "allow":
        return {"action": "allow"}

    if tool_name in READ_TOOLS:
        return {"action": "allow"}

    if mode == "plan":
        if tool_name == "todolist":
            return {"action": "deny", "message": "todolist is disabled in plan mode. Use the plan system's tasks.md instead."}
        if tool_name in EDIT_TOOLS:
            file_path = inp.get("file_path") or inp.get("path")
            # Plan 模式允许写 {plan_dir}/ 下任意文件
            if plan_dir and file_path:
                try:
                    from pathlib import Path
                    file_path_obj = Path(file_path).resolve()
                    plan_dir_obj = Path(plan_dir).resolve()
                    if file_path_obj.is_relative_to(plan_dir_obj):
                        return {"action": "allow"}
                except (ValueError, OSError):
                    pass
            # 向后兼容：允许写 plan_file_path
            if plan_file_path and file_path == plan_file_path:
                return {"action": "allow"}
            return {"action": "deny", "message": f"Blocked in plan mode: {tool_name}"}
        if tool_name == "run_shell":
            return {"action": "deny", "message": "Shell commands blocked in plan mode"}

    if tool_name == "enter_plan_mode":
        return {"action": "allow"}
    
    if tool_name == "exit_plan_mode":
        # Let the tool handle the confirmation internally with the plan content
        return {"action": "allow"}

    if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
        return {"action": "allow"}

    needs_confirm = False
    confirm_message = ""

    if tool_name == "run_shell" and is_dangerous(inp.get("command", "")):
        needs_confirm = True
        confirm_message = inp.get("command", "")
    elif tool_name == "write_file" and not resolve_tool_path(inp.get("file_path", ""), must_exist=False).exists():
        needs_confirm = True
        confirm_message = f"write new file: {inp.get('file_path', '')}"
    elif tool_name == "edit_file" and not resolve_tool_path(inp.get("file_path", "")).exists():
        needs_confirm = True
        confirm_message = f"edit non-existent file: {inp.get('file_path', '')}"
    elif tool_name == "skill_evolve":
        needs_confirm = True
        confirm_message = f"evolve skill: {inp.get('skill_name', '')}"
    elif tool_name == "skill_create":
        needs_confirm = True
        confirm_message = f"create skill: {inp.get('name', '')}"

    if needs_confirm:
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Auto-denied (dontAsk mode): {confirm_message}"}
        return {"action": "confirm", "message": confirm_message}

    return {"action": "allow"}
