"""Plan 基础设施 — CRUD 与任务解析。

Plan 是主动的目标规划，存储在 .mycode/plans/ 下，跟随主项目仓库。

这里**不再有执行状态机**，也不再有前端可调的 plan 执行端点：

- Plan 3a Task 4 删掉了 mark_task_*、start_plan_execution、rollback_plan、
  complete_plan/reopen_plan、build_task_prompt/build_retry_prompt、list_plans——
  它们随 24 个 plan_* 工具一起消失；
- Plan 3b Task A1 删掉了 frontend/server/routers/sessions.py 的 8 个 plan REST
  端点（PlanControlPanel 的整个后端），连带只被那些端点调用的状态函数
  （pause_plan / resume_plan / skip_task / redo_task / abandon_plan /
  update_plan_status / _update_task_status_in_file）、整条 ledger 链
  （_get_ledger_path / append_ledger / read_ledger / get_task_ledger /
  get_last_task_commit），以及 read_artifact（唯一调用方是已删的
  `/plan/{slug}/artifacts` 端点；草稿产物走的是另一条不经这里的
  `/plan-draft/artifacts`）。

执行状态的唯一载体是 task_list（agents/tools/task_store.py）。留下来的是：

- 计划文档的 CRUD（create_plan / add_artifact / append_tasks_to_plan / get_plan /
  get_plan_context）——plan 批准时物化路径的上游；
- 任务解析（parse_tasks_content 及其两个格式分支）——物化时把 tasks.md 文本变成
  Task 列表；
- _git_commit——create_plan / add_artifact / append_tasks_to_plan 都要它，所以
  物化路径的上游一并依赖它，**不是**孤儿。
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agents.core.workspace import get_workspace
from agents.core.frontmatter import parse_frontmatter, format_frontmatter
from agents.plan.plan_models import Plan, PlanStatus, PlanGranularity, Task


logger = logging.getLogger(__name__)

VALID_STATUSES = {s.value for s in PlanStatus}
VALID_GRANULARITIES = {g.value for g in PlanGranularity}


# tasks.md 里的 `**状态**:` 载荷归一化。原住在 agents/plan/task_models.py，那个模块
# 随 Task 4 删除（StructuredTask 与 _parse_structured_tasks 重复、LedgerEntry 随
# ledger 死），而本模块的 _parse_structured_tasks 仍需要它，于是搬到这里——它是任务
# 解析器的一部分，本来就该和解析器同住。
_STATUS_ALIASES = {
    "completed": "done", "complete": "done", "finished": "done",
    "in_progress": "in-progress", "inprogress": "in-progress", "progress": "in-progress",
    "error": "failed", "skip": "skipped",
}
_VALID_TASK_STATUSES = {"pending", "in-progress", "done", "failed", "skipped"}


def normalize_status(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = _STATUS_ALIASES.get(s, s)
    return s if s in _VALID_TASK_STATUSES else "pending"


def get_plans_dir() -> Path:
    d = get_workspace() / ".mycode" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


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

    return parse_tasks_content(tasks_path.read_text())


def parse_tasks_content(content: str) -> list[Task]:
    """get_tasks 的解析半段：tasks.md **文本** → list[Task]，不碰磁盘。

    两种格式都认（结构化 `### Task N:` 优先，回退 checkbox 简单格式），与
    get_tasks 走的是同一份逻辑，所以重量轨与轻量轨两条都对。

    单独暴露出来是给 plan 物化的追加路径用的：同一 session 第二次批准时，
    handle_plan_system_integration 走 append_tasks_to_plan，盘上的 tasks.md 已经
    是「旧 + 新」的合并体，而只有本次新增的那一块（调用方手里就有那段文本）该被
    物化进 task_list。按 slug 读会把上一轮已物化过的任务再物化一遍。
    """
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


# 任务块里不进 body 的行（Plan 3a Task 3）：
# - `### Task N:` 标题行——已进 description；
# - 七个已知标记——各自进对应字段；**验收** 刻意不重复进 body，它是单独字段，
#   抄一遍只会浪费常驻 S 与注入预算。标记判定与上方解析用的 re.search 同口径
#   （不要求行首），凡被解析器消费过的行都不该再出现在 body 里；
# - `<!-- TASKS START/END -->` 包裹注释——格式脚手架，不是任务内容。
_BODY_SKIP_LINE_RE = re.compile(
    r"^\s*###\s*Task\s+\d+:"
    r"|\*\*(?:文件|函数|接口|验收|状态|错误|重试次数)\*\*:"
    r"|^\s*<!--\s*TASKS\s+(?:START|END)\s*-->"
)


def _collect_task_body(task_block: str) -> str:
    """收集任务块里未被已知标记消费的行，作为块正文（body）。

    其余内容——散文段落、checkbox_tasks_to_structured 保留下来的 `- 改动:`/
    `- 注意:` 缩进子项——逐行原样保留（含内部空行，两端 strip），物化时与
    file/function/interface 一起组合进 TaskItem.detail。
    """
    kept = [
        line for line in task_block.split("\n")
        if not _BODY_SKIP_LINE_RE.search(line)
    ]
    return "\n".join(kept).strip()


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
            body=_collect_task_body(task_block),
        )
        tasks.append(task)
    
    return tasks


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


# ── Git ──

def _git_commit(message: str) -> None:
    """Commit 只收 `.mycode/plans/` 目录，不碰仓库其他未提交变更。

    这里提交进的是**用户自己的项目仓库**（plans 跟随主仓库，见模块 docstring），
    因此沿用用户的 git 身份，不注入合成身份——否则会篡改用户历史的作者信息。
    代价是用户未配置 git 身份时提交会失败；此前该失败被 `except: pass` 完全吞掉，
    plan 的版本追溯会静默失效且无从排查，故改为记 warning。
    """
    plans_dir = get_plans_dir()
    try:
        subprocess.run(
            ["git", "add", "--", "."],
            cwd=plans_dir, capture_output=True, timeout=10, check=True,
        )
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", "."],
            cwd=plans_dir, capture_output=True, timeout=10,
        )
        if staged.returncode == 0:
            return
        result = subprocess.run(
            ["git", "commit", "-m", message, "--", "."],
            cwd=plans_dir, capture_output=True, timeout=10, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "[plan_git] commit failed (rc=%s): %s",
                result.returncode, (result.stderr or "").strip()[:300],
            )
    except Exception as exc:
        logger.warning("[plan_git] commit error: %s: %s", type(exc).__name__, exc)


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
