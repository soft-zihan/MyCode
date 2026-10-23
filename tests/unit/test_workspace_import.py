"""工作区导入（云端执行目标的落地那一步）：校验、克隆、幂等更新。

不 mock git，也不打外网：用 `python -m http.server` 提供 git 的 **dumb HTTP** 传输
（`git update-server-info` 生成 info/refs 之后，静态文件服务器就能当 git 远端）。
这样 `git clone http://127.0.0.1:<port>/demo.git` 走的是真的 http 传输层，
而 `validate_repo_url` 对 http(s) 的放行也是真的被测到——不是绕过去的。
"""
import functools
import http.server
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agents.core import workspace_import as wi

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _commit(cwd, msg="c"):
    _git(["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg], cwd)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):        # 否则每个请求都往 pytest 的输出里灌一行
        pass


@pytest.fixture(scope="module")
def http_repo(tmp_path_factory):
    """一个能真克隆的本地 http git 远端，返回它的 URL。

    module 级：这些测试只**读**这个远端，谁都不往上推。做成函数级的话，
    45 个测试要建 45 次仓库、跑 45×8 次 git——而仓库在 /mnt/d（9p 挂载）上，
    单次 git 调用就要几百毫秒，实测整个文件因此从 ~20s 涨到 146s。
    """
    tmp_path = tmp_path_factory.mktemp("http-repo")
    serve_root = tmp_path / "srv"
    serve_root.mkdir()
    bare = serve_root / "demo.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True, text=True)

    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "-b", "main"], work)
    (work / "README.md").write_text("# demo\n")
    _git(["add", "."], work)
    _commit(work, "init")
    _git(["branch", "feature-x"], work)
    _git(["remote", "add", "origin", str(bare)], work)
    pushed = _git(["push", "origin", "main", "feature-x"], work)
    assert pushed.returncode == 0, pushed.stderr
    _git(["update-server-info"], bare)

    handler = functools.partial(_Quiet, directory=str(serve_root))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/demo.git"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    """把导入根与 project 注册表都指到 tmp，别碰真的 ~/.mycode。"""
    # 清掉代理：本机常驻 HTTP(S)_PROXY，而下面的远端是 127.0.0.1，
    # 走代理就绕到 Windows 侧再回来，能不能通全看运气。
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "workspaces"
    monkeypatch.setenv(wi.WORKSPACES_ENV, str(root))
    monkeypatch.setenv("MYCODE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.delenv(wi.GIT_PROXY_ENV, raising=False)
    return root


class TestValidation:
    @pytest.mark.parametrize("url", [
        "https://github.com/soft-zihan/MyCode.git",
        "https://github.com/soft-zihan/MyCode",
        "http://gitea.internal/team/repo.git",
        "git@github.com:soft-zihan/MyCode.git",
    ])
    def test_accepts(self, url):
        assert wi.validate_repo_url(url) == url

    @pytest.mark.parametrize("url", [
        "",
        "   ",
        "-u",
        "--upload-pack=touch /tmp/pwned",
        "file:///etc/passwd",
        "/mnt/d/Projects/MyCode",
        "C:\\repos\\MyCode",
        "ext::sh -c touch% /tmp/pwned",
        "https:///no-host.git",
        "http://",
        "ssh://git@host/repo.git",
        "https://github.com/a/b.git\nrm -rf /",
    ])
    def test_rejects(self, url):
        with pytest.raises(wi.WorkspaceImportError):
            wi.validate_repo_url(url)

    def test_nothing_written_on_reject(self):
        for bad in ("--upload-pack=touch /tmp/wi-pwned", "ext::sh -c touch% /tmp/wi-pwned"):
            with pytest.raises(wi.WorkspaceImportError):
                wi.import_workspace(bad)
        assert not Path("/tmp/wi-pwned").exists()


class TestNaming:
    @pytest.mark.parametrize("url,name", [
        ("https://github.com/soft-zihan/MyCode.git", "MyCode"),
        ("https://github.com/soft-zihan/MyCode", "MyCode"),
        ("git@github.com:soft-zihan/MyCode.git", "MyCode"),
        ("https://host/group/sub/repo.git", "repo"),
    ])
    def test_derive_name(self, url, name):
        assert wi.derive_name(url) == name

    @pytest.mark.parametrize("raw,expect", [
        ("MyCode", "MyCode"),
        ("../../etc/passwd", "etc_passwd"),
        ("..", None),
        (".", None),
        ("a b/c", "a_b_c"),
    ])
    def test_sanitize_name(self, raw, expect):
        if expect is None:
            with pytest.raises(wi.WorkspaceImportError):
                wi.sanitize_name(raw)
        else:
            assert wi.sanitize_name(raw) == expect

    def test_sanitize_ref_rejects_option_like(self):
        with pytest.raises(wi.WorkspaceImportError):
            wi.sanitize_ref("--branch=evil")


class TestGitArgs:
    def test_no_proxy_by_default(self):
        assert wi.git_base_args(None) == ["git"]

    def test_proxy_goes_through_c_not_env(self):
        # `-c http.proxy=` 只作用于这一次调用；设 HTTPS_PROXY 会波及同进程所有 git，
        # 连 branches.py 的 fetch 一起改道。
        assert wi.git_base_args("http://192.168.64.1:7899") == [
            "git", "-c", "http.proxy=http://192.168.64.1:7899",
        ]


class TestImport:
    def test_clone_and_register(self, http_repo, ws_root):
        r = wi.import_workspace(http_repo)
        assert r.created is True
        assert Path(r.directory) == ws_root / "demo"
        assert (Path(r.directory) / "README.md").read_text() == "# demo\n"
        assert r.branch == "main" and r.head
        from agents.core.project import get_project
        assert get_project(r.directory) is not None

    def test_clone_specific_ref(self, http_repo, ws_root):
        r = wi.import_workspace(http_repo, ref="feature-x")
        assert r.branch == "feature-x"

    def test_unknown_ref_fails_and_leaves_no_half_dir(self, http_repo, ws_root):
        with pytest.raises(wi.WorkspaceImportError, match="克隆失败"):
            wi.import_workspace(http_repo, ref="no-such-branch")
        assert not (ws_root / "demo").exists()

    def test_second_import_updates_not_reclones(self, http_repo, ws_root):
        first = wi.import_workspace(http_repo)
        again = wi.import_workspace(http_repo)
        assert first.created is True and again.created is False
        assert again.directory == first.directory

    def test_same_name_different_remote_rejected(self, http_repo, ws_root):
        wi.import_workspace(http_repo)
        with pytest.raises(wi.WorkspaceImportError, match="另一个仓库"):
            wi.import_workspace("https://github.com/someone/else.git", name="demo")

    def test_existing_non_git_dir_rejected(self, http_repo, ws_root):
        (ws_root / "demo").mkdir(parents=True)
        (ws_root / "demo" / "keep.txt").write_text("mine")
        with pytest.raises(wi.WorkspaceImportError, match="不是 git 仓库"):
            wi.import_workspace(http_repo)
        assert (ws_root / "demo" / "keep.txt").read_text() == "mine"

    def test_update_refuses_when_dirty(self, http_repo, ws_root):
        r = wi.import_workspace(http_repo)
        (Path(r.directory) / "README.md").write_text("uncommitted")
        with pytest.raises(wi.WorkspaceImportError, match="未提交"):
            wi.import_workspace(http_repo, ref="feature-x")

    def test_update_switches_branch_when_clean(self, http_repo, ws_root):
        wi.import_workspace(http_repo)
        r = wi.import_workspace(http_repo, ref="feature-x")
        assert r.branch == "feature-x" and r.created is False

    def test_explicit_name(self, http_repo, ws_root):
        r = wi.import_workspace(http_repo, name="cloud-demo")
        assert r.name == "cloud-demo" and Path(r.directory).name == "cloud-demo"

    def test_local_path_rejected(self, ws_root, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _git(["init", "-b", "main"], src)
        with pytest.raises(wi.WorkspaceImportError):
            wi.import_workspace(str(src))

    def test_missing_git_reports_clearly(self, http_repo, ws_root, tmp_path, monkeypatch):
        # python:3.12-slim 不带 git。缺它时不能吐 traceback（HTTP 500），
        # 得说一句人能照着修的话——2026-10-24 云端就是这么失败的。
        empty = tmp_path / "emptybin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        with pytest.raises(wi.WorkspaceImportError, match="git 不可用"):
            wi.import_workspace(http_repo)

    def test_proxy_env_is_picked_up(self, http_repo, ws_root, monkeypatch):
        # 指向一个解析不出来的代理主机：克隆必须因此失败，这就证明代理真被 git 吃进去了。
        # 不用「没人监听的本地端口」——本机 WSL 连关闭的端口是超时不是拒绝，
        # 而 git 的 curl 默认 connect 超时是 300s，一条测试能挂五分钟。
        monkeypatch.setenv(wi.GIT_PROXY_ENV, "http://no-such-host.invalid:7899")
        with pytest.raises(wi.WorkspaceImportError, match="克隆失败"):
            wi.import_workspace(http_repo)


class TestListWorkspaces:
    def test_empty_root(self, ws_root):
        assert wi.list_workspaces() == []

    def test_lists_imported(self, http_repo, ws_root):
        wi.import_workspace(http_repo)
        (ws_root / "not-a-repo").mkdir()
        items = {w["name"]: w for w in wi.list_workspaces()}
        assert items["demo"]["git"] is True and items["demo"]["branch"] == "main"
        assert items["not-a-repo"]["git"] is False


class TestApi:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from main import app
        return TestClient(app)

    def test_import_endpoint(self, client, http_repo, ws_root):
        r = client.post("/api/workspace/import", json={"repo_url": http_repo})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] is True
        assert body["name"] == "demo"
        assert body["branch"] == "main"

    def test_import_endpoint_rejects_bad_url(self, client, ws_root):
        r = client.post("/api/workspace/import", json={"repo_url": "file:///etc/passwd"})
        assert r.status_code == 400

    def test_list_endpoint(self, client, http_repo, ws_root):
        client.post("/api/workspace/import", json={"repo_url": http_repo})
        r = client.get("/api/workspace/imports")
        assert r.status_code == 200
        assert [w["name"] for w in r.json()["workspaces"]] == ["demo"]
