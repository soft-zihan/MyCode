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
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from agents.core.workspace import reset_workspace, set_workspace
from agents.tools.task_store import (
    VALID_STATUSES,
    add_task,
    find_focus,
    list_tasks,
    mark_detail_disclosed,
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


def test_patch_detail_clears_detail_origin_seq(ws, api):
    """硬要求 2：这是「UI 改 detail → 模型下一轮看得到」的全部机制。"""
    a = add_task(SID, "A", detail="旧方案")
    mark_detail_disclosed(SID, a.id, 42)
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
    mark_detail_disclosed(SID, a.id, 42)

    r = _patch(api, a.id, **{field: value})
    assert r.status_code == 200, r.text
    assert r.json()["detail_origin_seq"] == 42
    assert list_tasks(SID)[0].detail_origin_seq == 42


def test_patch_after_id_alone_keeps_detail_origin_seq(ws, api):
    a = add_task(SID, "A", detail="方案A")
    b = add_task(SID, "B", detail="方案B")
    mark_detail_disclosed(SID, b.id, 42)

    r = _patch(api, b.id, after_id=0)     # 移到最前
    assert r.status_code == 200, r.text
    assert _ids(api) == [b.id, a.id]
    assert list_tasks(SID)[0].detail_origin_seq == 42


def test_patch_rejects_bookkeeping_fields_in_body(ws, api):
    a = add_task(SID, "A", detail="方案")
    mark_detail_disclosed(SID, a.id, 42)

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
    mark_detail_disclosed(SID, a.id, 42)

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
