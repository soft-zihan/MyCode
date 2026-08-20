
#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from typing import Any
import json
import os


def session_dir() -> Path:
    """会话存储目录。默认 ~/.bear-code/sessions/，
    可用 BEAR_SESSION_DIR 环境变量重定向（测试用，避免污染真实目录）。"""
    override = os.environ.get("BEAR_SESSION_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".bear-code" / "sessions"


def _ensure_dir() -> None:
    session_dir().mkdir(parents=True, exist_ok=True)


def get_project_session_dir() -> Path:
    d = Path.cwd() / ".bear" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session_id: str, data: dict[str, Any]) -> None:
    _ensure_dir()
    (session_dir() / f"{session_id}.json").write_text(json.dumps(data, indent=2, default=str))


def save_folded_session_memory(session_id: str, record: dict[str, Any]) -> None:
    d = get_project_session_dir()
    line = json.dumps(record, ensure_ascii=False, default=str)
    with (d / f"{session_id}.folded-memory.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    (d / f"{session_id}.folded-memory.latest.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def load_session(session_id: str) -> dict[str, Any] | None:
    path = session_dir() / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def list_sessions() -> list[dict[str, Any]]:
    _ensure_dir()
    results = []
    for f in session_dir().glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if "metadata" in data:
                results.append(data["metadata"])
        except Exception:
            pass
    return results


def delete_session(session_id: str) -> bool:
    """删除指定会话文件。返回是否真的删除了。"""
    path = session_dir() / f"{session_id}.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def clean_sessions(keep_latest: int = 20, only_tmp: bool = False) -> int:
    """批量清理会话文件，返回删除数量。

    only_tmp=True 时只删 cwd 位于临时目录（pytest/tmp）的测试污染会话；
    否则按时间排序只保留最近 keep_latest 个。
    """
    sessions = list_sessions()
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    deleted = 0
    tmp_markers = ("/pytest-of-", "/tmp/", "/private/tmp/", "/var/folders/")
    if only_tmp:
        for s in sessions:
            cwd = s.get("cwd", "")
            if any(m in cwd for m in tmp_markers):
                if delete_session(s.get("id", "")):
                    deleted += 1
    else:
        for s in sessions[keep_latest:]:
            if delete_session(s.get("id", "")):
                deleted += 1
    return deleted


def get_latest_session_id() -> str | None:
    sessions = list_sessions()
    if not sessions:
        return None
    sessions.sort(key=lambda s: s.get("startTime", ""), reverse=True)
    return sessions[0].get("id")
