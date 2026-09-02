"""子代理系统 —— 内置代理类型 + 自定义代理类型的 fork-return 模式。
镜像了 Claude Code 的 AgentTool：explore（只读）、plan（结构化）、general（全量工具），
另外支持通过 .bear/agents/*.md 定义用户自定义代理。"""

from __future__ import annotations

import os
from pathlib import Path

from agents.memory.frontmatter import parse_frontmatter
from agents.tools import tool_definitions, ToolDef


def get_agent_model_ref_env(agent_type: str) -> str:
    """读取 BEAR_MODEL_<TYPE> 环境变量（端点 ID 或裸模型名），未配置返回空串。

    与 model_registry.get_agent_model_ref 行为一致；此处内联以避免循环导入。
    """
    sanitized = "".join(ch.upper() if ch.isalnum() else "_" for ch in agent_type)
    return os.environ.get(f"BEAR_MODEL_{sanitized}", "").strip()

# ─── Read-only tools (for explore and plan agents) ──────────

# explore 子代理只能拿到这几个只读工具，避免它们修改项目文件或系统状态。
READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search"}

# ─── Prompt loading from files ──────────────────────────────

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "subagent"

def _load_subagent_prompt(name: str) -> str:
    """Load subagent prompt from file."""
    prompt_path = _PROMPTS_DIR / f"{name}.txt"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return ""

# ─── Custom agent discovery ─────────────────────────────────

# 自定义代理发现结果的进程内缓存；读取 .bear/agents/*.md 后会复用，避免每次调用 agent 工具都扫目录。
_cached_custom_agents: dict[str, dict] | None = None


def _discover_custom_agents() -> dict[str, dict]:
    """发现用户级和项目级自定义代理，并按代理名称返回配置。"""
    global _cached_custom_agents
    if _cached_custom_agents is not None:
        return _cached_custom_agents

    agents: dict[str, dict] = {}
    # User-level (lower priority)
    _load_agents_from_dir(Path.home() / ".bear" / "agents", agents)
    # Project-level (higher priority, overwrites)
    _load_agents_from_dir(Path.cwd() / ".bear" / "agents", agents)

    _cached_custom_agents = agents
    return agents


def _load_agents_from_dir(directory: Path, agents: dict[str, dict]) -> None:
    """从指定目录读取 Markdown 代理定义，并合并到 agents 字典中。"""
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if not entry.suffix == ".md":
            continue
        try:
            raw = entry.read_text()
            result = parse_frontmatter(raw)
            meta = result.meta
            name = meta.get("name") or entry.stem
            allowed_tools = None
            if "allowed-tools" in meta:
                # allowed-tools 是逗号分隔的工具白名单；缺省时会在 get_sub_agent_config 中开放全部非 agent 工具。
                allowed_tools = [s.strip() for s in meta["allowed-tools"].split(",")]
            agents[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "allowed_tools": allowed_tools,
                "system_prompt": result.body,
                # 自定义代理可在 frontmatter 中声明 model: 字段来指定模型。
                "model": (meta.get("model") or "").strip() or None,
            }
        except Exception:
            # 单个自定义代理文件解析失败时不影响整个程序启动或其他代理加载。
            pass




def get_sub_agent_config(agent_type: str) -> dict:
    """根据代理类型生成 Agent 运行时需要的 system_prompt、tools 和 model_ref 配置。

    model_ref 是"端点 ID 或裸模型名"的引用，真正解析成完整端点在
    agent.py 里通过 model_registry 完成。优先级：
      1. 自定义代理 frontmatter 中的 model: 字段
      2. 环境变量 BEAR_MODEL_<TYPE>
      3. 空串（继承父 Agent 端点）
    """
    # 子智能体不应具备的工具：
    # - agent: 避免递归创建子代理导致控制流复杂化
    # - tool_search: 子智能体没有 MCP 工具，不应搜索 deferred tools
    _sub_agent_excluded = {"agent", "tool_search"}

    custom = _discover_custom_agents().get(agent_type)
    if custom:
        if custom["allowed_tools"]:
            # 自定义代理显式声明工具白名单时，只授予白名单中的工具。
            tools = [t for t in tool_definitions if t["name"] in custom["allowed_tools"]]
        else:
            tools = [t for t in tool_definitions if t["name"] not in _sub_agent_excluded]
        model_ref = custom.get("model") or get_agent_model_ref_env(agent_type)
        return {"system_prompt": custom["system_prompt"], "tools": tools, "model_ref": model_ref}

    # 内置子智能体从文件加载提示词
    model_ref = get_agent_model_ref_env(agent_type)

    if agent_type == "explore":
        read_only = [t for t in tool_definitions if t["name"] in READ_ONLY_TOOLS]
        return {"system_prompt": _load_subagent_prompt("explore"), "tools": read_only, "model_ref": model_ref}
    else:  # general
        return {"system_prompt": _load_subagent_prompt("general"), "tools": [t for t in tool_definitions if t["name"] not in _sub_agent_excluded], "model_ref": model_ref}


# ─── 可用的agent类型(for system prompt) ──────────────


def get_available_agent_types() -> list[dict[str, str]]:
    """返回系统提示词中可展示的全部代理类型说明，包括内置代理和自定义代理。"""
    types = [
        {"name": "explore", "description": "Fast, read-only codebase search and exploration"},
        {"name": "general", "description": "Full tools for independent tasks"},
    ]
    for name, defn in _discover_custom_agents().items():
        types.append({"name": name, "description": defn["description"]})
    return types


def build_agent_descriptions() -> str:
    """把自定义代理类型格式化成 Markdown，供主 Agent 注入到系统提示词中。"""
    types = get_available_agent_types()
    if len(types) <= 3:
        return ""  # Only built-in types, already in system prompt

    custom = types[3:]
    lines = ["\n# Custom Agent Types", ""]
    for t in custom:
        lines.append(f"- **{t['name']}**: {t['description']}")
    return "\n".join(lines)


def reset_agent_cache() -> None:
    """清空自定义代理缓存；测试或运行中刷新 .bear/agents 配置时使用。"""
    global _cached_custom_agents
    _cached_custom_agents = None
