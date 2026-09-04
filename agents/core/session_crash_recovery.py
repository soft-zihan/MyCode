"""Session crash recovery - torn tail detection and auto-repair.

DeepSeek 对齐：
- Torn Tail 检测：加载时检测 seq 不连续
- 自动修复：合成缺失的闭合事件（turn/end）
- Revision 递增
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class TornTailReport:
    """Torn tail 检测报告。
    
    Attributes:
        has_torn_tail: 是否有 torn tail
        expected_seq: 期望的下一个 seq
        actual_last_seq: 实际的最后一个 seq
        gap_count: 缺失的事件数量
        interrupted_turn: 是否有中断的 turn（缺少 turn/end）
    """
    has_torn_tail: bool
    expected_seq: int
    actual_last_seq: int
    gap_count: int
    interrupted_turn: bool


def detect_torn_tail(events: list[dict[str, Any]]) -> TornTailReport:
    """检测 torn tail（seq 不连续或中断的 turn）。
    
    Args:
        events: 事件列表
    
    Returns:
        Torn tail 检测报告
    """
    if not events:
        return TornTailReport(
            has_torn_tail=False,
            expected_seq=0,
            actual_last_seq=-1,
            gap_count=0,
            interrupted_turn=False,
        )
    
    # 检查 seq 连续性
    gap_count = 0
    expected_seq = 0
    for event in events:
        seq = event.get("seq", -1)
        if seq != expected_seq:
            gap_count += 1
        expected_seq = seq + 1
    
    # 检查是否有中断的 turn（缺少 turn/end）
    interrupted_turn = False
    turn_depth = 0
    for event in events:
        event_type = event.get("type", "")
        if event_type == "turn/start":
            turn_depth += 1
        elif event_type == "turn/end":
            turn_depth -= 1
    
    # 如果 turn_depth > 0，说明有未闭合的 turn
    if turn_depth > 0:
        interrupted_turn = True
    
    has_torn_tail = gap_count > 0 or interrupted_turn
    actual_last_seq = events[-1].get("seq", -1) if events else -1
    
    return TornTailReport(
        has_torn_tail=has_torn_tail,
        expected_seq=expected_seq,
        actual_last_seq=actual_last_seq,
        gap_count=gap_count,
        interrupted_turn=interrupted_turn,
    )


def synthesize_closers(
    events: list[dict[str, Any]],
    session_id: str,
) -> list[dict[str, Any]]:
    """合成缺失的闭合事件。
    
    Args:
        events: 事件列表
        session_id: Session ID
    
    Returns:
        合成的闭合事件列表
    """
    closers = []
    
    # 检查是否有中断的 turn
    turn_depth = 0
    last_turn_start_seq = -1
    for event in events:
        event_type = event.get("type", "")
        seq = event.get("seq", -1)
        if event_type == "turn/start":
            turn_depth += 1
            last_turn_start_seq = seq
        elif event_type == "turn/end":
            turn_depth -= 1
    
    # 为每个未闭合的 turn 合成 turn/end
    if turn_depth > 0:
        next_seq = events[-1].get("seq", -1) + 1 if events else 0
        for i in range(turn_depth):
            closer = {
                "type": "turn/end",
                "time": int(time.time() * 1000),
                "session_id": session_id,
                "seq": next_seq + i,
                "name": None,
                "synthesized": True,  # 标记为合成的事件
                "reason": "crash_recovery",
            }
            closers.append(closer)
    
    return closers


def repair_session(
    events: list[dict[str, Any]],
    session_id: str,
) -> tuple[list[dict[str, Any]], TornTailReport]:
    """修复 session（检测 torn tail + 合成闭合事件）。
    
    Args:
        events: 事件列表
        session_id: Session ID
    
    Returns:
        (修复后的事件列表, 检测报告)
    """
    report = detect_torn_tail(events)
    
    if not report.has_torn_tail:
        return events, report
    
    # 合成闭合事件
    closers = synthesize_closers(events, session_id)
    
    # 追加闭合事件
    repaired_events = list(events) + closers
    
    print(f"[CRASH_RECOVERY] session={session_id}, gaps={report.gap_count}, "
          f"interrupted_turn={report.interrupted_turn}, synthesized={len(closers)} closers")
    
    return repaired_events, report


def validate_and_repair_events(
    events: list[dict[str, Any]],
    session_id: str,
) -> list[dict[str, Any]]:
    """验证并修复事件列表（用于加载时）。
    
    Args:
        events: 原始事件列表
        session_id: Session ID
    
    Returns:
        修复后的事件列表
    """
    repaired_events, report = repair_session(events, session_id)
    
    if report.has_torn_tail:
        print(f"[CRASH_RECOVERY] Repaired session {session_id}: "
              f"gaps={report.gap_count}, interrupted_turn={report.interrupted_turn}")
    
    return repaired_events
