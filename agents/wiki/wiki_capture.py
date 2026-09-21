"""Wiki 捕获 — Session → Wiki/session/。

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
        其中 max_seq - last_extract_pos > threshold，
        且会话空闲 ≥ capture.idleMinutes（2.4：避免提取进行中的会话，
        以 events.jsonl mtime 为最后活动时间；进行中场景由压缩触发覆盖）
    """
    import time
    from agents.core.session import session_dir
    from agents.wiki.evolution.settings import get_setting

    idle_seconds = float(get_setting("capture.idleMinutes", 30)) * 60
    now = time.time()
    sessions_needing = []
    
    for session_id in get_all_session_ids():
        last_pos = get_last_extract_pos(session_id)
        max_seq = get_session_max_seq(session_id)
        
        # 如果有未提取的事件且超过阈值
        if max_seq > last_pos and (max_seq - last_pos) > threshold:
            events_path = session_dir() / f"{session_id}.events.jsonl"
            try:
                if now - events_path.stat().st_mtime < idle_seconds:
                    continue
            except OSError:
                continue
            sessions_needing.append((session_id, last_pos, max_seq))
    
    return sessions_needing


def _capture_settings() -> dict:
    from agents.wiki.evolution.settings import get_setting
    return {
        "tool_result_max_chars": int(get_setting("capture.toolResultMaxChars", 500)),
        "context_ratio": float(get_setting("capture.contextRatio", 0.7)),
        "side_context_window": int(get_setting("capture.sideContextWindow", 32000)),
        "external_tools": [str(t).lower() for t in (get_setting("capture.externalTools") or [])],
    }


def _is_external_tool(tool_name: str, external_tools: list[str]) -> bool:
    name = (tool_name or "").lower()
    return any(marker in name for marker in external_tools)


def _clean_tool_result(tool_name: str, content: str, cfg: dict) -> str:
    """tool_result 降级（2.2）：外部内容占位，其余截断到 toolResultMaxChars。"""
    if _is_external_tool(tool_name, cfg["external_tools"]):
        return f"[external content from {tool_name} omitted]"
    limit = cfg["tool_result_max_chars"]
    if len(content) > limit:
        return content[:limit] + "\n[truncated]"
    return content


def _fit_context_budget(content: str, cfg: dict) -> tuple[str, bool]:
    """输入总预算：超过 side model 上下文 contextRatio 时头尾保留、中间截断。"""
    budget = int(cfg["context_ratio"] * cfg["side_context_window"])
    if len(content) <= budget:
        return content, False
    head = budget // 2
    tail = budget - head
    marker = f"\n\n[middle truncated: {len(content) - budget} chars omitted]\n\n"
    return content[:head] + marker + content[-tail:], True


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

    from agents.wiki.redact import redact_secrets

    cfg = _capture_settings()

    # B1 修复：tool_result_msg 事件不带 tool_name，
    # 从同段 assistant_message 的 tool_calls 反查 call_id → tool_name
    tool_name_by_call_id: dict[str, str] = {}
    for event in events:
        for tc in event.get("tool_calls") or []:
            call_id = tc.get("id") or ""
            name = (tc.get("function") or {}).get("name") or ""
            if call_id and name:
                tool_name_by_call_id[call_id] = name

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
            tool_name = (
                event.get("tool_name")
                or tool_name_by_call_id.get(event.get("call_id") or "")
                or _parse_tool_name_from_wrapper(content)
            )
            cleaned = _clean_tool_result(tool_name, content, cfg)
            content_parts.append(f"## Tool Result: {tool_name}\n{cleaned}")
    
    content = "\n\n".join(content_parts) if content_parts else "(empty session)"
    # 2.3：secrets 脱敏（segment 落盘前）
    content = redact_secrets(content)
    # 2.2：输入总预算，头尾保留中间截断
    content, truncated = _fit_context_budget(content, cfg)

    meta = {
        "session_id": session_id,
        "segment_index": str(segment_index),
        "type": "session",
        "captured_at": now.isoformat(timespec="seconds"),
        "compiled": "false",
        "event_count": str(len(events)),
        "max_seq": str(max(e.get("seq", 0) for e in events)),
        "truncated": "true" if truncated else "false",
    }

    from agents.core.frontmatter import format_frontmatter
    filepath.write_text(format_frontmatter(meta, content))
    _git_commit(f"wiki: capture session {session_id} segment {segment_index}")
    return filepath


