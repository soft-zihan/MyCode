"""JSON 配置管理模块。

配置文件位置：~/.my-code/config.json
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional


DEFAULT_CONTEXT_WINDOW = 1_000_000
DEFAULT_AUTO_COMPACT_THRESHOLD = 0.80


def config_path() -> Path:
    """配置文件路径：~/.my-code/config.json"""
    return Path.home() / ".my-code" / "config.json"


@dataclass
class ModelEndpointConfig:
    """模型端点配置"""
    model: str
    base_url: str
    api_key: str
    context_window: int = DEFAULT_CONTEXT_WINDOW
    thinking: bool | None = None  # None=跟随模型默认；True/False=显式开/关（qwen 系走 enable_thinking）
    thinking_feedback: bool = False  # True=历史思考以 reasoning_content 回传模型；False=剥离（省 token）
    provider_name: str = ""  # User-friendly name for the provider

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelEndpointConfig:
        return cls(
            model=data.get("model", ""),
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            context_window=data.get("context_window", DEFAULT_CONTEXT_WINDOW),
            thinking=data.get("thinking"),
            thinking_feedback=bool(data.get("thinking_feedback", False)),
            provider_name=data.get("provider_name", ""),
        )


@dataclass
class PlanStrategyConfig:
    """Plan 策略配置"""
    grill_spec: str = "simple"
    tasks: str = "structured"
    execute: str = "direct"
    review: str = "none"
    converge: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanStrategyConfig:
        return cls(
            grill_spec=data.get("grill_spec", "simple"),
            tasks=data.get("tasks", "structured"),
            execute=data.get("execute", "direct"),
            review=data.get("review", "none"),
            converge=data.get("converge", "none"),
        )

    def to_stage_dict(self) -> dict[str, str]:
        """转换为 {stage: name} 格式"""
        return {
            "grill-spec": self.grill_spec,
            "tasks": self.tasks,
            "execute": self.execute,
            "review": self.review,
            "converge": self.converge,
        }


@dataclass
class AppConfig:
    """应用配置"""
    endpoints: dict[str, ModelEndpointConfig]
    routing: dict[str, str]
    cross_session_memory: bool = True
    plan_strategies: PlanStrategyConfig = field(default_factory=PlanStrategyConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoints": {k: v.to_dict() for k, v in self.endpoints.items()},
            "routing": self.routing,
            "cross_session_memory": self.cross_session_memory,
            "plan_strategies": self.plan_strategies.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppConfig:
        endpoints_data = data.get("endpoints", {})
        routing_data = data.get("routing", {})
        strategies_data = data.get("plan_strategies", {})

        endpoints = {
            k: ModelEndpointConfig.from_dict(v)
            for k, v in endpoints_data.items()
        }

        return cls(
            endpoints=endpoints,
            routing=routing_data,
            cross_session_memory=data.get("cross_session_memory", True),
            plan_strategies=PlanStrategyConfig.from_dict(strategies_data),
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
