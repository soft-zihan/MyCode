"""MYCODE_HOME：全局状态根的统一解析。

存算分离要的是「状态不落容器里」，而不是「容器里少放代码」。要做到前者，
所有全局态必须能整体重定向到一个挂载点，而不是每类状态各自一个 env。

本文件锁两件事：
1. **不设 MYCODE_HOME 时行为逐字节不变**——42 个解析点（7 个既有分项旋钮 +
   35 处写死的 `Path.home() / ".mycode"`）分布在 8 个包里，任何一处默认值漂移
   都会让本机与既有部署的表现改变。
2. **设了以后全部收进来**——包括历史上分裂的两个根（`~/.mycode` 数据区 /
   `~/.my-code` 配置区）。这个分裂是既成事实，MYCODE_HOME 把它收成一个。

优先级：分项 env > MYCODE_HOME/<子目录> > 历史默认。
分项 env 必须优先，因为它们已经写在 deploy manifests 里（MYCODE_CONFIG_PATH
指向 K8s Secret 挂载的只读路径），MYCODE_HOME 不能把它盖掉。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from agents.core import state_root

ALL_ENV_KNOBS = (
    "MYCODE_HOME",
    "MYCODE_SESSION_DIR",
    "MYCODE_CONFIG_PATH",
    "MYCODE_PROJECTS_DIR",
    "MYCODE_WORKSPACES_DIR",
    "MYCODE_WORKTREE_HOME",
    "MYCODE_WORKTREE_MANIFEST",
    "MYCODE_SKILL_VENV",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每条用例都从「一个旋钮都没设」开始，否则上一条的 setenv 会漏进来。"""
    for name in ALL_ENV_KNOBS:
        monkeypatch.delenv(name, raising=False)


# ── 1. 不设 MYCODE_HOME：默认值逐条锁死 ────────────────────────────────


def test_legacy_defaults_when_home_unset():
    """历史默认。注意配置区是 `.my-code`（带横线），数据区是 `.mycode`。

    这两个根是不同时期加的，一直没统一。这里先把现状钉住，
    下面的 test_mycode_home_collapses_both_roots 才说明「收成一个」收的是什么。
    """
    home = Path.home()

    assert state_root.session_dir() == home / ".mycode" / "sessions"
    assert state_root.workspaces_dir() == home / ".mycode" / "workspaces"
    assert state_root.worktree_home() == home / ".mycode" / "worktree"
    assert state_root.worktree_manifest_path() == home / ".mycode" / "worktree" / "worktrees.json"
    assert state_root.snapshots_dir() == home / ".mycode" / "snapshots"
    assert state_root.db_dir() == home / ".mycode" / "db"
    assert state_root.agents_dir() == home / ".mycode" / "agents"
    assert state_root.skills_dir() == home / ".mycode" / "skills"
    assert state_root.settings_path() == home / ".mycode" / "settings.json"
    assert state_root.embed_cache_dir() == home / ".mycode" / "embed-cache"
    assert state_root.logs_dir() == home / ".mycode" / "logs"
    assert state_root.disabled_mcp_path() == (
        home / ".mycode" / "config" / "disabled_mcp_servers.json"
    )

    assert state_root.config_path() == home / ".my-code" / "config.json"
    assert state_root.projects_dir() == home / ".my-code" / "projects"
    assert state_root.tool_results_dir() == home / ".my-code" / "tool-results"
    assert state_root.skill_venv_dir() == home / ".my-code" / "skill-venv"


# ── 2. 设了 MYCODE_HOME：全部收进来 ─────────────────────────────────────


def test_mycode_home_collapses_both_roots(tmp_path, monkeypatch):
    """一个变量收全部，包括原来分裂在 `.my-code` 下的配置区。"""
    root = tmp_path / "state"
    monkeypatch.setenv("MYCODE_HOME", str(root))

    assert state_root.session_dir() == root / "sessions"
    assert state_root.workspaces_dir() == root / "workspaces"
    assert state_root.worktree_home() == root / "worktree"
    assert state_root.worktree_manifest_path() == root / "worktree" / "worktrees.json"
    assert state_root.snapshots_dir() == root / "snapshots"
    assert state_root.db_dir() == root / "db"
    assert state_root.agents_dir() == root / "agents"
    assert state_root.skills_dir() == root / "skills"
    assert state_root.settings_path() == root / "settings.json"
    assert state_root.embed_cache_dir() == root / "embed-cache"
    assert state_root.logs_dir() == root / "logs"
    # 历史上多套了一层 config/，收进根之后保留这层，不迁移旧文件
    assert state_root.disabled_mcp_path() == root / "config" / "disabled_mcp_servers.json"

    # 这两条是重点：原来在 ~/.my-code 下，现在也在同一个根里
    assert state_root.config_path() == root / "config.json"
    assert state_root.projects_dir() == root / "projects"
    assert state_root.tool_results_dir() == root / "tool-results"
    assert state_root.skill_venv_dir() == root / "skill-venv"

    # 且确实全部在根下——没有一条漏出去
    for p in (
        state_root.session_dir(),
        state_root.workspaces_dir(),
        state_root.worktree_home(),
        state_root.snapshots_dir(),
        state_root.db_dir(),
        state_root.agents_dir(),
        state_root.skills_dir(),
        state_root.settings_path(),
        state_root.embed_cache_dir(),
        state_root.logs_dir(),
        state_root.disabled_mcp_path(),
        state_root.config_path(),
        state_root.projects_dir(),
        state_root.tool_results_dir(),
        state_root.skill_venv_dir(),
    ):
        assert root in p.parents, f"{p} 逃出了 MYCODE_HOME"


