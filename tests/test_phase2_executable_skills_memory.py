"""Phase 2 测试：可执行 Skills + Memory 进化联动 + 进化评测指标。

测试覆盖：
- executable_skills.py 检测、安装、注入
- memory.py usage tracking + maintenance
- online_skill_eval.py 进化评测指标（replay、规则编译、champion）
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── executable_skills.py 测试 ─────────────────────────────────────────────────


class TestExecutableSkills:
    """测试可执行 Skills 加载器。"""

    def test_is_executable_skill_false_no_pyproject(self, tmp_path):
        from agents.executable_skills import is_executable_skill

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\nContent")

        assert is_executable_skill(skill_dir) is False

    def test_is_executable_skill_false_no_init(self, tmp_path):
        from agents.executable_skills import is_executable_skill

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\nContent")
        (skill_dir / "pyproject.toml").write_text("[project]\nname = 'my-skill'")

        assert is_executable_skill(skill_dir) is False

    def test_is_executable_skill_true(self, tmp_path):
        from agents.executable_skills import is_executable_skill

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\nContent")
        (skill_dir / "pyproject.toml").write_text("[project]\nname = 'my-skill'")
        src_dir = skill_dir / "src" / "my_skill"
        src_dir.mkdir(parents=True)
        (src_dir / "__init__.py").write_text("def run(): pass")

        assert is_executable_skill(skill_dir) is True

    def test_get_import_name(self):
        from agents.executable_skills import get_import_name

        assert get_import_name("my-skill") == "my_skill"
        assert get_import_name("web-search") == "web_search"
        assert get_import_name("simple") == "simple"

    def test_executable_skill_loader_basic(self, tmp_path):
        from agents.executable_skills import ExecutableSkillLoader

        loader = ExecutableSkillLoader()

        # 创建可执行 skill
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: test-skill\n---\nContent")
        (skill_dir / "pyproject.toml").write_text("[project]\nname = 'test-skill'")
        src_dir = skill_dir / "src" / "test_skill"
        src_dir.mkdir(parents=True)
        (src_dir / "__init__.py").write_text("def run(): return 'hello'")

        # 检测
        from agents.executable_skills import is_executable_skill
        assert is_executable_skill(skill_dir) is True

        # 加载（不实际安装，只检测逻辑）
        assert loader.is_loaded("test_skill") is False
        assert loader.list_loaded() == []


# ── memory.py usage tracking 测试 ─────────────────────────────────────────────


class TestMemoryUsageTracking:
    """测试 memory usage tracking 和 maintenance。"""

    @pytest.fixture
    def temp_memory_dir(self, tmp_path):
        """创建临时 memory 目录。"""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        with patch("agents.memory.get_memory_dir", return_value=memory_dir):
            yield memory_dir

    def test_record_memory_recall(self, temp_memory_dir):
        from agents.memory import save_memory, record_memory_recall, get_memory_usage_stats

        # 创建 memory
        filename = save_memory("test-memory", "Test description", "user", "Test content")

        # 初始状态
        stats = get_memory_usage_stats(filename)
        assert stats["recall_count"] == 0

        # 记录召回
        record_memory_recall(filename)
        stats = get_memory_usage_stats(filename)
        assert stats["recall_count"] == 1

        record_memory_recall(filename)
        stats = get_memory_usage_stats(filename)
        assert stats["recall_count"] == 2

    def test_record_memory_success(self, temp_memory_dir):
        from agents.memory import save_memory, record_memory_success, get_memory_usage_stats

        filename = save_memory("test-memory", "Test", "user", "Content")

        stats = get_memory_usage_stats(filename)
        assert stats["success_associated"] == 0

        record_memory_success(filename)
        stats = get_memory_usage_stats(filename)
        assert stats["success_associated"] == 1

    def test_maybe_promote_memory(self, temp_memory_dir):
        from agents.memory import (
            save_memory, record_memory_recall, record_memory_success,
            maybe_promote_or_archive_memory,
        )

        filename = save_memory("hot-memory", "Frequently used", "user", "Content")

        # 模拟高频使用
        for _ in range(25):
            record_memory_recall(filename)
        for _ in range(20):
            record_memory_success(filename)

        # 应该被提权
        result = maybe_promote_or_archive_memory(
            filename,
            promote_threshold=20,
            promote_success_rate=0.6,
        )
        assert result["action"] == "promote"

    def test_maybe_archive_memory(self, temp_memory_dir):
        from agents.memory import save_memory, maybe_promote_or_archive_memory
        from datetime import datetime, timezone, timedelta

        filename = save_memory("old-memory", "Old memory", "user", "Content")

        # 手动设置 last_recalled 为 60 天前
        memory_path = temp_memory_dir / filename
        raw = memory_path.read_text()
        from agents.frontmatter import parse_frontmatter, format_frontmatter
        result = parse_frontmatter(raw)
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        result.meta["last_recalled"] = old_date
        result.meta["recall_count"] = 0
        memory_path.write_text(format_frontmatter(result.meta, result.body))

        # 应该被归档
        res = maybe_promote_or_archive_memory(filename, archive_days=30)
        assert res["action"] == "archive"

    def test_maintenance_all_memories(self, temp_memory_dir):
        from agents.memory import save_memory, maintenance_all_memories

        save_memory("mem1", "Memory 1", "user", "Content 1")
        save_memory("mem2", "Memory 2", "project", "Content 2")

        result = maintenance_all_memories()
        assert "checked" in result
        assert result["checked"] == 2


# ── online_skill_eval.py 进化评测指标测试 ─────────────────────────────────────


class TestEvolutionEvalMetrics:
    """测试进化评测指标（replay、规则编译、champion）。"""

    def test_compile_eval_rules_basic(self):
        from agents.online_skill_eval import _compile_eval_rules

        skill = {
            "name": "test-skill",
            "description": "A test skill",
            "when_to_use": "When testing",
            "instructions": "Always cite sources and keep within 3 paragraphs.",
            "tags": ["test"],
        }

        rules = _compile_eval_rules(skill)

        # 至少有 response_nonempty 规则
        assert any(r["rule_id"] == "response_nonempty" for r in rules)
        # 应该检测到引用来源要求
        assert any(r["rule_id"] == "must_cite_sources" for r in rules)
        # 应该检测到段落限制
        assert any(r["rule_id"] == "paragraph_limit" for r in rules)

    def test_compile_eval_rules_json(self):
        from agents.online_skill_eval import _compile_eval_rules

        skill = {
            "name": "json-skill",
            "description": "Output JSON",
            "instructions": "Output valid JSON only.",
            "tags": [],
        }

        rules = _compile_eval_rules(skill)
        assert any(r["rule_id"] == "json_parseable" for r in rules)

    def test_compile_eval_rules_conclusion(self):
        from agents.online_skill_eval import _compile_eval_rules

        skill = {
            "name": "conclusion-skill",
            "description": "Lead with conclusion",
            "instructions": "先给结论，再展开说明。",
            "tags": [],
        }

        rules = _compile_eval_rules(skill)
        assert any(r["rule_id"] == "lead_with_conclusion" for r in rules)

    def test_evaluate_rule_nonempty(self):
        from agents.online_skill_eval import _evaluate_rule

        rule = {"rule_id": "response_nonempty", "params": {"mode": "nonempty"}, "hard": True}

        assert _evaluate_rule(rule, "Hello")["passed"] is True
        assert _evaluate_rule(rule, "")["passed"] is False
        assert _evaluate_rule(rule, "   ")["passed"] is False

    def test_evaluate_rule_json(self):
        from agents.online_skill_eval import _evaluate_rule

        rule = {"rule_id": "json_parseable", "params": {"mode": "json_parseable"}, "hard": True}

        assert _evaluate_rule(rule, '{"key": "value"}')["passed"] is True
        assert _evaluate_rule(rule, "not json")["passed"] is False

    def test_evaluate_rule_paragraph_limit(self):
        from agents.online_skill_eval import _evaluate_rule

        rule = {
            "rule_id": "paragraph_limit",
            "params": {"mode": "max_paragraphs", "max_paragraphs": 3},
            "hard": True,
        }

        # 2 段应该通过
        assert _evaluate_rule(rule, "Para 1\n\nPara 2")["passed"] is True
        # 4 段应该失败
        assert _evaluate_rule(rule, "P1\n\nP2\n\nP3\n\nP4")["passed"] is False

    def test_evaluate_rule_markdown_table(self):
        from agents.online_skill_eval import _evaluate_rule

        rule = {"rule_id": "markdown_table", "params": {"mode": "markdown_table"}, "hard": False}

        table_text = "| A | B |\n|---|---|\n| 1 | 2 |"
        assert _evaluate_rule(rule, table_text)["passed"] is True
        assert _evaluate_rule(rule, "No table here")["passed"] is False

    def test_build_replay_pool(self):
        from agents.online_skill_eval import _build_replay_pool

        rows = [
            {
                "time": "2024-01-01T00:00:00Z",
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Hi there"},
                ],
            },
            {
                "time": "2024-01-02T00:00:00Z",
                "messages": [
                    {"role": "user", "content": "How are you?"},
                    {"role": "assistant", "content": "I'm fine"},
                ],
            },
        ]

        pool = _build_replay_pool("test-skill", rows, {}, freeze=False)

        assert len(pool) == 2
        assert all("sample_id" in s for s in pool)
        assert all("latest_user" in s for s in pool)

    def test_skill_status_healthy(self):
        from agents.online_skill_eval import _skill_status

        status, reasons = _skill_status(
            replay_count=10,
            promotion_test_count=3,
            retrieved=20,
            relevant=15,
            used=10,
            pruned=False,
            rule_summary={"pass_rate": 0.9, "hard_failures": 0, "promotion_test_hard_failures": 0},
            min_replay_samples=2,
            min_promotion_tests=1,
            min_retrieved=5,
            min_used_rate=0.2,
            min_relevance_rate=0.35,
            min_rule_pass_rate=0.8,
        )

        assert status == "healthy"

    def test_skill_status_watch(self):
        from agents.online_skill_eval import _skill_status

        status, reasons = _skill_status(
            replay_count=10,
            promotion_test_count=3,
            retrieved=20,
            relevant=5,  # low relevance
            used=2,
            pruned=False,
            rule_summary={"pass_rate": 0.9, "hard_failures": 0, "promotion_test_hard_failures": 0},
            min_replay_samples=2,
            min_promotion_tests=1,
            min_retrieved=5,
            min_used_rate=0.2,
            min_relevance_rate=0.35,
            min_rule_pass_rate=0.8,
        )

        assert status == "watch"

    def test_skill_status_incubating(self):
        from agents.online_skill_eval import _skill_status

        status, reasons = _skill_status(
            replay_count=1,  # too few
            promotion_test_count=0,
            retrieved=2,
            relevant=1,
            used=0,
            pruned=False,
            rule_summary={"pass_rate": 0.0, "hard_failures": 0, "promotion_test_hard_failures": 0},
            min_replay_samples=2,
            min_promotion_tests=1,
            min_retrieved=5,
            min_used_rate=0.2,
            min_relevance_rate=0.35,
            min_rule_pass_rate=0.8,
        )

        assert status == "incubating"

    def test_promotion_decision_first_healthy(self):
        from agents.online_skill_eval import _promotion_decision

        result = _promotion_decision(
            status="healthy",
            candidate={"average_score": 0.8, "hard_failures": 0},
            champion={},  # no previous champion
        )

        assert result["promoted"] is True
        assert result["status"] == "active_champion"

    def test_promotion_decision_beats_champion(self):
        from agents.online_skill_eval import _promotion_decision

        result = _promotion_decision(
            status="healthy",
            candidate={"average_score": 0.9, "hard_failures": 0},
            champion={"summary": {"average_score": 0.8, "hard_failures": 0}},
        )

        assert result["promoted"] is True

    def test_promotion_decision_rejected_by_retention(self):
        from agents.online_skill_eval import _promotion_decision

        result = _promotion_decision(
            status="healthy",
            candidate={"average_score": 0.7, "hard_failures": 0},
            champion={"summary": {"average_score": 0.8, "hard_failures": 0}},
        )

        assert result["promoted"] is False
        assert "retention" in result.get("reason", "").lower() or "regress" in result.get("reason", "").lower()

    def test_promotion_decision_rejected_by_hard_failures(self):
        from agents.online_skill_eval import _promotion_decision

        result = _promotion_decision(
            status="healthy",
            candidate={"average_score": 0.9, "hard_failures": 3},
            champion={"summary": {"average_score": 0.8, "hard_failures": 0}},
        )

        assert result["promoted"] is False

    def test_champion_auto_activate_disabled_by_default(self):
        from agents.online_skill_eval import _auto_activate_enabled

        # 默认关闭
        assert _auto_activate_enabled() is False

    def test_champion_auto_activate_enabled(self):
        from agents.online_skill_eval import _auto_activate_enabled

        with patch.dict("os.environ", {"BEAR_SKILL_AUTO_ACTIVATE": "1"}):
            assert _auto_activate_enabled() is True


# ── 运行测试 ───────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
