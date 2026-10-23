"""Git 分支清单与切换。

叶子模块（零 agents 依赖），风格对齐 core/worktree.py：git 命令是唯一真相源，
失败抛 BranchError 带人话 message，由路由层转 400。

两条边界（显式裁定，不是漏做）：

- **脏工作树拒绝切换**，不替用户 stash。静默 stash 会让人以为改动没了，
  而 `git checkout` 自己能带着未提交改动切过去时更糟——改动跟着跑到另一个分支上，
  用户毫不知情。worktree.py 的 force_required 是同一条纪律。
- **只接受清单里已有的分支名**，不接受任意 rev（tag / sha / `HEAD~1`）。
  这样以 `-` 开头的值在我们这一层就被挡掉，而不是指望 git 自己报错——
  `git checkout --upload-pack=<cmd>` 这类参数注入不该由「git 恰好拒绝了」来兜底。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class BranchError(Exception):
    """分支操作失败。dirty=True 表示工作树有未提交改动，需用户自己处置。"""

    def __init__(
        self,
        operation: str,
        project: str | None = None,
        message: str = "",
        dirty: bool = False,
    ) -> None:
        super().__init__(message or f"branch {operation} failed: {project}")
        self.operation = operation
        self.project = project
        self.message = message
        self.dirty = dirty


@dataclass
class BranchRef:
    """一条分支。remote 项的 name 带远端前缀（`origin/foo`），与 `git branch -a` 一致。"""
    name: str
    kind: str                   # local | remote
    head: str | None = None     # 短 sha
    current: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "head": self.head, "current": self.current}


@dataclass
class BranchInfo:
    project: str
    git: bool                   # False = 不是 git 仓库（能力降级，不是错误）
    current: str | None = None
    detached: bool = False
    dirty: bool = False
    remote_url: str | None = None
    branches: list[BranchRef] = field(default_factory=list)
    fetch_error: str | None = None      # fetch=True 且失败时的原因（清单仍返回本地已知项）

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "git": self.git,
            "current": self.current,
            "detached": self.detached,
            "dirty": self.dirty,
            "remote_url": self.remote_url,
            "branches": [b.to_dict() for b in self.branches],
            "fetch_error": self.fetch_error,
        }


def _run_git(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str, str]:
    # GIT_TERMINAL_PROMPT=0：这段代码跑在 web 后端里，没有 TTY 可问密码。
    # 不设的话，一个要认证的私有远端会让 fetch 挂在那儿等输入，直到 timeout 才回来。
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        # git 不在 PATH 里。返回非零 rc 而不是让异常冒到路由层变成 500：
        # list_branches 会因此降级成 git=False（UI 直接隐藏入口），checkout 会抛
        # BranchError 带一句人话——两者都比一个 traceback 有用。
        return 127, "", "git 不可用（没装，或不在 PATH 里）"
    except subprocess.TimeoutExpired:
        # fetch 打到不可达的远端时会走满 timeout。同上，转成人话而不是 500。
        return 124, "", f"git {' '.join(args)} 超过 {timeout}s 未返回"
    return proc.returncode, proc.stdout, proc.stderr


def _is_git(project: Path) -> bool:
    if not project.is_dir():
        return False
    rc, _, _ = _run_git(["rev-parse", "--git-dir"], project)
    return rc == 0


def _remote_url(project: Path) -> str | None:
    """取 origin 的地址；没有 origin 就退回第一个远端；一个远端都没有返回 None。"""
    rc, out, _ = _run_git(["remote"], project)
    if rc != 0:
        return None
    remotes = [r for r in out.split() if r]
    if not remotes:
        return None
    name = "origin" if "origin" in remotes else remotes[0]
    rc, out, _ = _run_git(["remote", "get-url", name], project)
    return out.strip() or None if rc == 0 else None


def list_branches(project: str | Path, fetch: bool = False) -> BranchInfo:
    """列本地 + 远端分支。非 git 目录降级为 git=False 空清单（合法状态而非错误）。

    fetch=True 时先 `git fetch --prune --all`：分支切换的典型场景是「别人/CI 刚推了
    一个新分支」，不 fetch 就永远看不到它。fetch 失败不致命——本地已知的照返回，
    原因放 fetch_error 里让 UI 说得清（要认证？没网？），而不是静默给一份旧清单。
    """
    p = Path(project)
    if not _is_git(p):
        return BranchInfo(project=str(p), git=False)

    fetch_error: str | None = None
    if fetch:
        rc, out, err = _run_git(["fetch", "--prune", "--all"], p, timeout=60)
        if rc != 0:
            fetch_error = (err or out).strip() or f"git fetch 退出码 {rc}"

    rc, out, err = _run_git(
        ["for-each-ref", "--format=%(refname)%09%(objectname:short)%09%(HEAD)",
         "refs/heads", "refs/remotes"],
        p,
    )
    if rc != 0:
        raise BranchError("list", str(p), (err or out).strip())

    branches: list[BranchRef] = []
    current: str | None = None
    for line in out.splitlines():
        parts = line.split("\t")
        refname = parts[0] if parts else ""
        sha = parts[1] if len(parts) > 1 else ""
        star = parts[2] if len(parts) > 2 else ""
        if refname.startswith("refs/heads/"):
            name, kind = refname[len("refs/heads/"):], "local"
        elif refname.startswith("refs/remotes/"):
            remote, _, bname = refname[len("refs/remotes/"):].partition("/")
            # refs/remotes/origin/HEAD 是符号引用不是分支，列出来 UI 会多一个假选项
            if not bname or bname == "HEAD":
                continue
            name, kind = f"{remote}/{bname}", "remote"
        else:
            continue
        is_current = star == "*" and kind == "local"
        branches.append(BranchRef(name=name, kind=kind, head=sha or None, current=is_current))
        if is_current:
            current = name

    detached = False
    if current is None:
        rc, _, _ = _run_git(["symbolic-ref", "-q", "HEAD"], p)
        detached = rc != 0

    _, status, _ = _run_git(["status", "--porcelain", "--untracked-files=no"], p)

    return BranchInfo(
        project=str(p),
        git=True,
        current=current,
        detached=detached,
        dirty=bool(status.strip()),
        remote_url=_remote_url(p),
        branches=branches,
        fetch_error=fetch_error,
    )


def _resolve_ref(p: Path, ref: str, info: BranchInfo) -> str:
    """把用户给的 ref 校验并归一化成 `git checkout` 能直接吃的本地分支名。"""
    if not isinstance(ref, str) or not ref.strip():
        raise BranchError("checkout", str(p), "分支名为空")
    name = ref.strip()
    if name.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in name):
        raise BranchError("checkout", str(p), f"非法分支名：{name!r}")

    locals_ = {b.name for b in info.branches if b.kind == "local"}
    remotes = {b.name for b in info.branches if b.kind == "remote"}
    if name in locals_:
        return name
    if name in remotes:
        # 远端项要剥掉 `origin/` 前缀再交给 git：`git checkout origin/foo` 会得到
        # 一个 detached HEAD，而 `git checkout foo` 才会建出跟踪 origin/foo 的本地分支。
        stripped = name.partition("/")[2]
        return stripped if stripped and stripped not in locals_ else name
    # 只给远端名的短形式也要认（`feature-x` 而本地只有 `origin/feature-x`）：
    # 「把某个分支推到云端」传过来的就是源项目的分支名，不带 origin/ 前缀。
    # 唯一匹配才放行——两个远端都有同名分支时 git 自己也会拒，这里提前说清楚。
    matches = [r for r in remotes if r.partition("/")[2] == name]
    if len(matches) == 1:
        return name
    if len(matches) > 1:
        raise BranchError(
            "checkout", str(p),
            f"多个远端都有分支 {name}（{'、'.join(sorted(matches))}），请写全名",
        )
    raise BranchError("checkout", str(p), f"没有这个分支：{name}")


def checkout(project: str | Path, ref: str) -> BranchInfo:
    """切到 ref 指向的分支，返回切换后的清单。失败抛 BranchError。"""
    p = Path(project)
    if not p.is_dir():
        raise BranchError("checkout", str(p), f"目录不存在：{p}")
    if not _is_git(p):
        raise BranchError("checkout", str(p), f"不是 git 仓库：{p}")

    info = list_branches(str(p))
    if info.dirty:
        raise BranchError(
            "checkout", str(p),
            "工作树有未提交的改动，切分支会把它们带到另一个分支上或直接失败。"
            "先提交或 git stash，再切。",
            dirty=True,
        )

    name = _resolve_ref(p, ref, info)
    rc, out, err = _run_git(["checkout", name], p)
    if rc != 0:
        raise BranchError("checkout", str(p), (err or out).strip() or f"git checkout {name} 失败")
    return list_branches(str(p))
