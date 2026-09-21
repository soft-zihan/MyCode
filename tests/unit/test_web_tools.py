import json
import sys
from types import SimpleNamespace

import agents.tools.web_tools as web_tools
from agents.tools.web_tools import _parse_exa_text, web_search


def test_web_search_requires_query():
    assert web_search({}).startswith("Error: query is required.")


def test_web_search_unknown_backend():
    assert web_search({"query": "x", "backend": "bing"}).startswith("Error: unknown backend")


def test_web_search_normalizes_results(monkeypatch):
    captured: list[dict] = []

    class FakeDDGS:
        def __init__(self, timeout=None):
            captured.append({"timeout": timeout})

        def text(self, query, **kwargs):
            captured[0].update({"query": query, **kwargs})
            return [
                {"title": "Example", "href": "https://example.com", "body": "snippet"},
                {"title": "Alt", "url": "https://alt.example", "description": "alt snippet"},
            ]

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))

    raw = web_search({"query": "hello world", "max_results": 5, "timelimit": "y"})
    assert raw.startswith("{")
    payload = json.loads(raw)
    assert payload["query"] == "hello world"
    assert payload["backend"] == "ddgs"
    assert payload["count"] == 2
    assert payload["results"][0] == {
        "title": "Example",
        "url": "https://example.com",
        "snippet": "snippet",
    }
    assert payload["results"][1]["url"] == "https://alt.example"
    assert captured[0]["query"] == "hello world"
    assert captured[0]["max_results"] == 5
    assert captured[0]["timelimit"] == "y"


def test_web_search_ddgs_failure_falls_back_to_exa(monkeypatch):
    class FakeDDGS:
        def __init__(self, timeout=None):
            pass

        def text(self, query, **kwargs):
            raise RuntimeError("blocked")

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))
    exa_calls: list[tuple] = []

    def fake_exa(query, max_results):
        exa_calls.append((query, max_results))
        return [{"title": "Exa Hit", "url": "https://exa.example", "snippet": "from exa"}]

    monkeypatch.setattr(web_tools, "_search_exa", fake_exa)

    payload = json.loads(web_search({"query": "hello", "max_results": 3}))
    assert payload["backend"] == "exa"
    assert payload["results"][0]["title"] == "Exa Hit"
    assert exa_calls == [("hello", 3)]


def test_web_search_ddgs_empty_falls_back_to_exa(monkeypatch):
    class FakeDDGS:
        def __init__(self, timeout=None):
            pass

        def text(self, query, **kwargs):
            return []

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))
    monkeypatch.setattr(
        web_tools, "_search_exa",
        lambda q, n: [{"title": "T", "url": "https://t.example", "snippet": "s"}],
    )
    payload = json.loads(web_search({"query": "hello"}))
    assert payload["backend"] == "exa"


def test_web_search_both_backends_fail(monkeypatch):
    class FakeDDGS:
        def __init__(self, timeout=None):
            pass

        def text(self, query, **kwargs):
            raise RuntimeError("blocked")

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))

    def broken_exa(query, max_results):
        raise OSError("unreachable")

    monkeypatch.setattr(web_tools, "_search_exa", broken_exa)

    out = web_search({"query": "hello"})
    assert out.startswith("Error: web_search failed: ")
    assert "ddgs: RuntimeError: blocked" in out
    assert "exa: OSError: unreachable" in out


def test_web_search_explicit_exa_backend_skips_ddgs(monkeypatch):
    def fail_ddgs(*args, **kwargs):
        raise AssertionError("ddgs should not be called")

    monkeypatch.setattr(web_tools, "_search_ddgs", fail_ddgs)
    monkeypatch.setattr(
        web_tools, "_search_exa",
        lambda q, n: [{"title": "T", "url": "https://t.example", "snippet": "s"}],
    )
    payload = json.loads(web_search({"query": "hello", "backend": "exa"}))
    assert payload["backend"] == "exa"


def test_parse_exa_text_multiple_blocks():
    text = """Title: First Doc
URL: https://first.example
Published: N/A
Author: N/A
Highlights:
line one
...
line two

Title: Second Doc
URL: https://second.example
Published: 2026-01-01
Author: someone
Highlights:
second highlight"""
    results = _parse_exa_text(text)
    assert len(results) == 2
    assert results[0]["title"] == "First Doc"
    assert results[0]["url"] == "https://first.example"
    assert results[0]["snippet"] == "line one line two"
    assert results[1]["title"] == "Second Doc"
    assert results[1]["snippet"] == "second highlight"


def test_parse_exa_text_skips_blocks_without_content():
    text = "Title: Empty\nURL: \nPublished: N/A\n"
    assert _parse_exa_text(text) == []
