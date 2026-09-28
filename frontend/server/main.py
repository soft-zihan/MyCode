#!/usr/bin/env python3
"""FastAPI server that wraps existing CLI modules to provide HTTP API for frontend."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from routers import (
    sessions_router,
    chat_router,
    skills_router,
    agents_router,
    config_router,
    workspace_router,
    mcp_router,
    events_router,
    projects_router,
    worktrees_router,
    websocket_router,
    eval_router,
    bad_cases_router,
    hello_router,
    version_router,
    auth_router,
    artifacts_router,
)

# Import global MCP manager from dedicated module
from frontend.server.mcp_manager import global_mcp_manager
from frontend.server import auth

app = FastAPI(title="MyCode API", version="1.0.0")


@app.middleware("http")
async def token_auth_middleware(request, call_next):
    """U13：MYCODE_AUTH_TOKEN 设置后，/api（豁免面除外）必须带 Bearer token。"""
    if auth.auth_enabled() and not auth.is_exempt_path(request.url.path):
        if not auth.verify_bearer(request.headers.get("authorization")):
            # U9：<img> 标签无法携带 Authorization header——artifacts 只读通道
            # 允许 query token（与 WS 握手同一信任模型：静态 token 走 query，
            # 直接暴露仅限受信内网/配合 TLS 反代）。
            if not (request.url.path.startswith("/api/artifacts/")
                    and auth.verify_query_token(request.query_params.get("token"))):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://localhost:8090"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions_router)
app.include_router(chat_router)
app.include_router(skills_router)
app.include_router(agents_router)
app.include_router(config_router)
app.include_router(workspace_router)
app.include_router(mcp_router)
app.include_router(events_router)
# worktrees 必须先于 projects 注册：/api/projects/{cwd:path} 是贪婪路由，
# 会吞掉 /api/projects/<cwd>/worktrees（FastAPI 按注册顺序匹配）
app.include_router(worktrees_router)
app.include_router(projects_router)
app.include_router(websocket_router)
app.include_router(eval_router)
app.include_router(bad_cases_router)
app.include_router(hello_router)
app.include_router(version_router)
app.include_router(auth_router)
app.include_router(artifacts_router)


@app.on_event("startup")
async def startup_event():
    """Initialize observability + MCP connections at startup."""
    import asyncio
    import json
    from pathlib import Path

    # Set workspace to project root for MCP config loading
    from agents.core.workspace import set_workspace, get_workspace
    project_root = Path(__file__).parent.parent.parent  # frontend/server -> frontend -> project root
    set_workspace(project_root)
    print(f"[STARTUP] Workspace set to: {get_workspace()}")

    # U6 崩溃恢复：启动扫描——悬挂会话标记 interrupted（保守版，不自动续跑）
    try:
        from agents.core.session_crash_recovery import scan_and_mark_interrupted
        repaired = scan_and_mark_interrupted()
        if repaired:
            ids = [r["session_id"] for r in repaired]
            print(f"[STARTUP] Crash recovery: 标记 {len(repaired)} 个悬挂会话 interrupted: {ids}")
    except Exception as e:
        print(f"[STARTUP] Crash recovery scan failed (non-fatal): {e!r}")

    # 可观测性初始化（OTel → Langfuse，失败不阻塞主流程）
    try:
        from agents.observability import init_tracing
        init_tracing()
        print("[STARTUP] Observability initialized")
    except Exception as e:
        print(f"[STARTUP] Observability init failed (non-fatal): {e}")

    # Load disabled servers from config
    disabled_servers = set()
    config_path = Path.home() / ".mycode" / "config" / "disabled_mcp_servers.json"
    if config_path.exists():
        try:
            disabled_servers = set(json.loads(config_path.read_text()))
        except Exception:
            pass
    
    try:
        # Connect only enabled servers
        print(f"[STARTUP] Calling load_and_connect with disabled: {disabled_servers}")
        await asyncio.wait_for(global_mcp_manager.load_and_connect(disabled_servers), timeout=30.0)
        print(f"[STARTUP] MCP initialized: {len(global_mcp_manager._tools)} tools from {len(global_mcp_manager._connections)} servers")
        for name in global_mcp_manager._connections:
            tool_count = len([t for t in global_mcp_manager._tools if t["serverName"] == name])
            print(f"[STARTUP]   - {name}: {tool_count} tools")
    except asyncio.TimeoutError:
        print(f"[STARTUP] MCP initialization timed out")
    except Exception as e:
        print(f"[STARTUP] MCP initialization failed: {e}")
        import traceback
        traceback.print_exc()


@app.on_event("shutdown")
async def shutdown_event():
    """Flush traces and cleanup MCP connections at shutdown."""
    # U6：先优雅收口活跃会话（abort → 限时等待 → 合成 turn/end{shutdown}）
    try:
        from agents.core.session_crash_recovery import shutdown_active_sessions
        closed = await shutdown_active_sessions()
        if closed:
            print(f"[SHUTDOWN] Force-closed sessions: {closed}")
    except Exception as e:
        print(f"[SHUTDOWN] Session shutdown failed: {e!r}")
    try:
        from agents.observability import shutdown_tracing
        shutdown_tracing()
        print("[SHUTDOWN] Traces flushed")
    except Exception as e:
        print(f"[SHUTDOWN] Trace flush failed: {e}")
    await global_mcp_manager.disconnect_all()
    print("[SHUTDOWN] MCP connections closed")


@app.get("/api/health")
def api_health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    host = auth.get_bind_host()
    if auth.auth_enabled():
        print(f"[auth] token 鉴权已启用（MYCODE_AUTH_TOKEN），绑定 {host}:5555")
    elif host != "127.0.0.1":
        print(f"[auth] 警告：绑定 {host}:5555 且未设 MYCODE_AUTH_TOKEN——网络可达者即可驱动 agent，"
              f"仅限受信网络；远程访问首选 SSH 隧道（ssh -L 5555:localhost:5555）")
    uvicorn.run(app, host=host, port=5555, ws_max_size=10*1024*1024)  # 10MB
