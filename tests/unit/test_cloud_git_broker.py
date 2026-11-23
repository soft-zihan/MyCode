"""PAT 存取与 token 泄漏断言。断言是安全承重墙，必须能红。"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from frontend.server.cloud import git_broker as gb
from frontend.server.cloud.task_store import CloudTask


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    yield


def test_token_file_is_0600():
    gb.write_token("ghp_secret_value")
    p = gb.token_path()
    assert p.is_file()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert gb.read_token() == "ghp_secret_value"
    assert gb.token_configured() is True


def test_no_token_is_not_configured():
    assert gb.read_token() is None
    assert gb.token_configured() is False


def test_empty_token_clears():
    gb.write_token("x")
    gb.write_token("")
    assert gb.token_configured() is False


def test_leak_assertion_catches_git_config(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://ghp_secret_value@github.com/o/r\n', encoding="utf-8")
    with pytest.raises(gb.GitBrokerError):
        gb.assert_no_token_leak(tmp_path, "ghp_secret_value")


def test_leak_assertion_catches_packed_refs_and_remote(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    (tmp_path / ".git" / "FETCH_HEAD").write_text("ghp_secret_value", encoding="utf-8")
    with pytest.raises(gb.GitBrokerError):
        gb.assert_no_token_leak(tmp_path, "ghp_secret_value")


def test_clean_repo_passes(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/o/r\n', encoding="utf-8")
    gb.assert_no_token_leak(tmp_path, "ghp_secret_value")   # 不抛


def test_land_without_token_raises(tmp_path):
    t = CloudTask(id="x", repo_url="https://github.com/o/r", prompt="p")
    with pytest.raises(gb.GitBrokerError):
        gb.land_task(t, tmp_path)


def test_repo_slug_parsing():
    assert gb.repo_slug("https://github.com/o/r") == "o/r"
    assert gb.repo_slug("https://github.com/o/r.git") == "o/r"
    assert gb.repo_slug("git@github.com:o/r.git") == "o/r"
    with pytest.raises(gb.GitBrokerError):
        gb.repo_slug("https://gitlab.com/o/r")     # A 批只支持 GitHub


def test_askpass_script_is_removed_and_token_never_in_url(tmp_path, monkeypatch):
    """push 用 GIT_ASKPASS 一次性注入：脚本用完删掉，remote URL 里没有 token。"""
    gb.write_token("ghp_secret_value")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/o/r\n', encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_git(args, cwd, timeout, env=None):
        seen["args"] = args
        seen["env"] = env or {}
        if args[:2] == ["status", "--porcelain"]:
            return 0, " M a.py\n", ""
        return 0, "", ""

    monkeypatch.setattr(gb, "_git", fake_git)

    class _Resp:
        status_code = 201

        @staticmethod
        def json():
            return {"html_url": "https://github.com/o/r/pull/1", "mergeable": True}

    monkeypatch.setattr(gb.httpx, "post", lambda *a, **kw: _Resp())
    out = gb.land_task(
        CloudTask(id="abc123", repo_url="https://github.com/o/r", prompt="改点东西"),
        tmp_path,
    )
    assert out["pr_url"] == "https://github.com/o/r/pull/1"
    assert out["branch"] == "mycode/abc123"
    assert "ghp_secret_value" not in repr(seen["args"])
    askpass = Path(seen["env"]["GIT_ASKPASS"])
    assert not askpass.exists()      # finally 删掉了