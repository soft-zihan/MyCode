"""Plan Mode Manager — Plan 模式逻辑封装。

职责：
- Plan 目录管理（{plan_dir}/ 下存储 spec.md, tasks.md 等）
- Plan 模式提示词构建（从策略文件动态加载）
- Plan 区域解析
- Plan 系统集成
- 产物校验
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Awaitable, Any

from agents.plan.strategy_loader import load_strategy, VALID_STAGES


class PlanModeManager:
    """Plan 模式逻辑管理器。"""

    def __init__(
        self,
        *,
        workspace: Path,
        session_id: str,
        plan_approval_fn: Callable[[str], Awaitable[bool]] | None = None,
        strategy_config: dict[str, str] | None = None,
    ):
        self.workspace = workspace
        self.session_id = session_id
        self.plan_approval_fn = plan_approval_fn
        self.strategy_config = strategy_config or {}
        self._plan_dir: Path | None = None
        self._pre_plan_mode: str | None = None

    def generate_plan_dir(self) -> Path:
        """生成 Plan 目录路径。"""
        d = self.workspace / ".mycode" / "plans" / f"plan-{self.session_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def build_plan_mode_prompt(self) -> str:
        """构建 Plan 模式系统提示词（从策略文件动态加载）。"""
        plan_dir = self._plan_dir or self.generate_plan_dir()

        # 加载各阶段策略（全局应用配置为底，会话级覆盖）
        from agents.plan.strategy_loader import strategy_config_from_app_config
        cfg = {**strategy_config_from_app_config(), **self.strategy_config}
        grill_spec_content, _, _ = load_strategy(
            "grill-spec",
            cfg.get("grill-spec", "simple"),
            self.workspace,
            plan_dir,
        )
        tasks_content, _, _ = load_strategy(
            "tasks",
            cfg.get("tasks", "structured"),
            self.workspace,
            plan_dir,
        )

        return f"""

# Plan Mode Active

Plan mode is active. You MUST NOT make any edits (except files under {plan_dir}), run non-readonly tools, or make any changes to the system.

## Plan Directory: {plan_dir}

## 双轨规划 — 按任务复杂度选择轨道（二选一，不要混用）

### 轻量轨（默认，适合大多数任务）：单一 plan.md
分钟级~小时级、涉及文件较少的任务，只写一个文件：
- `{plan_dir}/plan.md` — 必须包含以下小节：
  - `## 背景`（为什么做）
  - `## 方案`(怎么做)
  - `## 任务清单`（checkbox 格式：`- [ ] 1. 任务描述`，每行一个，编号从 1 开始）
  - `## 验收`（如何验证整体完成）

### 重量轨（复杂任务显式升级）：三文档
跨多模块、需要审计、跨会话的大任务，写三个文件：
- `{plan_dir}/spec.md` — 需求规格（必须含"验收标准"小节）
- `{plan_dir}/design.md` — 设计文档
- `{plan_dir}/tasks.md` — 任务清单

#### Phase 1: Grill + Spec（需求澄清 + 规格）

{grill_spec_content}

#### Phase 2: Tasks（任务拆分）

{tasks_content}

## 提交审批

Call exit_plan_mode when your plan is ready for user review.

## 追加模式
如果 plan 目录已存在，先 read_file 读取现有内容。保留已有规格/方案不变，在任务清单末尾追加新的 tasks。

IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    def read_draft_artifacts(self) -> dict[str, Any]:
        """读取草稿产物并判定轨道。

        plan.md 存在且 spec.md 不存在 → 轻量轨（minimal）；否则重量轨（standard）。
        """
        result = {"granularity": "standard", "spec": "", "design": "", "tasks": "", "plan": ""}
        if not self._plan_dir:
            return result

        def _read(name: str) -> str:
            f = self._plan_dir / name
            return f.read_text(encoding="utf-8") if f.exists() else ""

        plan_md = _read("plan.md")
        spec = _read("spec.md")
        if plan_md and not spec:
            result["granularity"] = "minimal"
            result["plan"] = plan_md
        else:
            result["spec"] = spec
            result["design"] = _read("design.md")
            result["tasks"] = _read("tasks.md")
            result["plan"] = plan_md
        return result

    @staticmethod
    def checkbox_tasks_to_structured(content: str) -> str:
        """把 checkbox 任务清单转为结构化 tasks.md（执行状态机依赖结构化格式更新状态）。"""
        status_map = {"x": "done", "!": "failed", "~": "in-progress", "-": "skipped"}
        icon_map = {"pending": "[ ]", "in-progress": "[~]", "done": "[x]", "failed": "[!]", "skipped": "[-]"}
        out = ["<!-- TASKS START -->"]
        for line in content.split("\n"):
            m = re.match(r"\s*- \[([ x!~-])\]\s*(\d+)\.\s*(.+)", line)
            if not m:
                continue
            marker, tid, desc = m.groups()
            status = status_map.get(marker, "pending")
            out.append(f"### Task {tid}: {desc.strip()}")
            out.append(f"- **状态**: {icon_map[status]} {status}")
            out.append("")
        out.append("<!-- TASKS END -->")
        return "\n".join(out) + "\n"

    def validate_plan_artifacts(self) -> dict[str, Any]:
        """校验 Plan 产物完整性。

        Returns:
            {
                "valid": bool,
                "errors": list[str],
                "artifacts": {
                    "spec.md": {"exists": bool, "size": int},
                    "tasks.md": {"exists": bool, "size": int, "task_count": int},
                }
            }
        """
        errors: list[str] = []
        artifacts: dict[str, dict[str, Any]] = {}

        if not self._plan_dir:
            return {"valid": False, "errors": ["plan_dir not set"], "artifacts": {}, "granularity": "standard"}

        from agents.plan.plan_manager import count_tasks

        plan_path = self._plan_dir / "plan.md"
        spec_path = self._plan_dir / "spec.md"
        minimal = plan_path.exists() and not spec_path.exists()

        if minimal:
            # 轻量轨：plan.md 非空 + 含 checkbox 任务清单
            content = plan_path.read_text(encoding="utf-8")
            task_count = count_tasks(content)
            artifacts["plan.md"] = {"exists": True, "size": len(content), "task_count": task_count}
            if not content.strip():
                errors.append("plan.md 为空")
            if task_count == 0:
                errors.append("plan.md 缺少任务清单（checkbox 格式：- [ ] 1. 任务描述）")
        else:
            # 重量轨：spec.md（含验收标准）+ tasks.md（任务数 > 0）
            if spec_path.exists():
                spec_content = spec_path.read_text(encoding="utf-8")
                has_acc = "验收标准" in spec_content or "Acceptance" in spec_content
                artifacts["spec.md"] = {"exists": True, "size": len(spec_content), "has_acceptance_criteria": has_acc}
                if not has_acc:
                    errors.append("spec.md 缺少验收标准")
            else:
                artifacts["spec.md"] = {"exists": False, "size": 0}
                errors.append("spec.md 不存在（重量轨需 spec.md+tasks.md；轻量轨请写 plan.md）")

            tasks_path = self._plan_dir / "tasks.md"
            if tasks_path.exists():
                tasks_content = tasks_path.read_text(encoding="utf-8")
                task_count = count_tasks(tasks_content)
                artifacts["tasks.md"] = {"exists": True, "size": len(tasks_content), "task_count": task_count}
                if task_count == 0:
                    errors.append("tasks.md 没有定义任何任务")
            else:
                artifacts["tasks.md"] = {"exists": False, "size": 0, "task_count": 0}
                errors.append("tasks.md 不存在")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "artifacts": artifacts,
            "granularity": "minimal" if minimal else "standard",
        }

    def handle_plan_system_integration(
        self,
        spec_content: str,
        plan_content: str,
        tasks_content: str,
        session: Any,
        granularity: str = "standard",
        plan_md: str = "",
    ) -> dict | None:
        """自动创建或追加 Plan 系统条目。

        granularity="minimal"：轻量轨，plan.md 为源文档，
        checkbox 任务清单转换为结构化 tasks.md（执行状态机依赖）。
        """
        from agents.plan.plan_manager import (
            create_plan,
            get_plan,
            append_tasks_to_plan,
            add_artifact,
        )
        from agents.plan.plan_models import PlanGranularity
        from agents.logging import print_error

        if granularity == "minimal":
            if not plan_md.strip():
                return None
            structured_tasks = self.checkbox_tasks_to_structured(plan_md)
            if "### Task" not in structured_tasks:
                return None
            tasks_content = tasks_content or structured_tasks
        elif not tasks_content:
            return None

        slug = ""
        if granularity == "minimal":
            title_match = re.search(r"^#\s+(.+)", plan_md, re.M)
            if title_match:
                slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", title_match.group(1).strip().lower()).strip("-")[:40]
        elif spec_content:
            title_match = re.search(r"# Spec:\s*(.+)", spec_content)
            if title_match:
                slug = re.sub(r"[^a-z0-9]+", "-", title_match.group(1).strip().lower()).strip("-")[:40]

        if not slug:
            slug = f"plan-{self.session_id}"

        if session.plan_slug:
            existing_plan = get_plan(session.plan_slug)
            if existing_plan and existing_plan.status.value not in ("archived", "abandoned"):
                try:
                    append_tasks_to_plan(session.plan_slug, tasks_content)
                    return {"slug": session.plan_slug, "action": "appended"}
                except Exception as e:
                    print_error(f"Failed to append tasks to plan: {e}")
                    return None

        try:
            plan_dir = create_plan(
                slug=slug,
                granularity=PlanGranularity.MINIMAL if granularity == "minimal" else PlanGranularity.STANDARD,
            )

            if granularity == "minimal":
                add_artifact(slug, "plan.md", plan_md)
            else:
                if spec_content:
                    add_artifact(slug, "spec.md", spec_content)
                if plan_content:
                    add_artifact(slug, "design.md", plan_content)

            add_artifact(slug, "tasks.md", tasks_content)

            session.plan_slug = slug
            session.append("session/plan_linked", {"plan_slug": slug})

            return {"slug": slug, "action": "created", "plan_dir": str(plan_dir)}
        except Exception as e:
            print_error(f"Failed to create plan: {e}")
            return None

    @property
    def plan_dir(self) -> Path | None:
        return self._plan_dir

    @plan_dir.setter
    def plan_dir(self, value: Path | None) -> None:
        self._plan_dir = value

    @property
    def pre_plan_mode(self) -> str | None:
        return self._pre_plan_mode

    @pre_plan_mode.setter
    def pre_plan_mode(self, value: str | None) -> None:
        self._pre_plan_mode = value
