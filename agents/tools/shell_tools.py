"""Shell Tools - Shell 命令执行工具。

包括：run_shell, shell_status
支持后台执行和状态查询。
"""

from __future__ import annotations

import subprocess
import threading
import time as _time
from typing import Any

from agents.observability.trace import trace_event


BACKGROUND_JOBS: dict[str, dict[str, Any]] = {}
_BG_LOCK = threading.Lock()
_BG_COUNTER = 0
_on_background_done: Any = None


def set_background_done_callback(fn: Any) -> None:
    global _on_background_done
    _on_background_done = fn


def _next_job_id() -> str:
    global _BG_COUNTER
    with _BG_LOCK:
        _BG_COUNTER += 1
        return f"job{_BG_COUNTER}"


def _watch_background(job_id: str) -> None:
    job = BACKGROUND_JOBS.get(job_id)
    if not job:
        return
    proc: subprocess.Popen = job["process"]
    try:
        stdout, stderr = proc.communicate()
    except Exception as e:
        stdout, stderr = "", f"watcher error: {e}"
    exit_code = proc.returncode
    output = (stdout or "") + (f"\nStderr:\n{stderr}" if stderr else "")
    if not output.strip():
        output = "(no output)"
    if len(output) > 20000:
        output = output[:20000] + f"\n... (truncated, {len(output)} chars total)"
    job["output"] = output
    job["exit_code"] = exit_code
    job["done"] = True
    job["end_time"] = _time.time()
    trace_event(
        "bg.done",
        job_id=job_id,
        command=job["command"],
        exit_code=exit_code,
        duration_s=round(job["end_time"] - job["start_time"], 2),
        output_preview=output[:300],
    )
    if _on_background_done:
        try:
            _on_background_done(job_id, job["command"], output, exit_code)
        except Exception:
            pass


def _start_background_shell(command: str) -> str:
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        return f"Error starting background command: {e}"
    job_id = _next_job_id()
    BACKGROUND_JOBS[job_id] = {
        "command": command,
        "process": proc,
        "pid": proc.pid,
        "start_time": _time.time(),
        "done": False,
        "output": None,
        "exit_code": None,
        "end_time": None,
    }
    threading.Thread(target=_watch_background, args=(job_id,), daemon=True).start()
    trace_event("bg.start", job_id=job_id, pid=proc.pid, command=command)
    return (
        f"Background command started (job_id={job_id}, pid={proc.pid}). "
        "It is running in the background; you can continue with other work. "
        "Use shell_status with this job_id to check progress. "
        "You will also be notified automatically when it finishes."
    )


def shell_status(inp: dict) -> str:
    job_id = str(inp.get("job_id") or "").strip()
    with _BG_LOCK:
        if job_id:
            job = BACKGROUND_JOBS.get(job_id)
            if not job:
                return f"No background job named '{job_id}'. Active jobs: {', '.join(BACKGROUND_JOBS) or '(none)'}"
            jobs = {job_id: job}
        else:
            jobs = dict(BACKGROUND_JOBS)
    if not jobs:
        return "No background jobs."
    lines = []
    for jid, job in jobs.items():
        state = "done" if job["done"] else "running"
        line = f"{jid}: [{state}] {job['command']}"
        if job["done"]:
            line += f" (exit {job['exit_code']})\nOutput:\n{job['output']}"
        lines.append(line)
    return "\n\n".join(lines)


def run_shell(inp: dict) -> str:
    if inp.get("background"):
        return _start_background_shell(inp["command"])
    try:
        from agents.runtime import get_runtime
        rt = get_runtime()
        timeout_ms = inp.get("timeout", 30000)
        timeout_s = timeout_ms / 1000
        exit_code, stdout, stderr = rt.run_command(inp["command"], timeout_s)
        if exit_code != 0:
            stderr_msg = f"\nStderr: {stderr}" if stderr else ""
            stdout_msg = f"\nStdout: {stdout}" if stdout else ""
            return f"Command failed (exit code {exit_code}){stdout_msg}{stderr_msg}"
        return stdout or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {inp.get('timeout', 30000)}ms"
    except Exception as e:
        return f"Error: {e}"
