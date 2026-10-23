"""Git 分支清单与切换：core 层 + API 路由（真 git 仓库 fixture）。

与 test_worktree.py 同一纪律：不 mock git，用 tmp_path 里的真仓库跑。
分支这件事的正确性全在 git 的行为里，mock 掉了就等于什么都没测。
"""
import subprocess
import sys
from pathlib import Path

import pytest

from agents.core import branches as br

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _commit(cwd, msg="c"):
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg], cwd)


@pytest.fixture
def git_project(tmp_path):
    """proj 克隆自 origin：本地有 main / feature-a，origin 上另有 only-remote。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(["init", "-b", "main"], origin)
    (origin / "f.txt").write_text("hello")
    _git(["add", "."], origin)
    _commit(origin, "init")
    _git(["branch", "only-remote"], origin)

    proj = tmp_path / "proj"
    subprocess.run(
        ["git", "clone", str(origin), str(proj)],
        capture_output=True, text=True, check=True,
    )
    _git(["branch", "feature-a"], proj)
    return proj.resolve()


def _names(info):
    return {(b.kind, b.name) for b in info.branches}


class TestList:
    def test_non_git_degrades_not_errors(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        info = br.list_branches(str(d))
        assert info.git is False
        assert info.branches == []
        assert info.current is None

    def test_local_and_remote_branches_listed(self, git_project):
        info = br.list_branches(str(git_project))
        assert info.git is True
        assert ("local", "main") in _names(info)
        assert ("local", "feature-a") in _names(info)
        assert ("remote", "origin/only-remote") in _names(info)

    def test_current_marked_exactly_once(self, git_project):
        info = br.list_branches(str(git_project))
        assert info.current == "main"
        assert [b.name for b in info.branches if b.current] == ["main"]

    def test_remote_head_symref_not_listed_as_branch(self, git_project):
        # refs/remotes/origin/HEAD 是个符号引用，列出来会让 UI 多出一个假分支
        info = br.list_branches(str(git_project))
        assert ("remote", "origin/HEAD") not in _names(info)
        assert all(not b.name.endswith("/HEAD") for b in info.branches)

    def test_remote_url_reported(self, git_project):
        info = br.list_branches(str(git_project))
        assert info.remote_url and info.remote_url.endswith("origin")

    def test_dirty_flag_ignores_untracked(self, git_project):
        assert br.list_branches(str(git_project)).dirty is False
        (git_project / "new.txt").write_text("x")
        assert br.list_branches(str(git_project)).dirty is False
        (git_project / "f.txt").write_text("changed")
        assert br.list_branches(str(git_project)).dirty is True

    def test_fetch_picks_up_branch_created_after_clone(self, git_project):
        origin = git_project.parent / "origin"
        _git(["branch", "made-later"], origin)
        assert ("remote", "origin/made-later") not in _names(br.list_branches(str(git_project)))
        info = br.list_branches(str(git_project), fetch=True)
        assert ("remote", "origin/made-later") in _names(info)
        assert info.fetch_error is None

    def test_fetch_failure_degrades_with_reason(self, git_project, monkeypatch):
        # 断网/要认证时不能让接口 500：本地已知的分支照返回，原因放 fetch_error
        _git(["remote", "set-url", "origin", "https://127.0.0.1:1/nope.git"], git_project)
        info = br.list_branches(str(git_project), fetch=True)
        assert info.git is True and info.branches
        assert info.fetch_error

    def test_missing_git_degrades_not_500(self, git_project, tmp_path, monkeypatch):
        # 镜像里没装 git 时（python:3.12-slim 就不带），异常冒到路由层会变成 500，
        # 前端只看到一个没有理由的红。降级成 git=False 让 UI 直接隐藏入口。
        empty = tmp_path / "emptybin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        info = br.list_branches(str(git_project))
        assert info.git is False and info.branches == []
        with pytest.raises(br.BranchError):
            br.checkout(str(git_project), "main")

    def test_detached_head_has_no_current_branch(self, git_project):
        head = _git(["rev-parse", "HEAD"], git_project).stdout.strip()
        _git(["checkout", "-q", head], git_project)
        info = br.list_branches(str(git_project))
        assert info.current is None
        assert info.detached is True


class TestCheckout:
    def test_switch_to_existing_local_branch(self, git_project):
        info = br.checkout(str(git_project), "feature-a")
        assert info.current == "feature-a"
        assert _git(["symbolic-ref", "--short", "HEAD"], git_project).stdout.strip() == "feature-a"

    def test_switch_to_remote_only_branch_creates_tracking(self, git_project):
        info = br.checkout(str(git_project), "origin/only-remote")
        assert info.current == "only-remote"
        assert ("local", "only-remote") in _names(info)
        up = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], git_project)
        assert up.stdout.strip() == "origin/only-remote"

    def test_unknown_ref_rejected(self, git_project):
        with pytest.raises(br.BranchError, match="no-such-branch"):
            br.checkout(str(git_project), "no-such-branch")

    def test_dash_prefixed_ref_rejected(self, git_project):
        # 不以「git 自己会报错」为满足：这类值必须在我们这一层就挡掉，
        # 否则哪天参数拼装顺序变了，`-` 开头的值就会被 git 当成选项吃掉。
        for bad in ("--help", "-b", "--upload-pack=touch /tmp/pwned"):
            with pytest.raises(br.BranchError):
                br.checkout(str(git_project), bad)
        assert not Path("/tmp/pwned").exists()

    def test_dirty_tree_refuses_and_keeps_branch(self, git_project):
        (git_project / "f.txt").write_text("uncommitted")
        with pytest.raises(br.BranchError) as ei:
            br.checkout(str(git_project), "feature-a")
        assert ei.value.dirty is True
        assert _git(["symbolic-ref", "--short", "HEAD"], git_project).stdout.strip() == "main"

    def test_non_git_rejected(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        with pytest.raises(br.BranchError):
            br.checkout(str(d), "main")

    def test_missing_dir_rejected(self, tmp_path):
        with pytest.raises(br.BranchError):
            br.checkout(str(tmp_path / "nope"), "main")


class TestApi:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from main import app
        return TestClient(app)

    def test_list_endpoint(self, client, git_project):
        r = client.get(f"/api/projects/{git_project}/branches")
        assert r.status_code == 200
        body = r.json()
        assert body["git"] is True
        assert body["current"] == "main"
        assert any(b["name"] == "feature-a" and b["kind"] == "local" for b in body["branches"])

    def test_non_git_endpoint_degrades(self, client, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        r = client.get(f"/api/projects/{d}/branches")
        assert r.status_code == 200
        assert r.json() == {"project": str(d), "git": False, "current": None,
                            "detached": False, "dirty": False, "remote_url": None,
                            "branches": [], "fetch_error": None}

    def test_checkout_endpoint(self, client, git_project):
        r = client.post(f"/api/projects/{git_project}/checkout", json={"ref": "feature-a"})
        assert r.status_code == 200
        assert r.json()["current"] == "feature-a"

    def test_checkout_dirty_is_400_with_reason(self, client, git_project):
        (git_project / "f.txt").write_text("uncommitted")
        r = client.post(f"/api/projects/{git_project}/checkout", json={"ref": "feature-a"})
        assert r.status_code == 400
        assert r.json()["dirty"] is True
        assert "未提交" in r.json()["detail"]

    def test_checkout_unknown_is_400(self, client, git_project):
        r = client.post(f"/api/projects/{git_project}/checkout", json={"ref": "nope"})
        assert r.status_code == 400
