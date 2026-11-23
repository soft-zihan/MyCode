"""全局状态根解析：MYCODE_HOME 与既有分项 env 旋钮的统一入口。

**为什么要有这个模块**：存算分离要的是「状态不落容器里」，不是「容器里少放代码」。
要做到前者，全部全局态必须能整体指向一个挂载点（PVC / 共享文件系统），
而不是每类状态各自一个 env——此前有 7 个分项旋钮、35 处直接写死
`Path.home() / ".mycode" / ...` 的调用点，重定向根本做不干净。

**只管全局态。** 工作区级的 `<workspace>/.mycode/`（plans / wiki / todos / rules /
artifacts / 项目级 skills 与 settings）跟着仓库走，本来就该在仓库里，
不受 MYCODE_HOME 影响，**别往这个模块里加**。

**优先级：分项 env > MYCODE_HOME/<子目录> > 历史默认。**
分项 env 必须优先：它们已经写在 deploy manifests 里了（如 MYCODE_CONFIG_PATH
指向 K8s Secret 挂载的只读路径），MYCODE_HOME 不能把它盖掉。

**历史默认有两个根**，这是既成事实：数据区 `~/.mycode`，配置区 `~/.my-code`。
MYCODE_HOME 把两者收成一个；不设它时各自的默认保持原样，一个字节都不动。

**不缓存。** 每次调用实时读 env——运行时重定向契约必须对整个进程生命周期有效
（见 session_backend_jsonl.py:21）。rewind_service.py 曾把 SNAPSHOT_DIR 做成模块级
常量并当默认参数用，import 时就冻结了，那是反例。本模块禁止出现路径常量。
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "MYCODE_HOME"


def _env(name: str) -> str | None:
    """读 env 并把空白当没设。

    空白必须回退默认：否则一个 `MYCODE_HOME=""` 会把状态指到进程 CWD，
    与 config.py 里既有的同一条契约一致。
    """
    value = os.environ.get(name, "").strip()
    return value or None


def mycode_home() -> Path | None:
    """全局状态根。未设置返回 None，调用方回退到各自的历史默认。"""
    override = _env(HOME_ENV)
    return Path(override) if override else None


def _resolve(sub: str, *, env: str | None = None, legacy_root: str = ".mycode") -> Path:
    """按「分项 env > MYCODE_HOME/<sub> > ~/<legacy_root>/<sub>」解析一个路径。

    sub 可以是目录名也可以是文件名（config.json / settings.json），这里只做拼接。
    """
    if env:
        specific = _env(env)
        if specific:
            return Path(specific)
    root = mycode_home()
    if root is not None:
        return root / sub
    return Path.home() / legacy_root / sub


# ── 数据区（历史默认 ~/.mycode/）─────────────────────────────────────────


def session_dir() -> Path:
    """会话事件流。后端切换见 MYCODE_SESSION_BACKEND（session.py）。"""
    return _resolve("sessions", env="MYCODE_SESSION_DIR")


def workspaces_dir() -> Path:
    """导入的工作区根。Pod 里这里是 PVC 挂载点，代码跨 Pod 重建存活。"""
    return _resolve("workspaces", env="MYCODE_WORKSPACES_DIR")


def worktree_home() -> Path:
    """worktree 根。全局数据区，不污染项目目录。"""
    return _resolve("worktree", env="MYCODE_WORKTREE_HOME")


def worktree_manifest_path() -> Path:
    """worktree 清单。默认是 worktree_home() 的兄弟文件，不是一级子目录，
    所以重定向 worktree_home 之后它要跟着走。"""
    specific = _env("MYCODE_WORKTREE_MANIFEST")
    if specific:
        return Path(specific)
    return worktree_home() / "worktrees.json"


def snapshots_dir() -> Path:
    """git 影子仓库快照。回退（rewind）与压缩前 diff 都依赖它。"""
    return _resolve("snapshots")


def db_dir() -> Path:
    """观测用的 sqlite 目录（bad_cases.db）。"""
    return _resolve("db")


def agents_dir() -> Path:
    """用户级自定义 agent 定义（<name>.md）。"""
    return _resolve("agents")


def skills_dir() -> Path:
    """用户级 skills（<name>/SKILL.md）。优先级高于项目级。"""
    return _resolve("skills")


def settings_path() -> Path:
    """全局 settings.json（权限规则、MCP 全局配置）。"""
    return _resolve("settings.json")


def embed_cache_dir() -> Path:
    """embedding 缓存。缓存键是内容 sha256、按模型分名，跨工作区共享安全。"""
    return _resolve("embed-cache")


def logs_dir() -> Path:
    """前端上报日志的落盘目录（frontend.log）。"""
    return _resolve("logs")


def tool_results_dir() -> Path:
    """超大工具结果的溢出落盘目录（>30KB 的结果只给模型预览，全文写这里）。"""
    return _resolve("tool-results", legacy_root=".my-code")


def skill_venv_dir() -> Path:
    """可执行 skill 的隔离 venv。"""
    return _resolve("skill-venv", env="MYCODE_SKILL_VENV", legacy_root=".my-code")


def cloud_tasks_dir() -> Path:
    """云端任务记录。每个任务目录同时是沙箱 Job 的 subPath 挂载源。"""
    return _resolve("cloud_tasks")


def cloud_git_token_path() -> Path:
    """Git PAT。在状态根而不是 cloud_tasks_dir() 里：沙箱只挂得到子目录，够不到根。"""
    return _resolve("cloud_git_token")


def disabled_mcp_path() -> Path:
    """被停用的 MCP server 名单。

    历史上多套了一层 `config/`，这里保持原样：改名要迁移旧文件而收益为零。
    """
    return _resolve("config/disabled_mcp_servers.json")


# ── 配置区（历史默认 ~/.my-code/，带横线）────────────────────────────────


def config_path() -> Path:
    """主配置文件。容器里从 K8s Secret 挂到只读路径，MYCODE_CONFIG_PATH 指定。"""
    return _resolve("config.json", env="MYCODE_CONFIG_PATH", legacy_root=".my-code")


def projects_dir() -> Path:
    """项目注册表（projects.json）。"""
    return _resolve("projects", env="MYCODE_PROJECTS_DIR", legacy_root=".my-code")
