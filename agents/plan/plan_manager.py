"""Plan 基础设施 — CRUD、状态机、任务管理。

Plan 是主动的目标规划，存储在 .mycode/plans/ 下，跟随主项目仓库。
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agents.core.workspace import get_workspace
from agents.memory.frontmatter import parse_frontmatter, format_frontmatter
from agents.plan.task_models import normalize_status
from agents.plan.plan_models import Plan, PlanStatus, PlanGranularity, Task


VALID_STATUSES = {s.value for s in PlanStatus}
VALID_GRANULARITIES = {g.value for g in PlanGranularity}


def get_plans_dir() -> Path:
    d = get_workspace() / ".mycode" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())
    s = s.strip("-")
    return s[:40] or "plan"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_plan_dir(plan_dir: Path) -> Plan | None:
    meta_path = plan_dir / "_meta.md"
    if not meta_path.exists():
        return None
    try:
        result = parse_frontmatter(meta_path.read_text())
        meta = result.meta

        tags = meta.get("tags", [])
        if isinstance(tags, str):
            import ast
            try:
                tags = ast.literal_eval(tags)
            except (ValueError, SyntaxError):
                tags = [tags] if tags else []

        return Plan(
            slug=meta.get("slug", plan_dir.name),
            status=PlanStatus(meta.get("status", "proposed")),
            priority=meta.get("priority", "P2"),
            created=meta.get("created", ""),
            modified=meta.get("modified", ""),
            tags=tags,
            granularity=PlanGranularity(meta.get("granularity", "minimal")),
            plan_dir=str(plan_dir),
        )
    except Exception:
        return None


def create_plan(
    slug: str,
    *,
    priority: str = "P2",
    tags: list[str] | None = None,
    granularity: PlanGranularity = PlanGranularity.MINIMAL,
) -> Path:
    plans_dir = get_plans_dir()
    plan_dir = plans_dir / slug
    if plan_dir.exists():
        raise ValueError(f"Plan '{slug}' already exists")

    plan_dir.mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    meta = {
        "slug": slug,
        "status": PlanStatus.PROPOSED.value,
        "priority": priority,
        "created": now,
        "modified": now,
        "tags": tags or [],
        "granularity": granularity.value,
    }

    meta_path = plan_dir / "_meta.md"
    meta_path.write_text(format_frontmatter(meta, ""))

    tasks_path = plan_dir / "tasks.md"
    tasks_path.write_text("## 任务清单\n\n")

    if granularity in (PlanGranularity.STANDARD, PlanGranularity.FULL):
        proposal_path = plan_dir / "proposal.md"
        proposal_path.write_text("## 为什么做\n\n## 做什么\n")

    if granularity == PlanGranularity.FULL:
        specs_dir = plan_dir / "specs"
        specs_dir.mkdir(exist_ok=True)
        design_path = plan_dir / "design.md"
        design_path.write_text("## 技术方案\n")

    _git_commit(f"plan: create {slug}")
    return plan_dir


def get_plan(slug: str) -> Plan | None:
    plans_dir = get_plans_dir()
    plan_dir = plans_dir / slug
    return _parse_plan_dir(plan_dir)


def list_plans(include_archived: bool = False) -> list[Plan]:
    plans_dir = get_plans_dir()
    plans: list[Plan] = []

    for plan_dir in plans_dir.iterdir():
        if not plan_dir.is_dir() or plan_dir.name == "archive":
            continue
        plan = _parse_plan_dir(plan_dir)
        if plan is None:
            continue
        if not include_archived and plan.status == PlanStatus.ARCHIVED:
            continue
        plans.append(plan)

    plans.sort(key=lambda p: p.modified, reverse=True)
    return plans


def update_plan_status(slug: str, status: PlanStatus) -> bool:
    plans_dir = get_plans_dir()
    meta_path = plans_dir / slug / "_meta.md"
    if not meta_path.exists():
        return False

    try:
        result = parse_frontmatter(meta_path.read_text())
        result.meta["status"] = status.value
        result.meta["modified"] = _now_iso()
        meta_path.write_text(format_frontmatter(result.meta, result.body))
        _git_commit(f"plan: {slug} -> {status.value}")
        return True
    except Exception:
        return False


def update_plan_tags(slug: str, tags: list[str]) -> bool:
    plans_dir = get_plans_dir()
    meta_path = plans_dir / slug / "_meta.md"
    if not meta_path.exists():
        return False

    try:
        result = parse_frontmatter(meta_path.read_text())
        result.meta["tags"] = tags
        result.meta["modified"] = _now_iso()
        meta_path.write_text(format_frontmatter(result.meta, result.body))
        _git_commit(f"plan: update tags {slug}")
        return True
    except Exception:
        return False


def add_artifact(slug: str, filename: str, content: str) -> Path | None:
    plans_dir = get_plans_dir()
    filepath = plans_dir / slug / filename
    if not filepath.parent.exists():
        filepath.parent.mkdir(parents=True, exist_ok=True)

    try:
        filepath.write_text(content)
        _git_commit(f"plan: add {slug}/{filename}")
        return filepath
    except Exception:
        return None


def read_artifact(slug: str, filename: str) -> str | None:
    plans_dir = get_plans_dir()
    filepath = plans_dir / slug / filename
    if not filepath.exists():
        return None
    return filepath.read_text()


def archive_plan(slug: str) -> bool:
    plans_dir = get_plans_dir()
    plan_dir = plans_dir / slug
    if not plan_dir.exists():
        return False

    now = datetime.now(timezone.utc)
    archive_dir = plans_dir / "archive" / now.strftime("%Y-%m")
    archive_dir.mkdir(parents=True, exist_ok=True)

    target = archive_dir / slug
    if target.exists():
        return False

    try:
        plan_dir.rename(target)
        update_plan_status_in_dir(target, PlanStatus.ARCHIVED)
        _git_commit(f"plan: archive {slug}")
        return True
    except Exception:
        return False


def update_plan_status_in_dir(plan_dir: Path, status: PlanStatus) -> None:
    meta_path = plan_dir / "_meta.md"
    if not meta_path.exists():
        return
    try:
        result = parse_frontmatter(meta_path.read_text())
        result.meta["status"] = status.value
        result.meta["modified"] = _now_iso()
        meta_path.write_text(format_frontmatter(result.meta, result.body))
    except Exception:
        pass


# ── Task 管理 ──

def get_tasks(slug: str) -> list[Task]:
    """解析 tasks.md，支持两种格式：
    1. 简单格式: - [ ] 1. task description
    2. 结构化格式: ### Task 1: title ...
    """
    plans_dir = get_plans_dir()
    tasks_path = plans_dir / slug / "tasks.md"
    if not tasks_path.exists():
        return []

    content = tasks_path.read_text()
    
    # 先尝试解析结构化格式
    structured = _parse_structured_tasks(content)
    if structured:
        return structured
    
    # 回退到简单格式
    return _parse_simple_tasks(content)


def count_tasks(content: str) -> int:
    """统计 tasks 内容中的任务数（兼容结构化与 checkbox 两种格式）。"""
    structured = len(re.findall(r"### Task \d+:", content))
    if structured:
        return structured
    return len(re.findall(r"^\s*- \[[ x!~-]\]\s*\d+\.", content, re.M))


def _parse_simple_tasks(content: str) -> list[Task]:
    """解析简单格式: - [ ] 1. task description"""
    tasks: list[Task] = []

    for line in content.split("\n"):
        line = line.strip()
        if not line.startswith("- ["):
            continue

        task_id_match = re.match(r"- \[([ x!~-])\]\s*(\d+)\.\s*(.+)", line)
        if not task_id_match:
            continue

        marker, task_id_str, description = task_id_match.groups()
        task_id = int(task_id_str)

        if marker == "x":
            status = "done"
        elif marker == "!":
            status = "failed"
        elif marker == "~":
            status = "in-progress"
        elif marker == "-":
            status = "skipped"
        else:
            status = "pending"

        tasks.append(Task(
            id=task_id,
            description=description,
            status=status,
        ))

    return tasks


def _parse_structured_tasks(content: str) -> list[Task]:
    """解析结构化格式: ### Task 1: title ..."""
    tasks: list[Task] = []
    task_pattern = r"### Task (\d+): ([^\n]+)"
    
    for match in re.finditer(task_pattern, content):
        task_id = int(match.group(1))
        title = match.group(2).strip()
        
        start = match.end()
        next_match = re.search(r"### Task \d+:", content[start:])
        end = start + next_match.start() if next_match else len(content)
        task_block = content[match.start():end]
        
        file_match = re.search(r"\*\*文件\*\*:\s*`?([^`\n]+)`?", task_block)
        function_match = re.search(r"\*\*函数\*\*:\s*`?([^`\n]+)`?", task_block)
        interface_match = re.search(r"\*\*接口\*\*:\s*`?([^`\n]+)`?", task_block)
        acceptance_match = re.search(r"\*\*验收\*\*:\s*`?([^`\n]+)`?", task_block)
        status_match = re.search(r"\*\*状态\*\*:\s*\[[ x~!-]\]\s*([\w-]+)", task_block)
        error_match = re.search(r'\*\*错误\*\*:\s*"([^"]+)"', task_block)
        retry_match = re.search(r"\*\*重试次数\*\*:\s*(\d+)", task_block)
        
        task = Task(
            id=task_id,
            description=title,
            file=file_match.group(1).strip() if file_match else "",
            function=function_match.group(1).strip() if function_match else "",
            interface=interface_match.group(1).strip() if interface_match else "",
            acceptance=acceptance_match.group(1).strip() if acceptance_match else "",
            status=normalize_status(status_match.group(1)) if status_match else "pending",
            error=error_match.group(1).strip() if error_match else "",
            retry_count=int(retry_match.group(1)) if retry_match else 0,
        )
        tasks.append(task)
    
    return tasks


