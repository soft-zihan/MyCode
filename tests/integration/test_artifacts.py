"""U9：artifacts 只读通道——伺服会话图表产物 + 穿越防护 + query token 鉴权回退。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
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


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "sessions"))
    _clear_state()
    from main import app

    yield TestClient(app)
    _clear_state()


@pytest.fixture
def artifact_session(tmp_path):
    """磁盘会话（cwd=workspace）+ workspace 内一张图表产物。"""
    from agents.core.session import Session

    ws = tmp_path / "workspace"
    sid = "art-sess"
    art_dir = ws / ".mycode" / "artifacts" / sid
    art_dir.mkdir(parents=True)
    (art_dir / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    session = Session(session_id=sid)
    session.append("session/created", {"cwd": str(ws)})
    session.append("user_message", {"content": "画个图"})
    session.append("assistant_message", {"content": "![图](/api/artifacts/art-sess/chart.png)"})
    session.append("turn/end", {"turn": 1})
    return sid, ws


def test_artifact_served(api, artifact_session):
    sid, _ = artifact_session
    r = api.get(f"/api/artifacts/{sid}/chart.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content.startswith(b"\x89PNG")


def test_artifact_missing_file_404(api, artifact_session):
    sid, _ = artifact_session
    r = api.get(f"/api/artifacts/{sid}/nope.png")
    assert r.status_code == 404


def test_artifact_missing_session_404(api):
    r = api.get("/api/artifacts/ghost-sess/chart.png")
    assert r.status_code == 404


def test_artifact_rejects_traversal(artifact_session):
    """直接函数级验证穿越防护（TestClient 会规范化 URL，无法投递原始 ../ 路径）。"""
    from frontend.server.routers.artifacts import get_artifact

    sid, ws = artifact_session
    secret = ws.parent / "secret.txt"
    secret.write_text("top secret")

    for bad_sid, bad_name in [
        (sid, "../../secret.txt"),
        (sid, ".."),
        ("..", "chart.png"),
        (sid, "a/b.png"),
        (sid, "a\\b.png"),
    ]:
        with pytest.raises(HTTPException) as exc:
            get_artifact(bad_sid, bad_name)
        assert exc.value.status_code in (400, 404)


def test_artifact_auth_query_token_fallback(api, artifact_session, monkeypatch):
    """<img> 无法带 Authorization header：artifacts 通道允许 query token（WS 同款信任模型）。"""
    sid, _ = artifact_session
    monkeypatch.setenv("MYCODE_AUTH_TOKEN", "s3cret")

    assert api.get(f"/api/artifacts/{sid}/chart.png").status_code == 401
    assert api.get(f"/api/artifacts/{sid}/chart.png?token=wrong").status_code == 401
    r = api.get(f"/api/artifacts/{sid}/chart.png?token=s3cret")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")

    # query token 回退仅限 artifacts 通道，其余 /api 不受影响
    assert api.get("/api/sessions?token=s3cret").status_code == 401
