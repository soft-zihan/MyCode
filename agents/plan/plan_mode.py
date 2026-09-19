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

    def generate_plan_file_path(self) -> str:
        """生成 Plan 文件路径（向后兼容）。"""
        return str(self.generate_plan_dir() / "plan.md")

    def build_plan_mode_prompt(self) -> str:
        """构建 Plan 模式系统提示词（从策略文件动态加载）。"""
        plan_dir = self._plan_dir or self.generate_plan_dir()

        # 加载各阶段策略
        grill_spec_content, _, _ = load_strategy(
            "grill-spec",
            self.strategy_config.get("grill-spec", "simple"),
            self.workspace,
            plan_dir,
        )
        tasks_content, _, _ = load_strategy(
            "tasks",
            self.strategy_config.get("tasks", "structured"),
            self.workspace,
            plan_dir,
        )

        return f"""

# Plan Mode Active

Plan mode is active. You MUST NOT make any edits (except files under {plan_dir}), run non-readonly tools, or make any changes to the system.

## Plan Directory: {plan_dir}
Write your plan incrementally to files under this directory. You are allowed to edit:
- `{plan_dir}/spec.md` — 需求规格
- `{plan_dir}/tasks.md` — 任务清单
- `{plan_dir}/design.md` — 设计文档

## Workflow

### Phase 1: Grill + Spec（需求澄清 + 规格）

{grill_spec_content}

### Phase 2: Tasks（任务拆分）

{tasks_content}

### Phase 3: Exit（提交审批）

Call exit_plan_mode when your plan is ready for user review.

## 追加模式
如果 plan 目录已存在，先 read_file 读取现有内容。保留 spec.md 不变，在 tasks.md 末尾追加新的 tasks。

IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    def extract_plan_section(self, content: str, section: str) -> str:
        """从 plan 文件中提取指定区域的内容。"""
        pattern = rf"<!-- {section} START -->(.*?)<!-- {section} END -->"
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""

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
            return {"valid": False, "errors": ["plan_dir not set"], "artifacts": {}}

        # 检查 spec.md
        spec_path = self._plan_dir / "spec.md"
        if spec_path.exists():
            spec_content = spec_path.read_text()
            artifacts["spec.md"] = {
                "exists": True,
                "size": len(spec_content),
                "has_acceptance_criteria": "验收标准" in spec_content or "Acceptance" in spec_content,
            }
            if not artifacts["spec.md"]["has_acceptance_criteria"]:
                errors.append("spec.md 缺少验收标准")
        else:
            artifacts["spec.md"] = {"exists": False, "size": 0}
            errors.append("spec.md 不存在")

        # 检查 tasks.md
        tasks_path = self._plan_dir / "tasks.md"
        if tasks_path.exists():
            tasks_content = tasks_path.read_text()
            task_count = len(re.findall(r"### Task \d+:", tasks_content))
            artifacts["tasks.md"] = {
                "exists": True,
                "size": len(tasks_content),
                "task_count": task_count,
            }
            if task_count == 0:
                errors.append("tasks.md 没有定义任何任务")
        else:
            artifacts["tasks.md"] = {"exists": False, "size": 0, "task_count": 0}
            errors.append("tasks.md 不存在")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "artifacts": artifacts,
        }

    def handle_plan_system_integration(
        self,
        spec_content: str,
        plan_content: str,
        tasks_content: str,
        session: Any,
    ) -> dict | None:
        """自动创建或追加 Plan 系统条目（v2.0）。"""
        from agents.plan.plan_manager import (
            create_plan,
            get_plan,
            append_tasks_to_plan,
            add_artifact,
        )
        from agents.plan.plan_models import PlanGranularity
        from agents.logging import print_error

        if not tasks_content:
            return None

        slug = ""
        if spec_content:
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
                granularity=PlanGranularity.STANDARD,
            )

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
    def plan_file_path(self) -> str | None:
        """向后兼容：返回 plan_dir 下的 plan.md 路径。"""
        if self._plan_dir:
            return str(self._plan_dir / "plan.md")
        return None

    @plan_file_path.setter
    def plan_file_path(self, value: str | None) -> None:
        """向后兼容：从 plan_file_path 推导 plan_dir。"""
        if value:
            self._plan_dir = Path(value).parent
        else:
            self._plan_dir = None

    @property
    def pre_plan_mode(self) -> str | None:
        return self._pre_plan_mode

    @pre_plan_mode.setter
    def pre_plan_mode(self, value: str | None) -> None:
        self._pre_plan_mode = value
