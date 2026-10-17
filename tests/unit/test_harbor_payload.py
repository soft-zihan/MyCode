"""MyCode 送进容器的 payload：选哪些文件、怎么装、怎么起。"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.harbor import payload
from eval.harbor.payload import (
    API_KEY_ENV,
    BASE_URL_ENV,
    CONTAINER_ROOT,
    INSTRUCTION_PATH,
    PAYLOAD_DIRS,
    PAYLOAD_FILES,
    RESULT_PATH,
    container_env,
    context_updates,
    hard_failure,
    install_commands,
    model_id,
    parse_bool,
    parse_int,
    payload_members,
    run_command,
    settings_from_env,
    stage_payload,
    thinking_misalignment,
)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in (
        "agents/agent.py",
        "agents/prompts/system.txt",
        "agents/tools/__pycache__/read.cpython-312.pyc",
        "agents/__pycache__/agent.cpython-312.pyc",
        "eval/common/runner_base.py",
        "eval/common/__init__.py",
        "eval/harbor/__init__.py",
        "eval/harbor/agent_main.py",
        "eval/loca/runner.py",
        "eval/reports/old.json",
        "requirements.txt",
        "frontend/package.json",
        "mindmap/mindmaps/01.md",
    ):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return root


def test_members_include_agent_code_and_prompt_data(fake_repo):
    members = {m.as_posix() for m in payload_members(fake_repo)}

    assert "agents/agent.py" in members
    # prompts/SKILL.md 这类数据文件是运行必需的，不能只挑 .py
    assert "agents/prompts/system.txt" in members
    assert "eval/common/runner_base.py" in members
    assert "eval/harbor/agent_main.py" in members
    assert "requirements.txt" in members


def test_members_exclude_pycache(fake_repo):
    members = {m.as_posix() for m in payload_members(fake_repo)}

    assert not any("__pycache__" in m for m in members)
    assert not any(m.endswith(".pyc") for m in members)


def test_members_exclude_irrelevant_trees(fake_repo):
    members = {m.as_posix() for m in payload_members(fake_repo)}

    # 前端 406MB、mindmap 21MB、别的 benchmark、历史报告都不该进容器
    assert not any(m.startswith("frontend/") for m in members)
    assert not any(m.startswith("mindmap/") for m in members)
    assert not any(m.startswith("eval/loca/") for m in members)
    assert not any(m.startswith("eval/reports/") for m in members)


def test_members_are_relative_and_sorted(fake_repo):
    members = payload_members(fake_repo)

    assert all(not m.is_absolute() for m in members)
    assert members == sorted(members, key=lambda p: p.as_posix())


def test_missing_required_entry_raises(tmp_path):
    (tmp_path / "requirements.txt").write_text("x", encoding="utf-8")

    with pytest.raises(FileNotFoundError) as exc:
        payload_members(tmp_path)

    assert "agents" in str(exc.value)


def test_stage_payload_copies_tree_and_reports_count(fake_repo, tmp_path):
    dest = tmp_path / "staged"

    n = stage_payload(fake_repo, dest)

    assert n == len(payload_members(fake_repo))
    assert (dest / "agents" / "prompts" / "system.txt").read_text(encoding="utf-8") == "x"
    assert (dest / "requirements.txt").exists()
    assert not (dest / "frontend").exists()
    assert not list(dest.rglob("__pycache__"))


def test_payload_dirs_and_files_are_declared():
    assert "agents" in PAYLOAD_DIRS
    assert "eval/common" in PAYLOAD_DIRS
    assert "eval/harbor" in PAYLOAD_DIRS
    assert "requirements.txt" in PAYLOAD_FILES


def test_install_commands_use_venv_not_system_python():
    cmds = install_commands()
    joined = "\n".join(cmds)

    venv = f"{CONTAINER_ROOT}/.venv"
    assert any("python3 -m venv" in c for c in cmds)
    assert venv in joined
    # 装在 venv 里，不污染题目镜像自带的 python（那是要被测的语言环境）
    assert "pip install" in joined
    assert "-r" in joined and "requirements.txt" in joined
    # venv 建好之后才能往里装
    assert joined.index("python3 -m venv") < joined.index("pip install")


def test_install_commands_do_not_hardcode_a_mirror():
    # 镜像源属于运行环境配置，走容器的 PIP_INDEX_URL，不写死在代码里
    assert not any("tuna" in c or "aliyun" in c for c in install_commands())


def test_run_command_invokes_agent_main_module():
    cmd = run_command(model="qwen3.6-27b", workdir="/app")

    assert f"{CONTAINER_ROOT}/.venv/bin/python" in cmd
    assert "-m eval.harbor.agent_main" in cmd
    assert "--model" in cmd and "qwen3.6-27b" in cmd
    assert "--workdir" in cmd and "/app" in cmd
    assert INSTRUCTION_PATH in cmd
    assert RESULT_PATH in cmd


def test_run_command_quotes_hostile_values():
    cmd = run_command(model="m", workdir="/app/$(rm -rf /)")

    # 带 shell 元字符的值必须被引号包住，不能被解释
    assert "$(rm -rf /)" in cmd
    assert "'/app/$(rm -rf /)'" in cmd


def test_run_command_passes_timeout():
    assert "--timeout-s 600" in run_command(model="m", workdir="/app", timeout_s=600)
    assert "--timeout-s" not in run_command(model="m", workdir="/app", timeout_s=0)


def test_env_names_are_mycode_scoped():
    # 不复用 OPENAI_API_KEY：那名字是 harbor 给 opencode 臂解析凭据用的，
    # 两臂共用一个变量名会让「谁在读哪个凭据」变得含糊
    assert API_KEY_ENV == "MYCODE_API_KEY"
    assert BASE_URL_ENV == "MYCODE_BASE_URL"
    assert API_KEY_ENV != BASE_URL_ENV


# ── harbor 的 provider/model 两段式 → 网关认的裸模型名 ──


def test_model_id_strips_only_the_provider_segment():
    assert model_id("openrouter/qwen3.6-27b") == "qwen3.6-27b"
    # 只切第一个斜杠：模型名自己带斜杠时（org/model）要原样保留后半段
    assert model_id("openrouter/org/model") == "org/model"


def test_model_id_passes_a_bare_name_through():
    assert model_id("qwen3.6-27b") == "qwen3.6-27b"


@pytest.mark.parametrize("bad", [None, ""])
def test_model_id_rejects_empty(bad):
    # 空模型名会让 harbor 解析不出 provider，凭据静默变 None，最后在容器里
    # 以一个看不懂的 401 出现——不如在这里就说清楚
    with pytest.raises(ValueError):
        model_id(bad)


# ── 往容器里带的 env ──


def test_container_env_carries_exactly_the_two_credentials():
    env = container_env(api_key="sk-x", base_url="https://gw.test/v1")

    assert env == {API_KEY_ENV: "sk-x", BASE_URL_ENV: "https://gw.test/v1"}


def test_container_env_does_not_reuse_openai_names():
    # 多带一个 OPENAI_API_KEY 就会和 opencode 臂的凭据来源混在一起，
    # 出问题时分不清是谁在读哪个 key
    env = container_env(api_key="sk-x", base_url="https://gw.test/v1")

    assert not any(name.startswith("OPENAI") for name in env)


# ── 结果 JSON → harbor AgentContext ──


def test_context_updates_maps_all_four_token_counters():
    updates = context_updates(
        {"tokens": {"input": 100, "output": 20, "cached": 30, "reasoning": 8}, "text": "abc"}
    )

    # harbor 的 n_input_tokens 口径是「含 cache 的总输入」，MyCode 的
    # tokens.input 来自 prompt_tokens，同样含 cache，所以直接对上，不做减法
    assert updates["n_input_tokens"] == 100
    assert updates["n_output_tokens"] == 20
    assert updates["n_cache_tokens"] == 30
    # reasoning 是 output 的子集，只能挂在 metadata 里：harbor 没有对应的顶层字段，
    # 而 opencode 的 completion_tokens 不含思考，不单列就没法跟它对齐口径
    assert updates["metadata"]["mycode_reasoning_tokens"] == 8


def test_context_updates_metadata_keeps_the_diagnostic_trail():
    updates = context_updates(
        {
            "text": "hello",
            "error": "TimeoutError: timeout after 600s",
            "duration_s": 601.2,
            "agent_session_id": "s-1",
            "trace_id": "t-1",
            "compression_events": {"tool_folded": 2},
        }
    )
    meta = updates["metadata"]

    # 这些字段是「跑了但出错」时唯一的可观测痕迹，必须进 trial 的 result.json
    assert meta["mycode_error"] == "TimeoutError: timeout after 600s"
    assert meta["mycode_duration_s"] == 601.2
    assert meta["mycode_text_chars"] == 5
    assert meta["mycode_session_id"] == "s-1"
    assert meta["mycode_trace_id"] == "t-1"
    assert meta["mycode_compression_events"] == {"tool_folded": 2}


def test_context_updates_tolerates_a_result_without_tokens():
    updates = context_updates({"error": "boom"})

    assert updates["n_input_tokens"] == 0
    assert updates["n_output_tokens"] == 0
    assert updates["n_cache_tokens"] == 0
    assert updates["metadata"]["mycode_reasoning_tokens"] == 0
    assert updates["metadata"]["mycode_text_chars"] == 0


# ── 思考开关对齐哨兵 ──────────────────────────────────────────────────


def test_sentinel_stays_quiet_when_thinking_actually_happened():
    result = {"tokens": {"input": 10, "output": 40, "reasoning": 25}}

    assert thinking_misalignment(result, {"thinking": None}) is None


def test_sentinel_fires_when_default_thinking_produced_no_reasoning():
    # 这就是首轮 25 题的状态：以为在跟随模型默认，实际一个思考 token 都没有
    result = {"tokens": {"input": 10, "output": 40, "reasoning": 0}}

    warning = thinking_misalignment(result, {"thinking": None})

    assert warning is not None and "思考开关" in warning


def test_sentinel_stays_quiet_when_thinking_was_explicitly_turned_off():
    # 显式关掉是有意为之，两臂都关就是对齐的，不该报警
    result = {"tokens": {"input": 10, "output": 40, "reasoning": 0}}

    assert thinking_misalignment(result, {"thinking": False}) is None


def test_sentinel_ignores_a_failed_run():
    # 跑失败了 token 本来就是 0，再报一次变量警告只是噪声
    result = {"error": "TimeoutError: timeout after 600s", "tokens": {}}

    assert thinking_misalignment(result, {"thinking": None}) is None


# ── 「根本没跑起来」vs「跑了没做对」 ──


def test_hard_failure_is_none_when_the_agent_ran_clean():
    assert hard_failure({"text": "done", "error": None}) is None


def test_hard_failure_reports_an_error_that_produced_nothing():
    # 一个字都没产出 = 这次 trial 不含任何信号，必须显式失败而不是按 0 分算，
    # 否则网关抖动和「模型真不会做」在通过率里会变成同一件事
    assert hard_failure({"text": "", "error": "APIConnectionError: boom"}) == (
        "APIConnectionError: boom"
    )


@pytest.mark.parametrize("text", ["", "   \n\t ", None])
def test_hard_failure_treats_blank_output_as_no_output(text):
    assert hard_failure({"text": text, "error": "boom"}) == "boom"


def test_hard_failure_keeps_a_partial_run_scoreable():
    # 中途超时但已经改过文件：交给 verifier 判分，别把做过的工作扔掉
    assert hard_failure({"text": "I edited two_bucket.py", "error": "timeout"}) is None


# ── 行为开关：容器里没有 ~/.my-code/config.json，得从 env 搬 ──


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("on", True), ("false", False), ("0", False)],
)
def test_parse_bool_reads_the_usual_spellings(raw, expected):
    assert parse_bool(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_bool_keeps_unset_as_none(raw):
    # None 是「跟随模型默认」，False 是「显式关掉」，两个意思不能混
    assert parse_bool(raw) is None


def test_parse_bool_rejects_garbage_instead_of_guessing():
    with pytest.raises(ValueError):
        parse_bool("maybe")


def test_parse_int():
    assert parse_int("200000") == 200_000
    assert parse_int(None) is None
    assert parse_int("  ") is None


def test_settings_from_env_reads_the_three_switches():
    env = {
        payload.THINKING_ENV: "false",
        payload.THINKING_FEEDBACK_ENV: "true",
        payload.CONTEXT_WINDOW_ENV: "1000000",
    }

    assert settings_from_env(env.get) == {
        "thinking": False,
        "thinking_feedback": True,
        "context_window": 1_000_000,
    }


def test_settings_from_env_of_an_empty_env_is_all_defaults():
    assert settings_from_env(lambda _: None) == {
        "thinking": None,
        "thinking_feedback": None,
        "context_window": None,
    }


def test_settings_keys_are_real_run_agent_task_parameters():
    # 键名写错的话不会在宿主上报错，只会在容器里跑到一半炸掉——
    # 那条路径要几分钟才走到，所以在这里用签名钉住
    import inspect

    from eval.common.runner_base import run_agent_task

    parameters = inspect.signature(run_agent_task).parameters
    for key in settings_from_env(lambda _: None):
        assert key in parameters, f"run_agent_task 没有 {key} 这个形参"
