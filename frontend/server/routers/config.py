"""Config and model management APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.config import (
    DEFAULT_CONTEXT_WINDOW,
    load_config, save_config, list_endpoints, get_primary_endpoint,
    AppConfig, ModelEndpointConfig
)

router = APIRouter(tags=["config"])


class EndpointConfig(BaseModel):
    model: str
    base_url: str
    api_key: str
    context_window: int = DEFAULT_CONTEXT_WINDOW
    thinking: bool | None = None  # null=跟随模型默认
    provider_name: str = ""


class ConfigUpdate(BaseModel):
    endpoints: dict[str, EndpointConfig]
    routing: dict[str, str]


class ModelVerifyRequest(BaseModel):
    base_url: str
    api_key: str
    model: str


@router.get("/api/endpoints")
def api_list_endpoints() -> list[dict[str, Any]]:
    return list_endpoints()


@router.get("/api/endpoints/primary")
def api_get_primary_endpoint() -> dict[str, Any]:
    return get_primary_endpoint()


@router.post("/api/models/verify")
async def api_verify_model(data: ModelVerifyRequest) -> dict[str, Any]:
    import httpx
    
    try:
        chat_url = f"{data.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {data.api_key}",
            "Content-Type": "application/json"
        }
        
        test_payload = {
            "model": data.model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
            "stream": False
        }
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(chat_url, headers=headers, json=test_payload)
            
            if response.status_code == 200:
                return {
                    "status": "success",
                    "message": f"Model '{data.model}' is accessible and working",
                    "model_found": True
                }
            elif response.status_code == 401:
                return {
                    "status": "error",
                    "message": "Authentication failed - invalid API key"
                }
            elif response.status_code == 404:
                return {
                    "status": "error",
                    "message": f"Model '{data.model}' not found or endpoint not available"
                }
            else:
                try:
                    error_data = response.json()
                    error_msg = error_data.get('error', {}).get('message', response.text[:200])
                except:
                    error_msg = response.text[:200]
                
                return {
                    "status": "error",
                    "message": f"API returned HTTP {response.status_code}: {error_msg}"
                }
    
    except httpx.TimeoutException:
        return {
            "status": "error",
            "message": "Connection timeout - API endpoint not reachable"
        }
    except httpx.ConnectError as e:
        return {
            "status": "error",
            "message": f"Connection failed: {str(e)}"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Verification failed: {str(e)}"
        }


@router.get("/api/config")
def api_get_config() -> dict[str, Any]:
    config = load_config()
    return config.to_dict()


@router.put("/api/config")
def api_update_config(data: ConfigUpdate) -> dict[str, Any]:
    config = load_config()
    config.endpoints = {
        k: ModelEndpointConfig(
            model=v.model,
            base_url=v.base_url,
            api_key=v.api_key,
            context_window=v.context_window,
            thinking=v.thinking,
            provider_name=v.provider_name,
        )
        for k, v in data.endpoints.items()
    }
    config.routing = data.routing
    save_config(config)
    return {"status": "ok", "message": "Config saved successfully"}


@router.get("/api/config/plan-strategies")
def api_get_plan_strategies() -> dict[str, Any]:
    """获取 Plan 策略配置"""
    config = load_config()
    return config.plan_strategies.to_dict()


@router.put("/api/config/plan-strategies")
def api_update_plan_strategies(data: dict[str, str]) -> dict[str, Any]:
    """更新 Plan 策略配置"""
    from agents.config import PlanStrategyConfig
    config = load_config()
    config.plan_strategies = PlanStrategyConfig(
        grill_spec=data.get("grill_spec", config.plan_strategies.grill_spec),
        tasks=data.get("tasks", config.plan_strategies.tasks),
        execute=data.get("execute", config.plan_strategies.execute),
        review=data.get("review", config.plan_strategies.review),
        converge=data.get("converge", config.plan_strategies.converge),
    )
    save_config(config)
    return {"status": "ok", "message": "Plan strategies saved successfully"}


# ── Embedding 模型配置（BC-30：wiki 语义召回/编译去重的 embedding 后端） ──


class EmbeddingConfigUpdate(BaseModel):
    backend: str = "openai"          # openai=OpenAI 兼容 API；ollama=本地
    base_url: str = "https://api.siliconflow.cn/v1"
    model: str = "BAAI/bge-large-zh-v1.5"
    api_key: str | None = None       # None=保留现值（GET 不回显密钥）


class EmbeddingVerifyRequest(BaseModel):
    backend: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str | None = None       # None=用已保存的密钥验证


@router.get("/api/embedding-config")
def api_get_embedding_config() -> dict[str, Any]:
    from agents.config import get_embedding_config
    cfg = get_embedding_config()
    # 密钥不回显，只报是否已配置
    return {
        "backend": cfg.backend,
        "base_url": cfg.base_url,
        "model": cfg.model,
        "api_key_set": bool(cfg.api_key),
    }


@router.post("/api/embedding-config")
def api_update_embedding_config(data: EmbeddingConfigUpdate) -> dict[str, Any]:
    from agents.config import update_embedding_config
    update_embedding_config(data.backend, data.base_url, data.model, data.api_key)
    return {"status": "ok", "message": "Embedding config saved successfully"}


@router.post("/api/embedding-config/verify")
async def api_verify_embedding_config(data: EmbeddingVerifyRequest) -> dict[str, Any]:
    """真实调用一次 embeddings API 验证连通性，返回维度与延迟。"""
    import time

    import httpx

    from agents.config import get_embedding_config

    cfg = get_embedding_config()
    backend = data.backend or cfg.backend
    base_url = (data.base_url or cfg.base_url).rstrip("/")
    model = data.model or cfg.model
    api_key = data.api_key if data.api_key is not None else cfg.api_key

    if backend == "ollama":
        url = "http://localhost:11434/api/embeddings"
        headers: dict[str, str] = {}
        payload = {"model": model, "prompt": "verify"}
        extract = lambda d: d["embedding"]  # noqa: E731
    else:
        url = f"{base_url}/embeddings"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {"model": model, "input": "verify"}
        extract = lambda d: d["data"][0]["embedding"]  # noqa: E731

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        return {"status": "error", "message": f"请求失败: {type(exc).__name__}: {exc}"}

    latency_ms = round((time.time() - t0) * 1000)
    if resp.status_code == 200:
        try:
            dim = len(extract(resp.json()))
        except (KeyError, IndexError, TypeError):
            return {"status": "error", "message": f"响应格式异常: {resp.text[:200]}"}
        return {"status": "success", "message": f"连接成功（{latency_ms}ms）", "dim": dim, "latency_ms": latency_ms}
    if resp.status_code == 401:
        return {"status": "error", "message": "鉴权失败——API key 无效"}
    try:
        msg = resp.json().get("error", {}).get("message") or resp.text[:200]
    except Exception:
        msg = resp.text[:200]
    return {"status": "error", "message": f"HTTP {resp.status_code}: {msg}"}
