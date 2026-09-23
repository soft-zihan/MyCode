from __future__ import annotations

import concurrent.futures
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)


def _clear_state() -> None:
    import agents.core.rewind_service as rewind_module
    import agents.core.session as session_module
    import agents.core.session_projection_cache as projection_module
    import agents.session_manager as manager_module

    session_module._backend = None
    projection_module._projection_cache = None
    rewind_module._service = None
    manager_module._session_manager = None


def _write_session(
    session_id: str,
    *,
    cwd: Path | None = None,
    turns: int = 1,
    title: str | None = None,
    stats: dict | None = None,
):
    from agents.core.session import Session

    session = Session(session_id=session_id)
    if cwd is not None:
        session.append("session/created", {"cwd": str(cwd)})
    for i in range(turns):
        session.append("user_message", {"content": f"question {i + 1}"})
        session.append("assistant_message", {"content": f"answer {i + 1}"})
    if stats:
        session.append("stats", stats)
    session.append("turn/end", {"turn": turns})
    if title:
        session.append("session/title", {"title": title})
    return session


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    _clear_state()
    from main import app

    yield TestClient(app)
    _clear_state()


class TestHealth:
    def test_health(self, api):
        r = api.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestSteerEndpoint:
    """U1：steer 端点护栏——非运行中会话拒绝，前端凭 success=false 退回普通发送。"""

    def test_steer_rejects_inactive_session(self, api):
        r = api.post("/api/sessions/nonexistent-session/steer", json={"message": "hi"})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False

    def test_steer_request_accepts_context_files(self, api):
        # schema 校验：context_files 字段存在且可选（非运行中仍走拒绝分支）
        r = api.post(
            "/api/sessions/nonexistent-session/steer",
            json={"message": "hi", "context_files": ["README.md"]},
        )
        assert r.status_code == 200
        assert r.json()["success"] is False


