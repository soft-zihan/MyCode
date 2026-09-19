"""Config and model management APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.config import (
    load_config, save_config, list_endpoints, get_primary_endpoint,
    AppConfig, ModelEndpointConfig
)

router = APIRouter(tags=["config"])


class EndpointConfig(BaseModel):
    model: str
    base_url: str
    api_key: str
    context_window: int = 128000
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
            "Authorization": f"mycodeer {data.api_key}",
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
    config = AppConfig(
        endpoints={
            k: ModelEndpointConfig(
                model=v.model,
                base_url=v.base_url,
                api_key=v.api_key,
                context_window=v.context_window,
                provider_name=v.provider_name,
            )
            for k, v in data.endpoints.items()
        },
        routing=data.routing,
    )
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
