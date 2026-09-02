"""MCP server management APIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["mcp"])

project_root = Path(__file__).parent.parent.parent.parent


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
    from agents.tools.mcp import McpManager

    manager = McpManager()
    try:
        await manager.load_and_connect()
        tools = manager.get_tool_definitions()
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
                "input_schema": t.get("input_schema", {}),
            })
        return [
            {"server": name, "tools": tlist, "tool_count": len(tlist)}
            for name, tlist in server_tools.items()
        ]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        await manager.disconnect_all()


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
