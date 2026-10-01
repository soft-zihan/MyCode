"""Strategy Loader — 策略查找、加载、硬编码回退。

配置面仍是 5 个阶段，但 Task 4 之后只有前两个真的会被加载：
- grill-spec: 需求澄清 + spec 生成   ← build_plan_mode_prompt 加载
- tasks: 任务拆分                    ← build_plan_mode_prompt 加载
- execute / review / converge: 执行编排、代码审查、差距分析

后三个的内置策略文件与它们唯一的消费者（PlanExecutor，注入的是已删除的
`plan_task_*` 协议）都在 Task 4 一起删了；设计文档描述的 Review Loop / Converge
本来也没有落成代码。键**留着**是因为 PlanStrategyConfig 经
`/api/config/plan-strategies` 暴露给前端，收缩到两阶段会改 openapi.json，那是
Plan 3b 的事。所以此时给 execute/review/converge 调 load_strategy 会抛
FileNotFoundError——生产路径上没有调用方，测试也只打 grill-spec / tasks。

策略存储位置（按优先级，高优先级覆盖低优先级）：
1. 项目级：{workspace}/.mycode/plan-strategies/
2. 用户级：~/.my-code/plan-strategies/
3. 内置：agents/plan/strategies/
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from agents.config import load_config

logger = logging.getLogger(__name__)


DEFAULT_STRATEGIES: dict[str, str] = {
    "grill-spec": "simple",
    "tasks": "structured",
    "execute": "direct",
    "review": "none",
    "converge": "none",
}


VALID_STAGES = frozenset(DEFAULT_STRATEGIES.keys())


def strategy_config_from_app_config() -> dict[str, str]:
    """从全局应用配置读取五阶段策略选择（键名转为连字符阶段名）。"""

    c = load_config().plan_strategies
    return {
        "grill-spec": c.grill_spec,
        "tasks": c.tasks,
        "execute": c.execute,
        "review": c.review,
        "converge": c.converge,
    }


def _strategy_dirs(workspace: Path) -> list[Path]:
    """返回策略目录列表（按优先级降序）。"""
    return [
        workspace / ".mycode" / "plan-strategies",
        Path.home() / ".my-code" / "plan-strategies",
        Path(__file__).parent / "strategies",
    ]


def find_strategy(stage: str, name: str, workspace: Path) -> Path | None:
    """查找策略文件路径。

    Args:
        stage: 阶段名（grill-spec/tasks/execute/review/converge）
        name: 策略名（simple/structured/direct 等）
        workspace: 工作区路径

    Returns:
        策略文件路径，不存在返回 None
    """
    if stage not in VALID_STAGES:
        logger.warning(f"未知策略阶段: {stage}，有效阶段: {VALID_STAGES}")
        return None

    for dir_path in _strategy_dirs(workspace):
        path = dir_path / stage / f"{name}.md"
        if path.exists():
            return path
    return None


def load_strategy(
    stage: str,
    name: str,
    workspace: Path,
    plan_dir: Path | None = None,
) -> tuple[str, str, str]:
    """加载策略文件内容。

    Args:
        stage: 阶段名
        name: 策略名
        workspace: 工作区路径
        plan_dir: Plan 目录路径（用于替换 {plan_dir} 占位符）

    Returns:
        (content, source, resolved_name) 元组：
        - content: 策略内容
        - source: 来源（project/user/builtin/fallback）
        - resolved_name: 实际加载的策略名（可能因回退而不同）

    Raises:
        FileNotFoundError: 策略不存在且默认策略也不存在
    """
    path = find_strategy(stage, name, workspace)
    source = _detect_source(path, workspace) if path else "not_found"

    if not path:
        # 回退到默认策略
        default_name = DEFAULT_STRATEGIES.get(stage, "simple")
        path = find_strategy(stage, default_name, workspace)
        if not path:
            raise FileNotFoundError(
                f"策略不存在: {stage}/{name}，且默认策略 {default_name} 也不存在"
            )
        logger.warning(f"策略 {stage}/{name} 不存在，回退到 {default_name}")
        source = "fallback"
        name = default_name

    content = path.read_text(encoding="utf-8")

    # 替换 {plan_dir} 占位符
    if plan_dir is not None:
        content = content.replace("{plan_dir}", str(plan_dir))

    return content, source, name


def _detect_source(path: Path | None, workspace: Path) -> str:
    """检测策略来源。"""
    if path is None:
        return "not_found"

    project_dir = workspace / ".mycode" / "plan-strategies"
    user_dir = Path.home() / ".my-code" / "plan-strategies"
    builtin_dir = Path(__file__).parent / "strategies"

    try:
        if path.is_relative_to(project_dir):
            return "project"
        if path.is_relative_to(user_dir):
            return "user"
        if path.is_relative_to(builtin_dir):
            return "builtin"
    except (ValueError, OSError):
        pass

    return "unknown"


def list_strategies(workspace: Path) -> dict[str, list[dict[str, Any]]]:
    """列出所有可用的策略。

    Returns:
        {stage: [{name, path, source}, ...]} 字典
    """
    result: dict[str, list[dict[str, Any]]] = {}

    for stage in VALID_STAGES:
        strategies: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        for dir_path in _strategy_dirs(workspace):
            stage_dir = dir_path / stage
            if not stage_dir.exists():
                continue

            for md_file in stage_dir.glob("*.md"):
                name = md_file.stem
                if name in seen_names:
                    continue
                seen_names.add(name)

                source = _detect_source(md_file, workspace)
                strategies.append({
                    "name": name,
                    "path": str(md_file),
                    "source": source,
                })

        result[stage] = strategies

    return result
