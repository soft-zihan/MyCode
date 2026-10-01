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
class EmbeddingConfig:
    """Embedding 模型配置（wiki 语义召回/编译去重专用）。

    BC-30：本机 ollama 单槽串行 + 冷加载不可靠，召回曾 94s 超时（预算 20s）。
    切换为云端 OpenAI 兼容 /embeddings（默认 SiliconFlow BAAI/bge-large-zh-v1.5（免费），
    实测 ~0.2s/次、1024 维）。backend: openai=OpenAI 兼容 API；ollama=本地。
    """
    backend: str = "openai"
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    model: str = "BAAI/bge-large-zh-v1.5"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmbeddingConfig":
        return cls(
            backend=data.get("backend", "openai"),
            base_url=data.get("base_url", "https://api.siliconflow.cn/v1"),
            api_key=data.get("api_key", ""),
            model=data.get("model", "BAAI/bge-large-zh-v1.5"),
        )


@dataclass
class PlanStrategyConfig:
    """Plan 策略配置。

    只剩两个阶段：`execute`/`review`/`converge` 的内置策略文件与它们唯一的消费者
    （PlanExecutor，注入的是已删除的 `plan_task_*` 协议）都在 Plan 3a Task 4 一起
    删了，设计文档描述的 Review Loop / Converge 本来也没有落成代码。那三个键此前
    仅仅因为本 dataclass 经 `/api/config/plan-strategies` 暴露给前端而留着，Plan 3b
    Task A1 收缩到两阶段。

    旧配置文件里残留的 `execute`/`review`/`converge` 键会被 from_dict 静默忽略——
    它只 get 认识的键、不做严格校验，所以盘上的老 config.json 不需要迁移。
    """
    grill_spec: str = "simple"
    tasks: str = "structured"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanStrategyConfig:
        return cls(
            grill_spec=data.get("grill_spec", "simple"),
            tasks=data.get("tasks", "structured"),
        )


@dataclass
class AppConfig:
    """应用配置"""
    endpoints: dict[str, ModelEndpointConfig]
    routing: dict[str, str]
    plan_strategies: PlanStrategyConfig = field(default_factory=PlanStrategyConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoints": {k: v.to_dict() for k, v in self.endpoints.items()},
            "routing": self.routing,
            "plan_strategies": self.plan_strategies.to_dict(),
            "embedding": self.embedding.to_dict(),
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
            plan_strategies=PlanStrategyConfig.from_dict(strategies_data),
            embedding=EmbeddingConfig.from_dict(data.get("embedding", {})),
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


def get_embedding_config() -> EmbeddingConfig:
    return load_config().embedding


def update_embedding_config(backend: str, base_url: str, model: str, api_key: str | None = None) -> EmbeddingConfig:
    """更新 embedding 配置；api_key=None 表示保留现值（前端不回显密钥）。"""
    config = load_config()
    cfg = config.embedding
    cfg.backend = backend
    cfg.base_url = base_url
    cfg.model = model
    if api_key is not None:
        cfg.api_key = api_key
    save_config(config)
    return cfg


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
