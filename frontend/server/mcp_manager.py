"""Global MCP manager singleton to avoid circular imports."""

from agents.tools.mcp import McpManager

# Global MCP manager instance - shared across all routers
global_mcp_manager = McpManager()
