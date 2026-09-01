"""文件检查点存储：为 /rewind 提供"回退对话同时回退文件"的能力。

设计：
- 每次 write_file / edit_file 修改文件【之前】，先对原文件拍快照
  （文件不存在则记录"原本不存在"）。
- 每个对话轮次（turn）开始时记录一个边界：当前消息数 + 已有快照数。
- /rewind 到某个边界时：
  1. 把消息历史截断到边界消息数（边界记录在用户消息追加之前，
     因此截断后保留的是完整的 tool_use/tool_result 配对）；
  2. 把边界之后所有被修改过的文件恢复到边界时刻的内容；
     边界之后【新建】的文件直接删除；
  3. 清理 Agent 的 _read_file_state，避免"外部修改"误报。

快照存放在 ~/.bear-code/checkpoints/<session_id>/files/，
边界与快照元数据可随 session 一起序列化/恢复。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.session import atomic_write_text


def _checkpoint_root() -> Path:
    """检查点根目录。默认 ~/.bear-code/checkpoints/，
    可用 BEAR_CHECKPOINT_DIR 重定向（测试用，避免污染真实目录）。"""
    override = os.environ.get("BEAR_CHECKPOINT_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".bear-code" / "checkpoints"


@dataclass
class TurnBoundary:
    """一个对话轮次开始时的状态标记。"""

    turn: int
    message_count: int  # 该轮用户消息追加【之前】的消息数
    checkpoint_count: int  # 该轮开始时已存在的快照数


class FileCheckpointStore:
    """按 session 隔离的文件快照存储。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.dir = _checkpoint_root() / session_id / "files"
        self.dir.mkdir(parents=True, exist_ok=True)
        # 每个快照：目标文件绝对路径 / 快照文件名 / 修改前是否存在
        self._snapshots: list[dict[str, Any]] = []
        self._existed_before: dict[str, bool] = {}

    # ── 快照 ────────────────────────────────────────────────

    def snapshot(self, path: str | Path) -> int | None:
        """在修改前给文件拍快照，返回快照 id；同一路径同内容不重复拍。"""
        p = Path(path).expanduser()
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)

        if p.exists():
            content = p.read_text(errors="replace")
            # 去重：如果最近一次快照就是同一路径同一内容，跳过。
            if self._snapshots:
                last = self._snapshots[-1]
                if last["path"] == key:
                    try:
                        last_content = (self.dir / last["snapshot"]).read_text(errors="replace")
                        if last_content == content:
                            return len(self._snapshots) - 1
                    except OSError:
                        pass
            snap_name = f"{len(self._snapshots):05d}.txt"
            atomic_write_text(self.dir / snap_name, content)
            self._snapshots.append({"path": key, "snapshot": snap_name, "existed": True})
            self._existed_before.setdefault(key, True)
        else:
            # 文件不存在：记录"原本不存在"，rewind 时若它是后来创建的则删除。
            self._snapshots.append({"path": key, "snapshot": None, "existed": False})
            self._existed_before.setdefault(key, False)
        return len(self._snapshots) - 1

    @property
    def checkpoint_count(self) -> int:
        return len(self._snapshots)

    # ── 恢复 ────────────────────────────────────────────────

    def files_changed_after(self, checkpoint_index: int) -> list[str]:
        """边界快照序号之后被修改过的文件路径列表（去重）。"""
        seen: list[str] = []
        for snap in self._snapshots[checkpoint_index:]:
            if snap["path"] not in seen:
                seen.append(snap["path"])
        return seen

    def restore_after(self, checkpoint_index: int) -> dict[str, str]:
        """把 checkpoint_index 之后的所有修改回滚。

        每个文件恢复到边界之后的【第一个】快照（即边界时刻的内容）；
        边界时不存在的文件直接删除。返回 {路径: "restored"|"deleted"}。
        """
        results: dict[str, str] = {}
        # 每个路径取边界后最早的快照，就是边界时刻的状态。
        earliest: dict[str, dict[str, Any]] = {}
        for snap in self._snapshots[checkpoint_index:]:
            earliest.setdefault(snap["path"], snap)

        for path_key, snap in earliest.items():
            target = Path(path_key)
            if snap["existed"] and snap["snapshot"]:
                try:
                    content = (self.dir / snap["snapshot"]).read_text(errors="replace")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                    results[path_key] = "restored"
                except OSError as e:
                    results[path_key] = f"error: {e}"
            else:
                # 边界时不存在 → 是边界之后新建的，回退即删除。
                try:
                    if target.exists():
                        target.unlink()
                        results[path_key] = "deleted"
                except OSError as e:
                    results[path_key] = f"error: {e}"
        return results

    # ── 序列化（随 session 持久化/恢复）─────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "snapshots": self._snapshots,
            "existed_before": self._existed_before,
        }

    def restore_state(self, data: dict[str, Any]) -> None:
        """从 session 数据恢复快照元数据（快照文件仍在磁盘上）。"""
        snaps = data.get("snapshots") or []
        self._snapshots = [s for s in snaps if isinstance(s, dict)]
        self._existed_before = dict(data.get("existed_before") or {})

    # ── fork（session 分支）────────────────────────────────

    def fork(self, new_session_id: str) -> "FileCheckpointStore":
        """创建一个完全相同的分支存储：复制全部快照文件与元数据。

        fork 后的两个 store 完全独立，各自的 /rewind 互不影响。
        """
        import shutil

        new_store = FileCheckpointStore(new_session_id)
        for snap in self._snapshots:
            if snap.get("snapshot"):
                src = self.dir / snap["snapshot"]
                if src.exists():
                    shutil.copy2(src, new_store.dir / snap["snapshot"])
        new_store._snapshots = [dict(s) for s in self._snapshots]
        new_store._existed_before = dict(self._existed_before)
        return new_store
