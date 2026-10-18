"""统一运行记录：所有 benchmark 写同一份信封，特有的东西全进 extra。

为什么要有这一层：五个 benchmark 原本各写各的行字典（gaia 有 correct 没 reward、
loca 有 agent_error/eval_error、harbor 是 task_name/n_input_tokens/agent_execution_s），
前端得为每一种写一套渲染，对比层也没法跨 benchmark 复用。

**这个模块必须是叶子**：`runner_base.write_reports` 要 import 它，而 `jobs.py` 又
import `runner_base`——这里只要有一个反向依赖就成了导入环。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable

ENVELOPE_KEYS = ("benchmark", "run_id", "generated_at", "meta", "summary", "results")
META_CORE = ("model", "seed", "git_revision", "task_ids", "variables")
SUMMARY_CORE = ("n_tasks", "n_scored", "n_unscored", "n_passed", "pass_rate",
                "mean_duration_s", "totals")
ROW_CORE = ("task_id", "name", "status", "passed", "reward", "duration_s", "tokens",
            "error", "session_id", "trace_id", "extra")
TOKEN_FIELDS = ("input", "output", "reasoning", "cached")
ROW_STATUSES = ("pending", "running", "passed", "failed", "error", "aborted", "unscored")

# aider-polyglot 的判分：测试全过才算过。harbor/compare.py 原来是它自己的常量，
# 归一化也要用同一把尺子，所以搬到这里（Task 12 会把 harbor 那份删掉）
PASSED_THRESHOLD = 1.0

_Adapter = Callable[[Mapping[str, Any]], tuple[dict[str, Any], frozenset[str]]]


def _as_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _as_int(value: Any) -> int | None:
    # bool 是 int 的子类，True 会变成 1——token 数不可能是布尔
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _tokens(source: Mapping[str, Any] | None = None, **named: Any) -> dict[str, int | None]:
    merged = dict(source or {})
    merged.update({k: v for k, v in named.items() if v is not None})
    return {f: _as_int(merged.get(f)) for f in TOKEN_FIELDS}


def _status(raw_status: Any, passed: bool | None, error: str | None) -> str:
    if raw_status in ROW_STATUSES:
        return raw_status
    # 顺序是刻意的：判过分的先落 passed/failed。harbor 的 cpp 构建失败同时带着
    # reward=0.0 和 exception_type，它是**做错了**；error 先判就会把失败洗成
    # "没跑起来"，那一题于是被踢出分母，做得差的一臂通过率反而虚高
    if passed is True:
        return "passed"
    if passed is False:
        return "failed"
    if error:
        return "error"
    return "unscored"


def _row_from_task_result(raw: Mapping[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    """gaia / hle / smoke：`EvalTaskResult.to_dict()` 的形状。"""
    passed = raw.get("passed")
    if passed is None:
        passed = raw.get("correct")  # 判分口径唯一：correct 映射到 passed
    return {
        "task_id": raw.get("task_id"),
        "name": raw.get("name"),
        "passed": _as_bool(passed),
        "reward": None,
        "duration_s": raw.get("duration_s"),
        "tokens": _tokens(raw.get("tokens")),
        "error": raw.get("error"),
        "session_id": raw.get("session_id"),
        "trace_id": raw.get("trace_id"),
    }, frozenset({"correct", "benchmark"})


def _row_from_loca_row(raw: Mapping[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    return {
        "task_id": raw.get("task_id"),
        "name": raw.get("name"),
        "passed": _as_bool(raw.get("passed")),
        "reward": raw.get("reward"),
        "duration_s": raw.get("duration_s"),
        "tokens": _tokens(raw.get("tokens")),
        "error": raw.get("agent_error") or raw.get("eval_error"),
        "session_id": raw.get("session_id"),
        "trace_id": raw.get("trace_id"),
    }, frozenset()


def _row_from_harbor_trial(raw: Mapping[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    reward = raw.get("reward")
    metadata = raw.get("agent_metadata") or {}
    exception = raw.get("exception_type")
    message = raw.get("exception_message") or ""
    return {
        "task_id": raw.get("task_name"),
        "name": raw.get("task_name"),
        "passed": None if reward is None else bool(reward >= PASSED_THRESHOLD),
        "reward": reward,
        "duration_s": raw.get("agent_execution_s"),
        "tokens": _tokens(
            None,
            input=raw.get("n_input_tokens"),
            output=raw.get("n_output_tokens"),
            cached=raw.get("n_cache_tokens"),
            # 思考量只有 MyCode 臂有（opencode 的 metadata 是 null），缺就是 None
            reasoning=metadata.get("mycode_reasoning_tokens"),
        ),
        "error": f"{exception}: {message}".rstrip(": ") if exception else None,
        "session_id": metadata.get("mycode_session_id"),
        "trace_id": metadata.get("mycode_trace_id"),
    }, frozenset({"task_name", "reward", "n_input_tokens", "n_output_tokens",
                  "n_cache_tokens", "agent_execution_s"})


_ROW_ADAPTERS: dict[str, _Adapter] = {
    "harbor": _row_from_harbor_trial,
    "loca": _row_from_loca_row,
}


def normalize_row(raw: Mapping[str, Any], *, benchmark: str) -> dict[str, Any]:
    core_keys = set(ROW_CORE)
    if core_keys <= set(raw):
        # 已经是公共核心的形状（normalize_row 自己的输出，或 harbor/loca 的
        # unified_* 记录）：原样带过去。再走一遍适配器只会读到缺席的原名——
        # task_name / agent_error / n_input_tokens 第一轮就被收进 extra 了，
        # 于是 task_id、error、tokens 全被清空，"幂等"就成了"第二次归一化洗掉数据"。
        prior = raw.get("extra")
        extra = dict(prior) if isinstance(prior, Mapping) else {}
        extra.update({k: v for k, v in raw.items() if k not in core_keys})
        normalized: dict[str, Any] = {k: raw[k] for k in ROW_CORE}
        normalized["extra"] = extra
        return normalized

    core, consumed = _ROW_ADAPTERS.get(benchmark, _row_from_task_result)(raw)
    duration = core.get("duration_s")
    row: dict[str, Any] = {
        "task_id": str(core.get("task_id") or ""),
        "name": core.get("name") or "",
        "status": _status(raw.get("status"), core.get("passed"), core.get("error")),
        "passed": core.get("passed"),
        "reward": core.get("reward"),
        "duration_s": float(duration) if isinstance(duration, (int, float)) else 0.0,
        "tokens": core.get("tokens") or _tokens(),
        "error": core.get("error"),
        "session_id": core.get("session_id"),
        "trace_id": core.get("trace_id"),
        "extra": {},
    }
    skip = set(ROW_CORE) | consumed
    row["extra"] = {k: v for k, v in raw.items() if k not in skip}
    return row


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    scored = [r for r in rows if r.get("passed") is not None]
    passed = [r for r in scored if r.get("passed")]
    durations = [r["duration_s"] for r in rows
                 if isinstance(r.get("duration_s"), (int, float)) and r["duration_s"] > 0]
    return {
        "n_tasks": len(rows),
        "n_scored": len(scored),
        "n_unscored": len(rows) - len(scored),
        "n_passed": len(passed),
        "pass_rate": round(len(passed) / len(scored), 4) if scored else None,
        "mean_duration_s": round(sum(durations) / len(durations), 1) if durations else None,
        "totals": {
            f"{field}_tokens": sum(
                (r.get("tokens") or {}).get(field) or 0 for r in rows
            )
            for field in TOKEN_FIELDS
        },
    }


def normalize_report(report: Mapping[str, Any], *, benchmark: str | None = None) -> dict[str, Any]:
    """补成标准信封。幂等：对已经归一化过的报告再跑一遍结果不变。"""
    name = benchmark or report.get("benchmark") or "unknown"
    rows = [normalize_row(r, benchmark=name) for r in report.get("results") or []]

    meta = dict(report.get("meta") or {})
    meta.setdefault("model", None)
    meta.setdefault("seed", None)
    meta.setdefault("git_revision", "unknown")
    meta.setdefault("variables", {})
    meta.setdefault("task_ids", [r["task_id"] for r in rows])

    # 公共核心**覆盖**同名的旧键（它们才是从归一化后的行算出来的），
    # 但旧键整体保留：前端和既有报告读的是 total/correct/pass_at_1，
    # 改名会连带改前端，那是下一份计划的事
    summary = dict(report.get("summary") or {})
    summary.update(summarize_rows(rows))

    return {
        "benchmark": name,
        "run_id": report.get("run_id"),
        "generated_at": report.get("generated_at"),
        "meta": meta,
        "summary": summary,
        "results": rows,
    }


def check_report(report: Mapping[str, Any]) -> list[str]:
    """契约校验。返回空列表 = 合格；否则每条是一个具体缺什么。"""
    problems: list[str] = []
    for key in ENVELOPE_KEYS:
        if key not in report:
            problems.append(f"缺顶层字段 {key}")

    meta = report.get("meta")
    if isinstance(meta, Mapping):
        problems += [f"meta 缺 {key}" for key in META_CORE if key not in meta]
    for key in SUMMARY_CORE:
        summary = report.get("summary")
        if isinstance(summary, Mapping) and key not in summary:
            problems.append(f"summary 缺 {key}")

    results = report.get("results")
    if isinstance(results, Iterable):
        for index, row in enumerate(results):
            if not isinstance(row, Mapping):
                problems.append(f"results[{index}] 不是对象")
                continue
            missing = [k for k in ROW_CORE if k not in row]
            if missing:
                # 每行只报第一条：25 题 × 11 个字段的报告能刷出 275 行噪音
                problems.append(f"results[{index}] 缺 {missing[0]}")
            elif set(row.get("tokens") or {}) != set(TOKEN_FIELDS):
                problems.append(f"results[{index}] tokens 的键不是 {list(TOKEN_FIELDS)}")
            elif row.get("status") not in ROW_STATUSES:
                problems.append(f"results[{index}] status={row.get('status')!r} 不在词表里")
    return problems


def looks_like_run_report(data: Any) -> bool:
    """便宜的判别：这份 JSON 是不是一次运行的记录。

    `eval/reports/` 里还躺着 `harbor_compare_*.json`（对比输出）和别的产物，
    形状都像报告；`list_jobs` 要靠这个把它们挡在外面。
    """
    return (
        isinstance(data, Mapping)
        and {"benchmark", "run_id", "results"} <= set(data)
    )
