"""
文件型记忆系统。

核心思路：
1. 每个项目有独立的 memory 目录，目录名由当前工作目录 hash 得到。
2. 每条记忆都是一个 Markdown 文件，文件头部用 YAML frontmatter 保存元信息。
3. MEMORY.md 是自动生成的索引，给 system prompt 快速展示已有记忆。
4. 对话时先轻量扫描记忆文件头，再用 side query 让模型挑出相关记忆。
5. 召回到的记忆会以 <system-reminder> 形式注入当前对话。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .frontmatter import parse_frontmatter, format_frontmatter
from .ui import print_warning
from typing import Callable
# side query 是一个异步函数：输入 system prompt 和 user prompt，返回模型文本。
# 这里标成 Any 是为了避免在运行时引入复杂 Awaitable 类型约束。
SideQueryFn = Callable[[str, str], Any]  # actually Awaitable[str]


VALID_TYPES = {"user", "feedback", "project", "reference"}
MAX_INDEX_LINES = 200       # MEMORY.md 注入 system prompt 前最多保留的行数。
MAX_INDEX_BYTES = 25000     # MEMORY.md 注入 system prompt 前最多保留的字节数。


class MemoryEntry:
    """完整 memory 条目，用于 /memory 列表和 CRUD 操作。"""

    __slots__ = ("name", "description", "type", "filename", "content")

    def __init__(self, name: str, description: str, type: str, filename: str, content: str):
        self.name = name
        self.description = description
        self.type = type
        self.filename = filename
        self.content = content




def _project_hash() -> str:
    """用当前工作目录生成稳定 hash，让不同项目的记忆互相隔离。"""
    return hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]


def get_memory_dir() -> Path:
    """返回当前项目的 memory 目录，不存在时自动创建。"""
    d = Path.home() / ".BearCode" / "projects" / _project_hash() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_index_path() -> Path:
    """MEMORY.md 是当前项目 memory 文件的索引文件。"""
    return get_memory_dir() / "MEMORY.md"




def _slugify(text: str) -> str:
    """把记忆名称转成适合文件名的短 slug。

    保留字母数字和 CJK 字符（中文记忆名很常见）；如果结果为空
    （如纯符号名称），用名称 hash 兜底，避免所有此类记忆挤进同一个
    `{type}_.md` 文件互相覆盖。
    """
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", text.lower())
    s = s.strip("_")
    return s[:40] or hashlib.md5(text.encode()).hexdigest()[:8]




def list_memories() -> list[MemoryEntry]:
    """读取当前项目所有 memory 文件，并按修改时间倒序返回。"""
    d = get_memory_dir()
    entries: list[MemoryEntry] = []
    for f in sorted(d.glob("*.md")):
        # MEMORY.md 是索引，不是一条真实记忆。
        if f.name == "MEMORY.md":
            continue
        try:
            result = parse_frontmatter(f.read_text())
            meta = result.meta
            # 没有 name/type 的文件不算合法 memory。
            if not meta.get("name") or not meta.get("type"):
                continue
            # type 不合法时降级为 project，避免坏文件中断列表。
            t = meta["type"] if meta["type"] in VALID_TYPES else "project"
            entries.append(MemoryEntry(
                name=meta["name"],
                description=meta.get("description", ""),
                type=t,
                filename=f.name,
                content=result.body,
            ))
        except Exception:
            pass
    # 最近修改的记忆排在前面，方便 /memory 展示。
    entries.sort(key=lambda e: (d / e.filename).stat().st_mtime, reverse=True)
    return entries


def save_memory(name: str, description: str, type: str, content: str) -> str:
    """保存一条 memory，并刷新 MEMORY.md 索引。"""
    d = get_memory_dir()
    filename = f"{type}_{_slugify(name)}.md"
    meta = {
        "name": name,
        "description": description,
        "type": type,
        # modified 时间戳自动维护，供后续做陈旧记忆清理/衰减。
        "modified": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # usage tracking 字段（供进化联动使用）
        "recall_count": 0,
        "last_recalled": "",
        "success_associated": 0,
        "status": "active",
    }
    text = format_frontmatter(meta, content)
    (d / filename).write_text(text)
    _update_memory_index()
    return filename


def delete_memory(filename: str) -> bool:
    """按文件名删除 memory，删除成功后刷新索引。"""
    filepath = get_memory_dir() / filename
    if not filepath.exists():
        return False
    filepath.unlink()
    _update_memory_index()
    return True




def _update_memory_index() -> None:
    """根据当前 memory 文件重新生成 MEMORY.md。"""
    memories = list_memories()
    lines = ["# Memory Index", ""]
    for m in memories:
        lines.append(f"- **[{m.name}]({m.filename})** ({m.type}) — {m.description}")
    _get_index_path().write_text("\n".join(lines))


def load_memory_index() -> str:
    """读取 MEMORY.md，并在注入 system prompt 前做长度保护。"""
    index_path = _get_index_path()
    if not index_path.exists():
        return ""
    content = index_path.read_text()
    lines = content.split("\n")
    if len(lines) > MAX_INDEX_LINES:
        content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n\n[... truncated, too many memory entries ...]"
    # 按字节截断时必须保证截断点落在字符边界上，不能切半个多字节字符。
    encoded = content.encode()
    if len(encoded) > MAX_INDEX_BYTES:
        cut = encoded[:MAX_INDEX_BYTES]
        content = cut.decode(errors="ignore") + "\n\n[... truncated, index too large ...]"
    return content


# ─── Memory Header (lightweight scan) ──────────────────────

class MemoryHeader:
    """轻量 memory 摘要，只包含召回筛选需要的元信息。"""

    __slots__ = ("filename", "file_path", "mtime_ms", "description", "type")

    def __init__(self, filename: str, file_path: str, mtime_ms: float,
                 description: str | None, type: str | None):
        self.filename = filename
        self.file_path = file_path
        self.mtime_ms = mtime_ms
        self.description = description
        self.type = type


MAX_MEMORY_FILES = 200                    # 参与召回筛选的最多 memory 文件数。
MAX_MEMORY_BYTES_PER_FILE = 4096          # 单个 memory 注入前的最大字节数。
MAX_SESSION_MEMORY_BYTES = 60 * 1024      # 单个会话最多注入的 memory 总量。


def scan_memory_headers() -> list[MemoryHeader]:
    """快速扫描 memory 文件头，不读取完整正文，用于低成本召回筛选。"""
    d = get_memory_dir()
    headers: list[MemoryHeader] = []
    for f in d.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        try:
            stat = f.stat()
            raw = f.read_text()
            # 只解析前 30 行，通常 frontmatter 足够在文件开头完成。
            first30 = "\n".join(raw.split("\n")[:30])
            result = parse_frontmatter(first30)
            meta = result.meta
            t = meta.get("type")
            headers.append(MemoryHeader(
                filename=f.name,
                file_path=str(f),
                mtime_ms=stat.st_mtime * 1000,
                description=meta.get("description"),
                type=t if t in VALID_TYPES else None,
            ))
        except Exception:
            pass
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    return headers[:MAX_MEMORY_FILES]


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    """把 memory 摘要列表格式化成给 side query 阅读的 manifest。"""
    lines = []
    for h in headers:
        tag = f"[{h.type}] " if h.type else ""
        ts = datetime.fromtimestamp(h.mtime_ms / 1000, tz=timezone.utc).isoformat()
        if h.description:
            lines.append(f"- {tag}{h.filename} ({ts}): {h.description}")
        else:
            lines.append(f"- {tag}{h.filename} ({ts})")
    return "\n".join(lines)


# ─── Memory Age / Freshness ────────────────────────────────

def memory_age(mtime_ms: float) -> str:
    """把修改时间转换成适合展示的相对时间。"""
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def memory_freshness_warning(mtime_ms: float) -> str:
    """旧记忆可能过期，注入时提醒模型先核对当前代码。"""
    days = max(0, int((time.time() * 1000 - mtime_ms) / 86_400_000))
    if days <= 1:
        return ""
    return (f"This memory is {days} days old. Memories are point-in-time observations, "
            "not live state — claims about code behavior may be outdated. "
            "Verify against current code before asserting as fact.")



SELECT_MEMORIES_PROMPT = """You are selecting memories that will be useful to an AI coding assistant as it processes a user's query. You will be given the user's query and a list of available memory files with their filenames and descriptions.

