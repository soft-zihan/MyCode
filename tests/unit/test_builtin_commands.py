"""U5b-#11/#12：explore 双面文案 + 内置命令资产（/initialize、/review）。"""
from pathlib import Path

from agents.core.workspace import workspace_scope


def _write_skill(ws: Path, name: str) -> None:
    d = ws / ".mycode" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\ndescription: test skill {name}\n---\nBody.\n")


class TestBuiltinCommandAssets:
    def test_builtin_commands_discovered(self, tmp_path):
        import agents.skills.skills as skills_mod

        skills_mod.reset_skill_cache()
        try:
            with workspace_scope(tmp_path):
                by_name = {s.name: s for s in skills_mod.discover_skills()}
            for cmd in ("initialize", "review"):
                assert cmd in by_name, f"内置命令 {cmd} 未被发现"
                assert by_name[cmd].source == "builtin"
                assert by_name[cmd].user_invocable
        finally:
            skills_mod.reset_skill_cache()

    def test_builtin_prompt_markers_and_arguments(self, tmp_path):
        import agents.skills.skills as skills_mod

        skills_mod.reset_skill_cache()
        try:
            with workspace_scope(tmp_path):
                skills = {s.name: s for s in skills_mod.discover_skills()}
                init_prompt = skills_mod.resolve_skill_prompt(skills["initialize"], "")
                review_prompt = skills_mod.resolve_skill_prompt(skills["review"], "abc123")
        finally:
            skills_mod.reset_skill_cache()
        # v2 initialize.txt 标志性纪律条款（MyCode 适配：ask_user）
        assert "Would an agent likely miss this without help?" in init_prompt
        assert "Prefer executable sources of truth over prose" in init_prompt
        assert "ask_user" in init_prompt
        assert "opencode.json" not in init_prompt
        # v2 review.txt 标志性纪律条款 + $ARGUMENTS 替换
        assert "Diffs alone are not enough." in review_prompt
        assert "AVOID flattery" in review_prompt
        assert "abc123" in review_prompt
        assert "$ARGUMENTS" not in review_prompt.split("## Determining")[0]

    def test_project_skill_overrides_builtin(self, tmp_path):
        import agents.skills.skills as skills_mod

        _write_skill(tmp_path, "review")
        skills_mod.reset_skill_cache()
        try:
            with workspace_scope(tmp_path):
                by_name = {s.name: s for s in skills_mod.discover_skills()}
            assert by_name["review"].source == "project"
            assert by_name["initialize"].source == "builtin"
        finally:
            skills_mod.reset_skill_cache()

    def test_builtin_commands_in_system_prompt_skill_section(self, tmp_path):
        import agents.skills.skills as skills_mod

        skills_mod.reset_skill_cache()
        try:
            with workspace_scope(tmp_path):
                section = skills_mod.build_skill_descriptions()
        finally:
            skills_mod.reset_skill_cache()
        assert "/initialize" in section
        assert "/review" in section


class TestExploreDualFace:
    def test_description_faces_parent_with_thoroughness_protocol(self):
        from agents.core.subagent import get_available_agent_types

        explore = next(t for t in get_available_agent_types() if t["name"] == "explore")
        desc = explore["description"]
        # 写给父代理：路由说明 + 三档协议
        assert "thoroughness" in desc
        for level in ('"quick"', '"medium"', '"very thorough"'):
            assert level in desc

    def test_system_faces_child_with_thoroughness_adaptation(self):
        from agents.core.subagent import _load_subagent_prompt

        prompt = _load_subagent_prompt("explore")
        # 写给子代理自己：按档位调整策略（不含父代理路由文案）
        assert "彻底程度" in prompt
        assert "very thorough" in prompt
        assert "Fast agent specialized for exploring codebases" not in prompt


class TestReviewerDiscipline:
    def test_reviewer_prompt_has_v2_discipline(self):
        from agents.core.subagent import _load_subagent_prompt

        prompt = _load_subagent_prompt("reviewer")
        assert "Diffs alone are not enough." in prompt
        assert "Be certain." in prompt
        assert "反谄媚" in prompt
        # 严格 JSON 输出契约保持
        assert '"passed"' in prompt and '"fix_list"' in prompt
