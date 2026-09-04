"""JSON 配置管理模块。

配置文件位置：~/.my-code/config.json
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional


def config_path() -> Path:
    """配置文件路径：~/.my-code/config.json"""
    return Path.home() / ".my-code" / "config.json"


@dataclass
class ModelEndpointConfig:
    """模型端点配置"""
    model: str
    base_url: str
    api_key: str
    context_window: int = 128000
    auto_compact_threshold: float = 0.70
    provider_name: str = ""  # User-friendly name for the provider

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelEndpointConfig:
        return cls(
            model=data.get("model", ""),
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            context_window=data.get("context_window", 128000),
            auto_compact_threshold=data.get("auto_compact_threshold", 0.70),
            provider_name=data.get("provider_name", ""),
        )


@dataclass
class AppConfig:
    """应用配置"""
    endpoints: dict[str, ModelEndpointConfig]
    routing: dict[str, str]
    cross_session_memory: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoints": {k: v.to_dict() for k, v in self.endpoints.items()},
            "routing": self.routing,
            "cross_session_memory": self.cross_session_memory,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        endpoints_data = data.get("endpoints", {})
        routing_data = data.get("routing", {})

        endpoints = {
            k: ModelEndpointConfig.from_dict(v)
            for k, v in endpoints_data.items()
        }

        return cls(
            endpoints=endpoints,
            routing=routing_data,
            cross_session_memory=data.get("cross_session_memory", True),
        )


def load_config() -> AppConfig:
    """加载配置文件，不存在则返回空配置"""
    path = config_path()
    if not path.exists():
        return AppConfig(
            endpoints={},
            routing={},
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # 配置文件损坏，返回空配置
        return AppConfig(
            endpoints={},
            routing={},
        )


def save_config(config: AppConfig) -> None:
    """保存配置到文件"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_agent_model_ref(agent_type: str) -> str:
    """获取指定 agent 类型的模型引用"""
    config = load_config()
    return config.routing.get(agent_type, "")


def get_endpoint_by_model(model_name: str) -> Optional[ModelEndpointConfig]:
    """根据模型名查找端点配置"""
    config = load_config()

    # 查 endpoints
    for endpoint in config.endpoints.values():
        if endpoint.model == model_name:
            return endpoint

    return None


def list_endpoints() -> list[dict[str, Any]]:
    """列出所有端点配置"""
    config = load_config()
    result = []

    for endpoint_id, endpoint in config.endpoints.items():
        result.append({
            "id": endpoint_id,
            "model": endpoint.model,
            "base_url": endpoint.base_url,
            "has_api_key": bool(endpoint.api_key),
            "protocol": "openai",
        })

    return result


def get_primary_endpoint() -> dict[str, Any]:
    """获取主端点配置"""
    config = load_config()
    primary_id = config.routing.get("primary", "")
    if primary_id and primary_id in config.endpoints:
        endpoint = config.endpoints[primary_id]
        return {
            "model": endpoint.model,
            "base_url": endpoint.base_url,
            "has_api_key": bool(endpoint.api_key),
        }
    # Fallback: return first endpoint
    if config.endpoints:
        first_id = next(iter(config.endpoints))
        endpoint = config.endpoints[first_id]
        return {
            "model": endpoint.model,
            "base_url": endpoint.base_url,
            "has_api_key": bool(endpoint.api_key),
        }
    return {"model": "", "base_url": "", "has_api_key": False}
