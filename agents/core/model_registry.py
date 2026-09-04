from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

# 端点环境变量前缀：MYCODE_ENDPOINT_<ID>_<FIELD>
_ENDPOINT_PREFIX = "MYCODE_ENDPOINT_"
_FIELDS = ("BASE_URL", "API_KEY", "MODEL")


@dataclass(frozen=True)
class ModelEndpoint:
    """一个完整的模型端点：模型名 + base url + api key。"""

    model: str
    base_url: str | None
    api_key: str | None
    use_openai: bool = True


def discover_endpoints() -> dict[str, ModelEndpoint]:
    """扫描环境变量，返回 {端点ID(小写): ModelEndpoint}。

    缺少 BASE_URL 的端点视为不完整，直接跳过。
    """
    endpoints: dict[str, ModelEndpoint] = {}
    ids: set[str] = set()
    for key in os.environ:
        if not key.startswith(_ENDPOINT_PREFIX):
            continue
        rest = key[len(_ENDPOINT_PREFIX):]
        for field in _FIELDS:
            suffix = "_" + field
            if rest.endswith(suffix):
                ids.add(rest[: -len(suffix)])
                break

    for raw_id in ids:
        if not raw_id:
            continue
        base = os.environ.get(f"{_ENDPOINT_PREFIX}{raw_id}_BASE_URL", "").strip()
        if not base:
            continue  # 不完整端点
        api_key = os.environ.get(f"{_ENDPOINT_PREFIX}{raw_id}_API_KEY", "").strip() or None
        model = os.environ.get(f"{_ENDPOINT_PREFIX}{raw_id}_MODEL", "").strip()
        endpoints[raw_id.lower()] = ModelEndpoint(
            model=model,
            base_url=base,
            api_key=api_key,
            use_openai=True,
        )
    return endpoints


def _env_key_for_agent_type(agent_type: str) -> str:
    """explore -> MYCODE_MODEL_EXPLORE；my-agent -> MYCODE_MODEL_MY_AGENT。"""
    sanitized = "".join(ch.upper() if ch.isalnum() else "_" for ch in agent_type)
    return f"MYCODE_MODEL_{sanitized}"


def get_agent_model_ref(agent_type: str) -> str:
    """读取某类子 Agent 配置的模型名（MYCODE_MODEL_<TYPE>），未配置返回空串。"""
    return os.environ.get(_env_key_for_agent_type(agent_type), "").strip()


def resolve_agent_endpoint(
    agent_type: str,
    *,
    model_ref: str,
    primary: ModelEndpoint,
) -> ModelEndpoint:
    """把模型引用解析成完整端点。选择入口是**模型名**，不是端点 ID。

    解析顺序：
    1. model_ref 为空 → 继承主端点
    2. model_ref 匹配某个已注册端点的 MODEL（按模型名反查）→ 该端点全套
       url/key/model。这是主要用法：子 Agent / 自定义 agent frontmatter 里
       直接写模型名，注册表自动找到提供该模型的 API。
    3. model_ref 恰好等于某个端点 ID（兼容写法）→ 该端点
    4. 都不匹配 → 当作裸模型名，沿用主端点 url/key 仅换模型名
       （配置错误时 API 会报 model not found，便于暴露问题）
    """
    ref = (model_ref or "").strip()
    if not ref:
        return primary

    endpoints = discover_endpoints()

    # 按模型名反查：哪个端点提供这个模型
    for ep in endpoints.values():
        if ep.model and ep.model.lower() == ref.lower():
            return ep

    # 兼容：直接写端点 ID
    hit = endpoints.get(ref.lower())
    if hit is not None:
        if hit.model:
            return hit
        return ModelEndpoint(
            model=primary.model,
            base_url=hit.base_url,
            api_key=hit.api_key,
            use_openai=hit.use_openai,
        )

    # 裸模型名：沿用主端点连接信息
    return ModelEndpoint(
        model=ref,
        base_url=primary.base_url,
        api_key=primary.api_key,
        use_openai=primary.use_openai,
    )


def resolve_side_endpoint(*, primary: ModelEndpoint) -> ModelEndpoint:
    """解析 side query（记忆召回/折叠/skill 进化）使用的端点。

    MYCODE_SIDE_MODEL 支持端点 ID 或裸模型名；未配置时用主端点。
    """
    ref = os.environ.get("MYCODE_SIDE_MODEL", "").strip()
    if not ref:
        return primary
    return resolve_agent_endpoint("side", model_ref=ref, primary=primary)
