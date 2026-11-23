"""云端任务表：状态机与 JSON IO。"""
from __future__ import annotations

import pytest

from frontend.server.cloud import task_store as ts


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    yield


def test_create_then_load_roundtrip():
    t = ts.create_task("https://github.com/o/r", "修一下登录", ref="main", setup_command="npm ci")
    assert t.status == "queued"
    assert t.repo_url == "https://github.com/o/r"
    got = ts.load_task(t.id)
    assert got is not None and got.prompt == "修一下登录" and got.ref == "main"


def test_tasks_root_follows_mycode_home(tmp_path):
    assert ts.tasks_root() == tmp_path / "cloud_tasks"


def test_legal_transition_chain():
    t = ts.create_task("https://github.com/o/r", "p")
    for s in ("provisioning", "running", "landing", "succeeded"):
        t = ts.update_task(t.id, s)
        assert t.status == s
    assert t.id not in {x.id for x in ts.active_tasks()}


def test_illegal_transition_rejected():
    t = ts.create_task("https://github.com/o/r", "p")
    with pytest.raises(ValueError):
        ts.update_task(t.id, "succeeded")          # queued 不能直跳 succeeded
    with pytest.raises(ValueError):
        ts.update_task(t.id, "running")            # 必须先 provisioning


def test_terminal_is_absorbing():
    t = ts.create_task("https://github.com/o/r", "p")
    ts.update_task(t.id, "failed", error="clone 失败")
    with pytest.raises(ValueError):
        ts.update_task(t.id, "running")


def test_landing_can_end_in_timeout():
    """超时的任务也要走落地（半成品不该丢），所以 landing → timeout 必须合法。"""
    t = ts.create_task("https://github.com/o/r", "p")
    for s in ("provisioning", "running", "landing", "timeout"):
        t = ts.update_task(t.id, s)
    assert t.status == "timeout"


def test_provisioning_can_jump_to_landing():
    """2s 一轮的轮询：短任务可能还没被看到 Running 就已经 Succeeded。"""
    t = ts.create_task("https://github.com/o/r", "p")
    ts.update_task(t.id, "provisioning")
    assert ts.update_task(t.id, "landing").status == "landing"


def test_one_unreadable_record_does_not_break_list(monkeypatch):
    good = ts.create_task("https://github.com/o/r", "good")
    bad = ts.create_task("https://github.com/o/r", "bad")
    real_load = ts.load_task

    def flaky(tid):
        if tid == bad.id:
            raise RuntimeError("boom")
        return real_load(tid)

    monkeypatch.setattr(ts, "load_task", flaky)
    assert [x.id for x in ts.list_tasks()] == [good.id]


def test_update_fields_without_status():
    t = ts.create_task("https://github.com/o/r", "p")
    t2 = ts.update_task(t.id, None, job_name="mycode-task-x", session_id="abc")
    assert t2.status == "queued" and t2.job_name == "mycode-task-x" and t2.session_id == "abc"


def test_unknown_field_rejected():
    t = ts.create_task("https://github.com/o/r", "p")
    with pytest.raises(ValueError):
        ts.update_task(t.id, None, nope="x")


def test_corrupt_json_is_quarantined_not_fatal(tmp_path):
    t = ts.create_task("https://github.com/o/r", "p")
    p = ts.task_dir(t.id) / "task.json"
    p.write_text("{not json", encoding="utf-8")
    assert ts.load_task(t.id) is None
    assert ts.list_tasks() == []                   # 一条坏记录不能带走整个列表
    assert p.with_suffix(".json.bad").exists()     # 隔离留痕


def test_list_sorted_desc_and_limited():
    ids = [ts.create_task("https://github.com/o/r", f"p{i}").id for i in range(5)]
    got = ts.list_tasks(limit=3)
    assert len(got) == 3
    assert [x.id for x in got] == list(reversed(ids))[:3]


def test_load_missing_returns_none():
    assert ts.load_task("does-not-exist") is None