_TOOL_WRAPPER_RE = re.compile(r'<tool_result tool="([^"]+)">')


def _parse_tool_name_from_wrapper(content: Any) -> str:
    """兜底：append_tool_message 把工具名包在 <tool_result tool="..."> 里。"""
    if not isinstance(content, str):
        return "unknown"
    m = _TOOL_WRAPPER_RE.search(content[:200])
    return m.group(1) if m else "unknown"


def mark_session_compiled(filepath: Path) -> None:
    from agents.core.frontmatter import parse_frontmatter, format_frontmatter
    try:
        result = parse_frontmatter(filepath.read_text())
        result.meta["compiled"] = "true"
        filepath.write_text(format_frontmatter(result.meta, result.body))
    except Exception as e:
        print(f"[wiki_capture] mark_session_compiled failed for {filepath}: {type(e).__name__}: {e}")
        raise


MAX_COMPILE_ATTEMPTS = 3


def register_compile_failure(filepath: Path) -> bool:
    """记录 segment 编译失败次数；达到毒丸上限则终态化并推进水位线（BC-3）。

    终态：compiled=failed（保留文件供人工回看，不再重试），水位线推进到
    segment 的 max_seq，释放该 session 后续事件的提取。失败清单落日志，不静默吞。

    Returns:
        True 表示已达上限并终态化。
    """
    from agents.core.frontmatter import parse_frontmatter, format_frontmatter
    try:
        result = parse_frontmatter(filepath.read_text())
        attempts = int(result.meta.get("compile_attempts", "0")) + 1
        result.meta["compile_attempts"] = str(attempts)
        terminal = attempts >= MAX_COMPILE_ATTEMPTS
        if terminal:
            result.meta["compiled"] = "failed"
        filepath.write_text(format_frontmatter(result.meta, result.body))
    except Exception as e:
        print(f"[wiki_capture] register_compile_failure failed for {filepath}: {type(e).__name__}: {e}")
        return False

    if not terminal:
        print(f"[wiki_capture] compile attempt {attempts}/{MAX_COMPILE_ATTEMPTS} failed: {filepath.name}")
        return False

    print(f"[wiki_capture] segment reached max attempts ({attempts}), marked compiled=failed, advancing watermark: {filepath.name}")
    try:
        session_id = result.meta.get("session_id", "")
        seg_max_seq = int(result.meta.get("max_seq", "0"))
        if session_id and seg_max_seq > get_last_extract_pos(session_id):
            set_last_extract_pos(session_id, seg_max_seq)
    except Exception as e:
        print(f"[wiki_capture] watermark advance after terminal failure failed for {filepath}: {type(e).__name__}: {e}")
    _git_commit(f"wiki: segment {filepath.stem} terminally failed after {MAX_COMPILE_ATTEMPTS} attempts")
    return True


def list_uncompiled_segments() -> list[Path]:
    """列出所有未编译的 session segment 文件（编译失败/中断后待重试）。

    compiled=failed 是毒丸终态，不再重试。
    """
    from agents.core.frontmatter import parse_frontmatter
    session_root = get_wiki_dir() / "session"
    if not session_root.exists():
        return []
    
    uncompiled = []
    for f in session_root.rglob("*_seg*.md"):
        try:
            result = parse_frontmatter(f.read_text())
            if result.meta.get("compiled") not in ("true", "failed"):
                uncompiled.append(f)
        except Exception as e:
            print(f"[wiki_capture] parse segment failed {f}: {type(e).__name__}: {e}")
    return uncompiled
