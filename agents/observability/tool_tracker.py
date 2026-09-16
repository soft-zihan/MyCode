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
    - 重复检测：1 分钟内相同工具+参数调用 5 次触发警告
    - 走歪路检测：警告后仍重复调用（第 7 次）视为 bad case
    - 循环检测：A→B→A→B 或 A→B→C→A→B→C 模式，警告后仍循环视为 bad case
    - 硬兜底：任何工具调用达到 10 次强行停止
    """
    
    call_history: dict[tuple[str, str], list[float]] = field(default_factory=lambda: defaultdict(list))
    tool_sequence: list[str] = field(default_factory=list)
    warned_keys: set[tuple[str, str]] = field(default_factory=set)  # 已警告的重复调用 key
    cycle_warned: bool = False  # 是否已警告过循环
    cycle_count_after_warn: int = 0  # 警告后循环次数
    
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
        
        count = len(self.call_history[key])
        
        # 第 5 次：触发警告
        if count == 5:
            self.warned_keys.add(key)
            return f"这是第 {count} 次重复的调用，请注意是否必要"
        
        return None
    
    def is_going_off_track(self, tool_name: str, args: dict[str, Any]) -> bool:
        """检测是否走歪路（警告后仍重复调用）。
        
        警告后继续调用同一工具超过 2 次（总计第 7 次），视为走歪路。
        
        Returns:
            True 表示走歪路，应标记为 bad case
        """
        args_key = json.dumps(args, sort_keys=True, ensure_ascii=False)
        key = (tool_name, args_key)
        
        # 已警告过，且继续调用超过 2 次（总计第 7 次）
        if key in self.warned_keys and len(self.call_history[key]) >= 7:
            return True
        
        return False
    
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
            cycle_desc = f"检测到工具调用循环：{last_4[0]} → {last_4[1]} → {last_4[0]} → {last_4[1]}"
            # 如果已经警告过，增加循环计数
            if self.cycle_warned:
                self.cycle_count_after_warn += 1
            else:
                # 首次检测到循环，标记为已警告
                self.cycle_warned = True
            return cycle_desc
        
        # 检测 3 工具循环：A→B→C→A→B→C
        if len(self.tool_sequence) >= 6:
            last_6 = self.tool_sequence[-6:]
            if (last_6[0] == last_6[3] and 
                last_6[1] == last_6[4] and 
                last_6[2] == last_6[5] and
                len(set(last_6[:3])) == 3):
                cycle_desc = f"检测到工具调用循环：{last_6[0]} → {last_6[1]} → {last_6[2]} → ..."
                # 如果已经警告过，增加循环计数
                if self.cycle_warned:
                    self.cycle_count_after_warn += 1
                else:
                    # 首次检测到循环，标记为已警告
                    self.cycle_warned = True
                return cycle_desc
        
        return None
    
    def is_cycle_bad_case(self) -> bool:
        """检测是否因循环而成为 bad case。
        
        警告后仍继续循环 2 次，视为 bad case。
        
        Returns:
            True 表示应标记为 bad case
        """
        # 已警告过循环，且警告后又循环了 2 次
        return self.cycle_warned and self.cycle_count_after_warn >= 2
    
    def should_force_stop(self) -> bool:
        """检测是否应该强行停止（硬兜底）。
        
        任何工具调用达到 10 次，强行停止。
        
        Returns:
            True 表示应该强行停止
        """
        for key, timestamps in self.call_history.items():
            if len(timestamps) >= 10:
                return True
        return False
    
    def get_force_stop_info(self) -> dict[str, Any] | None:
        """获取强行停止的信息。
        
        Returns:
            包含工具名称和调用次数的字典，或 None
        """
        for key, timestamps in self.call_history.items():
            if len(timestamps) >= 10:
                tool_name, args_key = key
                return {
                    "tool": tool_name,
                    "count": len(timestamps),
                    "args": json.loads(args_key) if args_key else {}
                }
        return None
    
    def reset(self) -> None:
        """重置跟踪器（新 turn 开始时调用）。"""
        self.call_history.clear()
        self.tool_sequence.clear()
        self.warned_keys.clear()
        self.cycle_warned = False
        self.cycle_count_after_warn = 0


def check_tool_warnings(tracker: ToolCallTracker, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """检查工具调用警告。
    
    Args:
        tracker: 工具调用跟踪器
        tool_name: 工具名称
        args: 工具参数
    
    Returns:
        包含以下字段：
        - warnings: 警告信息列表
        - going_off_track: 是否走歪路（重复调用 bad case）
        - cycle_bad_case: 是否循环 bad case
        - force_stop: 是否应该强行停止（硬兜底）
        - force_stop_info: 强行停止的信息
    """
    result = {
        "warnings": [],
        "going_off_track": False,
        "cycle_bad_case": False,
        "force_stop": False,
        "force_stop_info": None,
    }
    
    # 记录调用并检查重复
    repeat_warning = tracker.record_call(tool_name, args)
    if repeat_warning:
        result["warnings"].append(repeat_warning)
    
    # 检查循环
    cycle_warning = tracker.detect_cycle()
    if cycle_warning:
        result["warnings"].append(cycle_warning)
    
    # 检查走歪路（重复调用）
    if tracker.is_going_off_track(tool_name, args):
        result["going_off_track"] = True
    
    # 检查循环 bad case
    if tracker.is_cycle_bad_case():
        result["cycle_bad_case"] = True
    
    # 检查硬兜底
    if tracker.should_force_stop():
        result["force_stop"] = True
        result["force_stop_info"] = tracker.get_force_stop_info()
    
    return result
