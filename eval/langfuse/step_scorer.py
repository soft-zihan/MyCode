"""Step scorer — 单步打分器。

对 Agent 轨迹中的每个 step 进行多维度打分，用于定位 Failure Onset。

打分维度（与 13-trace-eval-implementation-plan.md Phase 2 对应）：
- tool_selection: 是否选择了合适的工具（规则 + LLM Judge）
- parameter_validity: 工具参数是否合理（规则检查）
- result_interpretation: 是否正确解读工具返回（LLM Judge）
- progress: 是否向目标推进（LLM Judge）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ScoreStatus(Enum):
    """步骤打分状态。"""
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class DimensionScore:
    """单维度打分结果。"""
    status: ScoreStatus
    reason: str = ""
    confidence: float = 1.0


@dataclass
class StepScore:
    """单步综合打分结果。"""
    step_index: int
    tool_name: str
    tool_selection: DimensionScore
    parameter_validity: DimensionScore
    result_interpretation: DimensionScore
    progress: DimensionScore
    
    @property
    def overall_status(self) -> ScoreStatus:
        """综合状态：任一 FAIL 则 FAIL，否则取最差的。"""
        statuses = [
            self.tool_selection.status,
            self.parameter_validity.status,
            self.result_interpretation.status,
            self.progress.status,
        ]
        if ScoreStatus.FAIL in statuses:
            return ScoreStatus.FAIL
        if ScoreStatus.WARN in statuses:
            return ScoreStatus.WARN
        if all(s == ScoreStatus.SKIP for s in statuses):
            return ScoreStatus.SKIP
        return ScoreStatus.PASS
    
    def is_passing(self) -> bool:
        """是否通过（PASS 或 SKIP）。"""
        return self.overall_status in (ScoreStatus.PASS, ScoreStatus.SKIP)
    
    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "step_index": self.step_index,
            "tool_name": self.tool_name,
            "overall": self.overall_status.value,
            "tool_selection": {
                "status": self.tool_selection.status.value,
                "reason": self.tool_selection.reason,
            },
            "parameter_validity": {
                "status": self.parameter_validity.status.value,
                "reason": self.parameter_validity.reason,
            },
            "result_interpretation": {
                "status": self.result_interpretation.status.value,
                "reason": self.result_interpretation.reason,
            },
            "progress": {
                "status": self.progress.status.value,
                "reason": self.progress.reason,
            },
        }


@dataclass
class StepRecord:
    """步骤记录，从 trace bundle 提取。"""
    step_index: int
    tool_name: str
    tool_input: Any
    tool_output: str
    tool_level: str
    assistant_content: str
    assistant_tool_calls: list[dict]
    error: str | None = None


def extract_steps(bundle: dict[str, Any]) -> list[StepRecord]:
    """从 trace bundle 提取步骤记录。
    
    按时间顺序遍历 observations，提取每个 tool call 作为一个 step。
    """
    observations = bundle.get("observations", [])
    
    tool_obs = [o for o in observations if o.get("type") == "TOOL"]
    tool_obs.sort(key=lambda o: o.get("startTime", ""))
    
    steps = []
    for i, tool in enumerate(tool_obs):
        tool_input = tool.get("input")
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except (json.JSONDecodeError, TypeError):
                pass
        
        tool_output = tool.get("output", "")
        if isinstance(tool_output, dict):
            tool_output = json.dumps(tool_output, ensure_ascii=False)
        
        step = StepRecord(
            step_index=i,
            tool_name=tool.get("name", "unknown"),
            tool_input=tool_input,
            tool_output=tool_output or "",
            tool_level=tool.get("level", "DEFAULT"),
            assistant_content="",
            assistant_tool_calls=[tool],
            error=tool.get("statusMessage"),
        )
        steps.append(step)
    
    return steps


class RuleBasedScorer:
    """基于规则的打分器。"""
    
    READ_TOOLS = {"read_file", "glob", "grep", "bash"}
    WRITE_TOOLS = {"edit", "write_file", "bash"}
    SEARCH_TOOLS = {"grep", "glob", "read_file"}
    
    def score_tool_selection(
        self,
        step: StepRecord,
        user_request: str,
        prev_steps: list[StepRecord],
    ) -> DimensionScore:
        """规则检查：工具选择是否合理。"""
        tool = step.tool_name
        
        if step.tool_level == "ERROR":
            return DimensionScore(
                status=ScoreStatus.FAIL,
                reason=f"工具执行失败: {step.error or 'unknown error'}",
            )
        
        if tool == "bash":
            cmd = self._extract_bash_command(step.tool_input)
            if cmd:
                dangerous_patterns = ["rm -rf", "sudo", "chmod 777", "curl | sh"]
                for pattern in dangerous_patterns:
                    if pattern in cmd:
                        return DimensionScore(
                            status=ScoreStatus.WARN,
                            reason=f"危险命令: {pattern}",
                        )
        
        if tool in self.SEARCH_TOOLS and prev_steps:
            recent_searches = [s for s in prev_steps[-3:] if s.tool_name in self.SEARCH_TOOLS]
            if len(recent_searches) >= 3:
                same_tool_same_input = all(
                    s.tool_name == tool and s.tool_input == step.tool_input
                    for s in recent_searches
                )
                if same_tool_same_input:
                    return DimensionScore(
                        status=ScoreStatus.FAIL,
                        reason="连续 3 次相同搜索，可能陷入死循环",
                    )
        
        return DimensionScore(status=ScoreStatus.PASS, reason="工具选择合理")
    
    def score_parameter_validity(self, step: StepRecord) -> DimensionScore:
        """规则检查：工具参数是否合理。"""
        tool = step.tool_input
        
        if not isinstance(tool, dict):
            return DimensionScore(status=ScoreStatus.PASS, reason="参数格式跳过检查")
        
        if step.tool_name == "read_file":
            file_path = tool.get("file_path", "")
            if not file_path:
                return DimensionScore(
                    status=ScoreStatus.FAIL,
                    reason="read_file 缺少 file_path",
                )
            if file_path.startswith("/etc/") or file_path.startswith("/proc/"):
                return DimensionScore(
                    status=ScoreStatus.WARN,
                    reason=f"读取系统路径: {file_path}",
                )
        
        if step.tool_name == "edit":
            if not tool.get("file_path"):
                return DimensionScore(
                    status=ScoreStatus.FAIL,
                    reason="edit 缺少 file_path",
                )
            if not tool.get("old_string") and not tool.get("new_string"):
                return DimensionScore(
                    status=ScoreStatus.WARN,
                    reason="edit 缺少 old_string 或 new_string",
                )
        
        if step.tool_name == "bash":
            cmd = self._extract_bash_command(tool)
            if not cmd:
                return DimensionScore(
                    status=ScoreStatus.FAIL,
                    reason="bash 缺少 command",
                )
            if len(cmd) > 5000:
                return DimensionScore(
                    status=ScoreStatus.WARN,
                    reason=f"命令过长 ({len(cmd)} 字符)",
                )
        
        return DimensionScore(status=ScoreStatus.PASS, reason="参数合理")
    
    def _extract_bash_command(self, tool_input: Any) -> str:
        """从 bash 工具输入提取命令。"""
        if isinstance(tool_input, dict):
            return tool_input.get("command", "")
        if isinstance(tool_input, str):
            try:
                parsed = json.loads(tool_input)
                if isinstance(parsed, dict):
                    return parsed.get("command", "")
            except (json.JSONDecodeError, TypeError):
                pass
        return ""


class StepScorer:
    """单步打分器，组合规则打分和 LLM Judge。"""
    
    def __init__(self, use_llm_judge: bool = False):
        self.rule_scorer = RuleBasedScorer()
        self.use_llm_judge = use_llm_judge
    
    def score_step(
        self,
        step: StepRecord,
        user_request: str,
        prev_steps: list[StepRecord],
    ) -> StepScore:
        """对单步打分。"""
        tool_selection = self.rule_scorer.score_tool_selection(step, user_request, prev_steps)
        parameter_validity = self.rule_scorer.score_parameter_validity(step)
        
        result_interpretation = DimensionScore(
            status=ScoreStatus.PASS,
            reason="跳过 LLM Judge（未启用）",
        )
        
        progress = DimensionScore(
            status=ScoreStatus.PASS,
            reason="跳过 LLM Judge（未启用）",
        )
        
        return StepScore(
            step_index=step.step_index,
            tool_name=step.tool_name,
            tool_selection=tool_selection,
            parameter_validity=parameter_validity,
            result_interpretation=result_interpretation,
            progress=progress,
        )
    
    def score_trajectory(
        self,
        steps: list[StepRecord],
        user_request: str,
    ) -> list[StepScore]:
        """对整个轨迹打分。"""
        scores = []
        for i, step in enumerate(steps):
            prev_steps = steps[:i]
            score = self.score_step(step, user_request, prev_steps)
            scores.append(score)
        return scores
