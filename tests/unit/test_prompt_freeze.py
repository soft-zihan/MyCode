"""U5a 真冻结：system prompt 会话内冻结（所有模式），plan 状态走尾部 ephemeral 通道。

回归保护：
- plan 进出不改主 system prompt（旧实现拼接 plan 提示词 → prefix cache 失效
  + surface generation bump 触发消息全量重派生）。
- 尾部通道每请求对账重注入（v2 plugin/plan.ts 模式）。
- TestCacheNeutrality：spec §D5/§十二 的靶心——进出 plan 模式对 prefix cache
  **完全中性**（工具数组逐字节相同 + plan 提示词只住尾部通道）。
"""
from __future__ import annotations

import json

from agents.agent import Agent


def _make_agent(**kwargs):
    defaults = dict(model="deepseek-chat", api_base="https://example.com/v1", api_key="sk-test")
    defaults.update(kwargs)
    return Agent(**defaults)


class TestSystemPromptFreeze:

    def test_frozen_across_refresh(self):
        agent = _make_agent()
        p1 = agent.session.system_prompt
        assert p1
        for _ in range(3):
            agent._refresh_runtime_system_prompt()
        assert agent.session.system_prompt is p1

    def test_plan_enter_exit_does_not_mutate_system_prompt(self):
        agent = _make_agent()
        p1 = agent.session.system_prompt
        g1 = agent.session._surface_generation

        agent._enter_plan_mode_internal()
        assert agent.permission_mode == "plan"
        assert agent.session.system_prompt is p1
        tails = agent.build_tail_system_messages()
        assert any("Plan Mode Active" in t for t in tails)

        agent._exit_plan_mode_internal()
        assert agent.session.system_prompt is p1
        assert not any("Plan Mode Active" in t for t in agent.build_tail_system_messages())
        # 模式切换不再触发消息全量重派生
        assert agent.session._surface_generation == g1

    def test_plan_mode_init_not_in_system_prompt(self):
        agent = _make_agent(permission_mode="plan")
        assert "Plan Mode Active" not in (agent.session.system_prompt or "")
        assert any("Plan Mode Active" in t for t in agent.build_tail_system_messages())

    def test_fold_guidance_in_tails(self):
        agent = _make_agent()
        tails = agent.build_tail_system_messages()
        assert len(tails) == 1
        assert "Runtime Fold Guidance" in tails[0]

    def test_custom_prompt_no_tails(self):
        agent = _make_agent(custom_system_prompt="custom")
        assert agent.build_tail_system_messages() == []
        assert agent.session.system_prompt == "custom"

    def test_plan_tail_before_fold_guidance(self):
        agent = _make_agent(permission_mode="plan")
        tails = agent.build_tail_system_messages()
        assert len(tails) == 2
        assert "Plan Mode Active" in tails[0]
        assert "Runtime Fold Guidance" in tails[1]


