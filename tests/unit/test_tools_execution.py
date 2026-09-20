from __future__ import annotations

import asyncio
import pytest

from agents.tools import (
    execute_tool,
    CONCURRENCY_SAFE_TOOLS,
    _truncate_result,
)


class TestReadBeforeWrite:

    def test_write_without_read_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "code.py"
        f.write_text("original")
        result = asyncio.run(execute_tool(
            "write_file",
            {"file_path": str(f), "content": "new"},
            read_file_state={},
        ))
        assert "Error" in result
        assert "read" in result.lower()

    def test_write_after_read_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "code.py"
        f.write_text("original")
        import os
        mtime = os.path.getmtime(str(f))
        read_state = {str(f.resolve()): mtime}
        result = asyncio.run(execute_tool(
            "write_file",
            {"file_path": str(f), "content": "new content"},
            read_file_state=read_state,
        ))
        assert not result.startswith("Error")
        assert f.read_text() == "new content"

    def test_edit_without_read_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "code.py"
        f.write_text("original")
        result = asyncio.run(execute_tool(
            "edit_file",
            {"file_path": str(f), "old_string": "original", "new_string": "new"},
            read_file_state={},
        ))
        assert "Error" in result

    def test_externally_modified_file_warns(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "code.py"
        f.write_text("v1")
        import os
        old_mtime = os.path.getmtime(str(f))
        read_state = {str(f.resolve()): old_mtime}
        import time
        time.sleep(0.05)
        f.write_text("v2 externally modified")
        result = asyncio.run(execute_tool(
            "write_file",
            {"file_path": str(f), "content": "new"},
            read_file_state=read_state,
        ))
        assert "modified externally" in result.lower() or "Warning" in result


class TestTruncation:

    def test_short_result_unchanged(self):
        assert _truncate_result("hello") == "hello"

    def test_long_result_truncated(self):
        long = "x" * 100000
        result = _truncate_result(long)
        assert len(result) < len(long)
        assert "truncated" in result

    def test_truncation_preserves_head_and_tail(self):
        long = "HEAD" + "x" * 100000 + "TAIL"
        result = _truncate_result(long)
        assert result.startswith("HEAD")
        assert result.endswith("TAIL")


class TestConcurrencyBatching:

    def test_concurrency_safe_tools_defined(self):
        assert "read_file" in CONCURRENCY_SAFE_TOOLS
        assert "list_files" in CONCURRENCY_SAFE_TOOLS
        assert "grep_search" in CONCURRENCY_SAFE_TOOLS
        assert "shell_status" in CONCURRENCY_SAFE_TOOLS

    def test_write_tools_not_concurrency_safe(self):
        assert "write_file" not in CONCURRENCY_SAFE_TOOLS
        assert "edit_file" not in CONCURRENCY_SAFE_TOOLS

    def test_shell_not_concurrency_safe(self):
        assert "run_shell" not in CONCURRENCY_SAFE_TOOLS


class TestFileTools:

    def test_read_file_returns_content_with_line_numbers(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        result = asyncio.run(execute_tool(
            "read_file",
            {"file_path": str(f)},
        ))
        assert "line1" in result
        assert "line2" in result

    def test_read_file_with_offset_and_limit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "test.py"
        lines = "\n".join(f"line{i}" for i in range(20))
        f.write_text(lines)
        result = asyncio.run(execute_tool(
            "read_file",
            {"file_path": str(f), "offset": 5, "limit": 3},
        ))
        assert "line4" in result
        assert "line5" in result
        assert "line6" in result

    def test_read_nonexistent_file_errors(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = asyncio.run(execute_tool(
            "read_file",
            {"file_path": str(tmp_path / "nonexistent.py")},
        ))
        assert "Error" in result or "not found" in result.lower() or "No such file" in result

    def test_list_files_with_glob(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("x")
        (tmp_path / "c.txt").write_text("x")
        result = asyncio.run(execute_tool(
            "list_files",
            {"pattern": "*.py", "path": str(tmp_path)},
        ))
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result
