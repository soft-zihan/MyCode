"""Simple logging wrapper for MyCode.

替代 TUI 的 print_* 函数，使用标准 logging。

⚠️ 冻结（2026-09-23 用户裁定）：随 CLI REPL（agents/main.py）冻结，
TUI 落地后一并删除；Web/API 路径不使用本模块新增能力。
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger("mycode")


def _safe_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def print_info(msg: str) -> None:
    logger.info(_safe_text(msg))


def print_warning(msg: str) -> None:
    logger.warning(_safe_text(msg))


def print_error(msg: str) -> None:
    logger.error(_safe_text(msg))


def print_confirmation(command: str) -> None:
    logger.info(f"Permission required: {command}")


def print_divider() -> None:
    logger.info("-" * 40)


def print_cost(input_tokens: int, output_tokens: int) -> None:
    cost = (input_tokens / 1_000_000) * 3 + (output_tokens / 1_000_000) * 15
    logger.info(f"Tokens: {input_tokens} in / {output_tokens} out | Cost: ${cost:.4f}")


def print_retry(attempt: int, max_retries: int, reason: str) -> None:
    logger.warning(f"Retry {attempt}/{max_retries}: {reason}")


def print_assistant_text(text: str) -> None:
    sys.stdout.write(_safe_text(text))
    sys.stdout.flush()


def print_sub_agent_start(agent_type: str, description: str) -> None:
    logger.info(f"Sub-agent start: {agent_type} - {description}")


def print_sub_agent_end(agent_type: str, description: str) -> None:
    logger.info(f"Sub-agent end: {agent_type}")