def get_next_task(slug: str) -> Task | None:
    tasks = get_tasks(slug)
    for task in tasks:
        if task.status == "pending":
            return task
    return None


def has_failed_tasks(slug: str) -> bool:
    tasks = get_tasks(slug)
    return any(t.status == "failed" for t in tasks)


def _check_plan_completion(slug: str) -> None:
    tasks = get_tasks(slug)
    if not tasks:
        return
    if all(t.status == "done" for t in tasks):
        update_plan_status(slug, PlanStatus.COMPLETED)


def get_plan_context(slug: str) -> dict:
    plans_dir = get_plans_dir()
    plan_dir = plans_dir / slug
    if not plan_dir.exists():
        return {}

    context = {
        "slug": slug,
        "meta": {},
        "proposal": "",
        "design": "",
        "tasks": [],
    }

    meta_path = plan_dir / "_meta.md"
    if meta_path.exists():
        result = parse_frontmatter(meta_path.read_text())
        context["meta"] = result.meta

    proposal_path = plan_dir / "proposal.md"
    if proposal_path.exists():
        context["proposal"] = proposal_path.read_text()

    design_path = plan_dir / "design.md"
    if design_path.exists():
        context["design"] = design_path.read_text()

    context["tasks"] = get_tasks(slug)
    return context


