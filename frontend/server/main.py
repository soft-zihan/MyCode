#!/usr/bin/env python3
"""FastAPI server that wraps existing CLI modules to provide HTTP API for frontend."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化可观测性与 MCP，关闭时收口会话并 flush trace。"""
    import asyncio
    import json

    # Set workspace to project root for MCP config loading
    from agents.core.workspace import set_workspace, get_workspace
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

    yield

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


app = FastAPI(title="MyCode API", version="1.0.0", lifespan=lifespan)


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


@app.get("/api/health")
def api_health() -> dict[str, str]:
    return {"status": "ok"}


# ── 生产态静态托管 ──────────────────────────────────────────────────────────
# frontend/dist 存在时由后端直接托管构建产物，无需再跑 Vite dev server
# （dev server 只适合本地开发：无压缩、带 HMR 开销、且固定绑 0.0.0.0）。
# 必须注册在所有 router 之后——Starlette 按注册顺序匹配，否则会吞掉 /api。
_FRONTEND_DIST = (project_root / "frontend" / "dist").resolve()

if _FRONTEND_DIST.is_dir():
    if (_FRONTEND_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_FRONTEND_DIST / "assets"), name="assets")

    # include_in_schema=False：frontend/openapi.json 是前后端契约单源，
    # 这个 catch-all 不属于 API 面，不能让它污染契约。
    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa_fallback(spa_path: str):
        """SPA 路由回退：命中真实文件则返回该文件，否则返回 index.html。"""
        # /api 下的未知路径必须仍返回 JSON 404，否则 API 客户端会收到一坨 HTML
        if spa_path == "api" or spa_path.startswith("api/"):
            raise HTTPException(status_code=404)
        if spa_path:
            candidate = (_FRONTEND_DIST / spa_path).resolve()
            # 防目录穿越：解析后仍须落在 dist 内
            if candidate.is_file() and candidate.is_relative_to(_FRONTEND_DIST):
                return FileResponse(candidate)
        return FileResponse(_FRONTEND_DIST / "index.html")

    print(f"[static] 托管前端构建产物: {_FRONTEND_DIST}")


if __name__ == "__main__":
    import uvicorn
    host = auth.get_bind_host()
    if auth.auth_enabled():
        print(f"[auth] token 鉴权已启用（MYCODE_AUTH_TOKEN），绑定 {host}:5555")
    elif host != "127.0.0.1":
        print(f"[auth] 警告：绑定 {host}:5555 且未设 MYCODE_AUTH_TOKEN——网络可达者即可驱动 agent，"
              f"仅限受信网络；远程访问首选 SSH 隧道（ssh -L 5555:localhost:5555）")
    uvicorn.run(app, host=host, port=5555, ws_max_size=10*1024*1024)  # 10MB
