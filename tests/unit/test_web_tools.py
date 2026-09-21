import json
import sys
from types import SimpleNamespace

from agents.tools.web_tools import web_search


def test_web_search_requires_query():
    assert web_search({}).startswith("Error: query is required.")


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


def test_web_search_returns_error_message(monkeypatch):
    class FakeDDGS:
        def __init__(self, timeout=None):
            pass

        def text(self, query, **kwargs):
            raise RuntimeError("blocked")

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FakeDDGS))

    assert web_search({"query": "hello"}) == "Error: web_search failed: RuntimeError: blocked"
