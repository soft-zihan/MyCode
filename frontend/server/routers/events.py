"""权限/提问响应 router + task_list 的 REST 读写面。

SSE 事件流端点已删除（零消费者——前端实时流走 WebSocket，且旧实现对流式事件
缺 seq 字段存在 KeyError）。

task_list 的四个端点（GET/POST/PATCH/DELETE）让 UI 成为 task_list 的**第二个写入方**
（第一个是模型经工具层）。写端点绕过工具层，所以工具层那些边界校验必须在这里重新
成立，否则 HTTP 面就成了它们的后门。四条被 tests/unit/test_task_endpoints.py 钉住的
性质，理由写在各自的模型/端点注释里：请求体不接受服务端自有的记账字段、PATCH 改
detail 时清空 detail_origin_seq、after_id 以 int 抵达 store、非法 status 回 4xx。
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.tools.task_store import (
    TaskItem,
    add_task,
    find_focus,
    list_tasks,
    remove_task,
    update_task,
)

router = APIRouter(tags=["events"])

# 五值状态词表。**与 task_store.VALID_STATUSES 单源对齐**由
# tests/unit/test_task_endpoints.py::test_status_vocabulary_is_single_sourced_with_the_store
# 钉住（同 tests/unit/test_task_tools.py 钉工具 schema enum 的手法）。用 Literal 而
# 不是 str + 手写 if：pydantic 直接给出 422 且报出整个合法词表，生成的 TS 类型也是
# 一个五值联合而不是 string——面板的状态下拉于是拿不到非法值。
TaskStatus = Literal["pending", "in_progress", "completed", "skipped", "failed"]


class PermissionResponseData(BaseModel):
    rpc_id: str
    allowed: bool
    session_id: str
    feedback: str = ""
    choice: str = ""


class QuestionResponseData(BaseModel):
    request_id: str
    answer: str
    session_id: str


# 一个 task 的完整对外形状（= TaskItem.to_dict()）。
# `detail_origin_seq` / `started_seq` **出现在响应里但绝不出现在请求模型里**：它们是
# 服务端自有的记账字段，UI 读得到（调试/展示用）却写不了。GET 此前就已经返回它们
# （item.to_dict()），所以这不是新增的暴露面。
class TaskOut(BaseModel):
    id: int
    content: str
    # 刻意是 str 而不是 TaskStatus：这是**读**端点，而 response_model 校验失败会变成
    # 500 并把整个面板打空（Plan 3a 硬化过的那类故障）。store 的 _normalize_status
    # 保证只会是五值之一，但为一个不可能的状态付「面板全灭」的代价不划算。写入侧的
    # 类型安全由 TaskUpdateRequest.status 那个 Literal 提供，前端要联合类型就从那里取。
    status: str
    created_at: str
    updated_at: str
    detail: str
    acceptance: str
    detail_origin_seq: Optional[int] = None
    started_seq: Optional[int] = None
    error: str

    @classmethod
    def from_item(cls, item: TaskItem) -> "TaskOut":
        return cls(**item.to_dict())


class TaskListResponse(BaseModel):
    tasks: list[TaskOut]
    # 焦点条由**后端**算（task_store.find_focus）：焦点规则（in_progress > failed >
    # pending，同档取列表顺序第一个）只能有一处实现，前端自己推导一份就一定会漂移。
    focus_id: Optional[int] = None


# POST 请求体。**刻意只列允许的字段。**
# Plan 3a 的裁定：若写端点接受 detail_origin_seq / started_seq，就必须加一个 _as_seq
# helper 且**排除 bool**（bool 是 int 的子类，True 会变成 1，而 1 是合法 seq）；不接受
# 就根本不需要那个 helper。所以这里不接受——pydantic 默认 extra="ignore"，面板回传一个
# 完整 task 对象时多余键被丢掉，而不是让整个写入失败。
# after_id 声明成 int | None 而不是 str：PATCH/POST 完全绕过工具层，update_task 的自锚
# 守卫（after_id != task_id）是「移到我自己后面」这件事的**唯一**保护；字符串 "3" 会绕过
# !=，然后 _insert_after 找不到 id=="3" 的条目、退回追加末尾——任务被静默甩到列表末尾
# （Plan 1 修过的那个缺陷）。声明成 int 让非数字直接 422、让数字字符串被 pydantic 收窄
# 成 int，两条路都到不了那个缺陷。往下传给 store 时**不再 stringify**。
class TaskCreateRequest(BaseModel):
    content: str
    detail: str = ""
    acceptance: str = ""
    after_id: Optional[int] = None


# PATCH 请求体。字段全是 Optional：给了才改，没给不动。
# 记账字段同上——不接受。status 走 TaskStatus（非法值 422，见端点上方的注释）。
class TaskUpdateRequest(BaseModel):
    content: Optional[str] = None
    detail: Optional[str] = None
    acceptance: Optional[str] = None
    status: Optional[TaskStatus] = None
    error: Optional[str] = None
    after_id: Optional[int] = None


class TaskDeleteResponse(BaseModel):
    success: bool
    message: str = ""


@router.post("/api/events/respond")
async def api_respond_permission(data: PermissionResponseData):
    """响应权限请求。"""
    from agents.session_manager import get_session_manager
    
    sm = get_session_manager()
    agent = sm.get_agent(data.session_id)
    if not agent:
        return {"success": False, "message": "Session not active"}
    
    # Use set_permission_response to work with the permission gate
    agent.set_permission_response(data.rpc_id, data.allowed, data.feedback, data.choice)
    return {"success": True}


@router.post("/api/events/question-respond")
async def api_respond_question(data: QuestionResponseData):
    """响应用户提问。"""
    from agents.session_manager import get_session_manager
    
    sm = get_session_manager()
    agent = sm.get_agent(data.session_id)
    if not agent:
        return {"success": False, "message": "Session not active"}
    
    agent.session.question_responses[data.request_id] = {"answer": data.answer}
    return {"success": True}


@router.get("/api/tasks/{session_id}", response_model=TaskListResponse)
async def api_get_tasks(session_id: str) -> TaskListResponse:
    """获取会话任务清单 + 后端算出的焦点条 id。"""
    items = list_tasks(session_id)
    focus = find_focus(items)
    return TaskListResponse(
        tasks=[TaskOut.from_item(item) for item in items],
        focus_id=focus.id if focus is not None else None,
    )


# POST/PATCH/DELETE 的说明刻意放在装饰器**外面**：FastAPI 会把函数 docstring 整个
# 塞进 openapi 的 description，于是它会被复制进生成物 frontend/src/api/schema.d.ts
# （每个端点几十行 JSDoc）。裁定属于源码，不属于契约产物；本文件其余端点的 docstring
# 也都是一行。
#
# POST：新建一条任务。
#   **不传 current_seq**，于是 detail_origin_seq 保持 None → 这条 detail 会在它成为
#   焦点条时被披露给模型。这与 plan 物化路径是同一个不变量：UI 建的条目模型从没见过，
#   「模型自己写过所以不必重注入」那条豁免对它不成立。HTTP 层也绝不能自己编一个 seq
#   ——那会与正在运行的 Agent 的内存 `_log` 争抢 seq 空间。
#   content 必填且非空白，与工具层 add 分支同一条（空条目会在常驻摘要 S 里渲染成一行
#   空任务）。
@router.post("/api/tasks/{session_id}", response_model=TaskOut)
async def api_create_task(session_id: str, data: TaskCreateRequest) -> TaskOut:
    """新建一条任务。"""
    content = data.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content is required")
    item = add_task(
        session_id,
        content,
        detail=data.detail,
        acceptance=data.acceptance,
        after_id=data.after_id,
    )
    return TaskOut.from_item(item)


# PATCH：改一条任务，返回改完的样子。
#   **detail 变更 → detail_origin_seq 清空**，这是「UI 编辑回灌模型」的全部机制：
#   update_task 在 detail is not None 时把 detail_origin_seq 重指向 current_seq，而
#   这里**不传** current_seq（HTTP 层没有、也不该编一个事件 seq），于是它落成 None →
#   下一次模型调用 needs_disclosure 为真 → 新 detail 被重新注入。改
#   content/acceptance/status/error/顺序都**不需要**任何回灌动作：S 每请求从 store
#   现读，自动反映。
#   刻意**不**写 memory_injection 事件（spec §十一 原本设想的那条路）：那会与正在运行
#   的 Agent 的内存 `_log` 争抢 seq，是本架构里真实存在的危险，而收益为零。
#   status 非法 → 422（TaskStatus 这个 Literal 给的），而不是「200 且什么都没改」：
#   update_task 对非法 status 是静默忽略的，端点照抄那份静默就复现了工具层当初专门加
#   `Error: invalid status` 要防的那一类缺陷。
#   未知 task_id → 404。成功响应体是一个 task，没有 success 信封可放失败，所以这条走
#   HTTPException（与 sessions.py 的 "Session not found" 同一约定），而不是本文件那两
#   个权限端点的 200 + {"success": false} 形状。
#   content 给了但 strip 后为空 → 422，与工具层 update 分支同一条（HTTP 面绕过工具层，
#   那边界校验必须在这里重新成立）。`null`/缺席 = 「不改这个字段」，照旧放行。
#   这条**不改 openapi.json**：字段类型仍是 Optional[str]，而 POST 那条同形状的 422
#   本来也没有在 `responses=` 里声明过（见 :170-175）。
@router.patch("/api/tasks/{session_id}/{task_id}", response_model=TaskOut)
async def api_update_task(
    session_id: str, task_id: int, data: TaskUpdateRequest
) -> TaskOut:
    """改一条任务，返回改完的样子。"""
    if data.content is not None and not data.content.strip():
        raise HTTPException(status_code=422, detail="content cannot be blank")
    item = update_task(
        session_id,
        task_id,
        status=data.status,
        content=data.content,
        detail=data.detail,
        acceptance=data.acceptance,
        error=data.error,
        after_id=data.after_id,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskOut.from_item(item)


# DELETE：删一条任务。
#   未知 id 沿用**本文件既有端点**的失败形状（200 + success=false + message，见
#   api_respond_permission）：DELETE 的响应体本来就是 success 信封，失败放进同一个
#   信封即可，不必为它引入第二种错误通道。
@router.delete(
    "/api/tasks/{session_id}/{task_id}", response_model=TaskDeleteResponse
)
async def api_delete_task(
    session_id: str, task_id: int
) -> TaskDeleteResponse:
    """删一条任务。"""
    if not remove_task(session_id, task_id):
        return TaskDeleteResponse(success=False, message="Task not found")
    return TaskDeleteResponse(success=True)
