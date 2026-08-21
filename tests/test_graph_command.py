"""/graph 命令层测试：子命令分发、CLI 缺失提示、帮助文本。

代码图谱本体由外部 code-review-graph（MCP + CLI）提供，BearCode 侧
只有薄薄的命令层（agents/graph_cmd.py），这里用假可执行文件验证它：
- 命令拼装与输出透传（fake CLI + PATH 注入）
- 未安装 CLI 时给出安装提示
- 未知子命令回退到帮助

不依赖真实安装 code-review-graph。
"""

from __future__ import annotations

import os
import stat

import pytest

from agents.graph_cmd import run_graph_command


@pytest.fixture
def fake_crg(tmp_path, monkeypatch):
    """在 tmp_path 放一个假的 code-review-graph 可执行文件，并置顶 PATH。

    脚本只回显自己的参数，用于断言命令拼装是否正确。
    """
    script = tmp_path / "code-review-graph"
    script.write_text('#!/bin/sh\necho "FAKE-CRG $@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    return tmp_path


def test_no_arg_shows_help():
    result = run_graph_command("")
    assert "/graph build" in result
    assert "/graph update" in result
    assert "/graph status" in result


def test_whitespace_arg_shows_help():
    result = run_graph_command("   ")
    assert "/graph build" in result


def test_unknown_subcommand_shows_error_and_help():
    result = run_graph_command("fly")
    assert "Unknown subcommand: fly" in result
    assert "/graph build" in result


def test_missing_cli_shows_install_hint(tmp_path, monkeypatch):
    # PATH 指向空目录 → shutil.which 找不到可执行文件
    monkeypatch.setenv("PATH", str(tmp_path))
    result = run_graph_command("build")
    assert "pip install code-review-graph" in result


def test_build_invokes_cli_and_warns_restart(fake_crg):
    result = run_graph_command("build")
    assert "FAKE-CRG build" in result
    # 构建后必须提醒重启会话，否则模型看不到新加载的 MCP 工具
    assert "重启" in result


def test_update_invokes_cli(fake_crg):
    result = run_graph_command("update")
    assert "FAKE-CRG update" in result
    assert "重启" in result


def test_status_passes_json_flag_and_no_restart_note(fake_crg):
    result = run_graph_command("status")
    assert "FAKE-CRG status --json" in result
    # status 是只读查询，不需要重启提示
    assert "重启" not in result
