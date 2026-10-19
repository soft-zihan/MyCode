"""事件落盘：一行一个 JSON，写完立刻 flush，读的时候容忍半行。"""

from __future__ import annotations

import json

from eval.common.events import (
    EVENTS_PATH_ENV,
    RUN_ID_ENV,
    JsonlEventSink,
    NullSink,
    read_events,
    sink_from_env,
)


def test_jsonl_sink_appends_one_json_per_line_with_run_id_and_ts(tmp_path):
    path = tmp_path / "r.events.jsonl"
    sink = JsonlEventSink(path, run_id="r1")

    sink.emit("eval/task_started", task_id="t1", index=0, total=2)
    sink.emit("eval/task_finished", task_id="t1", passed=True)

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["type"] == "eval/task_started"
    assert first["run_id"] == "r1"
    assert first["task_id"] == "t1"
    assert first["index"] == 0 and first["total"] == 2
    # ts 必须有：ETA 和"卡在哪个任务"都要靠它
    assert isinstance(first["ts"], float) and first["ts"] > 0


def test_sink_flushes_each_line_so_a_tailer_can_follow(tmp_path):
    path = tmp_path / "r.events.jsonl"
    sink = JsonlEventSink(path, run_id="r1")

    sink.emit("eval/run_progress", completed=1, total=9)

    # 不 close 就要能读到：后端 tailer 靠这个看进度
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_sink_creates_parent_dirs(tmp_path):
    sink = JsonlEventSink(tmp_path / "nested" / "r.events.jsonl", run_id="r1")

    sink.emit("eval/run_started", benchmark="gaia")

    assert (tmp_path / "nested" / "r.events.jsonl").exists()


def test_read_events_returns_events_and_next_offset(tmp_path):
    path = tmp_path / "r.events.jsonl"
    sink = JsonlEventSink(path, run_id="r1")
    sink.emit("eval/task_started", task_id="a")
    sink.emit("eval/task_started", task_id="b")

    events, offset = read_events(path)
    assert [e["task_id"] for e in events] == ["a", "b"]
    assert offset == path.stat().st_size

    # 增量：从上次 offset 再读，没有新事件就是空
    events2, offset2 = read_events(path, since=offset)
    assert events2 == [] and offset2 == offset

    sink.emit("eval/task_started", task_id="c")
    events3, _ = read_events(path, since=offset)
    assert [e["task_id"] for e in events3] == ["c"]


def test_read_events_skips_a_torn_last_line(tmp_path):
    # 子进程被 kill 时最后一行可能只写了一半，tailer 不能因此抛异常
    path = tmp_path / "r.events.jsonl"
    path.write_text(
        json.dumps({"type": "eval/task_started", "run_id": "r", "ts": 1.0, "task_id": "a"})
        + "\n"
        + '{"type": "eval/task_fini',
        encoding="utf-8",
    )

    events, offset = read_events(path)

    assert [e["task_id"] for e in events] == ["a"]
    # offset 只推进到最后一个完整行，半行留着等下次补齐
    assert offset == path.stat().st_size - len('{"type": "eval/task_fini')


def test_read_events_on_missing_file_is_empty(tmp_path):
    assert read_events(tmp_path / "nope.jsonl") == ([], 0)


def test_null_sink_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # 不能断言 tmp_path 为空：conftest 的 isolated_home 已经预建了 home/workdir，
    # 所以改成比对 emit 前后的目录快照
    before = sorted(tmp_path.iterdir())
    sink = NullSink()

    sink.emit("eval/run_started", benchmark="x")

    assert sorted(tmp_path.iterdir()) == before


def test_sink_from_env_builds_jsonl_sink_when_path_is_set(tmp_path, monkeypatch):
    path = tmp_path / "e.jsonl"
    monkeypatch.setenv(EVENTS_PATH_ENV, str(path))
    monkeypatch.setenv(RUN_ID_ENV, "r9")

    sink = sink_from_env()
    sink.emit("eval/run_started", benchmark="harbor")

    assert isinstance(sink, JsonlEventSink)
    assert json.loads(path.read_text(encoding="utf-8").strip())["run_id"] == "r9"


def test_sink_from_env_is_null_when_unset(monkeypatch):
    # 直接敲 CLI 时行为必须完全不变：不落任何文件
    monkeypatch.delenv(EVENTS_PATH_ENV, raising=False)

    assert isinstance(sink_from_env(), NullSink)


def test_sink_from_env_tolerates_missing_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv(EVENTS_PATH_ENV, str(tmp_path / "e.jsonl"))
    monkeypatch.delenv(RUN_ID_ENV, raising=False)

    sink = sink_from_env()
    sink.emit("eval/run_started")

    assert json.loads((tmp_path / "e.jsonl").read_text(encoding="utf-8").strip())["run_id"] == ""


def test_envelope_fields_win_over_a_colliding_payload_key(tmp_path):
    # 信封字段必须赢过同名的 payload key：调用方万一传了 run_id= / ts=，
    # 被覆盖掉的是 run 归属和时间戳，前端按它们过滤/排序——静默丢弃比报错难查得多。
    # （type= 到不了这里：它是 emit 的具名参数，Python 直接 TypeError，是"响"的失败）
    path = tmp_path / "r.events.jsonl"
    sink = JsonlEventSink(path, run_id="r1")

    sink.emit("eval/task_started", run_id="other", ts=0.0, task_id="t1")

    event = json.loads(path.read_text(encoding="utf-8").strip())
    assert event["type"] == "eval/task_started"
    assert event["run_id"] == "r1"
    assert event["ts"] > 0
    assert event["task_id"] == "t1"
