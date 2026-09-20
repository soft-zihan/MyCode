"""Wiki 编译 — session → knowledge/self_improvement/user/reference/patterns。

从 session 笔记中提取各类知识，写入对应分类。
增强：compile lock、state tracking、dedup。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.core.workspace import get_workspace
from agents.wiki.wiki_manager import (
    get_wiki_dir,
    write_wiki_entry,
    write_workflow_pattern,
    _git_commit,
    update_wiki_index,
)
from agents.wiki.wiki_capture import (
    mark_session_compiled,
    find_sessions_needing_compilation,
    get_session_events_from_seq,
    capture_session_to_session,
    set_last_extract_pos,
    list_uncompiled_segments,
)


COMPILE_LOCK_FILE = ".compile.lock"
COMPILE_STATE_FILE = ".compile_state.json"


def _acquire_compile_lock() -> bool:
    """获取 compile 锁。"""
    wiki_dir = get_wiki_dir()
    lock_path = wiki_dir / COMPILE_LOCK_FILE
    if lock_path.exists():
        try:
            lock_data = json.loads(lock_path.read_text())
            lock_time = datetime.fromisoformat(lock_data["timestamp"])
            if (datetime.now(timezone.utc) - lock_time).total_seconds() > 3600:
                lock_path.unlink()
            else:
                return False
        except Exception:
            lock_path.unlink()

    lock_path.write_text(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()}))
    return True


def _release_compile_lock() -> None:
    """释放 compile 锁。"""
    wiki_dir = get_wiki_dir()
    lock_path = wiki_dir / COMPILE_LOCK_FILE
    if lock_path.exists():
        lock_path.unlink()


def _load_compile_state() -> dict[str, Any]:
    """加载 compile 状态。"""
    wiki_dir = get_wiki_dir()
    state_path = wiki_dir / COMPILE_STATE_FILE
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except Exception:
            pass
    return {"last_attempted_date": None, "metadata_retry_counts": {}}


def _save_compile_state(state: dict[str, Any]) -> None:
    """保存 compile 状态。"""
    wiki_dir = get_wiki_dir()
    state_path = wiki_dir / COMPILE_STATE_FILE
    state_path.write_text(json.dumps(state, indent=2))


def _dedup_check(wiki_type: str, name: str) -> bool:
    """检查是否已存在相同条目。"""
    from agents.wiki.wiki_manager import list_wiki_entries
    entries = list_wiki_entries(wiki_type)
    for entry in entries:
        if entry.name == name:
            return True
    return False


# 从文件加载 side query 提示词
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"
EXTRACT_PROMPT = (_PROMPTS_DIR / "extract_knowledge.txt").read_text(encoding="utf-8")


async def compile_single_session(session_path: Path, side_query: Any) -> dict[str, int] | None:
    """编译单个 session 文件。
    
    Args:
        session_path: session 文件路径
        side_query: side query 函数
    
    Returns:
        提取统计信息，或 None（如果编译锁被占用）
    """
    if not _acquire_compile_lock():
        return None
    
    try:
        stats: dict[str, int] = {
            "knowledge": 0, "self_improvement": 0, "user": 0,
            "reference": 0, "workflow_pattern": 0,
            "deduped": 0,
        }
        
        from agents.memory.frontmatter import parse_frontmatter
        result = parse_frontmatter(session_path.read_text())
        content = result.body
        
        if not content.strip() or content.strip() == "(empty session)":
            mark_session_compiled(session_path)
            return stats
        
        # 按对话轮次分段
        segments = _split_into_segments(content)
        
        for segment in segments:
            extractions = await _extract_from_session(segment, side_query)
            
            for item in extractions:
                item_type = item.get("type", "")
                if item_type not in stats:
                    continue
                
                name = item.get("name", "untitled")
                description = item.get("description", "")
                
                if _dedup_check(item_type, name):
                    stats["deduped"] += 1
                    continue
                
                if item_type == "workflow_pattern":
                    write_workflow_pattern(
                        name=name,
                        symptom=item.get("symptom", ""),
                        root_cause=item.get("root_cause", ""),
                        workaround=item.get("workaround", ""),
                        description=description,
                    )
                elif item_type in ("knowledge_pattern", "plan"):
                    write_wiki_entry(
                        wiki_type="knowledge",
                        name=name,
                        content=item.get("content", ""),
                        description=description,
                    )
                    stats["knowledge"] += 1
                elif item_type == "self_improvement":
                    write_wiki_entry(
                        wiki_type="self_improvement",
                        name=name,
                        content=item.get("content", ""),
                        description=description,
                        extra_meta={"pending_confirm": "true"},
                    )
                else:
                    write_wiki_entry(
                        wiki_type=item_type,
                        name=name,
                        content=item.get("content", ""),
                        description=description,
                    )
                
                stats[item_type] += 1
        
        mark_session_compiled(session_path)
        _git_commit(f"wiki: compile {sum(stats.values())} entries from {session_path.name}")
        update_wiki_index()
        
        return stats
    
    finally:
        _release_compile_lock()


def _split_into_segments(content: str) -> list[str]:
    """将内容按对话轮次分段。

    新格式以 ## User 开头，按 ## User 分割为多段。
    旧格式以 --- Session X started at 开头，按该标记分割。
    """
    import re
    
    # 新格式：按 ## User 分割
    if "## User" in content:
        parts = re.split(r"(?=## User\n)", content)
        segments = [p.strip() for p in parts if p.strip()]
        if segments:
            return segments
    
    # 旧格式：按 --- Session 分割
    session_pattern = r"(--- Session \d+ started at .*? ---)"
    parts = re.split(session_pattern, content)
    
    segments = []
    current_segment = []
    
    for part in parts:
        if re.match(session_pattern, part):
            if current_segment:
                segments.append("\n".join(current_segment))
            current_segment = [part]
        else:
            current_segment.append(part)
    
    if current_segment:
        segments.append("\n".join(current_segment))
    
    if not segments:
        segments = [content]
    
    return segments


async def _extract_from_session(content: str, side_query: Any) -> list[dict]:
    """LLM 提取结构化知识。失败时抛异常（不标记 compiled，等待重试）。"""
    text = await side_query(
        EXTRACT_PROMPT,
        f"Session events:\n{content}",
    )

    match = re.search(r"\[[\s\S]*\]", text)
    if not match:
        raise ValueError(f"extract response has no JSON array: {text[:200]!r}")

    items = json.loads(match.group(0))
    if not isinstance(items, list):
        raise ValueError(f"extract response JSON is not a list: {type(items).__name__}")

    return [item for item in items if isinstance(item, dict) and item.get("type")]


async def compile_to_skill(pattern_rel_path: str, side_query: Any) -> str | None:
    from agents.wiki.wiki_manager import (
        read_wiki_entry,
        mark_pattern_compiled,
        pattern_content_hash,
        is_skill_stale,
    )

    entry = read_wiki_entry(pattern_rel_path)
    if not entry:
        return None

    if entry.type != "workflow_pattern":
        return None

    applied_count = int(entry.meta.get("applied_count", "0"))
    if applied_count < 2:
        return None

    # 二次检查：内容未变更则无需重新编译（避免并发召回重复触发）
    if not is_skill_stale(entry):
        return None

    content_hash = pattern_content_hash(entry.content)
    skill_content = await _generate_skill_from_pattern(entry, side_query)
    if not skill_content:
        return None

    skill_name = re.sub(r"[^a-z0-9]+", "-", entry.name.lower()).strip("-")[:40]
    skills_dir = get_workspace() / ".mycode" / "skills" / skill_name
    skills_dir.mkdir(parents=True, exist_ok=True)

    skill_path = skills_dir / "SKILL.md"
    skill_path.write_text(skill_content)

    mark_pattern_compiled(pattern_rel_path, content_hash)
    _git_commit(f"skill: compile from pattern {entry.name}")

    # 验证门禁：新 skill 变体落盘后运行在线评测，
    # 仅当严格优于历史最佳（分差 >= 0.01 且无新增硬失败）才晋升 champion
    try:
        from agents.skills.skill_evaluator import evaluate_online_skill_evolution_async

        await evaluate_online_skill_evolution_async(side_query=side_query)
    except Exception as e:
        print(f"[skill_compile] 验证门禁评测失败: {type(e).__name__}: {e}")

    return str(skill_path)


SKILL_GENERATION_PROMPT = (_PROMPTS_DIR / "generate_skill.txt").read_text(encoding="utf-8")


async def _generate_skill_from_pattern(entry: Any, side_query: Any) -> str | None:
    try:
        text = await side_query(
            SKILL_GENERATION_PROMPT,
            f"Pattern name: {entry.name}\n\nPattern content:\n{entry.content}",
        )
        print(f"[skill_generate] raw response length={len(text) if text else 0}")
        return text.strip() if text.strip() else None
    except Exception as e:
        print(f"[skill_generate] error: {type(e).__name__}: {e}")
        return None


async def check_and_compile_pending_sessions(side_query: Any, threshold: int = 20) -> dict[str, int]:
    """补编译：重试未编译 segment + 扫描落后 session。

    两个来源：
    1. 未编译的 segment 文件（编译失败/中断残留）→ 直接重试编译
    2. 水位线落后 max_seq 超过 threshold 的 session（未触发过压缩）→ 从 events.jsonl 补捕获再编译

    水位线只在编译成功后推进，失败留待下次重试。

    Args:
        side_query: side query 函数
        threshold: 未提取事件数阈值，超过此值才触发补捕获

    Returns:
        总提取统计信息
    """
    from agents.memory.frontmatter import parse_frontmatter

    total_stats: dict[str, int] = {
        "knowledge": 0, "self_improvement": 0, "user": 0,
        "reference": 0, "workflow_pattern": 0,
        "deduped": 0,
    }

    def _merge(stats: dict[str, int]) -> None:
        for k, v in stats.items():
            if isinstance(v, int):
                total_stats[k] = total_stats.get(k, 0) + v

    # 阶段 1：重试未编译的 segment（失败/中断残留）
    uncompiled = list_uncompiled_segments()
    sessions_with_pending_segment: set[str] = set()
    for seg_path in uncompiled:
        try:
            meta = parse_frontmatter(seg_path.read_text()).meta
            session_id = meta.get("session_id", "")
            if session_id:
                sessions_with_pending_segment.add(session_id)

            stats = await compile_single_session(seg_path, side_query)
            if stats is None:
                print(f"[wiki_backfill] compile lock busy, will retry later: {seg_path.name}")
                continue

            _merge(stats)
            # 编译成功后才推进水位线
            if session_id:
                seg_max_seq = int(meta.get("max_seq", "0"))
                if seg_max_seq > 0:
                    from agents.wiki.wiki_capture import get_last_extract_pos
                    if seg_max_seq > get_last_extract_pos(session_id):
                        set_last_extract_pos(session_id, seg_max_seq)
            print(f"[wiki_backfill] retried segment {seg_path.name}: {sum(v for v in stats.values() if isinstance(v, int))} entries")
        except Exception as e:
            print(f"[wiki_backfill] retry segment failed {seg_path.name}: {type(e).__name__}: {e}")
            continue

    # 阶段 2：扫描水位线落后的 session（未触发过压缩）
    sessions_needing = find_sessions_needing_compilation(threshold)
    # 已有待重试 segment 的 session 跳过，避免重复捕获同一段事件
    sessions_needing = [s for s in sessions_needing if s[0] not in sessions_with_pending_segment]

    if sessions_needing:
        print(f"[wiki_backfill] found {len(sessions_needing)} sessions needing capture")

    for session_id, last_pos, max_seq in sessions_needing:
        try:
            events = get_session_events_from_seq(session_id, last_pos)
            if not events:
                continue

            from agents.wiki.wiki_manager import get_wiki_dir
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            session_dir = get_wiki_dir() / "session" / now.strftime("%Y/%m/%d")
            slug = session_id.replace("/", "_").replace("\\", "_")[:40]
            existing_segments = list(session_dir.glob(f"{slug}_seg*.md")) if session_dir.exists() else []
            segment_index = len(existing_segments) + 1

            captured_path = capture_session_to_session(session_id, events, segment_index)
            if not captured_path:
                continue

            stats = await compile_single_session(captured_path, side_query)
            if stats is None:
                print(f"[wiki_backfill] compile lock busy, segment left for retry: {captured_path.name}")
                continue

            _merge(stats)
            # 编译成功后才推进水位线
            max_event_seq = max(e.get("seq", 0) for e in events)
            set_last_extract_pos(session_id, max_event_seq)
            print(f"[wiki_backfill] compiled session {session_id}: {sum(v for v in stats.values() if isinstance(v, int))} entries")
        except Exception as e:
            print(f"[wiki_backfill] error compiling session {session_id}: {type(e).__name__}: {e}")
            continue

    return total_stats
