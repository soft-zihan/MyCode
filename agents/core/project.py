"""Project 管理模块。

Project 是 session 的容器，每个 project 对应一个工作目录（cwd）。
一个 project 下可以有多个 session。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field
import time

from .worktree import prune_project_worktrees


@dataclass
class Project:
    """Project 数据类。"""
    cwd: str
    name: str | None = None
    created_at: str = ""
    updated_at: str = ""
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not self.updated_at:
            self.updated_at = self.created_at
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "name": self.name,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Project:
        return cls(
            cwd=data["cwd"],
            name=data.get("name"),
            created_at=data.get("createdAt", ""),
            updated_at=data.get("updatedAt", ""),
        )


def _projects_dir() -> Path:
    """Project 存储目录。"""
    override = os.environ.get("MYCODE_PROJECTS_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".my-code" / "projects"


def _projects_file() -> Path:
    """Project 列表文件路径。"""
    return _projects_dir() / "projects.json"


def _ensure_dir() -> None:
    _projects_dir().mkdir(parents=True, exist_ok=True)


def _load_projects() -> dict[str, Project]:
    """加载所有 project。"""
    path = _projects_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {cwd: Project.from_dict(p) for cwd, p in data.items()}
    except Exception:
        return {}


def _save_projects(projects: dict[str, Project]) -> None:
    """保存所有 project。"""
    _ensure_dir()
    path = _projects_file()
    data = {cwd: p.to_dict() for cwd, p in projects.items()}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_projects() -> list[Project]:
    """列出所有 project。"""
    projects = _load_projects()
    return sorted(projects.values(), key=lambda p: p.updated_at, reverse=True)


def get_project(cwd: str) -> Project | None:
    """获取指定 project。"""
    projects = _load_projects()
    return projects.get(cwd)


def register_project(cwd: str, name: str | None = None) -> Project:
    """注册 project（如果已存在则更新 updated_at）。"""
    projects = _load_projects()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    
    if cwd in projects:
        projects[cwd].updated_at = now
        if name:
            projects[cwd].name = name
    else:
        projects[cwd] = Project(cwd=cwd, name=name, created_at=now, updated_at=now)
    
    _save_projects(projects)
    return projects[cwd]


def update_project(cwd: str, name: str | None = None) -> Project | None:
    """更新 project 信息。"""
    projects = _load_projects()
    if cwd not in projects:
        return None
    
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    projects[cwd].updated_at = now
    if name is not None:
        projects[cwd].name = name
    
    _save_projects(projects)
    return projects[cwd]


def delete_project(cwd: str) -> bool:
    """删除 project。

    U11：级联清理该项目的受管 worktree（v2 WorktreeTable onDelete=cascade 语义）——
    清单条目总是清除；git 目录删除尽力而为，脏目录遗留并如实打印（不静默吞）。
    """
    projects = _load_projects()
    if cwd not in projects:
        return False

    leftover = prune_project_worktrees(cwd)
    if leftover:
        print(f"[project] worktree 级联：脏目录遗留（需手动清理或 force）: {leftover}")

    del projects[cwd]
    _save_projects(projects)
    return True
