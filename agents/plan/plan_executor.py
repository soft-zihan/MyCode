"""Plan Executor — 执行阶段策略插件引擎。

主 Agent 的执行模型是「LLM 自主执行 + 工具状态机 + verification 门禁」，
策略以可插拔的执行指导（prompt 块）方式被消费：

- execute:  批准通过后注入执行指令（direct/subagent/tdd/自定义）
- review:   plan_task_done 返回时注入自查指导（none/single-axis/dual-axis/自定义）
- converge: 执行指令中注入完成前收敛指导（none/gap-analysis/自定义）

策略即 markdown 文件，三级查找（高优先级覆盖低优先级）：
1. 项目级: {workspace}/.mycode/plan-strategies/{stage}/{name}.md
2. 用户级: ~/.my-code/plan-strategies/{stage}/{name}.md
3. 内置:   agents/plan/strategies/{stage}/{name}.md

自定义策略 = 在项目级/用户级目录放置同名 md 文件，并在 UI 策略选择器中选用。
"""

from __future__ import annotations

import logging
from pathlib import Path

from agents.plan.strategy_loader import (
    DEFAULT_STRATEGIES,
    load_strategy,
    get_strategy_snapshot,
    strategy_config_from_app_config,
)
from agents.plan.plan_manager import complete_plan, get_plans_dir
from agents.core.workspace import get_workspace

logger = logging.getLogger(__name__)


class PlanExecutor:
    """策略插件引擎：按策略配置构建执行阶段的 prompt 块。"""

    def __init__(
        self,
        *,
        slug: str,
        workspace: Path,
        strategy_config: dict[str, str] | None = None,
    ):
        self.slug = slug
        self.workspace = Path(workspace)
        self.strategy_config = {**DEFAULT_STRATEGIES, **(strategy_config or {})}

    @classmethod
    def for_plan(cls, slug: str) -> "PlanExecutor":
        """从全局应用配置 + 当前 workspace 上下文创建。"""

        return cls(
            slug=slug,
            workspace=get_workspace(),
            strategy_config=strategy_config_from_app_config(),
        )

    @property
    def plan_dir(self) -> Path:
        return get_plans_dir() / self.slug

    def get_strategy_snapshot(self) -> dict[str, dict[str, str]]:
        """策略快照（用于持久化审计）。"""
        return get_strategy_snapshot(self.workspace, self.strategy_config)

    def _load(self, stage: str) -> str:
        name = self.strategy_config.get(stage) or DEFAULT_STRATEGIES.get(stage, "")
        try:
            content, _, resolved = load_strategy(stage, name, self.workspace, self.plan_dir)
        except FileNotFoundError:
            logger.warning(f"策略加载失败: {stage}/{name}")
            return ""
        if resolved != name:
            logger.warning(f"策略 {stage}/{name} 回退为 {resolved}")
        return content.replace("{slug}", self.slug)

    def build_execute_instructions(self) -> str:
        """execute 阶段：任务执行方式指导。"""
        return self._load("execute")

    def build_review_guidance(self) -> str:
        """review 阶段：任务完成后的自查指导（none 返回空）。"""
        if self.strategy_config.get("review", "none") == "none":
            return ""
        return self._load("review")

    def build_converge_guidance(self) -> str:
        """converge 阶段：plan 完成前的收敛指导（none 返回空）。"""
        if self.strategy_config.get("converge", "none") == "none":
            return ""
        return self._load("converge")

    def complete(self) -> bool:
        """标记 Plan 完成（ready_to_archive）。"""
        return complete_plan(self.slug)
