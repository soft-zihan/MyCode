"""Shared fixtures: isolate HOME / cwd side effects so tests never touch real user data."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point HOME and memory/session dirs at a temp dir for every test.

    agents.session derives its paths from Path.home() or
    Path.cwd(), so redirecting both keeps tests from touching ~/.mycode,
    ~/.my-code or the real project's .mycode/ directory.
    """
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workdir)
    # Clear any ambient API config so tests never hit real endpoints.
    for var in (
        "APIKEY",
        "API",
        "MODEL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield home
