"""Git 凭据与结果落地。**唯一接触 PAT 的模块。**

PAT 明文 0600 落 PVC 根（`~/.mycode/cloud_git_token`）。沙箱挂的是 subPath，
够不到 PVC 根，所以 token 不进沙箱文件系统；Job 的 env 里也没有任何 token 字段。
刻意不引 cryptography 做 Fernet：主密钥本身也要放在同一个 Pod 能读的 Secret 里，
对「能读 PVC 的人」这个真实威胁几乎不加防护，真正挡住沙箱的是 subPath 隔离。
诚实边界：这是明文静态存储，多租户（C 批）必须换 per-user 加密 + OAuth。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from agents.core import state_root
from agents.core.workspace_import import git_base_args, git_proxy

from .task_store import CloudTask

GIT_TIMEOUT = 120
PUSH_TIMEOUT = 300
API_TIMEOUT = 30


class GitBrokerError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def token_path() -> Path:
    return state_root.cloud_git_token_path()


def write_token(token: str) -> None:
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    value = token.strip()
    if not value:
        p.unlink(missing_ok=True)
        return
    # 先建空文件再 chmod 再写：避免「内容已落盘但权限还是 0644」那个窗口
    p.touch(mode=0o600, exist_ok=True)
    p.chmod(0o600)
    p.write_text(value, encoding="utf-8")


def read_token() -> str | None:
    p = token_path()
    if not p.is_file():
        return None
    return p.read_text(encoding="utf-8").strip() or None


def token_configured() -> bool:
    return read_token() is not None


def assert_no_token_leak(workspace: Path, token: str) -> None:
    """扫 .git 下的文本文件，命中 token 子串即抛。这是安全承重墙。"""
    git_dir = workspace / ".git"
    if not git_dir.exists():
        return
    for p in git_dir.rglob("*"):
        if not p.is_file() or p.stat().st_size > 1_000_000:
            continue
        try:
            if token in p.read_text(encoding="utf-8", errors="ignore"):
                raise GitBrokerError(f"安全断言失败：{p.relative_to(workspace)} 里含 token，已中止落地")
        except OSError:
            continue


def repo_slug(url: str) -> str:
    """GitHub 的 owner/repo。A 批只支持 GitHub——PR API 是 GitHub 形状的。"""
    m = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?/?", url.strip())
    if not m:
        raise GitBrokerError(f"A 批只支持 GitHub 仓库，收到：{url}")
    return f"{m.group(1)}/{m.group(2)}"


def _git(args: list[str], cwd: Path, timeout: int, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """list 形式不经 shell：仓库名、分支名、prompt 都来自外部，拼字符串就是注入。"""
    full = git_base_args(git_proxy()) + args
    run_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    try:
        proc = subprocess.run(
            full, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=run_env,
        )
    except FileNotFoundError:
        return 127, "", "git 不可用（没装，或不在 PATH 里）"
    except subprocess.TimeoutExpired:
        return 124, "", f"git 超过 {timeout}s 未返回（远端不可达？代理没配？）"
    return proc.returncode, proc.stdout, proc.stderr


def _fail(step: str, out: str, err: str, token: str) -> GitBrokerError:
    detail = (err or out).strip()[:300] or "无输出"
    # 错误串会进 task.json 再上前端，token 万一走进 stderr 也不能跟着出去
    return GitBrokerError(f"{step}失败：{detail.replace(token, '***')}")


def _push(workspace: Path, branch: str, token: str) -> None:
    """GIT_ASKPASS 一次性注入：token 只在子进程 env 里，不进 remote URL / .git/config。"""
    fd, tmp = tempfile.mkstemp(prefix="mycode-askpass-", suffix=".sh")
    os.close(fd)
    script = Path(tmp)
    try:
        script.chmod(0o700)
        script.write_text('#!/bin/sh\necho "$MYCODE_GIT_TOKEN"\n', encoding="utf-8")
        rc, out, err = _git(
            ["push", "-u", "origin", branch], workspace, PUSH_TIMEOUT,
            env={"MYCODE_GIT_TOKEN": token, "GIT_ASKPASS": str(script)},
        )
        if rc != 0:
            raise _fail("push", out, err, token)
    finally:
        script.unlink(missing_ok=True)


def _open_pr(slug: str, branch: str, task: CloudTask, token: str) -> dict[str, Any]:
    title = task.prompt.strip().splitlines()[0][:72] if task.prompt.strip() else f"MyCode 任务 {task.id}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    base = task.ref or "main"
    try:
        r = httpx.post(
            f"https://api.github.com/repos/{slug}/pulls",
            headers=headers,
            json={"title": title, "head": branch, "base": base, "body": f"MyCode 云端任务 {task.id}"},
            timeout=API_TIMEOUT, trust_env=False, proxy=git_proxy(),
        )
    except httpx.HTTPError as e:
        raise GitBrokerError(f"开 PR 请求失败：{e!r}") from e
    if r.status_code in (401, 403):
        raise GitBrokerError(f"GitHub 拒绝凭据（{r.status_code}），检查 PAT 的 repo scope")
    if r.status_code == 422 and "pull request already exists" in r.text:
        return _existing_pr(slug, branch, headers)
    if r.status_code >= 400:
        raise GitBrokerError(f"开 PR 失败（{r.status_code}）：{r.text[:300]}")
    return r.json()


def _existing_pr(slug: str, branch: str, headers: dict[str, str]) -> dict[str, Any]:
    owner = slug.split("/")[0]
    r = httpx.get(
        f"https://api.github.com/repos/{slug}/pulls",
        headers=headers, params={"head": f"{owner}:{branch}", "state": "open"},
        timeout=API_TIMEOUT, trust_env=False, proxy=git_proxy(),
    )
    items = r.json() if r.status_code < 400 else []
    if not items:
        raise GitBrokerError("GitHub 说 PR 已存在，但按分支查不到，请手动检查仓库的 PR 列表")
    return items[0]


def land_task(task: CloudTask, workspace: Path) -> dict[str, Any]:
    """把沙箱改出来的工作区变成一个 PR。每步失败都抛人话 GitBrokerError。"""
    token = read_token()
    if token is None:
        raise GitBrokerError("未配置 GitHub PAT，无法落地（先跑 mycode cloud token <PAT>）")
    assert_no_token_leak(workspace, token)

    rc, out, err = _git(["status", "--porcelain"], workspace, GIT_TIMEOUT)
    if rc != 0:
        raise _fail("查改动", out, err, token)
    if not out.strip():
        return {"branch": "", "pr_url": "", "mergeable": None, "no_changes": True}

    branch = f"mycode/{task.id}"
    rc, out, err = _git(["checkout", "-b", branch], workspace, GIT_TIMEOUT)
    if rc != 0:
        raise _fail("建分支", out, err, token)
    rc, out, err = _git(["add", "-A"], workspace, GIT_TIMEOUT)
    if rc != 0:
        raise _fail("暂存改动", out, err, token)
    title = task.prompt.strip().splitlines()[0][:72] if task.prompt.strip() else f"MyCode 任务 {task.id}"
    rc, out, err = _git(
        ["-c", "user.name=MyCode", "-c", "user.email=mycode@localhost", "commit", "-m", title],
        workspace, GIT_TIMEOUT,
    )
    if rc != 0:
        raise _fail("提交", out, err, token)

    _push(workspace, branch, token)
    assert_no_token_leak(workspace, token)

    pr = _open_pr(repo_slug(task.repo_url), branch, task, token)
    return {
        "branch": branch,
        "pr_url": pr.get("html_url", ""),
        "mergeable": pr.get("mergeable"),
        "no_changes": False,
    }