class TestCacheNeutrality:
    """spec §D5 / §十二：进出 plan 模式对 prefix cache **完全中性**。

    中性由两半组成，两半各一条测试：
    - 工具数组**不按权限模式过滤**（§D5 用户裁定 B）。硬约束由权限层执行
      （permissions._check_plan_mode），提示词只带一份负面清单。
    - plan 提示词只在**尾部 ephemeral 通道**里，绝不进 system[0]。

    这两条都不是在测「功能对不对」，而是在测「一个很容易被好心加回来的性能退化
    没有发生」。prefix cache 的前缀 = system[0] + 工具数组 + 历史，本项目实测约
    10k tokens；动其中任何一半 = 每次进/出 plan 模式各一次全前缀失效。
    """

    @staticmethod
    def _tools_payload(agent) -> str:
        """走**真实的请求组装路径**取工具数组的字节表示。

        刻意不在测试里自己拼 get_active_tool_definitions + _to_openai_tools：
        自己拼的那份不会因为「将来有人在 _assemble_request 里按 permission_mode
        过滤」而变红——那正是本测试唯一要防的改动。_assemble_request 是同步的、
        不发网络请求，唯一的依赖是 span.update(...)。
        """
        from agents.core.model_caller import ModelCaller

        class _Span:
            def update(self, **_kwargs):
                pass

        params, _raw_messages, _metrics = ModelCaller(agent)._assemble_request(_Span(), True)
        return json.dumps(params.get("tools"), ensure_ascii=False)

    def test_tool_array_is_byte_identical_across_permission_modes(self):
        """§D5 靶心：发给模型的工具数组在 plan 模式与非 plan 模式下**逐字节相同**。

        「把模型不该看到的工具藏起来」看起来太像一个显然的改进了——权限层已经拒绝
        它们，何必让模型白调一次？将来一定会有人加上 `PLAN_MODE_HIDDEN_TOOLS` 并在
        组装处过滤。这条测试存在的唯一目的就是让那次「好心」变红：过滤工具数组 =
        每次进/出 plan 模式各一次全前缀失效，而收益只是省一次被拒的来回。
        裁定与被否方案见 spec §D5 / §十二 的那张对照表。

        断言的是**整个 JSON 串相等**（含顺序、描述文本、schema），不是工具名集合
        相等：改描述、改顺序同样破坏缓存命中，同样该红。
        """
        agent = _make_agent()
        before = self._tools_payload(agent)

        agent.set_permission_mode("plan")
        assert agent.permission_mode == "plan"
        after = self._tools_payload(agent)

        assert after == before

        # 防空转：两边都是空数组也能让上面那条相等成立，测试就成了永真式。
        # 点名 task_list ——它正是 plan 模式下被权限层拒绝的那个工具，
        # 「藏起来最合理」的候选，所以它必须在两边都出现。
        names = [t["function"]["name"] for t in json.loads(after)]
        assert "task_list" in names
        assert len(names) > 5

    def test_plan_mode_prompt_lives_only_in_the_tail_channel(self):
        """plan 提示词的唯一去处是尾部 ephemeral 通道，system[0] 一个字节都不动。

        走 `set_permission_mode("plan")`（UI/PUT 的真实入口）而不是直接调
        `_enter_plan_mode_internal`：将来若有人在**这个**入口上挂一次 prompt 重建，
        上面那条只调内部入口的测试抓不到。
        断言用 `build_plan_mode_prompt()` 的**全文**做包含判断，而不是只找
        "Plan Mode Active" 这个标题：把提示词搬进 system[0] 时顺手改个标题，
        就绕过了只认标题的断言。
        """
        agent = _make_agent()
        system_before = agent.session.system_prompt

        agent.set_permission_mode("plan")

        plan_prompt = agent._plan_mode_manager.build_plan_mode_prompt().strip()
        assert plan_prompt                                   # 提示词非空，否则包含判断无意义
        assert agent.session.system_prompt is system_before   # 同一个对象：根本没重建
        assert plan_prompt not in (agent.session.system_prompt or "")
        tails = agent.build_tail_system_messages()
        assert plan_prompt in tails[0]                        # 它在尾部，且是尾部第一条


class TestTitlePromptAsset:
    """U5a-3：title 提示词从文件加载（单一来源），含 v2 三段结构。"""

    def test_title_prompt_loaded_from_file(self):
        from agents.core.agent_mode import BUILTIN_HIDDEN_AGENTS

        prompt = BUILTIN_HIDDEN_AGENTS["side_query_title"].system_prompt
        assert "<task>" in prompt
        assert "<rules>" in prompt
        assert "<examples>" in prompt
        # 防抱怨条款 + 语言跟随
        assert "Never complain" in prompt
        assert "match the language" in prompt
        # 10 条 few-shot
        assert prompt.count("<example>") == 10

    def test_title_prompt_in_registry(self):
        from agents.core.prompt_registry import list_all_prompts

        names = {info.name for info in list_all_prompts()}
        assert any("title" in n for n in names)
