"""/graph 命令 —— code-review-graph 代码图谱管理。

MyCode 通过 MCP（见 `.mcp.json`）接入外部 code-review-graph（CRG），
让模型获得符号搜索 / 调用关系 / 影响面分析能力。本模块提供 `/graph`
REPL 命令，用 CRG 的 CLI 显式触发图谱构建 / 增量更新 / 状态查看。

为什么走 CLI 而不是 MCP 的 `build_or_update_graph_tool`：
- MCP 工具调用没有超时保护（见 mcp_client.py 的 call_tool），
  图谱全量构建可能把对话循环挂死；
- CLI 命令在 MCP 连接失败时依然可用，也方便人工排障。

子命令：
- /graph build     全量构建（首次使用）
- /graph update    增量更新（只重解析变化的文件）
- /graph status    查看图谱统计
- /graph           显示帮助
"""

from __future__ import annotations

import shutil

from .tools import _run_shell

# CRG CLI 可执行文件名（pip install code-review-graph 提供）。
CRG_COMMAND = "code-review-graph"

# 各子命令的超时（毫秒）。全量构建在大仓库上较慢，给足时间。
_TIMEOUTS = {"build": 120000, "update": 60000, "status": 15000}

# 子命令 → CRG CLI 参数。status 用 --json 让输出紧凑、机器可读。
_SUBCOMMANDS = {
    "build": ["build"],
    "update": ["update"],
    "status": ["status", "--json"],
}

_HELP = (
    "代码图谱（code-review-graph）命令：\n"
    "  /graph build    全量构建代码图谱（首次，约 10 秒/500 文件）\n"
    "  /graph update   增量更新（只重解析变化的文件）\n"
    "  /graph status   查看图谱统计\n"
    "\n"
    "构建后模型可自动使用 mcp__code-review-graph__query_graph_tool 等 MCP 工具，\n"
    "做符号搜索、调用关系查询、变更影响面分析。\n"
    "若本次会话是首次构建，需重启会话才能刷新 MCP 工具列表。"
)

_INSTALL_HINT = (
    "未找到 code-review-graph CLI，请先安装：\n"
    "  pip install code-review-graph\n"
    "安装后重新运行 /graph build"
)


def run_graph_command(arg: str) -> str:
    """执行 /graph 子命令，返回要显示的文本。

    Args:
        arg: "/graph" 之后的参数（可为空字符串）。

    Returns:
        帮助文本、安装提示，或 CRG CLI 的执行输出。
    """
    sub = arg.strip()
    if not sub or sub in ("help", "-h", "--help"):
        return _HELP
    if sub not in _SUBCOMMANDS:
        return f"Unknown subcommand: {sub}\n\n{_HELP}"
    if shutil.which(CRG_COMMAND) is None:
        return _INSTALL_HINT

    command = " ".join([CRG_COMMAND, *_SUBCOMMANDS[sub]])
    result = _run_shell({"command": command, "timeout": _TIMEOUTS[sub]})

    # 构建/更新后提醒重启：MCP 工具列表在会话启动时一次性加载
    # （agent.py chat() 里 _mcp_initialized 只置一次），首次构建发生在
    # 启动之后，模型要重启才能看到图谱工具。
    if sub in ("build", "update"):
        result += (
            "\n\n提示：MCP 工具列表在会话启动时加载，若本会话首次构建图谱，"
            "需重启会话（exit 后重进）模型才能使用图谱工具。"
        )
    return result