Return a JSON object with a "selected_memories" array of filenames for the memories that will clearly be useful (up to 5). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful, do not include it.
- If no memories would clearly be useful, return an empty array.

IMPORTANT: Do NOT answer the user's query. Do NOT explain. Respond with ONLY the JSON object, nothing else. Example: {"selected_memories": ["project_build.md"]}"""


class RelevantMemory:
    """被召回并准备注入对话的完整 memory。"""

    __slots__ = ("path", "content", "mtime_ms", "header")

    def __init__(self, path: str, content: str, mtime_ms: float, header: str):
        self.path = path
        self.content = content
        self.mtime_ms = mtime_ms
        self.header = header

    @property
    def size(self) -> int:
        """当前 memory 内容占用的字节数，供 Agent 统计本会话注入预算。"""
        return len(self.content.encode())


async def select_relevant_memories(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
) -> list[RelevantMemory]:
    """
    从 memory 目录中选择和当前 query 最相关的记忆。

    流程：
    1. 扫描 memory 头信息，避免一开始就读取所有正文。
    2. 排除本 session 已经注入过的 memory，避免重复污染上下文。
    3. 把候选摘要交给 side query，让模型选择最多 5 个文件名。
    4. 读取被选中的 memory 正文，截断过大的文件。
    5. 包装成 RelevantMemory，交给后续注入逻辑。
    """

    # 扫描所有 memory 文件头信息。
    headers = scan_memory_headers()

    if not headers:
        return []

    # 排除已经展示过的 memory。
    candidates = [h for h in headers if h.file_path not in already_surfaced]
    if not candidates:
        return []

    # manifest 是给 side query 看的候选摘要列表。
    manifest = format_memory_manifest(candidates)

    # 调用 side_query，让模型根据文件名和描述挑选相关 memory。
    try:
        text = await side_query(
            SELECT_MEMORIES_PROMPT,
            f"Query: {query}\n\nAvailable memories:\n{manifest}",
        )

        # side query 可能返回解释文本，这里只提取其中的 JSON 对象。
        match = re.search(r"\{[\s\S]*\}", text)
        selected_filenames: list[str] = []
        if match:
            try:
                parsed = json.loads(match.group(0))
                selected_filenames = parsed.get("selected_memories", [])
            except Exception:
                selected_filenames = []
        if not selected_filenames:
            # 兜底：模型没按 JSON 返回时，若回复里直接出现了候选文件名，
            # 也视为选中（按候选顺序），避免一次格式偏差导致召回全空。
            selected_filenames = [h.filename for h in candidates if h.filename in text]
        # 按 LLM 返回的顺序（相关性排序）取候选，最多 5 个。
        by_name = {h.filename: h for h in candidates}
        selected = [by_name[n] for n in selected_filenames if n in by_name][:5]

        result: list[RelevantMemory] = []
        for h in selected:
            # 读取每个选中的 memory 文件内容。
            content = Path(h.file_path).read_text()
            # 如果文件太大，就按字节截断（保证不切半个多字节字符）。
            encoded = content.encode()
            if len(encoded) > MAX_MEMORY_BYTES_PER_FILE:
                content = encoded[:MAX_MEMORY_BYTES_PER_FILE].decode(errors="ignore") + "\n\n[... truncated, memory file too large ...]"

            # 根据 memory 修改时间生成提示头；旧记忆会附带 freshness warning。
            freshness = memory_freshness_warning(h.mtime_ms)
            header_text = (
                f"{freshness}\n\nMemory: {h.file_path}:" if freshness
                else f"Memory (saved {memory_age(h.mtime_ms)}): {h.file_path}:"
            )
            # 返回 RelevantMemory 列表，后续会被格式化成 <system-reminder>。
            result.append(RelevantMemory(
                path=h.file_path, content=content,
                mtime_ms=h.mtime_ms, header=header_text,
            ))
        return result
    except Exception as e:
        # 召回失败不应该影响主对话；取消类错误直接静默。
        if "cancel" in str(e).lower():
            return []
        print_warning(f"[memory] semantic recall failed: {e}")
        return []


