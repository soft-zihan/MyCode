"""文件写入工具的前后快照（Code Review 警告检查输入）。

U0 从 agent_loop 迁出：快照属工具执行域，与推理循环无关。
"""

from __future__ import annotations

from pathlib import Path
from agents.tools import resolve_tool_path
from agents.tools.runtime import get_runtime, DockerRuntime


def capture_file_snapshot(file_path: str) -> dict | None:
    """Capture file content before/after modification for Code Review."""
    try:
        rt = get_runtime()
        if isinstance(rt, DockerRuntime):
            abs_path = file_path
            if not abs_path.startswith("/"):
                abs_path = f"{rt.workdir}/{abs_path}"
        else:
            abs_path = str(resolve_tool_path(file_path, must_exist=False).resolve())

        path = Path(abs_path)
        if path.exists():
            content = path.read_text(encoding="utf-8", errors="replace")
            return {"file_path": file_path, "content": content, "is_new": False}
        else:
            return {"file_path": file_path, "content": "", "is_new": True}
    except Exception:
        return None
