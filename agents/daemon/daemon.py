"""Daemon supervisor for background Agent execution.

Architecture:
    mycode daemon start
        │
        ▼
    Supervisor (常驻后台进程)
        │
        ├─ Worker A (session 1)
        │   ├─ Agent Loop
        │   ├─ Scheduler
        │   └─ Children
        │
        └─ Worker B (session 2)
            └─ Agent Loop

Features:
- Unix socket 通信
- Worker 进程管理
- 会话持久化
- 优雅关闭
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agents.observability.trace import trace_event


DEFAULT_SOCKET_PATH = Path.home() / ".bear-code" / "daemon.sock"
DEFAULT_PID_FILE = Path.home() / ".bear-code" / "daemon.pid"
DEFAULT_LOG_FILE = Path.home() / ".bear-code" / "daemon.log"


@dataclass
class WorkerProcess:
    """表示一个运行中的 Worker 进程。"""
    session_id: str
    process: asyncio.subprocess.Process | None = None
    status: str = "idle"  # idle, running, stopped, error
    last_activity: float = field(default_factory=time.time)
    prompt_queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    @property
    def is_alive(self) -> bool:
        if self.process is None:
            return False
        return self.process.returncode is None


class DaemonSupervisor:
    """Daemon 主进程，管理所有 Worker。"""

    def __init__(
        self,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        pid_file: Path = DEFAULT_PID_FILE,
        log_file: Path = DEFAULT_LOG_FILE,
    ):
        self.socket_path = socket_path
        self.pid_file = pid_file
        self.log_file = log_file
        self.workers: dict[str, WorkerProcess] = {}
        self._server: asyncio.Server | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """启动 Daemon，监听 Unix socket。"""
        # 确保目录存在
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # 清理旧的 socket 文件
        if self.socket_path.exists():
            self.socket_path.unlink()

        # 写入 PID 文件
        self.pid_file.write_text(str(os.getpid()))

        # 启动 Unix socket 服务器
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            str(self.socket_path),
        )

        self._running = True
        trace_event("daemon.start", pid=os.getpid(), socket=str(self.socket_path))

        # 设置信号处理
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        # 主循环
        try:
            await self._shutdown_event.wait()
        finally:
            await self._cleanup()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """处理客户端连接（attach/detach/command）。"""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                command = line.decode().strip()
                response = await self._dispatch_command(command, reader, writer)

                if response:
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()

        except Exception as e:
            trace_event("daemon.client_error", error=str(e))
        finally:
            writer.close()

    async def _dispatch_command(
        self,
        command: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> dict[str, Any] | None:
        """分发命令到对应的处理函数。"""
        parts = command.split(maxsplit=1)
        cmd = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "ping":
            return {"status": "ok", "workers": len(self.workers)}

        elif cmd == "status":
            return {
                "status": "ok",
                "workers": {
                    sid: {
                        "status": w.status,
                        "alive": w.is_alive,
                        "last_activity": w.last_activity,
                    }
                    for sid, w in self.workers.items()
                },
            }

        elif cmd == "prompt":
            # 格式: prompt <session_id> <message>
            prompt_parts = args.split(maxsplit=1)
            if len(prompt_parts) < 2:
                return {"status": "error", "message": "Usage: prompt <session_id> <message>"}
            session_id, message = prompt_parts
            return await self._handle_prompt(session_id, message)

        elif cmd == "attach":
            # 流式输出会话事件
            session_id = args.strip()
            await self._stream_session_events(session_id, writer)
            return None

        elif cmd == "list":
            return {
                "status": "ok",
                "sessions": list(self.workers.keys()),
            }

        elif cmd == "stop":
            session_id = args.strip()
            return await self._stop_worker(session_id)

        elif cmd == "shutdown":
            asyncio.create_task(self.shutdown())
            return {"status": "ok", "message": "Shutting down"}

        else:
            return {"status": "error", "message": f"Unknown command: {cmd}"}

    async def _handle_prompt(self, session_id: str, message: str) -> dict[str, Any]:
        """处理 prompt 命令，路由到对应的 Worker。"""
        if session_id not in self.workers:
            # 创建新的 Worker
            worker = await self._create_worker(session_id)
            self.workers[session_id] = worker

        worker = self.workers[session_id]
        await worker.prompt_queue.put(message)
        worker.last_activity = time.time()

        trace_event("daemon.prompt", session_id=session_id, message_length=len(message))
        return {"status": "ok", "session_id": session_id, "queued": True}

    async def _create_worker(self, session_id: str) -> WorkerProcess:
        """创建新的 Worker 进程。"""
        worker = WorkerProcess(session_id=session_id)
        trace_event("daemon.worker_create", session_id=session_id)
        return worker

    async def _stop_worker(self, session_id: str) -> dict[str, Any]:
        """停止指定的 Worker。"""
        if session_id not in self.workers:
            return {"status": "error", "message": f"Worker not found: {session_id}"}

        worker = self.workers[session_id]
        if worker.process and worker.is_alive:
            worker.process.terminate()
            try:
                await asyncio.wait_for(worker.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                worker.process.kill()

        worker.status = "stopped"
        del self.workers[session_id]

        trace_event("daemon.worker_stop", session_id=session_id)
        return {"status": "ok", "session_id": session_id, "stopped": True}

    async def _stream_session_events(
        self,
        session_id: str,
        writer: asyncio.StreamWriter,
    ) -> None:
        """流式输出会话事件（用于 attach）。"""
        # TODO: 实现事件流
        pass

    async def shutdown(self) -> None:
        """优雅关闭 Daemon。"""
        trace_event("daemon.shutdown")
        self._running = False

        # 停止所有 Worker
        for session_id in list(self.workers.keys()):
            await self._stop_worker(session_id)

        self._shutdown_event.set()

    async def _cleanup(self) -> None:
        """清理资源。"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        if self.socket_path.exists():
            self.socket_path.unlink()

        if self.pid_file.exists():
            self.pid_file.unlink()


