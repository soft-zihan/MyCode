"""运行时抽象层 - 支持本地和 Docker 容器执行

环境变量：
- MYCODE_DOCKER_CONTAINER: Docker 容器 ID，设置后自动启用 DockerRuntime
- MYCODE_DOCKER_WORKDIR: 容器内工作目录（默认 /testbed）
- MYCODE_DOCKER_CONDA: conda 环境名（默认 testbed）
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Protocol
from agents.core.workspace import get_workspace


class Runtime(Protocol):
    """运行时接口"""
    
    def read_file(self, path: str) -> str:
        """读取文件内容"""
        ...
    
    def write_file(self, path: str, content: str) -> None:
        """写入文件"""
        ...
    
    def run_command(self, command: str, timeout_s: float = 30) -> tuple[int, str, str]:
        """执行命令，返回 (exit_code, stdout, stderr)"""
        ...
    
    def list_files(self, base: str, pattern: str) -> list[str]:
        """列出文件"""
        ...
    
    def grep_search(self, pattern: str, path: str, include: str | None = None) -> str:
        """搜索文件内容"""
        ...


class LocalRuntime:
    """本地运行时 - 直接操作文件系统"""
    
    def read_file(self, path: str) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = get_workspace() / p
        return p.read_text(errors="replace")
    
    def write_file(self, path: str, content: str) -> None:
        p = Path(path)
        if not p.is_absolute():
            p = get_workspace() / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    
    def run_command(self, command: str, timeout_s: float = 30) -> tuple[int, str, str]:
        """执行命令。独立进程组（start_new_session）：超时时 killpg 杀整棵进程树。

        subprocess.run 超时只杀直接子进程 /bin/sh，孙进程（node、chromium 等）会
        孤儿化存活——浏览器类任务每次超时泄漏 200-400MB，长评测下累积成 OOM。
        """
        import os
        import signal
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(get_workspace()),
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.communicate()  # 收尸，避免僵尸
            raise
    
    def list_files(self, base: str, pattern: str) -> list[str]:
        import os
        base_path = Path(base)
        if not base_path.is_absolute():
            base_path = get_workspace() / base_path
        files = []
        for p in base_path.glob(pattern):
            if p.is_file():
                rel = str(p.relative_to(base_path))
                if "node_modules" in rel or ".git" in rel.split(os.sep):
                    continue
                files.append(rel)
                if len(files) >= 200:
                    break
        return files
    
    def grep_search(self, pattern: str, path: str, include: str | None = None) -> str:
        p = Path(path)
        if not p.is_absolute():
            path = str(get_workspace() / p)
        args = ["grep", "--line-number", "--color=never", "-r", "-E"]
        for excl in ("node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".embed-cache"):
            args.append(f"--exclude-dir={excl}")
        if include:
            args.append(f"--include={include}")
        args.extend(["--", pattern, path])
        
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            return f"[grep timed out after 10s: search scope too large ({path}), narrow the path or use include filter]"
        if result.returncode == 1:
            return ""
        if result.returncode == 0:
            return result.stdout
        return f"[grep exited with code {result.returncode}: {result.stderr.strip()[:200]}]"


class DockerRuntime:
    """Docker 运行时 - 通过 docker exec 操作容器"""
    
    def __init__(self, container_id: str, workdir: str = "/testbed", conda_env: str = "testbed"):
        self.container_id = container_id
        self.workdir = workdir
        self.conda_env = conda_env
    
    def _exec(self, cmd: str, timeout_s: float = 30) -> tuple[int, str, str]:
        """在容器内执行命令"""
        activate_cmd = f"source /opt/miniconda3/bin/activate {self.conda_env} 2>/dev/null || true"
        inner = f"{activate_cmd} && cd {self.workdir} && {cmd}"
        escaped = inner.replace("'", "'\\''")
        full_cmd = f"docker exec {self.container_id} bash -c '{escaped}'"
        result = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return result.returncode, result.stdout, result.stderr
    
    def read_file(self, path: str) -> str:
        # 确保路径在 workdir 内
        if not path.startswith("/"):
            path = f"{self.workdir}/{path}"
        
        exit_code, stdout, stderr = self._exec(f"cat '{path}'")
        if exit_code != 0:
            raise FileNotFoundError(f"File not found: {path}")
        return stdout
    
    def write_file(self, path: str, content: str) -> None:
        if not path.startswith("/"):
            path = f"{self.workdir}/{path}"
        
        import tempfile
        import os
        
        # 写入临时文件
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.tmp') as f:
            f.write(content)
            tmp_path = f.name
        
        try:
            # 确保目录存在
            dir_path = str(Path(path).parent)
            self._exec(f"mkdir -p '{dir_path}'")
            
            # docker cp 到容器
            result = subprocess.run(
                f"docker cp {tmp_path} {self.container_id}:{path}",
                shell=True,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"docker cp failed: {result.stderr}")
        finally:
            os.unlink(tmp_path)
    
    def run_command(self, command: str, timeout_s: float = 30) -> tuple[int, str, str]:
        # 在 workdir 下执行
        return self._exec(f"cd {self.workdir} && {command}", timeout_s)
    
    def list_files(self, base: str, pattern: str) -> list[str]:
        if not base.startswith("/"):
            base = f"{self.workdir}/{base}"
        
        # 使用 find 命令
        exit_code, stdout, _ = self._exec(f"find '{base}' -name '{pattern}' -type f 2>/dev/null | head -200")
        if exit_code != 0:
            return []
        
        files = []
        for line in stdout.strip().split("\n"):
            if line:
                # 转为相对路径
                if line.startswith(self.workdir + "/"):
                    line = line[len(self.workdir) + 1:]
                files.append(line)
        return files
    
    def grep_search(self, pattern: str, path: str, include: str | None = None) -> str:
        if not path.startswith("/"):
            path = f"{self.workdir}/{path}"
        
        # 使用引号包裹 pattern 和 path，避免空格问题
        pattern_escaped = pattern.replace("'", "'\\''")
        path_escaped = path.replace("'", "'\\''")
        
        cmd = f"grep --line-number --color=never -r -E"
        if include:
            cmd += f" --include={include}"
        cmd += f" -- '{pattern_escaped}' '{path_escaped}'"
        
        exit_code, stdout, _ = self._exec(cmd)
        return stdout if exit_code == 0 else ""


# 全局运行时实例
_current_runtime: Runtime | None = None
_initialized: bool = False


def _auto_init() -> None:
    """根据环境变量自动初始化运行时。"""
    global _initialized
    if _initialized:
        return
    _initialized = True
    container = os.environ.get("MYCODE_DOCKER_CONTAINER", "").strip()
    if container:
        set_docker_runtime(
            container,
            workdir=os.environ.get("MYCODE_DOCKER_WORKDIR", "/testbed"),
            conda_env=os.environ.get("MYCODE_DOCKER_CONDA", "testbed"),
        )


def get_runtime() -> Runtime:
    """获取当前运行时"""
    _auto_init()
    return _current_runtime or LocalRuntime()


def set_runtime(runtime: Runtime | None) -> None:
    """设置当前运行时"""
    global _current_runtime
    _current_runtime = runtime


def set_docker_runtime(container_id: str, workdir: str = "/testbed", conda_env: str = "testbed") -> None:
    """便捷方法：设置 Docker 运行时"""
    set_runtime(DockerRuntime(container_id, workdir, conda_env))
