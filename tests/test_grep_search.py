"""grep_search 工具：regex 语义在两个后端（grep -E / Python re）必须一致。

回归背景 1：Unix 分支曾用默认 BRE 的 grep，`|` 被当字面字符，
导致模型写 `A|B` 交替时全部返回 "No matches found"，
子智能体反复重试烧掉大量 tokens。

回归背景 2：模型曾传 path=/ 触发对根目录的递归 grep，长时间挂起。
现在递归搜索工具（grep_search/list_files）受工作区边界守卫保护。
"""

from __future__ import annotations

import pytest

from agents.tools import _grep_search, _grep_python


@pytest.fixture
def search_dir(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("gamma only\n", encoding="utf-8")
    # chdir 使 tmp_path 成为工作区：绝对路径搜索合法
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_alternation_matches_either_branch(search_dir):
    # 核心回归：| 必须表示"或"，而不是字面字符
    result = _grep_search({"pattern": "alpha|gamma", "path": str(search_dir)})
    assert "alpha" in result
    assert "gamma" in result
    assert "No matches found" not in result


def test_single_term_still_works(search_dir):
    result = _grep_search({"pattern": "beta", "path": str(search_dir)})
    assert "beta" in result


def test_no_match_returns_message(search_dir):
    result = _grep_search({"pattern": "nonexistent_term", "path": str(search_dir)})
    assert result == "No matches found."


def test_include_glob_filters_files(search_dir):
    result = _grep_search({"pattern": "gamma", "path": str(search_dir), "include": "*.py"})
    assert result == "No matches found."  # gamma 只在 .txt 里


def test_python_fallback_alternation(search_dir):
    # 直接测 Python 后备分支，确保与 grep -E 语义一致
    result = _grep_python("alpha|gamma", str(search_dir), None)
    assert "alpha" in result
    assert "gamma" in result


def test_grouping_works(search_dir):
    result = _grep_search({"pattern": "(alpha|beta) = 1", "path": str(search_dir)})
    assert "alpha = 1" in result
    assert "beta" not in result


def test_default_path_is_workspace(search_dir):
    result = _grep_search({"pattern": "alpha"})
    assert "alpha" in result


# ─── 工作区边界守卫 ─────────────────────────────────────────

def test_grep_rejects_root_path(search_dir):
    # 回归：path=/ 必须被立即拒绝，而不是递归 grep 整个文件系统
    result = _grep_search({"pattern": "alpha", "path": "/"})
    assert result.startswith("Error:")
    assert "outside the workspace" in result


def test_grep_rejects_path_outside_workspace(search_dir):
    outside = search_dir.parent
    result = _grep_search({"pattern": "alpha", "path": str(outside)})
    assert "outside the workspace" in result


def test_list_files_rejects_path_outside_workspace(search_dir):
    from agents.tools import _list_files

    result = _list_files({"pattern": "*", "path": "/"})
    assert "outside the workspace" in result


def test_list_files_allows_workspace(search_dir):
    from agents.tools import _list_files

    result = _list_files({"pattern": "*.py"})
    assert "a.py" in result
    assert "Error" not in result
