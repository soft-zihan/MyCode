"""SkillOrchestrator — Skill 进化、使用追踪的编排器。

职责：
- Skill 进化触发与执行
- Skill 使用追踪与评判
- 后台 Skill 任务管理
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable

from agents.logging import print_confirmation, print_error, print_info


# 类型别名
SideQueryFn = Callable[[str, str], Awaitable[str]]
ConfirmFn = Callable[[str], Awaitable[bool]]
RefreshFn = Callable[[], None]


class SkillOrchestrator:
    """Skill 进化、使用追踪的编排器。"""

    def __init__(
        self,
        *,
        side_query_fn: Callable[[int], SideQueryFn | None],
        permission_mode: str = "default",
        session_id: str = "",
        refresh_system_prompt: RefreshFn | None = None,
    ) -> None:
        self._side_query_fn = side_query_fn
        self._permission_mode = permission_mode
        self._session_id = session_id
        self._refresh_system_prompt = refresh_system_prompt

        # Skill 检索状态
        self._last_retrieved_skill_reference: dict[str, Any] | None = None

        # Skill 进化状态
        self._pending_skill_extraction_window: dict[str, Any] | None = None
        self._turns_since_last_evolution: int = 0
        self._last_evolution_time: float = time.time()

        # 后台任务
        self._background_skill_tasks: set[asyncio.Task] = set()

    @property
    def last_retrieved_skill_reference(self) -> dict[str, Any] | None:
        return self._last_retrieved_skill_reference

    @last_retrieved_skill_reference.setter
    def last_retrieved_skill_reference(self, value: dict[str, Any] | None) -> None:
        self._last_retrieved_skill_reference = value

    @property
    def pending_skill_extraction_window(self) -> dict[str, Any] | None:
        return self._pending_skill_extraction_window

    @property
    def turns_since_last_evolution(self) -> int:
        return self._turns_since_last_evolution

    @turns_since_last_evolution.setter
    def turns_since_last_evolution(self, value: int) -> None:
        self._turns_since_last_evolution = value

    @property
    def last_evolution_time(self) -> float:
        return self._last_evolution_time

    @last_evolution_time.setter
    def last_evolution_time(self, value: float) -> None:
        self._last_evolution_time = value

    def set_permission_mode(self, mode: str) -> None:
        self._permission_mode = mode

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    def set_refresh_system_prompt(self, fn: RefreshFn) -> None:
        self._refresh_system_prompt = fn



    # ── 对话消息提取 ──

    def _normalize_text(self, text: str) -> str:
        # U0 清理：<retrieved_skills> 只有 strip 无 producer（死防御代码），删除
        return str(text or "").strip()

    def _message_text(self, msg: dict[str, Any]) -> str:
        content = msg.get("content")
        if isinstance(content, str):
            return self._normalize_text(content)
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif "content" in block and block.get("type") not in {"tool_result", "tool_use"}:
                        parts.append(str(block.get("content") or ""))
            return self._normalize_text("\n".join(parts))
        return ""

    def get_recent_dialog_messages(self, messages: list[dict[str, Any]], *, max_messages: int = 8) -> list[dict[str, str]]:
        """从消息列表中提取最近的对话消息。"""
        out: list[dict[str, str]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = self._message_text(msg)
            if text:
                out.append({"role": role, "content": text})
        return out[-max(2, int(max_messages)):]

    # ── 权限确认 ──

    async def _confirm_online_skill_write(self, summary: str, confirm_fn: ConfirmFn | None) -> bool:
        if self._permission_mode in {"bypassPermissions", "acceptEdits"}:
            return True
        if self._permission_mode in {"plan", "dontAsk"}:
            return False
        if confirm_fn is None:
            return False
        print_confirmation(summary)
        try:
            return bool(await confirm_fn(summary))
        except Exception:
            return False

    async def _confirm_background_online_skill_write(self, summary: str) -> bool:
        return self._permission_mode in {"bypassPermissions", "acceptEdits"}

    def _online_evolution_enabled(self) -> bool:
        raw = os.environ.get("MYCODE_AUTO_SKILL_EVOLUTION", "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    # ── 后台任务 ──

    def schedule_background_task(self, coro, plan_mode: bool = False) -> None:
        """调度后台 Skill 任务。"""
        if plan_mode:
            try:
                coro.close()
            except Exception:
                pass
            return
        task = asyncio.create_task(coro)
        self._background_skill_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._background_skill_tasks.discard(done_task)
            try:
                done_task.result()
            except Exception:
                pass

        task.add_done_callback(_done)

    async def drain_background_tasks(self) -> None:
        """等待所有后台 Skill 任务完成。"""
        tasks = [task for task in self._background_skill_tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── 提取窗口管理 ──

    def pop_pending_extraction_window(self, next_user_feedback: str, tool_error_streak: int = 0) -> dict[str, Any] | None:
        """弹出待处理的 Skill 提取窗口。"""
        pending = self._pending_skill_extraction_window
        self._pending_skill_extraction_window = None
        if not pending:
            return None
        messages = list(pending.get("messages") or [])
        feedback = next_user_feedback.strip()
        if feedback:
            messages.append({"role": "user", "content": feedback})
        pending["messages"] = messages[-10:]
        pending["next_user_feedback"] = feedback
        return pending

    def set_pending_extraction_window(
        self,
        *,
        messages: list[dict[str, Any]],
        original_user_message: str,
        assistant_text: str,
        retrieved_reference: dict[str, Any] | None,
        tool_error_streak: int = 0,
    ) -> None:
        """设置待处理的 Skill 提取窗口。"""
        if not original_user_message.strip() or not assistant_text.strip():
            return

        # EvoSkill 启发：失败驱动进化
        tool_error_hint = ""
        if tool_error_streak >= 3:
            tool_error_hint = f"[Tool failure streak: {tool_error_streak} consecutive failures detected]"

        self._pending_skill_extraction_window = {
            "messages": messages[-10:],
            "latest_user": original_user_message,
            "latest_assistant": assistant_text,
            "retrieved_reference": self._compact_retrieved_reference(retrieved_reference),
            "session_id": self._session_id,
            "tool_error_hint": tool_error_hint,
        }

    def _compact_retrieved_reference(self, ref: dict[str, Any] | None) -> dict[str, Any] | None:
        if not ref:
            return None
        return {k: v for k, v in ref.items() if k != "all_hits"}

    # ── 进化触发 ──

    def should_trigger_evolution(self) -> bool:
        """检查是否应触发 Skill 进化（cadence 门控）。"""
        min_turns = int(os.environ.get("MYCODE_SKILL_EVOLUTION_MIN_TURNS", "3"))
        min_minutes = int(os.environ.get("MYCODE_SKILL_EVOLUTION_MIN_MINUTES", "10"))

        turns_ok = self._turns_since_last_evolution >= min_turns
        minutes_since = (time.time() - self._last_evolution_time) / 60
        time_ok = minutes_since >= min_minutes

        return turns_ok and time_ok

    def record_evolution_event(self) -> None:
        """记录一次进化事件，重置 cadence 计数器。"""
        self._turns_since_last_evolution = 0
        self._last_evolution_time = time.time()

    # ── 进化执行 ──

    async def run_online_skill_evolution(
        self,
        window: dict[str, Any],
        *,
        interactive_confirm: bool = False,
        confirm_fn: ConfirmFn | None = None,
    ) -> dict[str, Any]:
        """执行在线 Skill 进化。"""
        if not self._online_evolution_enabled() or self._permission_mode == "plan":
            return {"ok": False, "action": "disabled"}

        messages = list(window.get("messages") or [])
        if not messages:
            return {"ok": False, "action": "no_messages"}

        side_query = self._side_query_fn(2200)
        if side_query is None:
            return {"ok": False, "action": "no_side_query"}

        try:
            from agents.skills.skill_extractor import online_ingest
        except Exception:
            return {"ok": False, "action": "import_error"}

        # EvoSkill 启发：合并失败信号到 hint
        hint_parts = []
        tool_error_hint = str(window.get("tool_error_hint") or "").strip()
        if tool_error_hint:
            hint_parts.append(tool_error_hint)
        user_hint = str(window.get("hint") or "").strip()
        if user_hint:
            hint_parts.append(user_hint)
        combined_hint = " | ".join(hint_parts) if hint_parts else ""

        result = await online_ingest(
            messages=messages,
            side_query=side_query,
            retrieved_reference=window.get("retrieved_reference") or None,
            hint=combined_hint,
            confirm_write=self._confirm_online_skill_write if interactive_confirm else self._confirm_background_online_skill_write,
            target=os.environ.get("MYCODE_AUTO_SKILL_TARGET", "project"),
        )
        if result.get("ok"):
            if result.get("action") in {"add", "merge"}:
                if self._refresh_system_prompt:
                    self._refresh_system_prompt()
                print_info(f"Online skill {result.get('action')}: {result.get('skill')}")
                await self._run_promotion_gate(side_query)
        elif result.get("action") not in {"add_denied", "merge_denied"}:
            print_error(f"Online skill evolution failed: {result.get('error') or result}")

        return result

    # ── 验证门禁 ──

    async def _run_promotion_gate(self, side_query: SideQueryFn) -> None:
        """Skill 变体变更后运行在线评测门禁。

        回放在线 provenance 样本 → 确定性规则 + LLM judge 评分 →
        仅当严格优于历史最佳（分差 >= 0.01 且无新增硬失败）才晋升 champion。
        评测失败只记日志，不影响主进化流程。
        """
        try:
            from agents.skills.skill_evaluator import evaluate_online_skill_evolution_async

            report = await evaluate_online_skill_evolution_async(side_query=side_query)
            aggregate = report.get("aggregate") if isinstance(report, dict) else {}
            champion_statuses = aggregate.get("champion_statuses") if isinstance(aggregate, dict) else {}
            print_info(f"[skill_gate] Online evaluation done: {champion_statuses or 'no promotable samples'}")
        except Exception as e:
            print_error(f"[skill_gate] Online evaluation failed: {type(e).__name__}: {e}")

    # ── 即时提取 ──

    async def extract_now(self, hint: str = "", confirm_fn: ConfirmFn | None = None) -> dict[str, Any]:
        """立即执行 Skill 提取（交互式）。"""
        pending = self._pending_skill_extraction_window
        if not pending:
            return {"ok": False, "error": "no pending online skill extraction window"}
        window = dict(pending)
        window["hint"] = hint
        result = await self.run_online_skill_evolution(window, interactive_confirm=True, confirm_fn=confirm_fn)
        self._pending_skill_extraction_window = None
        return {"ok": True, "result": result}

    # ── 重置 ──

    def reset(self) -> None:
        """重置所有状态。"""
        self._last_retrieved_skill_reference = None
        self._pending_skill_extraction_window = None
        self._turns_since_last_evolution = 0
        self._last_evolution_time = time.time()
