"""ToolExecutor - 工具执行器。

负责：
1. 权限检查（通过 PermissionSet）
2. 按 execution_mode 分组（sequential/parallel）
3. 执行工具并返回结果

替代原有的全局 CONCURRENCY_SAFE_TOOLS 机制，每个工具可声明自己的执行模式。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from agents.permissions import PermissionCheckResult, PermissionSet


@dataclass
class ToolResult:
    """工具执行结果。
    
    Attributes:
        call_id: 工具调用 ID
        name: 工具名称
        result: 执行结果
        status: 执行状态 ("ok" | "error" | "denied" | "cancelled")
        snapshot: 文件变更快照（用于 /rewind）
    """
    
    call_id: str
    name: str
    result: str
    status: Literal["ok", "error", "denied", "cancelled"]
    snapshot: dict | None = None


@dataclass
class ToolCall:
    """工具调用请求。
    
    Attributes:
        call_id: 调用 ID
        name: 工具名称
        input: 输入参数
        execution_mode: 执行模式 ("sequential" | "parallel")
    """
    
    call_id: str
    name: str
    input: dict[str, Any]
    execution_mode: str = "sequential"


class ToolExecutor:
    """工具执行器。
    
    负责权限检查、执行模式分组、工具执行。
    """
    
    def __init__(
        self,
        permission_set: PermissionSet,
        confirm_callback: Callable[[str], Awaitable[bool]] | None = None,
        tool_registry: Any | None = None,
        execute_callback: Callable[[str, dict], Awaitable[str]] | None = None,
    ) -> None:
        """初始化工具执行器。
        
        Args:
            permission_set: 权限集
            confirm_callback: 权限确认回调（Web 端使用）
            tool_registry: 工具注册表
            execute_callback: 工具执行回调（Agent 使用，传入 _execute_tool_call）
        """
        self.permission_set = permission_set
        self.confirm_callback = confirm_callback
        self.tool_registry = tool_registry
        self.execute_callback = execute_callback
    
    async def execute_batch(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """批量执行工具调用。
        
        按 execution_mode 分组：
        - parallel 工具连续执行
        - sequential 工具等待前一组完成
        
        Args:
            tool_calls: 工具调用列表
        
        Returns:
            list[ToolResult]: 执行结果列表
        """
        results: list[ToolResult] = []
        parallel_group: list[ToolCall] = []
        
        for tc in tool_calls:
            # 权限检查
            perm_result = await self._check_permission(tc)
            if perm_result.action == "deny":
                results.append(ToolResult(
                    call_id=tc.call_id,
                    name=tc.name,
                    result=f"Denied: {perm_result.message}",
                    status="denied",
                ))
                continue
            
            if perm_result.action == "ask":
                confirmed = await self._confirm(tc, perm_result.message)
                if not confirmed:
                    results.append(ToolResult(
                        call_id=tc.call_id,
                        name=tc.name,
                        result="User denied",
                        status="denied",
                    ))
                    continue
            
            # 按 execution_mode 分组
            if tc.execution_mode == "parallel":
                parallel_group.append(tc)
            else:
                # 先执行之前的并行组
                if parallel_group:
                    results.extend(await self._execute_parallel(parallel_group))
                    parallel_group = []
                # 顺序执行当前工具
                results.append(await self._execute_single(tc))
        
        # 执行最后的并行组
        if parallel_group:
            results.extend(await self._execute_parallel(parallel_group))
        
        return results
    
    async def _check_permission(self, tc: ToolCall) -> PermissionCheckResult:
        """检查工具权限。
        
        Args:
            tc: 工具调用
        
        Returns:
            PermissionCheckResult: 权限检查结果
        """
        # 从工具名推断 action 类型
        action = self._infer_action_type(tc.name)
        resource = self._infer_resource(tc.input)
        return self.permission_set.check(action, resource)
    
    def _infer_action_type(self, tool_name: str) -> str:
        """从工具名推断 action 类型。
        
        Args:
            tool_name: 工具名称
        
        Returns:
            str: action 类型
        """
        if tool_name in ("read_file", "list_files"):
            return "read"
        if tool_name in ("write_file", "edit_file"):
            return "edit"
        if tool_name in ("run_shell",):
            return "bash"
        if tool_name in ("grep_search",):
            return "grep"
        # Skill、MCP、子智能体等工具需要确认
        if tool_name in ("skill", "skill_create", "skill_evolve"):
            return "skill"
        if tool_name.startswith("mcp__"):
            return "mcp"
        if tool_name == "agent":
            return "agent"
        # 其他未知工具默认需要确认
        return "ask"
    
    def _infer_resource(self, input: dict[str, Any]) -> str:
        """从输入推断 resource 路径。
        
        Args:
            input: 工具输入参数
        
        Returns:
            str: resource 路径
        """
        # 尝试从常见参数名获取路径
        for key in ("path", "file", "file_path", "directory"):
            if key in input:
                return str(input[key])
        return ""
    
    async def _confirm(self, tc: ToolCall, message: str) -> bool:
        """请求用户确认。
        
        Args:
            tc: 工具调用
            message: 确认消息
        
        Returns:
            bool: 是否确认
        """
        # 显示确认对话框
        from agents.ui import print_confirmation
        # 构建命令显示
        if tc.name == "bash":
            cmd_display = tc.input.get("command", str(tc.input))
        elif tc.name == "skill":
            cmd_display = f"skill: {tc.input.get('skill_name', '')}"
        elif tc.name == "agent":
            cmd_display = f"agent: {tc.input.get('type', '')} - {tc.input.get('description', '')}"
        elif tc.name.startswith("mcp__"):
            cmd_display = f"mcp: {tc.name}"
        else:
            cmd_display = f"{tc.name}: {message}"
        
        print_confirmation(cmd_display)
        
        if self.confirm_callback:
            return await self.confirm_callback(message)
        # CLI 模式：阻塞等待输入
        return await self._confirm_cli(message)
    
    async def _confirm_cli(self, message: str) -> bool:
        """CLI 模式确认。
        
        Args:
            message: 确认消息
        
        Returns:
            bool: 是否确认
        """
        print(f"\n[Permission Request] {message}")
        try:
            loop = asyncio.get_event_loop()
            answer = await loop.run_in_executor(None, input, "  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False
    
    async def _execute_parallel(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """并行执行一组工具。
        
        Args:
            tool_calls: 工具调用列表
        
        Returns:
            list[ToolResult]: 执行结果列表
        """
        tasks = [self._execute_single(tc) for tc in tool_calls]
        return list(await asyncio.gather(*tasks))
    
    async def _execute_single(self, tc: ToolCall) -> ToolResult:
        """执行单个工具。
        
        Args:
            tc: 工具调用
        
        Returns:
            ToolResult: 执行结果
        """
        try:
            # 优先使用执行回调（Agent 的 _execute_tool_call）
            if self.execute_callback:
                result = await self.execute_callback(tc.name, tc.input)
            # 其次使用工具注册表
            elif self.tool_registry:
                result = await self.tool_registry.execute(tc.name, tc.input)
            else:
                # 回退到旧的 execute_tool
                from agents.tools import execute_tool
                result = await execute_tool(tc.name, tc.input)
            
            return ToolResult(
                call_id=tc.call_id,
                name=tc.name,
                result=str(result),
                status="ok",
            )
        except Exception as e:
            return ToolResult(
                call_id=tc.call_id,
                name=tc.name,
                result=str(e),
                status="error",
            )


# ============================================================
# 辅助函数
# ============================================================


def create_tool_call(
    call_id: str,
    name: str,
    input: dict[str, Any],
    execution_mode: str = "sequential",
) -> ToolCall:
    """创建工具调用。
    
    Args:
        call_id: 调用 ID
        name: 工具名称
        input: 输入参数
        execution_mode: 执行模式
    
    Returns:
        ToolCall: 工具调用对象
    """
    return ToolCall(
        call_id=call_id,
        name=name,
        input=input,
        execution_mode=execution_mode,
    )
