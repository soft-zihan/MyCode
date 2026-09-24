from .sessions import router as sessions_router
from .chat import router as chat_router
from .skills import router as skills_router
from .agents import router as agents_router
from .config import router as config_router
from .workspace import router as workspace_router
from .mcp import router as mcp_router
from .events import router as events_router
from .projects import router as projects_router
from .websocket import router as websocket_router
from .eval import router as eval_router
from .bad_cases import router as bad_cases_router
from .hello import router as hello_router
from .version import router as version_router
from .auth import router as auth_router

__all__ = [
    "sessions_router",
    "chat_router",
    "skills_router",
    "agents_router",
    "config_router",
    "workspace_router",
    "mcp_router",
    "events_router",
    "projects_router",
    "websocket_router",
    "eval_router",
    "bad_cases_router",
    "hello_router",
    "version_router",
    "auth_router",
]
