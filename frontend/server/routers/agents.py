"""Agents management APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.core.subagent import get_sub_agent_config, _discover_custom_agents, get_available_agent_types, get_agent_model_ref_env, reset_agent_cache
from agents.core.workspace import get_workspace
from agents.core.agent_mode import BUILTIN_HIDDEN_AGENTS
from agents.tools.registry import tool_definitions

router = APIRouter(tags=["agents"])

project_root = Path(__file__).parent.parent.parent.parent
_PROMPTS_DIR = project_root / "agents" / "prompts"


class PromptUpdate(BaseModel):
    prompt: str


class PromptContentUpdate(BaseModel):
    content: str


class CustomAgentCreate(BaseModel):
    name: str
    description: str
    system_prompt: str
    allowed_tools: Optional[list[str]] = None
    model: Optional[str] = None


class CustomAgentUpdate(BaseModel):
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    model: Optional[str] = None


@router.get("/api/tools")
def api_list_tools() -> list[dict[str, Any]]:
    """List all native tools."""
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "deferred": t.get("deferred", False),
            "input_schema": t.get("input_schema", {}),
            "source": "native",
        }
        for t in tool_definitions
    ]


@router.get("/api/tools/all")
def api_list_all_tools() -> list[dict[str, Any]]:
    """List all tools including MCP tools."""
    from frontend.server.mcp_manager import global_mcp_manager
    
    tools = [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "deferred": t.get("deferred", False),
            "input_schema": t.get("input_schema", {}),
            "source": "native",
        }
        for t in tool_definitions
    ]
    
    try:
        mcp_tools = global_mcp_manager.get_tool_definitions()
        for t in mcp_tools:
            tools.append({
                "name": t["name"],
                "description": t.get("description", ""),
                "deferred": False,
                "input_schema": t.get("input_schema", {}),
                "source": "mcp",
            })
    except Exception:
        pass
    
    return tools


@router.get("/api/agents")
def api_list_agents() -> list[dict[str, Any]]:
    """List all agents: primary, sub, hidden, and custom."""
    agents = []
    custom_agents = _discover_custom_agents()
    
    # Primary agents (build, plan)
    primary_agents = [
        {"name": "build", "description": "Default agent with full tool access", "category": "primary"},
        {"name": "plan", "description": "Read-only planning agent for structured task analysis", "category": "primary"},
    ]
    
    for agent_info in primary_agents:
        name = agent_info["name"]
        description = agent_info["description"]
        model_ref = get_agent_model_ref_env(name)
        is_custom = name in custom_agents
        custom_config = custom_agents.get(name, {})
        
        has_override = (Path.home() / ".mycode" / "agents" / f"{name}.md").exists()
        
        # Load prompt for primary agents
        prompt_path = _PROMPTS_DIR / "subagent" / f"{name}.txt"
        has_system_prompt = prompt_path.exists() or has_override
        
        agents.append({
            "name": name,
            "description": description,
            "model_ref": model_ref,
            "is_custom": is_custom,
            "has_override": has_override,
            "allowed_tools": custom_config.get("allowed_tools"),
            "has_system_prompt": has_system_prompt,
            "category": "primary",
        })
    
    # Sub agents (explore, general)
    for agent_info in get_available_agent_types():
        name = agent_info["name"]
        if name in {"build", "plan"}:
            continue  # Skip primary agents
        
        description = agent_info["description"]
        model_ref = get_agent_model_ref_env(name)
        is_custom = name in custom_agents
        custom_config = custom_agents.get(name, {})
        
        has_override = (Path.home() / ".mycode" / "agents" / f"{name}.md").exists()
        
        agents.append({
            "name": name,
            "description": description,
            "model_ref": model_ref,
            "is_custom": is_custom,
            "has_override": has_override,
            "allowed_tools": custom_config.get("allowed_tools"),
            "has_system_prompt": bool(custom_config.get("system_prompt")) or (Path.home() / ".mycode" / "agents" / f"{name}.md").exists(),
            "category": "sub",
        })
    
    # Hidden agents (title, compaction, summary)
    for name, config in BUILTIN_HIDDEN_AGENTS.items():
        model_ref = get_agent_model_ref_env(name)
        is_custom = name in custom_agents
        custom_config = custom_agents.get(name, {})
        
        has_override = (Path.home() / ".mycode" / "agents" / f"{name}.md").exists()
        
        agents.append({
            "name": name,
            "description": config.description,
            "model_ref": model_ref,
            "is_custom": is_custom,
            "has_override": has_override,
            "allowed_tools": custom_config.get("allowed_tools"),
            "has_system_prompt": bool(config.system_prompt) or has_override,
            "category": "hidden",
        })
    
    return agents


@router.get("/api/agents/{agent_name}")
def api_get_agent(agent_name: str) -> dict[str, Any]:
    """Get agent details including prompt and tools."""
    custom_agents = _discover_custom_agents()
    is_custom = agent_name in custom_agents
    
    # Check if it's a hidden agent
    hidden_config = BUILTIN_HIDDEN_AGENTS.get(agent_name)
    
    # Check if it's a primary agent
    primary_names = {"build", "plan"}
    
    try:
        if hidden_config:
            # Hidden agent
            config = {
                "system_prompt": hidden_config.system_prompt,
                "tools": [],  # Hidden agents don't use tools
                "model_ref": get_agent_model_ref_env(agent_name),
            }
        elif agent_name in primary_names:
            # Primary agent - load from file
            # For build agent, use system.txt; for plan agent, use plan.txt
            if agent_name == "build":
                prompt_path = _PROMPTS_DIR / "system.txt"
            else:
                prompt_path = _PROMPTS_DIR / "subagent" / f"{agent_name}.txt"
            
            system_prompt = ""
            if prompt_path.exists():
                system_prompt = prompt_path.read_text(encoding="utf-8")
            
            # Check for override
            override_path = Path.home() / ".mycode" / "agents" / f"{agent_name}.md"
            if override_path.exists():
                from agents.memory.frontmatter import parse_frontmatter
                raw = override_path.read_text()
                result = parse_frontmatter(raw)
                system_prompt = result.body
            
            config = {
                "system_prompt": system_prompt,
                "tools": tool_definitions,  # Primary agents have all tools
                "model_ref": get_agent_model_ref_env(agent_name),
            }
        else:
            # Sub agent or custom agent
            config = get_sub_agent_config(agent_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Agent not found: {e}")
    
    has_override = (Path.home() / ".mycode" / "agents" / f"{agent_name}.md").exists()
    
    # Determine category
    if agent_name in primary_names:
        category = "primary"
    elif agent_name in BUILTIN_HIDDEN_AGENTS:
        category = "hidden"
    else:
        category = "sub"
    
    return {
        "name": agent_name,
        "is_custom": is_custom,
        "has_override": has_override,
        "model_ref": config.get("model_ref", ""),
        "tools_count": len(config.get("tools", [])),
        "tools": [t["name"] for t in config.get("tools", [])],
        "system_prompt_preview": config.get("system_prompt", ""),
        "custom_config": custom_agents.get(agent_name, {}),
        "category": category,
    }


@router.get("/api/agents/{agent_name}/prompt")
def api_get_agent_prompt(agent_name: str) -> dict[str, Any]:
    """Get agent's system prompt."""
    custom_agents = _discover_custom_agents()
    
    # Check custom agents first
    if agent_name in custom_agents:
        return {"prompt": custom_agents[agent_name].get("system_prompt", "")}
    
    # Check hidden agents
    hidden_config = BUILTIN_HIDDEN_AGENTS.get(agent_name)
    if hidden_config:
        # Check for override
        override_path = Path.home() / ".mycode" / "agents" / f"{agent_name}.md"
        if override_path.exists():
            from agents.memory.frontmatter import parse_frontmatter
            raw = override_path.read_text()
            result = parse_frontmatter(raw)
            return {"prompt": result.body}
        return {"prompt": hidden_config.system_prompt}
    
    # Check primary agents
    primary_names = {"build", "plan"}
    if agent_name in primary_names:
        # Check for override first
        override_path = Path.home() / ".mycode" / "agents" / f"{agent_name}.md"
        if override_path.exists():
            from agents.memory.frontmatter import parse_frontmatter
            raw = override_path.read_text()
            result = parse_frontmatter(raw)
            return {"prompt": result.body}
        
        # For build agent, use system.txt; for plan agent, use plan.txt
        if agent_name == "build":
            prompt_file = _PROMPTS_DIR / "system.txt"
        else:
            prompt_file = _PROMPTS_DIR / "subagent" / f"{agent_name}.txt"
        
        if prompt_file.exists():
            return {"prompt": prompt_file.read_text(encoding="utf-8")}
    
    # Check sub agents
    prompt_file = _PROMPTS_DIR / "subagent" / f"{agent_name}.txt"
    if prompt_file.exists():
        return {"prompt": prompt_file.read_text(encoding="utf-8")}
    
    raise HTTPException(status_code=404, detail=f"Agent not found: {agent_name}")


