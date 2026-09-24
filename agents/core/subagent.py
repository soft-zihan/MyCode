"""子代理系统 —— 内置代理类型 + 自定义代理类型的 fork-return 模式。
镜像了 Claude Code 的 AgentTool：explore（只读）、plan（结构化）、general（全量工具），
另外支持通过 .mycode/agents/*.md 定义用户自定义代理。"""

from __future__ import annotations

import os
from pathlib import Path

from agents.core.workspace import get_workspace
from agents.core.frontmatter import parse_frontmatter
from agents.tools import tool_definitions, ToolDef


def get_agent_model_ref_env(agent_type: str) -> str:
    """读取 MYCODE_MODEL_<TYPE> 环境变量（端点 ID 或裸模型名），未配置返回空串。

    与 model_registry.get_agent_model_ref 行为一致；此处内联以避免循环导入。
    """
    sanitized = "".join(ch.upper() if ch.isalnum() else "_" for ch in agent_type)
    return os.environ.get(f"MYCODE_MODEL_{sanitized}", "").strip()


def _positive_int_or_none(value: object) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def get_sub_agent_max_tool_calls(agent_type: str, custom_value: object | None = None) -> int | None:
    """解析子 Agent 工具调用预算。

    优先级：环境变量 `MYCODE_<TYPE>_MAX_TOOL_CALLS` > 自定义 frontmatter >
    环境变量 `MYCODE_SUB_AGENT_MAX_TOOL_CALLS` > 内置默认值。
    """
    sanitized = "".join(ch.upper() if ch.isalnum() else "_" for ch in agent_type)
    env_value = _positive_int_or_none(os.environ.get(f"MYCODE_{sanitized}_MAX_TOOL_CALLS", ""))
    if env_value is not None:
        return env_value

    custom_parsed = _positive_int_or_none(custom_value)
    if custom_parsed is not None:
        return custom_parsed

    global_env_value = _positive_int_or_none(os.environ.get("MYCODE_SUB_AGENT_MAX_TOOL_CALLS", ""))
    if global_env_value is not None:
        return global_env_value

    return BUILT_IN_SUB_AGENT_MAX_TOOL_CALLS.get(agent_type, DEFAULT_SUB_AGENT_MAX_TOOL_CALLS)

# ─── Read-only tools (for explore and plan agents) ──────────

# explore 子代理只能拿到这几个只读工具，避免它们修改项目文件或系统状态。
READ_ONLY_TOOLS = {"read_file", "outline_file", "list_files", "grep_search", "web_search"}

DEFAULT_SUB_AGENT_MAX_TOOL_CALLS = 120
BUILT_IN_SUB_AGENT_MAX_TOOL_CALLS = {
    "explore": 80,
    "reviewer": 80,
    "general": 120,
}

# reviewer 子 Agent 工具白名单（只读 + run_shell 执行验收命令）
REVIEWER_TOOLS = {
    "read_file",
    "outline_file",
    "list_files",
    "grep_search",
    "run_shell",  # 仅用于执行 task 定义中的验收命令
}

# ─── Prompt loading from files ──────────────────────────────

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "subagent"

def _load_subagent_prompt(name: str) -> str:
    """Load subagent prompt from file."""
    prompt_path = _PROMPTS_DIR / f"{name}.txt"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    return ""

# ─── Custom agent discovery ─────────────────────────────────

# 自定义代理发现结果按工作区缓存；读取 .mycode/agents/*.md 后会复用，避免每次调用 agent 工具都扫目录。
# 服务器多会话并发时各会话工作区不同，必须按 workspace 键控，否则项目级 agents 串台。
_custom_agents_cache: dict[Path, dict[str, dict]] = {}


def _discover_custom_agents() -> dict[str, dict]:
    """发现用户级和项目级自定义代理，并按代理名称返回配置。"""
    workspace = get_workspace().resolve()
    cached = _custom_agents_cache.get(workspace)
    if cached is not None:
        return cached

    agents: dict[str, dict] = {}
    # User-level (lower priority)
    _load_agents_from_dir(Path.home() / ".mycode" / "agents", agents)
    # Project-level (higher priority, overwrites)
    _load_agents_from_dir(workspace / ".mycode" / "agents", agents)

    _custom_agents_cache[workspace] = agents
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
                "max_tool_calls": meta.get("max-tool-calls"),
            }
        except Exception:
            # 单个自定义代理文件解析失败时不影响整个程序启动或其他代理加载。
            pass