def build_task_prompt(slug: str, task: Task) -> str:
    context = get_plan_context(slug)

    parts = [f"# Plan: {slug}\n"]

    if context.get("proposal"):
        parts.append(f"## Proposal\n{context['proposal']}\n")

    if context.get("design"):
        parts.append(f"## Design\n{context['design']}\n")

    done_tasks = [t for t in context.get("tasks", []) if t.status == "done"]
    if done_tasks:
        parts.append("## Completed Tasks")
        for t in done_tasks:
            parts.append(f"- [x] {t.id}. {t.description}")
        parts.append("")

    failed_tasks = [t for t in context.get("tasks", []) if t.status == "failed"]
    if failed_tasks:
        parts.append("## Failed Tasks")
        for t in failed_tasks:
            parts.append(f"- [!] {t.id}. {t.description}")
            if t.error:
                parts.append(f"  Error: {t.error}")
        parts.append("")

    parts.append(f"## Current Task\n{task.id}. {task.description}\n")
    parts.append("请执行此任务。完成后调用 plan_task_done 标记完成。如果失败，调用 plan_task_failed 记录错误。")

    return "\n".join(parts)


def build_retry_prompt(slug: str, task: Task) -> str:
    context = get_plan_context(slug)

    parts = [f"# Plan: {slug} - Retry Task {task.id}\n"]

    if context.get("proposal"):
        parts.append(f"## Proposal\n{context['proposal']}\n")

    parts.append(f"## Task\n{task.description}\n")

    if task.error:
        parts.append(f"## Previous Error\n{task.error}\n")

    parts.append(f"Retry count: {task.retry_count}\n")
    parts.append("请重新执行此任务，注意避免之前的错误。")

    return "\n".join(parts)


def abandon_plan(slug: str) -> bool:
    plan = get_plan(slug)
    if not plan:
        return False
    if plan.status not in (PlanStatus.PROPOSED, PlanStatus.IN_PROGRESS):
        return False
    return update_plan_status(slug, PlanStatus.ABANDONED)


