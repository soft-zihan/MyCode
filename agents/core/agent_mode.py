"""Agent Mode System - Agent 模式系统。

定义 Agent 的三种运行模式：
- primary: 主 Agent，拥有完整权限
- subagent: 子 Agent，权限受限（按类型区分）
- all: 全权限模式（用于测试或特殊场景）

Hidden Agent 是特殊的 primary Agent，用于后台任务（压缩、标题、摘要）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

_SIDE_QUERY_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "side_query"


def _load_side_query_prompt(filename: str) -> str:
    """从 prompts/side_query/ 加载提示词（单一来源，供 registry/override 复用）。"""
    return (_SIDE_QUERY_PROMPTS_DIR / filename).read_text(encoding="utf-8")


class AgentMode(str, Enum):
    """Agent 运行模式。"""
    PRIMARY = "primary"
    SUBAGENT = "subagent"
    ALL = "all"


@dataclass
class AgentConfig:
    """Agent 配置。
    
    Attributes:
        name: Agent 名称
        mode: 运行模式
        hidden: 是否为隐藏 Agent（不在 UI 显示）
        description: Agent 描述
        system_prompt: 系统提示词
        allowed_tools: 允许使用的工具列表（None 表示全部）
        max_turns: 最大轮次限制
    """
    
    name: str
    mode: AgentMode = AgentMode.PRIMARY
    hidden: bool = False
    description: str = ""
    system_prompt: str = ""
    allowed_tools: list[str] | None = None
    max_turns: int | None = None


# ============================================================
# 内置 Hidden Agent 定义
# ============================================================


BUILTIN_HIDDEN_AGENTS: dict[str, AgentConfig] = {
    # 会话标题生成（U5a：v2 plugin/agent.ts 模式——task/rules/examples + few-shot + 防抱怨 + 语言跟随）
    "side_query_title": AgentConfig(
        name="side_query_title",
        mode=AgentMode.PRIMARY,
        hidden=True,
        description="生成会话标题",
        system_prompt=_load_side_query_prompt("generate_title.txt"),
    ),
    # Side Query: Wiki 选择
    "side_query_wiki": AgentConfig(
        name="side_query_wiki",
        mode=AgentMode.PRIMARY,
        hidden=True,
        description="Side Query: 选择相关 Wiki 条目",
        system_prompt="根据用户消息和文件名，选择最相关的 Wiki 条目。",
    ),
    # Side Query: 压缩 - 工具折叠
    "side_query_tool_fold": AgentConfig(
        name="side_query_tool_fold",
        mode=AgentMode.PRIMARY,
        hidden=True,
        description="Side Query: 工具结果折叠摘要",
        system_prompt="为工具调用结果生成简洁的摘要。",
    ),
    # Side Query: 压缩 - 会话笔记编译
    "side_query_compile": AgentConfig(
        name="side_query_compile",
        mode=AgentMode.PRIMARY,
        hidden=True,
        description="Side Query: 编译会话笔记和项目知识",
        system_prompt="从对话历史中提取会话笔记和项目知识。",
    ),
    # Side Query: 技能提取
    "side_query_skill": AgentConfig(
        name="side_query_skill",
        mode=AgentMode.PRIMARY,
        hidden=True,
        description="Side Query: 技能提取和评估",
        system_prompt="从对话中提取可复用的技能模式。",
    ),
    # Side Query: 目标提取
    "side_query_goal": AgentConfig(
        name="side_query_goal",
        mode=AgentMode.PRIMARY,
        hidden=True,
        description="Side Query: 目标标准提取",
        system_prompt="从用户目标中提取验收标准。",
    ),
}


# ============================================================
# Agent 类型注册表
# ============================================================


class AgentRegistry:
    """Agent 类型注册表。
    
    管理所有可用的 Agent 类型（包括自定义 Agent）。
    """
    
    def __init__(self) -> None:
        self._agents: dict[str, AgentConfig] = {}
        self._register_builtins()
    
    def _register_builtins(self) -> None:
        """注册内置 Agent。"""
        for name, config in BUILTIN_HIDDEN_AGENTS.items():
            self._agents[name] = config
    
    def register(self, config: AgentConfig) -> None:
        """注册 Agent 配置。"""
        self._agents[config.name] = config
    
    def get(self, name: str) -> AgentConfig | None:
        """获取 Agent 配置。"""
        return self._agents.get(name)
    
    def list_visible(self) -> list[AgentConfig]:
        """列出可见的 Agent（排除 hidden）。"""
        return [c for c in self._agents.values() if not c.hidden]
    
    def list_all(self) -> list[AgentConfig]:
        """列出所有 Agent。"""
        return list(self._agents.values())
    
    def is_hidden(self, name: str) -> bool:
        """检查 Agent 是否为 hidden。"""
        config = self._agents.get(name)
        return config.hidden if config else False


# 全局注册表实例
_agent_registry = AgentRegistry()


def get_agent_registry() -> AgentRegistry:
    """获取全局 Agent 注册表。"""
    return _agent_registry


def get_hidden_agent_config(name: str) -> AgentConfig | None:
    """获取 Hidden Agent 配置。"""
    config = _agent_registry.get(name)
    if config and config.hidden:
        return config
    return None


def get_visible_agents() -> list[AgentConfig]:
    """获取可见的 Agent 列表。"""
    return _agent_registry.list_visible()


def register_agent(config: AgentConfig) -> None:
    """注册自定义 Agent。"""
    _agent_registry.register(config)
