"""沙箱 Job：manifest 渲染 + k8s REST。

用已有的 httpx 而不是 kubernetes 客户端：只需要 create/get/delete job + list pods
+ get log 五个调用，SA 凭据三个路径是标准的。引官方客户端会带进
requests/websocket-client/google-auth 一棵依赖树，而镜像体积是要实测上报的数字。
轮询代替 watch——并发上限 1-2，每 2s 一轮的成本可以忽略。

隔离靠 subPath：沙箱只拿 PVC 的 cloud_tasks/<id> 与 workspaces/<name> 两个子目录，
够不到 PVC 根的 Git token，也看不到别的任务。控制面挂整个 PVC，所以两边看的是
同一份文件（同节点同卷），不需要额外传补丁。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from .task_store import CloudTask

SANDBOX_COMPONENT = "sandbox"
SANDBOX_LABELS = {
    "app.kubernetes.io/name": "mycode",
    "app.kubernetes.io/component": SANDBOX_COMPONENT,
}
SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
API_BASE = "https://kubernetes.default.svc"
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)


class SandboxError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def job_name(task_id: str) -> str:
    return f"mycode-task-{task_id}"


def sandbox_settings() -> dict[str, Any]:
    image = os.environ.get("MYCODE_SANDBOX_IMAGE", "").strip()
    if not image:
        raise SandboxError("未配置 MYCODE_SANDBOX_IMAGE，无法建沙箱")
    return {
        "image": image,
        "cpu": os.environ.get("MYCODE_SANDBOX_CPU", "1").strip() or "1",
        "memory": os.environ.get("MYCODE_SANDBOX_MEMORY", "2Gi").strip() or "2Gi",
        "deadline_s": int(os.environ.get("MYCODE_SANDBOX_DEADLINE", "1800")),
        "max_concurrency": int(os.environ.get("MYCODE_CLOUD_MAX_CONCURRENCY", "1")),
        "namespace": os.environ.get("MYCODE_NAMESPACE", "mycode").strip() or "mycode",
    }


def render_job(
    task: CloudTask,
    *,
    image: str,
    namespace: str = "mycode",
    cpu: str = "1",
    memory: str = "2Gi",
    deadline_s: int = 1800,
    pvc_name: str = "mycode-data",
) -> dict[str, Any]:
    setup = task.setup_command.strip()
    prefix = f"{setup} && " if setup else ""
    # prompt 只经 env 传，绝不拼进 shell 字符串：任务描述是用户输入，拼进去就是命令注入。
    script = f'{prefix}exec python -m agents.main --yolo "$MYCODE_TASK_PROMPT"'
    labels = {**SANDBOX_LABELS, "mycode.cloud/task-id": task.id}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(task.id),
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,              # 重试会重复烧 token，失败就是失败
            "activeDeadlineSeconds": deadline_s,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    # 不设 serviceAccountName：沙箱不需要任何 k8s 权限，
                    # default SA 的零权限正好。
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "runAsGroup": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "sandbox",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "workingDir": "/home/app/workspace",
                            "command": ["/bin/sh", "-c", script],
                            "env": [
                                {"name": "MYCODE_TASK_PROMPT", "value": task.prompt},
                                {"name": "MYCODE_HOME", "value": "/home/app/.mycode"},
                                {"name": "MYCODE_TRACING", "value": "0"},
                                # workingDir 是仓库目录，agents 包在 /app 下：
                                # 不给 PYTHONPATH，`python -m agents.main` 直接 ModuleNotFoundError。
                                {"name": "PYTHONPATH", "value": "/app"},
                                # 模型凭据内联在 config.json 里，沙箱要调模型就得有它。
                                # 访问令牌与 Git PAT 都不在沙箱（前者只在控制面 env，
                                # 后者在 PVC 根，subPath 够不到）。
                                {"name": "MYCODE_CONFIG_PATH", "value": "/etc/mycode/config.json"},
                                # skill venv 挡在任务目录之外，否则跟着 PVC 无上限吃配额
                                {"name": "MYCODE_SKILL_VENV", "value": "/home/app/.my-code/skill-venv"},
                            ],
                            "securityContext": {
                                "readOnlyRootFilesystem": True,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "256Mi"},
                                "limits": {"cpu": cpu, "memory": memory, "ephemeral-storage": "2Gi"},
                            },
                            "volumeMounts": [
                                # HOME 整体可写（CLI 历史、skills、pip/npm 的安装目标），
                                # 只读根因此不必放开。kubelet 按路径深度排序挂载，
                                # 下面两个 subPath 嵌在它之下。
                                {"name": "home", "mountPath": "/home/app"},
                                {
                                    "name": "data",
                                    "mountPath": "/home/app/.mycode",
                                    "subPath": f"cloud_tasks/{task.id}",
                                },
                                {
                                    "name": "data",
                                    "mountPath": "/home/app/workspace",
                                    "subPath": f"workspaces/{task.workspace_name}",
                                },
                                {"name": "config", "mountPath": "/etc/mycode", "readOnly": True},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name}},
                        {"name": "home", "emptyDir": {}},
                        {"name": "config", "secret": {"secretName": "mycode-config"}},
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def _client() -> httpx.Client:
    token_file = SA_DIR / "token"
    ca_file = SA_DIR / "ca.crt"
    if not token_file.is_file():
        raise SandboxError("控制面不在集群里：找不到 ServiceAccount 凭据，无法建沙箱")
    token = token_file.read_text(encoding="utf-8").strip()
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {token}"},
        verify=str(ca_file),
        timeout=TIMEOUT,
        trust_env=False,
    )


def create_job(manifest: dict) -> dict:
    ns = manifest["metadata"]["namespace"]
    with _client() as c:
        r = c.post(f"/apis/batch/v1/namespaces/{ns}/jobs", json=manifest)
    if r.status_code == 403:
        raise SandboxError("控制面缺 RBAC：建 Job 被拒（403），检查 deploy/base/rbac.yaml 是否已同步")
    if r.status_code >= 400:
        raise SandboxError(f"建 Job 失败（{r.status_code}）：{r.text[:300]}")
    return r.json()


def get_job(name: str) -> dict | None:
    ns = sandbox_settings()["namespace"]
    with _client() as c:
        r = c.get(f"/apis/batch/v1/namespaces/{ns}/jobs/{name}")
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise SandboxError(f"查 Job 失败（{r.status_code}）：{r.text[:300]}")
    return r.json()


def delete_job(name: str) -> bool:
    ns = sandbox_settings()["namespace"]
    with _client() as c:
        r = c.request(
            "DELETE", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}",
            json={"propagationPolicy": "Background"},
        )
    return r.status_code in (200, 202, 404)


def job_phase(job: dict | None) -> str:
    if job is None:
        return "NotFound"
    st = job.get("status") or {}
    for cond in st.get("conditions") or []:
        if cond.get("type") == "Failed" and cond.get("status") == "True":
            return "DeadlineExceeded" if cond.get("reason") == "DeadlineExceeded" else "Failed"
    if st.get("succeeded"):
        return "Succeeded"
    if st.get("failed"):
        return "Failed"
    if st.get("active"):
        return "Running"
    return "Pending"


def pod_logs(name: str, tail_lines: int = 200) -> str:
    """沙箱日志。诊断辅助，任何一步失败都返回空串——不该让 reconcile 崩。"""
    ns = sandbox_settings()["namespace"]
    task_id = name.removeprefix("mycode-task-")
    try:
        with _client() as c:
            r = c.get(
                f"/api/v1/namespaces/{ns}/pods",
                params={"labelSelector": f"mycode.cloud/task-id={task_id}"},
            )
            if r.status_code >= 400:
                return ""
            items = r.json().get("items") or []
            if not items:
                return ""
            pod = items[0]["metadata"]["name"]
            lr = c.get(f"/api/v1/namespaces/{ns}/pods/{pod}/log", params={"tailLines": tail_lines})
            return lr.text if lr.status_code < 400 else ""
    except (httpx.HTTPError, SandboxError, KeyError, ValueError):
        return ""