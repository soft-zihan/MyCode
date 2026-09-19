"""MCP server management APIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["mcp"])

project_root = Path(__file__).parent.parent.parent.parent
_config_path = Path.home() / ".mycode" / "config" / "disabled_mcp_servers.json"


@router.get("/api/config/disabled-mcp-servers")
def api_get_disabled_mcp_servers() -> list[str]:
    """Get list of disabled MCP server names."""
    if _config_path.exists():
        try:
            return json.loads(_config_path.read_text())
        except Exception:
            pass
    return []


@router.put("/api/config/disabled-mcp-servers")
def api_set_disabled_mcp_servers(servers: list[str]) -> dict[str, str]:
    """Save list of disabled MCP server names."""
    _config_path.parent.mkdir(parents=True, exist_ok=True)
    _config_path.write_text(json.dumps(servers))
    return {"status": "ok"}


@router.get("/api/mcp")
def api_list_mcp_servers() -> list[dict[str, Any]]:
    mcp_config_path = project_root / ".mcp.json"
    if not mcp_config_path.exists():
        return []
    try:
        config = json.loads(mcp_config_path.read_text())
        servers = config.get("mcpServers", {})
        return [
            {
                "name": name,
                "command": server.get("command", ""),
                "args": server.get("args", []),
                "env": server.get("env", {}),
            }
            for name, server in servers.items()
        ]
    except Exception:
        return []


@router.get("/api/mcp/tools")
async def api_list_mcp_tools() -> list[dict[str, Any]]:
    from frontend.server.mcp_manager import global_mcp_manager

    try:
        tools = global_mcp_manager.get_tool_definitions()
        server_tools: dict[str, list[dict]] = {}
        for t in tools:
            parts = t["name"].split("__", 2)
            server_name = parts[1] if len(parts) >= 2 else "unknown"
            tool_name = parts[2] if len(parts) >= 3 else t["name"]
            if server_name not in server_tools:
                server_tools[server_name] = []
            server_tools[server_name].append({
                "name": tool_name,
                "full_name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema") or t.get("inputSchema") or {"type": "object", "properties": {}},
            })
        return [
            {"server": name, "tools": tlist, "tool_count": len(tlist)}
            for name, tlist in server_tools.items()
        ]
    except Exception as e:
        return [{"error": str(e)}]


@router.get("/api/mcp/status")
def api_get_mcp_status() -> list[dict[str, Any]]:
    """获取所有 MCP Server 的状态（包括启用/禁用）。"""
    from frontend.server.mcp_manager import global_mcp_manager
    return global_mcp_manager.get_all_servers_status()


@router.post("/api/mcp/{server_name}/enable")
async def api_enable_mcp_server(server_name: str) -> dict[str, Any]:
    """动态启用一个 MCP Server。"""
    from frontend.server.mcp_manager import global_mcp_manager
    success = await global_mcp_manager.enable_server(server_name)
    if success:
        disabled = api_get_disabled_mcp_servers()
        if server_name in disabled:
            disabled.remove(server_name)
            api_set_disabled_mcp_servers(disabled)
    return {"success": success, "server": server_name}


@router.post("/api/mcp/{server_name}/disable")
async def api_disable_mcp_server(server_name: str) -> dict[str, Any]:
    """动态禁用一个 MCP Server。"""
    from frontend.server.mcp_manager import global_mcp_manager
    success = await global_mcp_manager.disable_server(server_name)
    if success:
        disabled = api_get_disabled_mcp_servers()
        if server_name not in disabled:
            disabled.append(server_name)
            api_set_disabled_mcp_servers(disabled)
    return {"success": success, "server": server_name}


@router.get("/api/tools/native")
def api_list_native_tools() -> list[dict[str, Any]]:
    from agents.tools import tool_definitions
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("input_schema", {}),
        }
        for t in tool_definitions
    ]
