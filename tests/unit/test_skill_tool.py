"""U9/BC-28：skill 工具恢复——注册表定义、发现内置技能、运行时变量替换、dispatcher 路由。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_skill_tool_registered_in_definitions():
    from agents.tools.registry import get_active_tool_definitions

    names = {t["name"] for t in get_active_tool_definitions()}
    assert "skill" in names, "system prompt 广告了 skill 工具，registry 必须有定义（BC-28）"


def test_builtin_visualization_skill_discovered():
    from agents.skills.skills import discover_skills

    skills = {s.name: s for s in discover_skills()}
    viz = skills.get("visualization")
    assert viz is not None, "agents/prompts/skills/visualization 必须被发现"
    assert viz.source == "builtin"
    assert viz.user_invocable is False, "自动调用型：模型按 when_to_use 主动调 skill 工具"
    assert viz.when_to_use


def test_execute_skill_substitutes_runtime_vars(tmp_path):
    from agents.skills.skills import execute_skill

    substitutions = {
        "${SESSION_ID}": "sess-42",
        "${ARTIFACTS_DIR}": str(tmp_path / "artifacts" / "sess-42"),
    }
    result = execute_skill("visualization", "", substitutions)
    assert result is not None
    prompt = result["prompt"]
    assert "sess-42" in prompt
    assert str(tmp_path / "artifacts" / "sess-42") in prompt
    assert "${ARTIFACTS_DIR}" not in prompt
    assert "${SESSION_ID}" not in prompt
    # ${CLAUDE_SKILL_DIR} 指向内置技能目录（脚本模板可执行）
    scripts_dir = Path(prompt.split("脚本目录：`")[1].split("`")[0])
    assert (scripts_dir / "matplotlib_chart.py").is_file()
    assert "scripts/matplotlib_chart.py" in prompt or "matplotlib_chart.py" in prompt


def test_execute_skill_unknown_returns_none():
    from agents.skills.skills import execute_skill

    assert execute_skill("no-such-skill-xyz", "") is None


@pytest.mark.asyncio
async def test_dispatcher_skill_tool_inline(tmp_path):
    from agents.tools.dispatcher import ToolDispatcher

    fake_agent = SimpleNamespace(
        session_id="sess-42",
        session=SimpleNamespace(projections={"cwd": str(tmp_path)}),
    )
    dispatcher = ToolDispatcher(agent_ref=fake_agent)

    result = await dispatcher._execute_skill_tool({"skill_name": "visualization"})
    assert isinstance(result, str)
    expected_dir = str(tmp_path / ".mycode" / "artifacts" / "sess-42")
    assert expected_dir in result
    assert "sess-42" in result


@pytest.mark.asyncio
async def test_dispatcher_skill_tool_unknown_lists_available(tmp_path):
    from agents.tools.dispatcher import ToolDispatcher

    fake_agent = SimpleNamespace(
        session_id="s", session=SimpleNamespace(projections={"cwd": str(tmp_path)})
    )
    dispatcher = ToolDispatcher(agent_ref=fake_agent)

    result = await dispatcher._execute_skill_tool({"skill_name": "no-such-skill"})
    assert isinstance(result, str)
    assert result.startswith("Error: unknown skill")
    assert "visualization" in result


@pytest.mark.asyncio
async def test_dispatcher_skill_tool_requires_name(tmp_path):
    from agents.tools.dispatcher import ToolDispatcher

    fake_agent = SimpleNamespace(
        session_id="s", session=SimpleNamespace(projections={"cwd": str(tmp_path)})
    )
    dispatcher = ToolDispatcher(agent_ref=fake_agent)
    result = await dispatcher._execute_skill_tool({})
    assert result == "Error: skill_name is required."
