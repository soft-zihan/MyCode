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
from agents.observability.trace import trace_span


# plan 模式下拒绝 task_list 的文案。**单一来源**，两个消费者：
# - _check_plan_mode（本模块）——权限门，正常路径上先拒的那个；
# - dispatcher 的 task_list 分支——很可能是不可达的纵深防御（权限门先拒），但删它
#   需要先证明不可达（要穷举所有调用路径），所以留着，只收敛文案。
# 此前两处逐字重复、只差 `Error: ` 前缀：改一处忘另一处，模型在两条路径上就会读到
# 两种说法，而这两条路径本该是同一件事。
PLAN_MODE_TASK_LIST_DENIAL = (
    "task_list is disabled in plan mode. Write the plan into tasks.md; "
    "approved tasks are materialized into task_list automatically."
)


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


# Plan mode 下允许的只读命令
READONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "less", "more",
    "find", "grep", "egrep", "fgrep",
    "git log", "git show", "git diff", "git status", "git branch", "git remote",
    "pwd", "echo", "wc", "file", "du", "df",
    "tree", "stat",
    "python -c", "python3 -c",  # 允许 python 单行脚本（用于查看）
}


def _is_readonly_shell_command(command: str) -> bool:
    """检查 shell 命令是否为只读命令。"""
    if not command:
        return False
    cmd_lower = command.strip().lower()
    # 检查是否以只读命令开头
    for readonly in READONLY_COMMANDS:
        if cmd_lower.startswith(readonly):
            # 排除危险操作（如 cat > file）
            if ">" in command or "|" in command:
                # 允许管道到 grep/less/more/head/tail
                pipe_safe = False
                if "|" in command:
                    parts = command.split("|")
                    if len(parts) >= 2:
                        last_part = parts[-1].strip().lower()
                        for safe in ["grep", "less", "more", "head", "tail", "wc", "sort", "uniq"]:
                            if last_part.startswith(safe):
                                pipe_safe = True
                                break
                    if not pipe_safe:
                        return False
                if ">" in command:
                    return False
            return True
    return False


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
    plan_dir: str | None = None,
    sub_agent_type: str | None = None,
    allowed_commands: list[str] | None = None,
    plan_execution_active: bool = False,
) -> dict:
    """Returns {"action": "allow"|"deny"|"confirm", "message": ...}"""
    if mode == "bypassPermissions":
        return _check_permission_inner(tool_name, inp, mode, plan_dir, sub_agent_type, allowed_commands, plan_execution_active)


    permission_input = {
        "tool_name": tool_name,
        "mode": mode,
        "sub_agent_type": sub_agent_type,
        "plan_execution_active": plan_execution_active,
        "input": inp,
    }
    with trace_span(
        "permission.check",
        name=f"permission.{tool_name}",
        input=json.dumps(permission_input, ensure_ascii=False, default=str)[:4000],
        metadata={
            "tool_name": tool_name,
            "mode": mode,
            "sub_agent_type": sub_agent_type or "",
            "plan_execution_active": plan_execution_active,
        },
    ) as span:
        result = _check_permission_inner(tool_name, inp, mode, plan_dir, sub_agent_type, allowed_commands, plan_execution_active)
        metadata: dict[str, Any] = {"action": result["action"]}
        if result.get("message"):
            metadata["message"] = result["message"][:500]
        span.update(
            output=json.dumps(result, ensure_ascii=False, default=str)[:2000],
            metadata=metadata,
        )
        return result


def _check_reviewer_shell(
    tool_name: str,
    inp: dict,
    sub_agent_type: str | None,
    allowed_commands: list[str] | None,
) -> dict | None:
    """Reviewer 子 Agent 的 run_shell 白名单验证。"""
    if sub_agent_type != "reviewer" or tool_name != "run_shell":
        return None
    command = inp.get("command", "")
    if not allowed_commands:
        return {"action": "deny", "message": "Reviewer run_shell: no allowed_commands provided"}
    # 检查命令是否在白名单中
    for allowed in allowed_commands:
        if allowed.strip() and allowed.strip() in command:
            return None
    return {"action": "deny", "message": f"Reviewer run_shell: command not in whitelist. Allowed: {allowed_commands}"}


def _check_plan_mode(tool_name: str, inp: dict, plan_dir: str | None) -> dict | None:
    """plan 模式下的工具裁决；返回 None 表示无特殊裁决，继续走通用流程。"""
    if tool_name == "task_list":
        return {"action": "deny", "message": PLAN_MODE_TASK_LIST_DENIAL}
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
        return {"action": "deny", "message": f"Blocked in plan mode: {tool_name}"}
    if tool_name == "run_shell":
        command = inp.get("command", "")
        # Plan mode 允许只读命令
        if _is_readonly_shell_command(command):
            return {"action": "allow"}
        return {"action": "deny", "message": "Shell commands blocked in plan mode (only read-only commands allowed)"}
    return None


def _check_needs_confirmation(tool_name: str, inp: dict, mode: str) -> dict | None:
    """有副作用的操作需要确认；dontAsk 模式自动拒绝。返回 None 表示无需确认。"""
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
    return None


def _check_permission_inner(
    tool_name: str,
    inp: dict,
    mode: str = "default",
    plan_dir: str | None = None,
    sub_agent_type: str | None = None,
    allowed_commands: list[str] | None = None,
    plan_execution_active: bool = False,
) -> dict:
    """Internal permission check logic."""
    if mode == "bypassPermissions":
        return {"action": "allow"}

    # Reviewer 子 Agent 的 run_shell 白名单验证
    reviewer = _check_reviewer_shell(tool_name, inp, sub_agent_type, allowed_commands)
    if reviewer:
        return reviewer

    rule_result = _check_permission_rules(tool_name, inp)
    if rule_result == "deny":
        return {"action": "deny", "message": f"Denied by permission rule for {tool_name}"}
    if rule_result == "allow":
        return {"action": "allow"}

    if tool_name in READ_TOOLS:
        return {"action": "allow"}

    if mode == "plan":
        plan_result = _check_plan_mode(tool_name, inp, plan_dir)
        if plan_result:
            return plan_result

    if tool_name in ("enter_plan_mode", "exit_plan_mode"):
        # Let the tool handle the confirmation internally with the plan content
        return {"action": "allow"}

    if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
        return {"action": "allow"}

    confirmation = _check_needs_confirmation(tool_name, inp, mode)
    if confirmation:
        return confirmation

    return {"action": "allow"}