class MemoryPrefetch:
    """封装 memory 召回异步任务，供 Agent 主循环轮询。"""

    def __init__(self, task: asyncio.Task):
        self.task = task
        # consumed 表示结果是否已经注入过，避免同一个任务结果重复使用。
        self.consumed = False

    @property
    def settled(self) -> bool:
        """任务是否已经完成。"""
        return self.task.done()


def start_memory_prefetch(
    query: str,
    side_query: SideQueryFn,
    already_surfaced: set[str],
    session_memory_bytes: int,
) -> MemoryPrefetch | None:
    """
    在主模型回复前，提前异步启动 memory 召回。

    返回值不是 memory 内容，而是 MemoryPrefetch 句柄。
    Agent 主循环后续会检查任务是否完成，完成后再把 memory 注入当前消息。
    already_surfaced 是"仍在冷却期内"的 memory 路径集合（由调用方按
    轮次衰减计算），冷却到期后会重新参与召回。
    """

    # 触发条件：含空格的多词输入，或足够长的单串输入（中文等 CJK 文本
    # 通常不含空格，用长度门控保证中文 query 也能触发召回）。
    q = query.strip()
    if not q:
        return None
    if not re.search(r"\s", q) and len(q) < 4:
        return None

    # 当前 session 的 memory 使用量不能超过预算。
    if session_memory_bytes >= MAX_SESSION_MEMORY_BYTES:
        return None

    # memory 目录里必须真的有 memory 文件。
    d = get_memory_dir()
    has_memories = any(f.suffix == ".md" and f.name != "MEMORY.md" for f in d.iterdir())
    if not has_memories:
        return None

    # 条件通过后创建异步任务，让召回和主模型请求并行推进。
    task = asyncio.create_task(
        select_relevant_memories(query, side_query, already_surfaced)
    )
    return MemoryPrefetch(task)


