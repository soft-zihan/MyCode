"""云端实例的探测与转发：配置解析、探活降级、转发错误映射。

不依赖真集群。用一个 http.server 假扮云端实例——要测的是「本地后端怎么处理云端的
各种回应」，不是云端本身（那是 verify/deploy-probe.sh 的活）。
"""
import http.server
import json
import sys
import threading
import time
from pathlib import Path

import pytest

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

from routers import cloud as cr        # noqa: E402


class _FakeCloud(http.server.BaseHTTPRequestHandler):
    """够用的假云端：/api/health、/api/auth/status、/api/version、/api/workspace/*。"""
    token = None                  # None = 不鉴权
    auth_required = False
    version = "9.9.9-test"
    import_status = 200
    seen_auth: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        header = self.headers.get("Authorization")
        _FakeCloud.seen_auth.append(header)
        if not _FakeCloud.token:
            return True
        return header == f"Bearer {_FakeCloud.token}"

    def do_GET(self):
        if self.path == "/api/health":
            return self._send(200, {"status": "ok"})
        if self.path == "/api/auth/status":
            return self._send(200, {"auth_required": _FakeCloud.auth_required})
        if self.path == "/api/version":
            if not self._authorized():
                return self._send(401, {"detail": "unauthorized"})
            return self._send(200, {"version": _FakeCloud.version})
        if self.path == "/api/workspace/imports":
            if not self._authorized():
                return self._send(401, {"detail": "unauthorized"})
            return self._send(200, {"root": "/home/app/.mycode/workspaces",
                                    "workspaces": [{"name": "demo", "directory": "/x/demo",
                                                    "git": True, "head": "abc1234", "branch": "main"}]})
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        if self.path == "/api/workspace/import":
            if not self._authorized():
                return self._send(401, {"detail": "unauthorized"})
            if _FakeCloud.import_status != 200:
                return self._send(_FakeCloud.import_status, {"detail": "克隆失败：远端不可达"})
            _FakeCloud.last_payload = payload
            return self._send(200, {
                "directory": f"/home/app/.mycode/workspaces/{payload.get('name') or 'demo'}",
                "name": payload.get("name") or "demo",
                "repo_url": payload.get("repo_url", ""),
                "ref": payload.get("ref"),
                "head": "abc1234", "branch": payload.get("ref") or "main", "created": True,
            })
        return self._send(404, {"detail": "not found"})


@pytest.fixture
def fake_cloud():
    _FakeCloud.token = None
    _FakeCloud.auth_required = False
    _FakeCloud.import_status = 200
    _FakeCloud.seen_auth = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeCloud)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def client(monkeypatch, tmp_path, fake_cloud):
    """默认把云端指向假实例，并把 cloud.json 指到一个空目录（隔离真配置）。"""
    monkeypatch.setenv(cr.URL_ENV, fake_cloud)
    monkeypatch.delenv(cr.TOKEN_ENV, raising=False)
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(tmp_path / "cfg" / "config.json"))
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)


class TestConfig:
    def test_default_when_unconfigured(self, monkeypatch, tmp_path):
        monkeypatch.delenv(cr.URL_ENV, raising=False)
        monkeypatch.delenv(cr.TOKEN_ENV, raising=False)
        monkeypatch.setenv("MYCODE_CONFIG_PATH", str(tmp_path / "cfg" / "config.json"))
        url, token, configured = cr.cloud_config()
        assert (url, token, configured) == (cr.DEFAULT_URL, None, False)

    def test_cloud_json_used_when_no_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv(cr.URL_ENV, raising=False)
        monkeypatch.delenv(cr.TOKEN_ENV, raising=False)
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "config.json").write_text("{}")
        (cfg / "cloud.json").write_text(json.dumps({"url": "http://10.0.0.9:8080/", "token": "sek"}))
        monkeypatch.setenv("MYCODE_CONFIG_PATH", str(cfg / "config.json"))
        url, token, configured = cr.cloud_config()
        assert (url, token, configured) == ("http://10.0.0.9:8080", "sek", True)

    def test_env_wins_over_file(self, monkeypatch, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "cloud.json").write_text(json.dumps({"url": "http://file:1", "token": "file-token"}))
        monkeypatch.setenv("MYCODE_CONFIG_PATH", str(cfg / "config.json"))
        monkeypatch.setenv(cr.URL_ENV, "http://env:2")
        monkeypatch.setenv(cr.TOKEN_ENV, "env-token")
        assert cr.cloud_config() == ("http://env:2", "env-token", True)

    def test_broken_cloud_json_does_not_crash(self, monkeypatch, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "cloud.json").write_text("{ not json")
        monkeypatch.setenv("MYCODE_CONFIG_PATH", str(cfg / "config.json"))
        monkeypatch.delenv(cr.URL_ENV, raising=False)
        url, token, configured = cr.cloud_config()
        assert url == cr.DEFAULT_URL and token is None and configured is True


