"""TaskList 存储 — JSON 文件持久化。

存储在 ~/.mycode/todos/{session_id}.json（目录名刻意保留，见 get_tasks_dir）。

本模块是纯 JSON IO 层：**禁止 import agents.core.session**。事件 seq 一律由调用方
作为参数传入（`detail_origin_seq` / `current_seq`），以保持 store 与事件流解耦。
（`agents.logging` 是允许的：它是叶子模块，隔离坏文件时必须留痕，见 _quarantine。）
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Sequence

from agents.core.workspace import get_workspace
from agents.logging import print_error


TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_SKIPPED = "skipped"
TASK_STATUS_FAILED = "failed"

VALID_STATUSES = {
    TASK_STATUS_PENDING,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_SKIPPED,
    TASK_STATUS_FAILED,
}

# 旧 JSON 里的 cancelled 归一化为 skipped，不做文件迁移
_LEGACY_STATUS_ALIASES = {"cancelled": TASK_STATUS_SKIPPED}

# 刻意**没有**任务级优先级：Plan 3a 删除了 TaskItem.priority。它没有任何读者——
# format_task_list_block 不渲染它、find_focus 不用它做 tie-break，只有 list 返回它，
# 而 S 常驻之后模型没有理由调 list。把它变成 find_focus 的 tie-break 会贬低列表
# 顺序（列表顺序正是自锚守卫存在的前提），在 S 里渲染它则是每请求付费去表达一个
# 模型无法据此行动的东西。旧 JSON 里的 priority 键被 from_dict 自然忽略，无需迁移。
# 注意与**计划级**的 Plan.priority（P0/P1/P2，agents/plan/plan_models.py）无关：那是
# plan 文档的元数据，由 create_plan 写进 _meta.md、经 REST 的 get_plan 端点读出，
# 与 task_list 的字段集是两回事（Plan 3a Task 4 之后它已无 Python 侧读者）。


def _normalize_status(value: str) -> str:
    value = _LEGACY_STATUS_ALIASES.get(value, value)
    return value if value in VALID_STATUSES else TASK_STATUS_PENDING


def _as_str(value: Any) -> str:
    """把任意 JSON 值收窄为 str；非字符串（int/dict/list/None）一律变空串。

    模型可能给 detail 传一个步骤数组或给 acceptance 传一个对象。不收窄就会让
    .strip() 在每请求路径上抛 AttributeError，被 model_caller 与 prompt_runtime
    两处宽 except 吞掉——推式与常驻两层会永久静默关闭，而工具早已回过 ok:true，
    模型没有任何可见的恢复路径。这里是最后一道防线；第一道在工具边界。
    """
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int:
    """把任意 JSON 值收窄为 int；非整数一律 0。

    与 _as_str 同一理由：Plan 3b 的 UI 写端点会让 id 从 JSON 请求体进来。
    一个字符串 id 会静默让 mark_detail_disclosed 的 item.id == task_id 匹配失败，
    于是每次调用都重注入最多 6000 字符——与 C1 同类的静默黏性缺陷。

    bool 必须显式排除：它是 int 的子类，`isinstance(True, int)` 为真，不排除的话
    `{"id": true}` 会变成 1 并匹配到 id=1 的真实任务——比失配更糟，那是**错配**。
    """
    if isinstance(value, bool):        # bool 是 int 的子类，必须排除
        return 0
    return value if isinstance(value, int) else 0


def get_tasks_dir() -> Path:
    # 目录名刻意保留 "todos"：非模型可见契约，改名需迁移回退路径而收益为零
    d = get_workspace() / ".mycode" / "todos"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class TaskItem:
    id: int
    content: str
    status: str = TASK_STATUS_PENDING
    created_at: str = ""
    updated_at: str = ""
    detail: str = ""
    acceptance: str = ""
    detail_origin_seq: int | None = None
    started_seq: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskItem:
        seq = data.get("detail_origin_seq")
        started = data.get("started_seq")
        # content/detail/acceptance/error 四个字符串字段一律过 _as_str：既收窄
        # nullity（显式 JSON null，例如未来 UI/API 写入方）也收窄**类型**（模型
        # 给 detail 传步骤数组、给 acceptance 传对象——网关不强制 schema 类型）。
        # 不收窄就会让 needs_disclosure / format_disclosure_block / _first_line 的
        # .strip() 在每模型请求的路径上抛 AttributeError，而那两处宽 except 会把它
        # 吞成「推式层与常驻层永久静默关闭」。在 from_dict 收窄对任何写入方、任何
        # 消费方都生效。
        # id 同理过 _as_int：字符串 id 会让 mark_detail_disclosed 的等值比较静默
        # 失配，披露于是每轮重来（见 _as_int 的 docstring）。
        # 旧文件里的 priority 键在这里被自然忽略——from_dict 按字段名取值，
        # 多余的键根本不读，所以删字段不需要迁移脚本。
        return cls(
            id=_as_int(data.get("id")),
            content=_as_str(data.get("content")),
            status=_normalize_status(data.get("status", TASK_STATUS_PENDING)),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            detail=_as_str(data.get("detail")),
            acceptance=_as_str(data.get("acceptance")),
            detail_origin_seq=seq if isinstance(seq, int) else None,
            started_seq=started if isinstance(started, int) else None,
            error=_as_str(data.get("error")),
        )


@dataclass
class TaskList:
    session_id: str
    tasks: list[TaskItem] = field(default_factory=list)
    next_id: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tasks": [t.to_dict() for t in self.tasks],
            "next_id": self.next_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskList:
        raw = data.get("tasks")
        if raw is None:
            raw = data.get("todos", [])  # 旧文件用 "todos" 键
        return cls(
            session_id=data.get("session_id", ""),
            tasks=[TaskItem.from_dict(t) for t in raw],
            next_id=data.get("next_id", 1),
        )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_tasks(session_id: str) -> TaskList:
    tasks_dir = get_tasks_dir()
    path = tasks_dir / f"{session_id}.json"
    if not path.exists():
        return TaskList(session_id=session_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # 读/解析失败不能退化成「空清单」：所有 mutating 调用方都是 load → mutate
        # → save，空清单会让下一次 save 直接覆盖掉整个文件，静默丢数据。把坏文件
        # 挪到一边保留证据再返回空清单，工具仍可用。异常范围刻意收窄——意料之外
        # 的错误应当抛出来，不该被当成「文件坏了」。
        return _quarantine(path, session_id, f"unreadable/invalid JSON: {e!r}")
    try:
        return TaskList.from_dict(data)
    except (AttributeError, TypeError, KeyError) as e:
        # JSON 合法但**形状**非法（例如 {"tasks": "x"}、顶层是数组、条目是标量、
        # status 是不可哈希的对象）。此前 from_dict 在上面那个 try 内部，而这些
        # 异常不在收窄的元组里 → 不隔离 → 文件永远坏着、每请求重抛，并让
        # GET /api/tasks/{id} 永久 500。形状损坏与读/解析损坏走同一条隔离路径，
        # 但日志文案可区分，operator 才分得清是文件被截断还是写入方写错了结构。
        # 收窄依然是刻意的：这三类之外的错误（编程错误）应当抛出来。
        return _quarantine(path, session_id, f"shape-corrupt JSON: {e!r}")


def _quarantine(path: Path, session_id: str, reason: str) -> TaskList:
    """把坏文件挪到一边并**打日志**，返回空清单。

    隔离本身改动了用户的文件系统，而两层（S 与推式披露）会随之同时消失——不打
    日志的话唯一证据是一个要人手工去 `~/.mycode/todos/` 里找的重命名文件。rename
    失败（跨设备、权限、Windows 上被占用）也要响一声，否则就是「隔离没发生且
    无人知晓」。
    """
    quarantine = path.with_name(f"{path.stem}.corrupt-{int(time.time())}.json")
    try:
        path.rename(quarantine)
    except OSError as e:
        print_error(
            f"[task_store] {reason}; quarantine rename failed for {path}: {e!r}"
        )
        return TaskList(session_id=session_id)
    print_error(
        f"[task_store] {reason}; quarantined {path} -> {quarantine}"
    )
    return TaskList(session_id=session_id)


def save_tasks(task_list: TaskList) -> None:
    """原子写：同目录临时文件 + os.replace。

    此前是 truncate-then-write（`path.write_text`），非原子且原地。**本分支改变了
    一次撕裂写入的含义**：store 现在是每请求上下文层（S 与推式披露）的承重件、
    读取翻倍、Plan 3 还要加并发 UI 写入方，所以撕裂写入意味着「模型静默失去它的
    计划」，而不是「面板空了」；而 load_tasks 的隔离缓解对半截 JSON 也只是把数据
    挪走。等并发写入方落地后再补原子性就不是几行了。

    同目录是必需的：os.replace 只在同一文件系统上原子。临时名以 "." 开头且带
    .tmp- 中缀，既不会被 `*.json` 的读取方撞上，也不会与真实 session 文件同名；
    mkstemp 的随机后缀让并发写入方（Plan 3 的 UI 端点）不会互相踩临时文件。
    刻意不 fsync——威胁模型是并发/被打断的写入留下半截文件，不是掉电。

    刻意**不复用** session.py 的 atomic_write_text：(1) 本模块禁止 import
    agents.core.session；(2) 那个 helper 用 tmp.write_text 不带 encoding（跟随
    locale）且用 Path.rename（Windows 上目标已存在会失败），两处都比这里弱。

    副作用：mkstemp 建的文件是 0600，而 write_text 之前是 0644。store 在
    ~/.mycode 下、只由同一用户的进程读写，收紧到 0600 无害且更合适。
    """
    tasks_dir = get_tasks_dir()
    path = tasks_dir / f"{task_list.session_id}.json"
    payload = json.dumps(task_list.to_dict(), indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{task_list.session_id}.json.tmp-", dir=tasks_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        # 写入或替换失败时不能把临时文件留在 store 目录里；目标文件因为还没被
        # 碰过，仍是上一份完整内容（这正是原子性买到的东西）。
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _insert_after(tasks: list[TaskItem], item: TaskItem, after_id: int | None) -> None:
    """原地插入。after_id=None 追加末尾；0 插到最前；id 不存在则退回追加末尾。"""
    if after_id is None:
        tasks.append(item)
        return
    if after_id == 0:
        tasks.insert(0, item)
        return
    for index, existing in enumerate(tasks):
        if existing.id == after_id:
            tasks.insert(index + 1, item)
            return
    tasks.append(item)


def add_task(
    session_id: str,
    content: str,
    detail: str = "",
    acceptance: str = "",
    after_id: int | None = None,
    current_seq: int | None = None,
) -> TaskItem:
    # current_seq = 承载本次 tool_calls 的 assistant 事件 seq，由调用方传入
    # （dispatcher → handle_task_list），理由同 update_task：store 不 import session。
    #
    # detail 非空且 current_seq 有值时记账 detail_origin_seq：模型自己写的 detail
    # 正躺在那次 tool_calls 的参数里，折叠前一直可见，再注入一份是纯重复——与
    # update(detail=...) 的重指向裁定对称（此前 add 没有归属人，于是
    # add(detail=...) 每次都会造出设计 §五 点名要消灭的那次重复注入）。
    #
    # current_seq 为 None 时保持 None，needs_disclosure 于是无条件注入——这正是
    # Plan 3 物化路径要的：物化调用 add_task **不传** current_seq，因为物化出来的
    # detail 来自磁盘上的 tasks.md，不在模型自己的 tool_calls 里。
    # _as_str 只是让这行对非 str 入参也保持 total（不新增一条 AttributeError 路径）；
    # 类型校验本身在 task_tools 的模型边界，见 _check_string_fields。
    origin_seq = (
        current_seq
        if current_seq is not None and _as_str(detail).strip()
        else None
    )
    task_list = load_tasks(session_id)
    now = _now_iso()
    item = TaskItem(
        id=task_list.next_id,
        content=content,
        status=TASK_STATUS_PENDING,
        created_at=now,
        updated_at=now,
        detail=detail,
        acceptance=acceptance,
        detail_origin_seq=origin_seq,
    )
    task_list.next_id += 1
    _insert_after(task_list.tasks, item, after_id)
    save_tasks(task_list)
    return item


def update_task(
    session_id: str,
    task_id: int,
    status: str | None = None,
    content: str | None = None,
    detail: str | None = None,
    acceptance: str | None = None,
    error: str | None = None,
    after_id: int | None = None,
    current_seq: int | None = None,
) -> TaskItem | None:
    # current_seq 由调用方传入（dispatcher 传承载本次 tool_calls 的 assistant
    # 消息的 seq），用于两处记账：
    # - started_seq：首次转入 in_progress 时写入，重入不覆盖（Plan 2 的验收
    #   闸门靠它界定扫描区间）。
    # - detail_origin_seq：detail 被编辑时重指向 current_seq。既不清空（模型
    #   自己刚写的 detail 正躺在那次 tool_calls 参数里，清空会导致一次纯重复
    #   注入）也不保持不动（指向旧事件会在旧事件被折叠而新编辑仍可见时多注入
    #   一次）。current_seq 为 None 时记为 None，代价是之后多披露一次——安全
    #   方向，不做特判。
    task_list = load_tasks(session_id)
    for item in task_list.tasks:
        if item.id == task_id:
            if status is not None and status in VALID_STATUSES:
                item.status = status
                if status == TASK_STATUS_IN_PROGRESS and item.started_seq is None:
                    item.started_seq = current_seq
            if content is not None:
                item.content = content
            if detail is not None:
                item.detail = detail
                item.detail_origin_seq = current_seq
            if acceptance is not None:
                item.acceptance = acceptance
            if error is not None:
                item.error = error
            if after_id is not None and after_id != task_id:
                task_list.tasks = [t for t in task_list.tasks if t.id != task_id]
                _insert_after(task_list.tasks, item, after_id)
            item.updated_at = _now_iso()
            save_tasks(task_list)
            return item
    return None


def remove_task(session_id: str, task_id: int) -> bool:
    task_list = load_tasks(session_id)
    original_len = len(task_list.tasks)
    task_list.tasks = [t for t in task_list.tasks if t.id != task_id]
    if len(task_list.tasks) < original_len:
        save_tasks(task_list)
        return True
    return False


def list_tasks(session_id: str) -> list[TaskItem]:
    task_list = load_tasks(session_id)
    return task_list.tasks


_FOCUS_STATUS_ORDER = (
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
)


def find_focus(tasks: list[TaskItem]) -> TaskItem | None:
    """推导焦点条：in_progress > failed > pending，各档取列表顺序第一个。

    读列表状态而非事件记账，所以乱序完成、中途插条、跳过、resume 都自愈。
    in_progress 优先意味着中途插入的 pending 条不会抢走正在执行任务的焦点。
    failed 排在 pending 之前，是旧 plan_continue 里 has_failed_tasks 硬闸门的
    软版本：不阻塞，但失败条会被披露出来，模型没法假装没看见。
    """
    for status in _FOCUS_STATUS_ORDER:
        for task in tasks:
            if task.status == status:
                return task
    return None


# 单次注入的 detail 体积上限。理由是 token 成本：这段文本每被折叠一次就要重注入
# 一次，不限长会让每次重注入的代价无上界，也会让承载它的那段缓存前缀频繁变动。
# 注意它**不是**为了躲开 persist_large_result（executor.py:27，30KB）或
# _truncate_result（registry.py:25-33，50K 字符）——那两个阈值只作用于工具结果，
# 而披露块走的是 memory_injection 事件（见 spec §五），根本不经它们。
# store 里始终存全量，只有注入块被截断。
DETAIL_DISCLOSURE_CHAR_LIMIT = 6000

# 披露块里单行字段（acceptance / error）的上限。error 是模型自填的自由文本，
# 通常是一段 traceback；不限长会让整个披露块的体积在这个轴上无界。
_DISCLOSURE_LINE_CHAR_LIMIT = 500

# content 的上限。content 的契约是「一句话摘要」（怎么做/为什么属于 detail），
# 所以比 acceptance/error 更紧；披露块标题与 S 的条目行**共用**这一个上限——
# content 在哪儿渲染都是同一句话摘要，没有理由在两处取不同的界。
_S_CONTENT_CHAR_LIMIT = 80

# S 是常驻块，每请求重发，所以它的单行上限必须远小于一次性披露块的上限。
# acceptance 的契约是「一条命令」（设计 §六），160 字符足够；error 只需认得出
# 是哪个失败，200 字符足够。_DISCLOSURE_LINE_CHAR_LIMIT (500) 保留给披露块。
_S_ACCEPTANCE_CHAR_LIMIT = 160
_S_ERROR_CHAR_LIMIT = 200


def _first_line(value: Any, limit: int) -> str:
    """单行化 + 限长。空/纯空白/None/非字符串返回 ""。

    S 与披露块共享同一条约束：常驻或注入的文本里不许出现换行——一个多行字段会把
    「一条任务一行」/「一个字段一行」的块结构撑散，模型也就没法再按行定位条目或
    字段。先 strip 再取首行，这样 content 以换行开头时不会渲染出一个空条目。

    `_as_str` 是防御性收窄：from_dict 已把四个字符串字段的显式 null 与非字符串
    归一成 ""，但 TaskItem 也可以被直接构造（例如未来的写端点绕过 from_dict）。
    S 每请求都渲染且外层包着 try/except，一个 None（或一个 int）字段会把整个常驻
    层吞掉，所以这里对所有插值字段一次性免疫，而不是逐个调用点去防。

    被截断时末尾追加 `…`：静默截断会让模型把半条 acceptance 当完整命令去执行
    （验收闸门正是叫它执行读到的那条命令），所以「这里被切过」必须是块里可读的
    事实。**两个轴都算截断**：横向（首行超过 limit）与纵向（首行之后的行被丢掉）
    ——只标横向的话，一条三行的 acceptance 会渲染成光秃秃的 `pytest -k a`，看起来
    正是一条完整命令，而这三个字段（content/acceptance/error）按约定都是单行的，
    多行是意外不是意图，所以披露块同样不豁免。
    `…` 让返回值比 limit 多 1 个字符，这是刻意的——limit 约束的是**保留的正文字符
    数**，标记不是正文（与 clip_detail 同一取舍，Plan 1 已裁定过）。未截断时逐字
    不变，不给短字段凭空加噪。
    """
    stripped = _as_str(value).strip()
    if not stripped:
        return ""
    lines = stripped.splitlines()
    first = lines[0].strip() if lines else ""
    if len(lines) > 1 or len(first) > limit:
        return first[:limit] + "…"
    return first


def clip_detail(detail: str) -> str:
    if len(detail) <= DETAIL_DISCLOSURE_CHAR_LIMIT:
        return detail
    kept = detail[:DETAIL_DISCLOSURE_CHAR_LIMIT]
    dropped = len(detail) - DETAIL_DISCLOSURE_CHAR_LIMIT
    return f"{kept}\n\n[... 已截断 {dropped} 字符；完整方案在 task_list store 里，用 task_list get 取 ...]"


def needs_disclosure(item: TaskItem | None, visible_seqs: Sequence[int]) -> bool:
    """焦点条的 detail 是否需要（重新）注入上下文。

    默认不披露：模型自己写的 detail 已经在它那次 tool_calls 的参数里，折叠前
    一直可见，再注入一份是纯重复。只有承载它的事件已被折叠隐藏、上下文被 clear、
    或从未注入过（detail_origin_seq is None，例如 plan 物化出来的）时才注入。
    """
    if item is None or not item.detail.strip():
        return False
    if item.detail_origin_seq is None:
        return True
    return item.detail_origin_seq not in visible_seqs


def mark_detail_disclosed(session_id: str, task_id: int, seq: int) -> None:
    """记录 detail 已进入上下文的承载事件 seq，作为下次判定的依据。"""
    task_list = load_tasks(session_id)
    for item in task_list.tasks:
        if item.id == task_id:
            item.detail_origin_seq = seq
            save_tasks(task_list)
            return


def format_disclosure_block(item: TaskItem) -> str:
    """把焦点条的详细执行方案拼成注入块。detail 为空时返回空串（不注入）。

    用 <system-reminder> 包裹，与 wiki 召回注入同一约定
    （agents/core/turn_runner.py:117-123）。

    三个插值字段（content / acceptance / error）一律走 _first_line —— 单行化 +
    限长。此前只有 acceptance/error 有界且不取首行，content 两样都没有，于是
    §五 的 6000 字符注入上限可被轻易击穿（实测：10 万字符 content + 10 字符
    detail → 100,113 字符的注入块，16.7×），而一条多行 acceptance 会把块的
    「一个字段一行」结构撑散。detail 是唯一允许多行且长到 6000 的字段，它由
    clip_detail 单独限界。
    """
    if not item.detail.strip():
        return ""

    lines = [
        "<system-reminder>",
        f"## 当前任务的执行方案（#{item.id} "
        f"{_first_line(item.content, _S_CONTENT_CHAR_LIMIT)}）",
    ]
    acceptance = _first_line(item.acceptance, _DISCLOSURE_LINE_CHAR_LIMIT)
    if acceptance:
        lines.append(f"验收: {acceptance}")
    error = _first_line(item.error, _DISCLOSURE_LINE_CHAR_LIMIT)
    if item.status == TASK_STATUS_FAILED and error:
        lines.append(f"上次失败原因: {error}")
    lines.append("")
    lines.append(clip_detail(item.detail))
    lines.append("")
    lines.append(
        f"（自动披露。开始执行时把 #{item.id} 标为 in_progress；"
        f"完成前需有通过的验收命令。）"
    )
    lines.append("</system-reminder>")
    return "\n".join(lines)


def format_task_list_block(tasks: list[TaskItem], focus: TaskItem | None) -> str:
    """清单摘要 S —— 常驻尾部 ephemeral 通道的内容。空清单返回 ""。

    刻意不含 detail：那会让常驻块变成全量方案，等于放弃渐进式披露（spec §六
    三层可见性——detail 只走「推」与「拉」两层，不走常驻层）。S 每请求都重发
    一次，所以这里出现的每个字符都是按请求数付费的。

    已完成/已跳过只报计数，不逐条列 content，所以 S 随计划推进而**收缩**。

    纯函数：只读传入的列表，不碰 store、不碰 session（本模块禁止 import
    session）。焦点条由调用方算好传进来（见 find_focus），本函数只负责渲染。
    """
    if not tasks:
        return ""

    done = sum(1 for t in tasks if t.status == TASK_STATUS_COMPLETED)
    skipped = sum(1 for t in tasks if t.status == TASK_STATUS_SKIPPED)
    listed = [
        t for t in tasks
        if t.status not in (TASK_STATUS_COMPLETED, TASK_STATUS_SKIPPED)
    ]

    header = f"# Task List  ({done}/{len(tasks)} done"
    if skipped:
        header += f", {skipped} skipped"
    header += ")"

    lines = [header]
    if not listed:
        # listed 为空有两种成因：真的全部完成，或剩下的全是 skipped。后者若也说
        # 「全部完成」，就和 header 的 `(0/3 done, 3 skipped)` 自相矛盾——S 是
        # 模型判断计划状态的常驻依据，footer 不能说错。
        lines.append("（全部完成）" if done == len(tasks) else "（无未完成条目）")
        return "\n".join(lines)

    focus_id = focus.id if focus is not None else None
    for task in listed:
        marker = ">" if task.id == focus_id else " "
        content = _first_line(task.content, _S_CONTENT_CHAR_LIMIT)
        line = f"{marker} #{task.id} {content}  [{task.status}]"
        # acceptance/error 用 S 自己的上限，**不复用**披露块的
        # _DISCLOSURE_LINE_CHAR_LIMIT (500)：披露块是一次性注入，S 每请求重发。
        # 沿用 500 会让「S 不含 detail」这条性质退化成字段级而非内容级——模型
        # 可以把 500 字符自由文本停在 acceptance 里让它变成常驻成本，而那正是
        # 常驻层存在的目的所要防止的。
        acceptance = _first_line(task.acceptance, _S_ACCEPTANCE_CHAR_LIMIT)
        if acceptance:
            line += f"  验收: {acceptance}"
        error = _first_line(task.error, _S_ERROR_CHAR_LIMIT)
        if task.status == TASK_STATUS_FAILED and error:
            line += f"  错误: {error}"
        lines.append(line)

    return "\n".join(lines)
