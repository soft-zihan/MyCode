"""Phase 1 测试：全 Harness 进化 + 回滚 + retention 检查。

测试覆盖：
- refinement.py 数据结构序列化/反序列化
- harness_evolution.py 应用 proposal
- harness_rollback.py 回滚逻辑
- online_skill_eval.py retention 检查
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ── refinement.py 测试 ─────────────────────────────────────────────────────────


class TestRefinementDataStructures:
    """测试 refinement 数据结构的序列化和反序列化。"""

    def test_refinement_kind_enum(self):
        from agents.refinement import RefinementKind

        assert RefinementKind.PROMPT.value == "prompt"
        assert RefinementKind.MEMORY.value == "memory"
        assert RefinementKind.SKILL.value == "skill"
        assert RefinementKind.SUBAGENT.value == "subagent"

    def test_refinement_action_enum(self):
        from agents.refinement import RefinementAction

        assert RefinementAction.CREATE.value == "create"
        assert RefinementAction.UPDATE.value == "update"
        assert RefinementAction.DELETE.value == "delete"

    def test_refinement_edit_to_dict(self):
        from agents.refinement import RefinementEdit, RefinementAction, RefinementKind

        edit = RefinementEdit(
            action=RefinementAction.CREATE,
            kind=RefinementKind.SKILL,
            title="test-skill",
            content="Test content",
            reason="Test reason",
        )
        d = edit.to_dict()

        assert d["action"] == "create"
        assert d["kind"] == "skill"
        assert d["title"] == "test-skill"
        assert d["content"] == "Test content"
        assert d["reason"] == "Test reason"

    def test_refinement_edit_from_dict(self):
        from agents.refinement import RefinementEdit

        d = {
            "action": "update",
            "kind": "memory",
            "target_id": "memory-test",
            "content": "Updated content",
        }
        edit = RefinementEdit.from_dict(d)

        assert edit.action.value == "update"
        assert edit.kind.value == "memory"
        assert edit.target_id == "memory-test"
        assert edit.content == "Updated content"

    def test_refinement_proposal_roundtrip(self):
        from agents.refinement import RefinementProposal, RefinementEdit, RefinementAction, RefinementKind

        proposal = RefinementProposal(
            summary="Test proposal",
            rationale="Test rationale",
            edits=[
                RefinementEdit(
                    action=RefinementAction.CREATE,
                    kind=RefinementKind.SKILL,
                    title="new-skill",
                    content="Skill content",
                ),
            ],
            expected_outcome="New skill available",
        )

        d = proposal.to_dict()
        restored = RefinementProposal.from_dict(d)

        assert restored.summary == proposal.summary
        assert restored.rationale == proposal.rationale
        assert len(restored.edits) == 1
        assert restored.edits[0].title == "new-skill"

    def test_harness_state_basic(self):
        from agents.refinement import HarnessState, HarnessEntry, RefinementKind, HarnessScope

        state = HarnessState()

        # 初始状态应该有四个空 kind
        assert len(state.entries) == 4
        for kind in RefinementKind:
            assert kind.value in state.entries

        # 添加 entry
        entry = HarnessEntry(
            id="skill-test",
            kind=RefinementKind.SKILL,
            title="Test Skill",
            content="Content",
            path="/path/to/skill.md",
        )
        state.set_entry(entry)

        # 检索 entry
        retrieved = state.get_entry(RefinementKind.SKILL, "skill-test")
        assert retrieved is not None
        assert retrieved.title == "Test Skill"

        # 列出 entries
        all_entries = state.list_entries()
        assert len(all_entries) == 1

        skill_entries = state.list_entries(RefinementKind.SKILL)
        assert len(skill_entries) == 1

    def test_harness_state_roundtrip(self):
        from agents.refinement import HarnessState, HarnessEntry, RefinementKind

        state = HarnessState()
        entry = HarnessEntry(
            id="prompt-test",
            kind=RefinementKind.PROMPT,
            title="Test Prompt",
            content="Prompt content",
            path="/path/to/prompt.md",
        )
        state.set_entry(entry)

        d = state.to_dict()
        restored = HarnessState.from_dict(d)

        retrieved = restored.get_entry(RefinementKind.PROMPT, "prompt-test")
        assert retrieved is not None
        assert retrieved.title == "Test Prompt"


# ── harness_evolution.py 测试 ──────────────────────────────────────────────────


class TestHarnessEvolution:
    """测试 harness 进化执行。"""

    @pytest.fixture
    def temp_project(self, tmp_path):
        """创建临时项目目录。"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".bear").mkdir()
        (project_dir / ".bear" / "skills").mkdir()
        (project_dir / ".bear" / "prompts").mkdir()
        (project_dir / ".bear" / "harness-state").mkdir()

        with patch("agents.refinement.Path.cwd", return_value=project_dir):
            with patch("agents.harness_evolution.Path.cwd", return_value=project_dir):
                yield project_dir

    def test_apply_create_edit(self, temp_project):
        from agents.harness_evolution import apply_edit
        from agents.refinement import HarnessState, RefinementEdit, RefinementAction, RefinementKind

        state = HarnessState()
        edit = RefinementEdit(
            action=RefinementAction.CREATE,
            kind=RefinementKind.PROMPT,
            title="test-prompt",
            content="Test prompt content",
        )

        result = apply_edit(edit, state)

        assert result["applied"] is True
        assert result["error"] is None
        assert result["after"]["file_content"] == "Test prompt content"

        # 验证文件已创建
        prompt_path = temp_project / ".bear" / "prompts" / "test-prompt.md"
        assert prompt_path.is_file()
        assert prompt_path.read_text() == "Test prompt content"

    def test_apply_update_edit(self, temp_project):
        from agents.harness_evolution import apply_edit
        from agents.refinement import HarnessState, HarnessEntry, RefinementEdit, RefinementAction, RefinementKind

        state = HarnessState()
        # 先创建
        entry = HarnessEntry(
            id="prompt-test",
            kind=RefinementKind.PROMPT,
            title="Original",
            content="Original content",
            path=str(temp_project / ".bear" / "prompts" / "test.md"),
        )
        state.set_entry(entry)
        entry.path_obj = Path(entry.path)
        entry.path_obj.parent.mkdir(parents=True, exist_ok=True)
        entry.path_obj.write_text("Original content")

        # 再更新
        edit = RefinementEdit(
            action=RefinementAction.UPDATE,
            kind=RefinementKind.PROMPT,
            target_id="prompt-test",
            content="Updated content",
        )

        result = apply_edit(edit, state)

        assert result["applied"] is True
        assert result["before"]["file_content"] == "Original content"
        assert result["after"]["file_content"] == "Updated content"

    def test_apply_delete_edit(self, temp_project):
        from agents.harness_evolution import apply_edit
        from agents.refinement import HarnessState, HarnessEntry, RefinementEdit, RefinementAction, RefinementKind

        state = HarnessState()
        # 先创建
        entry = HarnessEntry(
            id="prompt-delete",
            kind=RefinementKind.PROMPT,
            title="To Delete",
            content="Content",
            path=str(temp_project / ".bear" / "prompts" / "delete.md"),
        )
        state.set_entry(entry)
        file_path = Path(entry.path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("Content")

        # 删除
        edit = RefinementEdit(
            action=RefinementAction.DELETE,
            kind=RefinementKind.PROMPT,
            target_id="prompt-delete",
        )

        result = apply_edit(edit, state)

        assert result["applied"] is True
        assert result["after"]["entry"] is None
        assert not file_path.is_file()

    def test_apply_proposal(self, temp_project):
        from agents.harness_evolution import apply_proposal
        from agents.refinement import RefinementProposal, RefinementEdit, RefinementAction, RefinementKind

        proposal = RefinementProposal(
            summary="Create test prompt",
            rationale="Testing",
            edits=[
                RefinementEdit(
                    action=RefinementAction.CREATE,
                    kind=RefinementKind.PROMPT,
                    title="auto-prompt",
                    content="Auto-generated prompt",
                ),
            ],
            expected_outcome="Prompt available",
        )

        event = apply_proposal(proposal, trigger="test")

        assert event.trigger == "test"
        assert len(event.applied_edits) == 1
        assert event.applied_edits[0]["applied"] is True


# ── harness_rollback.py 测试 ───────────────────────────────────────────────────


class TestHarnessRollback:
    """测试 harness 回滚逻辑。"""

    @pytest.fixture
    def temp_project(self, tmp_path):
        """创建临时项目目录。"""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / ".bear").mkdir()
        (project_dir / ".bear" / "skills").mkdir()
        (project_dir / ".bear" / "skill-evolution").mkdir()
        (project_dir / ".bear" / "skill-evolution" / "history").mkdir()
        (project_dir / ".bear" / "harness-state").mkdir()

        with patch("agents.refinement.Path.cwd", return_value=project_dir):
            with patch("agents.harness_evolution.Path.cwd", return_value=project_dir):
                with patch("agents.harness_rollback.Path.cwd", return_value=project_dir):
                    yield project_dir

    def test_rollback_nonexistent_entry(self, temp_project):
        from agents.harness_rollback import rollback_entry
        from agents.refinement import RefinementKind

        result = rollback_entry(RefinementKind.SKILL, "nonexistent")

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_rollback_skill_from_evolution_history(self, temp_project):
        from agents.harness_rollback import rollback_skill

        # 创建 skill 文件
        skill_dir = temp_project / ".bear" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("Current version")

        # 创建 history
        history_dir = temp_project / ".bear" / "skill-evolution" / "history"
        history_file = history_dir / "test-skill.jsonl"
        history_file.write_text(json.dumps({
            "version": 1,
            "content": "Version 1 content",
            "time": "2024-01-01T00:00:00Z",
        }) + "\n")
        history_file.write_text(json.dumps({
            "version": 2,
            "content": "Version 2 content",
            "time": "2024-01-02T00:00:00Z",
        }) + "\n", append=True) if False else None
        # 追加第二条
        with open(history_file, "a") as f:
            f.write(json.dumps({
                "version": 2,
                "content": "Version 2 content",
                "time": "2024-01-02T00:00:00Z",
            }) + "\n")

        # 回滚到上一版本
        result = rollback_skill("test-skill")

        assert result["success"] is True
        assert result["source"] == "skill_evolution_history"
        assert skill_file.read_text() == "Version 1 content"