def reopen_plan(slug: str) -> bool:
    plan = get_plan(slug)
    if not plan:
        return False
    if plan.status not in (PlanStatus.COMPLETED, PlanStatus.ABANDONED):
        return False
    new_status = PlanStatus.IN_PROGRESS if plan.status == PlanStatus.COMPLETED else PlanStatus.PROPOSED
    return update_plan_status(slug, new_status)


def check_expired_plans() -> list[dict]:
    from datetime import datetime, timezone, timedelta

    expired = []
    now = datetime.now(timezone.utc)

    for plan in list_plans(include_archived=False):
        modified = plan.modified
        if not modified:
            continue

        try:
            modified_dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))
        except ValueError:
            continue

        days_since_modified = (now - modified_dt).days

        if plan.status == PlanStatus.PROPOSED and days_since_modified > 30:
            expired.append({
                "slug": plan.slug,
                "status": plan.status.value,
                "days_inactive": days_since_modified,
                "suggestion": "Consider continuing or abandoning this plan",
            })
        elif plan.status == PlanStatus.IN_PROGRESS and days_since_modified > 60:
            expired.append({
                "slug": plan.slug,
                "status": plan.status.value,
                "days_inactive": days_since_modified,
                "suggestion": "Consider completing or abandoning this plan",
            })

    return expired


# ── Git ──

def _git_commit(message: str) -> None:
    plans_dir = get_plans_dir()
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=plans_dir, capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=plans_dir, capture_output=True, timeout=10,
        )
    except Exception:
        pass


# ── Ledger (v2.0) ──

def _get_ledger_path(slug: str) -> Path:
    plan_dir = get_plans_dir() / slug
    return plan_dir / "_ledger.jsonl"


def append_ledger(slug: str, entry: dict) -> None:
    """追加 ledger 条目（原子写入，使用文件锁）。"""
    import json
    import fcntl
    
    ledger_path = _get_ledger_path(slug)
    
    # 使用文件锁确保原子写入
    with open(ledger_path, "a") as f:
        try:
            # 获取排他锁
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
        finally:
            # 释放锁
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_ledger(slug: str) -> list[dict]:
    """读取所有 ledger 条目。"""
    import json
    ledger_path = _get_ledger_path(slug)
    if not ledger_path.exists():
        return []
    entries = []
    for line in ledger_path.read_text().strip().split("\n"):
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def get_task_ledger(slug: str, task_id: int) -> list[dict]:
    """获取指定 task 的 ledger 条目。"""
    return [e for e in read_ledger(slug) if e.get("task_id") == task_id]


def get_last_task_commit(slug: str, task_id: int) -> str:
    """获取指定 task 最后一次成功执行的 commit。"""
    for entry in reversed(read_ledger(slug)):
        if entry.get("task_id") == task_id and entry.get("status") == "done":
            return entry.get("commit", "")
    return ""


# ── Structured Tasks (v2.0) ──

def get_structured_tasks(slug: str) -> list:
    """获取结构化任务列表。"""
    from agents.plan.task_models import parse_tasks_from_markdown
    
    plan_dir = get_plans_dir() / slug
    tasks_path = plan_dir / "tasks.md"
    if not tasks_path.exists():
        return []
    
    content = tasks_path.read_text()
    return parse_tasks_from_markdown(content)


def append_tasks_to_plan(slug: str, tasks_content: str) -> None:
    """追加任务到 plan 的 tasks.md。"""
    plan_dir = get_plans_dir() / slug
    tasks_path = plan_dir / "tasks.md"
    
    existing = tasks_path.read_text().rstrip() if tasks_path.exists() else ""
    new_content = existing + "\n\n" + tasks_content.strip()
    tasks_path.write_text(new_content)
    
    _git_commit(f"plan({slug}): append tasks")



def _get_current_commit(plan_dir: Path) -> str:
    """获取当前 HEAD commit hash。"""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=plan_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _auto_commit(plan_dir: Path, message: str, round_num: int = 1, allowed_files: list[str] | None = None) -> None:
    """自动 commit 所有未提交的变更。
    
    Args:
        plan_dir: Plan 目录
        message: Commit 消息
        round_num: 轮次号
        allowed_files: 允许 commit 的文件列表（从 task 的 **文件** 字段提取）。
                       如果提供，只 commit 这些文件；否则 commit 所有变更。
    """
    import subprocess
    try:
        # 检查是否有未提交的变更
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=plan_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if not result.stdout.strip():
            return
        
        # 如果提供了 allowed_files，只 add 这些文件
        if allowed_files:
            for file_path in allowed_files:
                subprocess.run(
                    ["git", "add", file_path],
                    cwd=plan_dir,
                    capture_output=True,
                    timeout=10,
                )
        else:
            # git add -A
            subprocess.run(
                ["git", "add", "-A"],
                cwd=plan_dir,
                capture_output=True,
                timeout=10,
                check=True,
            )
        
        # git commit
        commit_msg = message
        if round_num > 1:
            commit_msg += f" (fix round {round_num})"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=plan_dir,
            capture_output=True,
            timeout=10,
            check=True,
        )
    except Exception:
        pass


