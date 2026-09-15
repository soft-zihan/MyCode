"""
Rewind 时间旅行调试器集成模块

提供 LLM 调用录制、回放、诊断功能。
"""
from __future__ import annotations

import os
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_rewind_initialized = False


def is_enabled() -> bool:
    """检查 Rewind 是否启用"""
    return os.environ.get("MYCODE_REWIND", "").lower() in ("1", "true", "yes")


def _get_rewind_cli() -> str | None:
    """获取 rewind CLI 路径"""
    # 优先使用 PATH 中的
    import shutil
    cli_path = shutil.which("rewind")
    if cli_path:
        return cli_path
    
    # 尝试 ~/.rewind/bin/ 下的版本
    rewind_bin = Path.home() / ".rewind" / "bin"
    if rewind_bin.exists():
        versions = sorted(rewind_bin.glob("rewind-*"), reverse=True)
        if versions:
            return str(versions[0])
    
    return None


def init_rewind(mode: str = "direct") -> bool:
    """
    初始化 Rewind 录制
    
    Args:
        mode: "direct" (默认) 或 "proxy"
    
    Returns:
        是否成功初始化
    """
    global _rewind_initialized
    
    if _rewind_initialized:
        return True
    
    if not is_enabled():
        return False
    
    try:
        import rewind_agent
        rewind_agent.init(mode=mode)
        _rewind_initialized = True
        logger.info(f"[Rewind] Initialized in {mode} mode")
        return True
    except ImportError:
        logger.warning("[Rewind] rewind-agent not installed. Run: pip install rewind-agent")
        return False
    except Exception as e:
        logger.warning(f"[Rewind] Init failed: {e}")
        return False


def get_session_id() -> str | None:
    """获取当前 Rewind session ID"""
    if not _rewind_initialized:
        return None
    
    try:
        import rewind_agent
        return rewind_agent.get_session_id()
    except Exception:
        return None


def export_session(session_id: str | None = None, output_dir: str | None = None) -> bool:
    """
    导出 session 为 OTel 格式（可通过 rewind import 导入）
    
    Args:
        session_id: 可选的 session ID，默认使用最新 session
        output_dir: 导出目录
    
    Returns:
        是否成功导出
    """
    cli = _get_rewind_cli()
    if not cli:
        logger.warning("[Rewind] CLI not found")
        return False
    
    try:
        cmd = [cli, "export", "otel"]
        if session_id:
            cmd.extend(["--session", session_id])
        if output_dir:
            cmd.extend(["--output", output_dir])
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            logger.info(f"[Rewind] Session exported to {output_dir}")
            return True
        else:
            logger.warning(f"[Rewind] Export failed: {result.stderr}")
            return False
    except Exception as e:
        logger.warning(f"[Rewind] Export failed: {e}")
        return False


def import_from_langfuse(trace_id: str) -> str | None:
    """
    从 Langfuse 导入 trace
    
    Args:
        trace_id: Langfuse trace ID
    
    Returns:
        导入后的 Rewind session ID，失败返回 None
    """
    cli = _get_rewind_cli()
    if not cli:
        logger.warning("[Rewind] CLI not found")
        return None
    
    try:
        cmd = [cli, "import", "from-langfuse", "--trace", trace_id]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            # 解析输出获取 session ID
            logger.info(f"[Rewind] Imported trace {trace_id}")
            return trace_id  # 简化处理
        else:
            logger.warning(f"[Rewind] Import failed: {result.stderr}")
            return None
    except Exception as e:
        logger.warning(f"[Rewind] Import failed: {e}")
        return None


def replay_from(step: int, session_id: str | None = None) -> bool:
    """
    从指定步骤开始回放
    
    Args:
        step: 从第几步开始回放（前面的步骤走缓存）
        session_id: 可选的 session ID
    
    Returns:
        是否成功
    """
    cli = _get_rewind_cli()
    if not cli:
        logger.warning("[Rewind] CLI not found")
        return False
    
    try:
        cmd = [cli, "replay"]
        if session_id:
            cmd.append(session_id)
        cmd.extend(["--from", str(step)])
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"[Rewind] Replay failed: {e}")
        return False


def diagnose_failure(session_id: str | None = None) -> dict[str, Any] | None:
    """
    使用 LLM 诊断失败原因
    
    Args:
        session_id: 可选的 session ID
    
    Returns:
        诊断结果字典，包含 failure_reason, suggested_fix 等
    """
    cli = _get_rewind_cli()
    if not cli:
        logger.warning("[Rewind] CLI not found")
        return None
    
    try:
        cmd = [cli, "fix"]
        if session_id:
            cmd.append(session_id)
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            # 解析输出
            return {"raw_output": result.stdout}
        else:
            logger.warning(f"[Rewind] Diagnosis failed: {result.stderr}")
            return None
    except Exception as e:
        logger.warning(f"[Rewind] Diagnosis failed: {e}")
        return None


def show_sessions(limit: int = 5) -> list[dict]:
    """列出最近的 sessions"""
    cli = _get_rewind_cli()
    if not cli:
        return []
    
    try:
        cmd = [cli, "sessions", "--limit", str(limit), "--json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            import json
            return json.loads(result.stdout)
        return []
    except Exception:
        return []
