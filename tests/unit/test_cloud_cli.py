"""`mycode cloud` 的退出码与「不加 --yes 不真删」。httpx 全部 monkeypatch。"""
from __future__ import annotations

import httpx
import pytest

from agents import cloud_cli as cli


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setenv("MYCODE_CLOUD_URL", "http://cloud.test:8080")
    monkeypatch.delenv("MYCODE_CLOUD_TOKEN", raising=False)
    yield


def test_exec_success_prints_id_and_logs_hint(monkeypatch, capsys):
    seen = {}

    def fake(method, url, **kw):
        seen["method"], seen["url"], seen["json"] = method, url, kw.get("json")
        return _Resp(200, {"id": "abc123"})

    monkeypatch.setattr(cli.httpx, "request", fake)
    assert cli.main(["exec", "--repo", "https://github.com/o/r", "改点东西"]) == 0
    out = capsys.readouterr().out
    assert "abc123" in out and "mycode cloud logs abc123" in out
    # A 批没有浏览器任务表单，不许打印一个打开是空页面的 URL
    assert "?task=" not in out
    assert seen["method"] == "POST" and seen["url"].endswith("/api/cloud/tasks")
    assert seen["json"]["prompt"] == "改点东西"


def test_exec_http_error_exits_1(monkeypatch, capsys):
    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli.httpx, "request", boom)
    assert cli.main(["exec", "--repo", "https://github.com/o/r", "x"]) == 1
    assert "提交失败" in capsys.readouterr().err


def test_exec_rejected_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(cli.httpx, "request", lambda *a, **kw: _Resp(400, {"detail": "仓库地址不合法"}))
    assert cli.main(["exec", "--repo", "bad", "x"]) == 1
    assert "仓库地址不合法" in capsys.readouterr().err


def test_list_prints_task_id(monkeypatch, capsys):
    payload = [{"id": "abc123", "status": "running", "repo_url": "https://github.com/o/r", "pr_url": ""}]
    monkeypatch.setattr(cli.httpx, "request", lambda *a, **kw: _Resp(200, payload))
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "abc123" in out and "running" in out


def test_prune_without_yes_does_not_post(monkeypatch, capsys):
    calls = []

    def fake(method, url, **kw):
        calls.append(method)
        return _Resp(200, [{"id": "a", "status": "succeeded", "workspace_name": "r--a"}])

    monkeypatch.setattr(cli.httpx, "request", fake)
    assert cli.main(["prune"]) == 0
    assert calls == ["GET"]
    assert "r--a" in capsys.readouterr().out


def test_prune_with_yes_posts(monkeypatch, capsys):
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url))
        return _Resp(200, {"pruned": 2, "freed_bytes": 10 * 1024 * 1024})

    monkeypatch.setattr(cli.httpx, "request", fake)
    assert cli.main(["prune", "--yes"]) == 0
    assert calls == [("POST", "http://cloud.test:8080/api/cloud/prune")]
    assert "10.0 MiB" in capsys.readouterr().out


def test_prune_dry_run_unreachable_exits_1(monkeypatch, capsys):
    def boom(*a, **kw):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(cli.httpx, "request", boom)
    assert cli.main(["prune"]) == 1
    err = capsys.readouterr().err
    assert "连不上云端" in err and "Traceback" not in err


def test_prune_yes_unreachable_exits_1(monkeypatch, capsys):
    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli.httpx, "request", boom)
    assert cli.main(["prune", "--yes"]) == 1
    assert "连不上云端" in capsys.readouterr().err


def test_logs_events_unreachable_still_prints_status(monkeypatch, capsys):
    task = {"id": "a", "status": "succeeded", "repo_url": "https://github.com/o/r",
            "branch": "", "pr_url": "", "error": ""}

    def fake(method, url, **kw):
        if url.endswith("/events"):
            raise httpx.ConnectTimeout("timed out")
        return _Resp(200, task)

    monkeypatch.setattr(cli.httpx, "request", fake)
    assert cli.main(["logs", "a"]) == 0
    assert "succeeded" in capsys.readouterr().out


def test_token_goes_in_bearer_header(monkeypatch):
    monkeypatch.setenv("MYCODE_CLOUD_TOKEN", "sek")
    seen = {}

    def fake(method, url, **kw):
        seen["headers"] = kw.get("headers")
        seen["trust_env"] = kw.get("trust_env")
        return _Resp(200, [])

    monkeypatch.setattr(cli.httpx, "request", fake)
    cli.main(["list"])
    assert seen["headers"]["Authorization"] == "Bearer sek"
    assert seen["trust_env"] is False      # 本机代理会把 localhost 请求绕出去