def test_mycode_home_trailing_slash_is_normalized(tmp_path, monkeypatch):
    """挂载点常写成 `/data/mycode/`，不能拼出 `//snapshots`。"""
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path) + "/")

    assert state_root.snapshots_dir() == tmp_path / "snapshots"


# ── 3. 分项 env 优先于 MYCODE_HOME ──────────────────────────────────────


def test_specific_env_wins_over_mycode_home(tmp_path, monkeypatch):
    """K8s 里 config.json 从 Secret 挂到只读路径，MYCODE_HOME 不许盖掉它。"""
    monkeypatch.setenv("MYCODE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MYCODE_CONFIG_PATH", "/etc/mycode/config.json")
    monkeypatch.setenv("MYCODE_SESSION_DIR", str(tmp_path / "elsewhere" / "sess"))
    monkeypatch.setenv("MYCODE_SKILL_VENV", str(tmp_path / "venv"))

    assert state_root.config_path() == Path("/etc/mycode/config.json")
    assert state_root.session_dir() == tmp_path / "elsewhere" / "sess"
    assert state_root.skill_venv_dir() == tmp_path / "venv"
    # 没被分项 env 覆盖的仍然收进根
    assert state_root.snapshots_dir() == tmp_path / "state" / "snapshots"


def test_worktree_manifest_derives_from_worktree_home(tmp_path, monkeypatch):
    """manifest 的默认位置是 worktree_home 的兄弟文件，不是根下的一级目录。

    所以重定向 worktree_home 之后 manifest 要跟着走，除非它自己被显式指定。
    """
    monkeypatch.setenv("MYCODE_WORKTREE_HOME", str(tmp_path / "wt"))
    assert state_root.worktree_manifest_path() == tmp_path / "wt" / "worktrees.json"

    monkeypatch.setenv("MYCODE_WORKTREE_MANIFEST", str(tmp_path / "m.json"))
    assert state_root.worktree_manifest_path() == tmp_path / "m.json"


# ── 4. 空白值回退（沿用 config.py 已有的契约）──────────────────────────


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_blank_env_falls_back_to_default(blank, monkeypatch):
    """空 env 必须回退默认，否则一个空值会把状态指到进程 CWD。

    这条契约在 config.py:23 的注释里已经写明过，MYCODE_HOME 同样适用。
    """
    monkeypatch.setenv("MYCODE_HOME", blank)
    monkeypatch.setenv("MYCODE_SESSION_DIR", blank)

    assert state_root.snapshots_dir() == Path.home() / ".mycode" / "snapshots"
    assert state_root.session_dir() == Path.home() / ".mycode" / "sessions"


# ── 5. 运行时重定向必须整个进程生命周期有效 ───────────────────────────


def test_env_change_after_import_takes_effect(tmp_path, monkeypatch):
    """不许在 import 期或首次调用后缓存。

    session_backend_jsonl.py:21 已经把这条写成契约；rewind_service.py 曾把
    SNAPSHOT_DIR 做成模块级常量并当默认参数（import 时求值），那是反例。
    """
    assert state_root.mycode_home() is None
    assert state_root.snapshots_dir() == Path.home() / ".mycode" / "snapshots"

    first = tmp_path / "one"
    monkeypatch.setenv("MYCODE_HOME", str(first))
    assert state_root.snapshots_dir() == first / "snapshots"

    second = tmp_path / "two"
    monkeypatch.setenv("MYCODE_HOME", str(second))
    assert state_root.snapshots_dir() == second / "snapshots"


def test_no_module_level_path_constants():
    """防复发：state_root.py 里不许出现 import 期求值的路径常量。

    模块级 `X = Path.home() / ...` 会在 import 时冻结，之后 setenv 无效——
    rewind_service.py 的 SNAPSHOT_DIR 与 routers/mcp.py 的 _config_path 都是
    这么坏的。这条扫的是源码而不是行为，因为行为在上一条用例已经覆盖了；
    这里拦的是「将来有人再加一个常量」。
    """
    src = Path(state_root.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in src.splitlines()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*(:[^=]+)?=", line) and "Path.home()" in line
    ]
    assert not offenders, f"import 期求值的路径常量：{offenders}"


