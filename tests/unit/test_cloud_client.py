"""云端地址三源解析。服务端与 CLI 共用这一份，所以解析语义只能有一处。"""
from __future__ import annotations

from agents.core import cloud_client as cc


def test_env_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(tmp_path / "config.json"))
    (tmp_path / "cloud.json").write_text('{"url":"http://file:1","token":"ftok"}', encoding="utf-8")
    monkeypatch.setenv("MYCODE_CLOUD_URL", "http://env:2")
    monkeypatch.setenv("MYCODE_CLOUD_TOKEN", "etok")
    url, token, configured = cc.cloud_config()
    assert url == "http://env:2" and token == "etok" and configured is True


def test_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("MYCODE_CLOUD_URL", raising=False)
    monkeypatch.delenv("MYCODE_CLOUD_TOKEN", raising=False)
    (tmp_path / "cloud.json").write_text('{"url":"http://file:1/","token":"ftok"}', encoding="utf-8")
    url, token, configured = cc.cloud_config()
    assert url == "http://file:1" and token == "ftok" and configured is True


def test_default_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.delenv("MYCODE_CLOUD_URL", raising=False)
    monkeypatch.delenv("MYCODE_CLOUD_TOKEN", raising=False)
    url, token, configured = cc.cloud_config()
    assert url == cc.DEFAULT_URL and token is None and configured is False


def test_router_reexports_the_same_function():
    """routers/cloud.py 不许留第二份实现——两份会各自漂移。"""
    from frontend.server.routers import cloud as cr
    assert cr.cloud_config is cc.cloud_config
    assert (cr.URL_ENV, cr.TOKEN_ENV, cr.DEFAULT_URL) == (cc.URL_ENV, cc.TOKEN_ENV, cc.DEFAULT_URL)