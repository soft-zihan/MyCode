"""Settings 系统 — YAML 配置替代硬编码常量。

参考：projects/llm-wiki-memory/scripts/lib/settings.mjs
"""

from __future__ import annotations

import yaml
from pathlib import Path
from typing import Any

from agents.wiki.wiki_manager import get_wiki_dir


_SETTINGS_CACHE: dict[str, Any] | None = None
_SETTINGS_MTIME: float = 0


def get_settings_path() -> Path:
    """获取 settings.yaml 路径。"""
    return get_wiki_dir() / ".settings" / "settings.yaml"


def load_settings() -> dict[str, Any]:
    """加载配置。"""
    global _SETTINGS_CACHE, _SETTINGS_MTIME

    path = get_settings_path()
    if not path.exists():
        return get_default_settings()

    mtime = path.stat().st_mtime
    if _SETTINGS_CACHE is not None and mtime == _SETTINGS_MTIME:
        return _SETTINGS_CACHE

    try:
        data = yaml.safe_load(path.read_text())
        _SETTINGS_CACHE = data if data else get_default_settings()
        _SETTINGS_MTIME = mtime
    except Exception:
        _SETTINGS_CACHE = get_default_settings()

    return _SETTINGS_CACHE


def get_default_settings() -> dict[str, Any]:
    """默认配置。"""
    return {
        "recall": {
            "scoreThreshold": 0.12,
            "priorityBand": 0.05,
        },
        "embed": {
            "model": "qwen3-embedding",
            "backend": "ollama",
            "chunk": {
                "enabled": True,
                "maxChunks": 10,
                "penalty": 0.1,
                "fullMaxChunks": 20,
                "fullPenalty": 0.0,
            },
        },
        "consolidate": {
            "staleAfterMonths": 6,
            "maxRefreshPerRun": 25,
            "dedupCosineThreshold": 0.9,
        },
        "cold": {
            "maxCold": 50,
        },
    }


def get_setting(path: str, default: Any = None) -> Any:
    """获取配置值。

    path 格式：section.key.subkey
    """
    settings = load_settings()
    keys = path.split(".")
    value = settings
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default
    return value


def save_settings(settings: dict[str, Any]) -> None:
    """保存配置。"""
    global _SETTINGS_CACHE, _SETTINGS_MTIME

    path = get_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(settings, default_flow_style=False, allow_unicode=True))

    _SETTINGS_CACHE = settings
    _SETTINGS_MTIME = path.stat().st_mtime
