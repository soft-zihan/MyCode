"""云端任务 REST。不打真集群：只测编排与「token 绝不回显」这条边界。"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

from frontend.server.cloud import git_broker as gb        # noqa: E402
from frontend.server.cloud import task_store as ts        # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(tmp_path / "cfg" / "config.json"))
    monkeypatch.delenv("MYCODE_SANDBOX_IMAGE", raising=False)
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)


def test_create_returns_queued(client):
    r = client.post("/api/cloud/tasks", json={"repo_url": "https://github.com/o/r", "prompt": "改点东西"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["id"]
    assert ts.load_task(body["id"]) is not None


def test_create_rejects_bad_repo_and_empty_prompt(client):
    r = client.post("/api/cloud/tasks", json={"repo_url": "file:///etc/passwd", "prompt": "x"})
    assert r.status_code == 400
    r = client.post("/api/cloud/tasks", json={"repo_url": "https://github.com/o/r", "prompt": "  "})
    assert r.status_code == 400


def test_list_only_reads_cloud_tasks(client):
    a = client.post("/api/cloud/tasks", json={"repo_url": "https://github.com/o/r", "prompt": "a"}).json()
    got = client.get("/api/cloud/tasks").json()
    assert [t["id"] for t in got] == [a["id"]]


def test_get_missing_is_404(client):
    assert client.get("/api/cloud/tasks/nope").status_code == 404


def test_cancel_terminal_is_409(client):
    t = ts.create_task("https://github.com/o/r", "p")
    ts.update_task(t.id, "failed", error="x")
    r = client.delete(f"/api/cloud/tasks/{t.id}")
    assert r.status_code == 409 and "已结束" in r.json()["detail"]


def test_cancel_active_marks_cancelled(client):
    t = ts.create_task("https://github.com/o/r", "p")
    r = client.delete(f"/api/cloud/tasks/{t.id}")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_prune_only_removes_terminal_workspaces(client, tmp_path):
    dead = ts.create_task("https://github.com/o/r", "dead")
    ts.update_task(dead.id, "failed", error="x")
    ts.update_task(dead.id, None, workspace_name="repo--dead")
    live = ts.create_task("https://github.com/o/r", "live")
    ts.update_task(live.id, "provisioning")
    ts.update_task(live.id, None, workspace_name="repo--live")
    root = tmp_path / "workspaces"
    for name in ("repo--dead", "repo--live"):
        (root / name).mkdir(parents=True)
        (root / name / "f.txt").write_text("hello", encoding="utf-8")

    r = client.post("/api/cloud/prune")
    assert r.status_code == 200
    assert r.json()["pruned"] == 1 and r.json()["freed_bytes"] >= 5
    assert not (root / "repo--dead").exists()
    assert (root / "repo--live").is_dir()      # 活跃任务的工作区绝不动


def test_git_token_never_echoed(client):
    r = client.put("/api/cloud/git-token", json={"token": "ghp_super_secret"})
    assert r.status_code == 200
    assert r.json() == {"configured": True}
    assert "ghp_super_secret" not in r.text
    got = client.get("/api/cloud/git-token")
    assert got.json() == {"configured": True}
    assert "ghp_super_secret" not in got.text


def test_put_token_writes_0600(client):
    client.put("/api/cloud/git-token", json={"token": "ghp_x"})
    p = gb.token_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_events_endpoint_reads_task_session_files(client, tmp_path):
    t = ts.create_task("https://github.com/o/r", "p")
    d = ts.task_dir(t.id) / "sessions"
    d.mkdir(parents=True)
    (d / "sess1.events.jsonl").write_text('{"type":"turn/start"}\n{"type":"turn/end"}\n', encoding="utf-8")
    r = client.get(f"/api/cloud/tasks/{t.id}/events")
    assert r.status_code == 200
    assert [e["type"] for e in r.json()["events"]] == ["turn/start", "turn/end"]