@router.put("/api/agents/{agent_name}/prompt")
def api_update_agent_prompt(agent_name: str, data: PromptUpdate) -> dict[str, Any]:
    """Update agent's system prompt."""
    import frontmatter
    
    # All agents can be overridden via ~/.mycode/agents/{name}.md
    agents_dir = Path.home() / ".mycode" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / f"{agent_name}.md"
    
    # Determine category for metadata
    primary_names = {"build", "plan"}
    if agent_name in primary_names:
        category = "primary"
    elif agent_name in BUILTIN_HIDDEN_AGENTS:
        category = "hidden"
    else:
        category = "sub"
    
    metadata = {
        "name": agent_name,
        "category": category,
    }
    
    # Add description if it's a known agent
    if agent_name in primary_names:
        metadata["description"] = f"Custom override for {agent_name} primary agent"
    elif agent_name in BUILTIN_HIDDEN_AGENTS:
        metadata["description"] = BUILTIN_HIDDEN_AGENTS[agent_name].description
    else:
        metadata["description"] = f"Custom {agent_name} agent"
    
    post = frontmatter.Post(data.prompt, **metadata)
    
    try:
        frontmatter.dump(post, str(agent_file))
        reset_agent_cache()
        return {"status": "ok", "message": f"Prompt saved for '{agent_name}'"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save prompt: {e}")


@router.post("/api/agents")
def api_create_custom_agent(data: CustomAgentCreate) -> dict[str, Any]:
    """Create a new custom sub-agent."""
    import frontmatter
    
    # Validate name
    if not data.name or not data.name.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid agent name. Use only letters, numbers, hyphens, and underscores.")
    
    # Check if agent already exists
    custom_agents = _discover_custom_agents()
    if data.name in custom_agents:
        raise HTTPException(status_code=400, detail=f"Agent '{data.name}' already exists.")
    
    # Also check built-in names
    builtin_names = {"build", "plan", "explore", "general"} | set(BUILTIN_HIDDEN_AGENTS.keys())
    if data.name in builtin_names:
        raise HTTPException(status_code=400, detail=f"Agent name '{data.name}' is reserved.")
    
    # Create agent file in user's home directory
    agents_dir = Path.home() / ".mycode" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / f"{data.name}.md"
    
    # Build frontmatter
    metadata = {
        "name": data.name,
        "description": data.description,
        "category": "custom",
    }
    if data.allowed_tools:
        metadata["allowed-tools"] = ", ".join(data.allowed_tools)
    if data.model:
        metadata["model"] = data.model
    
    # Create post with frontmatter
    post = frontmatter.Post(data.system_prompt, **metadata)
    
    try:
        frontmatter.dump(post, str(agent_file))
        reset_agent_cache()
        return {"status": "ok", "message": f"Custom agent '{data.name}' created.", "path": str(agent_file)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {e}")


@router.put("/api/agents/{agent_name}")
def api_update_custom_agent(agent_name: str, data: CustomAgentUpdate) -> dict[str, Any]:
    """Update a custom agent's configuration."""
    import frontmatter
    
    custom_agents = _discover_custom_agents()
    if agent_name not in custom_agents:
        raise HTTPException(status_code=404, detail=f"Custom agent '{agent_name}' not found.")
    
    # Find the agent file
    agent_file = None
    for base_dir in [Path.home() / ".mycode" / "agents", get_workspace() / ".mycode" / "agents"]:
        path = base_dir / f"{agent_name}.md"
        if path.exists():
            agent_file = path
            break
    
    if not agent_file:
        raise HTTPException(status_code=404, detail=f"Agent file not found for '{agent_name}'")
    
    try:
        post = frontmatter.load(str(agent_file))
        
        if data.description is not None:
            post.metadata["description"] = data.description
        if data.system_prompt is not None:
            post.content = data.system_prompt
        if data.allowed_tools is not None:
            if data.allowed_tools:
                post.metadata["allowed-tools"] = ", ".join(data.allowed_tools)
            elif "allowed-tools" in post.metadata:
                del post.metadata["allowed-tools"]
        if data.model is not None:
            if data.model:
                post.metadata["model"] = data.model
            elif "model" in post.metadata:
                del post.metadata["model"]
        
        frontmatter.dump(post, str(agent_file))
        reset_agent_cache()
        return {"status": "ok", "message": f"Agent '{agent_name}' updated."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update agent: {e}")


@router.delete("/api/agents/{agent_name}")
def api_delete_custom_agent(agent_name: str) -> dict[str, Any]:
    """Delete a custom agent or reset a built-in agent's override."""
    custom_agents = _discover_custom_agents()
    builtin_names = {"build", "plan", "explore", "general"} | set(BUILTIN_HIDDEN_AGENTS.keys())
    
    if agent_name in builtin_names:
        override_file = Path.home() / ".mycode" / "agents" / f"{agent_name}.md"
        if override_file.exists():
            try:
                override_file.unlink()
                reset_agent_cache()
                return {"status": "ok", "message": f"Override reset for built-in agent '{agent_name}'"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to reset override: {e}")
        raise HTTPException(status_code=404, detail=f"No override found for built-in agent '{agent_name}'")
    
    if agent_name not in custom_agents:
        raise HTTPException(status_code=404, detail=f"Custom agent '{agent_name}' not found.")
    
    # Find and delete the agent file
    for base_dir in [Path.home() / ".mycode" / "agents", get_workspace() / ".mycode" / "agents"]:
        agent_file = base_dir / f"{agent_name}.md"
        if agent_file.exists():
            try:
                agent_file.unlink()
                reset_agent_cache()
                return {"status": "ok", "message": f"Agent '{agent_name}' deleted."}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to delete agent: {e}")
    
    raise HTTPException(status_code=404, detail=f"Agent file not found for '{agent_name}'")


# ============================================================
# 提示词管理 API
# ============================================================


@router.get("/api/prompts")
def api_list_prompts() -> list[dict[str, Any]]:
    """列出所有提示词摘要。"""
    from agents.core.prompt_registry import get_prompt_summary
    return get_prompt_summary()


@router.get("/api/prompts/{prompt_name}")
def api_get_prompt(prompt_name: str) -> dict[str, Any]:
    """获取指定提示词详情。"""
    from agents.core.prompt_registry import get_prompt
    prompt = get_prompt(prompt_name)
    if not prompt:
        raise HTTPException(status_code=404, detail=f"Prompt not found: {prompt_name}")
    return {
        "name": prompt.name,
        "category": prompt.category,
        "description": prompt.description,
        "source": prompt.source,
        "editable": prompt.editable,
        "content": prompt.content,
    }


class PromptSaveRequest(BaseModel):
    content: str


@router.put("/api/prompts/{prompt_name}")
def api_save_prompt(prompt_name: str, req: PromptSaveRequest) -> dict[str, Any]:
    """保存提示词。"""
    from agents.core.prompt_registry import save_prompt
    if not save_prompt(prompt_name, req.content):
        raise HTTPException(status_code=400, detail=f"Cannot save prompt: {prompt_name}")
    return {"status": "ok", "message": f"Prompt '{prompt_name}' saved."}
