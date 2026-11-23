"""准入与状态推进。k8s 与 git 全部 monkeypatch，不打真集群。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

from frontend.server.cloud import reconciler as rc
from frontend.server.cloud import task_store as ts


def test_default_broadcast_hits_the_live_subscriber_registry(monkeypatch):
    """routers.websocket 与 frontend.server.routers.websocket 是两个模块对象，

    各有各的 _subscribers。挑错名字，事件就广播进一个永远空着的列表——
    表面上一切正常，前端一条也收不到。
    """
    import routers.websocket as live
    seen = []
    monkeypatch.setattr(live, "broadcast_event",
                        lambda ev, target_session_id=None: seen.append((ev, target_session_id)))
    rc._default_broadcast({"type": "x"}, "s1")
    assert seen == [({"type": "x"}, "s1")]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    monkeypatch.setenv("MYCODE_SANDBOX_IMAGE", "img:tag")
    yield


def _stub(monkeypatch, phase="Pending", land=None):
    calls = {"create": 0, "land": 0, "delete": 0}
    monkeypatch.setattr(rc.sandbox, "create_job", lambda m: calls.__setitem__("create", calls["create"] + 1) or {"metadata": {"name": m["metadata"]["name"]}})
    monkeypatch.setattr(rc.sandbox, "get_job", lambda n: {"status": {"active": 1}} if phase == "Running" else ({"status": {"succeeded": 1}} if phase == "Succeeded" else {"status": {}}))
    monkeypatch.setattr(rc.sandbox, "delete_job", lambda n: calls.__setitem__("delete", calls["delete"] + 1) or True)
    monkeypatch.setattr(rc, "import_workspace", lambda **kw: type("W", (), {"directory": "/tmp/w", "name": kw.get("name", "w")})())
    monkeypatch.setattr(rc, "_land", lambda task: calls.__setitem__("land", calls["land"] + 1) or {"branch": "b", "pr_url": "u", "mergeable": True, "no_changes": False})
    return calls


def test_admission_respects_concurrency(monkeypatch):
    calls = _stub(monkeypatch)
    a = ts.create_task("https://github.com/o/r", "a")
    b = ts.create_task("https://github.com/o/r", "b")
    r = rc.CloudReconciler(max_concurrency=1)
    r.poll_once()
    assert calls["create"] == 1
    assert ts.load_task(a.id).status != "queued"
    assert ts.load_task(b.id).status == "queued"


def test_succeeded_job_triggers_landing(monkeypatch):
    calls = _stub(monkeypatch, phase="Succeeded")
    t = ts.create_task("https://github.com/o/r", "a")
    r = rc.CloudReconciler(max_concurrency=1)
    r.poll_once()          # queued → provisioning → running
    r.poll_once()          # Job Succeeded → landing → succeeded
    assert calls["land"] == 1
    assert ts.load_task(t.id).status == "succeeded"


def test_deadline_marks_timeout_but_still_lands(monkeypatch):
    calls = _stub(monkeypatch)
    monkeypatch.setattr(rc.sandbox, "get_job", lambda n: {"status": {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}})
    t = ts.create_task("https://github.com/o/r", "a")
    r = rc.CloudReconciler(max_concurrency=1)
    r.poll_once(); r.poll_once()
    got = ts.load_task(t.id)
    assert got.status == "timeout"
    assert calls["land"] == 1        # 有价值的半成品不该丢


def test_missing_job_marks_failed(monkeypatch):
    _stub(monkeypatch)
    monkeypatch.setattr(rc.sandbox, "get_job", lambda n: None)
    t = ts.create_task("https://github.com/o/r", "a")
    r = rc.CloudReconciler(max_concurrency=1)
    r.poll_once(); r.poll_once()
    got = ts.load_task(t.id)
    assert got.status == "failed" and "外部删除" in got.error


def test_clone_failure_marks_failed_without_job(monkeypatch):
    calls = _stub(monkeypatch)
    def boom(**kw):
        raise rc.WorkspaceImportError("克隆失败：仓库不存在")
    monkeypatch.setattr(rc, "import_workspace", boom)
    t = ts.create_task("https://github.com/o/r", "a")
    rc.CloudReconciler(max_concurrency=1).poll_once()
    got = ts.load_task(t.id)
    assert got.status == "failed" and calls["create"] == 0


def test_cancel_deletes_job_and_waits_for_gone(monkeypatch):
    calls = _stub(monkeypatch)
    t = ts.create_task("https://github.com/o/r", "a")
    r = rc.CloudReconciler(max_concurrency=1)
    r.poll_once()
    ts.update_task(t.id, "cancelled")
    monkeypatch.setattr(rc.sandbox, "get_job", lambda n: None)
    r.poll_once()
    assert calls["delete"] >= 1
    assert ts.load_task(t.id).status == "cancelled"


def test_cancelled_task_stops_retrying_delete_once_job_gone(monkeypatch):
    """Job 确认消失后清掉 job_name，否则每 2s 都对集群发一次 DELETE。"""
    calls = _stub(monkeypatch)
    t = ts.create_task("https://github.com/o/r", "a")
    r = rc.CloudReconciler(max_concurrency=1)
    r.poll_once()
    ts.update_task(t.id, "cancelled")
    monkeypatch.setattr(rc.sandbox, "get_job", lambda n: None)
    r.poll_once()
    assert ts.load_task(t.id).job_name == ""
    before = calls["delete"]
    r.poll_once()
    assert calls["delete"] == before


def test_orphaned_landing_releases_the_slot(monkeypatch):
    """落地中途服务重启：不装作能续跑，但必须放掉并发位，否则唯一位子被僵尸占死。"""
    calls = _stub(monkeypatch)
    stuck = ts.create_task("https://github.com/o/r", "stuck")
    for s in ("provisioning", "running", "landing"):
        ts.update_task(stuck.id, s)
    waiting = ts.create_task("https://github.com/o/r", "waiting")
    r = rc.CloudReconciler(max_concurrency=1)
    r.poll_once()
    assert ts.load_task(stuck.id).status == "failed"
    r.poll_once()      # 腾出来的位子下一轮生效
    assert ts.load_task(waiting.id).status != "queued"
    assert calls["create"] == 1


def test_one_bad_task_does_not_stop_the_loop(monkeypatch):
    _stub(monkeypatch)
    bad = ts.create_task("https://github.com/o/r", "bad")
    good = ts.create_task("https://github.com/o/r", "good")
    real_load = ts.load_task
    def flaky(tid):
        if tid == bad.id:
            raise RuntimeError("boom")
        return real_load(tid)
    monkeypatch.setattr(ts, "load_task", flaky)
    r = rc.CloudReconciler(max_concurrency=2)
    r.poll_once()      # 不抛