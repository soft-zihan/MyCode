"""增量读会话 JSONL 并广播。沙箱与控制面之间唯一的通道就是这份文件。"""
from __future__ import annotations

import json

import pytest

from frontend.server.cloud import tailer as tl
from frontend.server.cloud import task_store as ts


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    yield


def _write_events(task_id: str, session_id: str, events: list[dict]) -> None:
    d = ts.task_dir(task_id) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{session_id}.events.jsonl").open("a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def test_read_new_lines_is_incremental(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    events, off = tl.read_new_lines(p, 0)
    assert [e["a"] for e in events] == [1, 2] and off > 0
    with p.open("a", encoding="utf-8") as f:
        f.write('{"a":3}\n')
    events2, off2 = tl.read_new_lines(p, off)
    assert [e["a"] for e in events2] == [3] and off2 > off


def test_partial_last_line_is_not_consumed(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a":1}\n{"a":', encoding="utf-8")   # 半截行：写入方还没写完
    events, off = tl.read_new_lines(p, 0)
    assert [e["a"] for e in events] == [1]
    with p.open("a", encoding="utf-8") as f:
        f.write('2}\n')
    events2, _ = tl.read_new_lines(p, off)
    assert [e["a"] for e in events2] == [2]


def test_missing_file_returns_empty(tmp_path):
    assert tl.read_new_lines(tmp_path / "nope.jsonl", 0) == ([], 0)


def test_poll_broadcasts_with_target_session():
    t = ts.create_task("https://github.com/o/r", "p")
    ts.update_task(t.id, "provisioning")
    ts.update_task(t.id, "running")
    _write_events(t.id, "sess-1", [{"type": "turn/start", "session_id": "sess-1"}])
    seen: list[tuple[dict, str | None]] = []
    tr = tl.TaskTailer(lambda ev, target: seen.append((ev, target)))
    n = tr.poll_once()
    assert n == 1
    assert seen[0][1] == "sess-1"        # 路由到主会话，子代理事件也归到它
    assert tr.session_id_for(ts.load_task(t.id)) == "sess-1"


def test_poll_does_not_rebroadcast():
    t = ts.create_task("https://github.com/o/r", "p")
    ts.update_task(t.id, "provisioning"); ts.update_task(t.id, "running")
    _write_events(t.id, "sess-1", [{"type": "turn/start", "session_id": "sess-1"}])
    tr = tl.TaskTailer(lambda ev, target: None)
    assert tr.poll_once() == 1
    assert tr.poll_once() == 0


def test_one_bad_broadcast_does_not_drop_the_rest():
    t = ts.create_task("https://github.com/o/r", "p")
    ts.update_task(t.id, "provisioning"); ts.update_task(t.id, "running")
    _write_events(t.id, "sess-1", [
        {"type": "turn/start", "session_id": "sess-1"},
        {"type": "turn/end", "session_id": "sess-1"},
    ])
    seen = []
    def boom(ev, target):
        if ev["type"] == "turn/start":
            raise RuntimeError("subscriber gone")
        seen.append(ev)
    tr = tl.TaskTailer(boom)
    tr.poll_once()
    assert len(seen) == 1 and seen[0]["type"] == "turn/end"


def test_terminal_task_is_dropped_after_drain():
    t = ts.create_task("https://github.com/o/r", "p")
    ts.update_task(t.id, "provisioning"); ts.update_task(t.id, "running")
    _write_events(t.id, "sess-1", [{"type": "turn/end", "session_id": "sess-1"}])
    tr = tl.TaskTailer(lambda ev, target: None)
    tr.poll_once()
    ts.update_task(t.id, "landing"); ts.update_task(t.id, "succeeded")
    tr.poll_once()
    assert t.id not in tr.tracked()


def test_subagent_events_route_to_main_session():
    t = ts.create_task("https://github.com/o/r", "p")
    ts.update_task(t.id, "provisioning"); ts.update_task(t.id, "running")
    _write_events(t.id, "main", [{"type": "turn/start", "session_id": "main"}])
    _write_events(t.id, "sub", [{"type": "text", "session_id": "sub"}])
    seen: list[tuple[dict, str | None]] = []
    tr = tl.TaskTailer(lambda ev, target: seen.append((ev, target)))
    tr.poll_once()
    assert {target for _, target in seen} == {"main"}