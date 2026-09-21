"""Phase 4：citation 流式剥离 + usage_count 闭环单测。"""

from __future__ import annotations

import pytest

from agents.core.frontmatter import parse_frontmatter, format_frontmatter
from agents.core.workspace import workspace_scope
from agents.wiki.citation import CitationStripper, strip_citations


@pytest.fixture
def ws(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    with workspace_scope(workspace):
        yield workspace


def _stream(text: str, chunk_size: int) -> tuple[str, list[str]]:
    """按 chunk_size 逐块喂给剥离器，返回 (可见输出, paths)。"""
    stripper = CitationStripper()
    visible = []
    for i in range(0, len(text), chunk_size):
        visible.append(stripper.feed(text[i:i + chunk_size]))
    visible.append(stripper.flush())
    return "".join(visible), stripper.paths


def test_strip_whole_citation():
    text = "答案正文\n<wiki-citation>knowledge/pnpm.md</wiki-citation>"
    for size in (1, 3, 7, 200):
        visible, paths = _stream(text, size)
        assert visible == "答案正文\n", f"chunk={size}"
        assert paths == ["knowledge/pnpm.md"]


def test_partial_tag_prefix_passthrough():
    # 形似标签前缀但不是标签的文本必须完整可见
    text = "比较 a < b 和 <wiki 标签以及 <unknown> 都要透传"
    for size in (1, 2, 5, 50):
        visible, paths = _stream(text, size)
        assert visible == text, f"chunk={size}"
        assert paths == []


def test_multiple_citations():
    text = (
        "先引用<wiki-citation>user/p1.md</wiki-citation>中间文字"
        "<wiki-citation>feedback/p2.md</wiki-citation>结尾"
    )
    visible, paths = _stream(text, 4)
    assert visible == "先引用中间文字结尾"
    assert paths == ["user/p1.md", "feedback/p2.md"]


def test_unclosed_citation_flushed_as_text():
    text = "正文<wiki-citation>knowledge/broken.md"
    visible, paths = _stream(text, 3)
    assert visible == text
    assert paths == []


def test_strip_citations_oneshot():
    text = "回答<wiki-citation>knowledge/x.md</wiki-citation>"
    clean, paths = strip_citations(text)
    assert clean == "回答"
    assert paths == ["knowledge/x.md"]
    assert strip_citations("无 citation 文本") == ("无 citation 文本", [])


def test_increment_usage_no_commit(ws):
    from agents.wiki.wiki_manager import (
        get_wiki_dir, increment_usage, write_wiki_entry, _git_commit, init_wiki_git,
    )

    init_wiki_git()
    write_wiki_entry("knowledge", "计数条目", "内容", description="d")
    _git_commit("test: seed")

    import subprocess
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=get_wiki_dir(), capture_output=True, text=True
    ).stdout.strip()

    increment_usage("knowledge/计数条目.md")
    increment_usage("knowledge/计数条目.md")

    path = get_wiki_dir() / "knowledge" / "计数条目.md"
    meta = parse_frontmatter(path.read_text()).meta
    assert meta["usage_count"] == "2"
    assert meta["last_used"]

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=get_wiki_dir(), capture_output=True, text=True
    ).stdout.strip()
    assert head_before == head_after, "usage 增量不应产生独立 commit"


def test_increment_usage_missing_file_noop(ws):
    from agents.wiki.wiki_manager import increment_usage
    increment_usage("knowledge/不存在.md")  # 不抛异常


def test_injection_format_contains_citation_instruction(ws):
    from agents.wiki.wiki_manager import format_wiki_for_injection, WikiEntry

    entry = WikiEntry(
        name="t", type="knowledge", filename="t.md",
        rel_path="knowledge/t.md", content="内容", meta={},
    )
    text = format_wiki_for_injection([entry])
    assert "<wiki-citation>knowledge/t.md</wiki-citation>" in text