# ── 6. 全仓扫描：不许再出现绕过 state_root 的全局状态路径 ────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
# 只扫 Python 代码实际所在的两个目录。不扫 frontend/ 整个：那底下有 node_modules，
# rglob 虽然不返回 .py 却仍要遍历几万个目录，在 WSL 挂载的 /mnt/d 上实测 19s。
SCAN_DIRS = ("agents", "frontend/server")
# state_root.py 是唯一允许写出这些默认路径的地方——它就是解析层本身。
SCAN_EXCLUDE = {str(REPO_ROOT / "agents" / "core" / "state_root.py")}
PRUNE_DIRS = {"__pycache__", "node_modules", ".venv", ".git", "dist", ".pytest_cache"}

_HARDCODED_HOME = re.compile(r"""Path\.home\(\)\s*/\s*["']\.my-?code["']""")


def test_no_hardcoded_global_state_paths_in_source():
    """35 处写死的全局路径收口之后，这条守住「不再长出第 36 处」。

    为什么值得用源码扫描而不是逐个行为测试：漏一处的后果是**分裂**——
    设了 MYCODE_HOME 之后内核读一个目录、API 层读另一个，两边都「正常工作」，
    只是数据对不上。这类缺陷没有哪个单点行为测试能抓到，只能全局扫。

    只管 `Path.home() / ".mycode"`（全局态）。工作区级的
    `get_workspace() / ".mycode"` 跟着仓库走，本来就该在仓库里，不在此列。
    """
    offenders: list[str] = []
    for sub in SCAN_DIRS:
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT / sub):
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                py = Path(dirpath) / name
                if str(py) in SCAN_EXCLUDE:
                    continue
                for lineno, line in enumerate(
                    py.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if _HARDCODED_HOME.search(line):
                        offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "这些调用点绕过了 agents.core.state_root，MYCODE_HOME 对它们无效：\n"
        + "\n".join(offenders)
    )


# ── 7. 代表性调用点确实走了解析层 ───────────────────────────────────────


def test_rewind_service_default_follows_mycode_home(tmp_path, monkeypatch):
    """回退服务的默认快照目录。

    这是最阴的一处：原来 `SNAPSHOT_DIR` 既是模块级常量又当默认参数，
    两处都在 import 时求值，而 get_rewind_service() 还是单例——
    等于路径被钉死在第一次 import 那一刻，进程起来后 setenv 完全无效。
    """
    from agents.core.rewind_service import RewindService

    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    # 构造发生在 setenv 之后，但断言的是「用时解析」：再改一次 env 仍要跟着走
    svc = RewindService()
    assert svc.snapshot_dir == tmp_path / "snapshots"

    other = tmp_path / "moved"
    monkeypatch.setenv("MYCODE_HOME", str(other))
    assert svc.snapshot_dir == other / "snapshots", "snapshot_dir 被构造期冻结了"

    # 显式传参仍然优先（tests/test_rewind.py 全靠这条）
    explicit = tmp_path / "explicit"
    assert RewindService(snapshot_dir=explicit).snapshot_dir == explicit


def test_bad_cases_db_follows_mycode_home(tmp_path, monkeypatch):
    from agents.observability.bad_cases import _db_path

    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    assert _db_path() == tmp_path / "db" / "bad_cases.db"


def test_skill_venv_follows_mycode_home(tmp_path, monkeypatch):
    """MYCODE_SKILL_VENV 是既有旋钮，收进根之后它必须仍然优先。"""
    from agents.skills.executable_skills import get_kernel_venv_path

    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    assert get_kernel_venv_path() == tmp_path / "skill-venv"

    monkeypatch.setenv("MYCODE_SKILL_VENV", str(tmp_path / "own-venv"))
    assert get_kernel_venv_path() == tmp_path / "own-venv"


def test_large_tool_result_spills_under_mycode_home(tmp_path, monkeypatch):
    """>30KB 的工具结果溢出落盘。只读根文件系统下这条路径必须可重定向。"""
    from agents.tools.executor import persist_large_result

    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    big = "x" * (31 * 1024)
    preview = persist_large_result("run_shell", big)

    spill_dir = tmp_path / "tool-results"
    written = list(spill_dir.glob("*-run_shell.txt"))
    assert len(written) == 1, f"溢出文件没落在 MYCODE_HOME 下：{preview[:200]}"
    assert written[0].read_text(encoding="utf-8") == big


def test_user_skills_dir_follows_mycode_home(tmp_path, monkeypatch):
    """用户级 skills 目录。项目级 `<workspace>/.mycode/skills` 不受影响。"""
    from agents.skills import skill_file_ops

    monkeypatch.setenv("MYCODE_HOME", str(tmp_path))
    assert skill_file_ops._skills_root("user") == tmp_path / "skills"

