"""remember 工具 + redact 脱敏单测。"""

from __future__ import annotations

import json

import pytest

from agents.core.workspace import workspace_scope
from agents.wiki.redact import redact_secrets


@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # 隔离快照目录，避免写 ~/.mycode/snapshots
    monkeypatch.setenv("HOME", str(tmp_path))
    with workspace_scope(workspace):
        yield workspace


# ─── redact ───

def test_redact_known_key_prefixes():
    text = "keys: sk-abcdefgh12345678 ghp_0123456789abcdefghij AKIA0123456789ABCDEF xoxb-1234567890-abcdefgh"
    out = redact_secrets(text)
    assert "[REDACTED:openai_key]" in out
    assert "[REDACTED:github_token]" in out
    assert "[REDACTED:aws_access_key]" in out
    assert "[REDACTED:slack_token]" in out
    assert "sk-abcdefgh12345678" not in out


def test_redact_jwt_and_pem():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
    out = redact_secrets(f"token {jwt}\n{pem}")
    assert "[REDACTED:jwt]" in out
    assert "[REDACTED:pem_private_key]" in out
    assert "MIIEowIBAAKCAQEA" not in out


def test_redact_kv_and_false_positives():
    out = redact_secrets("password=hunter2secret api_key: abc123456")
    assert "hunter2secret" not in out
    assert "abc123456" not in out
    assert "password=[REDACTED:kv_secret]" in out
    # 占位符不误杀
    keep = redact_secrets("password=<your-password> token=${TOKEN} api_key=******")
    assert "<your-password>" in keep
    assert "${TOKEN}" in keep
    assert "******" in keep


def test_redact_no_secret_unchanged():
    text = "修复了 agents/wiki/wiki_capture.py 的 tool_name 映射，git hash 13951beb7da44d7ee55ed5e771dedb40338d063e"
    assert redact_secrets(text) == text


# ─── remember ───

async def test_remember_creates_entry(ws):
    from agents.tools.wiki_tools import remember

    out = json.loads(await remember({
        "wiki_type": "feedback",
        "name": "只用 pnpm",
        "content": "规则：本项目包管理只用 pnpm。Why：lockfile 统一。How：装依赖用 pnpm add。",
    }))
    assert out["action"] == "created"
    assert out["path"].startswith("feedback/")
    entry = (ws / ".mycode" / "wiki" / out["path"]).read_text()
    assert "只用 pnpm" in entry


async def test_remember_redacts_before_write(ws):
    from agents.tools.wiki_tools import remember

    out = json.loads(await remember({
        "wiki_type": "knowledge",
        "name": "api config",
        "content": "部署时用 sk-livekey123456789 调用服务",
    }))
    assert out["action"] == "created"
    entry = (ws / ".mycode" / "wiki" / out["path"]).read_text()
    assert "sk-livekey123456789" not in entry
    assert "[REDACTED:openai_key]" in entry


async def test_remember_merges_similar(ws, monkeypatch):
    from agents.tools import wiki_tools
    from agents.tools.wiki_tools import remember

    first = json.loads(await remember({
        "wiki_type": "knowledge", "name": "python 版本",
        "content": "本项目使用 python 3.12，虚拟环境在 .venv",
    }))
    assert first["action"] == "created"

    # stub preflight 返回高分命中（不依赖 ollama embedding）
    from agents.wiki.wiki_manager import read_wiki_entry

    async def fake_preflight(content, wiki_type, top_k=5):
        entry = read_wiki_entry(first["path"])
        return [(entry, 0.9)]

    monkeypatch.setattr("agents.wiki.wiki_manager.preflight_wiki_search", fake_preflight)
    second = json.loads(await remember({
        "wiki_type": "knowledge", "name": "python 版本 2",
        "content": "本项目使用 python 3.12，虚拟环境在 .venv，测试用 pytest",
    }))
    assert second["action"] == "merged"
    assert second["path"] == first["path"]


async def test_remember_appends_mid_similarity(ws, monkeypatch):
    from agents.tools.wiki_tools import remember

    first = json.loads(await remember({
        "wiki_type": "user", "name": "语言偏好", "content": "用户偏好中文回复",
    }))
    from agents.wiki.wiki_manager import read_wiki_entry

    async def fake_preflight(content, wiki_type, top_k=5):
        return [(read_wiki_entry(first["path"]), 0.75)]

    monkeypatch.setattr("agents.wiki.wiki_manager.preflight_wiki_search", fake_preflight)
    second = json.loads(await remember({
        "wiki_type": "user", "name": "回复风格", "content": "用户偏好简洁回复，不要客套话",
    }))
    assert second["action"] == "appended"
    body = (ws / ".mycode" / "wiki" / second["path"]).read_text()
    assert "中文回复" in body and "简洁回复" in body


async def test_remember_validation(ws):
    from agents.tools.wiki_tools import remember

    out = json.loads(await remember({"wiki_type": "self_improvement", "name": "x", "content": "y"}))
    assert out["action"] == "error"

    out = json.loads(await remember({"wiki_type": "workflow_pattern", "name": "x", "symptom": "s"}))
    assert out["action"] == "error"
    assert "workaround" in out["error"]

    out = json.loads(await remember({"wiki_type": "knowledge", "name": "", "content": "y"}))
    assert out["action"] == "error"


async def test_remember_workflow_pattern(ws):
    from agents.tools.wiki_tools import remember

    out = json.loads(await remember({
        "wiki_type": "workflow_pattern",
        "name": "db-timeout",
        "symptom": "连接超时",
        "root_cause": "连接池耗尽",
        "workaround": "调大 pool_size",
    }))
    assert out["action"] == "created"
    entry = (ws / ".mycode" / "wiki" / out["path"]).read_text()
    assert "## Symptom" in entry and "kind: failure" in entry
