"""工具调用检测模块。

实时检测工具调用重复和循环，在工具执行时注入警告提示。
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallTracker:
    """跟踪工具调用，检测重复调用和循环。
    
    设计：
    - 重复检测：1 分钟内相同工具+参数调用 5 次
    - 循环检测：A→B→A→B 或 A→B→C→A→B→C 模式
    """
    
    call_history: dict[tuple[str, str], list[float]] = field(default_factory=lambda: defaultdict(list))
    tool_sequence: list[str] = field(default_factory=list)
    
    def record_call(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """记录工具调用，返回警告信息或 None。
        
        Args:
            tool_name: 工具名称
            args: 工具参数
        
        Returns:
            警告信息字符串，或 None 表示无警告
        """
        now = time.time()
        args_key = json.dumps(args, sort_keys=True, ensure_ascii=False)
        key = (tool_name, args_key)
        
        # 清理 1 分钟前的记录
        self.call_history[key] = [
            t for t in self.call_history[key]
            if now - t < 60
        ]
        
        # 添加当前调用
        self.call_history[key].append(now)
        
        # 记录工具序列（用于循环检测）
        self.tool_sequence.append(tool_name)
        if len(self.tool_sequence) > 10:
            self.tool_sequence = self.tool_sequence[-10:]
        
        # 检查重复调用
        if len(self.call_history[key]) >= 5:
            count = len(self.call_history[key])
            return f"这是第 {count} 次重复的调用，请注意是否必要"
        
        return None
    
    def detect_cycle(self) -> str | None:
        """检测工具调用循环。
        
        检测模式：
        - 2 工具循环：A→B→A→B
        - 3 工具循环：A→B→C→A→B→C
        
        Returns:
            循环描述字符串，或 None 表示无循环
        """
        if len(self.tool_sequence) < 4:
            return None
        
        # 检测 2 工具循环：A→B→A→B
        last_4 = self.tool_sequence[-4:]
        if (last_4[0] == last_4[2] and 
            last_4[1] == last_4[3] and 
            last_4[0] != last_4[1]):
            return f"检测到工具调用循环：{last_4[0]} → {last_4[1]} → {last_4[0]} → {last_4[1]}"
        
        # 检测 3 工具循环：A→B→C→A→B→C
        if len(self.tool_sequence) >= 6:
            last_6 = self.tool_sequence[-6:]
            if (last_6[0] == last_6[3] and 
                last_6[1] == last_6[4] and 
                last_6[2] == last_6[5] and
                len(set(last_6[:3])) == 3):
                return f"检测到工具调用循环：{last_6[0]} → {last_6[1]} → {last_6[2]} → ..."
        
        return None
    
    def reset(self) -> None:
        """重置跟踪器（新 turn 开始时调用）。"""
        self.call_history.clear()
        self.tool_sequence.clear()


def check_tool_warnings(tracker: ToolCallTracker, tool_name: str, args: dict[str, Any]) -> list[str]:
    """检查工具调用警告。
    
    Args:
        tracker: 工具调用跟踪器
        tool_name: 工具名称
        args: 工具参数
    
    Returns:
        警告信息列表
    """
    warnings = []
    
    # 记录调用并检查重复
    repeat_warning = tracker.record_call(tool_name, args)
    if repeat_warning:
        warnings.append(repeat_warning)
    
    # 检查循环
    cycle_warning = tracker.detect_cycle()
    if cycle_warning:
        warnings.append(cycle_warning)
    
    return warnings
