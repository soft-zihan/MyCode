"""云端实例（K8s 里那份同一个后端）的探测与转发。

为什么由本地后端转发，而不是让浏览器直连云端：

1. 浏览器直连是跨源（本地 5555/8090 → 云端 8080）。要么给云端开 CORS，要么走同源代理。
   给一个带 Bearer token 的接口开 CORS 等于把凭据面摊给任意来源，不值得。
2. 云端 token 由后端持有、转发时加 Authorization 头，前端只看到结果，
   不需要把它塞进 localStorage 再随每个请求发出去。

配置来源（优先级从高到低）：

- 环境变量 `MYCODE_CLOUD_URL` / `MYCODE_CLOUD_TOKEN`
- `<config.json 同目录>/cloud.json`：`{"url": "...", "token": "..."}`。
  `scripts/cloud-up.sh` 装完集群会写这个文件，所以「一键起云端」之后
  前端的云端面板不需要用户再手填地址和 token。
- 缺省 `http://localhost:8080`（kind 的 extraPortMappings 把 ingress 映到宿主 8080）

⚠️ 所有出站都 `trust_env=False`：本机常驻 HTTP(S)_PROXY，而云端地址是 localhost。
让 httpx 读环境代理的话，请求会被交给 Windows 侧的代理再绕回来，失败时还会回一个
带 `Proxy-Connection` 头的假 502，把排查带偏（同一教训见 verify/deploy-probe.sh 顶部）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.config import config_path

router = APIRouter(tags=["cloud"])

URL_ENV = "MYCODE_CLOUD_URL"
TOKEN_ENV = "MYCODE_CLOUD_TOKEN"
DEFAULT_URL = "http://localhost:8080"
PROBE_TIMEOUT = 6.0
# 读超时要给足（云端克隆一个大仓库可能要几分钟，比 workspace_import.CLONE_TIMEOUT
# 稍长，否则本地先断、云端还在跑，用户看到「失败」而实际成功了）；
# 但**连接**超时必须短。实测过：把两者合成一个 620s 的总超时之后，
# 云端地址不可达时这个请求会挂住十分钟才回来——连不上是立刻就该知道的事。
FORWARD_TIMEOUT = httpx.Timeout(connect=5.0, read=620.0, write=60.0, pool=5.0)


def _cloud_file() -> Path:
    return config_path().parent / "cloud.json"


def cloud_config() -> tuple[str, str | None, bool]:
    """返回 (url, token, configured)。configured=False 表示 env 与 cloud.json 都没配。"""
    url = os.environ.get(URL_ENV, "").strip()
    token = os.environ.get(TOKEN_ENV, "").strip() or None
    configured = bool(url or token)

    path = _cloud_file()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            configured = True
            url = url or str(data.get("url") or "").strip()
            token = token or (str(data.get("token") or "").strip() or None)

    return (url or DEFAULT_URL).rstrip("/"), token, configured


class CloudStatus(BaseModel):
    url: str
    configured: bool            # false = 用的是缺省地址，没人配过
    reachable: bool
    auth_required: bool         # 云端是否开了 token 鉴权
    authenticated: bool         # 我们手上的 token 能不能过云端的鉴权
    version: Optional[str] = None
    latency_ms: Optional[int] = None
    error: Optional[str] = None


class CloudProjectList(BaseModel):
    url: str
    projects: list[dict[str, Any]]


class CloudWorkspaceList(BaseModel):
    url: str
    root: str
    workspaces: list[dict[str, Any]]


class CloudImportRequest(BaseModel):
    repo_url: str
    ref: Optional[str] = None
    name: Optional[str] = None


class CloudImportResponse(BaseModel):
    url: str
    directory: str
    name: str
    repo_url: str
    ref: Optional[str] = None
    head: Optional[str] = None
    branch: Optional[str] = None
    created: bool


@router.get("/api/cloud/status", response_model=CloudStatus)
async def api_cloud_status() -> CloudStatus:
    url, token, configured = cloud_config()
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, trust_env=False) as client:
        try:
            health = await client.get(f"{url}/api/health")
        except httpx.HTTPError as exc:
            return CloudStatus(
                url=url, configured=configured, reachable=False,
                auth_required=False, authenticated=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        latency = int((time.perf_counter() - started) * 1000)
        if health.status_code != 200:
            return CloudStatus(
                url=url, configured=configured, reachable=False,
                auth_required=False, authenticated=False, latency_ms=latency,
                error=f"/api/health 返回 {health.status_code}",
            )

        auth_required = False
        try:
            r = await client.get(f"{url}/api/auth/status")
            auth_required = bool(r.json().get("auth_required")) if r.status_code == 200 else False
        except (httpx.HTTPError, ValueError):
            pass

        version: str | None = None
        authenticated = False
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            r = await client.get(f"{url}/api/version", headers=headers)
            if r.status_code == 200:
                version = str(r.json().get("version") or "") or None
                authenticated = True
        except (httpx.HTTPError, ValueError):
            pass

    return CloudStatus(
        url=url, configured=configured, reachable=True,
        auth_required=auth_required, authenticated=authenticated,
        version=version, latency_ms=latency,
        # 探活通了但拿不到版本，最常见的原因就是缺 token——把这句写清楚，
        # 前端才不用猜「云端到底是不是坏了」。
        error=None if (version or not auth_required) else "云端开了鉴权，但没有可用的 token",
    )


async def _forward(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url, token, _ = cloud_config()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT, trust_env=False) as client:
            r = await client.request(method, f"{url}{path}", headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"连不上云端实例 {url}：{type(exc).__name__}: {exc}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail") or r.text
        except ValueError:
            detail = r.text
        raise HTTPException(status_code=r.status_code, detail=f"云端 {path} 返回 {r.status_code}：{detail}")
    return r.json()


@router.get("/api/cloud/projects", response_model=CloudProjectList)
async def api_cloud_projects() -> CloudProjectList:
    url, _, _ = cloud_config()
    data = await _forward("GET", "/api/projects")
    return CloudProjectList(url=url, projects=data if isinstance(data, list) else [])


@router.get("/api/cloud/workspaces", response_model=CloudWorkspaceList)
async def api_cloud_workspaces() -> CloudWorkspaceList:
    url, _, _ = cloud_config()
    data = await _forward("GET", "/api/workspace/imports")
    return CloudWorkspaceList(
        url=url, root=str(data.get("root") or ""), workspaces=data.get("workspaces") or []
    )


@router.post("/api/cloud/workspaces", response_model=CloudImportResponse)
async def api_cloud_import(data: CloudImportRequest) -> CloudImportResponse:
    url, _, _ = cloud_config()
    remote = await _forward("POST", "/api/workspace/import", data.model_dump(exclude_none=True))
    return CloudImportResponse(url=url, **remote)
