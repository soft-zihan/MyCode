"""端侧发版资产（安装包清单 + release-client.yml）的承重断言。

这两样东西改坏了都不会有任何测试变红，只会在发版那天炸：清单漏一条路径 =
用户装上就 ImportError；workflow 的触发条件写宽 = 每次 push 都发一个 Release。
参照 tests/unit/test_deploy_assets.py 的做法把它们钉住。
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOWS = ROOT / ".github" / "workflows"
BUNDLER = SCRIPTS / "make-client-bundle.sh"

# 只该在源码仓库里出现、绝不能进安装包的东西。
SOURCE_ONLY = ("tests", "eval", "mindmap", "docs", "deploy", "frontend/src", "rl", "infra")


def _bundle_paths() -> list[str]:
    """从 make-client-bundle.sh 里取出 BUNDLE_PATHS 数组（清单的唯一来源）。"""
    text = BUNDLER.read_text(encoding="utf-8")
    m = re.search(r"^BUNDLE_PATHS=\(\n(.*?)^\)\n", text, re.S | re.M)
    assert m, "make-client-bundle.sh 里找不到 BUNDLE_PATHS 数组——解析方式要跟着改了"
    return [
        ln.strip()
        for ln in m.group(1).splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _workflow(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.is_file(), f"缺少 {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dockerfile_runtime_copies() -> list[str]:
    """运行层（最后一个 FROM 之后）里 COPY 进镜像的仓库路径。"""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stage = text[text.rindex("\nFROM ") + 1:]
    sources = []
    for line in stage.splitlines():
        parts = line.strip().split()
        if not parts or parts[0] != "COPY":
            continue
        # --from= 的是上一阶段的构建产物（frontend/dist），不在仓库里
        if any(p.startswith("--from=") for p in parts):
            continue
        sources.append(parts[1].rstrip("/"))
    return sources


# ── 安装包清单 ─────────────────────────────────────────────────────

def test_bundle_manifest_covers_every_dockerfile_runtime_path():
    """端与云是同一个 commit 的两种产物，运行期需要的东西必须一致。

    Dockerfile 的运行层就是「云端跑起来需要什么」的权威清单（它是被
    verify/deploy-probe.sh 的 11 条断言实证过的）。安装包少了其中任何一条，
    症状是「仓库里跑得通、装上就 ImportError」——只在客户端暴露，最难查。
    """
    manifest = set(_bundle_paths())
    image = _dockerfile_runtime_copies()
    assert image, "没从 Dockerfile 里解析出任何 COPY，解析方式要跟着改了"
    missing = sorted(p for p in image if p not in manifest)
    assert not missing, f"安装包清单缺了镜像运行层有的路径：{missing}"


def test_bundle_manifest_ships_prebuilt_frontend():
    """客户端不装 node，所以前端产物必须在包里（`mycode prod` 直接托管它）。"""
    assert "frontend/dist" in _bundle_paths()


def test_bundle_manifest_excludes_source_only_dirs():
    """安装包是运行期产物，不是仓库快照：源码、测试、评测、设计文档都不该进去。"""
    manifest = _bundle_paths()
    leaked = [
        f"{p} -> {bad}"
        for p in manifest
        for bad in SOURCE_ONLY
        if p == bad or p.startswith(bad + "/")
    ]
    assert not leaked, f"安装包里有只属于源码仓库的东西：{leaked}"


def test_bundle_manifest_paths_all_exist():
    """就是 CI 里那条 --check，在本地也能跑。"""
    proc = subprocess.run(
        ["bash", str(BUNDLER), "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"--check 失败：\n{proc.stdout}\n{proc.stderr}"


def test_check_actually_fails_when_manifest_rots(tmp_path):
    """证明上面那条不是空转：把脚本单独放进一个空仓库，--check 必须报错并点名缺什么。

    没有这条，「清单校验通过」和「校验根本没在跑」长得一模一样。
    """
    (tmp_path / "scripts").mkdir()
    script = tmp_path / "scripts" / "make-client-bundle.sh"
    script.write_text(BUNDLER.read_text(encoding="utf-8"), encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(script), "--check"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0, "空仓库里 --check 竟然通过了，这条门禁是假的"
    assert "agents" in proc.stderr, f"没有点名缺失路径：{proc.stderr}"


def test_bundler_rejects_missing_dist_with_build_hint():
    """dist 是构建产物、不在 Git 里，所以 --check 跳过它，而真打包必须明确报错。
    否则用户看到的会是 tar 那句 "Cannot stat: No such file or directory"。"""
    text = BUNDLER.read_text(encoding="utf-8")
    assert "npm run build" in text, "缺 dist 时的提示里应给出构建命令"
    assert re.search(r'BUILD_ARTIFACT="frontend/dist"', text)


# ── release-client.yml ─────────────────────────────────────────────

def test_client_release_triggers_only_on_version_tags():
    """触发条件写宽（比如加上 branches）的后果是每次 push 都发一个公开 Release。"""
    wf = _workflow("release-client.yml")
    on = wf.get("on", wf.get(True))
    assert list(on) == ["push"], f"端侧发版只应由 push tag 触发，实际 {sorted(on)}"
    assert on["push"] == {"tags": ["v*"]}, f"触发条件被改宽了：{on['push']}"


def test_client_release_can_write_contents():
    """gh release create 要 contents:write；少了它作业会在最后一步 403，
    而前面构建的几分钟全白跑。"""
    assert _workflow("release-client.yml")["permissions"] == {"contents": "write"}


def test_client_release_does_not_cancel_in_progress():
    """取消会留下「有 tag、没有安装包」的半成品发布。"""
    assert _workflow("release-client.yml")["concurrency"]["cancel-in-progress"] is False


def test_release_rerun_overwrites_asset_instead_of_failing():
    """重跑同一个 tag 时 Release 已存在，gh release create 会直接失败。
    这条把「重跑永远安全」钉住（release-image.yml 的回写步骤踩过同一个坑）。"""
    run = "\n".join(
        s["run"] for s in _workflow("release-client.yml")["jobs"]["bundle-and-release"]["steps"]
        if "run" in s
    )
    assert "gh release view" in run and "--clobber" in run


def test_tag_must_match_version_file():
    """版本号只有一条线：放过不一致，Release 标题写 v1.2.0 而 `mycode --version`
    与 GET /api/version 答 1.1.0（agents/version.py 读的就是 VERSION 文件）。"""
    run = "\n".join(
        s["run"] for s in _workflow("release-client.yml")["jobs"]["bundle-and-release"]["steps"]
        if "run" in s
    )
    assert "VERSION" in run and "GITHUB_REF_NAME" in run


@pytest.mark.parametrize("name", ["release-image.yml", "release-client.yml"])
def test_release_workflows_pin_actions_to_commit_sha(name):
    """这两个作业持有可写 token（回写 tag / 建 Release），所以浮动 tag 被上游投毒时
    损失不是"构建坏了"而是"仓库被改了"。ci.yml 用浮动 tag 是可以的，它没有写权限。"""
    wf = _workflow(name)
    uses = [
        s["uses"]
        for job in wf["jobs"].values()
        for s in job["steps"]
        if "uses" in s
    ]
    assert uses, f"{name} 里没有用到任何 action，断言可能已经失效"
    floating = [u for u in uses if not re.search(r"@[0-9a-f]{40}$", u)]
    assert not floating, f"{name} 里的 action 没 pin 到 commit SHA：{floating}"


def test_launcher_guards_source_only_subcommands():
    """安装包里既没有 frontend/src（Vite dev server 起不来）也没有 deploy/
    （云端 bring-up 无从下手）。这两个子命令必须给出人话，而不是让 npm/kubectl
    抛一堆与安装包无关的错。"""
    text = (ROOT / "mycode").read_text(encoding="utf-8")
    assert "require_repo" in text
    guard_block = text[text.index("case \"${1:-}\" in"):text.index("# 优先用项目自带 venv")]
    assert "require_repo start" in guard_block
    assert re.search(r'require_repo "\$1" deploy/', guard_block)


# ── 版本号：整条发布模型的对账依据 ─────────────────────────────────

def _version_file() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_version_module_reads_version_file():
    """`mycode --version`、GET /api/version、发版 workflow 校验 tag，三者读的必须是
    同一个文件——否则「一条版本线」只是文档里的说法。"""
    from agents.version import __version__

    assert __version__ == _version_file()


def test_cli_version_exits_before_any_side_effect(monkeypatch, capsys):
    """报版本不该有副作用，也不该等 MCP / Langfuse 那几秒——所以它必须排在
    .env 加载与 tracing 初始化之前。"""
    import sys

    from agents import main as entry

    monkeypatch.setattr(sys, "argv", ["mycode", "--version"])
    touched = []
    monkeypatch.setattr(entry, "_load_env_file", lambda: touched.append(".env"))

    with pytest.raises(SystemExit) as excinfo:
        entry.main()

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == _version_file()
    assert not touched, "--version 之前就跑起了副作用，它会跟着 .env / tracing 一起变慢"