# ─── Client API ───────────────────────────────────────────────────────────────


class DaemonClient:
    """Daemon 客户端，用于 attach 和发送命令。"""

    def __init__(self, socket_path: Path = DEFAULT_SOCKET_PATH):
        self.socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> bool:
        """连接到 Daemon。"""
        if not self.socket_path.exists():
            return False
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(str(self.socket_path))
            return True
        except Exception:
            return False

    async def send_command(self, command: str) -> dict[str, Any]:
        """发送命令并等待响应。"""
        if self._writer is None or self._reader is None:
            raise RuntimeError("Not connected")

        self._writer.write(command.encode() + b"\n")
        await self._writer.drain()

        response_line = await self._reader.readline()
        if not response_line:
            return {"status": "error", "message": "No response"}

        return json.loads(response_line.decode())

    async def prompt(self, session_id: str, message: str) -> dict[str, Any]:
        """发送 prompt 到指定会话。"""
        return await self.send_command(f"prompt {session_id} {message}")

    async def status(self) -> dict[str, Any]:
        """获取 Daemon 状态。"""
        return await self.send_command("status")

    async def list_sessions(self) -> dict[str, Any]:
        """列出所有会话。"""
        return await self.send_command("list")

    async def attach(self, session_id: str) -> None:
        """附加到会话，流式接收事件。"""
        if self._writer is None:
            raise RuntimeError("Not connected")

        self._writer.write(f"attach {session_id}\n".encode())
        await self._writer.drain()

        # 流式读取事件
        if self._reader:
            async for line in self._reader:
                event = json.loads(line.decode())
                print(event)

    async def close(self) -> None:
        """关闭连接。"""
        if self._writer:
            self._writer.close()


# ─── CLI Commands ─────────────────────────────────────────────────────────────


async def daemon_start() -> int:
    """启动 Daemon 后台进程。"""
    # 检查是否已经运行
    if DEFAULT_PID_FILE.exists():
        pid = int(DEFAULT_PID_FILE.read_text())
        try:
            os.kill(pid, 0)
            print(f"Daemon already running (PID {pid})")
            return 0
        except OSError:
            DEFAULT_PID_FILE.unlink()

    # fork 到后台
    pid = os.fork()
    if pid > 0:
        # 父进程
        print(f"Daemon started (PID {pid})")
        return 0

    # 子进程：成为 session leader
    os.setsid()

    # 重定向标准流
    sys.stdin.close()
    log_file = open(DEFAULT_LOG_FILE, "a")
    sys.stdout = log_file
    sys.stderr = log_file

    # 启动 supervisor
    supervisor = DaemonSupervisor()
    await supervisor.start()
    return 0


async def daemon_stop() -> int:
    """停止 Daemon。"""
    if not DEFAULT_PID_FILE.exists():
        print("Daemon not running")
        return 0

    pid = int(DEFAULT_PID_FILE.read_text())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Daemon stopped (PID {pid})")
    except OSError as e:
        print(f"Failed to stop daemon: {e}")
        DEFAULT_PID_FILE.unlink()
    return 0


async def daemon_status() -> int:
    """显示 Daemon 状态。"""
    client = DaemonClient()
    if not await client.connect():
        print("Daemon not running")
        return 1

    status = await client.status()
    print(json.dumps(status, indent=2))
    await client.close()
    return 0


async def daemon_attach(session_id: str) -> int:
    """附加到指定会话。"""
    client = DaemonClient()
    if not await client.connect():
        print("Daemon not running")
        return 1

    print(f"Attached to session: {session_id}")
    print("Press Ctrl+C to detach")

    try:
        await client.attach(session_id)
    except KeyboardInterrupt:
        print("\nDetached")
    finally:
        await client.close()

    return 0


async def daemon_prompt(session_id: str, message: str) -> int:
    """发送 prompt 到指定会话。"""
    client = DaemonClient()
    if not await client.connect():
        print("Daemon not running")
        return 1

    result = await client.prompt(session_id, message)
    print(json.dumps(result, indent=2))
    await client.close()
    return 0