def get_sub_agent_config(agent_type: str) -> dict:
    """根据代理类型生成 Agent 运行时需要的 system_prompt、tools 和 model_ref 配置。

    model_ref 是"端点 ID 或裸模型名"的引用，真正解析成完整端点在
    agent.py 里通过 model_registry 完成。优先级：
      1. 自定义代理 frontmatter 中的 model: 字段
      2. 环境变量 MYCODE_MODEL_<TYPE>
      3. 空串（继承父 Agent 端点）
    """
    # 子智能体不应具备的工具：
    # - agent: 避免递归创建子代理导致控制流复杂化
    _sub_agent_excluded = {"agent"}

    custom = _discover_custom_agents().get(agent_type)
    if custom:
        if custom["allowed_tools"]:
            # 自定义代理显式声明工具白名单时，只授予白名单中的工具。
            tools = [t for t in tool_definitions if t["name"] in custom["allowed_tools"]]
        else:
            tools = [t for t in tool_definitions if t["name"] not in _sub_agent_excluded]
        model_ref = custom.get("model") or get_agent_model_ref_env(agent_type)
        return {
            "system_prompt": custom["system_prompt"],
            "tools": tools,
            "model_ref": model_ref,
            "max_tool_calls": get_sub_agent_max_tool_calls(agent_type, custom.get("max_tool_calls")),
        }

    # 内置子智能体从文件加载提示词
    model_ref = get_agent_model_ref_env(agent_type)
    max_tool_calls = get_sub_agent_max_tool_calls(agent_type)

    if agent_type == "explore":
        read_only = [t for t in tool_definitions if t["name"] in READ_ONLY_TOOLS]
        return {
            "system_prompt": _load_subagent_prompt("explore"),
            "tools": read_only,
            "model_ref": model_ref,
            "max_tool_calls": max_tool_calls,
        }
    elif agent_type == "reviewer":
        reviewer_tools = [t for t in tool_definitions if t["name"] in REVIEWER_TOOLS]
        return {
            "system_prompt": _load_subagent_prompt("reviewer"),
            "tools": reviewer_tools,
            "model_ref": model_ref,
            "max_tool_calls": max_tool_calls,
        }
    else:  # general
        return {
            "system_prompt": _load_subagent_prompt("general"),
            "tools": [t for t in tool_definitions if t["name"] not in _sub_agent_excluded],
            "model_ref": model_ref,
            "max_tool_calls": max_tool_calls,
        }


# ─── 可用的agent类型(for system prompt) ──────────────


def get_available_agent_types() -> list[dict[str, str]]:
    """返回全部代理类型说明（内置 + 自定义）。

    U5a：消费方为 agent 工具的动态 description/enum（每请求重解析，
    v2 subagent.ts "description 即动态 prompt" 模式），不再进 system prompt。
    """
    types = [
        # 双面文案（v2 agent.ts:110-111）：description 写给父代理——路由说明 +
        # thoroughness 三档调用协议；子代理自身的行为约束在 prompts/subagent/explore.txt。
        {"name": "explore", "description": (
            'Fast agent specialized for exploring codebases. Use this when you need to '
            'quickly find files by patterns (eg. "src/components/**/*.tsx"), search code '
            'for keywords (eg. "API endpoints"), or answer questions about the codebase '
            '(eg. "how do API endpoints work?"). When calling this agent, specify the '
            'desired thoroughness level: "quick" for basic searches, "medium" for moderate '
            'exploration, or "very thorough" for comprehensive analysis across multiple '
            'locations and naming conventions.'
        )},
        {"name": "reviewer", "description": "Code review with verification commands"},
        {"name": "general", "description": "Full tools for independent tasks"},
    ]
    for name, defn in _discover_custom_agents().items():
        types.append({"name": name, "description": defn["description"]})
    return types


def reset_agent_cache() -> None:
    """清空自定义代理缓存；测试或运行中刷新 .mycode/agents 配置时使用。"""
    _custom_agents_cache.clear()
