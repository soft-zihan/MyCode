"""进程组清理单测：超时/退出时整树回收，杜绝孤儿泄漏（OOM 事故根因）。"""

from __future__ import annotations

import os
import re
import subprocess
import time

import pytest

from agents.tools.runtime import LocalRuntime
from agents.tools import shell_tools


def _pid_alive(pid: int) -> bool:
    return subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0


def test_run_command_timeout_kills_process_group(tmp_path):
    """超时必须杀掉整棵进程树：sh 的孙进程（模拟 node/chromium）不能孤儿化存活。"""
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 30 & echo $! > {pidfile}; wait"
    rt = LocalRuntime()
    with pytest.raises(subprocess.TimeoutExpired):
        rt.run_command(script, timeout_s=1)
    time.sleep(0.5)
    grandchild = int(pidfile.read_text().strip())
    assert not _pid_alive(grandchild), f"孙进程 {grandchild} 应随进程组被杀（防孤儿泄漏）"


def test_run_command_normal_exit_unaffected(tmp_path):
    rt = LocalRuntime()
    code, out, _ = rt.run_command("echo hello", timeout_s=5)
    assert code == 0 and out.strip() == "hello"


def _start_bg(command: str) -> str:
    msg = shell_tools._start_background_shell(command)
    m = re.search(r"job_id=(\w+)", msg)
    assert m, f"background job 启动失败: {msg}"
    return m.group(1)


def test_background_job_is_process_group_leader():
    """background job 必须自建进程组（start_new_session），否则 killpg 会误伤宿主。"""
    job_id = _start_bg("sleep 30")
    try:
        job = shell_tools.BACKGROUND_JOBS[job_id]
        pid = job["pid"]
        time.sleep(0.3)
        assert os.getpgid(pid) == pid, "background job 应为进程组组长"
    finally:
        shell_tools._kill_all_background_jobs()


def test_kill_all_background_jobs_reaps_children():
    job_id = _start_bg("sleep 30 & echo started; wait")
    time.sleep(0.5)
    pid = shell_tools.BACKGROUND_JOBS[job_id]["pid"]
    shell_tools._kill_all_background_jobs()
    time.sleep(0.5)
    assert not _pid_alive(pid), "background job 进程组应被回收"
