"""Plan 工具处理器。"""

from __future__ import annotations

import json

from agents.plan.plan_manager import (
    create_plan,
    get_plan,
    list_plans,
    update_plan_status,
    get_tasks,
    get_next_task,
    has_failed_tasks,
    mark_task_done,
    mark_task_failed,
    add_artifact,
    read_artifact,
    archive_plan,
    build_task_prompt,
    build_retry_prompt,
    abandon_plan,
    reopen_plan,
    check_expired_plans,
)
from agents.plan.plan_models import PlanStatus, PlanGranularity
from agents.plan.plan_explore import save_explore_result, build_explore_prompt
from agents.plan.plan_recall import recall_plans, format_recall_results


def plan_propose(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    if not slug:
        return "Error: slug is required"

    priority = inp.get("priority", "P2")
    tags = inp.get("tags", [])
    granularity = inp.get("granularity", "minimal")

    try:
        gran = PlanGranularity(granularity)
    except ValueError:
        gran = PlanGranularity.MINIMAL

    try:
        plan_dir = create_plan(slug, priority=priority, tags=tags, granularity=gran)
        return json.dumps({
            "status": "created",
            "slug": slug,
            "plan_dir": str(plan_dir),
            "granularity": gran.value,
        }, indent=2)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def plan_status(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    if not slug:
        return "Error: slug is required"

    plan = get_plan(slug)
    if not plan:
        return f"Error: plan '{slug}' not found"

    tasks = get_tasks(slug)
    done = sum(1 for t in tasks if t.status == "done")
    failed = sum(1 for t in tasks if t.status == "failed")
    pending = sum(1 for t in tasks if t.status == "pending")

    result = {
        "slug": plan.slug,
        "status": plan.status.value,
        "priority": plan.priority,
        "granularity": plan.granularity.value,
        "tags": plan.tags,
        "created": plan.created,
        "modified": plan.modified,
        "tasks": {
            "total": len(tasks),
            "done": done,
            "failed": failed,
            "pending": pending,
        },
    }

    if tasks:
        result["task_list"] = [
            {"id": t.id, "description": t.description, "status": t.status}
            for t in tasks
        ]

    return json.dumps(result, ensure_ascii=False, indent=2)


def plan_list(inp: dict) -> str:
    include_archived = inp.get("include_archived", False)
    plans = list_plans(include_archived=include_archived)

    if not plans:
        return "No plans found."

    result = []
    for plan in plans:
        tasks = get_tasks(plan.slug)
        done = sum(1 for t in tasks if t.status == "done")
        result.append({
            "slug": plan.slug,
            "status": plan.status.value,
            "priority": plan.priority,
            "granularity": plan.granularity.value,
            "tags": plan.tags,
            "modified": plan.modified,
            "tasks": f"{done}/{len(tasks)}",
        })

    return json.dumps(result, ensure_ascii=False, indent=2)


def plan_update(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    if not slug:
        return "Error: slug is required"

    status = inp.get("status")
    if status:
        try:
            s = PlanStatus(status)
            if not update_plan_status(slug, s):
                return f"Error: failed to update status"
        except ValueError:
            return f"Error: invalid status '{status}'"

    tags = inp.get("tags")
    if tags is not None:
        from agents.plan.plan_manager import update_plan_tags
        if not update_plan_tags(slug, tags):
            return f"Error: failed to update tags"

    return json.dumps({"status": "updated", "slug": slug})


def plan_task_done(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    task_id = inp.get("task_id")
    if not slug or task_id is None:
        return "Error: slug and task_id are required"

    if mark_task_done(slug, int(task_id)):
        return json.dumps({"status": "done", "slug": slug, "task_id": task_id})
    return f"Error: task {task_id} not found in plan '{slug}'"


def plan_task_failed(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    task_id = inp.get("task_id")
    error = inp.get("error", "Unknown error")
    if not slug or task_id is None:
        return "Error: slug and task_id are required"

    if mark_task_failed(slug, int(task_id), error):
        return json.dumps({"status": "failed", "slug": slug, "task_id": task_id, "error": error})
    return f"Error: task {task_id} not found in plan '{slug}'"


def plan_add_artifact(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    filename = inp.get("filename", "").strip()
    content = inp.get("content", "")
    if not slug or not filename:
        return "Error: slug and filename are required"

    path = add_artifact(slug, filename, content)
    if path:
        return json.dumps({"status": "added", "slug": slug, "filename": filename, "path": str(path)})
    return f"Error: failed to add artifact"


def plan_read_artifact(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    filename = inp.get("filename", "").strip()
    if not slug or not filename:
        return "Error: slug and filename are required"

    content = read_artifact(slug, filename)
    if content is None:
        return f"Error: artifact '{filename}' not found in plan '{slug}'"
    return content


def plan_archive(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    if not slug:
        return "Error: slug is required"

    if archive_plan(slug):
        return json.dumps({"status": "archived", "slug": slug})
    return f"Error: failed to archive plan '{slug}'"


def plan_explore(inp: dict) -> str:
    topic = inp.get("topic", "").strip()
    if not topic:
        return "Error: topic is required"

    prompt = build_explore_prompt(topic)
    return json.dumps({
        "status": "explore_prompt_ready",
        "topic": topic,
        "prompt": prompt,
    })


def plan_save_explore(inp: dict) -> str:
    topic = inp.get("topic", "").strip()
    content = inp.get("content", "").strip()
    if not topic or not content:
        return "Error: topic and content are required"

    path = save_explore_result(topic, content)
    return json.dumps({
        "status": "saved",
        "topic": topic,
        "path": str(path),
    })


def plan_continue(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    if not slug:
        return "Error: slug is required"

    plan = get_plan(slug)
    if not plan:
        return f"Error: plan '{slug}' not found"

    if has_failed_tasks(slug):
        return json.dumps({
            "status": "has_failed_tasks",
            "message": "Plan has failed tasks. Use plan_retry to retry them first.",
        })

    task = get_next_task(slug)
    if not task:
        return json.dumps({
            "status": "no_pending_tasks",
            "message": "No pending tasks in this plan.",
        })

    prompt = build_task_prompt(slug, task)
    return json.dumps({
        "status": "ready",
        "task_id": task.id,
        "task_description": task.description,
        "prompt": prompt,
    })


def plan_retry(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    task_id = inp.get("task_id")
    if not slug or task_id is None:
        return "Error: slug and task_id are required"

    tasks = get_tasks(slug)
    task = None
    for t in tasks:
        if t.id == int(task_id):
            task = t
            break

    if not task:
        return f"Error: task {task_id} not found in plan '{slug}'"

    if task.status != "failed":
        return f"Error: task {task_id} is not in failed status"

    prompt = build_retry_prompt(slug, task)
    return json.dumps({
        "status": "ready",
        "task_id": task.id,
        "task_description": task.description,
        "previous_error": task.error,
        "retry_count": task.retry_count,
        "prompt": prompt,
    })


async def plan_recall(inp: dict) -> str:
    query = inp.get("query", "").strip()
    if not query:
        return "Error: query is required"

    tags = inp.get("tags", [])
    include_archived = inp.get("include_archived", False)

    results = await recall_plans(query, tags=tags, include_archived=include_archived)
    return format_recall_results(results)


def plan_abandon(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    if not slug:
        return "Error: slug is required"

    if abandon_plan(slug):
        return json.dumps({"status": "abandoned", "slug": slug})
    return f"Error: cannot abandon plan '{slug}' (must be proposed or in-progress)"


def plan_reopen(inp: dict) -> str:
    slug = inp.get("slug", "").strip()
    if not slug:
        return "Error: slug is required"

    if reopen_plan(slug):
        return json.dumps({"status": "reopened", "slug": slug})
    return f"Error: cannot reopen plan '{slug}' (must be completed or abandoned)"


def plan_check_expired(inp: dict) -> str:
    expired = check_expired_plans()
    if not expired:
        return "No expired plans found."
    return json.dumps(expired, ensure_ascii=False, indent=2)
