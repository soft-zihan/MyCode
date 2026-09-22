"""LOCA-bench 侧进程（运行于 LOCA venv）：env 生命周期 + MCP 配置生成 + 评分。

职责边界：只 import LOCA 仓库（gem.*），不 import MyCode；与 MyCode agent 通过
子进程 + 文件协议交换（loca_task.json / agent_result.json / loca_eval.json）。

用法（由 eval/loca/runner.py 以 LOCA venv 解释器调起）：
    python loca_side.py run-task --loca-repo <repo> --config-set 64k --index 0 \
        --task-dir <dir> --agent-cmd '["..."]' [--timeout 1800] [--max-tool-uses 100]
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _dynamic_import(class_path: str) -> Any:
    module_path, class_name = class_path.rsplit(".", 1)
    return getattr(importlib.import_module(module_path), class_name)


def _legacy_stdio_config(server_type: str, params: dict) -> dict:
    """build_server_config 无 YAML 时的旧版 helper 回退链（与上游 run_claude_agent 一致）。"""
    pkg = "gem.tools.mcp_server"
    table = {
        "canvas": ("canvas.helper", "get_canvas_stdio_config"),
        "email": ("emails.helper", "get_email_stdio_config"),
        "emails": ("emails.helper", "get_email_stdio_config"),
        "excel": ("excel.helper", "get_excel_stdio_config"),
        "python_execute": ("python_execute.helper", "get_python_execute_stdio_config"),
        "programmatic_tool_calling": ("programmatic_tool_calling.helper", "get_programmatic_tool_calling_stdio_config"),
        "programmatic-tool-calling": ("programmatic_tool_calling.helper", "get_programmatic_tool_calling_stdio_config"),
        "claim_done": ("claim_done.helper", "get_claim_done_stdio_config"),
        "memory": ("memory.helper", "get_memory_stdio_config"),
        "memory_tool": ("memory_tool.helper", "get_memory_tool_stdio_config"),
        "memory-tool": ("memory_tool.helper", "get_memory_tool_stdio_config"),
        "filesystem": ("filesystem.helper", "get_filesystem_stdio_config"),
        "terminal": ("terminal.helper", "get_terminal_stdio_config"),
        "google_cloud": ("google_cloud.helper", "get_google_cloud_stdio_config"),
        "google_sheet": ("google_sheet.helper", "get_google_sheet_stdio_config"),
        "google-sheet": ("google_sheet.helper", "get_google_sheet_stdio_config"),
        "pdf_tools": ("pdf_tools.helper", "get_pdf_tools_stdio_config"),
        "pdf-tools": ("pdf_tools.helper", "get_pdf_tools_stdio_config"),
        "calendar": ("calendar_server.helper", "get_calendar_stdio_config"),
        "woocommerce": ("woocommerce.helper", "get_woocommerce_stdio_config"),
        "snowflake": ("snowflake.helper", "get_snowflake_stdio_config"),
    }
    if server_type not in table:
        raise ValueError(f"Unknown MCP server type: {server_type}")
    mod, fn = table[server_type]
    helper = getattr(importlib.import_module(f"{pkg}.{mod}"), fn)
    return helper(**params)


def setup_mcp_servers(
    mcp_configs: dict[str, Any],
    task_workspace: Path,
    agent_workspace: Path,
    loca_repo: Path,
) -> dict[str, Any]:
    """构建 stdio MCP 配置，并做跨 venv 绝对化。

    上游 build_server_config 产出裸 `python` 命令与相对脚本路径（依赖 cwd=LOCA 仓库），
    而实际 spawn 方是 MyCode agent（本项目 venv、任意 cwd），因此：
    - command == "python" → LOCA venv 解释器绝对路径（本进程 sys.executable）
    - args[0] 相对脚本路径 → 相对 LOCA 仓库根的绝对路径
    """
    from gem.tools.mcp_server.config_loader import build_server_config

    config: dict[str, Any] = {"mcpServers": {}}
    for server_name, server_config in mcp_configs.items():
        if not server_config.get("enabled", True):
            continue
        server_type = server_config.get("type")
        params = dict(server_config.get("params", {}))
        for key, value in params.items():
            if isinstance(value, str):
                params[key] = (
                    value.replace("{task_workspace}", str(task_workspace))
                    .replace("{agent_workspace}", str(agent_workspace))
                )
        params["task_workspace"] = str(task_workspace)
        params["agent_workspace"] = str(agent_workspace)
        try:
            server_cfg = build_server_config(
                server_type=server_type, params=params, server_name=server_name
            )
        except FileNotFoundError:
            server_cfg = _legacy_stdio_config(server_type, params)
        config["mcpServers"].update(server_cfg)

    for _name, sc in config["mcpServers"].items():
        if sc.get("command") == "python":
            sc["command"] = sys.executable
        args = list(sc.get("args") or [])
        if args and isinstance(args[0], str) and args[0].endswith(".py") and not Path(args[0]).is_absolute():
            candidate = loca_repo / args[0]
            if candidate.exists():
                args[0] = str(candidate.resolve())
        sc["args"] = args
    return config


def cmd_run_task(a: argparse.Namespace) -> int:
    loca_repo = Path(a.loca_repo).resolve()
    sys.path.insert(0, str(loca_repo))
    os.chdir(loca_repo)  # env/资源内含相对路径，与上游 runner 行为一致

    cfg_file = loca_repo / "task-configs" / f"final_{a.config_set}_set_config.json"
    entry = json.loads(cfg_file.read_text(encoding="utf-8"))["configurations"][a.index]

    task_dir = Path(a.task_dir).resolve()
    agent_ws = task_dir / "agent_workspace"
    for d in (task_dir / "local_db", agent_ws, agent_ws / "memory"):
        d.mkdir(parents=True, exist_ok=True)

    env_params: dict[str, Any] = {}
    for key, value in (entry.get("env_params") or {}).items():
        if isinstance(value, str):
            value = value.replace("{task_workspace}", str(task_dir)).replace(
                "{agent_workspace}", str(agent_ws)
            )
        env_params[key] = value
    env_params.setdefault("task_dir", str(task_dir))
    env_params.setdefault("seed", 42)  # 确定性：评分侧重建 env 依赖同 seed

    t0 = time.time()
    env = _dynamic_import(entry["env_class"])(**env_params)
    mcp_config = setup_mcp_servers(entry.get("mcp_servers") or {}, task_dir, agent_ws, loca_repo)

    from gem.tools.mcp_tool import MCPTool
    from gem.tools.tool_env_wrapper import ToolEnvWrapperOpenAI

    tool = MCPTool(mcp_config, validate_on_init=False)
    wrapped = ToolEnvWrapperOpenAI(env, tools=[tool], max_tool_uses=a.max_tool_uses)
    _obs, _info, user_prompt, _tools = wrapped.reset()

    (agent_ws / ".mcp.json").write_text(
        json.dumps(mcp_config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (task_dir / "loca_task.json").write_text(
        json.dumps(
            {
                "name": entry.get("name"),
                "index": a.index,
                "config_set": a.config_set,
                "env_class": entry["env_class"],
                "env_params": env_params,
                "user_prompt": user_prompt,
                "agent_workspace": str(agent_ws),
                "max_tool_uses": a.max_tool_uses,
                "prepare_duration_s": round(time.time() - t0, 2),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[loca_side] prepared {entry.get('name')} idx={a.index} prompt={len(user_prompt)} chars", flush=True)

    agent_cmd = json.loads(a.agent_cmd)
    agent_rc: int | None = None
    agent_error: str | None = None
    try:
        proc = subprocess.run(
            agent_cmd,
            cwd=str(agent_ws),
            timeout=a.timeout if a.timeout > 0 else None,
            capture_output=True,
            text=True,
        )
        agent_rc = proc.returncode
        if proc.returncode != 0:
            agent_error = (proc.stderr or proc.stdout or "")[-2000:]
    except subprocess.TimeoutExpired:
        agent_error = f"agent timeout after {a.timeout}s"

    reward: Any = None
    terminated: Any = None
    step_info: Any = None
    eval_error: str | None = None
    try:
        _env_obs, reward, terminated, _truncated, step_info = wrapped.env.step("claim_done")
    except Exception as e:  # 评分失败不吞异常，落盘供归因
        eval_error = f"{type(e).__name__}: {e}"

    (task_dir / "loca_eval.json").write_text(
        json.dumps(
            {
                "reward": float(reward) if isinstance(reward, (int, float)) else reward,
                "terminated": bool(terminated) if terminated is not None else None,
                "agent_rc": agent_rc,
                "agent_error": agent_error,
                "eval_error": eval_error,
                "step_info": step_info,
                "total_duration_s": round(time.time() - t0, 2),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"[loca_side] evaluated reward={reward} agent_rc={agent_rc} eval_error={eval_error}", flush=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="LOCA-bench 侧进程（LOCA venv）")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run-task")
    p.add_argument("--loca-repo", required=True)
    p.add_argument("--config-set", required=True)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--task-dir", required=True)
    p.add_argument("--agent-cmd", required=True, help="JSON 数组：MyCode agent 执行命令")
    p.add_argument("--timeout", type=int, default=0, help="agent 子进程超时秒数（0=不限）")
    p.add_argument("--max-tool-uses", type=int, default=100)
    args = parser.parse_args()
    raise SystemExit(cmd_run_task(args))


if __name__ == "__main__":
    main()