def format_memories_for_injection(memories: list[RelevantMemory]) -> str:
    """把召回的 memory 包成 system-reminder，便于注入到用户消息。"""
    parts = []
    for m in memories:
        parts.append(f"<system-reminder>\n{m.header}\n\n{m.content}\n</system-reminder>")
    return "\n\n".join(parts)


def build_memory_prompt_section() -> str:
    """
    生成注入 system prompt 的 Memory System 说明。

    这段说明告诉模型：
    - memory 文件存放在哪里；
    - 有哪些 memory 类型；
    - 如何通过 write_file 保存 memory；
    - 哪些内容不应该保存；
    - 当前 MEMORY.md 索引里有哪些记忆。
    """
    index = load_memory_index()
    memory_dir = str(get_memory_dir())

    return f"""# Memory System

You have a persistent, file-based memory system at `{memory_dir}`.

## Memory Types
- **user**: User's role, preferences, knowledge level
- **feedback**: Corrections and guidance from the user (include Why + How to apply)
- **project**: Ongoing work, goals, deadlines, decisions
- **reference**: Pointers to external resources (URLs, tools, dashboards)

## How to Manage Memories
Use the `memory` tool — do NOT hand-write memory files with write_file.

- **add** a new memory:
  `memory(action="add", name="...", type="user|feedback|project|reference", description="one-line", content="...")`
- **replace** an existing memory (match by a unique substring of name/filename/description):
  `memory(action="replace", match="...", content="new content")`
- **remove** an outdated memory:
  `memory(action="remove", match="...")`

Rules:
- Prefer updating an existing memory (replace) over adding a near-duplicate. If `add` reports a duplicate or capacity error, consolidate first, then retry.
- Keep each memory short and focused; the MEMORY.md index is auto-maintained.
- The `modified` timestamp is updated automatically.

## What NOT to Save
- Code patterns or architecture (read the code instead)
- Git history (use git log)
- Anything already in CLAUDE.md
- Ephemeral task details

## When to Recall
When the user asks you to remember or recall, or when prior context seems relevant.
{chr(10) + "## Current Memory Index" + chr(10) + index if index else chr(10) + "(No memories saved yet.)"}"""


