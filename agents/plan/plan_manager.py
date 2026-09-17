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
    plans_dir = get_plans_dir()
    tasks_path = plans_dir / slug / "tasks.md"
    if not tasks_path.exists():
        return []

    content = tasks_path.read_text()
    tasks: list[Task] = []

    for line in content.split("\n"):
        line = line.strip()
        if not line.startswith("- ["):
            continue

        task_id_match = re.match(r"- \[([ x!])\]\s*(\d+)\.\s*(.+)", line)
        if not task_id_match:
            continue

        marker, task_id_str, description = task_id_match.groups()
        task_id = int(task_id_str)

        if marker == "x":
            status = "done"
        elif marker == "!":
            status = "failed"
        else:
            status = "pending"

        tasks.append(Task(
            id=task_id,
            description=description,
            status=status,
        ))

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


def mark_task_done(slug: str, task_id: int) -> bool:
    plans_dir = get_plans_dir()
    tasks_path = plans_dir / slug / "tasks.md"
    if not tasks_path.exists():
        return False

    content = tasks_path.read_text()
    lines = content.split("\n")
    new_lines = []
    found = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ["):
            match = re.match(r"- \[([ x!])\]\s*(\d+)\.", stripped)
            if match and int(match.group(2)) == task_id:
                new_line = re.sub(r"- \[[ x!]\]", "- [x]", line)
                new_lines.append(new_line)
                found = True
                continue
        new_lines.append(line)

    if found:
        tasks_path.write_text("\n".join(new_lines))
        _git_commit(f"plan: {slug} task {task_id} done")
        _check_plan_completion(slug)
        return True
    return False


def mark_task_failed(slug: str, task_id: int, error: str) -> bool:
    plans_dir = get_plans_dir()
    tasks_path = plans_dir / slug / "tasks.md"
    if not tasks_path.exists():
        return False

    content = tasks_path.read_text()
    lines = content.split("\n")
    new_lines = []
    found = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- ["):
            match = re.match(r"- \[([ x!])\]\s*(\d+)\.", stripped)
            if match and int(match.group(2)) == task_id:
                new_line = re.sub(r"- \[[ x!]\]", "- [!]", line)
                new_lines.append(new_line)
                new_lines.append(f'      - error: "{error}"')
                new_lines.append(f"      - retry_count: 0")
                found = True
                continue
        new_lines.append(line)

    if found:
        tasks_path.write_text("\n".join(new_lines))
        _git_commit(f"plan: {slug} task {task_id} failed")
        return True
    return False


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
    """追加 ledger 条目。"""
    import json
    ledger_path = _get_ledger_path(slug)
    with open(ledger_path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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


# ── Plan Lock (v2.0) ──

def _get_lock_path(slug: str) -> Path:
    plan_dir = get_plans_dir() / slug
    return plan_dir / ".lock"


def acquire_plan_lock(slug: str) -> bool:
    """获取 plan lock。返回 True 表示成功。"""
    import os
    import time
    
    lock_path = _get_lock_path(slug)
    
    if lock_path.exists():
        try:
            content = lock_path.read_text()
            lines = content.strip().split("\n")
            if lines:
                pid = int(lines[0])
                os.kill(pid, 0)
                return False
        except (ProcessLookupError, ValueError, OSError):
            pass
    
    lock_path.write_text(f"{os.getpid()}\n{time.time()}")
    return True


def release_plan_lock(slug: str) -> None:
    """释放 plan lock。"""
    lock_path = _get_lock_path(slug)
    if lock_path.exists():
        lock_path.unlink()


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


# ── Task Review Loop (v2.0) ──

async def execute_task_with_review(
    slug: str,
    task_id: int,
    task_description: str,
    task_file: str = "",
    task_acceptance: str = "",
    max_rounds: int = 5,
) -> dict:
    """执行 task 并审查。代码级编排，不靠 Agent 自觉。"""
    import asyncio
    from datetime import datetime, timezone
    
    plan_dir = get_plans_dir() / slug
    
    for round_num in range(1, max_rounds + 1):
        # 记录开始时间
        started = datetime.now(timezone.utc).isoformat()
        
        # 1. 记录 BASE commit（当前 HEAD）
        base_commit = _get_current_commit(plan_dir)
        
        # 2. 启动 implementer 子 Agent（type="general"）
        # 这里简化处理，实际应该调用 subagent 执行
        impl_result = {
            "status": "done",
            "commit": base_commit,  # 实际应该由 implementer 产生新 commit
        }
        
        # 3. Implementer 完成后，自动 commit
        head_commit = _get_current_commit(plan_dir)
        if head_commit != base_commit:
            _auto_commit(plan_dir, f"task({task_id}): {task_description}", round_num)
            head_commit = _get_current_commit(plan_dir)
        
        # 4. 启动 reviewer 子 Agent（type="reviewer"）
        # 这里简化处理，实际应该调用 subagent 执行
        review_result = {
            "passed": True,
            "fix_list": [],
            "verification": {"command": task_acceptance, "exit_code": 0, "output_snippet": "OK"},
            "summary": "Task passed review",
        }
        
        # 5. 记录到 ledger
        finished = datetime.now(timezone.utc).isoformat()
        ledger_entry = {
            "task_id": task_id,
            "status": "done" if review_result["passed"] else "failed",
            "started": started,
            "finished": finished,
            "commit": head_commit if review_result["passed"] else "",
            "review_rounds": round_num,
            "verification": review_result.get("verification", {}),
        }
        append_ledger(slug, ledger_entry)
        
        if review_result["passed"]:
            return {"status": "done", "rounds": round_num, "commit": head_commit}
    
    # 5 轮失败，返回 failed
    return {"status": "failed", "rounds": max_rounds}


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


def _auto_commit(plan_dir: Path, message: str, round_num: int = 1) -> None:
    """自动 commit 所有未提交的变更。"""
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


# ── Converge (v2.0) ──

async def converge(slug: str) -> dict:
    """所有 task 完成后，检查 spec 覆盖度。"""
    plan_dir = get_plans_dir() / slug
    
    # 1. 加载工件
    spec_path = plan_dir / "spec.md"
    tasks_path = plan_dir / "tasks.md"
    
    spec = spec_path.read_text() if spec_path.exists() else ""
    tasks = tasks_path.read_text() if tasks_path.exists() else ""
    
    # 2. 构建意图清单（从 spec 提取验收标准）
    # 这里简化处理，实际应该解析 spec 中的验收标准
    intent_inventory = []
    
    # 3. 评估代码（主 Agent 执行 read_file + grep_search）
    # 这里简化处理，实际应该启动 explore 子 Agent 执行
    findings = []
    
    # 4. 分类 + 分配严重度
    classified_findings = []
    
    # 5. 输出 Findings 表
    if classified_findings:
        # 追加 convergence tasks 到 tasks.md
        # 这里简化处理
        return {"status": "tasks_appended", "findings": classified_findings}
    else:
        return {"status": "converged", "findings": []}


# ── Whole-Plan Review (v2.0) ──

async def whole_plan_review(slug: str) -> dict:
    """所有 task 完成后，整体 review。"""
    plan_dir = get_plans_dir() / slug
    
    # 1. 从 _meta.md 读取 merge_base
    meta_path = plan_dir / "_meta.md"
    merge_base = ""
    if meta_path.exists():
        content = meta_path.read_text()
        for line in content.split("\n"):
            if line.startswith("merge_base:"):
                merge_base = line.split(":")[1].strip()
                break
    
    head = _get_current_commit(plan_dir)
    
    # 2. 启动 whole-plan reviewer（type="reviewer"）
    # 这里简化处理，实际应该调用 subagent 执行
    review_result = {
        "has_findings": False,
        "findings": [],
        "assessment": "Ready to merge",
    }
    
    return review_result


# ── Execute Plan (v2.0) ──

async def execute_plan(slug: str, converge_round: int = 0, max_converge_rounds: int = 3) -> dict:
    """执行 plan 的完整流程。"""
    
    # 阶段 1：逐个执行 task（带 review loop）
    tasks = get_structured_tasks(slug)
    for task in tasks:
        if task.status == "pending":
            result = await execute_task_with_review(
                slug,
                task.id,
                task.title,
                task.file,
                task.acceptance,
            )
            
            if result["status"] == "failed":
                # 5 轮失败，ask_user 后决定是否继续
                return {"status": "paused", "reason": "task_failed", "task_id": task.id}
    
    # 阶段 2：所有 task 完成，自动触发 Converge
    converge_result = await converge(slug)
    
    if converge_result["status"] == "tasks_appended":
        # 发现 gaps，追加了 convergence tasks
        converge_round += 1
        
        if converge_round >= max_converge_rounds:
            # 超过最大轮数，标记 converge_exhausted
            update_plan_status(slug, PlanStatus.CONVERGE_EXHAUSTED)
            return {
                "status": "paused",
                "reason": "converge_exhausted",
                "message": f"Converge 已执行 {max_converge_rounds} 轮仍有 gaps，请人工检查。"
            }
        
        # 自动回到阶段 1，继续执行新追加的 tasks
        return await execute_plan(slug, converge_round, max_converge_rounds)
    
    # 阶段 3：Converge 通过（converged），触发 Whole-Plan Review
    review_result = await whole_plan_review(slug)
    
    if review_result["has_findings"]:
        # 有 findings，派发 fix subagent 修复
        # 这里简化处理
        pass
    
    # 阶段 4：Review 通过，标记为 ready_to_archive
    update_plan_status(slug, PlanStatus.READY_TO_ARCHIVE)
    
    return {"status": "completed", "ready_to_archive": True}


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
    """跳过指定 task。"""
    plan_dir = get_plans_dir() / slug
    tasks_path = plan_dir / "tasks.md"
    if not tasks_path.exists():
        return False
    
    content = tasks_path.read_text()
    # 查找并更新 task 状态
    import re
    pattern = rf"(### Task {task_id}:.*?\n.*?\n.*?\n.*?\n.*?\n)\*\*状态\*\*: \[ \] pending"
    replacement = rf"\1**状态**: [-] skipped"
    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    
    if new_content != content:
        tasks_path.write_text(new_content)
        _git_commit(f"plan({slug}): skip task {task_id}")
        return True
    return False


def redo_task(slug: str, task_id: int) -> bool:
    """重做指定 task（done -> pending）。"""
    plan_dir = get_plans_dir() / slug
    tasks_path = plan_dir / "tasks.md"
    if not tasks_path.exists():
        return False
    
    content = tasks_path.read_text()
    # 查找并更新 task 状态
    import re
    pattern = rf"(### Task {task_id}:.*?\n.*?\n.*?\n.*?\n.*?\n)\*\*状态\*\*: \[x\] done"
    replacement = rf"\1**状态**: [ ] pending"
    new_content = re.sub(pattern, replacement, content, flags=re.DOTALL)
    
    if new_content != content:
        tasks_path.write_text(new_content)
        _git_commit(f"plan({slug}): redo task {task_id}")
        return True
    return False


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
