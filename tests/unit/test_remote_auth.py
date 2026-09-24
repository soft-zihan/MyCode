"""U13 远程访问安全：绑定默认值 + 可选 token 鉴权（auth 模块纯函数）。"""

from __future__ import annotations

import pytest

from frontend.server import auth


@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    monkeypatch.delenv(auth.HOST_ENV, raising=False)


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, "s3cret")


class TestBindHost:
    def test_default_localhost(self, no_token):
        assert auth.get_bind_host() == "127.0.0.1"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(auth.HOST_ENV, "0.0.0.0")
        assert auth.get_bind_host() == "0.0.0.0"

    def test_empty_env_falls_back(self, monkeypatch):
        monkeypatch.setenv(auth.HOST_ENV, "")
        assert auth.get_bind_host() == "127.0.0.1"


class TestAuthToggle:
    def test_disabled_by_default(self, no_token):
        assert not auth.auth_enabled()
        assert auth.get_auth_token() is None
        # 未启用时恒放行
        assert auth.verify_bearer(None)
        assert auth.verify_query_token(None)

    def test_enabled_with_token(self, with_token):
        assert auth.auth_enabled()


class TestBearerVerify:
    def test_valid(self, with_token):
        assert auth.verify_bearer("Bearer s3cret")

    def test_wrong_token(self, with_token):
        assert not auth.verify_bearer("Bearer wrong")

    def test_missing_header(self, with_token):
        assert not auth.verify_bearer(None)
        assert not auth.verify_bearer("")

    def test_wrong_scheme(self, with_token):
        assert not auth.verify_bearer("Basic s3cret")

    def test_scheme_case_insensitive(self, with_token):
        assert auth.verify_bearer("bearer s3cret")


class TestWsToken:
    def test_valid(self, with_token):
        assert auth.verify_query_token("s3cret")

    def test_missing_or_wrong(self, with_token):
        assert not auth.verify_query_token(None)
        assert not auth.verify_query_token("nope")


class TestExemptPaths:
    def test_static_and_exempt(self, with_token):
        assert auth.is_exempt_path("/")
        assert auth.is_exempt_path("/assets/app.js")
        assert auth.is_exempt_path("/api/health")
        assert auth.is_exempt_path("/api/auth/status")

    def test_api_protected(self, with_token):
        assert not auth.is_exempt_path("/api/sessions")
        assert not auth.is_exempt_path("/api/chat")
        assert not auth.is_exempt_path("/api/auth/status/x")  # 精确匹配非前缀
