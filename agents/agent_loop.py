"""Agent 推理循环。

负责与 LLM 的交互循环：发送消息、接收响应、处理工具调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from agents.core.model_caller import ModelCaller
from agents.core.text_sanitization import safe_utf8_text
from agents.plan.plan_tool_executor import (
    PLAN_APPROVAL_SUPERSEDED,
    PLAN_APPROVAL_TOOL_NAME,
    _validate_and_load_draft,
    finalize_approved_plan,
    record_plan_rejection,
)
from agents.tools.file_snapshot import capture_file_snapshot
from agents.tools.registry import CONCURRENCY_SAFE_TOOLS
from agents.observability.tool_tracker import ToolCallTracker, check_tool_warnings
from agents.wiki.wiki_manager import format_wiki_for_injection
from agents.core.snapshot_service import SnapshotService
from agents.logging import print_error, print_info
from agents.observability.bad_cases import BadCase, BadCaseSeverity, BadCaseSource, BadCaseStatus, create_bad_case
from agents.tools.permissions import check_permission

if TYPE_CHECKING:
    from agents.agent import Agent


class AgentLoop:
    """Agent 推理循环。

    职责：
    - run(): 主循环入口
    - call_model_stream(): 流式模型调用
    - 重试逻辑
    - 流式事件分发
    """

    def __init__(self, agent: Agent):
        self._agent = agent
        self._tool_tracker = ToolCallTracker()
        self._model_caller = ModelCaller(agent)

    def _auto_mark_bad_case(self, signal_type: str, diagnosis: dict, tool_name: str | None = None) -> None:
        import json
        import uuid

        diagnosis_with_context = dict(diagnosis)
        diagnosis_with_context["trace_id"] = getattr(self._agent, "_current_trace_id", None)
        diagnosis_with_context["session_id"] = self._agent.session_id
        reason = signal_type
        if tool_name:
            reason = f"{signal_type}:{tool_name}"

        try:
            create_bad_case(BadCase(
                id=uuid.uuid4().hex,
                session_id=self._agent.session_id,
                source=BadCaseSource.AUTO_DETECT,
                status=BadCaseStatus.PENDING,
                severity=BadCaseSeverity.MEDIUM,
                turn_number=self._agent._current_turn,
                step_number=self._agent._current_step,
                tool_name=tool_name,
                signal_type=signal_type,
                reason=reason,
                comment=json.dumps(diagnosis_with_context, ensure_ascii=False, default=str),
            ))
        except Exception as exc:
            print_error(f"[bad_case] auto mark failed: {type(exc).__name__}: {exc}")

    async def _run_step(self) -> tuple[Any, int]:
        """单个模型调用步：step/start → 流式调用 → assistant_message 落盘 → step/end。

        返回 (本步的 tool_calls, 承载它的 assistant 消息的 seq)；tool_calls 为
        None 表示纯文本回复。

        seq 在 assistant_message 落盘的同一时刻捕获一次，之后沿工具执行链传到
        execute_tool_call，供需要事件记账的工具使用（task_list 的 started_seq /
        detail_origin_seq）。刻意不让工具去读 agent.last_usage_seq：那个属性
        同时是 token 估算的可变状态，同批次先执行的 compact_context 会把它重置
        回 -1（见 dispatcher 的 task_list 分支注释）。
        """
        self._agent.session.append("step/start", {
            "turn": self._agent._current_turn,
            "step": self._agent._current_step + 1,
        })
        self._agent._current_step += 1

        response = await self.call_model_stream()

        choice = response.get("choices", [{}])[0] if response.get("choices") else {}
        message = choice.get("message", {})

        assistant_event = self._agent.session.append("assistant_message", {
            "turn": self._agent._current_turn,
            "step": self._agent._current_step,
            "thinking": message.get("thinking"),
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls"),
        })
        assistant_seq = int(assistant_event.get("seq", -1))
        self._agent.mark_last_usage_position(assistant_seq)
        self._update_token_stats(response)

        self._agent.session.append("step/end", {
            "turn": self._agent._current_turn,
            "step": self._agent._current_step,
        })
        return message.get("tool_calls"), assistant_seq

    async def _finalize_turn_budget(self, budget: dict) -> None:
        """U5a：turn/cost 预算耗尽同样走收敛应答（旧实现直接 break，
        悬挂 tool_calls 被静默丢弃、用户拿不到任何最终答复）。"""
        print_info(f"Budget exceeded: {budget['reason']}")
        await self._drop_queued("budget_exceeded")
        _kind_stop = {
            "turns": "turn_budget_exceeded",
            "cost": "cost_budget_exceeded",
            "tool_calls": "tool_budget_exceeded",
        }
        await self._finalize_budget(
            _kind_stop.get(budget.get("kind"), "budget_exceeded"),
            budget["reason"],
            "budget_exceeded",
        )

    async def _finalize_tool_budget(self) -> None:
        """工具调用预算耗尽：丢弃排队消息并走收敛应答。"""
        await self._drop_queued("tool_budget")
        a = self._agent
        reason = (
            f"Tool call budget exceeded: {a._tool_call_count}/{a.max_tool_calls}"
            if a.max_tool_calls is not None
            else "Tool call budget exceeded"
        )
        await self._finalize_budget("tool_budget_exceeded", reason, "tool_budget_exceeded")

    async def run(self, user_message: str | None) -> None:
        """主推理循环入口。user_message=None 为 U3b 自动唤醒轮（不写用户消息事件）。"""
        await self._prepare_turn(user_message)
        
        while True:
            if self._agent.abort_requested():
                self._agent.mark_aborted()
                await self._drop_queued("aborted")
                break

            await self._consume_wiki_prefetch()

            tool_calls, assistant_seq = await self._run_step()

            if not tool_calls:
                # U1：turn 收尾前 drain 双队列——steering/follow_up 尾到则同 run 内 continue
                # 消费（不新起 turn/start，满足"追问不重启轮次"）
                if await self._drain_steering() or await self._drain_follow_up():
                    continue
                # plan 模式：产物格式合格就送审（非阻塞），不合格就把错误喂回让模型
                # 自己修。返回 True = 已注入一条消息、本回合要继续跑。
                # 位置在双队列之后：用户刚插进来的话优先于「校验产物」，否则模型会先
                # 被喂一条格式错误、再被喂用户的话，两条消息互相打架。
                if await self._plan_mode_handoff():
                    continue
                await self._finalize_text_response()
                break

            self._agent.increment_turns()
            budget = self._agent.check_budget()
            if budget["exceeded"]:
                await self._finalize_turn_budget(budget)
                break

            guard_stop = await self._handle_tool_calls(tool_calls, assistant_seq)
            if guard_stop:
                await self._drop_queued("loop_guard")
                self._finalize_loop_guard_stop()
                break

            # U1：step 边界（工具批次完成后、下次模型调用前）——steering 唯一注入点
            # （v2 llm.ts promote 语义：注入进当前 run，不重启轮次）
            await self._drain_steering()

            if self._agent.tool_budget_exceeded():
                await self._finalize_tool_budget()
                break

            self._agent.clear_context_flag()
            self._agent.refresh_runtime_system_prompt()

    # ── U1 steer 接线：队列 drain/注入/丢弃审计 ──────────────────────

    async def _inject_queued(self, msgs: list, queue_name: str) -> None:
        """队列消息 → user_message 事件落盘（Event 唯一数据源）+ steer/delivered 审计事件。"""
        a = self._agent
        for m in msgs:
            a.append_user_message(m.content)
            a.session.append("steer/delivered", {
                "content": m.content[:200],
                "source": m.source,
                "queue": queue_name,
            })

    async def _drain_steering(self) -> bool:
        """step 边界注入 steering。返回是否有注入。"""
        msgs = await self._agent.message_queue.drain_steering()
        if not msgs:
            return False
        await self._inject_queued(msgs, "steering")
        # 纠偏即新方向：旧方向的连击计数语义失效，重置（硬预算不动）
        self._agent._tool_error_streak = 0
        self._agent._same_tool_repeat_count = 0
        self._agent._last_tool_name = ""
        return True

    async def _drain_follow_up(self) -> bool:
        """turn 收尾边界注入 follow_up。返回是否有注入。"""
        msgs = await self._agent.message_queue.drain_follow_up()
        if not msgs:
            return False
        await self._inject_queued(msgs, "follow_up")
        return True

    async def _drop_queued(self, reason: str) -> None:
        """终态 break（abort/预算/loop guard）丢弃残留队列——落盘审计，不静默吞。"""
        q = self._agent.message_queue
        dropped = await q.drain_steering() + await q.drain_follow_up()
        if dropped:
            self._agent.session.append("steer/dropped", {
                "reason": reason,
                "count": len(dropped),
                "contents": [m.content[:100] for m in dropped],
            })
        await q.clear()

    # ── plan 模式：turn 收尾的产物校验送审（spec §D1/§D2/§D3）──────────

    # 连续不合格时自动续回合让模型自我修正的次数上限（spec §D2）。
    # 两端都刻意不选：无界修正会烧钱并可能死循环；一次不合格就甩给用户是让用户当
    # 传声筒（模型往往只差一个标题层级）。「结构校验 + 有限次纠正重试」项目里已有
    # 先例（压缩模板就是这么做的）。
    _PLAN_VALIDATION_RETRY_LIMIT = 2

    async def _plan_mode_handoff(self) -> bool:
        """turn 收尾钩子：plan.md 合格就送审（非阻塞），不合格就把错误喂回模型。

        返回 True = 已注入一条消息、本回合要继续跑；False = 照常收尾结束回合。

        这个函数坐在**所有会话共享**的收尾路径上（spec §十.2），所以两件事是硬的：
        ① `permission_mode != "plan"` 时立刻返回，一个属性都不读、一个事件都不落；
        ② 整个函数体包 try/except，失败方向是「不送审、不阻塞回合」——但**必须留痕**
           （stderr 一条 + 事件流一条）。静默降级还给上层一个成功信号是本项目最贵的
           缺陷类：C1 里一个非字符串字段在两层嵌套的宽 except 内抛出来，把两档上下文
           可见性一起关掉，而工具照样返回 ok:true，十一次评审都没发现。
        """
        if self._agent.permission_mode != "plan":
            return False
        try:
            return await self._plan_mode_handoff_inner()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            print_error(
                "[plan] turn-end validation hook failed; the turn ends normally and "
                f"the plan was NOT submitted for approval: {detail}"
            )
            self._agent.session.append("error", {
                "message": f"plan 送审钩子异常（回合照常结束，计划未送审）: {detail}",
            })
            return False

    async def _plan_mode_handoff_inner(self) -> bool:
        """钩子主体。调用前 permission_mode == "plan" 已成立。"""
        a = self._agent
        mgr = a._plan_mode_manager
        if mgr is None or mgr.plan_dir is None:
            # 在 plan 模式却没有草稿目录：没有产物可校验，什么都不做。
            return False

        validation = mgr.validate_plan_artifacts()
        if not validation["valid"]:
            return self._feed_plan_errors_back(validation["errors"])

        # 装载草稿复用 plan_tool_executor 的同一个函数，不重写装载逻辑（两个装载器
        # 必然漂移）。校验刚放行、装载又返回失败文案 = 两侧判定口径漂移了。
        draft = _validate_and_load_draft(mgr)
        if isinstance(draft, str):
            print_error(f"[plan] draft load disagreed with the validator: {draft!r}")
            a.session.append("error", {
                "message": f"plan 草稿装载与校验器判定不一致（计划未送审）: {draft}",
            })
            return False

        if a.auto_approve_enabled():
            # spec §D9：开关已开 → **不落 permission/request**（沿用上一轮的裁定：
            # 开关已开时不凭空造请求），当场 finalize 并 continue 去执行。
            # 起始消息 = finalize_approved_plan 的返回值（D8 的当前状态行 + 物化说明）。
            a.append_user_message(finalize_approved_plan(a, draft, "execute"))
            return True

        if mgr.plan_approval_fn is not None:
            # spec §十.4：CLI/headless 的就地问人回退（阻塞）。
            return await self._plan_approval_blocking(draft)

        self._supersede_pending_plan_approval()
        rpc_id = uuid.uuid4().hex[:8]
        # 字段形状与权限门那条 permission/request **完全一致**（rpc_id 与 request_id
        # 同值、command 承载要审的文本、tool_name 是种类标记、sub_agent_id 透传），
        # 于是前端的既有渲染与「从历史恢复待审批」逻辑一行都不用改（spec §D4）。
        a.session.append("permission/request", {
            "rpc_id": rpc_id,
            "request_id": rpc_id,
            "command": draft.full_plan,
            "tool_name": PLAN_APPROVAL_TOOL_NAME,
            "message": "计划已写完并通过格式校验，等待批准。",
            "sub_agent_id": a.current_sub_agent_id,
        })
        a.set_pending_plan_approval({
            "rpc_id": rpc_id,
            "draft": draft,
            "choice_options": ["execute", "manual-execute", "clear-and-execute", "keep-planning"],
        })
        # 返回 False → 回合正常结束（turn/end 落盘），审批作为纯会话状态挂着。
        # 这就是 spec §D3 的非阻塞：没有任何协程在等，于是「等人的时候不能被计时器
        # 杀掉」这件事根本不存在（原 600s 工具超时缺陷就是阻塞式的产物）。
        return False

    def _supersede_pending_plan_approval(self) -> None:
        """spec §十.3：已有待审批时又产出一份新的合格计划 → 作废旧的那条。

        漏了它，用户会批准一份已经不是当前内容的计划：对话框还开着（历史里那条
        request 没有配对的 resolved），而 plan.md 已经被改过。作废方式是落
        permission/resolved{outcome:"superseded"}——请求已落盘，决议就必须落盘，
        否则前端的「从历史恢复待审批」会把它复活成幽灵对话框（上一轮踩过这条）。
        """
        a = self._agent
        old = a._pending_plan_approval
        if old is None:
            return
        a.session.append("permission/resolved", {
            "rpc_id": old.get("rpc_id", ""),
            "outcome": PLAN_APPROVAL_SUPERSEDED,
        })
        a._pending_plan_approval = None

    async def _plan_approval_blocking(self, draft) -> bool:
        """spec §十.4 的 CLI/headless 分叉：就地阻塞问人，拿到答复当场落地。

        判据是 `mgr.plan_approval_fn is not None`。⚠️ spec 对这个判据的**标注是反的**
        （它写「注册了（前端服务）→ 非阻塞；没注册（CLI/headless）→ input()」），
        而全仓只有一个注册点：agents/main.py:286 的交互式 CLI。前端服务器一个都不
        注册——它靠 permission/request 事件 + HTTP 应答。所以正确的读法是
        「注册了 = 终端上坐着一个人 = 可以就地问」；照 spec 的字面标注实现会让 CLI
        落一条没人应答的事件、回合结束后审批永久挂着。判据本身沿用，方向按事实。

        两条路共用同一个校验器（上面刚跑过）与同一个 finalize_approved_plan，
        只在「怎么问人」这一处分叉。返回值恒为 True：答复已经拿到，本回合直接接着执行
        （CLI 没有「起一个新回合」这回事，也没有前端要通知）。
        """
        a = self._agent
        result = await a._plan_mode_manager.plan_approval_fn(draft.full_plan) or {}
        choice = result.get("choice") or "manual-execute"
        if choice == "keep-planning":
            a.append_user_message(record_plan_rejection(a, result.get("feedback")))
            return True
        a.append_user_message(finalize_approved_plan(a, draft, choice))
        return True

    def _feed_plan_errors_back(self, errors: list[str]) -> bool:
        """不合格 → 把结构化错误喂回模型、自动续一个回合让它自己修（有界）。

        超过上限时把错误清单落成一条**用户可见**的 assistant 消息后结束回合：
        「计划没送审」这件事如果只在 stderr 里说，用户看到的就是模型写完了却什么都没
        发生——那是本项目最贵的那类静默故障。
        """
        a = self._agent
        a._plan_validation_attempts += 1
        listing = "\n".join(f"- {e}" for e in errors)

        if a._plan_validation_attempts > self._PLAN_VALIDATION_RETRY_LIMIT:
            message = (
                "计划未能送审：plan.md 的格式在 "
                f"{self._PLAN_VALIDATION_RETRY_LIMIT} 次自动修正后仍不合格。\n\n"
                f"{listing}\n\n"
                "修正上面这些问题后再结束回复，系统会重新校验并送审。"
            )
            if a._turn_output_buffer is not None:
                a._turn_output_buffer.append(message)
            a.session.append("assistant_message", {
                "turn": a._current_turn,
                "step": a._current_step,
                "content": message,
            })
            return False

        # 注入的形状照 _inject_queued：user_message 事件落盘（Event 是唯一数据源），
        # 于是下一轮模型调用看得到它。
        a.append_user_message(
            "plan.md 的格式校验未通过，计划**没有**送审。请按下面的清单修正 "
            f"{a._plan_mode_manager.plan_dir}/plan.md，然后直接结束回复"
            "（不要调用任何退出或送审工具），系统会自动重新校验：\n\n" + listing
        )
        return True

    async def _prepare_turn(self, user_message: str | None) -> None:
        """准备轮次：清理消息、重置状态、创建快照。None=自动唤醒轮（U3b）。"""
        from pathlib import Path
        
        a = self._agent
        clean_message = safe_utf8_text(user_message).strip() if user_message is not None else ""
        
        # 重置工具调用跟踪器
        self._tool_tracker.reset()
        
        snapshot_id = None
        if not a.is_sub_agent:
            cwd = a.session.projections.get("cwd")
            if cwd:
                try:
                    snapshot_dir = Path.home() / ".mycode" / "snapshots"
                    svc = SnapshotService(cwd, snapshot_dir)
                    snapshot = await svc.capture(
                        session_id=a.session.id,
                        label=f"Before: {clean_message[:50]}" if user_message is not None else "Before: [auto-wake]",
                    )
                    snapshot_id = snapshot.id
                    print(f"[DEBUG] snapshot created: {snapshot_id}")
                except Exception as e:
                    print(f"[ERROR] Failed to create snapshot: {type(e).__name__}: {e}")
                    import traceback
                    traceback.print_exc()
        
        if user_message is not None:
            a.append_user_message(clean_message, snapshot_id=snapshot_id)
        # D3 hotfix：chat() 暂存的记忆/提醒注入落盘为 memory_injection 事件
        # （user_message 之后、渲染为 user 消息；不动 system[0]，prefix cache 无损）
        if a._pending_system_injections:
            for injection in a._pending_system_injections:
                a.append_memory_injection(injection)
            a._pending_system_injections = []
        a._repeat_guard.reset()

        if not a.is_sub_agent and user_message is not None:
            sq = a.build_side_query()
            if sq:
                a.start_wiki_prefetch(user_message, sq)

    async def _consume_wiki_prefetch(self) -> None:
        a = self._agent
        if a._wiki_prefetch is None or a._wiki_prefetch_consumed:
            return
        # 等待 prefetch 完成（最多 20 秒，embedding 冷启动可能较慢）
        if not a._wiki_prefetch.done():
            try:
                await asyncio.wait_for(asyncio.shield(a._wiki_prefetch), timeout=20.0)
            except asyncio.TimeoutError:
                # BC-8：超时也要释放闩锁（标记已消费），否则后续轮次永远无法重新召回
                print("[wiki_recall] timeout after 20s, skipping injection")
                a._wiki_prefetch_consumed = True
                return
        a._wiki_prefetch_consumed = True
        try:
            entries = a._wiki_prefetch.result()
            if entries:
                injection_text = format_wiki_for_injection(entries)
                injection_text = safe_utf8_text(injection_text)
                a.append_memory_injection(injection_text)
                for e in entries:
                    a._wiki_surfaced_at[e.rel_path] = a._turn_number
                print(f"[wiki_recall] injected {len(entries)} entries: {[e.rel_path for e in entries]}")
            else:
                print(f"[wiki_recall] no entries found")
        except Exception as e:
            print(f"[wiki_recall] error: {type(e).__name__}: {e}")

    def _update_token_stats(self, response: dict) -> None:
        """更新 token 统计并发布流式事件。"""
        a = self._agent
        a.last_api_call_time = time.time()

        usage = response.get("usage") or {}
        if not usage:
            return

        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0) or (input_tokens + output_tokens)
        if total_tokens <= 0:
            logging.warning("model usage returned zero tokens: model=%s", a.model)

        a.add_input_tokens(input_tokens)
        a.add_output_tokens(output_tokens)
        a.set_last_usage_tokens(input_tokens, total_tokens)

        # BC-21 修复：stats 事件对子代理/eval 会话不再全抑制——落盘并打 is_sub_agent 标记，
        # 子会话压缩/cache 行为可观测（评测臂诊断依赖），展示层按标记自行过滤
        cached_tokens = int(usage.get("cached_tokens") or 0)
        a.total_cached_tokens += cached_tokens
        asm = response.get("_assembly_metrics", {})
        a.session.append("stats", {
            "is_sub_agent": a.is_sub_agent,
            # per-call 语义：本次模型调用的 usage
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cache_hit_rate": round(cached_tokens / input_tokens, 3) if input_tokens else 0.0,
            # 累计语义：会话至今总量
            "total_input_tokens": a.total_input_tokens,
            "total_output_tokens": a.total_output_tokens,
            "total_cached_tokens": a.total_cached_tokens,
            "context_window": a.context_window,
            "effective_window": a.effective_window,
            "estimated_context_tokens": a.estimated_context_tokens,
            "system_chars": asm.get("system_chars", 0),
            "user_chars": asm.get("user_chars", 0),
            "assistant_chars": asm.get("assistant_chars", 0),
            "tool_result_chars": asm.get("tool_result_chars", 0),
            "tool_count": asm.get("tool_count", 0),
            "tool_result_by_name": asm.get("tool_result_by_name", {}),
            "system_base_chars": asm.get("system_base_chars", 0),
            "system_claude_md_chars": asm.get("system_claude_md_chars", 0),
            "system_agents_md_chars": asm.get("system_agents_md_chars", 0),
            "system_skills_chars": asm.get("system_skills_chars", 0),
            "system_wiki_chars": asm.get("system_wiki_chars", 0),
            "system_workspace_chars": asm.get("system_workspace_chars", 0),
            "is_plan_mode": asm.get("is_plan_mode", False),
            "plan_mode_chars": asm.get("plan_mode_chars", 0),
        })

    async def _finalize_text_response(self) -> None:
        """完成文本响应：刷新 markdown、打印成本。"""
        pass

    def _finalize_loop_guard_stop(self) -> None:
        a = self._agent
        reason = a._loop_guard_stop_reason or "tool loop guard"
        message = (
            f"⚠️ Loop guard stopped this turn: {reason}\n\n"
            "The previous tool strategy was not making sufficient progress. "
            "Review the latest tool results, choose a different decomposition or tool, "
            "or finish with the evidence already collected."
        )
        if a._turn_output_buffer is not None:
            a._turn_output_buffer.append(message)
        a.session.append("assistant_message", {
            "turn": a._current_turn,
            "step": a._current_step,
            "content": message,
        })

    async def _finalize_budget(self, stop_reason: str, reason_text: str, step_reason: str) -> None:
        """预算耗尽收敛应答（U5a：对齐 v2 runner/max-steps.ts + llm.ts 模式）。

        - CRITICAL 措辞 + 编号清单 + overrides ALL other instructions + 回复四要素
        - 保留工具定义 + tool_choice="none"（工具数组参与 prefix cache，摘掉会整体失效）
        """
        a = self._agent
        a._tool_budget_stop_reason = stop_reason
        instruction = (
            f"CRITICAL: {reason_text}. This message ABSOLUTELY OVERRIDES ALL OTHER "
            "INSTRUCTIONS, including any intent to continue working, call tools, or start new subtasks.\n"
            "1. Do NOT call any tools; you are no longer allowed to use them.\n"
            "2. Review the conversation and the evidence already collected.\n"
            "3. Reply now with exactly these four parts:\n"
            "   - Conclusion: the best supported final result or answer.\n"
            "   - Evidence: which subtasks are verified, and how (files, commands, outputs).\n"
            "   - Unfinished: which parts remain uncertain or incomplete.\n"
            "   - Suggestion: the single most useful next step."
        )
        a.session.append("step/start", {
            "turn": a._current_turn,
            "step": a._current_step + 1,
            "reason": step_reason,
        })
        a._current_step += 1
        a.append_user_message(instruction)

        response = await self.call_model_stream(tools_enabled=True, tool_choice="none")
        choice = response.get("choices", [{}])[0] if response.get("choices") else {}
        message = choice.get("message", {})
        assistant_event = a.session.append("assistant_message", {
            "turn": a._current_turn,
            "step": a._current_step,
            "thinking": message.get("thinking"),
            "content": message.get("content") or "",
            "tool_calls": None,
        })
        a.mark_last_usage_position(int(assistant_event.get("seq", -1)))
        self._update_token_stats(response)
        a.session.append("step/end", {
            "turn": a._current_turn,
            "step": a._current_step,
            "reason": step_reason,
        })

    async def _handle_tool_calls(self, tool_calls: list[dict],
                                 assistant_seq: int | None = None) -> bool:
        """处理工具调用：权限检查、执行、结果收集。

        assistant_seq: 承载这批 tool_calls 的 assistant 消息的 seq（_run_step
        捕获），一路透传到 execute_tool_call 供工具做事件记账。
        """

        print_info(f"[DEBUG] _handle_tool_calls: start, {len(tool_calls)} tools")
        a = self._agent
        oai_checked: list[dict] = []

        published_calls: list[dict] = []
        
        for tc in tool_calls:
            if a.abort_requested():
                a.mark_aborted()
                for pub_tc in published_calls:
                    cancel_result = "Action cancelled: user abort."
                    a.publish_tool_result_event(pub_tc["id"], pub_tc["fn"], cancel_result, "cancelled")
                    a.append_tool_message(pub_tc["id"], cancel_result, pub_tc["fn"])
                break

            if tc.get("type") != "function":
                continue

            fn_name = tc["function"]["name"]
            try:
                inp = json.loads(tc["function"]["arguments"])
            except Exception:
                inp = {}

            a.publish_tool_call_event(tc["id"], fn_name, inp)
            published_calls.append({"id": tc["id"], "fn": fn_name})

            perm = check_permission(
                fn_name,
                inp,
                a.permission_mode,
                plan_dir=str(a._plan_mode_manager.plan_dir) if a._plan_mode_manager and a._plan_mode_manager.plan_dir else None,
            )

            if perm["action"] == "deny":
                a.record_tool_outcome(fn_name, False)
                deny_result = f"Action denied: {perm.get('message', '')}"
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": deny_result})
                a.publish_tool_result_event(tc["id"], fn_name, deny_result, "denied")
                continue

            if perm["action"] == "confirm" and perm.get("message") and perm["message"] not in a.confirmed_paths:
                a.set_current_tool_name(fn_name)
                confirmed = await a.confirm_dangerous(perm["message"])
                a.set_current_tool_name(None)
                if not confirmed:
                    a.record_tool_outcome(fn_name, False)
                    user_deny_result = "User denied this action."
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": user_deny_result})
                    a.publish_tool_result_event(tc["id"], fn_name, user_deny_result, "denied")
                    continue
                a.add_confirmed_path(perm["message"])

            oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

        print_info(f"[DEBUG] _handle_tool_calls: calling _execute_tool_batches with {len(oai_checked)} tools")
        guard_stop = await self._execute_tool_batches(oai_checked, assistant_seq)
        print_info(f"[DEBUG] _handle_tool_calls: done, guard_stop={guard_stop}")
        return guard_stop

    async def _execute_tool_batches(self, oai_checked: list[dict],
                                    assistant_seq: int | None = None) -> bool:
        """执行工具批次：并发安全工具并行执行，其他顺序执行。返回是否触发 loop guard stop。

        assistant_seq 同时传给并发批与顺序批两条路径——两者共用同一个
        execute_tool_call 契约，只穿一条会留下一个静默的 None。
        """

        print_info(f"[DEBUG] _execute_tool_batches: start, {len(oai_checked)} tools")
        a = self._agent
        oai_batches: list[dict] = []
        
        for ct in oai_checked:
            safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
            if safe and oai_batches and oai_batches[-1]["concurrent"]:
                oai_batches[-1]["items"].append(ct)
            else:
                oai_batches.append({"concurrent": safe, "items": [ct]})

        print_info(f"[DEBUG] _execute_tool_batches: {len(oai_batches)} batches")
        oai_context_break = False
        guard_stop = False
        guard_reason: str | None = None
        try:
            for i, batch in enumerate(oai_batches):
                print_info(f"[DEBUG] _execute_tool_batches: batch {i}, concurrent={batch['concurrent']}, items={len(batch['items'])}")
                if oai_context_break or guard_stop or a.abort_requested():
                    aborted = a.abort_requested()
                    if aborted:
                        a.mark_aborted()
                    cancel_result = (
                        "Action cancelled: user abort."
                        if aborted
                        else f"Action cancelled: loop guard stopped turn ({guard_reason or 'tool loop'})."
                    )
                    cancel_status = "cancelled" if aborted else "error"
                    cancel_outcome = "cancelled" if aborted else "blocked"
                    for remaining_batch in oai_batches[i:]:
                        for ct in remaining_batch["items"]:
                            if ct["allowed"]:
                                a.publish_tool_result_event(
                                    ct["tc"]["id"],
                                    ct["fn"],
                                    cancel_result,
                                    cancel_status,
                                    outcome=cancel_outcome,
                                    metadata={"reason": guard_reason} if guard_reason else None,
                                )
                                a.append_tool_message(ct["tc"]["id"], cancel_result, ct["fn"])
                    break

                if batch["concurrent"]:
                    guard_stop, guard_reason = await self._execute_concurrent_batch(
                        batch["items"], assistant_seq)
                else:
                    oai_context_break, guard_stop, guard_reason = await self._execute_sequential_batch(
                        batch["items"], assistant_seq)
                if guard_stop:
                    a._loop_guard_stop_reason = guard_reason or "tool_loop"
        finally:
            self._tool_tracker.clear_pending_near_duplicates()

        return guard_stop

    def _publish_blocked_tool_result(self, ct: dict, decision) -> None:
        a = self._agent
        message = decision.message or "Loop guard blocked this repeated tool call."
        a.publish_tool_result_event(
            ct["tc"]["id"],
            ct["fn"],
            message,
            "error",
            outcome="blocked",
            metadata=decision.metadata,
        )
        a.append_tool_message(ct["tc"]["id"], message, ct["fn"])
        self._auto_mark_bad_case(
            signal_type="tool_loop_blocked",
            diagnosis={
                "tool": ct["fn"],
                "args": ct["inp"],
                "reason": decision.reason,
                "verdict": decision.verdict,
                **(decision.metadata or {}),
            },
            tool_name=ct["fn"],
        )

    def _apply_warning_result(self, ct: dict, res: str, warning_result: dict) -> tuple[str, bool, str | None]:
        a = self._agent
        if warning_result["warnings"]:
            res = res + "\n\n" + "\n".join(warning_result["warnings"])

        if warning_result["going_off_track"]:
            self._auto_mark_bad_case(
                signal_type="tool_repeat",
                diagnosis={"tool": ct["fn"], "args": ct["inp"], "reason": "警告后仍重复调用"},
                tool_name=ct["fn"],
            )

        if warning_result["cycle_bad_case"]:
            self._auto_mark_bad_case(
                signal_type="tool_cycle",
                diagnosis={"tool": ct["fn"], "args": ct["inp"], "reason": "警告后仍循环"},
                tool_name=ct["fn"],
            )

        stop_reason: str | None = None
        if warning_result["force_stop"]:
            stop_info = warning_result.get("force_stop_info") or {}
            signal_type = "tool_loop_blocked" if warning_result.get("blocked") else "force_stop"
            stop_reason = stop_info.get("reason") or warning_result.get("verdict") or "force_stop"
            force_stop_msg = (
                f"\n\n⚠️ 硬兜底：工具 {stop_info.get('tool', ct['fn'])} 触发 {stop_reason}，"
                f"count={stop_info.get('count')}，outcome={stop_info.get('outcome') or warning_result.get('outcome')}。"
                "本轮将停止，请检查任务分解、工具选择或已有证据。"
            )
            res = res + force_stop_msg
            self._auto_mark_bad_case(
                signal_type=signal_type,
                diagnosis={
                    "tool": stop_info.get("tool", ct["fn"]),
                    "count": stop_info.get("count"),
                    "args": stop_info.get("args", ct["inp"]),
                    "reason": stop_reason,
                    "guard": stop_info.get("guard"),
                },
                tool_name=stop_info.get("tool", ct["fn"]),
            )

        repeat_warning = a._repeat_guard.check(ct["fn"], ct["inp"])
        if repeat_warning:
            res = res + "\n\n" + repeat_warning

        return res, warning_result["force_stop"], stop_reason

    async def _execute_concurrent_batch(self, items: list[dict],
                                        assistant_seq: int | None = None) -> tuple[bool, str | None]:
        """并发执行工具批次。"""
        a = self._agent
        allowed_items: list[dict] = []
        guard_stop = False
        guard_reason: str | None = None

        for ct in items:
            decision = self._tool_tracker.precheck(ct["fn"], ct["inp"])
            if decision.action == "block":
                self._publish_blocked_tool_result(ct, decision)
                guard_stop = True
                guard_reason = guard_reason or decision.reason
            else:
                allowed_items.append(ct)

        if guard_stop:
            for ct in allowed_items:
                cancel_result = f"Action cancelled: loop guard stopped turn ({guard_reason or 'tool loop'})."
                a.publish_tool_result_event(ct["tc"]["id"], ct["fn"], cancel_result, "error", outcome="blocked", metadata={"reason": guard_reason})
                a.append_tool_message(ct["tc"]["id"], cancel_result, ct["fn"])
            return guard_stop, guard_reason

        async def _run_oai_safe(ct_item: dict) -> tuple[dict, Any, str, dict | None]:
            pre_snapshot = None
            if ct_item["fn"] in ("write_file", "edit_file"):
                pre_snapshot = capture_file_snapshot(ct_item["inp"].get("file_path", ""))
            
            result = await a.execute_tool_call(ct_item["fn"], ct_item["inp"],
                                               assistant_seq=assistant_seq)
            raw = safe_utf8_text(result.text)
            res = a.persist_large_result(ct_item["fn"], raw)
            
            post_snapshot = None
            if ct_item["fn"] in ("write_file", "edit_file") and pre_snapshot:
                post_snapshot = capture_file_snapshot(ct_item["inp"].get("file_path", ""))
            
            file_snapshot = None
            if pre_snapshot and post_snapshot:
                file_snapshot = {
                    "file_path": pre_snapshot["file_path"],
                    "is_new": pre_snapshot["is_new"],
                    "old_content": pre_snapshot["content"],
                    "new_content": post_snapshot["content"],
                }
            return ct_item, result, res, file_snapshot

        results = await asyncio.gather(*[_run_oai_safe(ct) for ct in allowed_items])
        for ct_item, result, res, file_snapshot in results:
            text_failure = a.looks_like_tool_failure(ct_item["fn"], result.text, res)
            success = result.status == "ok" and result.outcome == "success" and not text_failure
            outcome = result.outcome if result.outcome != "success" else ("error" if text_failure else "success")
            metadata = dict(result.metadata or {})
            metadata["text_failure"] = text_failure
            a.publish_tool_result_event(
                ct_item["tc"]["id"],
                ct_item["fn"],
                res,
                result.status,
                snapshot=file_snapshot,
                outcome=outcome,
                metadata=metadata,
            )
            a.record_tool_outcome(ct_item["fn"], success)
            
            warning_result = check_tool_warnings(
                self._tool_tracker,
                ct_item["fn"],
                ct_item["inp"],
                success=success,
                outcome=outcome,
                metadata=metadata,
            )
            res, force_stop, stop_reason = self._apply_warning_result(ct_item, res, warning_result)
            if force_stop:
                guard_stop = True
                guard_reason = guard_reason or stop_reason
            # outcome 与上面 publish_tool_result_event / check_tool_warnings 用的是
            # 同一个局部值：tool_result 是只推不持久化的流式事件，验收闸门事后只能
            # 从落盘的 tool_result_msg 取事实，所以这里必须一并落盘（逐字，不归一化）。
            a.append_tool_message(ct_item["tc"]["id"], res, ct_item["fn"], outcome)

        return guard_stop, guard_reason

    async def _execute_sequential_batch(self, items: list[dict],
                                        assistant_seq: int | None = None) -> tuple[bool, bool, str | None]:
        """顺序执行工具批次。返回 (是否触发上下文清理, 是否触发 loop guard, guard reason)。"""

        print_info(f"[DEBUG] _execute_sequential_batch: start, {len(items)} tools")
        a = self._agent
        context_break = False
        guard_stop = False
        guard_reason: str | None = None

        i = -1
        for i, ct in enumerate(items):
            fn_name = ct["fn"]
            print_info(f"[DEBUG] _execute_sequential_batch: tool {i+1}/{len(items)}: {fn_name}")
            if not ct["allowed"]:
                # 权限拒绝路径没有 ToolExecutionResult：ct["result"] 是 _handle_tool_calls
                # 预生成的拒绝文案，那条 publish_tool_result_event 也只给 status="denied"
                # 而不给 outcome。所以这里 outcome 落空串（参数默认值），不就地编一个
                # 词表值——被拒绝的调用本来就不可能是闸门要找的「成功的验收命令」。
                a.append_tool_message(ct["tc"]["id"], ct["result"], ct["fn"])
                continue

            decision = self._tool_tracker.precheck(fn_name, ct["inp"])
            if decision.action == "block":
                self._publish_blocked_tool_result(ct, decision)
                guard_stop = True
                guard_reason = guard_reason or decision.reason
                break

            context_break, tool_guard_stop, tool_guard_reason = await self._run_sequential_tool(
                ct, assistant_seq)
            if tool_guard_stop:
                guard_stop = True
                guard_reason = guard_reason or tool_guard_reason
            if guard_stop or context_break:
                break

        if guard_stop or context_break:
            self._cancel_remaining_tools(items, i + 1, context_break, guard_stop, guard_reason)

        return context_break, guard_stop, guard_reason

    async def _run_sequential_tool(self, ct: dict,
                                   assistant_seq: int | None = None) -> tuple[bool, bool, str | None]:
        """单工具顺序执行：前后快照 → 调用 → 大结果落盘 → 事件 → 失败判定 → 警告检查。

        返回 (context_break, guard_stop, guard_reason)；context_break 时提前返回
        （与原实现一致：不再走警告检查与 append_tool_message）。
        """
        import time

        a = self._agent
        fn_name = ct["fn"]
        t0 = time.time()
        print_info(f"[DEBUG] _execute_sequential_batch: calling execute_tool_call for {fn_name}")

        pre_snapshot = None
        if fn_name in ("write_file", "edit_file"):
            pre_snapshot = capture_file_snapshot(ct["inp"].get("file_path", ""))

        result = await a.execute_tool_call(ct["fn"], ct["inp"], assistant_seq=assistant_seq)
        print_info(f"[DEBUG] _execute_sequential_batch: execute_tool_call done for {fn_name}, took {time.time()-t0:.2f}s")
        raw = safe_utf8_text(result.text)
        res = a.persist_large_result(ct["fn"], raw)

        post_snapshot = None
        if fn_name in ("write_file", "edit_file") and pre_snapshot:
            post_snapshot = capture_file_snapshot(ct["inp"].get("file_path", ""))

        file_snapshot = None
        if pre_snapshot and post_snapshot:
            file_snapshot = {
                "file_path": pre_snapshot["file_path"],
                "is_new": pre_snapshot["is_new"],
                "old_content": pre_snapshot["content"],
                "new_content": post_snapshot["content"],
            }

        text_failure = a.looks_like_tool_failure(ct["fn"], result.text, res)
        success = result.status == "ok" and result.outcome == "success" and not text_failure
        outcome = result.outcome if result.outcome != "success" else ("error" if text_failure else "success")
        metadata = dict(result.metadata or {})
        metadata["text_failure"] = text_failure
        a.publish_tool_result_event(
            ct["tc"]["id"],
            ct["fn"],
            res,
            result.status,
            snapshot=file_snapshot,
            outcome=outcome,
            metadata=metadata,
        )
        a.record_tool_outcome(ct["fn"], success)
        print_info(f"[DEBUG] _execute_sequential_batch: tool {fn_name} completed")

        if a.context_cleared:
            a.clear_context_flag()
            a.append_user_message(res)
            return True, False, None

        warning_result = check_tool_warnings(
            self._tool_tracker,
            ct["fn"],
            ct["inp"],
            success=success,
            outcome=outcome,
            metadata=metadata,
        )
        res, force_stop, stop_reason = self._apply_warning_result(ct, res, warning_result)

        # 同并发路径：outcome 逐字落盘，验收闸门的事实来源（见 _execute_concurrent_batch）
        a.append_tool_message(ct["tc"]["id"], res, ct["fn"], outcome)
        return False, bool(force_stop), (stop_reason if force_stop else None)

    def _cancel_remaining_tools(self, items: list[dict], from_index: int,
                                context_break: bool, guard_stop: bool,
                                guard_reason: str | None) -> None:
        """guard/清理中断后，为剩余工具补发取消结果（保持 tool_call/tool_result 配对完整）。"""
        a = self._agent
        for remaining in items[from_index:]:
            if remaining["allowed"]:
                cancel_result = (
                    "Action cancelled: context cleared."
                    if context_break
                    else f"Action cancelled: loop guard stopped turn ({guard_reason or 'tool loop'})."
                )
                a.publish_tool_result_event(
                    remaining["tc"]["id"],
                    remaining["fn"],
                    cancel_result,
                    "error" if guard_stop else "cancelled",
                    outcome="blocked" if guard_stop else "cancelled",
                    metadata={"reason": guard_reason} if guard_reason else None,
                )
                a.append_tool_message(remaining["tc"]["id"], cancel_result, remaining["fn"])

    async def call_model_stream(self, *, tools_enabled: bool = True, tool_choice: str | None = None) -> dict:
        """流式模型调用——委托 ModelCaller（测试 monkeypatch 缝，run() 必经 self 调用）。"""
        return await self._model_caller.call(tools_enabled=tools_enabled, tool_choice=tool_choice)