# ── online_skill_eval.py retention 检查测试 ────────────────────────────────────


class TestRetentionCheck:
    """测试 historical retention 检查。"""

    def test_retention_pass_improvement(self):
        from agents.online_skill_eval import _check_historical_retention

        result = _check_historical_retention(
            candidate_score=0.9,
            champion_score=0.8,
            candidate_hard=0,
            champion_hard=1,
        )

        assert result["passed"] is True
        assert abs(result["delta"] - 0.1) < 1e-9

    def test_retention_pass_equal(self):
        from agents.online_skill_eval import _check_historical_retention

        result = _check_historical_retention(
            candidate_score=0.8,
            champion_score=0.8,
            candidate_hard=0,
            champion_hard=0,
        )

        assert result["passed"] is True
        assert result["delta"] == 0.0

    def test_retention_fail_regression(self):
        from agents.online_skill_eval import _check_historical_retention

        result = _check_historical_retention(
            candidate_score=0.7,
            champion_score=0.8,
            candidate_hard=0,
            champion_hard=0,
        )

        assert result["passed"] is False
        assert "regresses" in result["reason"]

    def test_retention_fail_new_hard_failure(self):
        from agents.online_skill_eval import _check_historical_retention

        result = _check_historical_retention(
            candidate_score=0.85,
            champion_score=0.8,
            candidate_hard=2,
            champion_hard=0,
        )

        assert result["passed"] is False
        assert "hard failure" in result["reason"]

    def test_promotion_decision_with_retention(self):
        from agents.online_skill_eval import _promotion_decision

        # 候选退化，应被拒绝
        result = _promotion_decision(
            status="healthy",
            candidate={"average_score": 0.7, "hard_failures": 0},
            champion={"summary": {"average_score": 0.8, "hard_failures": 0}},
        )

        assert result["promoted"] is False
        assert "retention" in result.get("reason", "").lower() or "regress" in result.get("reason", "").lower()


# ── 运行测试 ───────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
