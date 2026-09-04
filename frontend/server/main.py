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
    memories_router,
    skills_router,
    agents_router,
    config_router,
    workspace_router,
    trace_router,
    mcp_router,
    events_router,
    projects_router,
    websocket_router,
)

app = FastAPI(title="MyCode API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://localhost:8090"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions_router)
app.include_router(chat_router)
app.include_router(memories_router)
app.include_router(skills_router)
app.include_router(agents_router)
app.include_router(config_router)
app.include_router(workspace_router)
app.include_router(trace_router)
app.include_router(mcp_router)
app.include_router(events_router)
app.include_router(projects_router)
app.include_router(websocket_router)


@app.get("/api/health")
def api_health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5555, ws_max_size=10*1024*1024)  # 10MB
