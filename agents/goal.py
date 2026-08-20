"""/goal 自主目标模式。

流程：
1. 用户 `/goal <目标>` → side query 把目标提炼成可判定的成功标准，用户确认；
2. GoalLoop 反复驱动 agent.chat 执行，每轮结束后用 verifier side query
   逐条判定成功标准（verifier 可引用 agent 的回复与工具证据）；
3. 未达标 → 把未达标项及证据回灌给 agent 继续；
4. 全部达标 / 迭代预算用尽 / 用户 Ctrl+C 中止 → 停止。

安全网：每轮都是普通 chat() 轮次，轮次边界与文件快照照常记录，
随时可用 /rewind 回退整个 goal 过程造成的改动。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .ui import print_info, print_warning

SideQueryFn = Callable[[str, str], Any]  # actually Awaitable[str]

DEFAULT_MAX_ITERATIONS = 10

EXTRACT_CRITERIA_PROMPT = """You convert a user's goal into verifiable success criteria for an autonomous coding agent.

Return ONLY a JSON object: {"criteria": ["...", "..."]}
- 2 to 5 criteria, each short, concrete, and independently checkable (e.g. "file X exists and contains Y", "tests pass", "function Z returns ...").
- Avoid vague criteria like "code is good".
- Do NOT explain. Do NOT add any text outside the JSON object."""

VERIFY_PROMPT = """You are a strict verifier for an autonomous coding agent's goal. Judge ONLY based on the evidence provided (the agent's report). Do not assume anything not in evidence.

Return ONLY a JSON object:
{"criteria": [{"id": 1, "met": true, "evidence": "short quote or observation"}], "all_met": false}

Rules:
- One entry per criterion, in order, ids starting at 1.
- "met" is true only if the evidence clearly satisfies the criterion.
- For unmet criteria, "evidence" must say what is missing or failing.
- "all_met" is true only if every criterion is met.
- Do NOT explain outside the JSON object."""


@dataclass
class GoalState:
    """一个 goal 的运行状态（可序列化进 session）。"""

    goal: str
    criteria: list[str] = field(default_factory=list)
    iteration: int = 0
    status: str = "running"  # running | achieved | budget_exhausted | aborted
    last_feedback: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "criteria": self.criteria,
            "iteration": self.iteration,
            "status": self.status,
            "last_feedback": self.last_feedback,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalState":
        return cls(
            goal=str(data.get("goal") or ""),
            criteria=[str(c) for c in (data.get("criteria") or [])],
            iteration=int(data.get("iteration") or 0),
            status=str(data.get("status") or "running"),
            last_feedback=str(data.get("last_feedback") or ""),
        )


def _extract_json(text: str) -> dict | None:
    """从模型回复中提取第一个 JSON 对象；失败返回 None。"""
    match = re.search(r"\{[\s\S]*\}", text or "")
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


async def extract_goal_criteria(goal: str, side_query: SideQueryFn) -> list[str]:
    """用 side query 把目标提炼成成功标准列表；解析失败返回空列表。"""
    try:
        text = await side_query(EXTRACT_CRITERIA_PROMPT, f"Goal: {goal}")
    except Exception as e:
        print_warning(f"[goal] criteria extraction failed: {e}")
        return []
    parsed = _extract_json(text)
    if not parsed:
        return []
    criteria = parsed.get("criteria")
    if not isinstance(criteria, list):
        return []
    return [str(c).strip() for c in criteria if str(c).strip()]


def _compose_criteria_block(criteria: list[str]) -> str:
    return "\n".join(f"{i}. {c}" for i, c in enumerate(criteria, 1))


class GoalLoop:
    """执行→验证→回灌证据的自主循环。"""

    def __init__(
        self,
        agent: Any,
        goal: str,
        criteria: list[str],
        *,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        side_query: SideQueryFn | None = None,
    ):
        self.agent = agent
        self.state = GoalState(goal=goal, criteria=list(criteria))
        self.max_iterations = max(1, max_iterations)
        self._side_query = side_query

    def _verifier_query(self) -> SideQueryFn | None:
        if self._side_query is not None:
            return self._side_query
        builder = getattr(self.agent, "_build_side_query", None)
        if builder is None:
            return None
        try:
            return builder(max_tokens=2400)
        except Exception:
            return None

    async def run(self) -> GoalState:
        first_prompt = (
            f"[GOAL MODE] Work autonomously toward this goal:\n"
            f"{self.state.goal}\n\n"
            f"Success criteria:\n{_compose_criteria_block(self.state.criteria)}\n\n"
            f"Work step by step. Use tools to make real progress, then report "
            f"what you did and the observable evidence for each criterion."
        )
        prompt = first_prompt
        while True:
            # 用户中止（Ctrl+C）→ 停止循环，已完成的轮次可用 /rewind 回退。
            if getattr(self.agent, "_abort_event", None) is not None and self.agent._abort_event.is_set():
                self.state.status = "aborted"
                break
            if self.state.iteration >= self.max_iterations:
                self.state.status = "budget_exhausted"
                break

            self.state.iteration += 1
            print_info(f"[goal] iteration {self.state.iteration}/{self.max_iterations}")
            await self.agent.chat(prompt)

            if getattr(self.agent, "_aborted", False):
                self.state.status = "aborted"
                break

            verdict = await self._verify()
            if verdict["all_met"]:
                self.state.status = "achieved"
                print_info("[goal] all success criteria met ✔")
                break

            self.state.last_feedback = verdict["feedback"]
            prompt = (
                f"[GOAL MODE] Not all success criteria are met yet.\n"
                f"{verdict['feedback']}\n\n"
                f"Continue working toward the goal and report new evidence."
            )
        return self.state

    async def _verify(self) -> dict[str, Any]:
        sq = self._verifier_query()
        if sq is None:
            # 没有可用的 side query 时无法验证，保守地继续执行。
            return {"all_met": False, "feedback": "(verifier unavailable)"}

        evidence = getattr(self.agent, "_last_assistant_text", "") or "(no report)"
        user_prompt = (
            f"Goal: {self.state.goal}\n\n"
            f"Success criteria:\n{_compose_criteria_block(self.state.criteria)}\n\n"
            f"Agent's report from the latest iteration:\n{evidence}"
        )
        try:
            text = await sq(VERIFY_PROMPT, user_prompt)
        except Exception as e:
            print_warning(f"[goal] verification failed: {e}")
            return {"all_met": False, "feedback": f"(verifier error: {e})"}

        parsed = _extract_json(text)
        if not parsed or not isinstance(parsed.get("criteria"), list):
            # 格式兜底：回复里明确说全部达成时视为达成。
            if "all_met\": true" in (text or "").replace(" ", ""):
                return {"all_met": True, "feedback": ""}
            return {
                "all_met": False,
                "feedback": "(verifier returned an unparseable response; re-check each criterion and report concrete evidence)",
            }

        unmet: list[str] = []
        all_met = True
        for i, item in enumerate(parsed["criteria"]):
            if not isinstance(item, dict):
                all_met = False
                continue
            met = bool(item.get("met"))
            criterion = self.state.criteria[i] if i < len(self.state.criteria) else f"criterion {i + 1}"
            if not met:
                all_met = False
                evidence_note = str(item.get("evidence") or "").strip()
                unmet.append(f"- NOT MET: {criterion}" + (f" — {evidence_note}" if evidence_note else ""))

        feedback = "\n".join(unmet) if unmet else ""
        return {"all_met": all_met, "feedback": feedback}
