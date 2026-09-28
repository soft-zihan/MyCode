"""BC-23：token breakdown 读渲染期缓存——观测埋点不再重建 system prompt 重资产。"""
from agents.core.model_caller import ModelCaller


class FakeAgent:
    def __init__(self, breakdown):
        self._system_prompt_breakdown = breakdown
        self.permission_mode = "default"
        self._plan_mode_manager = None
        self._tool_result_chars = {}


MSGS = [{"role": "system", "content": "x" * 1000}, {"role": "user", "content": "hi"}]


def test_breakdown_matches_rendered_segments():
    from agents.core.prompt import (
        build_system_prompt, build_system_prompt_with_breakdown,
        load_claude_md, load_agents_md, build_skill_descriptions,
        build_wiki_prompt_section, build_workspace_structure,
    )
    prompt, bd = build_system_prompt_with_breakdown()
    assert prompt == build_system_prompt()
    assert bd == {
        "claude_md_chars": len(load_claude_md()),
        "agents_md_chars": len(load_agents_md()),
        "skills_chars": len(build_skill_descriptions()),
        "wiki_chars": len(build_wiki_prompt_section()),
        "workspace_chars": len(build_workspace_structure()),
    }


def test_compute_reads_cache_without_rebuild(monkeypatch):
    import agents.core.prompt as prompt_mod

    def boom(*a, **k):
        raise AssertionError("breakdown 不得重建 system prompt 分段")

    for name in ("load_claude_md", "load_agents_md", "build_skill_descriptions",
                 "build_wiki_prompt_section", "build_workspace_structure"):
        monkeypatch.setattr(prompt_mod, name, boom)

    mc = ModelCaller(FakeAgent({"claude_md_chars": 100, "agents_md_chars": 50,
                                "skills_chars": 200, "wiki_chars": 300,
                                "workspace_chars": 150}))
    out = mc._compute_token_breakdown(MSGS)
    assert out["system_chars"] == 1000
    assert out["system_base_chars"] == 1000 - 800
    assert out["system_claude_md_chars"] == 100
    assert out["system_wiki_chars"] == 300


def test_custom_prompt_empty_breakdown_base_is_all():
    mc = ModelCaller(FakeAgent({}))
    out = mc._compute_token_breakdown(MSGS)
    assert out["system_base_chars"] == 1000
    assert out["system_claude_md_chars"] == 0
