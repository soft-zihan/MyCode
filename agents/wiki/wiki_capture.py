"""Wiki 捕获 — Session → Wiki/session/。

参考：projects/llm-wiki-memory/scripts/hooks/flush.mjs
增强：分块蒸馏、审计 frontmatter、redistill

以压缩为分界点，把事件流切成多段，每段独立提取知识。
使用水位线保证不重不漏。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.wiki.wiki_manager import get_wiki_dir, _git_commit


MAX_CHUNK_SIZE = 4000
MIN_CHUNK_SIZE = 500


def _get_extract_state_path() -> Path:
    """获取提取状态文件路径。"""
    return get_wiki_dir() / ".extract_state.json"


def _load_extract_state() -> dict[str, Any]:
    """加载提取状态（水位线）。"""
    state_path = _get_extract_state_path()
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except Exception as e:
            print(f"[wiki_capture] extract_state parse error, resetting: {type(e).__name__}: {e}")
    return {}


def _save_extract_state(state: dict[str, Any]) -> None:
    """保存提取状态。"""
    state_path = _get_extract_state_path()
    state_path.write_text(json.dumps(state, indent=2))


def get_last_extract_pos(session_id: str) -> int:
    """获取指定 session 的上次提取位置。"""
    state = _load_extract_state()
    return state.get(f"{session_id}_pos", 0)


def set_last_extract_pos(session_id: str, pos: int) -> None:
    """设置指定 session 的提取位置。"""
    state = _load_extract_state()
    state[f"{session_id}_pos"] = pos
    _save_extract_state(state)


def get_all_session_ids() -> list[str]:
    """获取所有 session ID（扫描 events.jsonl 文件）。"""
    from agents.core.session import session_dir
    sdir = session_dir()
    if not sdir.exists():
        return []
    
    session_ids = []
    for f in sdir.glob("*.events.jsonl"):
        session_id = f.stem.replace(".events", "")
        session_ids.append(session_id)
    return session_ids


def get_session_max_seq(session_id: str) -> int:
    """获取指定 session 的最大 seq（只读文件尾部，避免全量加载）。"""
    from agents.core.session import session_dir
    path = session_dir() / f"{session_id}.events.jsonl"
    if not path.exists():
        return 0
    
    try:
        size = path.stat().st_size
        if size == 0:
            return 0
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError as e:
        print(f"[wiki_capture] read tail failed for {session_id}: {type(e).__name__}: {e}")
        return 0
    
    # 从最后一行往前找第一条可解析的事件（容忍 torn tail）
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return int(json.loads(line).get("seq", 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return 0


def get_session_events_from_seq(session_id: str, from_seq: int) -> list[dict]:
    """从指定 seq 之后读取 session 事件。"""
    from agents.core.session_backend_jsonl import JsonlSessionBackend
    backend = JsonlSessionBackend()
    return backend.get_events(session_id, from_seq=from_seq + 1)


def find_sessions_needing_compilation(threshold: int = 10) -> list[tuple[str, int, int]]:
    """找出需要补编译的 session。
    
    Returns:
        [(session_id, last_extract_pos, max_seq), ...]
        其中 max_seq - last_extract_pos > threshold
    """
    sessions_needing = []
    
    for session_id in get_all_session_ids():
        last_pos = get_last_extract_pos(session_id)
        max_seq = get_session_max_seq(session_id)
        
        # 如果有未提取的事件且超过阈值
        if max_seq > last_pos and (max_seq - last_pos) > threshold:
            sessions_needing.append((session_id, last_pos, max_seq))
    
    return sessions_needing


def capture_session_to_session(
    session_id: str,
    events: list[dict],
    segment_index: int = 1,
) -> Path | None:
    """捕获会话的一段事件到 session 文件。
    
    Args:
        session_id: 会话 ID
        events: 本次折叠的新事件列表（不是全部事件）
        segment_index: 分段序号（同一个 session 可能有多段）
    """
    if not events:
        return None
    
    now = datetime.now(timezone.utc)
    session_dir = get_wiki_dir() / "session" / now.strftime("%Y/%m/%d")
    session_dir.mkdir(parents=True, exist_ok=True)

    slug = session_id.replace("/", "_").replace("\\", "_")[:40]
    # 带分段序号，避免覆盖
    filepath = session_dir / f"{slug}_seg{segment_index}.md"

    # 从原始事件构建内容
    content_parts = []
    
    for event in events:
        event_type = event.get("type", "")
        content = event.get("content", "")
        
        if event_type == "user_message":
            content_parts.append(f"## User\n{content}")
        elif event_type == "assistant_message":
            thinking = event.get("thinking", "")
            if thinking:
                content_parts.append(f"## Assistant (Thinking)\n{thinking}")
            if content:
                content_parts.append(f"## Assistant\n{content}")
            # 处理工具调用
            tool_calls = event.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    args = func.get("arguments", "")
                    content_parts.append(f"## Tool Call: {name}\n```json\n{args}\n```")
        elif event_type == "tool_result_msg":
            tool_name = event.get("tool_name", "unknown")
            content_parts.append(f"## Tool Result: {tool_name}\n{content}")
    
    content = "\n\n".join(content_parts) if content_parts else "(empty session)"

    meta = {
        "session_id": session_id,
        "segment_index": str(segment_index),
        "type": "session",
        "captured_at": now.isoformat(timespec="seconds"),
        "compiled": "false",
        "event_count": str(len(events)),
        "max_seq": str(max(e.get("seq", 0) for e in events)),
    }

    from agents.memory.frontmatter import format_frontmatter
    filepath.write_text(format_frontmatter(meta, content))
    _git_commit(f"wiki: capture session {session_id} segment {segment_index}")
    return filepath


def chunk_dialogue(dialogue: str, max_chunk: int = MAX_CHUNK_SIZE) -> list[str]:
    """将对话分块。

    按 ### 标题分块，段落分割，硬切兜底。
    """
    if len(dialogue) <= max_chunk:
        return [dialogue]

    chunks: list[str] = []
    current_chunk = ""

    sections = re.split(r"(?=###\s+)", dialogue)

    for section in sections:
        if len(current_chunk) + len(section) <= max_chunk:
            current_chunk += section
        else:
            if current_chunk:
                chunks.append(current_chunk)
            if len(section) <= max_chunk:
                current_chunk = section
            else:
                paragraphs = section.split("\n\n")
                current_chunk = ""
                for para in paragraphs:
                    if len(current_chunk) + len(para) + 2 <= max_chunk:
                        current_chunk += ("\n\n" if current_chunk else "") + para
                    else:
                        if current_chunk:
                            chunks.append(current_chunk)
                        if len(para) <= max_chunk:
                            current_chunk = para
                        else:
                            for i in range(0, len(para), max_chunk):
                                chunks.append(para[i:i+max_chunk])
                            current_chunk = ""

    if current_chunk:
        chunks.append(current_chunk)

    return chunks if chunks else [dialogue]


async def distill_chunks(
    chunks: list[str],
    side_query: Any,
) -> dict[str, Any]:
    """分块蒸馏。

    每块独立蒸馏，最后合并。
    返回蒸馏结果和审计信息。
    """
    results: list[dict] = []
    failed_chunks: list[int] = []
    provider_chain_tried = ["ollama"]
    final_provider = "ollama"

    for i, chunk in enumerate(chunks):
        try:
            distilled = await _distill_single_chunk(chunk, side_query)
            results.append({"chunk_index": i, "content": distilled, "status": "success"})
        except Exception as e:
            results.append({"chunk_index": i, "content": "", "status": "failed", "error": str(e)})
            failed_chunks.append(i)

    return {
        "chunks_total": len(chunks),
        "chunks_succeeded": len(chunks) - len(failed_chunks),
        "failed_chunks": failed_chunks,
        "provider_chain_tried": provider_chain_tried,
        "final_provider": final_provider,
        "results": results,
    }


# 从文件加载 side query 提示词
from pathlib import Path
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"
DISTILL_CHUNK_PROMPT = (_PROMPTS_DIR / "distill_chunk.txt").read_text(encoding="utf-8")


async def _distill_single_chunk(chunk: str, side_query: Any) -> str:
    """蒸馏单个块。"""
    try:
        return await side_query(DISTILL_CHUNK_PROMPT, chunk[:MAX_CHUNK_SIZE])
    except Exception:
        raise


async def redistill_failed_chunks(
    session_path: Path,
    side_query: Any,
) -> dict[str, Any]:
    """重蒸馏失败的块。

    从 stash 恢复失败分块，重新蒸馏。
    """
    from agents.memory.frontmatter import parse_frontmatter, format_frontmatter

    result = parse_frontmatter(session_path.read_text())
    meta = result.meta

    failed_indices = meta.get("failed_chunks", "[]")
    if isinstance(failed_indices, str):
        try:
            failed_indices = json.loads(failed_indices)
        except json.JSONDecodeError:
            failed_indices = []

    if not failed_indices:
        return {"redistilled": 0, "still_failed": 0}

    stash_path = session_path.with_suffix(".stash.json")
    if not stash_path.exists():
        return {"redistilled": 0, "still_failed": len(failed_indices)}

    try:
        stash = json.loads(stash_path.read_text())
    except (json.JSONDecodeError, KeyError):
        return {"redistilled": 0, "still_failed": len(failed_indices)}

    redistilled = 0
    still_failed: list[int] = []

    for idx in failed_indices:
        if idx >= len(stash.get("chunks", [])):
            continue

        chunk = stash["chunks"][idx]
        try:
            distilled = await _distill_single_chunk(chunk, side_query)
            stash["results"][idx] = {"chunk_index": idx, "content": distilled, "status": "success"}
            redistilled += 1
        except Exception:
            still_failed.append(idx)

    stash["failed_chunks"] = still_failed
    stash_path.write_text(json.dumps(stash, ensure_ascii=False, indent=2))

    meta["failed_chunks"] = json.dumps(still_failed)
    meta["chunks_succeeded"] = str(len(stash["results"]) - len(still_failed))
    session_path.write_text(format_frontmatter(meta, result.body))

    return {"redistilled": redistilled, "still_failed": len(still_failed)}


def mark_session_compiled(filepath: Path) -> None:
    from agents.memory.frontmatter import parse_frontmatter, format_frontmatter
    try:
        result = parse_frontmatter(filepath.read_text())
        result.meta["compiled"] = "true"
        filepath.write_text(format_frontmatter(result.meta, result.body))
    except Exception as e:
        print(f"[wiki_capture] mark_session_compiled failed for {filepath}: {type(e).__name__}: {e}")
        raise


def list_uncompiled_segments() -> list[Path]:
    """列出所有未编译的 session segment 文件（编译失败/中断后待重试）。"""
    from agents.memory.frontmatter import parse_frontmatter
    session_root = get_wiki_dir() / "session"
    if not session_root.exists():
        return []
    
    uncompiled = []
    for f in session_root.rglob("*_seg*.md"):
        try:
            result = parse_frontmatter(f.read_text())
            if result.meta.get("compiled") != "true":
                uncompiled.append(f)
        except Exception as e:
            print(f"[wiki_capture] parse segment failed {f}: {type(e).__name__}: {e}")
    return uncompiled
