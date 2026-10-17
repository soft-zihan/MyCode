"""runner 的配置生成与结果收集：这些值错了不会报错，只会悄悄量出另一件事。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from eval.harbor import payload
from eval.harbor.runner import (
    MODEL,
    MYCODE_AGENT_IMPORT_PATH,
    NO_PROXY,
    PROJECT_ROOT,
    PROVIDER,
    agent_spec,
    build_job_config,
    build_parser,
    collect_trials,
    container_env,
    harbor_command,
    harbor_env,
    latest_run_dir,
    list_task_ids,
    merge_trials,
    mycode_settings_env,
    prewarm_command,
    render_markdown,
    run_variables,
    settings_env_from_endpoint,
    summarize,
    wrap_with_docker_group,
)

BASE_URL = "https://gw.test/ada/v1"


# ── 容器 env ──────────────────────────────────────────────────────────


def test_container_env_points_the_gateway_away_from_the_proxy():
    # 评测网关在国内，走宿主代理（~110 KB/s）等于绕地球一圈，请求会超时
    env = container_env("10.147.17.2")

    assert env["HTTPS_PROXY"] == "http://10.147.17.2:7897"
    assert "cherygpt.com" in env["NO_PROXY"]
    # 大小写两套都要：requests 认大写，curl 和一部分工具只认小写
    assert env["no_proxy"] == env["NO_PROXY"]


def test_container_env_pins_the_pip_and_node_mirrors():
    env = container_env("10.0.0.1")

    # 题目 verifier 自己要 pip install pytest，opencode 臂要 nvm 下 node；
    # 这两个源直连不稳，必须走国内镜像。
    # pip 用阿里不用清华是量出来的：容器内 curl 清华 /simple/openai/ 返回 403
    # （强制 IPv4 也一样），阿里 200 / 0.2s；而 WSL 宿主里清华是 200——
    # 所以这个坑只在容器内出现，不在宿主上试是发现不了的。
    assert "aliyun" in env["PIP_INDEX_URL"]
    assert env["PIP_TRUSTED_HOST"] in env["PIP_INDEX_URL"]
    assert "aliyun" in env["NVM_NODEJS_ORG_MIRROR"]
    # uv 也认这个（harbor 的一部分安装步骤用 uv）
    assert env["UV_DEFAULT_INDEX"] == env["PIP_INDEX_URL"]


def test_no_proxy_is_consistent_with_the_documented_recipe():
    # README 1.5b 的表是量出来的，这里的常量必须和它一致
    assert NO_PROXY == container_env("10.0.0.1")["NO_PROXY"]
    for required in ("mirrors.aliyun.com", "pypi.tuna.tsinghua.edu.cn", "nodejs.org"):
        assert required in NO_PROXY


def test_no_proxy_keeps_the_deadsnakes_ppa_off_the_proxy():
    # python 题的 sources.list 里有 deadsnakes PPA，agent 安装期的 apt-get update
    # 每次都会打它。走代理实测被 503 打断 → 整个 trial 记异常，agent 一行都没跑；
    # 直连 200 / 7.2s。这一项是拿一次真实失败的 trial 换来的。
    assert "launchpadcontent.net" in NO_PROXY
    assert "keyserver.ubuntu.com" in NO_PROXY


def test_container_env_points_the_jvm_at_the_proxy_too():
    env = container_env("10.147.17.2")

    # JVM 不认 HTTPS_PROXY 这类环境变量，只认系统属性。java 题的 verifier 用
    # Gradle wrapper 下发行版，不设这两个属性它就走直连 → PKIX 验不过 / 连不上，
    # 4 道 java 题两臂全灭。
    options = env["JAVA_TOOL_OPTIONS"]
    assert "-Dhttps.proxyHost=10.147.17.2" in options
    assert "-Dhttps.proxyPort=7897" in options
    assert "-Dhttp.proxyHost=10.147.17.2" in options


def test_java_non_proxy_hosts_uses_java_syntax_not_no_proxy_syntax():
    options = container_env("10.147.17.2")["JAVA_TOOL_OPTIONS"]
    non_proxy = options.split("-Dhttp.nonProxyHosts=")[1]

    # Java 用 | 分隔、通配只认前导 *；照抄 NO_PROXY 的逗号写法不会报错，
    # 只会让匹配静默失效
    assert "," not in non_proxy
    assert "localhost" in non_proxy.split("|")
    assert "*.cherygpt.com" in non_proxy.split("|")
    # 境外的 gradle/maven 不能被排除，那正是要走代理的
    assert "gradle.org" not in non_proxy
    assert "maven.apache.org" not in non_proxy


# ── 合并多个 job 目录 ─────────────────────────────────────────────────


def test_merge_trials_lets_the_later_dir_win(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    _write_trial(first, "polyglot_java_x__1", {"task_name": "polyglot_java_x", "verifier_result": {"rewards": {"reward": 0.0}}})
    _write_trial(first, "polyglot_go_y__1", {"task_name": "polyglot_go_y", "verifier_result": {"rewards": {"reward": 1.0}}})
    _write_trial(second, "polyglot_java_x__2", {"task_name": "polyglot_java_x", "verifier_result": {"rewards": {"reward": 1.0}}})

    merged = merge_trials([first, second])

    # 补跑的那批要能顶掉先前整批废掉的结果，而且同名题不能被计两次
    assert [t["task_name"] for t in merged] == ["polyglot_go_y", "polyglot_java_x"]
    assert [t["reward"] for t in merged] == [1.0, 1.0]


def test_merge_trials_of_a_single_dir_matches_collect_trials(tmp_path):
    _write_trial(tmp_path, "t__1", {"task_name": "t", "verifier_result": {"rewards": {"reward": 1.0}}})

    assert merge_trials([tmp_path]) == collect_trials(tmp_path)


# ── agent 配置 ────────────────────────────────────────────────────────


def test_mycode_agent_spec_uses_the_import_path_and_provider_slug():
    spec = agent_spec("mycode", BASE_URL)

    assert spec["import_path"] == MYCODE_AGENT_IMPORT_PATH
    # 两段式是 harbor 的硬要求；provider 段决定它去哪张表里查凭据
    assert spec["model_name"] == f"{PROVIDER}/{MODEL}"
    assert spec["model_name"].startswith("openrouter/")


def test_mycode_agent_spec_carries_settings_but_never_the_key():
    spec = agent_spec("mycode", BASE_URL, settings_env={payload.THINKING_ENV: "false"})

    assert spec["env"] == {payload.THINKING_ENV: "false"}
    # 明文 key 会进 harbor 存的 config.json，所以凭据只走进程 env
    assert "api_key" not in json.dumps(spec).lower()
    assert payload.API_KEY_ENV not in json.dumps(spec)


def test_mycode_agent_spec_omits_env_when_there_are_no_settings():
    assert "env" not in agent_spec("mycode", BASE_URL)


def test_opencode_agent_spec_registers_the_model_and_a_small_model():
    spec = agent_spec("opencode", BASE_URL)

    config = spec["kwargs"]["opencode_config"]
    provider = config["provider"][PROVIDER]
    assert provider["options"]["baseURL"] == BASE_URL
    # 占位符而不是明文：harbor 会把整个 JSON echo 进 trial.log
    assert provider["options"]["apiKey"] == "{env:OPENROUTER_API_KEY}"
    assert MODEL in provider["models"]
    # 不指 small_model 的话它拿 gpt-5.4-nano-flex 去生成会话标题 → 404
    assert config["small_model"] == f"{PROVIDER}/{MODEL}"


def test_unknown_arm_is_rejected():
    with pytest.raises(ValueError):
        agent_spec("cursor", BASE_URL)


def test_settings_env_propagates_the_endpoint_switches():
    settings = settings_env_from_endpoint(
        thinking=False, thinking_feedback=True, context_window=200_000
    )

    # 容器里没有 ~/.my-code/config.json，不搬过去 MyCode 就按内置默认跑，
    # 而指标上看不出差别——这是最难查的一类偏差
    assert settings == {
        payload.THINKING_ENV: "false",
        payload.THINKING_FEEDBACK_ENV: "true",
        payload.CONTEXT_WINDOW_ENV: "200000",
    }


def test_settings_env_leaves_thinking_unset_when_the_endpoint_does():
    settings = settings_env_from_endpoint(
        thinking=None, thinking_feedback=False, context_window=None
    )

    # None = 跟随模型默认，不能翻译成 "false"，那是另一个意思
    assert payload.THINKING_ENV not in settings
    assert payload.CONTEXT_WINDOW_ENV not in settings
    assert settings[payload.THINKING_FEEDBACK_ENV] == "false"


# ── 思考开关：两臂必须对齐 ────────────────────────────────────────────


class _FakeEndpoint:
    """宿主 config.json 里 qwen3.6-27b 那条：thinking 是关的。"""

    thinking = False
    thinking_feedback = False
    context_window = 1_000_000


@pytest.fixture
def fake_endpoint(monkeypatch):
    import agents.config

    monkeypatch.setattr(agents.config, "get_endpoint_by_model", lambda m: _FakeEndpoint())


def test_thinking_default_does_not_inherit_the_host_endpoint_switch(fake_endpoint):
    # 回归：首轮 25 题就是继承了宿主的 thinking=False，于是 MyCode 臂全程不思考、
    # opencode 臂全程思考，测出的「快 1.9×」全是思考开关的差，不是 scaffold 的差。
    settings = mycode_settings_env(MODEL, "default")

    assert payload.THINKING_ENV not in settings
    # 其余开关照旧从宿主搬，它们两臂之间没有对照关系
    assert settings[payload.CONTEXT_WINDOW_ENV] == "1000000"
    assert settings[payload.THINKING_FEEDBACK_ENV] == "false"


@pytest.mark.parametrize("mode, expected", [("on", "true"), ("off", "false")])
def test_thinking_can_be_forced_both_ways(fake_endpoint, mode, expected):
    assert mycode_settings_env(MODEL, mode)[payload.THINKING_ENV] == expected


def test_run_variables_records_what_was_actually_sent(fake_endpoint):
    args = build_parser().parse_args(["--arm", "mycode", "--stage", "final"])
    settings_env = mycode_settings_env(args.model, args.thinking)

    variables = run_variables(args, settings_env)

    # 报告里没写「思考是关的」，假结论就能一路活到汇报环节——所以必须落盘
    assert variables["thinking_mode"] == "default"
    assert variables["thinking_effective"] == "跟随模型默认（未注入）"
    assert variables["seed"] == 20261017
    assert variables["model"] == MODEL
    assert variables["git_revision"]


def test_run_variables_shows_an_explicit_switch(fake_endpoint):
    args = build_parser().parse_args(["--arm", "mycode", "--thinking", "off"])

    variables = run_variables(args, mycode_settings_env(args.model, args.thinking))

    assert variables["thinking_effective"] == "false"


def test_render_markdown_prints_the_control_variables(tmp_path):
    _write_trial(tmp_path, "a__1", {"task_name": "a", "verifier_result": {"rewards": {"reward": 1.0}}})
    trials = collect_trials(tmp_path)
    summary = summarize("mycode", "final", trials)
    summary["variables"] = {"thinking_mode": "default", "git_revision": "abc1234"}

    text = render_markdown(summary, trials)

    assert "这轮的控制变量" in text
    assert "abc1234" in text
    # 变量块不能把逐题表格挤掉
    assert "| a |" in text


# ── job 配置 ──────────────────────────────────────────────────────────


def test_build_job_config_keeps_the_task_order():
    config = build_job_config(
        arm="mycode",
        tasks=["polyglot_rust_x", "polyglot_go_y", "polyglot_cpp_z"],
        dataset_dir="/data/aider_polyglot",
        host_ip="10.0.0.1",
        base_url=BASE_URL,
    )

    # 嵌套抽样（6 题是 25 题的前缀）完全依赖 harbor 按给定顺序跑
    assert config["datasets"][0]["task_names"] == [
        "polyglot_rust_x",
        "polyglot_go_y",
        "polyglot_cpp_z",
    ]
    assert config["datasets"][0]["path"] == "/data/aider_polyglot"
    assert len(config["agents"]) == 1
    assert config["environment"]["env"]["HTTPS_PROXY"].endswith(":7897")


def test_build_job_config_rejects_an_empty_task_list():
    with pytest.raises(ValueError):
        build_job_config(
            arm="mycode",
            tasks=[],
            dataset_dir="/data",
            host_ip="10.0.0.1",
            base_url=BASE_URL,
        )


def test_harbor_env_sets_pythonpath_and_credentials(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = harbor_env(api_key="sk-secret", base_url=BASE_URL)

    # 没有 PYTHONPATH，harbor 就 import 不到仓库里的适配器
    assert str(PROJECT_ROOT) in env["PYTHONPATH"].split(os.pathsep)
    assert env["MYCODE_API_KEY"] == "sk-secret"
    assert env["MYCODE_BASE_URL"] == BASE_URL
    # opencode 臂由 harbor 的 openrouter 槽位读这两个
    assert env["OPENROUTER_API_KEY"] == "sk-secret"
    assert env["OPENROUTER_BASE_URL"] == BASE_URL


def test_harbor_env_prepends_to_an_existing_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/other")
    env = harbor_env(api_key="k", base_url=BASE_URL)

    assert env["PYTHONPATH"].endswith("/other")
    assert env["PYTHONPATH"] != "/other"


# ── harbor 命令 ───────────────────────────────────────────────────────


def test_harbor_command_retries_only_network_failures():
    command = harbor_command(config_path="/c.json", jobs_dir="/jobs", n_concurrent=2)

    assert command[:2] == ["harbor", "run"]
    assert "-c" in command and "/c.json" in command
    assert "-n" in command and "2" in command
    # 网络抖动值得重试；别的失败重试只是白等 30 分钟
    assert command[command.index("--retry-include") + 1] == "NetworkConnectionError"
    assert "-y" in command
    assert "--dry-run" not in command and "--install-only" not in command


def test_harbor_command_gives_agent_install_room_past_360s():
    command = harbor_command(config_path="/c.json", jobs_dir="/jobs", n_concurrent=2)

    # opencode 装一次实测能超过 harbor 的 360s 默认值，撞上就是
    # AgentSetupTimeoutError + 一个没有判定的 trial（真丢过一道 rust 题）
    assert command[command.index("--agent-setup-timeout-multiplier") + 1] == "3"


def test_harbor_command_passes_through_the_short_circuit_flags():
    command = harbor_command(
        config_path="/c.json", jobs_dir="/j", n_concurrent=1, install_only=True
    )

    assert "--install-only" in command
    assert "--dry-run" not in command


def test_wrap_with_docker_group_collapses_to_one_shell_string():
    wrapped = wrap_with_docker_group(["harbor", "run", "-c", "/a b.json"])

    assert wrapped[:3] == ["sg", "docker", "-c"]
    # sg 只吃一个字符串，路径里的空格必须被引号保住
    assert "'/a b.json'" in wrapped[3]


# ── 题目镜像预热 ──────────────────────────────────────────────────────


def test_prewarm_command_builds_through_the_host_proxy():
    command = prewarm_command("/data/aider_polyglot/polyglot_go_poker/environment", "10.147.17.2")

    # go 题要拉 golang.org、rust 题要拉 rustup.rs，直连不通；
    # harbor 自己的 compose build 不带代理，所以这一步必须先做
    assert "--build-arg" in command
    assert "HTTPS_PROXY=http://10.147.17.2:7897" in command
    # 大小写两套：Dockerfile 里的工具认哪个不一定
    assert "https_proxy=http://10.147.17.2:7897" in command


def test_prewarm_command_keeps_domestic_mirrors_off_the_proxy():
    command = prewarm_command("/env", "10.147.17.2")
    no_proxy = command[command.index("NO_PROXY=" + NO_PROXY)]

    # apt/pip 走国内镜像直连比走代理快 40 倍，代理是整条链路最窄的地方
    assert no_proxy == f"NO_PROXY={NO_PROXY}"
    assert "mirrors.aliyun.com" in no_proxy


def test_prewarm_command_writes_cache_only_and_leaves_no_image():
    command = prewarm_command("/env", "10.147.17.2")

    # 25 道题就是 25 个镜像，不留 tag 的话每个都是悬空镜像，磁盘会被吃光
    assert command[command.index("--output") + 1] == "type=cacheonly"
    assert "-t" not in command


def test_prewarm_command_points_at_the_task_environment_dir():
    command = prewarm_command("/data/x/polyglot_rust_luhn-from/environment", "10.0.0.1")

    assert command[-1] == "/data/x/polyglot_rust_luhn-from/environment"
    assert command[:3] == ["docker", "buildx", "build"]


# ── 结果收集 ──────────────────────────────────────────────────────────


def _write_trial(run_dir: Path, name: str, body: dict) -> None:
    trial = run_dir / name
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps(body), encoding="utf-8")


def _phase(seconds: int) -> dict:
    return {
        "started_at": "2026-10-17T01:00:00.000000Z",
        "finished_at": f"2026-10-17T01:{seconds // 60}:{seconds % 60:02d}.000000Z",
    }


def test_collect_trials_reads_reward_and_tokens(tmp_path):
    _write_trial(
        tmp_path,
        "polyglot_python_two-bucket__abc",
        {
            "task_name": "polyglot_python_two-bucket",
            "trial_name": "polyglot_python_two-bucket__abc",
            "verifier_result": {"rewards": {"reward": 1.0}},
            "agent_result": {
                "n_input_tokens": 53093,
                "n_output_tokens": 762,
                "n_cache_tokens": 0,
                "metadata": {"mycode_error": None},
            },
            "agent_execution": _phase(37),
        },
    )

    trials = collect_trials(tmp_path)

    assert len(trials) == 1
    assert trials[0]["task_name"] == "polyglot_python_two-bucket"
    assert trials[0]["reward"] == 1.0
    assert trials[0]["exception_type"] is None
    assert trials[0]["n_input_tokens"] == 53093
    assert trials[0]["agent_execution_s"] == 37.0


def test_collect_trials_keeps_errored_trials_visible(tmp_path):
    # harbor 自己的 n_trials 不含出异常的 trial，直接拿它的汇总会把
    # 「根本没跑起来」当成「跑了没通过」
    _write_trial(
        tmp_path,
        "broken__x",
        {
            "task_name": "broken",
            "verifier_result": None,
            "agent_result": None,
            "exception_info": {
                "exception_type": "NonZeroAgentExitCodeError",
                "exception_message": "MyCode 没有任何产出：APIConnectionError",
            },
        },
    )

    trials = collect_trials(tmp_path)

    assert trials[0]["reward"] is None
    assert trials[0]["exception_type"] == "NonZeroAgentExitCodeError"
    assert "APIConnectionError" in trials[0]["exception_message"]


def test_collect_trials_scores_a_missing_reward_file_as_zero(tmp_path):
    # aider-polyglot 的 cpp test.sh 在 CMake 构建失败时 `exit 1`，跳过了写
    # reward.txt 的那段。C++ 里构建不过 = agent 给的 API 对不上测试文件 = 做错了，
    # 必须按 0 分计入；当异常剔掉的话，做得差的那一臂反而会因为难题被剔除而通过率虚高。
    _write_trial(
        tmp_path,
        "polyglot_cpp_x__1",
        {
            "task_name": "polyglot_cpp_x",
            "verifier_result": None,
            "exception_info": {
                "exception_type": "RewardFileNotFoundError",
                "exception_message": "No reward file found at /logs/verifier/reward.txt",
            },
        },
    )

    trial = collect_trials(tmp_path)[0]

    assert trial["reward"] == 0.0
    assert trial["no_reward_file"] is True
    # 异常类型留着当证据，报告里要能看出这个 0 是构建失败而不是测试没过
    assert trial["exception_type"] == "RewardFileNotFoundError"


def test_summarize_excludes_errored_trials_from_the_pass_rate(tmp_path):
    _write_trial(tmp_path, "a__1", {"task_name": "a", "verifier_result": {"rewards": {"reward": 1.0}}})
    _write_trial(tmp_path, "b__1", {"task_name": "b", "verifier_result": {"rewards": {"reward": 0.0}}})
    _write_trial(
        tmp_path,
        "c__1",
        {"task_name": "c", "exception_info": {"exception_type": "Boom"}},
    )

    summary = summarize("mycode", "probe", collect_trials(tmp_path))

    assert summary["n_trials"] == 3
    assert summary["n_scored"] == 2
    assert summary["n_unscored"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["mean_reward"] == 0.5


def test_summarize_counts_build_failures_inside_the_denominator(tmp_path):
    _write_trial(tmp_path, "a__1", {"task_name": "a", "verifier_result": {"rewards": {"reward": 1.0}}})
    _write_trial(
        tmp_path,
        "b__1",
        {
            "task_name": "b",
            "exception_info": {"exception_type": "RewardFileNotFoundError"},
        },
    )

    summary = summarize("mycode", "probe", collect_trials(tmp_path))

    assert summary["n_scored"] == 2
    assert summary["n_unscored"] == 0
    assert summary["n_build_failure"] == 1
    assert summary["pass_rate"] == 0.5


def test_summarize_of_nothing_is_empty_not_zero(tmp_path):
    summary = summarize("mycode", "probe", collect_trials(tmp_path))

    # 一个都没跑出来时通过率是 None（不知道），不是 0.0（全错）
    assert summary["n_scored"] == 0
    assert summary["pass_rate"] is None
    assert summary["mean_reward"] is None


def test_render_markdown_names_the_arm_and_lists_every_trial(tmp_path):
    _write_trial(tmp_path, "a__1", {"task_name": "a", "verifier_result": {"rewards": {"reward": 1.0}}})
    trials = collect_trials(tmp_path)
    text = render_markdown(summarize("opencode", "final", trials), trials)

    assert "opencode" in text and "final" in text
    assert "| a |" in text
    assert "通过率：1/1" in text


def test_latest_run_dir_ignores_older_runs(tmp_path):
    old = tmp_path / "2026-01-01__00-00-00"
    old.mkdir()
    (old / "result.json").write_text("{}", encoding="utf-8")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    new = tmp_path / "2026-10-17__00-00-00"
    new.mkdir()
    (new / "result.json").write_text("{}", encoding="utf-8")

    assert latest_run_dir(tmp_path, since=1_500_000_000) == new
    assert latest_run_dir(tmp_path, since=2_000_000_000) is None


def test_list_task_ids_needs_a_task_toml(tmp_path):
    for name in ("polyglot_python_a", "polyglot_go_b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "task.toml").write_text("version = '1.0'", encoding="utf-8")
    (tmp_path / "not-a-task").mkdir()
    (tmp_path / "stray.txt").write_text("x", encoding="utf-8")

    assert list_task_ids(tmp_path) == ["polyglot_go_b", "polyglot_python_a"]


def test_list_task_ids_points_at_the_setup_script_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        list_task_ids(tmp_path / "nope")

    assert "setup_env.sh" in str(exc.value)
