"""Agents management APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agents.core.subagent import get_sub_agent_config, _discover_custom_agents, get_available_agent_types, get_agent_model_ref_env
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
    allowed_tools: list[str] | None = None
    model: str | None = None


class CustomAgentUpdate(BaseModel):
    description: str | None = None
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    model: str | None = None


@router.get("/api/tools")
def api_list_tools() -> list[dict[str, Any]]:
    """List all available tools."""
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "deferred": t.get("deferred", False),
        }
        for t in tool_definitions
    ]


@router.get("/api/agents")
def api_list_agents() -> list[dict[str, Any]]:
    agents = []
    custom_agents = _discover_custom_agents()
    
    for agent_info in get_available_agent_types():
        name = agent_info["name"]
        description = agent_info["description"]
        model_ref = get_agent_model_ref_env(name)
        is_custom = name in custom_agents
        custom_config = custom_agents.get(name, {})
        
        agents.append({
            "name": name,
            "description": description,
            "model_ref": model_ref,
            "is_custom": is_custom,
            "allowed_tools": custom_config.get("allowed_tools"),
            "has_system_prompt": bool(custom_config.get("system_prompt")),
        })
    
    return agents


@router.get("/api/agents/{agent_name}")
def api_get_agent(agent_name: str) -> dict[str, Any]:
    custom_agents = _discover_custom_agents()
    is_custom = agent_name in custom_agents
    
    try:
        config = get_sub_agent_config(agent_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Agent not found: {e}")
    
    return {
        "name": agent_name,
        "is_custom": is_custom,
        "model_ref": config.get("model_ref", ""),
        "tools_count": len(config.get("tools", [])),
        "tools": [t["name"] for t in config.get("tools", [])],
        "system_prompt_preview": config.get("system_prompt", ""),
        "custom_config": custom_agents.get(agent_name, {}),
    }


@router.get("/api/agents/{agent_name}/prompt")
def api_get_agent_prompt(agent_name: str) -> dict[str, Any]:
    custom_agents = _discover_custom_agents()
    
    if agent_name in custom_agents:
        return {"prompt": custom_agents[agent_name].get("system_prompt", "")}
    
    try:
        config = get_sub_agent_config(agent_name)
        return {"prompt": config.get("system_prompt", "")}
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Agent not found: {e}")


@router.put("/api/agents/{agent_name}/prompt")
def api_update_agent_prompt(agent_name: str, data: PromptUpdate) -> dict[str, Any]:
    custom_agents = _discover_custom_agents()
    
    if agent_name in custom_agents:
        import frontmatter
        
        for base_dir in [Path.home() / ".bear" / "agents", Path.cwd() / ".bear" / "agents"]:
            agent_file = base_dir / f"{agent_name}.md"
            if agent_file.exists():
                try:
                    post = frontmatter.load(str(agent_file))
                    post.content = data.prompt
                    frontmatter.dump(post, str(agent_file))
                    return {"status": "ok", "message": f"Prompt updated for {agent_name}"}
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Failed to save prompt: {e}")
        
        raise HTTPException(status_code=404, detail=f"Custom agent file not found for '{agent_name}'")
    
    prompt_file = project_root / "agents" / "prompts" / "subagent" / f"{agent_name}.txt"
    if prompt_file.exists():
        try:
            prompt_file.write_text(data.prompt, encoding="utf-8")
            return {"status": "ok", "message": f"Prompt updated for {agent_name}"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to save prompt: {e}")
    
    raise HTTPException(status_code=404, detail=f"Built-in agent prompt file not found for '{agent_name}'")


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
    
    # Create agent file in user's home directory
    agents_dir = Path.home() / ".bear" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agents_dir / f"{data.name}.md"
    
    # Build frontmatter
    metadata = {
        "name": data.name,
        "description": data.description,
    }
    if data.allowed_tools:
        metadata["allowed-tools"] = ", ".join(data.allowed_tools)
    if data.model:
        metadata["model"] = data.model
    
    # Create post with frontmatter
    post = frontmatter.Post(data.system_prompt, **metadata)
    
    try:
        frontmatter.dump(post, str(agent_file))
        return {"status": "ok", "message": f"Custom agent '{data.name}' created.", "path": str(agent_file)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {e}")


@router.put("/api/agents/{agent_name}")
def api_update_custom_agent(agent_name: str, data: CustomAgentUpdate) -> dict[str, Any]:
    """Update a custom sub-agent's configuration."""
    import frontmatter
    
    custom_agents = _discover_custom_agents()
    if agent_name not in custom_agents:
        raise HTTPException(status_code=404, detail=f"Custom agent '{agent_name}' not found.")
    
    # Find the agent file
    agent_file = None
    for base_dir in [Path.home() / ".bear" / "agents", Path.cwd() / ".bear" / "agents"]:
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
        return {"status": "ok", "message": f"Agent '{agent_name}' updated."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update agent: {e}")


@router.delete("/api/agents/{agent_name}")
def api_delete_custom_agent(agent_name: str) -> dict[str, Any]:
    """Delete a custom sub-agent."""
    custom_agents = _discover_custom_agents()
    if agent_name not in custom_agents:
        raise HTTPException(status_code=404, detail=f"Custom agent '{agent_name}' not found.")
    
    # Find and delete the agent file
    for base_dir in [Path.home() / ".bear" / "agents", Path.cwd() / ".bear" / "agents"]:
        agent_file = base_dir / f"{agent_name}.md"
        if agent_file.exists():
            try:
                agent_file.unlink()
                return {"status": "ok", "message": f"Agent '{agent_name}' deleted."}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to delete agent: {e}")
    
    raise HTTPException(status_code=404, detail=f"Agent file not found for '{agent_name}'")


@router.get("/api/prompts")
def api_list_prompts() -> list[dict[str, Any]]:
    prompts = []
    
    system_path = _PROMPTS_DIR / "system.txt"
    if system_path.exists():
        prompts.append({
            "id": "system",
            "name": "System Prompt",
            "description": "主系统提示词，定义Agent的核心行为和工具使用规范",
            "path": "system.txt",
            "category": "system",
        })
    
    subagent_dir = _PROMPTS_DIR / "subagent"
    if subagent_dir.exists():
        for f in sorted(subagent_dir.glob("*.txt")):
            prompts.append({
                "id": f"subagent/{f.stem}",
                "name": f"Subagent: {f.stem}",
                "description": f"{f.stem} 子智能体的系统提示词",
                "path": f"subagent/{f.name}",
                "category": "subagent",
            })
    
    hidden_dir = _PROMPTS_DIR / "hidden"
    if hidden_dir.exists():
        for f in sorted(hidden_dir.glob("*.txt")):
            desc_map = {
                "compaction": "上下文压缩专用提示词",
                "title": "生成对话标题的提示词",
                "summary": "生成对话摘要的提示词",
            }
            prompts.append({
                "id": f"hidden/{f.stem}",
                "name": f"Hidden: {f.stem}",
                "description": desc_map.get(f.stem, f"{f.stem} 隐藏智能体的提示词"),
                "path": f"hidden/{f.name}",
                "category": "hidden",
            })
    
    memory_dir = _PROMPTS_DIR / "memory"
    if memory_dir.exists():
        for f in sorted(memory_dir.glob("*.txt")):
            prompts.append({
                "id": f"memory/{f.stem}",
                "name": f"Memory: {f.stem}",
                "description": "记忆系统相关提示词",
                "path": f"memory/{f.name}",
                "category": "memory",
            })
    
    session_dir = _PROMPTS_DIR / "session"
    if session_dir.exists():
        for f in sorted(session_dir.glob("*.txt")):
            prompts.append({
                "id": f"session/{f.stem}",
                "name": f"Session: {f.stem}",
                "description": "会话管理相关提示词",
                "path": f"session/{f.name}",
                "category": "session",
            })
    
    goal_dir = _PROMPTS_DIR / "goal"
    if goal_dir.exists():
        for f in sorted(goal_dir.glob("*.txt")):
            prompts.append({
                "id": f"goal/{f.stem}",
                "name": f"Goal: {f.stem}",
                "description": "目标系统相关提示词",
                "path": f"goal/{f.name}",
                "category": "goal",
            })
    
    return prompts


@router.get("/api/prompts/{prompt_id:path}")
def api_get_prompt(prompt_id: str) -> dict[str, Any]:
    if ".." in prompt_id or prompt_id.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid prompt ID")
    
    prompt_path = _PROMPTS_DIR / prompt_id
    if not prompt_path.exists():
        raise HTTPException(status_code=404, detail=f"Prompt not found: {prompt_id}")
    
    try:
        content = prompt_path.read_text(encoding="utf-8")
        return {
            "id": prompt_id,
            "content": content,
            "path": str(prompt_path.relative_to(_PROMPTS_DIR)),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read prompt: {e}")


@router.put("/api/prompts/{prompt_id:path}")
def api_update_prompt(prompt_id: str, data: PromptContentUpdate) -> dict[str, Any]:
    if ".." in prompt_id or prompt_id.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid prompt ID")
    
    prompt_path = _PROMPTS_DIR / prompt_id
    if not prompt_path.exists():
        raise HTTPException(status_code=404, detail=f"Prompt not found: {prompt_id}")
    
    try:
        prompt_path.write_text(data.content, encoding="utf-8")
        return {"status": "ok", "message": f"Prompt saved: {prompt_id}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save prompt: {e}")