class TestStatus:
    def test_unreachable_degrades_not_500(self, client, monkeypatch):
        # 用 .invalid（RFC 2606 保留，必然 NXDOMAIN）而不是「没人监听的端口」：
        # 本机 WSL 里连一个关闭的本地端口是**超时**而不是拒绝（实测 8s 才回来），
        # 拿它当不可达目标会让每条测试白等一个 connect 超时。
        monkeypatch.setenv(cr.URL_ENV, "http://no-such-host.invalid")
        r = client.get("/api/cloud/status")
        assert r.status_code == 200
        body = r.json()
        assert body["reachable"] is False and body["authenticated"] is False
        assert body["error"]

    def test_reachable_reports_version(self, client):
        r = client.get("/api/cloud/status")
        assert r.status_code == 200
        body = r.json()
        assert body["reachable"] is True and body["auth_required"] is False
        assert body["version"] == "9.9.9-test"
        assert body["latency_ms"] is not None and body["error"] is None

    def test_auth_required_without_token_is_reported(self, client):
        _FakeCloud.auth_required = True
        _FakeCloud.token = "sek"
        body = client.get("/api/cloud/status").json()
        assert body["reachable"] is True and body["auth_required"] is True
        assert body["authenticated"] is False and body["version"] is None
        assert "token" in body["error"]

    def test_token_authenticates(self, client, monkeypatch, fake_cloud):
        _FakeCloud.auth_required = True
        _FakeCloud.token = "sek"
        monkeypatch.setenv(cr.TOKEN_ENV, "sek")
        body = client.get("/api/cloud/status").json()
        assert body["authenticated"] is True and body["version"] == "9.9.9-test"
        assert body["error"] is None

    def test_env_proxy_is_ignored(self, client, monkeypatch, fake_cloud):
        # trust_env=False 的回归门：本机常驻 HTTP_PROXY，而云端地址是 localhost。
        # 少了这个参数，请求会被交给一个根本不存在于容器里的代理，然后失败得莫名其妙。
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        body = client.get("/api/cloud/status").json()
        assert body["reachable"] is True and body["version"] == "9.9.9-test"


class TestForward:
    def test_workspaces(self, client):
        r = client.get("/api/cloud/workspaces")
        assert r.status_code == 200
        body = r.json()
        assert body["root"] == "/home/app/.mycode/workspaces"
        assert body["workspaces"][0]["branch"] == "main"

    def test_import_forwards_payload_and_drops_none(self, client, fake_cloud):
        r = client.post("/api/cloud/workspaces",
                        json={"repo_url": "https://github.com/soft-zihan/MyCode.git",
                              "ref": "main", "name": None})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["url"] == fake_cloud
        assert body["created"] is True and body["name"] == "demo"
        # name=None 不该被转发过去：云端有自己的默认名推导，传个 null 只会盖掉它
        assert "name" not in _FakeCloud.last_payload

    def test_remote_400_is_mapped_with_context(self, client):
        _FakeCloud.import_status = 400
        r = client.post("/api/cloud/workspaces", json={"repo_url": "https://h/x.git"})
        assert r.status_code == 400
        assert "云端" in r.json()["detail"] and "克隆失败" in r.json()["detail"]

    def test_unauthorized_is_mapped(self, client):
        _FakeCloud.token = "sek"
        r = client.get("/api/cloud/workspaces")
        assert r.status_code == 401

    def test_unreachable_is_502(self, client, monkeypatch):
        monkeypatch.setenv(cr.URL_ENV, "http://no-such-host.invalid")
        r = client.get("/api/cloud/workspaces")
        assert r.status_code == 502
        assert "连不上云端实例" in r.json()["detail"]

    def test_unreachable_fails_fast(self, client, monkeypatch):
        # 回归门：转发超时曾经是一个 620s 的总超时，于是「云端不可达」要挂住十分钟
        # 才回来。读超时长是对的（克隆慢），连接超时必须短。
        # 这条**故意**用关闭的本地端口：本机 WSL 连它是超时而不是拒绝，
        # 正好把「connect 超时必须显式设短」这件事测出来（.invalid 走 DNS 失败，测不到）。
        monkeypatch.setenv(cr.URL_ENV, "http://127.0.0.1:1")
        started = time.perf_counter()
        assert client.get("/api/cloud/workspaces").status_code == 502
        assert time.perf_counter() - started < 20
