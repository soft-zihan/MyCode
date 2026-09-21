"""Wiki 写入管道 — 会话折叠事件的唯一消费者。

职责划分（R1）：ContextCompressor 只负责折叠上下文，折叠成功后通过
`on_session_folded` 通知本模块；所有 wiki 写入/提取编译集中在 wiki 包内。

流程：
1. session_notes 合并写入（原地更新，内容未变则跳过）
2. segment 捕获：水位线之后的新事件 → wiki/session/YYYY/MM/DD/<slug>_segN.md
3. 异步编译（compile lock 防并发）→ 成功后推进水位线 → 补编译残留 segment

水位线只在编译成功后推进；失败时 segment 保持 compiled=false，
由 check_and_compile_pending_sessions 重试，保证不重不漏。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

SideQueryFn = Callable[[str, str], Awaitable[str]]

BACKFILL_THRESHOLD = 20


async def on_session_folded(
    *,
    session_id: str,
    session: Any,
    session_notes: str,
    side_query: SideQueryFn | None,
    workspace: Path | None = None,
) -> None:
    """折叠事件入口：写 session_notes + 捕获编译 segment。

    由 compressor 以 asyncio.create_task 调用，整体在后台执行。
    workspace 用于恢复 contextvar（后台 task 不继承调用方上下文时兜底）。
    """
    if workspace is not None:
        from agents.core.workspace import set_workspace, reset_workspace
        token = set_workspace(workspace)
        try:
            await _run(session_id, session, session_notes, side_query)
        finally:
            reset_workspace(token)
    else:
        await _run(session_id, session, session_notes, side_query)


async def _run(
    session_id: str,
    session: Any,
    session_notes: str,
    side_query: SideQueryFn | None,
) -> None:
    if session_notes:
        await _write_session_notes(session_id, session_notes)
    if side_query is not None:
        await _capture_and_compile(session_id, session, side_query)


async def _write_session_notes(session_id: str, session_notes: str) -> None:
    """session_notes 合并写入（压缩摘要产物，原地更新）。"""
    try:
        from agents.wiki.wiki_manager import write_wiki_entry

        await asyncio.to_thread(
            write_wiki_entry,
            wiki_type="session_notes",
            name=f"session_{session_id}",
            content=session_notes,
            description=f"Session notes for session {session_id}",
            extra_meta={"session_id": session_id},
            skip_if_unchanged=True,
        )
        logger.info("[wiki_write] session_notes updated for session=%s", session_id)
    except Exception as e:
        logger.error("[wiki_write] session_notes failed session=%s: %s: %s", session_id, type(e).__name__, e)


async def _capture_and_compile(session_id: str, session: Any, side_query: SideQueryFn) -> None:
    """捕获水位线之后的新事件为 segment，并立即编译。"""
    from agents.wiki.wiki_capture import (
        capture_session_to_session,
        get_last_extract_pos,
    )
    from agents.wiki.wiki_manager import get_wiki_dir

    try:
        last_pos = get_last_extract_pos(session_id)
        new_events = [e for e in session.events if e.get("seq", 0) > last_pos]
        if not new_events:
            return

        slug = session_id.replace("/", "_").replace("\\", "_")[:40]
        session_root = get_wiki_dir() / "session"
        existing_segments = list(session_root.rglob(f"{slug}_seg*.md")) if session_root.exists() else []
        segment_index = len(existing_segments) + 1

        # 水位线不在此推进：编译成功后由 _compile_segment 更新；
        # 失败时 segment 保持 compiled=false，由补编译机制重试
        captured_path = capture_session_to_session(session_id, new_events, segment_index)
        if captured_path:
            await _compile_segment(captured_path, side_query)
    except Exception as e:
        logger.error("[wiki_capture] fold capture failed for %s: %s: %s", session_id, type(e).__name__, e)


async def _compile_segment(session_path: Path, side_query: SideQueryFn) -> None:
    """编译单个 segment。成功后推进水位线并触发补编译。"""
    from agents.wiki.wiki_compiler import compile_single_session, check_and_compile_pending_sessions

    try:
        # BC-6：折叠触发的编译对延迟敏感（评测 wait_for_files / 用户召回），
        # 锁忙时有限重试而非直接丢给空闲补编译（后者可能被 backfill 队列拖到几分钟后）
        result = None
        for attempt in range(3):
            result = await compile_single_session(session_path, side_query)
            if result is not None:
                break
            print(f"[wiki_compile_single] lock busy (attempt {attempt + 1}/3): {session_path.name}")
            await asyncio.sleep(15)
        if result is None:
            # 仍被占用：segment 保持 compiled=false，由补编译机制重试
            print(f"[wiki_compile_single] lock busy, deferred: {session_path.name}")
            return

        _advance_watermark(session_path)

        total = sum(v for v in result.values() if isinstance(v, int))
        if total > 0:
            breakdown = {k: v for k, v in result.items() if isinstance(v, int) and v > 0}
            print(f"[wiki_compile] extracted {total} entries from {session_path.name}: {breakdown}")

        backfill_result = await check_and_compile_pending_sessions(side_query, threshold=BACKFILL_THRESHOLD)
        backfill_total = sum(v for v in backfill_result.values() if isinstance(v, int))
        if backfill_total > 0:
            print(f"[wiki_backfill] extracted {backfill_total} entries from pending sessions")

        # Phase 3 触发点：编译成功后检查整理门槛（24h + ≥5 条目变更）
        from agents.wiki.wiki_consolidator import maybe_schedule_consolidate
        if maybe_schedule_consolidate(side_query):
            print("[wiki_consolidate] scheduled (threshold met)")
    except Exception as e:
        # 编译失败：水位线不推进，segment 保持 compiled=false，下次补编译自动重试；
        # 达到毒丸上限（3 次）由 register_compile_failure 终态化并推进水位线（BC-3）
        print(f"[wiki_compile_single] compile failed for {session_path.name}: {type(e).__name__}: {e}")
        from agents.wiki.wiki_capture import register_compile_failure
        register_compile_failure(session_path)


def _advance_watermark(session_path: Path) -> None:
    """编译成功后从 frontmatter 读取 max_seq，推进提取水位线。"""
    from agents.core.frontmatter import parse_frontmatter
    from agents.wiki.wiki_capture import get_last_extract_pos, set_last_extract_pos

    try:
        meta = parse_frontmatter(session_path.read_text()).meta
        session_id = meta.get("session_id", "")
        seg_max_seq = int(meta.get("max_seq", "0"))
        if session_id and seg_max_seq > get_last_extract_pos(session_id):
            set_last_extract_pos(session_id, seg_max_seq)
    except Exception as e:
        print(f"[wiki_compile_single] watermark update failed for {session_path.name}: {type(e).__name__}: {e}")
        raise
