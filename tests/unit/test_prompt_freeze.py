"""U5a 真冻结：system prompt 会话内冻结（所有模式），plan 状态走尾部 ephemeral 通道。

回归保护：
- plan 进出不改主 system prompt（旧实现拼接 plan 提示词 → prefix cache 失效
  + surface generation bump 触发消息全量重派生）。
- 尾部通道每请求对账重注入（v2 plugin/plan.ts 模式）。
"""
from __future__ import annotations

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
