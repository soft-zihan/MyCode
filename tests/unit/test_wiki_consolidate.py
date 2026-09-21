"""Phase 3：git-diff 驱动整理 + 索引剪枝闭环单测。"""

from __future__ import annotations

import json

import pytest

from agents.core.frontmatter import parse_frontmatter, format_frontmatter
from agents.core.workspace import workspace_scope


@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    with workspace_scope(workspace):
        yield workspace


def _write_entry(rel: str, name: str, content: str, wiki_type: str = "knowledge", **meta):
    from agents.wiki.wiki_manager import get_wiki_dir
    path = get_wiki_dir() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    front = {"name": name, "type": wiki_type, "description": name, "applied_count": "0", **meta}
    path.write_text(format_frontmatter(front, content))
    return path


async def test_supersede_contradictory_pair(ws, monkeypatch):
    """矛盾条目对：consolidate 后旧条目 superseded_by + archived，状态推进。"""
    from agents.wiki.wiki_manager import init_wiki_git, get_wiki_dir, update_wiki_index
    import agents.wiki.wiki_manager as wm
    from agents.wiki import wiki_consolidator as wc

    async def no_preflight(content, wiki_type, top_k=5):
        return []
    monkeypatch.setattr(wm, "preflight_wiki_search", no_preflight)

    init_wiki_git()
    # 6 条满足 ≥5 变更门槛；old/new 构成矛盾对
    _write_entry("knowledge/pool-old.md", "连接池配置", "config/database.py 连接池 pool_size=10")
    _write_entry("knowledge/pool-new.md", "连接池调整", "config/database.py 连接池因高并发从 10 调至 50（2026-09-20）")
    for i in range(4):
        _write_entry(f"knowledge/filler-{i}.md", f"filler{i}", f"内容 {i}")
    from agents.wiki.wiki_manager import _git_commit
    update_wiki_index()
    _git_commit("test: seed entries")

    async def stub_sq(system, user):
        entries = json.loads(user.split("Entries:\n", 1)[1])
        paths = {e["path"] for e in entries}
        assert "knowledge/pool-old.md" in paths and "knowledge/pool-new.md" in paths
        return json.dumps([{
            "action": "supersede",
            "targets": ["knowledge/pool-old.md"],
            "into": "knowledge/pool-new.md",
            "reason": "连接池 10 已被 50 取代",
        }], ensure_ascii=False)

    result = await wc.run_consolidate(stub_sq)
    assert result["status"] == "done"
    assert result["applied"] == 1

    old = parse_frontmatter((get_wiki_dir() / "knowledge/pool-old.md").read_text()).meta
    assert old["status"] == "archived"
    assert old["superseded_by"] == "knowledge/pool-new.md"

    state = wc.load_consolidate_state()
    assert state["last_consolidate_commit"]
    assert state["last_consolidate_at"]

    # 归档条目退出索引
    index = (get_wiki_dir() / "WIKI.md").read_text()
    assert "pool-old" not in index
    assert "pool-new" in index or "连接池调整" in index


async def test_consolidate_defers_below_threshold(ws):
    from agents.wiki.wiki_manager import init_wiki_git, _git_commit
    from agents.wiki import wiki_consolidator as wc

    init_wiki_git()
    _write_entry("knowledge/only-one.md", "唯一条目", "内容")
    _git_commit("test: one entry")

    calls = []

    async def stub_sq(system, user):
        calls.append(user)
        return "[]"

    result = await wc.run_consolidate(stub_sq)
    assert result["status"] == "deferred"
    assert not calls
    assert not wc.load_consolidate_state().get("last_consolidate_commit")


async def test_consolidate_noop_advances_state(ws):
    from agents.wiki.wiki_manager import init_wiki_git, _git_commit
    from agents.wiki import wiki_consolidator as wc

    init_wiki_git()
    for i in range(6):
        _write_entry(f"knowledge/e{i}.md", f"e{i}", f"c{i}")
    _git_commit("test: seed")
    head = wc._head_commit()
    wc.save_consolidate_state({"last_consolidate_commit": head})

    async def stub_sq(system, user):
        return "[]"

    result = await wc.run_consolidate(stub_sq)
    assert result["status"] == "noop"
    assert wc.load_consolidate_state()["last_consolidate_commit"] == head


def test_index_pruning_and_reheat(ws):
    """250 条超预算 → 剪到 ≤200 行、最冷被剪、回热自动回归。"""
    from agents.wiki.wiki_manager import (
        update_wiki_index, get_wiki_dir, increment_applied_count, MAX_INDEX_LINES,
    )

    # 热条目：最近使用 + 高计数
    _write_entry(
        "knowledge/hot.md", "热条目", "hot content",
        last_applied="2026-09-21T00:00:00+00:00", applied_count="9", usage_count="5",
    )
    # 249 个冷条目（无 last_applied，只有旧 modified）
    for i in range(249):
        _write_entry(f"knowledge/cold-{i:03d}.md", f"cold{i}", "x", modified=f"2026-01-01T00:00:{i % 60:02d}+00:00")

    update_wiki_index()

    index_text = (get_wiki_dir() / "WIKI.md").read_text()
    assert len(index_text.splitlines()) <= MAX_INDEX_LINES
    assert len(index_text.encode()) <= 25000
    # 热条目保留在索引
    assert "热条目" in index_text or "hot.md" in index_text

    state = json.loads((get_wiki_dir() / ".consolidate_state.json").read_text())
    pruned = state["pruned_from_index"]
    assert len(pruned) >= 50
    assert "knowledge/hot.md" not in pruned

    # 回热：被剪条目被召回 → 移出剪枝名单
    victim = pruned[0]
    increment_applied_count(victim)
    state = json.loads((get_wiki_dir() / ".consolidate_state.json").read_text())
    assert victim not in state["pruned_from_index"]
