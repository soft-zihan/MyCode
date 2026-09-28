"""U11：worktree 并行开发——清单 + git 操作层。

对齐 v2（packages/core/src/worktree/）：
- 清单 (project, directory) 联合主键（sql.ts WorktreeTable）
- create 用 detached HEAD（git.ts:657）——不占分支名，并行安全
- 删除安全链：脏 worktree → force_required 显式错误（git.ts:645 判定正则），不静默丢改动
- list 以 `git worktree list --porcelain` 为单源真相，与清单合并元数据并对账
  （目录已消失的清单项自动 prune）
- 默认根目录 ~/.mycode/worktree/<project-hash[:6]>——全局数据区，不污染项目目录

边界（方案裁定）：不做自动 merge（交给 agent 用普通 git）；子代理继承父
workspace 不自动开 worktree；用户在前端显式选择。

叶子模块：零 agents 依赖，可被 project.py/router/测试直接导入。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorktreeError(Exception):
    """worktree 操作失败。force_required=True 表示脏目录需显式 force。"""

    def __init__(
        self,
        operation: str,
        directory: str | None = None,
        message: str = "",
        force_required: bool = False,
    ) -> None:
        super().__init__(message or f"worktree {operation} failed: {directory}")
        self.operation = operation
        self.directory = directory
        self.message = message
        self.force_required = force_required


@dataclass
class WorktreeEntry:
    """list_worktrees 返回项：git 真相 + 清单元数据合并结果。"""
    project: str
    directory: str
    kind: str               # root | linked
    head: str | None = None
    branch: str | None = None
    name: str | None = None       # 清单登记名（外部创建的 worktree 为 None）
    created_at: str | None = None
    managed: bool = False         # 是否由 MyCode 清单管理

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "directory": self.directory,
            "kind": self.kind,
            "head": self.head,
            "branch": self.branch,
            "name": self.name,
            "created_at": self.created_at,
            "managed": self.managed,
        }


# 脏 worktree 判定（v2 git.ts:645 同款正则）
_DIRTY_RE = re.compile(r"contains modified or untracked files|is dirty", re.IGNORECASE)


def _worktree_home() -> Path:
    override = os.environ.get("MYCODE_WORKTREE_HOME", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".mycode" / "worktree"


def _manifest_path() -> Path:
    override = os.environ.get("MYCODE_WORKTREE_MANIFEST", "").strip()
    if override:
        return Path(override)
    return _worktree_home() / "worktrees.json"


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.rename(path)


def _load_manifest() -> list[dict[str, Any]]:
    path = _manifest_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    entries = data.get("entries", [])
    return [e for e in entries if isinstance(e, dict)]


def _save_manifest(entries: list[dict[str, Any]]) -> None:
    _atomic_write_json(_manifest_path(), {"entries": entries})


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def project_worktree_root(project: str) -> Path:
    """项目的 worktree 默认根目录：全局数据区 + 项目路径哈希前 6 位（v2 project_id[:6] 同构）。"""
    digest = hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()[:6]
    return _worktree_home() / digest


def discover_common_git_dir(directory: str | Path) -> Path:
    """解析目录所属 git 仓库的 common dir（worktree 下 .git 是文件也能解析）。"""
    d = Path(directory)
    if not d.is_dir():
        raise WorktreeError("discover", str(d), f"not a directory: {d}")
    rc, out, err = _run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], d)
    if rc != 0:
        raise WorktreeError(
            "discover", str(d),
            f"not a git repository: {(err or out).strip()}",
        )
    return Path(out.strip())


def create_worktree(project: str, name: str | None = None, ref: str | None = None) -> WorktreeEntry:
    """在项目下创建 detached worktree 并登记清单。

    name 缺省 = wt-<时间戳>；ref 缺省 = HEAD。目录位于 project_worktree_root 下。
    """
    project_dir = Path(project).resolve()
    discover_common_git_dir(project_dir)  # 非 git 仓库 → WorktreeError
    wt_name = f"wt-{time.strftime('%Y%m%d-%H%M%S')}" if name is None else name.strip()
    if not wt_name or "/" in wt_name or wt_name in (".", ".."):
        raise WorktreeError("create", str(project), f"invalid worktree name: {name!r}")
    directory = project_worktree_root(str(project_dir)) / wt_name

    entries = _load_manifest()
    if any(e["directory"] == str(directory) for e in entries):
        raise WorktreeError("create", str(directory), f"worktree name already registered: {wt_name}")
    if directory.exists():
        raise WorktreeError("create", str(directory), f"directory already exists: {directory}")

    directory.parent.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run_git(
        ["worktree", "add", "--detach", "--", str(directory), ref or "HEAD"],
        project_dir,
    )
    if rc != 0:
        raise WorktreeError("create", str(directory), (err or out).strip() or "git worktree add failed")
    if not directory.is_dir():
        # v2 git.ts:664 同款防御：创建后无法打开
        raise WorktreeError("create", str(directory), "created worktree could not be opened")

    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entries.append({
        "project": str(project_dir),
        "directory": str(directory),
        "name": wt_name,
        "created_at": created_at,
    })
    _save_manifest(entries)
    return WorktreeEntry(
        project=str(project_dir), directory=str(directory), kind="linked",
        name=wt_name, created_at=created_at, managed=True,
    )


def remove_worktree(directory: str, force: bool = False) -> None:
    """删除 worktree（安全链：脏目录且未 force → WorktreeError(force_required=True)）。

    同时 prune 清单。git 命令从 common dir 执行（v2 git.ts:679 同构）。
    """
    d = Path(directory)
    common = discover_common_git_dir(d) if d.is_dir() else None
    if common is not None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args += ["--", str(d.resolve())]
        rc, out, err = _run_git(args, common)
        if rc != 0:
            message = (err or out).strip() or "git worktree remove failed"
            raise WorktreeError(
                "remove", str(d), message,
                force_required=bool(_DIRTY_RE.search(message)),
            )
    else:
        # 目录已被手动删除：对账 git 元数据（仅当清单能定位 owning project）
        owner = _manifest_project_for(str(d)) or _manifest_project_for(str(d.resolve()))
        if owner is not None and owner.is_dir():
            _run_git(["worktree", "prune"], owner)

    entries = [e for e in _load_manifest() if e["directory"] != str(d) and e["directory"] != str(d.resolve())]
    _save_manifest(entries)


def _manifest_project_for(directory: str) -> Path | None:
    for e in _load_manifest():
        if e["directory"] == directory:
            return Path(e["project"])
    return None


def list_worktrees(project: str) -> list[WorktreeEntry]:
    """列出项目全部 worktree（root + linked）。

    单源真相 = `git worktree list --porcelain`；清单只补 name/created_at 元数据；
    顺带对账：清单中目录已不在 git 列表的条目 prune 掉。
    """
    project_dir = Path(project).resolve()
    common = discover_common_git_dir(project_dir)
    rc, out, err = _run_git(["worktree", "list", "--porcelain"], common)
    if rc != 0:
        raise WorktreeError("list", str(project_dir), (err or out).strip() or "git worktree list failed")

    parsed: list[dict[str, str | None]] = []
    current: dict[str, str | None] = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current:
                parsed.append(current)
            current = {"directory": line[len("worktree "):].strip(), "head": None, "branch": None}
        elif line.startswith("HEAD ") and current:
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch ") and current:
            current["branch"] = line[len("branch "):].strip()
    if current:
        parsed.append(current)

    manifest = {e["directory"]: e for e in _load_manifest()}
    git_dirs = {Path(p["directory"] or "").resolve().__str__() for p in parsed}

    result: list[WorktreeEntry] = []
    for i, p in enumerate(parsed):
        d = Path(p["directory"] or "")
        key = d.resolve().__str__() if d.exists() else str(d)
        meta = manifest.get(key) or manifest.get(str(d))
        branch = (p["branch"] or "").removeprefix("refs/heads/") or None
        result.append(WorktreeEntry(
            project=str(project_dir),
            directory=key,
            kind="root" if i == 0 else "linked",
            head=p["head"],
            branch=branch,
            name=meta.get("name") if meta else None,
            created_at=meta.get("created_at") if meta else None,
            managed=meta is not None,
        ))

    # 对账：本项目清单条目已不在 git 真相中 → prune
    remaining = [
        e for e in _load_manifest()
        if not (e["project"] == str(project_dir) and e["directory"] not in git_dirs)
    ]
    if len(remaining) != len(manifest):
        _save_manifest(remaining)

    return result


def prune_project_worktrees(project: str, force: bool = False) -> list[str]:
    """项目删除级联：移除其全部受管 worktree，返回遗留目录（脏且未 force）。

    对齐 v2 WorktreeTable onDelete=cascade 语义：清单条目总是清除；
    git 目录删除尽力而为，脏目录遗留并如实返回（不静默吞）。
    """
    project_dir = str(Path(project).resolve())
    leftover: list[str] = []
    for e in _load_manifest():
        if e["project"] != project_dir:
            continue
        try:
            remove_worktree(e["directory"], force=force)
        except WorktreeError as exc:
            if exc.force_required or Path(e["directory"]).is_dir():
                leftover.append(e["directory"])
        # 清单条目兜底清除（remove_worktree 失败时）
        entries = [x for x in _load_manifest() if x["directory"] != e["directory"]]
        _save_manifest(entries)
    return leftover
