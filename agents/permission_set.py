"""PermissionSet - 细粒度权限控制系统。

替代简单的工具白名单，支持基于规则的权限检查：
- 按 action 类型（read/edit/bash/grep 等）
- 按 resource 路径模式（支持通配符）
- 按 effect（allow/deny/ask）

与 Agent Mode 系统集成，不同模式使用不同权限集。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class PermissionRule:
    """权限规则。
    
    Attributes:
        action: 动作类型 ("read" | "edit" | "bash" | "grep" | "*" 等)
        resource: 资源路径模式，"*" 表示所有
        effect: 效果 ("allow" | "deny" | "ask")
    """
    
    action: str
    resource: str
    effect: Literal["allow", "deny", "ask"]


@dataclass
class PermissionCheckResult:
    """权限检查结果。
    
    Attributes:
        action: 检查结果 ("allow" | "deny" | "ask")
        message: 附加消息（如拒绝原因）
    """
    
    action: Literal["allow", "deny", "ask"]
    message: str = ""


class PermissionSet:
    """细粒度权限规则集。
    
    规则按顺序匹配，后定义的规则优先级更高（类似 CSS 层叠）。
    """
    
    def __init__(self, rules: list[PermissionRule] | None = None) -> None:
        self.rules = rules or []
    
    def check(self, action: str, resource: str = "") -> PermissionCheckResult:
        """检查权限。
        
        Args:
            action: 动作类型
            resource: 资源路径
        
        Returns:
            PermissionCheckResult: 检查结果
        """
        for rule in reversed(self.rules):
            if self._matches(rule.action, action) and self._matches(rule.resource, resource):
                return PermissionCheckResult(action=rule.effect)
        return PermissionCheckResult(action="ask")
    
    def _matches(self, pattern: str, value: str) -> bool:
        """检查模式是否匹配值。
        
        Args:
            pattern: 模式（支持 * 通配符）
            value: 要匹配的值
        
        Returns:
            bool: 是否匹配
        """
        if pattern == "*":
            return True
        if not value:
            return pattern == value
        return fnmatch.fnmatch(value, pattern)
    
    def add_rule(self, rule: PermissionRule) -> None:
        """添加规则。"""
        self.rules.append(rule)
    
    def remove_rule(self, index: int) -> None:
        """移除规则。"""
        if 0 <= index < len(self.rules):
            self.rules.pop(index)


# ============================================================
# 预定义规则集
# ============================================================


READONLY_RULESET = PermissionSet([
    PermissionRule("*", "*", "deny"),
    PermissionRule("read", "*", "allow"),
    PermissionRule("grep", "*", "allow"),
    PermissionRule("list", "*", "allow"),
])

EDIT_RULESET = PermissionSet([
    PermissionRule("*", "*", "deny"),
    PermissionRule("read", "*", "allow"),
    PermissionRule("edit", "*", "allow"),
    PermissionRule("write", "*", "allow"),
    PermissionRule("grep", "*", "allow"),
    PermissionRule("list", "*", "allow"),
    PermissionRule("bash", "*", "ask"),
    PermissionRule("skill", "*", "ask"),
    PermissionRule("mcp", "*", "ask"),
    PermissionRule("agent", "*", "ask"),
    PermissionRule("ask", "*", "ask"),
])

FULL_RULESET = PermissionSet([
    PermissionRule("*", "*", "allow"),
])


# ============================================================
# Agent Mode 与 PermissionSet 映射
# ============================================================


MODE_PERMISSIONS: dict[str, PermissionSet | dict[str, PermissionSet]] = {
    "primary": EDIT_RULESET,
    "subagent": {
        "explore": READONLY_RULESET,
        "plan": READONLY_RULESET,
        "general": EDIT_RULESET,
    },
    "all": EDIT_RULESET,
}


def get_permission_set_for_agent(mode: str, agent_type: str | None = None) -> PermissionSet:
    """根据 Agent Mode 和类型获取 PermissionSet。
    
    Args:
        mode: Agent 模式 ("primary" | "subagent" | "all")
        agent_type: 子 Agent 类型（仅当 mode="subagent" 时使用）
    
    Returns:
        PermissionSet: 对应的权限集
    """
    if mode == "subagent" and agent_type:
        subagent_perms = MODE_PERMISSIONS.get("subagent", {})
        if isinstance(subagent_perms, dict):
            return subagent_perms.get(agent_type, READONLY_RULESET)
    perm = MODE_PERMISSIONS.get(mode, EDIT_RULESET)
    if isinstance(perm, PermissionSet):
        return perm
    return EDIT_RULESET


# ============================================================
# 与旧 permission_mode 的兼容映射
# ============================================================


LEGACY_MODE_MAP: dict[str, PermissionSet] = {
    "default": EDIT_RULESET,
    "bypassPermissions": FULL_RULESET,
    "acceptEdits": EDIT_RULESET,
    "dontAsk": READONLY_RULESET,
    "plan": READONLY_RULESET,
}


def get_permission_set_from_legacy_mode(mode: str) -> PermissionSet:
    """从旧版 permission_mode 获取 PermissionSet。
    
    用于向后兼容旧的权限模式字符串。
    
    Args:
        mode: 旧版权限模式
    
    Returns:
        PermissionSet: 对应的权限集
    """
    return LEGACY_MODE_MAP.get(mode, EDIT_RULESET)
