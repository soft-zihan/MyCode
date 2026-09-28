"""Session crash recovery - torn tail detection and auto-repair.

DeepSeek 对齐：
- Torn Tail 检测：加载时检测 seq 不连续
- 自动修复：合成缺失的闭合事件（turn/end）
- Revision 递增

U6（v2 execution.ts / restart.ts 对齐，保守版）：
- 启动扫描：projcache running=True 的会话经事件流核验后，落盘
  turn/end{reason:"interrupted"} + session/interrupted（不自动续跑，等用户决定）
- 优雅 shutdown：abort 活跃 agent → 超时未收尾者合成 turn/end{reason:"shutdown"}
- claim 语义靠"turn/start 无配对闭合事件即悬挂"从事件流派生，不加新表
  （结构化 claim 表归 U7 sqlite-first 决策点）
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from agents.core.session import session_dir, Session, get_session_backend
from agents.session_manager import get_session_manager

# 闭合 turn 的事件集合（D4 一致性：abort 端点写 turn/cancel，同样终结 running）
_TURN_CLOSER_TYPES = ("turn/end", "turn/cancel")


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
    
    # 检查是否有中断的 turn（缺少闭合事件）
    interrupted_turn = False
    turn_depth = 0
    for event in events:
        event_type = event.get("type", "")
        if event_type == "turn/start":
            turn_depth += 1
        elif event_type in _TURN_CLOSER_TYPES:
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
        elif event_type in _TURN_CLOSER_TYPES:
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


# ============================================================
# U6：启动扫描 + 优雅 shutdown
# ============================================================


def unpaired_turn_depth(events: list[dict[str, Any]]) -> int:
    """事件流中未闭合的 turn 数（claim 语义：turn/start 无配对闭合即悬挂）。"""
    depth = 0
    for event in events:
        t = event.get("type", "")
        if t == "turn/start":
            depth += 1
        elif t in _TURN_CLOSER_TYPES:
            depth -= 1
    return max(depth, 0)


def _projcache_running_candidates() -> list[str]:
    """projcache running=True 的会话 ID（廉价初筛，事件流才是最终裁决）。"""
    import json


    candidates = []
    for f in session_dir().glob("*.projcache.json"):
        try:
            rows = json.loads(f.read_text()).get("rows", {})
        except (OSError, json.JSONDecodeError) as e:
            print(f"[CRASH_RECOVERY] projcache 读取失败 {f.name}: {e!r}")
            continue
        if (rows.get("running") or {}).get("val") is True:
            candidates.append(f.name[: -len(".projcache.json")])
    return candidates


def _refresh_projection_cache(session: Any) -> None:
    """从事件流全量重折叠投影并落盘 checkpoint（清理脏 running 缓存）。"""
    from .session_projection_cache import (
        ProjectionCheckpoint,
        get_projection_cache,
        get_projection_registry,
    )

    registry = get_projection_registry()
    state = registry.fold_events(registry.init_state(), session.events)
    last_seq = max(session.seq - 1, 0)
    rows = registry.checkpoint(state, last_seq)
    get_projection_cache().save_checkpoint(ProjectionCheckpoint.from_session(session, rows))


def scan_and_mark_interrupted() -> list[dict[str, Any]]:
    """U6 启动扫描：悬挂会话标记 interrupted（保守版——不自动续跑，等用户决定）。

    流程：projcache running=True 初筛 → 事件流核验未闭合 turn →
    load_from_events 合成 closer 落盘（turn/end{reason:"crash_recovery"}）→
    补 session/interrupted 用户可见标记 → 强制刷新投影缓存。
    脏缓存（事件流已平衡）只重建 checkpoint 不写事件；孤儿缓存直接清理。
    """

    backend = get_session_backend()
    repaired: list[dict[str, Any]] = []
    for sid in _projcache_running_candidates():
        try:
            events = backend.load_all_events(sid)
        except Exception as e:
            print(f"[CRASH_RECOVERY] {sid} 事件读取失败: {e!r}")
            continue
        if not events:
            # 孤儿 projcache（零事件）：缓存无对应事件流，直接清理
            from .session_projection_cache import get_projection_cache
            get_projection_cache().delete_checkpoint(sid)
            print(f"[CRASH_RECOVERY] {sid} projcache 无对应事件，已清理孤儿缓存")
            continue
        depth = unpaired_turn_depth(events)
        session = Session.load_from_events(sid)
        if session is None:
            continue
        if depth == 0:
            _refresh_projection_cache(session)
            continue
        # load_from_events 已合成 turn/end{reason:"crash_recovery"} 并落盘
        # （配对修复）；这里补用户/前端可见的中断标记，并强制刷新投影缓存
        # （turn/end 不再经扫描 append，节流写可能漏掉 running=False）
        session.append("session/interrupted", {
            "reason": "startup_scan",
            "message": "服务已重启，上一轮执行被中断。可以继续对话。",
        })
        _refresh_projection_cache(session)
        repaired.append({"session_id": sid, "unpaired_turns": depth})
        print(f"[CRASH_RECOVERY] {sid} 标记 interrupted（unpaired turns={depth}）")
    return repaired


async def shutdown_active_sessions(timeout_s: float = 2.0) -> list[str]:
    """U6 优雅 shutdown：abort 活跃 agent → 限时等各自收尾（agent 的
    CancelledError 路径自写 turn/end{reason:"aborted"}）→ 超时仍悬挂者
    合成 turn/end{reason:"shutdown"} + session/interrupted 落盘。
    """
    import asyncio


    sm = get_session_manager()
    live: list[tuple[Any, Any]] = []
    for session in sm.active_sessions():
        agent = sm.get_agent(session.id)
        if agent is not None and getattr(agent, "is_processing", False):
            live.append((session, agent))
    if not live:
        return []

    for _, agent in live:
        agent.abort()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    pending = list(live)
    while pending and loop.time() < deadline:
        await asyncio.sleep(0.05)
        pending = [(s, a) for s, a in pending if getattr(a, "is_processing", False)]

    force_closed: list[str] = []
    for session, _agent in pending:
        depth = unpaired_turn_depth(session.events)
        if depth > 0:
            for _ in range(depth):
                session.append("turn/end", {"reason": "shutdown", "synthesized": True})
            session.append("session/interrupted", {
                "reason": "shutdown",
                "message": "服务已关闭，上一轮执行被中断。",
            })
        force_closed.append(session.id)
    return force_closed
