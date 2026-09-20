"""Trajectory filter — 轨迹过滤器。

基于诊断结果过滤轨迹，保留高质量片段用于 SFT/RL 训练。

过滤策略：
- 保留 Failure Onset 之前的所有步骤
- 丢弃 Onset 之后的级联失败
- 可选：保留尝试恢复的步骤（如果 Agent 成功从错误中恢复）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from eval.langfuse.step_scorer import StepRecord, ScoreStatus, StepScore
from eval.langfuse.trajectory_diagnoser import DiagnosisResult


@dataclass
class FilteredTrajectory:
    """过滤后的轨迹。"""
    trace_id: str
    user_request: str
    high_quality_steps: list[StepRecord]
    recovery_steps: list[StepRecord]
    discarded_steps: list[StepRecord]
    
    @property
    def total_steps(self) -> int:
        return len(self.high_quality_steps) + len(self.recovery_steps) + len(self.discarded_steps)
    
    @property
    def quality_ratio(self) -> float:
        if self.total_steps == 0:
            return 0.0
        return len(self.high_quality_steps) / self.total_steps
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "user_request": self.user_request,
            "total_steps": self.total_steps,
            "high_quality_count": len(self.high_quality_steps),
            "recovery_count": len(self.recovery_steps),
            "discarded_count": len(self.discarded_steps),
            "quality_ratio": round(self.quality_ratio, 3),
            "high_quality_steps": [
                {
                    "step_index": s.step_index,
                    "tool_name": s.tool_name,
                    "tool_input": s.tool_input,
                    "tool_output": s.tool_output[:500] if s.tool_output else "",
                }
                for s in self.high_quality_steps
            ],
        }
    
    def to_training_format(self) -> dict[str, Any]:
        """转换为训练格式（用于 SFT/RL）。"""
        conversations = []
        
        conversations.append({
            "role": "user",
            "content": self.user_request,
        })
        
        for step in self.high_quality_steps:
            tool_call = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"call_{step.step_index}",
                    "type": "function",
                    "function": {
                        "name": step.tool_name,
                        "arguments": json.dumps(step.tool_input, ensure_ascii=False)
                            if isinstance(step.tool_input, dict) else str(step.tool_input),
                    },
                }],
            }
            conversations.append(tool_call)
            
            tool_result = {
                "role": "tool",
                "tool_call_id": f"call_{step.step_index}",
                "content": step.tool_output[:2000] if step.tool_output else "",
            }
            conversations.append(tool_result)
        
        return {
            "trace_id": self.trace_id,
            "conversations": conversations,
            "metadata": {
                "quality_ratio": self.quality_ratio,
                "step_count": len(self.high_quality_steps),
            },
        }


class TrajectoryFilter:
    """轨迹过滤器。"""
    
    def __init__(self, detect_recovery: bool = True):
        """
        Args:
            detect_recovery: 是否检测恢复行为（Agent 从错误中恢复）
        """
        self.detect_recovery = detect_recovery
    
    def filter(
        self,
        diagnosis: DiagnosisResult,
    ) -> FilteredTrajectory:
        """过滤轨迹，保留高质量片段。"""
        if diagnosis.failure_onset is None:
            return FilteredTrajectory(
                trace_id=diagnosis.trace_id,
                user_request=diagnosis.user_request,
                high_quality_steps=diagnosis.high_quality_steps,
                recovery_steps=[],
                discarded_steps=[],
            )
        
        onset_index = diagnosis.failure_onset.step_index
        all_steps = diagnosis.high_quality_steps + diagnosis.low_quality_steps
        
        high_quality = all_steps[:onset_index]
        low_quality = all_steps[onset_index:]
        
        if self.detect_recovery:
            recovery, discarded = self._detect_recovery(low_quality, diagnosis.scores)
        else:
            recovery = []
            discarded = low_quality
        
        return FilteredTrajectory(
            trace_id=diagnosis.trace_id,
            user_request=diagnosis.user_request,
            high_quality_steps=high_quality,
            recovery_steps=recovery,
            discarded_steps=discarded,
        )
    
    def _detect_recovery(
        self,
        low_quality_steps: list[StepRecord],
        scores: list[StepScore],
    ) -> tuple[list[StepRecord], list[StepRecord]]:
        """检测恢复行为。
        
        如果 Agent 在失败后尝试了不同的方法并成功，
        则这些步骤被视为"恢复步骤"而非"丢弃步骤"。
        """
        if not low_quality_steps:
            return [], []
        
        onset_score = None
        for score in scores:
            if score.step_index == low_quality_steps[0].step_index:
                onset_score = score
                break
        
        if onset_score is None:
            return [], low_quality_steps
        
        recovery = []
        discarded = []
        
        failed_tool = onset_score.tool_name
        tried_alternative = False
        
        for step in low_quality_steps:
            if step.tool_name != failed_tool:
                tried_alternative = True
            
            if tried_alternative and step.tool_level != "ERROR":
                recovery.append(step)
            else:
                discarded.append(step)
        
        return recovery, discarded
    
    def filter_multiple(
        self,
        diagnoses: list[DiagnosisResult],
    ) -> list[FilteredTrajectory]:
        """批量过滤多个轨迹。"""
        return [self.filter(d) for d in diagnoses]


def compute_filter_metrics(
    filtered: list[FilteredTrajectory],
) -> dict[str, Any]:
    """计算过滤指标。"""
    if not filtered:
        return {}
    
    total_traces = len(filtered)
    traces_with_recovery = sum(1 for f in filtered if f.recovery_steps)
    
    total_steps = sum(f.total_steps for f in filtered)
    total_hq = sum(len(f.high_quality_steps) for f in filtered)
    total_recovery = sum(len(f.recovery_steps) for f in filtered)
    total_discarded = sum(len(f.discarded_steps) for f in filtered)
    
    avg_quality_ratio = sum(f.quality_ratio for f in filtered) / total_traces
    
    return {
        "total_traces": total_traces,
        "traces_with_recovery": traces_with_recovery,
        "recovery_rate": round(traces_with_recovery / total_traces, 3) if total_traces else 0,
        "total_steps": total_steps,
        "total_high_quality": total_hq,
        "total_recovery": total_recovery,
        "total_discarded": total_discarded,
        "avg_quality_ratio": round(avg_quality_ratio, 3),
        "training_ready_steps": total_hq + total_recovery,
    }


def export_for_training(
    filtered: list[FilteredTrajectory],
    output_path: str,
    min_quality_ratio: float = 0.5,
) -> int:
    """导出高质量轨迹用于训练。
    
    Args:
        filtered: 过滤后的轨迹列表
        output_path: 输出文件路径（JSONL 格式）
        min_quality_ratio: 最小质量比例阈值
    
    Returns:
        导出的轨迹数量
    """
    exported = 0
    
    with open(output_path, "w", encoding="utf-8") as f:
        for traj in filtered:
            if traj.quality_ratio < min_quality_ratio:
                continue
            if not traj.high_quality_steps:
                continue
            
            training_data = traj.to_training_format()
            f.write(json.dumps(training_data, ensure_ascii=False) + "\n")
            exported += 1
    
    return exported
