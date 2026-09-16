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
