"""task_list 的三个写端点 + GET 的 focus_id（Plan 3b Task B1）。

端点是 task_list 的**第二个写入方**（第一个是模型经工具层）。它绕过工具层，所以
工具层那些边界校验（status 词表、after_id 的 id 类型、content 非空）必须在这里
重新成立，否则 HTTP 面就成了那些缺陷的后门。四条被钉住的性质：

1. 请求体 schema 不接受 `detail_origin_seq` / `started_seq`——服务端自有记账字段。
   Plan 3a 的裁定：接受它们就必须加 `_as_seq`（排除 bool，因为 True 会变成 1 而
   1 是合法 seq）；不接受就根本不需要那个 helper。
2. PATCH 改 `detail` 时清空 `detail_origin_seq`——这是「UI 编辑回灌模型」的**全部**
   机制（下一次请求 needs_disclosure 为真 → 重新注入）。改别的字段不清。
3. `after_id` 以 int 抵达 store——字符串会绕过 `update_task` 的自锚守卫
   （`after_id != task_id`），把任务静默甩到列表末尾。
4. 非法 status 回 4xx 而不是「200 且什么都没改」——`update_task` 对非法 status
   是静默忽略的，端点照抄那份静默就复现了工具层当初专门加 `Error:` 要防的缺陷。
5. 四个端点都在**会话的**工作区里读写（W1）：store 是工作区作用域的
   （`get_tasks_dir` = `get_workspace()/.mycode/todos`），而 HTTP 请求不在任何会话的
   `workspace_scope` 里，所以端点必须自己包一层 `resolve_session_workspace`。
   会话 cwd ≠ 服务器 project_root 时，不包的后果是 GET 恒空、写端点 200 写进幽灵
   文件。钉住它的三条测试在文末，它们**刻意让读与写处在两个不同的工作区**——
   本文件其余测试共用的 `ws` fixture 把两者放在同一处，于是这个 bug 在那里
   结构上不可能出现。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from agents.core.workspace import reset_workspace, set_workspace, workspace_scope
from agents.tools.task_store import (
    VALID_STATUSES,
    add_task,
    find_focus,
    list_tasks,
    mark_detail_disclosed,
    needs_disclosure,
    update_task,
)

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

SID = "s-endpoints"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """工作区隔离。

    两条都设：TestClient 经 anyio portal 在**另一个线程**里跑端点，contextvar 不
    一定跨线程可见，而 get_workspace() 的兜底是 os.getcwd()。两者都指向 tmp_path
    时，测试体与端点看到的 store 一定是同一个，无论 portal 有没有传播 context。

    W1 之后端点自己包 `workspace_scope(resolve_session_workspace(sid))`，于是它读的
    是**会话**的工作区（无活 agent 且无 projcache 时兜底 `Path.cwd()`，即这里的
    chdir）；测试体的直接 store 调用读的仍是 ContextVar。两者在这里**恒等**——这正是
    本 fixture 结构上抓不到 W1 的原因（读写永远不可能不一致），所以下面另有一组
    `split_ws` 测试把两者拆开。
    """
    monkeypatch.chdir(tmp_path)
    token = set_workspace(tmp_path)
    yield tmp_path
    reset_workspace(token)


@pytest.fixture
def api(ws):
    from main import app

    yield TestClient(app)


def _get(api) -> dict:
    r = api.get(f"/api/tasks/{SID}")
    assert r.status_code == 200, r.text
    return r.json()


def _ids(api) -> list[int]:
    return [t["id"] for t in _get(api)["tasks"]]


def _post(api, **body):
    return api.post(f"/api/tasks/{SID}", json=body)


def _patch(api, task_id, **body):
    return api.patch(f"/api/tasks/{SID}/{task_id}", json=body)


def _delete(api, task_id):
    return api.delete(f"/api/tasks/{SID}/{task_id}")


def _mark_disclosed(item, seq: int = 42) -> None:
    """记账「这条的 detail 已经披露过」。

    第 4 个参数必须是**当时真正注入的那段文本**（I-1：store 里的 detail 与它不一致
    就不盖章），所以直接从条目上取，而不是重打一遍字面量——字面量写错会让这些测试
    静默变成「什么都没盖章」，于是它们钉住的 detail_origin_seq 全都不动，看起来还是绿的。
    """
    mark_detail_disclosed(SID, item.id, seq, item.detail)


# ─────────────────────────── GET：focus_id ───────────────────────────


def test_get_on_empty_store_returns_no_focus(ws, api):
    body = _get(api)
    assert body["tasks"] == []
    assert body["focus_id"] is None


def test_get_focus_id_matches_find_focus(ws, api):
    """焦点规则只有后端一处实现（task_store.find_focus），前端不自己推导。

    断言的是与 find_focus 的**一致性**，而不是重抄一遍规则：抄一份的话两边漂移
    正是这条测试要防的事。
    """
    a = add_task(SID, "A")
    b = add_task(SID, "B")
    update_task(SID, b.id, status="failed", error="boom")
    assert _get(api)["focus_id"] == find_focus(list_tasks(SID)).id == b.id

    update_task(SID, a.id, status="in_progress")
    # in_progress 压过 failed
    assert _get(api)["focus_id"] == find_focus(list_tasks(SID)).id == a.id

    update_task(SID, a.id, status="completed")
    update_task(SID, b.id, status="skipped")
    # 没有 pending/in_progress/failed 了 → 无焦点
    assert _get(api)["focus_id"] is None
    assert find_focus(list_tasks(SID)) is None


def test_get_focus_id_prefers_first_in_list_order_within_tier(ws, api):
    a = add_task(SID, "A")
    b = add_task(SID, "B")
    add_task(SID, "C")
    update_task(SID, a.id, status="completed")
    # 同档（pending）里取列表顺序第一个：B，而不是 C，也不是已完成的 A
    assert _get(api)["focus_id"] == find_focus(list_tasks(SID)).id == b.id


def test_get_still_returns_the_full_task_dicts(ws, api):
    """GET 的既有形状不得因加 focus_id 而缩水（面板要读 detail/acceptance/error）。"""
    add_task(SID, "A", detail="方案A", acceptance="pytest -k a")
    task = _get(api)["tasks"][0]
    for key in ("id", "content", "status", "created_at", "updated_at",
                "detail", "acceptance", "detail_origin_seq", "started_seq", "error"):
        assert key in task
    assert task["detail"] == "方案A"
    assert task["acceptance"] == "pytest -k a"


# ─────────────────────────── POST ───────────────────────────


def test_post_creates_task_and_it_shows_up_in_get(ws, api):
    r = _post(api, content="新任务", detail="方案", acceptance="pytest")
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["content"] == "新任务"
    assert created["detail"] == "方案"
    assert created["acceptance"] == "pytest"
    assert created["status"] == "pending"
    assert isinstance(created["id"], int)

    assert _ids(api) == [created["id"]]
    assert [t.content for t in list_tasks(SID)] == ["新任务"]


def test_post_leaves_detail_origin_seq_none_so_the_model_gets_it_injected(ws, api):
    """UI 建的条目模型从没见过 → detail_origin_seq 必须是 None。

    与物化路径同一个不变量：`add_task` 不传 current_seq。若端点擅自编一个 seq
    （或把请求体里的值写进去），needs_disclosure 就会认为「模型自己写过」，这段
    detail 永远不会被披露。
    """
    created = _post(api, content="新任务", detail="方案").json()
    assert created["detail_origin_seq"] is None
    assert created["started_seq"] is None
    assert list_tasks(SID)[0].detail_origin_seq is None


def test_post_with_after_id_inserts_in_place(ws, api):
    a = add_task(SID, "A")
    b = add_task(SID, "B")
    r = _post(api, content="插在 A 后", after_id=a.id)
    assert r.status_code == 200, r.text
    assert _ids(api) == [a.id, r.json()["id"], b.id]


def test_post_rejects_bookkeeping_fields_in_body(ws, api):
    """硬要求 1：detail_origin_seq / started_seq 是服务端自有的，请求体给不了。

    被忽略（pydantic 默认 extra=ignore）而不是 422：面板回传一个完整 task 对象是
    合理用法，多余键不该让整个写入失败——但绝不允许生效。
    """
    add_task(SID, "A")
    created = _post(
        api, content="新任务", detail="方案",
        detail_origin_seq=7, started_seq=9,
    ).json()
    assert created["detail_origin_seq"] is None
    assert created["started_seq"] is None


def test_post_rejects_blank_content(ws, api):
    """与工具层同一条：`add` 的 content 必填。空条目在 S 里渲染成一行空任务。"""
    r = _post(api, content="   ")
    assert r.status_code == 422, r.text
    assert list_tasks(SID) == []


def test_post_requires_content_key(ws, api):
    r = api.post(f"/api/tasks/{SID}", json={"detail": "只有方案"})
    assert r.status_code == 422


# ─────────────────────────── PATCH ───────────────────────────


def test_patch_updates_fields_and_returns_the_task(ws, api):
    a = add_task(SID, "A")
    r = _patch(api, a.id, content="改过的 A", acceptance="pytest -k b")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == a.id
    assert body["content"] == "改过的 A"
    assert body["acceptance"] == "pytest -k b"
    stored = list_tasks(SID)[0]
    assert stored.content == "改过的 A"
    assert stored.acceptance == "pytest -k b"


def test_patch_rejects_blank_content(ws, api):
    """与工具层 update 分支同一条（F1）：HTTP 面绕过工具层，那道校验必须在这里重新成立。

    未修时 UI 的「全选内容 → 失焦」→ `PATCH {content:""}` → 200，于是常驻摘要 S
    渲染出一行空任务、披露块标题变成 `## 当前任务的执行方案（#5 ）`——UI 造出了
    POST 那条 422 专门防的状态，且全程零报错。
    """
    a = add_task(SID, "A", detail="方案A")
    _mark_disclosed(a)
    for blank in ("", "   "):
        r = _patch(api, a.id, content=blank)
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == "content cannot be blank"
    stored = list_tasks(SID)[0]
    assert stored.content == "A"
    assert stored.detail_origin_seq == 42      # 被拒的请求不得有任何副作用


def test_patch_without_content_key_updates_the_other_fields(ws, api):
    """「没给 content」≠「给了空的」：缺席与 null 都是「不改这个字段」，必须放行。

    守卫若写成 `not (data.content or "").strip()`，这条会红——那种写法把最常见的
    「只改 detail」「只改 status」也一起拒了。
    """
    a = add_task(SID, "A", detail="旧方案")
    r = _patch(api, a.id, detail="新方案")
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "A"          # 没给 content → 原值不动
    assert r.json()["detail"] == "新方案"
    assert r.json()["detail_origin_seq"] is None   # detail 改了 → 下一轮重新披露

    r = _patch(api, a.id, content=None, acceptance="pytest -k a")
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "A"
    assert r.json()["acceptance"] == "pytest -k a"


def test_patch_detail_clears_detail_origin_seq(ws, api):
    """硬要求 2：这是「UI 改 detail → 模型下一轮看得到」的全部机制。"""
    a = add_task(SID, "A", detail="旧方案")
    _mark_disclosed(a)
    assert list_tasks(SID)[0].detail_origin_seq == 42

    r = _patch(api, a.id, detail="人改过的新方案")
    assert r.status_code == 200, r.text
    assert r.json()["detail_origin_seq"] is None
    assert list_tasks(SID)[0].detail_origin_seq is None
    assert list_tasks(SID)[0].detail == "人改过的新方案"


@pytest.mark.parametrize("field,value", [
    ("content", "只是改标题"),
    ("acceptance", "pytest -k z"),
    ("error", "AssertionError"),
    ("status", "in_progress"),
])
def test_patch_other_fields_keep_detail_origin_seq(ws, api, field, value):
    """只有 detail 变更才清空。

    清空是「重新披露一次」的开关，对 content/acceptance/status/error 毫无意义
    ——那些字段经 S 每请求现读，自动反映。多清一次就是多注入一次最多 6000 字符。
    """
    a = add_task(SID, "A", detail="方案")
    _mark_disclosed(a)

    r = _patch(api, a.id, **{field: value})
    assert r.status_code == 200, r.text
    assert r.json()["detail_origin_seq"] == 42
    assert list_tasks(SID)[0].detail_origin_seq == 42


def test_patch_after_id_alone_keeps_detail_origin_seq(ws, api):
    a = add_task(SID, "A", detail="方案A")
    b = add_task(SID, "B", detail="方案B")
    _mark_disclosed(b)

    r = _patch(api, b.id, after_id=0)     # 移到最前
    assert r.status_code == 200, r.text
    assert _ids(api) == [b.id, a.id]
    assert list_tasks(SID)[0].detail_origin_seq == 42


def test_patch_rejects_bookkeeping_fields_in_body(ws, api):
    a = add_task(SID, "A", detail="方案")
    _mark_disclosed(a)

    # 不带 detail：两个记账字段都不得被请求体改动
    body = _patch(api, a.id, content="改标题",
                  detail_origin_seq=99, started_seq=98).json()
    assert body["detail_origin_seq"] == 42
    assert body["started_seq"] is None

    # 带 detail：detail_origin_seq 被清空（而不是变成请求体给的 99）
    body = _patch(api, a.id, detail="新方案", detail_origin_seq=99, started_seq=98).json()
    assert body["detail_origin_seq"] is None
    assert body["started_seq"] is None


def test_patch_invalid_status_is_4xx_and_changes_nothing(ws, api):
    """硬要求 4：update_task 对非法 status 静默忽略；端点不得照抄那份静默。"""
    a = add_task(SID, "A")
    _mark_disclosed(a)

    r = _patch(api, a.id, status="bogus")
    assert 400 <= r.status_code < 500, r.text
    # 「清晰的消息」= 报出合法词表。只断言 4xx 的话，路由压根不存在（405）也能过。
    assert "pending" in r.text and "in_progress" in r.text, r.text
    stored = list_tasks(SID)[0]
    assert stored.status == "pending"
    assert stored.detail_origin_seq == 42      # 顺带：非法请求不得有任何副作用


def test_patch_valid_statuses_are_all_accepted(ws, api):
    for status in sorted(VALID_STATUSES):
        a = add_task(SID, f"t-{status}")
        r = _patch(api, a.id, status=status)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == status
        assert list_tasks(SID)[-1].status == status


def test_patch_moves_task_after_given_id(ws, api):
    a = add_task(SID, "A")
    b = add_task(SID, "B")
    c = add_task(SID, "C")
    r = _patch(api, a.id, after_id=c.id)
    assert r.status_code == 200, r.text
    assert _ids(api) == [b.id, c.id, a.id]


def test_patch_self_anchor_is_a_positional_noop(ws, api):
    """硬要求 3 要防的正是这一条被字符串绕过：after_id == 自己的 id 不得移动。

    `update_task` 的自锚守卫用 `after_id != task_id` 比较。字符串 "1" 会绕过它，
    然后 `_insert_after` 找不到 id=="1" 的条目 → 退回追加末尾 → 任务被静默甩到
    列表末尾。这是 Plan 1 修过的缺陷，HTTP 面绕过工具层，守卫是唯一的保护。
    """
    a = add_task(SID, "A")
    b = add_task(SID, "B")
    c = add_task(SID, "C")

    r = _patch(api, b.id, after_id=b.id)
    assert r.status_code == 200, r.text
    assert _ids(api) == [a.id, b.id, c.id]


def test_patch_numeric_string_after_id_arrives_as_int(ws, api):
    """`"2"` 不得静默甩到末尾：pydantic 收窄成 int，守卫与查找都按 int 生效。"""
    a = add_task(SID, "A")
    b = add_task(SID, "B")
    c = add_task(SID, "C")

    r = _patch(api, a.id, after_id=str(c.id))
    assert r.status_code == 200, r.text
    assert _ids(api) == [b.id, c.id, a.id]

    # 自锚守卫同样要认字符串形式的自己的 id
    r = _patch(api, b.id, after_id=str(b.id))
    assert r.status_code == 200, r.text
    assert _ids(api) == [b.id, c.id, a.id]


def test_patch_non_numeric_after_id_is_rejected(ws, api):
    a = add_task(SID, "A")
    b = add_task(SID, "B")
    r = _patch(api, a.id, after_id="abc")
    assert r.status_code == 422, r.text
    assert _ids(api) == [a.id, b.id]


def test_post_non_numeric_after_id_is_rejected(ws, api):
    add_task(SID, "A")
    r = _post(api, content="新任务", after_id="abc")
    assert r.status_code == 422, r.text
    assert len(list_tasks(SID)) == 1


def test_patch_unknown_task_is_404(ws, api):
    add_task(SID, "A")
    r = _patch(api, 999, content="x")
    assert r.status_code == 404, r.text


def test_patch_non_int_task_id_is_422(ws, api):
    r = api.patch(f"/api/tasks/{SID}/abc", json={"content": "x"})
    assert r.status_code == 422


# ─────────────────────────── DELETE ───────────────────────────


def test_delete_removes_the_task(ws, api):
    a = add_task(SID, "A")
    b = add_task(SID, "B")
    r = _delete(api, a.id)
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert _ids(api) == [b.id]
    assert [t.id for t in list_tasks(SID)] == [b.id]


def test_delete_unknown_id_reports_failure_clearly(ws, api):
    """沿用本文件既有端点的失败形状（events.py：200 + success=false + message）。

    DELETE 的响应体本来就是一个 success 信封，失败放进同一个信封即可；PATCH/POST
    的成功响应体是 task 本身，没有信封可放，所以那两条走 HTTPException 404。
    """
    add_task(SID, "A")
    r = _delete(api, 999)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is False
    assert body["message"]
    assert _ids(api) == [1]


def test_delete_shifts_focus(ws, api):
    a = add_task(SID, "A")
    b = add_task(SID, "B")
    update_task(SID, a.id, status="in_progress")
    assert _get(api)["focus_id"] == a.id
    _delete(api, a.id)
    assert _get(api)["focus_id"] == b.id


# ─────────────────────────── 契约级钉子 ───────────────────────────


def test_request_schemas_do_not_expose_bookkeeping_fields():
    """schema 级：字段压根不在模型里，于是生成的 TS 类型也不会提供它们。

    上面两条运行时测试证明「给了也不生效」；这一条证明「契约里就没有」——后者才是
    前端不会被诱导去写这两个字段的原因。
    """
    from routers.events import TaskCreateRequest, TaskUpdateRequest

    for model in (TaskCreateRequest, TaskUpdateRequest):
        fields = set(model.model_fields)
        assert "detail_origin_seq" not in fields
        assert "started_seq" not in fields
        assert "id" not in fields          # id 只走路径参数，不走请求体


def test_status_vocabulary_is_single_sourced_with_the_store():
    """路由的 status 词表与 task_store.VALID_STATUSES 不得漂移。

    与 tests/unit/test_task_tools.py:237 同一条手法（那里钉工具 schema 的 enum）。
    """
    from routers.events import TaskStatus

    assert set(get_args(TaskStatus)) == VALID_STATUSES


def test_after_id_is_declared_as_int_not_str():
    """硬要求 3 的 schema 侧：声明成 str 的话守卫就形同虚设。"""
    from routers.events import TaskCreateRequest, TaskUpdateRequest

    for model in (TaskCreateRequest, TaskUpdateRequest):
        annotation = model.model_fields["after_id"].annotation
        assert int in getattr(annotation, "__args__", (annotation,))
        assert str not in getattr(annotation, "__args__", ())


# ─────────────────── I-2：detail 没变时 HTTP 面**不**清空标记 ───────────────────
#
# 判别式是 `current_seq` 有没有：工具层传（模型自己刚写的 detail 正躺在本次 tool_calls
# 的参数里）→ 无条件重指向；HTTP 面刻意不传（模型从没见过这段文本）→ 只在文本真的变了
# 才清空标记。两个面的正确行为**相反**，(a) 那半边在
# tests/unit/test_task_tools.py::test_update_same_detail_with_seq_repoints_origin_seq。


def test_patch_same_detail_without_seq_keeps_origin_seq(ws, api):
    """(b)：HTTP 传**相同**的 detail（无 current_seq）→ 标记一动不动。

    无条件重指向在这里就是白浪费：文本没变，清空标记只会让模型下一轮重新注入一份与
    上次逐字相同的块（最多 6000 字符）。评审建议的一行改法在 HTTP 面是对的，但它推到
    工具层就把 (a) 改坏了——所以 store 里按 `current_seq is not None or changed` 分流。
    """
    a = add_task(SID, "A", detail="方案")
    _mark_disclosed(a, 42)
    assert list_tasks(SID)[0].detail_origin_seq == 42

    r = _patch(api, a.id, detail="方案")
    assert r.status_code == 200, r.text
    assert r.json()["detail_origin_seq"] == 42
    assert list_tasks(SID)[0].detail_origin_seq == 42
    # 仍然「已披露」：承载事件 42 可见时不再注入
    assert needs_disclosure(list_tasks(SID)[0], [42]) is False


def test_patch_different_detail_without_seq_clears_origin_seq(ws, api):
    """(c)：HTTP 传**不同**的 detail → 标记变 None，新文本下一轮被披露。"""
    a = add_task(SID, "A", detail="旧方案")
    _mark_disclosed(a, 42)

    r = _patch(api, a.id, detail="新方案")
    assert r.status_code == 200, r.text
    assert r.json()["detail_origin_seq"] is None
    stored = list_tasks(SID)[0]
    assert stored.detail == "新方案"
    assert stored.detail_origin_seq is None
    # 清空的意义就在这一行：即使承载事件 42 仍然可见，新 detail 也要重新注入
    assert needs_disclosure(stored, [42]) is True


# ─────────────────── W1：端点必须跟着**会话**的工作区 ───────────────────
#
# A = 会话 cwd（Agent 在 workspace_scope(agent.workspace) 里写任务的地方），
# B = 前端服务器进程的工作区（main.py:50-52 在启动时把 ContextVar 设成 project_root，
# 此后 HTTP 请求里再没按会话设过）。store 是工作区作用域的，所以端点必须自己包一层
# `workspace_scope(resolve_session_workspace(sid))`；不包就是去 B 底下找 A 的文件。
#
# 这三条**刻意让 A ≠ B**：本文件其余测试共用的 `ws` fixture 把读写放在同一个工作区，
# 于是这个 bug 在那里结构上不可能出现——这正是它逃过 996 个测试的原因。

SID_SPLIT = "s-split-ws"
SID_SPLIT_2 = "s-split-ws-2"


def _write_projcache(sessions: Path, sid: str, cwd: Path) -> None:
    """照 sessions.py 读的形状写一份 projcache（会话 cwd 的落盘来源）。"""
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{sid}.projcache.json").write_text(
        json.dumps({"rows": {"cwd": {"val": str(cwd)}}}), encoding="utf-8"
    )


@pytest.fixture
def split_ws(api, tmp_path, monkeypatch):
    """会话 cwd（A）与服务器工作区（B）**不一致**的环境。

    刻意先请求 `api`：它依赖的 `ws` fixture 会 chdir + set_workspace 到 tmp_path，
    顺序反了的话这里对 B 的设置会被它冲掉。
    """
    a = tmp_path / "session-cwd"
    b = tmp_path / "server-project-root"
    a.mkdir()
    b.mkdir()
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(sessions))
    for sid in (SID_SPLIT, SID_SPLIT_2):
        _write_projcache(sessions, sid, a)          # 会话的 cwd 落在 A

    import routers.sessions as sessions_router

    # 「没有活 agent」是走 projcache 这条路的前提，直接钉住而不是依赖 session_manager
    # 恰好没有这个会话。
    monkeypatch.setattr(sessions_router, "_get_live_agent", lambda _sid: None)
    # 服务器侧：进程 CWD 与 ContextVar 都在 B（照 main.py 启动时的形状）。
    # 未修的端点于是只能在 B 底下找 store。
    monkeypatch.chdir(b)
    token = set_workspace(b)
    try:
        yield SimpleNamespace(session_ws=a, server_ws=b)
    finally:
        reset_workspace(token)


def _tasks_in(ws_path: Path, sid: str):
    """在指定工作区里直接读 store（绕开端点，确认落盘的那一份）。"""
    with workspace_scope(ws_path):
        return list_tasks(sid)


def test_get_and_patch_follow_the_session_workspace_not_the_server_one(split_ws, api):
    """W1 靶心：读到的与改到的都必须是**会话**工作区里的那份 store。

    未修时：GET 在 B/.mycode/todos/<sid>.json 找一个不存在的文件 → 200 + 空清单
    （面板永久空白、控制台零输出）；PATCH 要么 404（B 里没有这条），要么 200 写进
    B 下的一个幽灵文件，Agent 永远看不到——「用户以为改了计划、其实什么都没发生、
    还没有任何报错」。
    """
    with workspace_scope(split_ws.session_ws):
        a = add_task(SID_SPLIT, "会话里的任务", detail="方案A")

    # 读：GET 看得到 A 里的清单，而不是 B 的空清单
    r = api.get(f"/api/tasks/{SID_SPLIT}")
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()["tasks"]] == [a.id]
    assert r.json()["tasks"][0]["detail"] == "方案A"

    # 写：PATCH 改的也是 A 里的那份。直接在 A 的路径上读文件确认，不只信响应体。
    r = api.patch(f"/api/tasks/{SID_SPLIT}/{a.id}", json={"detail": "用户在 UI 上改的方案"})
    assert r.status_code == 200, r.text
    assert r.json()["detail"] == "用户在 UI 上改的方案"
    assert [t.detail for t in _tasks_in(split_ws.session_ws, SID_SPLIT)] == ["用户在 UI 上改的方案"]
    assert (split_ws.session_ws / ".mycode" / "todos" / f"{SID_SPLIT}.json").exists()
    # 幽灵文件：B 底下从头到尾不该出现这个会话的 store
    assert not (split_ws.server_ws / ".mycode" / "todos" / f"{SID_SPLIT}.json").exists()


def test_post_and_delete_also_follow_the_session_workspace(split_ws, api):
    """三个写端点走的是同一条路，所以同一条裁定对它们都要成立。"""
    r = api.post(f"/api/tasks/{SID_SPLIT_2}", json={"content": "UI 建的任务"})
    assert r.status_code == 200, r.text
    created_id = r.json()["id"]
    assert [t.content for t in _tasks_in(split_ws.session_ws, SID_SPLIT_2)] == ["UI 建的任务"]
    assert not (split_ws.server_ws / ".mycode" / "todos" / f"{SID_SPLIT_2}.json").exists()

    r = api.delete(f"/api/tasks/{SID_SPLIT_2}/{created_id}")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert _tasks_in(split_ws.session_ws, SID_SPLIT_2) == []


def test_without_projcache_or_live_agent_endpoints_fall_back_to_cwd(api, tmp_path, monkeypatch):
    """反向对照：没有 projcache 且没有活 agent → 回退 `Path.cwd()`，端点照常工作。

    证明 W1 的修法没有把兜底路径改坏（CLI/单进程场景，以及会话还没落过 projcache 的
    那一刻）。这里的 sid 刻意**没有** projcache 文件。
    """
    sid = "s-no-projcache"
    c = tmp_path / "cli-cwd"
    c.mkdir()
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions-without-this-sid"))
    import routers.sessions as sessions_router

    monkeypatch.setattr(sessions_router, "_get_live_agent", lambda _sid: None)
    monkeypatch.chdir(c)

    with workspace_scope(c):
        a = add_task(sid, "CLI 场景的任务", detail="方案C")

    r = api.get(f"/api/tasks/{sid}")
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()["tasks"]] == [a.id]

    r = api.patch(f"/api/tasks/{sid}/{a.id}", json={"status": "completed"})
    assert r.status_code == 200, r.text
    with workspace_scope(c):
        assert list_tasks(sid)[0].status == "completed"
