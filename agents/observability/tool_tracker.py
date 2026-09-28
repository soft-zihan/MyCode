"""工具调用检测模块。

实时检测工具调用重复、循环和失败重试，在工具执行前后注入警告或阻止明显打转的调用。
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

REPEAT_WINDOW_S = 60
REPEAT_WARN_COUNT = 5
OFF_TRACK_COUNT = 7
FORCE_STOP_COUNT = 10
IDENTICAL_FAILURE_BLOCK_COUNT = 3
IDENTICAL_TIMEOUT_BLOCK_COUNT = 2
LONG_RUNNING_TOOLS = {"agent"}
AGENT_TIMEOUT_BLOCK_COUNT = 2
AGENT_TIMEOUT_BLOCK_COUNT_WITH_PROGRESS = 4
PROGRESS_MIN_TOOL_CALLS = 5
PROGRESS_MAX_FAILURE_RATIO = 0.5

NEAR_DUPLICATE_TOOLS = {"web_search"}
NEAR_DUPLICATE_SIMILARITY = 0.60
NEAR_DUPLICATE_WARN_COUNT = 5
NEAR_DUPLICATE_BLOCK_COUNT = 9
NEAR_DUPLICATE_HISTORY_LIMIT = 200

_URL_RE = re.compile(r"https?://\S+")
_SITE_FILTER_RE = re.compile(r"site:\S+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SEARCH_STOPWORDS = frozenset({
    "the", "for", "and", "or", "of", "an", "to", "in", "on", "with",
    "site", "pdf", "current", "effective", "issue", "reprint",
    "standard", "standards", "grade", "grades", "supersede", "superseded",
})

GuardAction = Literal["allow", "warn", "block"]


def canonical_args(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


def make_key(tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
    return (tool_name, canonical_args(args))


def normalize_search_query(query: str) -> frozenset[str]:
    text = _URL_RE.sub(" ", str(query or "").lower())
    text = _SITE_FILTER_RE.sub(" ", text)
    tokens = _TOKEN_RE.findall(text)
    return frozenset(token for token in tokens if len(token) > 1 and token not in _SEARCH_STOPWORDS)


def tool_similarity_signature(tool_name: str, args: dict[str, Any]) -> frozenset[str] | None:
    if tool_name not in NEAR_DUPLICATE_TOOLS:
        return None
    query = args.get("query") if isinstance(args, dict) else None
    if not isinstance(query, str) or not query.strip():
        return None
    return normalize_search_query(query)


def jaccard_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def has_meaningful_progress(metadata: dict[str, Any] | None) -> bool:
    if not metadata:
        return False
    tool_calls = int(metadata.get("tool_call_count") or 0)
    failed_calls = int(metadata.get("failed_tool_call_count") or 0)
    child_turns = int(metadata.get("child_turn_count") or 0)
    if tool_calls >= PROGRESS_MIN_TOOL_CALLS:
        failure_ratio = failed_calls / tool_calls if tool_calls else 1.0
        if failure_ratio <= PROGRESS_MAX_FAILURE_RATIO:
            return True
    return child_turns > 1 and tool_calls > 0 and failed_calls == 0


@dataclass
class ToolGuardDecision:
    action: GuardAction
    reason: str | None = None
    message: str | None = None
    verdict: str = "allow"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallTracker:
    """跟踪工具调用，检测重复调用、循环和失败重试。"""

    call_history: dict[tuple[str, str], list[float]] = field(default_factory=lambda: defaultdict(list))
    tool_sequence: list[str] = field(default_factory=list)
    warned_keys: set[tuple[str, str]] = field(default_factory=set)
    cycle_warned: bool = False
    cycle_count_after_warn: int = 0
    outcome_history: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    consecutive_failures: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    tool_failure_streaks: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocked_keys: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    blocked_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    near_duplicate_history: dict[str, list[frozenset[str]]] = field(default_factory=lambda: defaultdict(list))
    pending_near_duplicate_signatures: dict[str, list[frozenset[str]]] = field(default_factory=lambda: defaultdict(list))
    last_decision: ToolGuardDecision | None = None

    def _near_duplicate_previous_count(self, tool_name: str, signature: frozenset[str]) -> int:
        history = self.near_duplicate_history.get(tool_name, [])[-NEAR_DUPLICATE_HISTORY_LIMIT:]
        pending = self.pending_near_duplicate_signatures.get(tool_name, [])
        return sum(
            1
            for previous in (*history, *pending)
            if jaccard_similarity(previous, signature) >= NEAR_DUPLICATE_SIMILARITY
        )

    def clear_pending_near_duplicates(self) -> None:
        self.pending_near_duplicate_signatures.clear()

    def _discard_pending_near_duplicate(self, tool_name: str, signature: frozenset[str]) -> None:
        pending = self.pending_near_duplicate_signatures.get(tool_name)
        if not pending:
            return
        for index, candidate in enumerate(pending):
            if candidate == signature:
                del pending[index]
                return

    def _near_duplicate_info(
        self,
        tool_name: str,
        args: dict[str, Any],
        signature: frozenset[str],
        similar_calls: int,
    ) -> dict[str, Any]:
        return {
            "scope": "near_duplicate_tool_calls",
            "tool": tool_name,
            "args": args,
            "args_key": canonical_args(args),
            "signature_size": len(signature),
            "count": similar_calls,
            "similar_calls": similar_calls,
            "similarity_threshold": NEAR_DUPLICATE_SIMILARITY,
            "warn_threshold": NEAR_DUPLICATE_WARN_COUNT,
            "block_threshold": NEAR_DUPLICATE_BLOCK_COUNT,
        }

    def _near_duplicate_message(self, info: dict[str, Any], *, blocked: bool) -> str:
        tool = info.get("tool", "tool")
        similar_calls = info.get("similar_calls")
        threshold = info.get("block_threshold") if blocked else info.get("warn_threshold")
        prefix = "Loop guard blocked" if blocked else "Loop guard warning:"
        return (
            f"{prefix} `{tool}`: {similar_calls} near-duplicate calls "
            f"(threshold={threshold}, similarity>={info.get('similarity_threshold')}). "
            "Do not keep rewriting the same search. Switch to an index/list page, batch the remaining items, "
            "use a different tool/source, or finish with the evidence already collected."
        )

    def near_duplicate_precheck(self, tool_name: str, args: dict[str, Any]) -> ToolGuardDecision | None:
        signature = tool_similarity_signature(tool_name, args)
        if not signature:
            return None
        previous_count = self._near_duplicate_previous_count(tool_name, signature)
        similar_calls = previous_count + 1
        if similar_calls < NEAR_DUPLICATE_BLOCK_COUNT:
            self.pending_near_duplicate_signatures[tool_name].append(signature)
            return None
        info = self._near_duplicate_info(tool_name, args, signature, similar_calls)
        return ToolGuardDecision(
            action="block",
            reason="near_duplicate_tool_calls",
            message=self._near_duplicate_message(info, blocked=True),
            verdict="block_near_duplicate_tool_calls",
            metadata=info,
        )

    def record_call(self, tool_name: str, args: dict[str, Any]) -> ToolGuardDecision:
        now = time.time()
        key = make_key(tool_name, args)

        self.call_history[key] = [t for t in self.call_history[key] if now - t < REPEAT_WINDOW_S]
        self.call_history[key].append(now)

        self.tool_sequence.append(tool_name)
        if len(self.tool_sequence) > 10:
            self.tool_sequence = self.tool_sequence[-10:]

        messages: list[str] = []
        action: GuardAction = "allow"
        reason: str | None = None
        verdict = "allow"
        metadata: dict[str, Any] = {}

        count = len(self.call_history[key])
        if count == REPEAT_WARN_COUNT:
            self.warned_keys.add(key)
            messages.append(f"这是第 {count} 次重复的调用，请注意是否必要")
            action = "warn"
            reason = reason or "repeat_call"
            verdict = "warn_repeat_call"

        signature = tool_similarity_signature(tool_name, args)
        if signature:
            self._discard_pending_near_duplicate(tool_name, signature)
            previous_count = self._near_duplicate_previous_count(tool_name, signature)
            history = self.near_duplicate_history[tool_name]
            history.append(signature)
            if len(history) > NEAR_DUPLICATE_HISTORY_LIMIT:
                del history[:-NEAR_DUPLICATE_HISTORY_LIMIT]
            similar_calls = previous_count + 1
            if similar_calls >= NEAR_DUPLICATE_BLOCK_COUNT:
                metadata = self._near_duplicate_info(tool_name, args, signature, similar_calls)
                self.last_decision = ToolGuardDecision(
                    action="block",
                    reason="near_duplicate_tool_calls",
                    message=self._near_duplicate_message(metadata, blocked=True),
                    verdict="block_near_duplicate_tool_calls",
                    metadata=metadata,
                )
                return self.last_decision
            if similar_calls >= NEAR_DUPLICATE_WARN_COUNT:
                info = self._near_duplicate_info(tool_name, args, signature, similar_calls)
                messages.append(self._near_duplicate_message(info, blocked=False))
                action = "warn"
                reason = reason or "near_duplicate_tool_calls"
                verdict = "warn_near_duplicate_tool_calls"
                metadata = metadata or info

        self.last_decision = ToolGuardDecision(
            action=action,
            reason=reason,
            message="\n".join(messages) if messages else None,
            verdict=verdict,
            metadata=metadata,
        )
        return self.last_decision

    def record_outcome(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        success: bool,
        outcome: str = "success",
        metadata: dict[str, Any] | None = None,
    ) -> ToolGuardDecision:
        """记录工具执行结果并给出守卫决策（编排层；拆分自 102 行超限函数）。"""
        key = make_key(tool_name, args)
        metadata = dict(metadata or {})
        progress = has_meaningful_progress(metadata)
        self._append_outcome_history(key, success, outcome, progress, metadata)

        if success:
            return self._record_success(key, tool_name, outcome)

        failures, identical_threshold, streak_count = self._update_failure_streaks(
            key, tool_name, outcome, progress, metadata,
        )
        block_info = self._detect_block(
            key, tool_name, outcome, failures, identical_threshold, progress, streak_count,
        )
        self.last_decision = self._build_failure_decision(
            tool_name, outcome, failures, progress, block_info,
        )
        return self.last_decision

    def _append_outcome_history(
        self,
        key: tuple,
        success: bool,
        outcome: str,
        progress: bool,
        metadata: dict[str, Any],
    ) -> None:
        self.outcome_history[key].append({
            "ts": time.time(),
            "success": success,
            "outcome": outcome,
            "progress": progress,
            "metadata": metadata,
        })
        self.outcome_history[key] = self.outcome_history[key][-20:]

    def _record_success(self, key: tuple, tool_name: str, outcome: str) -> ToolGuardDecision:
        self.consecutive_failures[key] = 0
        self.blocked_keys.pop(key, None)
        if self.tool_failure_streaks.get(tool_name, {}).get("outcome") in (None, outcome):
            self.tool_failure_streaks.pop(tool_name, None)
        self.last_decision = ToolGuardDecision(action="allow", verdict="success")
        return self.last_decision

    def _update_failure_streaks(
        self,
        key: tuple,
        tool_name: str,
        outcome: str,
        progress: bool,
        metadata: dict[str, Any],
    ) -> tuple[int, int, int]:
        """返回 (consecutive_failures, identical_threshold, streak_count)。"""
        failures = self.consecutive_failures[key] + 1
        self.consecutive_failures[key] = failures

        identical_threshold = IDENTICAL_TIMEOUT_BLOCK_COUNT if outcome == "timeout" else IDENTICAL_FAILURE_BLOCK_COUNT
        if progress and tool_name in LONG_RUNNING_TOOLS:
            identical_threshold += 1

        streak = self.tool_failure_streaks.get(tool_name)
        if streak and streak.get("outcome") == outcome:
            streak_count = int(streak.get("count", 0)) + 1
        else:
            streak_count = 1
        self.tool_failure_streaks[tool_name] = {
            "outcome": outcome,
            "count": streak_count,
            "progress": progress,
            "metadata": metadata,
        }
        return failures, identical_threshold, streak_count

    def _detect_block(
        self,
        key: tuple,
        tool_name: str,
        outcome: str,
        failures: int,
        identical_threshold: int,
        progress: bool,
        streak_count: int,
    ) -> dict[str, Any] | None:
        if failures >= identical_threshold:
            block_info = {
                "scope": "identical_call",
                "tool": tool_name,
                "args_key": key[1],
                "outcome": outcome,
                "consecutive_failures": failures,
                "threshold": identical_threshold,
                "progress": progress,
            }
            self.blocked_keys[key] = block_info
            return block_info
        if tool_name in LONG_RUNNING_TOOLS and outcome == "timeout":
            threshold = AGENT_TIMEOUT_BLOCK_COUNT_WITH_PROGRESS if progress else AGENT_TIMEOUT_BLOCK_COUNT
            if streak_count >= threshold:
                block_info = {
                    "scope": "tool_timeout_streak",
                    "tool": tool_name,
                    "outcome": outcome,
                    "streak_count": streak_count,
                    "threshold": threshold,
                    "progress": progress,
                }
                self.blocked_tools[tool_name] = block_info
                return block_info
        return None

    def _build_failure_decision(
        self,
        tool_name: str,
        outcome: str,
        failures: int,
        progress: bool,
        block_info: dict[str, Any] | None,
    ) -> ToolGuardDecision:
        if block_info:
            return ToolGuardDecision(
                action="block",
                reason=block_info["scope"],
                message=self._block_message(block_info),
                verdict="block_identical_failure" if block_info["scope"] == "identical_call" else "block_tool_timeout_streak",
                metadata=block_info,
            )
        if failures >= 2:
            message = (
                f"工具 `{tool_name}` 已连续失败 {failures} 次，最近 outcome={outcome}。"
                "不要重复相同调用；检查失败原因，缩小任务，换工具/参数，或直接给出当前证据下的结论。"
            )
            return ToolGuardDecision(
                action="warn",
                reason="consecutive_failure",
                message=message,
                verdict="warn_consecutive_failure",
                metadata={"tool": tool_name, "outcome": outcome, "consecutive_failures": failures, "progress": progress},
            )
        return ToolGuardDecision(
            action="allow",
            reason="failure_recorded",
            verdict="failure_recorded",
            metadata={"tool": tool_name, "outcome": outcome, "consecutive_failures": failures, "progress": progress},
        )

    def precheck(self, tool_name: str, args: dict[str, Any]) -> ToolGuardDecision:
        key = make_key(tool_name, args)
        blocked = self.blocked_keys.get(key) or self.blocked_tools.get(tool_name)
        if blocked:
            self.last_decision = ToolGuardDecision(
                action="block",
                reason=blocked.get("scope"),
                message=self._block_message(blocked),
                verdict="blocked_before_execution",
                metadata=blocked,
            )
            return self.last_decision

        near_duplicate = self.near_duplicate_precheck(tool_name, args)
        if near_duplicate:
            self.last_decision = near_duplicate
            return self.last_decision

        self.last_decision = ToolGuardDecision(action="allow", verdict="allow")
        return self.last_decision

    def _block_message(self, info: dict[str, Any]) -> str:
        tool = info.get("tool", "tool")
        outcome = info.get("outcome", "failure")
        if info.get("scope") == "tool_timeout_streak":
            count = info.get("streak_count")
            threshold = info.get("threshold")
            return (
                f"Loop guard blocked `{tool}`: it timed out {count} times (threshold={threshold}, outcome={outcome}). "
                "Do not spawn the same kind of long-running sub-agent again. Decompose the task, use direct tools "
                "such as read_file/grep_search/web_search/run_shell, or finish with the evidence already collected."
            )
        count = info.get("consecutive_failures")
        threshold = info.get("threshold")
        args_preview = str(info.get("args_key", ""))[:500]
        return (
            f"Loop guard blocked repeated `{tool}` call after {count} consecutive {outcome} results "
            f"(threshold={threshold}). Arguments: {args_preview}. "
            "Do not retry the identical call. Change strategy, use a different tool/arguments, or finish with current evidence."
        )

    def is_going_off_track(self, tool_name: str, args: dict[str, Any]) -> bool:
        key = make_key(tool_name, args)
        return key in self.warned_keys and len(self.call_history[key]) >= OFF_TRACK_COUNT

    def detect_cycle(self) -> str | None:
        if len(self.tool_sequence) < 4:
            return None

        last_4 = self.tool_sequence[-4:]
        if last_4[0] == last_4[2] and last_4[1] == last_4[3] and last_4[0] != last_4[1]:
            cycle_desc = f"检测到工具调用循环：{last_4[0]} → {last_4[1]} → {last_4[0]} → {last_4[1]}"
            if self.cycle_warned:
                self.cycle_count_after_warn += 1
            else:
                self.cycle_warned = True
            return cycle_desc

        if len(self.tool_sequence) >= 6:
            last_6 = self.tool_sequence[-6:]
            if (
                last_6[0] == last_6[3]
                and last_6[1] == last_6[4]
                and last_6[2] == last_6[5]
                and len(set(last_6[:3])) == 3
            ):
                cycle_desc = f"检测到工具调用循环：{last_6[0]} → {last_6[1]} → {last_6[2]} → ..."
                if self.cycle_warned:
                    self.cycle_count_after_warn += 1
                else:
                    self.cycle_warned = True
                return cycle_desc

        return None

    def is_cycle_bad_case(self) -> bool:
        return self.cycle_warned and self.cycle_count_after_warn >= 2

    def should_force_stop(self) -> bool:
        if self.blocked_keys or self.blocked_tools:
            return True
        for timestamps in self.call_history.values():
            if len(timestamps) >= FORCE_STOP_COUNT:
                return True
        return False

    def get_force_stop_info(self) -> dict[str, Any] | None:
        if self.blocked_keys:
            key, info = next(iter(self.blocked_keys.items()))
            return {
                "tool": info.get("tool") or key[0],
                "count": info.get("consecutive_failures") or len(self.call_history.get(key, [])),
                "args": json.loads(key[1]) if key[1] else {},
                "reason": "blocked_identical_failure",
                "outcome": info.get("outcome"),
                "guard": info,
            }
        if self.blocked_tools:
            tool, info = next(iter(self.blocked_tools.items()))
            return {
                "tool": tool,
                "count": info.get("streak_count"),
                "args": {},
                "reason": "blocked_tool_timeout_streak",
                "outcome": info.get("outcome"),
                "guard": info,
            }
        for key, timestamps in self.call_history.items():
            if len(timestamps) >= FORCE_STOP_COUNT:
                tool_name, args_key = key
                return {
                    "tool": tool_name,
                    "count": len(timestamps),
                    "args": json.loads(args_key) if args_key else {},
                    "reason": "repeat_force_stop",
                }
        return None

    def reset(self) -> None:
        self.call_history.clear()
        self.tool_sequence.clear()
        self.warned_keys.clear()
        self.cycle_warned = False
        self.cycle_count_after_warn = 0
        self.outcome_history.clear()
        self.consecutive_failures.clear()
        self.tool_failure_streaks.clear()
        self.blocked_keys.clear()
        self.blocked_tools.clear()
        self.near_duplicate_history.clear()
        self.pending_near_duplicate_signatures.clear()
        self.last_decision = None


class RepeatGuard:
    """同工具+同参数连续重复调用检测（U0 从 Agent._check_repeat_guard 迁入）。

    与 ToolCallTracker 的窗口检测互补：本守卫只看"连续同 key"链，
    第 3 次注入自省警告、第 5/8 次注入带参数预览的强警告。
    状态在每轮开始与上下文折叠后重置（原 Agent._record_fold_event 语义）。
    """

    def __init__(self) -> None:
        self._chain_key: str = ""
        self._chain_count: int = 0

    def reset(self) -> None:
        self._chain_key = ""
        self._chain_count = 0

    def check(self, tool_name: str, inp: dict) -> str | None:
        key = json.dumps([tool_name, canonical_args(inp)], ensure_ascii=False)
        if key == self._chain_key:
            self._chain_count += 1
        else:
            self._chain_key = key
            self._chain_count = 1
        if self._chain_count == 3:
            return (
                "You are repeating the exact same tool call with identical arguments. "
                "Carefully analyze the previous result before calling again: if the task is "
                "not complete, try a different approach or different arguments instead of "
                "repeating the call."
            )
        if self._chain_count in (5, 8):
            preview = canonical_args(inp)[:500]
            return (
                f"Repeated tool call detected:\n"
                f"- tool: {tool_name}\n"
                f"- consecutive_calls: {self._chain_count}\n"
                f"- arguments: {preview}\n"
                f"The repeated calls are not making progress. Do not call this tool with "
                f"these exact arguments again. Inspect the latest result and choose a "
                f"different action, different arguments, or finish the task if enough "
                f"evidence has been gathered."
            )
        return None


def check_tool_warnings(
    tracker: ToolCallTracker,
    tool_name: str,
    args: dict[str, Any],
    *,
    success: bool = True,
    outcome: str = "success",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """记录工具结果并检查警告/硬兜底。"""
    result: dict[str, Any] = {
        "warnings": [],
        "going_off_track": False,
        "cycle_bad_case": False,
        "force_stop": False,
        "force_stop_info": None,
        "blocked": False,
        "blocked_info": None,
        "verdict": "allow",
        "outcome": outcome,
    }

    call_decision = tracker.record_call(tool_name, args)
    if call_decision.message:
        result["warnings"].append(call_decision.message)
    result["verdict"] = call_decision.verdict
    if call_decision.action == "block":
        result["blocked"] = True
        result["blocked_info"] = call_decision.metadata
        result["force_stop"] = True
        result["force_stop_info"] = call_decision.metadata

    decision = tracker.record_outcome(
        tool_name,
        args,
        success=success,
        outcome=outcome,
        metadata=metadata,
    )
    if decision.message:
        result["warnings"].append(decision.message)
    if decision.action == "block":
        result["blocked"] = True
        result["blocked_info"] = result["blocked_info"] or decision.metadata
        result["force_stop"] = True
        result["force_stop_info"] = result["force_stop_info"] or tracker.get_force_stop_info()
        result["verdict"] = decision.verdict
    elif call_decision.action != "block":
        result["verdict"] = decision.verdict

    cycle_warning = tracker.detect_cycle()
    if cycle_warning:
        result["warnings"].append(cycle_warning)

    if tracker.is_going_off_track(tool_name, args):
        result["going_off_track"] = True

    if tracker.is_cycle_bad_case():
        result["cycle_bad_case"] = True

    if not result["force_stop"] and tracker.should_force_stop():
        result["force_stop"] = True
        result["force_stop_info"] = tracker.get_force_stop_info()

    return result
