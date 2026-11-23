"""沙箱 Job manifest 渲染与状态判定。安全断言是重点：PAT 绝不进 Job。"""
from __future__ import annotations

import re

import pytest

from frontend.server.cloud import sandbox as sb
from frontend.server.cloud.task_store import CloudTask


def _task(**kw) -> CloudTask:
    base = dict(id="abc123", repo_url="https://github.com/o/r", prompt="修登录",
                workspace_name="r--abc123", setup_command="npm ci")
    base.update(kw)
    return CloudTask(**base)


def test_job_name_is_dns_safe():
    assert sb.job_name("abc123") == "mycode-task-abc123"


def test_manifest_has_no_credential_field():
    m = sb.render_job(_task(), image="img:tag")
    blob = repr(m)
    # 裸 "PAT" 会命中 PYTHONPATH，所以按凭据字段名逐个查，再用词边界查 PAT 这个词
    for needle in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_ASKPASS", "git-token",
                   "cloud_git_token", "mycode-secrets"):
        assert needle not in blob
    env_names = [e["name"] for e in m["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert not any("TOKEN" in n for n in env_names)
    assert not any(re.search(r"\bPAT\b", n) for n in env_names)


def test_subpaths_isolate_state_and_workspace():
    m = sb.render_job(_task(), image="img:tag")
    mounts = m["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    by_path = {x["mountPath"]: x for x in mounts}
    assert by_path["/home/app/.mycode"]["subPath"] == "cloud_tasks/abc123"
    assert by_path["/home/app/workspace"]["subPath"] == "workspaces/r--abc123"
    # 沙箱只拿这两个子目录，绝不挂整个 PVC 根
    assert all(x.get("subPath") for x in mounts if x["name"] == "data")


def test_security_context_is_full_strength():
    sc = sb.render_job(_task(), image="img:tag")["spec"]["template"]["spec"]
    assert sc["securityContext"]["runAsNonRoot"] is True
    assert sc["securityContext"]["runAsUser"] == 10001
    assert sc["securityContext"]["fsGroup"] == 10001
    c = sc["containers"][0]["securityContext"]
    assert c["readOnlyRootFilesystem"] is True
    assert c["allowPrivilegeEscalation"] is False
    assert c["capabilities"]["drop"] == ["ALL"]
    assert c["seccompProfile"]["type"] == "RuntimeDefault"


def test_no_retry_and_deadline():
    spec = sb.render_job(_task(), image="img:tag", deadline_s=900)["spec"]
    assert spec["backoffLimit"] == 0            # 重试会重复烧 token
    assert spec["activeDeadlineSeconds"] == 900
    assert spec["ttlSecondsAfterFinished"] == 86400
    assert spec["template"]["spec"]["restartPolicy"] == "Never"


def test_command_runs_setup_then_agent_in_workspace():
    c = sb.render_job(_task(), image="img:tag")["spec"]["template"]["spec"]["containers"][0]
    assert c["workingDir"] == "/home/app/workspace"
    script = c["command"][-1]
    assert "npm ci" in script
    assert "agents.main" in script and "--yolo" in script
    # prompt 经 env 传，不拼进 shell 字符串（防注入）
    assert "修登录" not in script
    assert any(e["name"] == "MYCODE_TASK_PROMPT" for e in c["env"])


def test_empty_setup_command_still_runs_agent():
    c = sb.render_job(_task(setup_command=""), image="img:tag")["spec"]["template"]["spec"]["containers"][0]
    assert "agents.main" in c["command"][-1]


def test_agents_package_importable_from_repo_cwd():
    """workingDir 是仓库目录而不是 /app，不给 PYTHONPATH 就 ModuleNotFoundError。"""
    c = sb.render_job(_task(), image="img:tag")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["PYTHONPATH"] == "/app"


def test_agent_runs_image_code_not_the_cloned_repos():
    """`python -m` 把 CWD 插在 sys.path[0]，仓库自带 agents/ 就会盖掉镜像里那份。

    实测：拿 MyCode 自己当目标仓库时，沙箱执行的是克隆出来的旧代码，
    镜像里的修复一行都没生效。-P 去掉 CWD 那一项，PYTHONPATH=/app 才说话。
    """
    c = sb.render_job(_task(), image="img:tag")["spec"]["template"]["spec"]["containers"][0]
    assert "python -P -m agents.main" in c["command"][-1]


def test_sandbox_gets_model_config_readonly():
    """沙箱里的 agent 要调模型，就得拿到 config.json（api_key 内联在里面）。

    进沙箱的只有模型凭据；访问令牌（mycode-secrets）与 Git PAT 都不在。
    """
    m = sb.render_job(_task(), image="img:tag")
    spec = m["spec"]["template"]["spec"]
    c = spec["containers"][0]
    by_path = {x["mountPath"]: x for x in c["volumeMounts"]}
    assert by_path["/etc/mycode"]["readOnly"] is True
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols[by_path["/etc/mycode"]["name"]]["secret"]["secretName"] == "mycode-config"
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["MYCODE_CONFIG_PATH"] == "/etc/mycode/config.json"
    assert "mycode-secrets" not in repr(m)


def test_home_is_writable_without_relaxing_readonly_root():
    """只读根下 HOME 必须有可写落点（CLI 历史、skills、pip/npm 装的目标）。"""
    spec = sb.render_job(_task(), image="img:tag")["spec"]["template"]["spec"]
    c = spec["containers"][0]
    by_path = {x["mountPath"]: x for x in c["volumeMounts"]}
    assert "subPath" not in by_path["/home/app"]
    vols = {v["name"]: v for v in spec["volumes"]}
    assert "emptyDir" in vols[by_path["/home/app"]["name"]]
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    env = {e["name"]: e["value"] for e in c["env"]}
    # skill venv 挡在 PVC 之外：跟着任务目录活下来会无上限吃配额
    assert env["MYCODE_SKILL_VENV"].startswith("/home/app/.my-code/")


def test_resources_come_from_settings():
    m = sb.render_job(_task(), image="img:tag", cpu="2", memory="4Gi")
    r = m["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert r["limits"]["cpu"] == "2" and r["limits"]["memory"] == "4Gi"


@pytest.mark.parametrize("job,expected", [
    (None, "NotFound"),
    ({"status": {}}, "Pending"),
    ({"status": {"active": 1}}, "Running"),
    ({"status": {"succeeded": 1}}, "Succeeded"),
    ({"status": {"failed": 1}}, "Failed"),
    ({"status": {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}}, "DeadlineExceeded"),
    # k8s 1.31+ 先写 FailureTarget，等 Pod 真的收干净才写 Failed。实测这两条差 31s，
    # 而任务在 FailureTarget 之后 1s 就被判了终态——只认 Failed 会把超时误判成普通失败。
    ({"status": {"failed": 1, "conditions": [
        {"type": "FailureTarget", "status": "True", "reason": "DeadlineExceeded"}]}}, "DeadlineExceeded"),
    ({"status": {"failed": 1, "conditions": [
        {"type": "FailureTarget", "status": "True", "reason": "BackoffLimitExceeded"}]}}, "Failed"),
    ({"status": {"active": 1, "conditions": [
        {"type": "FailureTarget", "status": "False", "reason": "DeadlineExceeded"}]}}, "Running"),
])
def test_job_phase(job, expected):
    assert sb.job_phase(job) == expected


def test_settings_defaults(monkeypatch):
    for k in ("MYCODE_SANDBOX_IMAGE", "MYCODE_SANDBOX_CPU", "MYCODE_SANDBOX_MEMORY",
              "MYCODE_SANDBOX_DEADLINE", "MYCODE_CLOUD_MAX_CONCURRENCY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MYCODE_SANDBOX_IMAGE", "img:tag")
    s = sb.sandbox_settings()
    assert s["cpu"] == "1" and s["memory"] == "2Gi"
    assert s["deadline_s"] == 1800 and s["max_concurrency"] == 1


def test_missing_image_raises(monkeypatch):
    monkeypatch.delenv("MYCODE_SANDBOX_IMAGE", raising=False)
    with pytest.raises(sb.SandboxError):
        sb.sandbox_settings()