# ─── Hermes-style memory tool (add / replace / remove) ─────

# 注入安全：记忆内容会被包进 <system-reminder> 注入对话，也可能被
# parse_frontmatter 解析。禁止能伪造 reminder 边界或 frontmatter 的标记。
_FORBIDDEN_MARKERS = ("</system-reminder>", "<system-reminder>")


def _validate_memory_content(content: str) -> str | None:
    """基础校验：返回错误消息或 None（通过）。"""
    body = content.strip()
    if not body:
        return "Memory content is empty."
    lowered = body.lower()
    for marker in _FORBIDDEN_MARKERS:
        if marker in lowered:
            return f"Memory content contains forbidden marker '{marker}' (prompt-injection guard)."
    if len(body.encode()) > MAX_MEMORY_BYTES_PER_FILE:
        return (
            f"Memory content is {len(body.encode())} bytes, exceeding the "
            f"{MAX_MEMORY_BYTES_PER_FILE}-byte limit. Split it into several "
            "focused memories instead."
        )
    return None


def _current_entries_hint() -> str:
    """容量超限/匹配失败时，把现有条目列表返回给模型，要求先合并/删除再重试。"""
    entries = list_memories()
    if not entries:
        return ""
    lines = ["Current memory entries:"]
    for e in entries:
        lines.append(f"- {e.filename} (name: {e.name}, type: {e.type}) — {e.description}")
    return "\n".join(lines)


def _find_entry_by_match(match: str) -> tuple[MemoryEntry | None, list[MemoryEntry]]:
    """按子串匹配 name/filename/description，返回 (唯一命中或 None, 所有命中)。"""
    needle = match.strip().lower()
    hits = []
    for e in list_memories():
        haystack = f"{e.name}\n{e.filename}\n{e.description}".lower()
        if needle in haystack:
            hits.append(e)
    if len(hits) == 1:
        return hits[0], hits
    return None, hits