def _update_task_status_in_file(slug: str, task_id: int, status: str) -> bool:
    """更新 tasks.md 中指定 task 的状态（支持结构化与 checkbox 两种格式）。"""
    plan_dir = get_plans_dir() / slug
    tasks_path = plan_dir / "tasks.md"
    if not tasks_path.exists():
        return False

    content = tasks_path.read_text()
    status_icon = {"pending": "[ ]", "in-progress": "[~]", "done": "[x]", "failed": "[!]", "skipped": "[-]"}.get(status, "[ ]")
    new_content = None

    # 结构化格式：定位 "### Task N:" 块，块内替换 **状态** 行（不跨块）
    marker = f"### Task {task_id}:"
    start = content.find(marker)
    if start >= 0:
        nxt = content.find("### Task ", start + len(marker))
        end = nxt if nxt >= 0 else len(content)
        block = content[start:end]
        new_block, n = re.subn(
            r"(\*\*状态\*\*:\s*)\[[ x~!-]\]\s*[\w-]+",
            lambda m: f"{m.group(1)}{status_icon} {status}",
            block,
            count=1,
        )
        if n:
            new_content = content[:start] + new_block + content[end:]

    if new_content is None:
        # 简单格式：- [ ] N. description
        ch = {"pending": " ", "in-progress": "~", "done": "x", "failed": "!", "skipped": "-"}.get(status, " ")
        new_content = re.sub(
            rf"(- \[)[ x!~-](\]\s*{task_id}\.)",
            lambda m: f"{m.group(1)}{ch}{m.group(2)}",
            content,
            count=1,
            flags=re.M,
        )

    if new_content is not None and new_content != content:
        tasks_path.write_text(new_content)
        _git_commit(f"plan({slug}): update task {task_id} status to {status}")
        return True
    return False


# ── Plan State Management (v2.0) ──
# 纯状态管理，零 LLM 调用。主 Agent 自己执行 task，调用这些函数更新状态。

def start_plan_execution(slug: str) -> dict:
    """标记 plan 开始执行，返回待执行 task 列表。
    
    纯状态管理，不包含任何 LLM 调用。
    主 Agent 调用此函数后，自己逐个执行 task。
    """
    update_plan_status(slug, PlanStatus.IN_PROGRESS)
    
    tasks = get_tasks(slug)
    pending_tasks = [t for t in tasks if t.status == "pending"]
    
    return {
        "slug": slug,
        "status": "in-progress",
        "total_tasks": len(tasks),
        "pending_tasks": len(pending_tasks),
        "tasks": [
            {
                "id": t.id,
                "title": t.description,
                "file": t.file,
                "acceptance": t.acceptance,
                "status": t.status,
            }
            for t in pending_tasks
        ],
    }


def mark_task_in_progress(slug: str, task_id: int) -> bool:
    """标记 task 为执行中。主 Agent 开始执行 task 前调用。"""
    return _update_task_status_in_file(slug, task_id, "in-progress")


def mark_task_done(slug: str, task_id: int, commit: str = "", verification: dict | None = None) -> bool:
    """标记 task 为完成。主 Agent 执行完 task 后调用。
    
    Args:
        slug: Plan slug
        task_id: Task ID
        commit: 完成时的 git commit hash
        verification: 验证结果 {"command": "...", "exit_code": N, "output_snippet": "..."}
                      必须提供，否则拒绝标记完成。
    
    Returns:
        bool: 是否成功标记
    """
    from datetime import datetime, timezone
    
    # 强制验证证据
    if not verification:
        return False
    
    # 验证必须包含 command 和 exit_code
    if not verification.get("command"):
        return False
    if verification.get("exit_code") is None:
        return False
    
    ledger_entry = {
        "task_id": task_id,
        "status": "done",
        "started": "",
        "finished": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "review_rounds": 1,
        "verification": verification,
    }
    append_ledger(slug, ledger_entry)
    
    ok = _update_task_status_in_file(slug, task_id, "done")
    if ok:
        _check_plan_completion(slug)
    return ok


