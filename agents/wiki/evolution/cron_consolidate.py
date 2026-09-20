"""Cron Consolidate — 定时 consolidate 任务。

参考：projects/llm-wiki-memory/scripts/cron-job.mjs
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.wiki.wiki_manager import get_wiki_dir


CONSOLIDATE_INTERVAL_SECONDS = 3600
MAX_CONSECUTIVE_FAILURES = 3


def get_state_dir() -> Path:
    """获取状态目录。"""
    d = get_wiki_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_log_path() -> Path:
    """获取日志路径。"""
    return get_state_dir() / ".consolidate-attempts.log"


def get_entity_state_path() -> Path:
    """获取 entity 状态路径。"""
    return get_state_dir() / ".consolidate-entities.json"


def get_issues_dir() -> Path:
    """获取 issues 目录。"""
    d = get_wiki_dir() / "issues"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_attempt(status: str, details: dict[str, Any]) -> None:
    """记录一次 consolidate 尝试。"""
    log_path = get_log_path()
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "details": details,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def load_entity_state() -> dict[str, Any]:
    """加载 entity 状态。"""
    path = get_entity_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, KeyError):
        return {}


def save_entity_state(state: dict[str, Any]) -> None:
    """保存 entity 状态。"""
    path = get_entity_state_path()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def update_entity_health(entity_id: str, success: bool) -> None:
    """更新 entity 健康状态。"""
    state = load_entity_state()
    entity = state.get(entity_id, {"consecutive_failures": 0, "last_success": None, "last_failure": None})

    if success:
        entity["consecutive_failures"] = 0
        entity["last_success"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    else:
        entity["consecutive_failures"] += 1
        entity["last_failure"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    state[entity_id] = entity
    save_entity_state(state)


def should_escalate(entity_id: str) -> bool:
    """判断是否需要 escalation。"""
    state = load_entity_state()
    entity = state.get(entity_id, {})
    return entity.get("consecutive_failures", 0) >= MAX_CONSECUTIVE_FAILURES


def create_issue(entity_id: str, error: str) -> Path:
    """创建 issue 报告。"""
    now = datetime.now(timezone.utc)
    issues_dir = get_issues_dir() / now.strftime("%Y/%m/%d")
    issues_dir.mkdir(parents=True, exist_ok=True)

    sig = f"{entity_id}-{now.strftime('%H%M%S')}"
    issue_path = issues_dir / f"{sig}.md"

    content = f"""# Issue: {entity_id}

**Created**: {now.isoformat(timespec="seconds")}
**Entity**: {entity_id}
**Error**: {error}

## Details

Consecutive failures exceeded threshold ({MAX_CONSECUTIVE_FAILURES}).

## Resolution

- [ ] Investigate root cause
- [ ] Fix the issue
- [ ] Verify resolution
"""
    issue_path.write_text(content)
    return issue_path


async def run_consolidate_cron(side_query: Any) -> dict[str, Any]:
    """运行 cron consolidate 任务。"""
    from agents.wiki.wiki_compiler import compile_pending_session
    from agents.wiki.wiki_consolidator import consolidate

    result = {"compile": {}, "consolidate": {}, "status": "success"}

    try:
        compile_result = await compile_pending_session(side_query)
        result["compile"] = compile_result
        update_entity_health("system:compile", True)
        log_attempt("success", {"phase": "compile", "stats": compile_result})
    except Exception as e:
        update_entity_health("system:compile", False)
        log_attempt("failed", {"phase": "compile", "error": str(e)})
        result["status"] = "failed"

        if should_escalate("system:compile"):
            create_issue("system:compile", str(e))

    try:
        consolidate_result = await consolidate(side_query)
        result["consolidate"] = consolidate_result
        update_entity_health("system:consolidate", True)
        log_attempt("success", {"phase": "consolidate", "stats": consolidate_result})
    except Exception as e:
        update_entity_health("system:consolidate", False)
        log_attempt("failed", {"phase": "consolidate", "error": str(e)})
        result["status"] = "failed"

        if should_escalate("system:consolidate"):
            create_issue("system:consolidate", str(e))

    return result


async def start_cron_loop(side_query: Any, interval: int = CONSOLIDATE_INTERVAL_SECONDS) -> None:
    """启动 cron 循环。"""
    while True:
        try:
            await run_consolidate_cron(side_query)
        except Exception as e:
            log_attempt("error", {"error": str(e)})
        await asyncio.sleep(interval)
