"""容器内入口的前置校验：凭据/题目/工作区不对就立刻退出，别等到跑一半。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.harbor.agent_main import build_parser, main, write_result
from eval.harbor.payload import API_KEY_ENV, BASE_URL_ENV


@pytest.fixture
def ready_env(tmp_path, monkeypatch):
    """凑齐一个能走到 run_agent_task 之前的环境。"""
    monkeypatch.setenv(API_KEY_ENV, "k")
    monkeypatch.setenv(BASE_URL_ENV, "https://example.test/v1")
    instruction = tmp_path / "instruction.md"
    instruction.write_text("# do the thing\n", encoding="utf-8")
    workdir = tmp_path / "app"
    workdir.mkdir()
    result = tmp_path / "out" / "result.json"
    return {
        "instruction": instruction,
        "workdir": workdir,
        "result": result,
    }


def _argv(env, *extra):
    return [
        "--model", "qwen3.6-27b",
        "--workdir", str(env["workdir"]),
        "--instruction-file", str(env["instruction"]),
        "--result", str(env["result"]),
        *extra,
    ]


def test_parser_defaults():
    args = build_parser().parse_args([
        "--model", "m", "--workdir", "/app",
        "--instruction-file", "/i.md", "--result", "/r.json",
    ])

    assert args.benchmark == "aider_polyglot"
    assert args.task_id == ""
    # 默认不设内部超时：交给 harbor 的 agent 超时，与 opencode 臂一致
    assert args.timeout_s == 0


def test_missing_credentials_exits_2_without_writing_result(ready_env, monkeypatch):
    monkeypatch.delenv(API_KEY_ENV)

    rc = main(_argv(ready_env))

    assert rc == 2
    assert not ready_env["result"].exists()


def test_missing_base_url_exits_2(ready_env, monkeypatch):
    monkeypatch.delenv(BASE_URL_ENV)

    assert main(_argv(ready_env)) == 2


def test_empty_credentials_treated_as_missing(ready_env, monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "")

    assert main(_argv(ready_env)) == 2


def test_empty_instruction_exits_2(ready_env):
    ready_env["instruction"].write_text("   \n", encoding="utf-8")

    assert main(_argv(ready_env)) == 2


def test_missing_instruction_file_exits_2(ready_env):
    ready_env["instruction"].unlink()

    with pytest.raises(FileNotFoundError):
        main(_argv(ready_env))


def test_workdir_must_be_a_directory(ready_env, tmp_path):
    rc = main([
        "--model", "m",
        "--workdir", str(tmp_path / "nope"),
        "--instruction-file", str(ready_env["instruction"]),
        "--result", str(ready_env["result"]),
    ])

    assert rc == 2
    assert not ready_env["result"].exists()


def test_write_result_creates_parent_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "result.json"

    write_result(target, {"text": "你好", "tokens": {"input": 1, "output": 2}})

    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded["text"] == "你好"
    assert loaded["tokens"] == {"input": 1, "output": 2}


def test_write_result_survives_unserializable_values(tmp_path):
    target = tmp_path / "r.json"

    write_result(target, {"path": Path("/app/x.py")})

    assert json.loads(target.read_text(encoding="utf-8"))["path"] == "/app/x.py"


def test_credentials_are_never_taken_from_argv():
    # 命令行会原样印进 harbor 的 trial.log，所以凭据只允许走环境变量
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "--model", "m", "--workdir", "/app",
            "--instruction-file", "/i.md", "--result", "/r.json",
            "--api-key", "secret",
        ])
