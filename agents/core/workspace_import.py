"""把远端仓库导入本机工作区根——「云端执行目标」落地的那一步。

为什么这段代码在 core 而不是在某个「云端专用」模块里：**同一份代码、两种部署形态**。
控制面镜像与本地跑的是同一个后端，所以 `POST /api/workspace/import` 打到云端实例时，
克隆就发生在 Pod 里、落在 PVC 上（`~/.mycode/workspaces`）。这样「把项目推到云端」
既不需要宿主装 kubectl，也不需要往 Pod 里拷文件——和 Codex cloud 一样，
云端环境自己去远端仓库拉代码。

安全边界：repo_url 与 name 都是**用户输入**，而 repo_url 最终会进 git 的命令行。

- 一律 list 形式 subprocess，不经 shell；
- 显式拒绝 `-` 开头：`git clone --upload-pack=<cmd>` 能执行任意命令，这类值必须在
  我们这一层就挡掉，不能指望「git 恰好会拒绝」；
- 只放行 `http(s)://host/...` 与 `git@host:path` 两类。`file://` 能读宿主上任意
  仓库（在云端形态里就是读 Pod 里的东西），`ext::` 直接是命令执行通道，都拒；
- name 过滤成 `[A-Za-z0-9._-]` 并去掉首尾的点，杜绝 `../` 越出工作区根。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .branches import BranchError, checkout as checkout_branch
from .project import register_project

WORKSPACES_ENV = "MYCODE_WORKSPACES_DIR"
GIT_PROXY_ENV = "MYCODE_GIT_PROXY"

# 克隆一个大仓库可能要几分钟，而这是同步 HTTP 请求里干的活。
# 给足预算但一定要有上限：没有超时的子进程会把一个 web worker 永久占住。
CLONE_TIMEOUT = 600
FETCH_TIMEOUT = 120

_SCP_LIKE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^\s]+$")


class WorkspaceImportError(Exception):
    """导入失败。message 是可以直接给用户看的人话。"""

    def __init__(self, message: str, operation: str = "import") -> None:
        super().__init__(message)
        self.message = message
        self.operation = operation


@dataclass
class ImportedWorkspace:
    directory: str
    name: str
    repo_url: str
    ref: str | None
    head: str | None
    branch: str | None
    created: bool               # True = 新克隆；False = 已存在，做了 fetch + 切分支

    def to_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "name": self.name,
            "repo_url": self.repo_url,
            "ref": self.ref,
            "head": self.head,
            "branch": self.branch,
            "created": self.created,
        }


def workspace_root() -> Path:
    """导入的工作区根。

    默认 `~/.mycode/workspaces`，与 worktree 的 `~/.mycode/worktree/` 同属全局数据区。
    在 Pod 里 `~/.mycode` 正是 PVC 的挂载点（deploy/base/deployment.yaml），
    所以导入的代码跨 Pod 重建存活；`~/.my-code`（带横线，配置区）是 emptyDir，不能用。
    """
    override = os.environ.get(WORKSPACES_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / ".mycode" / "workspaces"


def git_proxy() -> str | None:
    """git 专用的代理地址（不是全局 HTTPS_PROXY）。

    Pod 直连 github.com:443 会超时，必须经宿主代理转发口出去
    （deploy/README.md §1.6 的四条对照实测）。但模型端点是直连可达的，
    设全局代理会把所有出站都改道、平白多一个故障面，所以只给 git 用。
    """
    return os.environ.get(GIT_PROXY_ENV, "").strip() or None


def validate_repo_url(url: str) -> str:
    """校验并原样返回可用的仓库地址；不合法抛 WorkspaceImportError。"""
    if not isinstance(url, str):
        raise WorkspaceImportError("仓库地址必须是字符串")
    u = url.strip()
    if not u:
        raise WorkspaceImportError("仓库地址为空")
    if u.startswith("-"):
        raise WorkspaceImportError(f"仓库地址不能以 - 开头（会被 git 当成选项）：{u!r}")
    if any(ch.isspace() or ord(ch) < 32 for ch in u):
        raise WorkspaceImportError(f"仓库地址含空白或控制字符：{u!r}")
    low = u.lower()
    if "::" in low:
        raise WorkspaceImportError(f"不支持 ext:: 这类传输助手（等于命令执行）：{u!r}")
    if low.startswith("file://") or low.startswith("/") or re.match(r"^[a-z]:[\\/]", low):
        raise WorkspaceImportError(f"不支持本地路径仓库：{u!r}")
    if low.startswith(("http://", "https://")):
        rest = u.split("://", 1)[1]
        # host 必须非空且不以 / 开头，否则 http:///x 这种畸形值会进到 git 里。
        # 不要求 host 里有点：`http://gitea.internal/x.git` 这类内网主机名是合法的。
        if not rest or rest.startswith("/") or not rest.split("/")[0]:
            raise WorkspaceImportError(f"仓库地址缺少主机名：{u!r}")
        return u
    if _SCP_LIKE.match(u):
        return u
    raise WorkspaceImportError(
        f"只支持 http(s)://host/path 或 git@host:path 两种形式，收到：{u!r}"
    )


def derive_name(repo_url: str) -> str:
    """从仓库地址推默认目录名：`.../MyCode.git` → `MyCode`。"""
    tail = re.split(r"[:/]", repo_url.rstrip("/"))[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return sanitize_name(tail or "workspace")


def sanitize_name(name: str) -> str:
    """把目录名过滤成安全字符集。越出工作区根的可能性在这一步被消掉。"""
    if not isinstance(name, str) or not name.strip():
        raise WorkspaceImportError("工作区名为空")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    # 去掉首尾的点：`..` → 空、`.git` → `git`，都不该成为目录名
    cleaned = cleaned.strip("._")
    if not cleaned or cleaned in {".", ".."}:
        raise WorkspaceImportError(f"非法工作区名：{name!r}")
    return cleaned[:64]


def git_base_args(proxy: str | None = None) -> list[str]:
    """git 命令前缀。proxy 用 `-c http.proxy=` 而不是环境变量：
    环境变量会波及同进程里所有 git 调用（包括 branches.py 的 fetch），
    `-c` 只作用于这一次。git 的 `http.*` 命名空间对 https:// 同样生效。
    """
    args = ["git"]
    if proxy:
        args += ["-c", f"http.proxy={proxy}"]
    return args


def _run(args: list[str], cwd: Path | None = None, timeout: int = FETCH_TIMEOUT) -> tuple[int, str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _head_info(directory: Path) -> tuple[str | None, str | None]:
    _, head, _ = _run(["git", "rev-parse", "--short", "HEAD"], directory)
    rc, branch, _ = _run(["git", "symbolic-ref", "--short", "-q", "HEAD"], directory)
    return (head.strip() or None), (branch.strip() or None if rc == 0 else None)


def list_workspaces() -> list[dict[str, Any]]:
    """工作区根下已有的导入项（目录名 + 是否 git 仓库）。"""
    root = workspace_root()
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        is_git = (child / ".git").exists()
        head, branch = _head_info(child) if is_git else (None, None)
        out.append({"name": child.name, "directory": str(child),
                    "git": is_git, "head": head, "branch": branch})
    return out


def import_workspace(
    repo_url: str,
    ref: str | None = None,
    name: str | None = None,
    proxy: str | None = None,
) -> ImportedWorkspace:
    """把 repo_url 克隆到工作区根并注册为 project；已存在则 fetch + 切到 ref。

    proxy 缺省取 `MYCODE_GIT_PROXY`（集群里由 ConfigMap mycode-net 注入）。
    """
    url = validate_repo_url(repo_url)
    ws_name = sanitize_name(name) if name else derive_name(url)
    root = workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / ws_name
    use_proxy = proxy if proxy is not None else git_proxy()

    if target.exists():
        return _update_existing(target, url, ref, ws_name, use_proxy)

    args = git_base_args(use_proxy) + ["clone"]
    if ref:
        # --branch 只认分支名与 tag，不认 sha。要按 sha 检出得先克隆再 checkout，
        # 而「切到某个分支」是这个功能唯一的需求，所以这里不做那条路。
        args += ["--branch", sanitize_ref(ref)]
    args += ["--", url, str(target)]
    rc, out, err = _run(args, timeout=CLONE_TIMEOUT)
    if rc != 0:
        # 克隆失败会留下半个目录，git 自己通常会清掉；没清掉的话下次进来会被
        # 当成「已存在但不是 git 仓库」而报错，所以这里显式兜一次。
        if target.exists() and not (target / ".git").exists():
            _rmtree(target)
        raise WorkspaceImportError(
            f"克隆失败：{(err or out).strip() or f'git clone 退出码 {rc}'}", operation="clone"
        )

    register_project(str(target), ws_name)
    head, branch = _head_info(target)
    return ImportedWorkspace(
        directory=str(target), name=ws_name, repo_url=url, ref=ref,
        head=head, branch=branch, created=True,
    )


def sanitize_ref(ref: str) -> str:
    """分支/tag 名的最小校验。与 branches._resolve_ref 同一纪律：
    不接受 `-` 开头，不接受空白与控制字符。"""
    if not isinstance(ref, str) or not ref.strip():
        raise WorkspaceImportError("ref 为空")
    r = ref.strip()
    if r.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in r):
        raise WorkspaceImportError(f"非法 ref：{r!r}")
    return r


def _update_existing(
    target: Path, url: str, ref: str | None, ws_name: str, proxy: str | None
) -> ImportedWorkspace:
    """目录已存在：同一个远端就更新，不同远端就报错（绝不覆盖别人的东西）。"""
    if not target.is_dir():
        raise WorkspaceImportError(f"{target} 已存在且不是目录，换个名字")
    if not (target / ".git").exists():
        raise WorkspaceImportError(f"{target} 已存在且不是 git 仓库，换个名字或先删掉它")

    rc, out, _ = _run(["git", "remote", "get-url", "origin"], target)
    existing = out.strip() if rc == 0 else ""
    if existing and existing.rstrip("/") != url.rstrip("/"):
        raise WorkspaceImportError(
            f"工作区 {ws_name} 已经指向另一个仓库（{existing}），换个名字"
        )

    args = git_base_args(proxy) + ["fetch", "--prune", "origin"]
    rc, out, err = _run(args, target, timeout=CLONE_TIMEOUT)
    if rc != 0:
        raise WorkspaceImportError(
            f"fetch 失败：{(err or out).strip() or f'退出码 {rc}'}", operation="fetch"
        )

    if ref:
        try:
            # 复用 branches.checkout：脏工作树保护、远端分支 DWIM、ref 白名单都在那儿，
            # 这里再写一遍就是两份会走偏的实现。
            checkout_branch(str(target), ref)
        except BranchError as e:
            raise WorkspaceImportError(e.message, operation="checkout") from e

    register_project(str(target), ws_name)
    head, branch = _head_info(target)
    return ImportedWorkspace(
        directory=str(target), name=ws_name, repo_url=url, ref=ref,
        head=head, branch=branch, created=False,
    )


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