class TestSessionsAPI:
    def test_list_sessions_empty(self, api):
        r = api.get("/api/sessions")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_session_not_found(self, api):
        r = api.get("/api/sessions/nonexistent")
        assert r.status_code == 404

    def test_delete_session_not_found(self, api):
        r = api.delete("/api/sessions/nonexistent")
        assert r.status_code == 404

    def test_create_and_list_session(self, api, tmp_path):
        _write_session("test-1", cwd=tmp_path, title="Test One")

        r = api.get("/api/sessions")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["id"] == "test-1"
        assert data[0]["name"] == "Test One"
        assert data[0]["cwd"] == str(tmp_path)

    def test_get_session_detail(self, api, tmp_path):
        _write_session("test-2", cwd=tmp_path, turns=2, title="Detailed")

        r = api.get("/api/sessions/test-2")
        assert r.status_code == 200
        data = r.json()
        assert data["metadata"]["id"] == "test-2"
        assert data["metadata"]["name"] == "Detailed"
        assert data["metadata"]["cwd"] == str(tmp_path)
        assert data["projections"]["title"] == "Detailed"

        types = [e["type"] for e in data["events"]]
        assert types == [
            "session/created",
            "user_message",
            "assistant_message",
            "user_message",
            "assistant_message",
            "turn/end",
            "session/title",
        ]

    def test_delete_session(self, api):
        _write_session("to-del")

        r = api.delete("/api/sessions/to-del")
        assert r.status_code == 200
        assert r.json()["success"] is True

        r2 = api.get("/api/sessions/to-del")
        assert r2.status_code == 404

        r3 = api.get("/api/sessions")
        assert all(s["id"] != "to-del" for s in r3.json())

    def test_update_session_name(self, api):
        _write_session("named")

        r = api.put("/api/sessions/named", json={"name": "new-name"})
        assert r.status_code == 200
        assert r.json()["success"] is True

        r2 = api.get("/api/sessions/named")
        assert r2.status_code == 200
        assert r2.json()["metadata"]["name"] == "new-name"
        assert r2.json()["projections"]["title"] == "new-name"

        from agents.core.session import Session
        reloaded = Session.load_from_events("named")
        assert reloaded is not None
        assert reloaded.title == "new-name"

    def test_update_session_requires_name(self, api):
        _write_session("bad-update")
        r = api.put("/api/sessions/bad-update", json={"other": "value"})
        assert r.status_code == 400

    def test_get_session_projections(self, api, tmp_path):
        _write_session("proj-1", cwd=tmp_path, title="Projected")
        r = api.get("/api/sessions/proj-1/projections")
        assert r.status_code == 200
        projections = r.json()
        assert projections["title"] == "Projected"
        assert projections["cwd"] == str(tmp_path)
        assert projections["running"] is False

    def test_fork_session(self, api):
        _write_session("orig", turns=2, title="original")

        r = api.post("/api/sessions/orig/fork")
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        new_id = data["new_session_id"]
        assert new_id != "orig"
        assert data["fork_name"] == "original(fork 1)"

        r2 = api.get(f"/api/sessions/{new_id}")
        assert r2.status_code == 200
        forked = r2.json()
        assert forked["metadata"]["name"] == "original(fork 1)"

        events = forked["events"]
        assert [e["type"] for e in events] == [
            "user_message",
            "assistant_message",
            "user_message",
            "assistant_message",
            "turn/end",
            "session/title",
            "session/title",
        ]
        assert all(e["session_id"] == new_id for e in events)

    def test_fork_session_at_seq(self, api):
        _write_session("orig-at", turns=2, title="original")

        source_events = api.get("/api/sessions/orig-at/events?limit=100").json()["events"]
        target_seq = next(
            e["seq"]
            for e in source_events
            if e["type"] == "user_message" and e["content"] == "question 2"
        )

        r = api.post("/api/sessions/orig-at/fork", json={"at_seq": target_seq})
        assert r.status_code == 200
        new_id = r.json()["new_session_id"]

        forked_events = api.get(f"/api/sessions/{new_id}/events?limit=100").json()["events"]
        assert [e["type"] for e in forked_events] == [
            "user_message",
            "assistant_message",
            "session/title",
        ]
        assert all(e["session_id"] == new_id for e in forked_events)

    def test_fork_session_keep_user_messages(self, api):
        _write_session("orig-keep", turns=2, title="original")

        r = api.post("/api/sessions/orig-keep/fork", json={"keep_user_messages": 1})
        assert r.status_code == 200
        new_id = r.json()["new_session_id"]

        forked_events = api.get(f"/api/sessions/{new_id}/events?limit=100").json()["events"]
        assert [e["type"] for e in forked_events] == [
            "user_message",
            "assistant_message",
            "session/title",
        ]

    def test_fork_increments_name(self, api):
        _write_session("orig-repeat", turns=1, title="original")

        first = api.post("/api/sessions/orig-repeat/fork").json()
        second = api.post("/api/sessions/orig-repeat/fork").json()
        assert first["fork_name"] == "original(fork 1)"
        assert second["fork_name"] == "original(fork 2)"

    def test_session_summary_from_events(self, api, tmp_path):
        _write_session(
            "summary-1",
            cwd=tmp_path,
            title="Summary",
            stats={
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_tokens": 2,
                "context_window": 128000,
                "effective_window": 108000,
                "cache_hit_rate": 0.2,
                "total_input_tokens": 20,
                "total_output_tokens": 25,
                "total_cached_tokens": 4,
                "estimated_context_tokens": 25,
                "msg_count": 2,
                "system_chars": 100,
                "user_chars": 20,
                "assistant_chars": 30,
                "tool_result_chars": 0,
            },
        )

        r = api.get("/api/sessions/summary-1/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["metadata"]["name"] == "Summary"
        assert data["metadata"]["cwd"] == str(tmp_path)
        assert data["stats"]["input_tokens"] == 10
        assert data["stats"]["output_tokens"] == 5
        assert data["stats"]["cached_tokens"] == 2
        assert data["breakdown"]["message_count"] == 2

class TestSessionEventsAPI:
    def test_events_default_pagination(self, api):
        _write_session("events-default", turns=5)

        r = api.get("/api/sessions/events-default/events?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert data["total_count"] == 11
        assert len(data["events"]) == 5
        assert data["has_more"] is True
        assert data["base_seq"] == 6

    def test_events_before_pagination(self, api):
        _write_session("events-before", turns=5)

        r = api.get("/api/sessions/events-before/events?before=6&limit=3")
        assert r.status_code == 200
        data = r.json()
        assert [e["seq"] for e in data["events"]] == [3, 4, 5]
        assert data["has_more"] is True

    def test_events_range_query(self, api):
        _write_session("events-range", turns=5)

        r = api.get("/api/sessions/events-range/events?from_seq=2&to_seq=5")
        assert r.status_code == 200
        data = r.json()
        assert [e["seq"] for e in data["events"]] == [2, 3, 4]
        assert data["has_more"] is False

    def test_events_not_found(self, api):
        r = api.get("/api/sessions/nonexistent/events")
        assert r.status_code == 404


class TestRewindAPI:
    def test_rewind_stage_commit_conversation_only(self, api):
        _write_session("rewind-api", turns=2)

        stage = api.post("/api/sessions/rewind-api/rewind/stage", json={"keep_user_messages": 1})
        assert stage.status_code == 200
        plan = stage.json()["plan"]
        assert plan["has_snapshot"] is False
        assert plan["removed_user_messages"] == 1
        assert plan["target_message"]["content"] == "question 2"

        commit = api.post(
            "/api/sessions/rewind-api/rewind/commit",
            json={"plan_id": plan["plan_id"]},
        )
        assert commit.status_code == 200
        result = commit.json()
        assert result["success"] is True
        assert result["removed_user_messages"] == 1
        assert result["restored_files"] == []

        events = api.get("/api/sessions/rewind-api/events?limit=100").json()["events"]
        assert [e["type"] for e in events] == [
            "user_message",
            "assistant_message",
            "rewind",
        ]
        assert events[-1]["target_seq"] == plan["truncate_at_seq"]

    def test_rewind_stage_clear(self, api):
        _write_session("rewind-clear", turns=2)

        stage = api.post("/api/sessions/rewind-clear/rewind/stage", json={"keep_user_messages": 1})
        plan_id = stage.json()["plan"]["plan_id"]

        clear = api.post("/api/sessions/rewind-clear/rewind/clear", json={"plan_id": plan_id})
        assert clear.status_code == 200
        assert clear.json()["status"] == "cleared"

        events = api.get("/api/sessions/rewind-clear/events?limit=100").json()["events"]
        assert len(events) == 5

        commit = api.post("/api/sessions/rewind-clear/rewind/commit", json={"plan_id": plan_id})
        assert commit.status_code == 400

    def test_rewind_direct(self, api):
        _write_session("rewind-direct", turns=2)

        r = api.post("/api/sessions/rewind-direct/rewind", json={"turns": 1})
        assert r.status_code == 200
        assert r.json()["success"] is True

        events = api.get("/api/sessions/rewind-direct/events?limit=100").json()["events"]
        assert [e["type"] for e in events] == [
            "user_message",
            "assistant_message",
            "rewind",
        ]

    def test_rewind_noop_is_rejected(self, api):
        _write_session("rewind-noop", turns=1)

        r = api.post("/api/sessions/rewind-noop/rewind/stage", json={"keep_user_messages": 1})
        assert r.status_code == 400
        assert "Nothing to rewind" in r.json()["detail"]


class TestWorkspaceAPI:
    def test_workspace_tree(self, api, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hi')")
        r = api.get("/api/workspace/tree")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == tmp_path.name

    def test_workspace_file_read(self, api, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "test.txt"
        f.write_text("hello")
        r = api.get(f"/api/workspace/file?path={f}")
        assert r.status_code == 200
        assert r.json()["content"] == "hello"

    def test_workspace_file_delete(self, api, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "del.txt"
        f.write_text("bye")
        r = api.delete(f"/api/workspace/file?path={f}")
        assert r.status_code == 200
        assert not f.exists()

    def test_workspace_create_file(self, api, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        r = api.post("/api/workspace/create", json={"path": str(tmp_path / "new.txt")})
        assert r.status_code == 200
        assert (tmp_path / "new.txt").exists()


class TestPermissionResponseAPI:
    def test_respond_permission_inactive_session(self, api):
        r = api.post("/api/events/respond", json={
            "rpc_id": "rpc-1",
            "allowed": True,
            "session_id": "nonexistent",
        })
        assert r.status_code == 200
        assert r.json()["success"] is False


class TestConcurrentRequests:
    def test_parallel_session_reads(self, api):
        for i in range(10):
            _write_session(f"par-{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(api.get, f"/api/sessions/par-{i}") for i in range(10)]
            results = [f.result() for f in futures]

        assert all(r.status_code == 200 for r in results)
        assert all(r.json()["metadata"]["id"] == f"par-{i}" for i, r in enumerate(results))


class _FakeEvalAdapter:
    benchmark = "smoke"

    def __init__(self):
        self.setup_called = False
        self.teardown_called = False

    def load_tasks(self, options):
        from eval.common.models import EvalTask

        return [
            EvalTask(
                task_id="api-task-1",
                benchmark="smoke",
                prompt="hello",
                name="API Task",
                expected_output="ok",
            )
        ]

    async def setup(self, state, options):
        self.setup_called = True

    async def run_task(self, state, task, result, options, index, total, emit):
        result.passed = True
        result.correct = True
        result.status = "passed"
        result.predicted = "ok"
        result.duration_s = 0.01
        emit({"type": "eval/task_event", "task_id": task.task_id})

    async def teardown(self, state, options):
        self.teardown_called = True


class TestEvalAPI:
    def test_eval_benchmarks(self, api):
        r = api.get("/api/eval/benchmarks")
        assert r.status_code == 200
        ids = [item["id"] for item in r.json()["benchmarks"]]
        assert ids == ["gaia", "hle", "smoke"]

    def test_start_and_inspect_fake_eval_run(self, api, tmp_path, monkeypatch):
        import time

        import eval.common.service as service_module

        monkeypatch.setattr(service_module, "REPORTS_DIR", tmp_path)
        monkeypatch.setattr(service_module, "_service", None)
        adapter = _FakeEvalAdapter()
        monkeypatch.setattr(service_module, "get_adapter", lambda benchmark: adapter)

        r = api.post("/api/eval/runs", json={
            "benchmark": "smoke",
            "skip_langfuse": True,
            "sync_langfuse_dataset": False,
            "judge_after_run": False,
        })
        assert r.status_code == 200
        run_id = r.json()["run_id"]

        deadline = time.time() + 5
        data = r.json()
        while time.time() < deadline and data.get("status") in ("pending", "running"):
            detail = api.get(f"/api/eval/runs/{run_id}")
            assert detail.status_code == 200
            data = detail.json()
            time.sleep(0.05)

        assert data["status"] == "completed"
        assert data["summary"]["passed"] == 1
        assert data["tasks"][0]["task_id"] == "api-task-1"
        assert adapter.setup_called
        assert adapter.teardown_called

        runs = api.get("/api/eval/runs")
        assert runs.status_code == 200
        assert any(run["run_id"] == run_id for run in runs.json()["runs"])

        report = api.get(f"/api/eval/reports/{run_id}")
        assert report.status_code == 200
        assert report.json()["run_id"] == run_id

        markdown = api.get(f"/api/eval/reports/{run_id}/markdown")
        assert markdown.status_code == 200
        assert run_id in markdown.text

        service_module._service = None