def memory_tool(action: str, **kwargs: Any) -> dict[str, Any]:
    """
    Hermes 风格的记忆管理工具。

    - add: 新增一条记忆（容量上限 + 精确去重 + modified 时间戳）。
    - replace: 用 match 子串定位唯一记忆后整体替换内容。
    - remove: 用 match 子串定位唯一记忆后删除。

    失败时返回 ok=False + error + 现有条目列表，引导模型先合并/删除再重试。
    """
    action = (action or "").strip().lower()

    if action == "add":
        name = str(kwargs.get("name") or "").strip()
        mtype = str(kwargs.get("type") or "").strip().lower() or "project"
        description = str(kwargs.get("description") or "").strip()
        content = str(kwargs.get("content") or "")
        if not name:
            return {"ok": False, "error": "Field 'name' is required for add."}
        if mtype not in VALID_TYPES:
            return {"ok": False, "error": f"Invalid type '{mtype}'. Valid types: {sorted(VALID_TYPES)}"}
        err = _validate_memory_content(content)
        if err:
            return {"ok": False, "error": err, "hint": _current_entries_hint()}
        # 精确去重：内容完全相同（忽略首尾空白）的记忆已存在时拒绝重复添加。
        new_body = content.strip()
        for e in list_memories():
            if e.content.strip() == new_body:
                return {
                    "ok": False,
                    "error": f"Duplicate memory: identical content already exists in {e.filename}. Update it with action=replace instead of adding again.",
                }
        filename = save_memory(name, description, mtype, content.strip())
        return {"ok": True, "action": "add", "filename": filename}

    if action in ("replace", "remove"):
        match = str(kwargs.get("match") or "").strip()
        if not match:
            return {"ok": False, "error": f"Field 'match' is required for {action}."}
        entry, hits = _find_entry_by_match(match)
        if entry is None:
            if not hits:
                return {
                    "ok": False,
                    "error": f"No memory matches '{match}'.",
                    "hint": _current_entries_hint(),
                }
            names = ", ".join(f"{h.filename} ({h.name})" for h in hits)
            return {
                "ok": False,
                "error": f"'{match}' matches {len(hits)} memories: {names}. Use a more specific match string.",
            }

        if action == "remove":
            delete_memory(entry.filename)
            return {"ok": True, "action": "remove", "removed": entry.filename}

        # replace
        content = str(kwargs.get("content") or "")
        err = _validate_memory_content(content)
        if err:
            return {"ok": False, "error": err}
        name = str(kwargs.get("name") or "").strip() or entry.name
        description = str(kwargs.get("description") or "").strip() or entry.description
        mtype = str(kwargs.get("type") or "").strip().lower() or entry.type
        if mtype not in VALID_TYPES:
            mtype = entry.type
        d = get_memory_dir()
        meta = {
            "name": name,
            "description": description,
            "type": mtype,
            "modified": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (d / entry.filename).write_text(format_frontmatter(meta, content.strip()))
        _update_memory_index()
        return {"ok": True, "action": "replace", "filename": entry.filename}

    return {"ok": False, "error": f"Unknown action '{action}'. Valid actions: add, replace, remove."}


# ─── Memory Usage Tracking (for evolution linkage) ────────────────────────────


def record_memory_recall(filename: str) -> None:
    """记录 memory 被召回一次。"""
    d = get_memory_dir()
    fpath = d / filename
    if not fpath.is_file():
        return
    try:
        raw = fpath.read_text()
        result = parse_frontmatter(raw)
        meta = result.meta
        meta["recall_count"] = int(meta.get("recall_count", 0)) + 1
        meta["last_recalled"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        fpath.write_text(format_frontmatter(meta, result.body))
    except Exception:
        pass


def record_memory_success(filename: str) -> None:
    """记录 memory 召回后任务成功。"""
    d = get_memory_dir()
    fpath = d / filename
    if not fpath.is_file():
        return
    try:
        raw = fpath.read_text()
        result = parse_frontmatter(raw)
        meta = result.meta
        meta["success_associated"] = int(meta.get("success_associated", 0)) + 1
        fpath.write_text(format_frontmatter(meta, result.body))
    except Exception:
        pass


def get_memory_usage_stats(filename: str) -> dict[str, Any]:
    """获取 memory 的 usage tracking 统计。"""
    d = get_memory_dir()
    fpath = d / filename
    if not fpath.is_file():
        return {}
    try:
        raw = fpath.read_text()
        result = parse_frontmatter(raw)
        meta = result.meta
        return {
            "recall_count": int(meta.get("recall_count", 0)),
            "last_recalled": meta.get("last_recalled", ""),
            "success_associated": int(meta.get("success_associated", 0)),
            "status": meta.get("status", "active"),
        }
    except Exception:
        return {}


def maybe_promote_or_archive_memory(
    filename: str,
    *,
    promote_threshold: int = 20,
    promote_success_rate: float = 0.6,
    archive_days: int = 30,
) -> dict[str, Any]:
    """根据 usage tracking 自动提权或归档 memory。

    高频成功关联 → 提权（priority=high）
    长期未用 → 归档（status=archived）

    Returns:
        {"action": "promote" | "archive" | "none", "reason": str}
    """
    stats = get_memory_usage_stats(filename)
    if not stats:
        return {"action": "none", "reason": "memory not found"}

    recall_count = stats.get("recall_count", 0)
    success_associated = stats.get("success_associated", 0)
    last_recalled = stats.get("last_recalled", "")
    status = stats.get("status", "active")

    # 归档检查
    if status != "archived" and recall_count == 0 and last_recalled:
        try:
            last_dt = datetime.fromisoformat(last_recalled.replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - last_dt).days
            if days_since > archive_days:
                _set_memory_status(filename, "archived")
                from .observability.trace import trace_event
                trace_event(
                    "memory.archive",
                    filename=filename,
                    days_since_last_recalled=days_since,
                )
                return {"action": "archive", "reason": f"not recalled for {days_since} days"}
        except Exception:
            pass

    # 提权检查
    if recall_count >= promote_threshold:
        success_rate = success_associated / recall_count if recall_count > 0 else 0
        if success_rate >= promote_success_rate:
            _set_memory_priority(filename, "high")
            from .observability.trace import trace_event
            trace_event(
                "memory.promote",
                filename=filename,
                recall_count=recall_count,
                success_rate=success_rate,
            )
            return {"action": "promote", "reason": f"high usage ({recall_count} recalls, {success_rate:.0%} success)"}

    return {"action": "none", "reason": "thresholds not met"}


def _set_memory_status(filename: str, status: str) -> None:
    """设置 memory 的 status 字段。"""
    d = get_memory_dir()
    fpath = d / filename
    if not fpath.is_file():
        return
    try:
        raw = fpath.read_text()
        result = parse_frontmatter(raw)
        result.meta["status"] = status
        fpath.write_text(format_frontmatter(result.meta, result.body))
    except Exception:
        pass


def _set_memory_priority(filename: str, priority: str) -> None:
    """设置 memory 的 priority 字段。"""
    d = get_memory_dir()
    fpath = d / filename
    if not fpath.is_file():
        return
    try:
        raw = fpath.read_text()
        result = parse_frontmatter(raw)
        result.meta["priority"] = priority
        fpath.write_text(format_frontmatter(result.meta, result.body))
    except Exception:
        pass


def maintenance_all_memories() -> dict[str, Any]:
    """对所有 memory 执行 maintenance（提权/归档检查）。"""
    d = get_memory_dir()
    results: list[dict[str, Any]] = []
    for f in d.glob("*.md"):
        if f.name == "MEMORY.md":
            continue
        result = maybe_promote_or_archive_memory(f.name)
        if result.get("action") != "none":
            results.append({"filename": f.name, **result})

    from .observability.trace import trace_event
    trace_event(
        "memory.maintenance",
        total_checked=len(list(d.glob("*.md"))) - 1,
        actions_taken=len(results),
    )

    return {"checked": len(list(d.glob("*.md"))) - 1, "actions": results}


# ─── 记忆衰减 ─────────────────────────────────────────────────────────────────

import math
from datetime import datetime, timezone


def compute_memory_score(entry: MemoryEntry) -> float:
    """计算记忆的重要性分数
    
    综合考虑：
    - 时间衰减（最近使用的记忆更重要）
    - 使用频率（被召回次数多的更重要）
    - 成功关联（与成功任务关联的更重要）
    """
    # 时间衰减（30 天半衰期）
    modified = entry.meta.get("modified", "")
    if modified:
        try:
            mod_time = datetime.fromisoformat(modified.replace("Z", "+00:00"))
            days_since_modified = (datetime.now(timezone.utc) - mod_time).days
            recency = math.exp(-days_since_modified * math.log(2) / 30)  # 30 天半衰期
        except Exception:
            recency = 0.5
    else:
        recency = 0.5
    
    # 使用频率（对数缩放）
    recall_count = entry.meta.get("recall_count", 0)
    frequency = math.log(1 + recall_count) / 10
    
    # 成功关联
    success_associated = entry.meta.get("success_associated", 0)
    success = min(1.0, success_associated / 5)
    
    # 综合分数
    return 0.5 * recency + 0.3 * frequency + 0.2 * success


def auto_prune_memories(threshold: float = 0.1, dry_run: bool = False) -> list[str]:
    """自动清理低分数记忆
    
    Args:
        threshold: 分数阈值，低于此值的记忆将被归档
        dry_run: 如果为 True，只返回将被清理的记忆列表，不实际清理
    
    Returns:
        被清理（或将被清理）的记忆文件名列表
    """
    entries = list_memories()
    pruned = []
    
    for entry in entries:
        score = compute_memory_score(entry)
        if score < threshold:
            if not dry_run:
                # 归档而非删除（移动到 archive 子目录）
                archive_dir = get_memory_dir() / "archive"
                archive_dir.mkdir(exist_ok=True)
                src = get_memory_dir() / entry.filename
                dst = archive_dir / entry.filename
                if src.exists():
                    src.rename(dst)
            pruned.append(entry.filename)
    
    if pruned:
        _update_memory_index()
    
    return pruned
