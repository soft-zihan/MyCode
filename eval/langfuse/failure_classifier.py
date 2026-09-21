"""Failure classifier — 失败分类器。

对失败的 Agent 轨迹进行分类，识别失败模式，用于改进 Agent 策略。

失败模式（与 13-trace-eval-implementation-plan.md Phase 2 对应）：
- TOOL_LOOP: 工具死循环（同一工具重复调用）
- WRONG_TOOL: 工具选择错误
- PARAM_ERROR: 参数错误
- CONTEXT_LOSS: 上下文丢失（忘记之前的信息）
- HALLUCINATION: 幻觉（编造不存在的文件/内容）
- PREMATURE_STOP: 过早停止（任务未完成就结束）
- INCOMPLETE_ACTION: 不完整行动（工具调用后没有后续处理）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from eval.langfuse.step_scorer import StepRecord, extract_steps


class FailureMode(Enum):
    """失败模式。"""
    TOOL_LOOP = "tool_loop"
    SUBAGENT_LOOP = "subagent_loop"
    WRONG_TOOL = "wrong_tool"
    PARAM_ERROR = "param_error"
    CONTEXT_LOSS = "context_loss"
    HALLUCINATION = "hallucination"
    PREMATURE_STOP = "premature_stop"
    INCOMPLETE_ACTION = "incomplete_action"
    UNKNOWN = "unknown"


@dataclass
class FailureClassification:
    """失败分类结果。"""
    mode: FailureMode
    confidence: float
    evidence: list[str]
    suggestion: str
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
        }


@dataclass
class TraceClassification:
    """轨迹分类结果。"""
    trace_id: str
    is_failure: bool
    classifications: list[FailureClassification]
    primary_mode: FailureMode
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "is_failure": self.is_failure,
            "primary_mode": self.primary_mode.value,
            "classifications": [c.to_dict() for c in self.classifications],
        }
    
    def to_markdown(self) -> str:
        """生成 Markdown 格式的分类报告。"""
        lines = [
            f"# Failure Classification: {self.trace_id}",
            "",
        ]
        
        if not self.is_failure:
            lines.append("**Status**: ✅ Success (no failure detected)")
            return "\n".join(lines)
        
        lines.extend([
            "**Status**: ❌ Failure",
            f"**Primary Mode**: `{self.primary_mode.value}`",
            "",
        ])
        
        for i, cls in enumerate(self.classifications, 1):
            lines.extend([
                f"## Classification {i}: {cls.mode.value}",
                "",
                f"- **Confidence**: {cls.confidence:.0%}",
                f"- **Suggestion**: {cls.suggestion}",
                "",
                "**Evidence**:",
            ])
            for ev in cls.evidence:
                lines.append(f"  - {ev}")
            lines.append("")
        
        return "\n".join(lines)


class FailureClassifier:
    """失败分类器。"""
    
    def __init__(self):
        self.checkers = [
            self._check_subagent_loop,
            self._check_tool_loop,
            self._check_wrong_tool,
            self._check_param_error,
            self._check_context_loss,
            self._check_hallucination,
            self._check_premature_stop,
            self._check_incomplete_action,
        ]
    
    def classify(
        self,
        bundle: dict[str, Any],
        trace_id: str,
    ) -> TraceClassification:
        """对轨迹进行分类。"""
        steps = extract_steps(bundle)
        agent_obs = [o for o in bundle.get("observations", []) if o.get("type") == "AGENT"]
        
        if not steps and not agent_obs:
            return TraceClassification(
                trace_id=trace_id,
                is_failure=False,
                classifications=[],
                primary_mode=FailureMode.UNKNOWN,
            )
        
        has_error = any(s.tool_level == "ERROR" for s in steps)
        has_agent_timeout = False
        for obs in agent_obs:
            meta = obs.get("metadata") if isinstance(obs.get("metadata"), dict) else {}
            status = str(meta.get("status") or "").lower()
            outcome = str(meta.get("outcome") or "").lower()
            output = str(obs.get("output") or "").lower()
            if outcome == "timeout" or status == "timeout" or "timed out" in output:
                has_agent_timeout = True
                break
        
        if not has_error and not has_agent_timeout and len(steps) < 50:
            return TraceClassification(
                trace_id=trace_id,
                is_failure=False,
                classifications=[],
                primary_mode=FailureMode.UNKNOWN,
            )
        
        classifications = []
        for checker in self.checkers:
            result = checker(steps, bundle)
            if result is not None:
                classifications.append(result)
        
        if classifications:
            primary = max(classifications, key=lambda c: c.confidence)
            primary_mode = primary.mode
        else:
            primary_mode = FailureMode.UNKNOWN
        
        return TraceClassification(
            trace_id=trace_id,
            is_failure=len(classifications) > 0,
            classifications=classifications,
            primary_mode=primary_mode,
        )
    
    def _check_subagent_loop(
        self,
        steps: list[StepRecord],
        bundle: dict[str, Any],
    ) -> FailureClassification | None:
        """检查子 Agent 超时/重复派发。"""
        agents = [o for o in bundle.get("observations", []) if o.get("type") == "AGENT"]
        if not agents:
            return None

        timeouts: list[dict[str, Any]] = []
        for obs in agents:
            meta = obs.get("metadata") if isinstance(obs.get("metadata"), dict) else {}
            status = str(meta.get("status") or "").lower()
            outcome = str(meta.get("outcome") or "").lower()
            output = str(obs.get("output") or "").lower()
            if outcome == "timeout" or status == "timeout" or "timed out" in output:
                timeouts.append(obs)

        if len(timeouts) < 2:
            return None

        seen: dict[tuple, int] = {}
        progressing = 0
        evidence: list[str] = []
        for obs in timeouts:
            meta = obs.get("metadata") if isinstance(obs.get("metadata"), dict) else {}
            key_input = obs.get("input")
            if not isinstance(key_input, str):
                key_input = json.dumps(key_input, ensure_ascii=False, sort_keys=True, default=str)
            key = (obs.get("name"), key_input[:200])
            seen[key] = seen.get(key, 0) + 1
            tool_calls = int(meta.get("tool_call_count") or 0)
            failed_calls = int(meta.get("failed_tool_call_count") or 0)
            if tool_calls >= 5 and failed_calls / max(tool_calls, 1) <= 0.5:
                progressing += 1
                evidence.append(
                    f"子 Agent `{obs.get('name')}` 超时但有进展：tool_calls={tool_calls}, failed={failed_calls}, child_turns={meta.get('child_turn_count')}"
                )
            else:
                evidence.append(
                    f"子 Agent `{obs.get('name')}` 超时且无明显进展：tool_calls={tool_calls}, failed={failed_calls}, child_turns={meta.get('child_turn_count')}"
                )

        worst = max(seen.items(), key=lambda kv: kv[1])
        repeated_count = worst[1]
        if repeated_count >= 2:
            evidence.insert(0, f"相同子 Agent 输入重复超时 {repeated_count} 次")
        evidence.insert(0, f"子 Agent 超时 {len(timeouts)}/{len(agents)} 次，其中有进展 {progressing} 次")

        looping = repeated_count >= 2 or progressing < len(timeouts)
        confidence = min(0.5 + 0.15 * len(timeouts) + (0.2 if repeated_count >= 2 else 0.0), 1.0)
        if not looping:
            confidence = min(confidence, 0.4)

        return FailureClassification(
            mode=FailureMode.SUBAGENT_LOOP,
            confidence=confidence,
            evidence=evidence[:8],
            suggestion=(
                "区分复杂子任务和循环失败：相同 prompt 重复超时时应阻止重试；"
                "不同 prompt 但持续超时应缩小任务、改用直接工具或提高/取消子 Agent 预算。"
            ),
        )

    def _check_tool_loop(
        self,
        steps: list[StepRecord],
        bundle: dict[str, Any],
    ) -> FailureClassification | None:
        """检查工具死循环。"""
        seen: dict[tuple, int] = {}
        failed_seen: dict[tuple, int] = {}
        for step in steps:
            input_str = json.dumps(step.tool_input, sort_keys=True, default=str)[:200]
            key = (step.tool_name, input_str)
            seen[key] = seen.get(key, 0) + 1
            if step.tool_level == "ERROR":
                failed_seen[key] = failed_seen.get(key, 0) + 1
        
        loops = {k: v for k, v in seen.items() if v >= 3}
        if not loops:
            return None
        
        worst = max(loops.items(), key=lambda kv: (failed_seen.get(kv[0], 0), kv[1]))
        tool_name, count = worst[0][0], worst[1]
        failed_count = failed_seen.get(worst[0], 0)
        confidence = min((count + failed_count) / 6, 1.0)
        evidence = [f"工具 `{tool_name}` 被调用 {count} 次（相同输入）"]
        if failed_count:
            evidence.append(f"其中 {failed_count} 次为 ERROR，重复失败比单纯重复更像死循环")
        else:
            evidence.append("没有 ERROR 级别，可能是合法重复读取，也可能是低效轨迹")
        
        return FailureClassification(
            mode=FailureMode.TOOL_LOOP,
            confidence=confidence,
            evidence=evidence,
            suggestion="增加重复检测，相同调用失败 2-3 次后阻止重试并要求更换策略",
        )
    
    def _check_wrong_tool(
        self,
        steps: list[StepRecord],
        bundle: dict[str, Any],
    ) -> FailureClassification | None:
        """检查工具选择错误。"""
        error_steps = [s for s in steps if s.tool_level == "ERROR"]
        if not error_steps:
            return None
        
        tool_errors: dict[str, int] = {}
        for step in error_steps:
            tool_errors[step.tool_name] = tool_errors.get(step.tool_name, 0) + 1
        
        if not tool_errors:
            return None
        
        worst_tool = max(tool_errors.items(), key=lambda kv: kv[1])
        if worst_tool[1] < 2:
            return None
        
        return FailureClassification(
            mode=FailureMode.WRONG_TOOL,
            confidence=min(worst_tool[1] / 3, 1.0),
            evidence=[
                f"工具 `{worst_tool[0]}` 失败 {worst_tool[1]} 次",
                "Agent 可能选择了不适合当前任务的工具",
            ],
            suggestion="优化工具描述，帮助 Agent 更好地理解工具适用场景",
        )
    
    def _check_param_error(
        self,
        steps: list[StepRecord],
        bundle: dict[str, Any],
    ) -> FailureClassification | None:
        """检查参数错误。"""
        param_errors = []
        for step in steps:
            if step.tool_level != "ERROR":
                continue
            error_msg = (step.error or "").lower()
            if any(kw in error_msg for kw in [
                "not found", "no such file", "invalid", "missing",
                "permission denied", "does not exist",
            ]):
                param_errors.append(step)
        
        if not param_errors:
            return None
        
        return FailureClassification(
            mode=FailureMode.PARAM_ERROR,
            confidence=min(len(param_errors) / 3, 1.0),
            evidence=[
                f"{len(param_errors)} 个工具因参数错误失败",
                f"示例: `{param_errors[0].tool_name}` - {param_errors[0].error[:100] if param_errors[0].error else 'unknown'}",
            ],
            suggestion="在工具调用前验证参数（如检查文件是否存在）",
        )
    
    def _check_context_loss(
        self,
        steps: list[StepRecord],
        bundle: dict[str, Any],
    ) -> FailureClassification | None:
        """检查上下文丢失。"""
        read_files: dict[str, int] = {}
        for step in steps:
            if step.tool_name == "read_file":
                file_path = ""
                if isinstance(step.tool_input, dict):
                    file_path = step.tool_input.get("file_path", "")
                if file_path:
                    read_files[file_path] = read_files.get(file_path, 0) + 1
        
        repeated_reads = {k: v for k, v in read_files.items() if v >= 3}
        if not repeated_reads:
            return None
        
        worst = max(repeated_reads.items(), key=lambda kv: kv[1])
        
        return FailureClassification(
            mode=FailureMode.CONTEXT_LOSS,
            confidence=min(worst[1] / 5, 1.0),
            evidence=[
                f"文件 `{worst[0]}` 被重复读取 {worst[1]} 次",
                "Agent 可能忘记了之前读取的内容",
            ],
            suggestion="增强上下文管理，避免重复读取相同内容",
        )
    
    def _check_hallucination(
        self,
        steps: list[StepRecord],
        bundle: dict[str, Any],
    ) -> FailureClassification | None:
        """检查幻觉（编造不存在的文件/内容）。"""
        hallucinations = []
        for step in steps:
            if step.tool_level != "ERROR":
                continue
            error_msg = (step.error or "").lower()
            if any(kw in error_msg for kw in [
                "not found", "no such file", "does not exist",
                "file not found", "directory not found",
            ]):
                if step.tool_name in ("read_file", "edit", "write_file"):
                    hallucinations.append(step)
        
        if not hallucinations:
            return None
        
        return FailureClassification(
            mode=FailureMode.HALLUCINATION,
            confidence=min(len(hallucinations) / 2, 1.0),
            evidence=[
                f"{len(hallucinations)} 次尝试操作不存在的文件",
                "Agent 可能编造了文件路径",
            ],
            suggestion="在操作文件前先验证文件存在（使用 glob 或 read_file）",
        )
    
    def _check_premature_stop(
        self,
        steps: list[StepRecord],
        bundle: dict[str, Any],
    ) -> FailureClassification | None:
        """检查过早停止。"""
        output = bundle.get("output", "")
        if isinstance(output, dict):
            output = output.get("output", "")
        
        if not output:
            return None
        
        premature_indicators = [
            "i cannot", "i can't", "unable to", "sorry, i",
            "let me know if you", "please let me know",
        ]
        output_lower = output.lower()
        
        for indicator in premature_indicators:
            if indicator in output_lower:
                return FailureClassification(
                    mode=FailureMode.PREMATURE_STOP,
                    confidence=0.7,
                    evidence=[
                        f"输出包含 `{indicator}`",
                        "Agent 可能在任务未完成时放弃",
                    ],
                    suggestion="增强 Agent 的坚持性，鼓励尝试替代方案",
                )
        
        return None
    
    def _check_incomplete_action(
        self,
        steps: list[StepRecord],
        bundle: dict[str, Any],
    ) -> FailureClassification | None:
        """检查不完整行动。"""
        edit_steps = [s for s in steps if s.tool_name == "edit"]
        
        no_followup = []
        for i, step in enumerate(edit_steps):
            if i + 1 < len(steps):
                next_step = steps[i + 1]
                if next_step.tool_name not in ("read_file", "bash", "edit"):
                    no_followup.append(step)
        
        if len(no_followup) >= 2:
            return FailureClassification(
                mode=FailureMode.INCOMPLETE_ACTION,
                confidence=min(len(no_followup) / 3, 1.0),
                evidence=[
                    f"{len(no_followup)} 次编辑后没有验证（read_file/bash）",
                    "Agent 可能没有检查编辑结果",
                ],
                suggestion="编辑后自动验证（读取文件或运行测试）",
            )
        
        return None


def compute_failure_distribution(
    classifications: list[TraceClassification],
) -> dict[str, int]:
    """计算失败模式分布。"""
    distribution: dict[str, int] = {}
    for cls in classifications:
        if cls.is_failure:
            mode = cls.primary_mode.value
            distribution[mode] = distribution.get(mode, 0) + 1
    return distribution
