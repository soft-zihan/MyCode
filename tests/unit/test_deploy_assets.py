"""deploy/ 下 manifests 的承重断言。

YAML 是纯数据，改坏了不会有任何测试变红，只会在集群里静默降级成不安全配置
（比如某天有人删掉 cap_drop 而没人发现）。参照 besimple-oss/broccoli 的
tests/test_deploy_assets.py（MIT）。
"""
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
BASE = DEPLOY / "base"
KIND = DEPLOY / "kind"

UID_GID = 10001
PORT = 5555
CONFIG_MOUNT = "/etc/mycode"
SESSION_MOUNT = "/home/app/.mycode"


def _load(name: str) -> dict:
    path = BASE / name
    assert path.is_file(), f"缺少 {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _walk(node: Any, key: str | None = None) -> Iterator[tuple[str | None, Any]]:
    """递归产出 (父键, 值)，用于在整份 YAML 里找不该出现的字段。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)
    else:
        yield key, node


@pytest.fixture(scope="module")
def deployment() -> dict:
    return _load("deployment.yaml")


def _pod(deployment: dict) -> dict:
    return deployment["spec"]["template"]["spec"]


def _container(deployment: dict) -> dict:
    return _pod(deployment)["containers"][0]


# ── 容器加固：少一条就是静默降级 ────────────────────────────────────

def test_runs_as_non_root_with_fsgroup(deployment):
    sc = _pod(deployment).get("securityContext", {})
    assert sc.get("runAsNonRoot") is True
    assert sc.get("runAsUser") == UID_GID
    assert sc.get("runAsGroup") == UID_GID
    # fsGroup 不是可有可无：PVC 里的文件要能被 10001 写，靠的就是它把卷 chgrp
    assert sc.get("fsGroup") == UID_GID, "缺 fsGroup，PVC 里的文件 10001 写不了"


def test_read_only_root_filesystem(deployment):
    assert _container(deployment)["securityContext"].get("readOnlyRootFilesystem") is True


def test_drops_all_capabilities_and_adds_none(deployment):
    caps = _container(deployment)["securityContext"].get("capabilities", {})
    assert caps.get("drop") == ["ALL"]
    assert not caps.get("add"), f"不该额外授予能力：{caps.get('add')}"


def test_no_privilege_escalation(deployment):
    assert _container(deployment)["securityContext"].get("allowPrivilegeEscalation") is False


def test_seccomp_runtime_default(deployment):
    assert _pod(deployment)["securityContext"].get("seccompProfile", {}).get("type") == "RuntimeDefault"


# ── 探针与资源 ─────────────────────────────────────────────────────

def test_three_probes_all_hit_health_endpoint(deployment):
    c = _container(deployment)
    for name in ("startupProbe", "livenessProbe", "readinessProbe"):
        probe = c.get(name)
        assert probe, f"缺 {name}"
        assert probe["httpGet"]["path"] == "/api/health"
        assert probe["httpGet"]["port"] == PORT


def test_startup_probe_budget_covers_slow_start(deployment):
    """startupProbe 的预算必须盖住真实冷启动。lifespan 里有崩溃恢复扫描与
    MCP 连接（最长 30s 超时），这里钉一个下限，防止有人把 failureThreshold 调到 1。"""
    p = _container(deployment)["startupProbe"]
    assert p["failureThreshold"] * p["periodSeconds"] >= 60


def test_resources_have_both_requests_and_limits(deployment):
    r = _container(deployment).get("resources", {})
    assert r.get("requests"), "没有 requests，调度器无从判断放不放得下，QoS 会掉到 BestEffort（最先被驱逐）"
    assert r.get("limits"), "没有 limits，一个失控任务能吃完整个节点"
    for kind in ("requests", "limits"):
        assert "cpu" in r[kind] and "memory" in r[kind]


# ── 部署策略：RWO 卷与滚动更新互斥 ─────────────────────────────────

def test_strategy_is_recreate_while_state_is_on_pvc(deployment):
    """B1 的状态在 RWO PVC 上，RollingUpdate 会让新旧两个 Pod 同时想挂同一个卷
    而卡在 Multi-Attach error。所以必须 Recreate。这条断言同时是 C 批的提醒：
    只有把会话后端换成 PostgreSQL、控制面无状态化之后，才谈得上滚动更新不断流。"""
    assert deployment["spec"]["strategy"]["type"] == "Recreate"


def test_single_replica_in_b1(deployment):
    assert deployment["spec"]["replicas"] == 1, "B1 是单副本；多副本要先做 C 批的无状态化"


# ── 镜像与网络 ─────────────────────────────────────────────────────

def test_image_tag_is_never_latest(deployment):
    tag = _container(deployment)["image"].rsplit(":", 1)[-1]
    assert tag != "latest", (
        "latest 会让 ArgoCD 认为 Git 内容无变化而永不同步，"
        "也会让 imagePullPolicy: IfNotPresent 静默用节点上的旧镜像"
    )


def test_container_port_matches_image(deployment):
    assert _container(deployment)["ports"][0]["containerPort"] == PORT


def test_binds_all_interfaces(deployment):
    env = {e["name"]: e.get("value") for e in _container(deployment).get("env", [])}
    assert env.get("MYCODE_HOST") == "0.0.0.0", \
        "auth.py 默认绑 127.0.0.1，不覆盖的话 Service 转不进来"


def test_ingress_allows_long_lived_websocket():
    """MyCode 的事件流走单条 WebSocket 长连接。nginx ingress 默认 60s 读写超时
    会把它掐断（前端表现为「跑着跑着事件不再更新」），所以必须显式放大。"""
    ann = _load("ingress.yaml")["metadata"].get("annotations", {})
    read_timeout = int(ann.get("nginx.ingress.kubernetes.io/proxy-read-timeout", "0"))
    send_timeout = int(ann.get("nginx.ingress.kubernetes.io/proxy-send-timeout", "0"))
    assert read_timeout >= 3600, f"WS 会被 {read_timeout}s 的读超时掐断"
    assert send_timeout >= 3600, f"WS 会被 {send_timeout}s 的写超时掐断"


def test_ingress_body_size_covers_ws_max_size():
    """main.py 里 uvicorn 的 ws_max_size 是 10MB，ingress 默认 1m 会先拦住。"""
    ann = _load("ingress.yaml")["metadata"].get("annotations", {})
    raw = ann.get("nginx.ingress.kubernetes.io/proxy-body-size", "1m")
    assert raw.endswith("m") and int(raw[:-1]) >= 16, f"proxy-body-size={raw} 小于 uvicorn 的 10MB ws_max_size"


def test_ingress_has_hostless_fallback_rule():
    """必须有一条**不写 host** 的兜底规则，否则浏览器打不开。

    宿主浏览器发的是 `Host: localhost:8080`，而只有 `host: mycode.local` 的规则
    不匹配它，请求会落到 ingress-nginx 的默认后端（实测 404）。浏览器发不出自定义
    Host 头，所以唯一的替代方案是改 Windows 的 hosts——那需要管理员权限，
    是「从开机到能用」里唯一没法自动化的一步，也就谈不上一键启动。
    """
    rules = _load("ingress.yaml")["spec"]["rules"]
    hostless = [r for r in rules if "host" not in r]
    assert len(hostless) == 1, f"要有且只有一条不写 host 的兜底规则，实际 {len(hostless)} 条"
    backends = {
        (p["backend"]["service"]["name"], p["backend"]["service"]["port"]["number"])
        for r in rules for p in r["http"]["paths"]
    }
    assert backends == {("control-plane", 80)}, f"兜底规则指到了别的后端：{backends}"


# ── 配置与存储的挂载方式 ───────────────────────────────────────────

def test_config_mounted_from_secret_readonly(deployment):
    """config.json 内联了 api_key（agents/config.py 的 ModelEndpointConfig.api_key），
    整份文件必须走 Secret；放 ConfigMap 等于把密钥明文摊在集群里。"""
    vols = {v["name"]: v for v in _pod(deployment)["volumes"]}
    mounts = [m for m in _container(deployment)["volumeMounts"] if m["mountPath"] == CONFIG_MOUNT]
    assert len(mounts) == 1, f"{CONFIG_MOUNT} 必须恰好挂一次"
    assert mounts[0].get("readOnly") is True
    assert "secret" in vols[mounts[0]["name"]], \
        f"配置卷必须来自 Secret，实际是 {sorted(vols[mounts[0]['name']])}"


def test_config_path_env_points_at_mount(deployment):
    env = {e["name"]: e for e in _container(deployment).get("env", [])}
    assert env["MYCODE_CONFIG_PATH"]["value"] == f"{CONFIG_MOUNT}/config.json"


def test_projects_dir_is_on_pvc(deployment):
    """project 注册表默认落 ~/.my-code/projects，而 HOME 是 emptyDir——Pod 一重启，
    「云端有哪些项目」就全忘了：导入的代码还在 PVC 上，列表却空了。"""
    env = {e["name"]: e for e in _container(deployment).get("env", [])}
    assert "MYCODE_PROJECTS_DIR" in env, "注册表会落进 emptyDir，Pod 重建后云端项目列表清空"
    value = env["MYCODE_PROJECTS_DIR"]["value"]
    assert value.startswith(SESSION_MOUNT + "/"), \
        f"{value} 不在 PVC 挂载点 {SESSION_MOUNT} 之下，等于没持久化"


def test_git_proxy_comes_from_optional_configmap(deployment):
    """Pod 直连 github.com:443 会超时，git 必须走宿主代理转发口。但那个网关地址
    **每台机器都不同**，所以不能写死在 base 里；optional 则保证没建 ConfigMap 的
    环境（本地 dev、CI 的 kustomize build）照常渲染与启动。"""
    env = {e["name"]: e for e in _container(deployment).get("env", [])}
    ref = env["MYCODE_GIT_PROXY"].get("valueFrom", {}).get("configMapKeyRef")
    assert ref, "MYCODE_GIT_PROXY 必须来自 configMapKeyRef，不能是写死的 value"
    assert ref.get("optional") is True, \
        "少了 optional，没建 mycode-net 的环境会直接 CreateContainerConfigError 起不来"
    assert (ref["name"], ref["key"]) == ("mycode-net", "git-proxy")


def test_no_global_proxy_env(deployment):
    """只给 git 走代理，不设全局 HTTPS_PROXY：模型端点当前是直连可达的，
    全局改道会把所有出站都塞进那个转发口，平白多一个故障面（deploy/README.md §1.6）。"""
    names = {e["name"] for e in _container(deployment).get("env", [])}
    leaked = names & {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"}
    assert not leaked, f"Pod 里设了全局代理 {sorted(leaked)}，模型端点的出站会被一起改道"


def test_session_dir_is_on_pvc(deployment):
    """会话事件是唯一数据源，必须落 PVC；挂 emptyDir 的话 Pod 一重建全丢。"""
    vols = {v["name"]: v for v in _pod(deployment)["volumes"]}
    mounts = {m["mountPath"]: m["name"] for m in _container(deployment)["volumeMounts"]}
    assert SESSION_MOUNT in mounts, f"{SESSION_MOUNT} 没挂卷，事件会写进只读根而失败"
    assert "persistentVolumeClaim" in vols[mounts[SESSION_MOUNT]], \
        f"{SESSION_MOUNT} 挂在非 PVC 卷上，Pod 重建后事件日志全部丢失"


def test_home_is_on_writable_volume(deployment):
    """HOME 整体必须是可写卷。写路径是散落且硬编码的：.mycode（session.py:524）、
    .my-code（config.py:21 / tools/executor.py）、.agents/skills（skills/skills.py:107）、
    CLI history（cli/history.py:66）都直接 Path.home()/...。逐个挂卷迟早漏一个，
    而只读根之下漏一个就是运行期 PermissionError。"""
    mounts = {m["mountPath"]: m["name"] for m in _container(deployment)["volumeMounts"]}
    vols = {v["name"]: v for v in _pod(deployment)["volumes"]}
    assert "/home/app" in mounts, "HOME 没挂卷，只读根下任何 Path.home() 写入都会失败"
    assert "emptyDir" in vols[mounts["/home/app"]]


def test_session_pvc_nests_under_home_emptydir(deployment):
    """PVC 必须挂在 HOME 之下的子路径，而不是取代 HOME——否则 .my-code / .agents
    这些同级写路径会被一起塞进持久卷，把可丢弃的运行期产物也持久化了。"""
    paths = [m["mountPath"] for m in _container(deployment)["volumeMounts"]]
    assert "/home/app" in paths and SESSION_MOUNT in paths
    assert SESSION_MOUNT.startswith("/home/app/"), "PVC 应嵌套在 HOME 之下"


def test_tmp_is_writable_volume(deployment):
    """只读根文件系统下 /tmp 必须单独给卷：PYTHONPYCACHEPREFIX 指向 /tmp/pycache，
    没卷的话 Python 连 .pyc 都写不了。"""
    mounts = {m["mountPath"]: m["name"] for m in _container(deployment)["volumeMounts"]}
    vols = {v["name"]: v for v in _pod(deployment)["volumes"]}
    assert "/tmp" in mounts
    assert "emptyDir" in vols[mounts["/tmp"]]


def test_pvc_is_rwo_with_explicit_size():
    pvc = _load("pvc.yaml")
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"], \
        "B1 单副本用 RWO 足够；RWX 需要额外存储后端，属 C 批"
    assert pvc["spec"]["resources"]["requests"]["storage"]


def test_service_targets_container_port():
    assert _load("service.yaml")["spec"]["ports"][0]["targetPort"] == PORT


def test_control_plane_has_ingress_networkpolicy():
    """B1 就给控制面上最小策略（默认拒绝 ingress、只放行 ingress-nginx）。
    这不只是为了安全，也是让 Task 3 那条 NetworkPolicy 证明在真实负载上再验一次。"""
    pol = _load("networkpolicy.yaml")
    assert pol["spec"]["policyTypes"] == ["Ingress"]
    assert pol["spec"]["podSelector"]["matchLabels"], "空 podSelector 会波及整个 namespace"


# ── kustomize 结构：CI 的 sed 回写依赖它 ───────────────────────────

def test_base_kustomization_lists_only_existing_files():
    """kustomize 不会告诉你「列了一个不存在的文件」有多严重——它直接报错退出，
    而这条在 CI 里表现为一次莫名的部署失败。"""
    kz = yaml.safe_load((BASE / "kustomization.yaml").read_text(encoding="utf-8"))
    missing = [r for r in kz.get("resources", []) if not (BASE / r).is_file()]
    assert not missing, f"deploy/base/kustomization.yaml 列了不存在的文件：{missing}"


def test_kind_overlay_points_at_base():
    kz = yaml.safe_load((KIND / "kustomization.yaml").read_text(encoding="utf-8"))
    assert kz.get("resources") == ["../base"]


def test_kind_overlay_has_exactly_one_newtag_line():
    """CI 用 `sed -i "s|^    newTag: .*|...|"` 回写镜像 tag，前提是这一行在
    overlay 里恰好出现一次且缩进固定。多一处或缩进变了，sed 会静默改错地方，
    集群于是跑着一个 Git 里查不到的版本——这是最难查的一类故障。"""
    text = (KIND / "kustomization.yaml").read_text(encoding="utf-8")
    hits = [ln for ln in text.splitlines() if ln.startswith("    newTag: ")]
    assert len(hits) == 1, f"deploy/kind/kustomization.yaml 里 `    newTag: ` 出现 {len(hits)} 次，CI 的 sed 回写会失准"


# ── 密钥绝不入库 ───────────────────────────────────────────────────

def test_no_secret_yaml_committed():
    assert not list(DEPLOY.rglob("secret.yaml")), (
        "deploy/ 下不该有 secret.yaml；Secret 由 scripts/k8s-secret.sh 在本地生成"
    )


def test_no_api_key_value_anywhere_under_deploy():
    """守住「某天有人图省事把 config.json 塞进 manifests 并提交」这个真实风险。"""
    offenders = []
    for path in sorted(DEPLOY.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            for key, value in _walk(doc):
                if key == "api_key" and isinstance(value, str) and value.strip():
                    offenders.append(f"{path.relative_to(DEPLOY)}: api_key={value[:6]}…")
    assert not offenders, f"deploy/ 下出现疑似真实密钥：{offenders}"
