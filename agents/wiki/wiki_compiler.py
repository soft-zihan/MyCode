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
    list_uncompiled_session,
    mark_session_compiled,
    record_compile_time,
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


async def compile_pending_session(side_query: Any) -> dict[str, int]:
    """编译未处理的 session 笔记。

    增强：compile lock、state tracking、dedup、分段编译。
    """
    if not _acquire_compile_lock():
        return {"error": "compile already running"}

    try:
        state = _load_compile_state()
        state["last_attempted_date"] = datetime.now(timezone.utc).isoformat()
        _save_compile_state(state)

        uncompiled = list_uncompiled_session()
        if not uncompiled:
            return {}

        stats: dict[str, int] = {
            "knowledge": 0, "self_improvement": 0, "user": 0,
            "reference": 0, "workflow_pattern": 0,
            "deduped": 0,
        }

        for session_path in uncompiled:
            try:
                from agents.memory.frontmatter import parse_frontmatter
                result = parse_frontmatter(session_path.read_text())
                content = result.body

                if not content.strip() or content.strip() == "(empty session)":
                    mark_session_compiled(session_path)
                    continue

                # 分段编译：按 session 分段
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

            except Exception:
                retry_key = str(session_path)
                state["metadata_retry_counts"][retry_key] = \
                    state["metadata_retry_counts"].get(retry_key, 0) + 1
                _save_compile_state(state)

        record_compile_time()
        _git_commit(f"wiki: compile {sum(stats.values())} entries from session")
        update_wiki_index()
        return stats

    finally:
        _release_compile_lock()


def _split_into_segments(content: str) -> list[str]:
    """将内容按 session 分段。

    每个 session 以 "--- Session X started at ..." 开头。
    """
    import re
    
    # 按 session 分割
    session_pattern = r"(--- Session \d+ started at .*? ---)"
    parts = re.split(session_pattern, content)
    
    segments = []
    current_segment = []
    
    for part in parts:
        if re.match(session_pattern, part):
            # 新的 session 开始
            if current_segment:
                segments.append("\n".join(current_segment))
            current_segment = [part]
        else:
            current_segment.append(part)
    
    # 添加最后一个 segment
    if current_segment:
        segments.append("\n".join(current_segment))
    
    # 如果没有找到 session 标记，返回整个内容
    if not segments:
        segments = [content]
    
    return segments


async def _extract_from_session(content: str, side_query: Any) -> list[dict]:
    try:
        text = await side_query(
            EXTRACT_PROMPT,
            f"Session notes:\n{content[:8000]}",
        )

        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return []

        items = json.loads(match.group(0))
        if not isinstance(items, list):
            return []

        return [item for item in items if isinstance(item, dict) and item.get("type")]
    except Exception:
        return []


async def compile_to_skill(pattern_rel_path: str, side_query: Any) -> str | None:
    from agents.wiki.wiki_manager import read_wiki_entry, mark_pattern_compiled

    entry = read_wiki_entry(pattern_rel_path)
    if not entry:
        return None

    if entry.type != "workflow_pattern":
        return None

    applied_count = int(entry.meta.get("applied_count", "0"))
    if applied_count < 2:
        return None

    skill_content = await _generate_skill_from_pattern(entry, side_query)
    if not skill_content:
        return None

    skill_name = re.sub(r"[^a-z0-9]+", "-", entry.name.lower()).strip("-")[:40]
    skills_dir = get_workspace() / ".mycode" / "skills" / skill_name
    skills_dir.mkdir(parents=True, exist_ok=True)

    skill_path = skills_dir / "SKILL.md"
    skill_path.write_text(skill_content)

    mark_pattern_compiled(pattern_rel_path)
    _git_commit(f"skill: compile from pattern {entry.name}")

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
