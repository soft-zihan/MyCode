"""U11 worktree：清单 + git 操作层 + API 路由 + 快照兼容（真 git 仓库 fixture）。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agents.core import worktree as wt

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture
def git_project(tmp_path, monkeypatch):
    home = tmp_path / "wt-home"
    monkeypatch.setenv("MYCODE_WORKTREE_HOME", str(home))
    monkeypatch.setenv("MYCODE_WORKTREE_MANIFEST", str(home / "worktrees.json"))
    proj = (tmp_path / "proj").resolve()
    proj.mkdir()
    _git(["init", "-b", "main"], proj)
    (proj / "f.txt").write_text("hello")
    _git(["add", "."], proj)
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init"], proj)
    return proj


def _manifest_entries():
    path = Path(wt._manifest_path())
    if not path.exists():
        return []
    return json.loads(path.read_text())["entries"]


class TestCreate:
    def test_create_detached_under_global_root(self, git_project):
        entry = wt.create_worktree(str(git_project), name="feature-x")
        d = Path(entry.directory)
        assert d.is_dir()
        assert d.parent == wt.project_worktree_root(str(git_project))
        # detached HEAD：symbolic-ref 失败
        assert _git(["symbolic-ref", "-q", "HEAD"], d).returncode != 0
        # 内容与 HEAD 一致
        assert (d / "f.txt").read_text() == "hello"
        assert entry.kind == "linked" and entry.managed and entry.name == "feature-x"
        assert [e["name"] for e in _manifest_entries()] == ["feature-x"]

    def test_create_default_name_unique(self, git_project):
        e1 = wt.create_worktree(str(git_project))
        assert e1.name.startswith("wt-")

    def test_create_name_collision_rejected(self, git_project):
        wt.create_worktree(str(git_project), name="dup")
        with pytest.raises(wt.WorktreeError, match="already registered"):
            wt.create_worktree(str(git_project), name="dup")

    def test_create_invalid_name_rejected(self, git_project):
        for bad in ["a/b", ".", "", "  "]:
            with pytest.raises(wt.WorktreeError, match="invalid worktree name"):
                wt.create_worktree(str(git_project), name=bad)

    def test_create_non_git_project_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MYCODE_WORKTREE_HOME", str(tmp_path / "wt"))
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(wt.WorktreeError, match="not a git repository"):
            wt.create_worktree(str(plain))

    def test_create_at_ref(self, git_project):
        head = _git(["rev-parse", "HEAD"], git_project).stdout.strip()
        (git_project / "g.txt").write_text("second")
        _git(["add", "."], git_project)
        _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "second"], git_project)
        entry = wt.create_worktree(str(git_project), name="at-old", ref=head)
        d = Path(entry.directory)
        assert not (d / "g.txt").exists()
        assert _git(["rev-parse", "HEAD"], d).stdout.strip() == head


class TestList:
    def test_list_root_and_linked_with_metadata(self, git_project):
        wt.create_worktree(str(git_project), name="wt-a")
        entries = wt.list_worktrees(str(git_project))
        kinds = [e.kind for e in entries]
        assert kinds == ["root", "linked"]
        linked = entries[1]
        assert linked.name == "wt-a" and linked.managed and created_at_ok(linked.created_at)
        assert entries[0].directory == str(git_project)
        assert entries[0].branch == "main" and not entries[0].managed

    def test_list_from_worktree_dir_sees_same_repo(self, git_project):
        entry = wt.create_worktree(str(git_project), name="wt-b")
        from_wt = wt.list_worktrees(entry.directory)
        assert {e.directory for e in from_wt} == {e.directory for e in wt.list_worktrees(str(git_project))}

    def test_reconcile_prunes_vanished_manifest_entry(self, git_project):
        entry = wt.create_worktree(str(git_project), name="wt-gone")
        subprocess.run(["rm", "-rf", entry.directory], check=True)
        _git(["worktree", "prune"], git_project)
        entries = wt.list_worktrees(str(git_project))
        assert all(e.name != "wt-gone" for e in entries)
        assert _manifest_entries() == []


class TestRemove:
    def test_remove_clean(self, git_project):
        entry = wt.create_worktree(str(git_project), name="wt-clean")
        wt.remove_worktree(entry.directory)
        assert not Path(entry.directory).exists()
        assert _manifest_entries() == []
        assert len(wt.list_worktrees(str(git_project))) == 1

    def test_remove_dirty_requires_force(self, git_project):
        entry = wt.create_worktree(str(git_project), name="wt-dirty")
        (Path(entry.directory) / "uncommitted.txt").write_text("dirty")
        with pytest.raises(wt.WorktreeError) as exc_info:
            wt.remove_worktree(entry.directory)
        assert exc_info.value.force_required is True
        assert Path(entry.directory).exists()          # 不静默丢改动
        assert len(_manifest_entries()) == 1
        wt.remove_worktree(entry.directory, force=True)
        assert not Path(entry.directory).exists()
        assert _manifest_entries() == []

    def test_remove_manually_deleted_dir_prunes_manifest(self, git_project):
        entry = wt.create_worktree(str(git_project), name="wt-manual")
        subprocess.run(["rm", "-rf", entry.directory], check=True)
        wt.remove_worktree(entry.directory)            # 不抛错，对账清单
        assert _manifest_entries() == []

    def test_remove_foreign_dir_not_git(self, tmp_path, monkeypatch, git_project):
        monkeypatch.setenv("MYCODE_WORKTREE_HOME", str(tmp_path / "wt"))
        stray = tmp_path / "stray"
        stray.mkdir()
        with pytest.raises(wt.WorktreeError, match="not a git repository"):
            wt.remove_worktree(str(stray))


class TestCascade:
    def test_prune_project_worktrees(self, git_project):
        wt.create_worktree(str(git_project), name="c1")
        dirty = wt.create_worktree(str(git_project), name="c2")
        (Path(dirty.directory) / "x.txt").write_text("x")
        leftover = wt.prune_project_worktrees(str(git_project))
        assert leftover == [dirty.directory]
        assert _manifest_entries() == []              # 清单级联清空（v2 onDelete=cascade 语义）
        assert Path(dirty.directory).exists()          # 脏目录如实遗留

    def test_delete_project_cascades(self, git_project, tmp_path, monkeypatch):
        monkeypatch.setenv("MYCODE_PROJECTS_DIR", str(tmp_path / "projects-store"))
        from agents.core.project import register_project, delete_project
        register_project(str(git_project), name="p")
        entry = wt.create_worktree(str(git_project), name="p-wt")
        assert delete_project(str(git_project)) is True
        assert _manifest_entries() == []
        assert not Path(entry.directory).exists()


class TestSnapshotInWorktree:
    async def test_shadow_repo_alternates_resolve_main_objects(self, git_project, tmp_path):
        from agents.core.git_repository import GitRepositoryManager
        entry = wt.create_worktree(str(git_project), name="wt-snap")
        snapshot_dir = tmp_path / "snapshots"
        repo = GitRepositoryManager(entry.directory, snapshot_dir)
        await repo.ensure_repository()
        alternates = (repo.repo_path / ".git" / "objects" / "info" / "alternates").read_text().strip()
        # worktree 下 .git 是文件 → alternates 必须解析到主仓 objects（common dir）
        assert alternates == str(git_project / ".git" / "objects")
        assert Path(alternates).is_dir()
        tree = await repo.capture_tree()
        files = await repo.list_tree_files(tree)
        assert "f.txt" in files


@pytest.fixture
def api():
    from fastapi.testclient import TestClient
    from main import app
    yield TestClient(app)


class TestApi:
    def test_list_root_only(self, api, git_project):
        r = api.get(f"/api/projects/{git_project}/worktrees")
        assert r.status_code == 200
        body = r.json()
        assert body["git"] is True
        entries = body["worktrees"]
        assert len(entries) == 1 and entries[0]["kind"] == "root"

    def test_create_and_list(self, api, git_project):
        r = api.post(f"/api/projects/{git_project}/worktrees", json={"name": "api-wt"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "linked" and body["managed"] and body["name"] == "api-wt"
        entries = api.get(f"/api/projects/{git_project}/worktrees").json()["worktrees"]
        assert [e["kind"] for e in entries] == ["root", "linked"]

    def test_create_invalid_name_400(self, api, git_project):
        r = api.post(f"/api/projects/{git_project}/worktrees", json={"name": "a/b"})
        assert r.status_code == 400

    def test_list_non_git_degrades_to_git_false(self, api, tmp_path):
        plain = tmp_path / "plain-api"
        plain.mkdir()
        r = api.get(f"/api/projects/{plain}/worktrees")
        assert r.status_code == 200
        assert r.json() == {"worktrees": [], "git": False}

    def test_create_non_git_400(self, api, tmp_path):
        plain = tmp_path / "plain-create"
        plain.mkdir()
        r = api.post(f"/api/projects/{plain}/worktrees", json={})
        assert r.status_code == 400

    def test_remove_clean(self, api, git_project):
        entry = api.post(f"/api/projects/{git_project}/worktrees", json={"name": "api-rm"}).json()
        r = api.delete(f"/api/worktrees/{entry['directory']}")
        assert r.status_code == 200 and r.json()["success"] is True
        assert not Path(entry["directory"]).exists()

    def test_remove_dirty_409_force_required(self, api, git_project):
        entry = api.post(f"/api/projects/{git_project}/worktrees", json={"name": "api-dirty"}).json()
        (Path(entry["directory"]) / "dirty.txt").write_text("x")
        r = api.delete(f"/api/worktrees/{entry['directory']}")
        assert r.status_code == 409
        assert r.json()["detail"]["force_required"] is True
        assert Path(entry["directory"]).exists()
        r2 = api.delete(f"/api/worktrees/{entry['directory']}", params={"force": "true"})
        assert r2.status_code == 200
        assert not Path(entry["directory"]).exists()


def created_at_ok(value: str | None) -> bool:
    return bool(value) and value.endswith("Z")