def mark_task_failed(slug: str, task_id: int, reason: str = "") -> bool:
    """标记 task 为失败。主 Agent 执行失败时调用。"""
    from datetime import datetime, timezone
    
    ledger_entry = {
        "task_id": task_id,
        "status": "failed",
        "started": "",
        "finished": datetime.now(timezone.utc).isoformat(),
        "commit": "",
        "review_rounds": 1,
        "verification": {},
        "failure_reason": reason,
    }
    append_ledger(slug, ledger_entry)
    
    return _update_task_status_in_file(slug, task_id, "failed")


def complete_plan(slug: str) -> bool:
    """标记 plan 为完成（ready_to_archive）。主 Agent 所有 task 完成后调用。"""
    return update_plan_status(slug, PlanStatus.READY_TO_ARCHIVE)


# ── Complex Commands (v2.0) ──

def pause_plan(slug: str) -> bool:
    """暂停 plan 执行。"""
    plan = get_plan(slug)
    if not plan:
        return False
    if plan.status != PlanStatus.IN_PROGRESS:
        return False
    return update_plan_status(slug, PlanStatus.PAUSED)


def resume_plan(slug: str) -> bool:
    """恢复 plan 执行。"""
    plan = get_plan(slug)
    if not plan:
        return False
    if plan.status != PlanStatus.PAUSED:
        return False
    return update_plan_status(slug, PlanStatus.IN_PROGRESS)


def skip_task(slug: str, task_id: int) -> bool:
    """跳过指定 task（pending/in-progress/failed → skipped）。"""
    from datetime import datetime, timezone

    task = next((x for x in get_tasks(slug) if x.id == task_id), None)
    if task is None:
        return False
    if task.status not in ("pending", "in-progress", "failed"):
        return False

    append_ledger(slug, {
        "task_id": task_id,
        "status": "skipped",
        "started": "",
        "finished": datetime.now(timezone.utc).isoformat(),
        "commit": "",
        "review_rounds": 0,
        "verification": {},
    })
    return _update_task_status_in_file(slug, task_id, "skipped")


def redo_task(slug: str, task_id: int) -> bool:
    """重做指定 task（done/skipped → pending）。failed 任务走 retry 流程。"""
    from datetime import datetime, timezone

    task = next((x for x in get_tasks(slug) if x.id == task_id), None)
    if task is None:
        return False
    if task.status not in ("done", "skipped"):
        return False

    append_ledger(slug, {
        "task_id": task_id,
        "status": "redo",
        "started": "",
        "finished": datetime.now(timezone.utc).isoformat(),
        "commit": "",
        "review_rounds": 0,
        "verification": {},
    })
    return _update_task_status_in_file(slug, task_id, "pending")


def rollback_plan(slug: str, to_task_id: int) -> dict:
    """回滚到指定 task 完成时的状态。"""
    import subprocess
    
    # 从 ledger 找到目标 task 的 commit
    target_commit = ""
    for entry in reversed(read_ledger(slug)):
        if entry.get("task_id") == to_task_id and entry.get("status") == "done":
            target_commit = entry.get("commit", "")
            break
    
    if not target_commit:
        return {"ok": False, "error": f"Task {to_task_id} not found in ledger or has no commit"}
    
    plans_dir = get_plans_dir()
    plan_dir = plans_dir / slug
    
    # stash 当前状态（安全网）
    try:
        subprocess.run(
            ["git", "stash", "push", "-m", f"pre-rollback-{slug}"],
            cwd=plan_dir, capture_output=True, timeout=10,
        )
    except Exception:
        pass
    
    # checkout 目标 commit 的文件（不移动 HEAD）
    try:
        subprocess.run(
            ["git", "checkout", target_commit, "--", "."],
            cwd=plan_dir, capture_output=True, timeout=10, check=True,
        )
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": f"Git checkout failed: {e.stderr.decode() if e.stderr else str(e)}"}
    
    # 创建新 commit 记录回滚
    _git_commit(f"plan({slug}): rollback to task {to_task_id} (commit {target_commit[:7]})")
    
    # 重置 ledger：标记 to_task_id 之后的 task 为 pending
    # 这里简化处理，实际应该解析 tasks.md 并更新状态
    
    return {
        "ok": True,
        "target_commit": target_commit,
        "message": f"Rolled back to task {to_task_id}",
    }
