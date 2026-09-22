from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

BenchmarkName = Literal["gaia", "hle", "smoke", "loca"]
ExecutionMode = Literal["backend_session", "in_process"]
RunStatus = Literal["pending", "running", "completed", "aborted", "failed"]
TaskStatus = Literal["pending", "running", "passed", "failed", "error", "aborted"]


@dataclass(slots=True)
class EvalTask:
    task_id: str
    benchmark: BenchmarkName
    prompt: str
    name: str = ""
    expected_output: str = ""
    level: int | None = None
    category: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EvalTaskResult:
    task_id: str
    benchmark: BenchmarkName
    status: TaskStatus = "pending"
    name: str = ""
    expected: str = ""
    predicted: str = ""
    correct: bool | None = None
    passed: bool | None = None
    duration_s: float = 0.0
    tokens: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    session_id: str | None = None
    error: str | None = None
    langfuse_dataset: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "benchmark": self.benchmark,
            "name": self.name,
            "status": self.status,
            "expected": self.expected,
            "predicted": self.predicted,
            "correct": self.correct,
            "passed": self.passed,
            "duration_s": self.duration_s,
            "tokens": self.tokens,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "error": self.error,
            "langfuse_dataset": self.langfuse_dataset,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class EvalRunOptions:
    benchmark: BenchmarkName
    sample: int | None = None
    seed: int = 42
    category: str | None = None
    include_image: bool = False
    only: list[str] | None = None
    suite: str = "chain"
    timeout_s: int = 0
    model: str | None = None
    api_base: str | None = None
    execution_mode: ExecutionMode = "backend_session"
    sync_langfuse_dataset: bool = True
    judge_after_run: bool = False
    skip_langfuse: bool = False
    keep_sessions: bool = True
    base_url: str = "http://localhost:5555"
    ws_url: str = "ws://localhost:5555/ws/events"
    thinking: bool | None = None  # None=跟随全局配置
    thinking_feedback: bool | None = None  # None=跟随端点配置; True=历史思考以 reasoning_content 回传
    compression_arm: str | None = None  # None/full=现状; truncate/tool_only/session_only=压缩消融实验臂
    context_window: int | None = None  # 显式窗口覆盖（压缩消融的压力窗口维度，None=按端点/臂默认）
    level: int | None = 3  # GAIA 难度档（默认 3=对比实验口径；None=全部）

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "sample": self.sample,
            "seed": self.seed,
            "category": self.category,
            "include_image": self.include_image,
            "thinking": self.thinking,
            "thinking_feedback": self.thinking_feedback,
            "compression_arm": self.compression_arm,
            "context_window": self.context_window,
            "level": self.level,
            "only": self.only,
            "suite": self.suite,
            "timeout_s": self.timeout_s,
            "model": self.model,
            "api_base": self.api_base,
            "execution_mode": self.execution_mode,
            "sync_langfuse_dataset": self.sync_langfuse_dataset,
            "judge_after_run": self.judge_after_run,
            "skip_langfuse": self.skip_langfuse,
            "keep_sessions": self.keep_sessions,
            "base_url": self.base_url,
            "ws_url": self.ws_url,
        }


@dataclass(slots=True)
class EvalRunState:
    run_id: str
    benchmark: BenchmarkName
    eval_session_id: str
    options: EvalRunOptions
    status: RunStatus = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    model: str = ""
    api_base: str | None = None
    api_key: str | None = None
    tasks: list[EvalTaskResult] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    report_json_path: str | None = None
    report_md_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "benchmark": self.benchmark,
            "eval_session_id": self.eval_session_id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "model": self.model,
            "options": self.options.to_dict(),
            "tasks": [t.to_dict() for t in self.tasks],
            "summary": self.summary,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "error": self.error,
        }

    def summary_snapshot(self) -> dict[str, Any]:
        completed = [t for t in self.tasks if t.status not in ("pending", "running")]
        correct = sum(1 for t in completed if t.correct is True or t.passed is True)
        return {
            "total": len(self.tasks),
            "completed": len(completed),
            "correct": correct,
            "pass_at_1": round(correct / len(completed), 4) if completed else 0.0,
            "errors": sum(1 for t in completed if t.status == "error"),
            "avg_duration_s": round(sum(t.duration_s for t in completed) / len(completed), 1) if completed else 0.0,
        }
