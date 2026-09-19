"""Plan Executor — Plan 执行编排器。

负责编排 Plan 的执行流程：
- execute: 任务执行（direct/subagent/tdd）
- review: 代码审查（none/single-axis/dual-axis）
- converge: 差距分析（none/gap-analysis）

编排器根据策略配置决定执行方式，并处理阶段间的转换。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from agents.plan.strategy_loader import load_strategy, get_strategy_snapshot
from agents.plan.plan_manager import (
    get_tasks,
    get_plan,
    mark_task_in_progress,
    mark_task_done,
    mark_task_failed,
    complete_plan,
    read_ledger,
    append_ledger,
)

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """任务执行结果"""
    task_id: int
    passed: bool
    commit: str = ""
    verification: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class ReviewResult:
    """审查结果"""
    passed: bool
    fix_list: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass
class ConvergeResult:
    """收敛结果"""
    findings: list[dict[str, Any]] = field(default_factory=list)
    convergence_tasks: list[dict[str, Any]] = field(default_factory=list)


class PlanExecutor:
    """Plan 执行编排器"""

    def __init__(
        self,
        *,
        slug: str,
        workspace: Path,
        plan_dir: Path,
        strategy_config: dict[str, str] | None = None,
        # 回调函数
        execute_task_fn: Callable[[int, str], Awaitable[TaskResult]] | None = None,
        spawn_reviewer_fn: Callable[[str, dict], Awaitable[ReviewResult]] | None = None,
        run_converge_fn: Callable[[str], Awaitable[ConvergeResult]] | None = None,
    ):
        self.slug = slug
        self.workspace = workspace
        self.plan_dir = plan_dir
        self.strategy_config = strategy_config or {}

        # 回调函数（由 Agent 注入）
        self._execute_task_fn = execute_task_fn
        self._spawn_reviewer_fn = spawn_reviewer_fn
        self._run_converge_fn = run_converge_fn

        # 策略快照（用于持久化）
        self._strategy_snapshot: dict[str, dict[str, str]] = {}

    def get_strategy_snapshot(self) -> dict[str, dict[str, str]]:
        """获取策略快照"""
        if not self._strategy_snapshot:
            self._strategy_snapshot = get_strategy_snapshot(
                self.workspace,
                self.strategy_config,
            )
        return self._strategy_snapshot

    async def execute_all_tasks(self) -> list[TaskResult]:
        """执行所有待执行任务"""
        tasks = get_tasks(self.slug)
        pending_tasks = [t for t in tasks if t.status == "pending"]

        results: list[TaskResult] = []
        for task in pending_tasks:
            result = await self.execute_single_task(task.id, task.description)
            results.append(result)

        return results

    async def execute_single_task(self, task_id: int, task_title: str) -> TaskResult:
        """执行单个任务"""
        execute_strategy = self.strategy_config.get("execute", "direct")

        # 标记任务开始
        mark_task_in_progress(self.slug, task_id)

        # 记录开始时间
        append_ledger(self.slug, {
            "task_id": task_id,
            "status": "started",
            "execute_strategy": execute_strategy,
        })

        # 根据策略选择执行方式
        if execute_strategy == "direct":
            result = await self._execute_direct(task_id, task_title)
        elif execute_strategy == "subagent":
            result = await self._execute_subagent(task_id, task_title)
        elif execute_strategy == "tdd":
            result = await self._execute_tdd(task_id, task_title)
        else:
            result = TaskResult(
                task_id=task_id,
                passed=False,
                error=f"Unknown execute strategy: {execute_strategy}",
            )

        # 更新状态
        if result.passed:
            mark_task_done(
                self.slug,
                task_id,
                commit=result.commit,
                verification=result.verification,
            )
        else:
            mark_task_failed(self.slug, task_id, reason=result.error)

        return result

    async def _execute_direct(self, task_id: int, task_title: str) -> TaskResult:
        """直接执行（主 Agent 执行）"""
        if self._execute_task_fn:
            return await self._execute_task_fn(task_id, task_title)
        return TaskResult(
            task_id=task_id,
            passed=False,
            error="execute_task_fn not provided",
        )

    async def _execute_subagent(self, task_id: int, task_title: str) -> TaskResult:
        """子 Agent 执行"""
        # 子 Agent 执行逻辑与直接执行类似，但使用隔离的上下文
        # 实际实现时，主 Agent 会调用 agent 工具派发子 Agent
        return await self._execute_direct(task_id, task_title)

    async def _execute_tdd(self, task_id: int, task_title: str) -> TaskResult:
        """TDD 循环执行"""
        # TDD 循环：先写测试，再实现功能
        # 实际实现时，主 Agent 会按 TDD 流程执行
        return await self._execute_direct(task_id, task_title)

    async def review_task(self, task_id: int, task_context: dict) -> ReviewResult:
        """审查任务"""
        review_strategy = self.strategy_config.get("review", "none")

        if review_strategy == "none":
            return ReviewResult(passed=True, summary="Review skipped")

        if review_strategy == "single-axis":
            return await self._review_single_axis(task_id, task_context)
        elif review_strategy == "dual-axis":
            return await self._review_dual_axis(task_id, task_context)
        else:
            return ReviewResult(
                passed=False,
                summary=f"Unknown review strategy: {review_strategy}",
            )

    async def _review_single_axis(
        self, task_id: int, task_context: dict
    ) -> ReviewResult:
        """单轴审查"""
        if self._spawn_reviewer_fn:
            return await self._spawn_reviewer_fn("single-axis", task_context)
        return ReviewResult(
            passed=False,
            summary="spawn_reviewer_fn not provided",
        )

    async def _review_dual_axis(
        self, task_id: int, task_context: dict
    ) -> ReviewResult:
        """双轴审查"""
        if self._spawn_reviewer_fn:
            return await self._spawn_reviewer_fn("dual-axis", task_context)
        return ReviewResult(
            passed=False,
            summary="spawn_reviewer_fn not provided",
        )

    async def run_fix_loop(
        self,
        task_id: int,
        review_result: ReviewResult,
        max_rounds: int = 3,
    ) -> tuple[bool, list[ReviewResult]]:
        """运行 Fix Loop

        Returns:
            (passed, review_history) 元组
        """
        review_history: list[ReviewResult] = [review_result]

        for round_num in range(1, max_rounds + 1):
            if review_result.passed:
                return True, review_history

            # 记录修复轮次
            append_ledger(self.slug, {
                "task_id": task_id,
                "status": "fix_round",
                "round": round_num,
                "fix_count": len(review_result.fix_list),
            })

            # 主 Agent 执行修复
            if self._execute_task_fn:
                await self._execute_task_fn(task_id, f"Fix round {round_num}")

            # 重新审查
            review_result = await self.review_task(task_id, {})
            review_history.append(review_result)

        # 超过最大轮次
        return False, review_history

    async def converge(self) -> ConvergeResult:
        """运行收敛分析"""
        converge_strategy = self.strategy_config.get("converge", "none")

        if converge_strategy == "none":
            return ConvergeResult()

        if converge_strategy == "gap-analysis":
            return await self._converge_gap_analysis()
        else:
            return ConvergeResult()

    async def _converge_gap_analysis(self) -> ConvergeResult:
        """差距分析"""
        if self._run_converge_fn:
            return await self._run_converge_fn("gap-analysis")
        return ConvergeResult()

    async def run_converge_loop(self, max_rounds: int = 3) -> tuple[bool, list[ConvergeResult]]:
        """运行收敛循环

        Returns:
            (converged, converge_history) 元组
        """
        converge_history: list[ConvergeResult] = []

        for round_num in range(1, max_rounds + 1):
            result = await self.converge()
            converge_history.append(result)

            if not result.findings:
                return True, converge_history

            # 记录收敛轮次
            append_ledger(self.slug, {
                "status": "converge_round",
                "round": round_num,
                "finding_count": len(result.findings),
            })

            # 执行收敛任务
            for task_def in result.convergence_tasks:
                # 主 Agent 执行收敛任务
                pass

        # 超过最大轮次
        return False, converge_history

    def complete(self) -> bool:
        """标记 Plan 完成"""
        return complete_plan(self.slug)


def create_executor(
    slug: str,
    workspace: Path,
    strategy_config: dict[str, str] | None = None,
) -> PlanExecutor:
    """创建 PlanExecutor 实例"""
    plan = get_plan(slug)
    if not plan:
        raise ValueError(f"Plan not found: {slug}")

    plan_dir = Path(plan.plan_dir)

    return PlanExecutor(
        slug=slug,
        workspace=workspace,
        plan_dir=plan_dir,
        strategy_config=strategy_config,
    )
