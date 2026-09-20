"""Trajectory diagnoser — 轨迹诊断器。

对 Agent 轨迹进行逐步诊断，定位 Failure Onset（首次出现错误决策的步骤），
过滤之后的级联失败，只保留高质量轨迹片段。

核心概念：
- Failure Onset: 首次出现错误决策的步骤
- 级联失败: Onset 之后因错误传播导致的后续失败
- 高质量轨迹: Onset 之前的所有步骤（可用于 SFT/RL 训练）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eval.langfuse.step_scorer import (
    ScoreStatus,
    StepRecord,
    StepScore,
    StepScorer,
    extract_steps,
)


@dataclass
class FailureOnset:
    """Failure Onset 信息。"""
    step_index: int
    tool_name: str
    reason: str
    dimension: str
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "tool_name": self.tool_name,
            "reason": self.reason,
            "dimension": self.dimension,
        }


@dataclass
class DiagnosisResult:
    """诊断结果。"""
    trace_id: str
    user_request: str
    total_steps: int
    scores: list[StepScore]
    failure_onset: FailureOnset | None
    high_quality_steps: list[StepRecord]
    low_quality_steps: list[StepRecord]
    cascade_failure_count: int
    
    @property
    def quality_ratio(self) -> float:
        """高质量轨迹占比。"""
        if self.total_steps == 0:
            return 0.0
        return len(self.high_quality_steps) / self.total_steps
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "user_request": self.user_request,
            "total_steps": self.total_steps,
            "failure_onset": self.failure_onset.to_dict() if self.failure_onset else None,
            "high_quality_count": len(self.high_quality_steps),
            "low_quality_count": len(self.low_quality_steps),
            "cascade_failure_count": self.cascade_failure_count,
            "quality_ratio": round(self.quality_ratio, 3),
            "step_scores": [s.to_dict() for s in self.scores],
        }
    
    def to_markdown(self) -> str:
        """生成 Markdown 格式的诊断报告。"""
        lines = [
            f"# Trajectory Diagnosis: {self.trace_id}",
            "",
            f"**User Request**: {self.user_request[:100]}...",
            "",
            f"**Total Steps**: {self.total_steps}",
            f"**High Quality Steps**: {len(self.high_quality_steps)}",
            f"**Low Quality Steps**: {len(self.low_quality_steps)}",
            f"**Quality Ratio**: {self.quality_ratio:.1%}",
            "",
        ]
        
        if self.failure_onset:
            lines.extend([
                "## Failure Onset",
                "",
                f"- **Step**: {self.failure_onset.step_index}",
                f"- **Tool**: {self.failure_onset.tool_name}",
                f"- **Dimension**: {self.failure_onset.dimension}",
                f"- **Reason**: {self.failure_onset.reason}",
                "",
            ])
        else:
            lines.extend([
                "## No Failure Onset Detected",
                "",
                "All steps passed quality checks.",
                "",
            ])
        
        lines.append("## Step-by-Step Scores")
        lines.append("")
        lines.append("| Step | Tool | Status | Reason |")
        lines.append("|------|------|--------|--------|")
        
        for score in self.scores:
            status_emoji = {
                ScoreStatus.PASS: "✅",
                ScoreStatus.WARN: "⚠️",
                ScoreStatus.FAIL: "❌",
                ScoreStatus.SKIP: "⏭️",
            }
            status = status_emoji.get(score.overall_status, "?")
            
            reasons = []
            if score.tool_selection.status == ScoreStatus.FAIL:
                reasons.append(f"tool_selection: {score.tool_selection.reason}")
            if score.parameter_validity.status == ScoreStatus.FAIL:
                reasons.append(f"param: {score.parameter_validity.reason}")
            if score.result_interpretation.status == ScoreStatus.FAIL:
                reasons.append(f"interpret: {score.result_interpretation.reason}")
            if score.progress.status == ScoreStatus.FAIL:
                reasons.append(f"progress: {score.progress.reason}")
            
            reason_str = "; ".join(reasons) if reasons else "-"
            lines.append(f"| {score.step_index} | {score.tool_name} | {status} | {reason_str} |")
        
        if self.high_quality_steps:
            lines.extend([
                "",
                "## High Quality Trajectory (for SFT/RL training)",
                "",
            ])
            for step in self.high_quality_steps[:5]:
                lines.append(f"- Step {step.step_index}: {step.tool_name}")
            if len(self.high_quality_steps) > 5:
                lines.append(f"- ... and {len(self.high_quality_steps) - 5} more")
        
        return "\n".join(lines)


class TrajectoryDiagnoser:
    """轨迹诊断器。"""
    
    def __init__(self, use_llm_judge: bool = False):
        self.scorer = StepScorer(use_llm_judge=use_llm_judge)
    
    def diagnose(
        self,
        bundle: dict[str, Any],
        trace_id: str,
        user_request: str = "",
    ) -> DiagnosisResult:
        """对轨迹进行诊断。"""
        steps = extract_steps(bundle)
        
        if not user_request:
            user_request = bundle.get("input", "")
            if isinstance(user_request, dict):
                user_request = user_request.get("input", "")
        
        scores = self.scorer.score_trajectory(steps, user_request)
        
        failure_onset = self._find_failure_onset(scores)
        
        if failure_onset is not None:
            high_quality = steps[:failure_onset.step_index]
            low_quality = steps[failure_onset.step_index:]
            cascade_count = len(low_quality)
        else:
            high_quality = steps
            low_quality = []
            cascade_count = 0
        
        return DiagnosisResult(
            trace_id=trace_id,
            user_request=user_request,
            total_steps=len(steps),
            scores=scores,
            failure_onset=failure_onset,
            high_quality_steps=high_quality,
            low_quality_steps=low_quality,
            cascade_failure_count=cascade_count,
        )
    
    def _find_failure_onset(self, scores: list[StepScore]) -> FailureOnset | None:
        """定位首次失败的步骤。
        
        遍历所有步骤的打分，找到第一个 FAIL 的步骤。
        """
        for score in scores:
            if score.tool_selection.status == ScoreStatus.FAIL:
                return FailureOnset(
                    step_index=score.step_index,
                    tool_name=score.tool_name,
                    reason=score.tool_selection.reason,
                    dimension="tool_selection",
                )
            
            if score.parameter_validity.status == ScoreStatus.FAIL:
                return FailureOnset(
                    step_index=score.step_index,
                    tool_name=score.tool_name,
                    reason=score.parameter_validity.reason,
                    dimension="parameter_validity",
                )
            
            if score.result_interpretation.status == ScoreStatus.FAIL:
                return FailureOnset(
                    step_index=score.step_index,
                    tool_name=score.tool_name,
                    reason=score.result_interpretation.reason,
                    dimension="result_interpretation",
                )
            
            if score.progress.status == ScoreStatus.FAIL:
                return FailureOnset(
                    step_index=score.step_index,
                    tool_name=score.tool_name,
                    reason=score.progress.reason,
                    dimension="progress",
                )
        
        return None
    
    def diagnose_multiple(
        self,
        bundles: list[tuple[dict[str, Any], str]],
    ) -> list[DiagnosisResult]:
        """批量诊断多个轨迹。
        
        Args:
            bundles: [(bundle, trace_id), ...]
        """
        results = []
        for bundle, trace_id in bundles:
            result = self.diagnose(bundle, trace_id)
            results.append(result)
        return results


def compute_trajectory_metrics(results: list[DiagnosisResult]) -> dict[str, Any]:
    """计算轨迹诊断的汇总指标。"""
    if not results:
        return {}
    
    total_traces = len(results)
    traces_with_onset = sum(1 for r in results if r.failure_onset is not None)
    
    total_steps = sum(r.total_steps for r in results)
    total_high_quality = sum(len(r.high_quality_steps) for r in results)
    total_cascade = sum(r.cascade_failure_count for r in results)
    
    avg_quality_ratio = sum(r.quality_ratio for r in results) / total_traces
    
    onset_dimensions: dict[str, int] = {}
    for r in results:
        if r.failure_onset:
            dim = r.failure_onset.dimension
            onset_dimensions[dim] = onset_dimensions.get(dim, 0) + 1
    
    return {
        "total_traces": total_traces,
        "traces_with_failure_onset": traces_with_onset,
        "onset_rate": round(traces_with_onset / total_traces, 3) if total_traces else 0,
        "total_steps": total_steps,
        "total_high_quality_steps": total_high_quality,
        "total_cascade_failures": total_cascade,
        "avg_quality_ratio": round(avg_quality_ratio, 3),
        "onset_dimensions": onset_dimensions,
    }
