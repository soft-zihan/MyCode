"""Harbor 驱动的 coding-agent benchmark runner（MyCode vs opencode 两臂对比）。

本模块不在导入期依赖 harbor：harbor 是被 shell 出去的，所以这些函数在仓库 venv
里就能单测。harbor 侧的适配器在 `eval/harbor/mycode_agent.py`。

用法（在 WSL 里，仓库根目录）::

    .venv/bin/python -m eval.harbor.runner --arm mycode --stage probe --dry-run
    .venv/bin/python -m eval.harbor.runner --arm mycode --tasks polyglot_python_two-bucket --install-only
    .venv/bin/python -m eval.harbor.runner --arm both --stage probe
    .venv/bin/python -m eval.harbor.runner --arm both --stage final
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = PROJECT_ROOT / "eval" / "reports"

DEFAULT_DATASET_DIR = Path.home() / ".mycode" / "harbor-datasets" / "datasets" / "aider_polyglot"
DEFAULT_JOBS_DIR = Path.home() / ".mycode" / "harbor_jobs"
DEFAULT_CONFIG_DIR = Path.home() / ".mycode" / "harbor_configs"

MODEL = "qwen3.6-27b"
# 借 openrouter 这个 provider 槽位承载 cherygpt 网关：harbor 从 slug 的 provider 段
# 查凭据表，而网关不支持 openai 槽位会走的 /responses。详见 README 2.1。
PROVIDER = "openrouter"
SEED = 20261017
PROBE_N = 6
FINAL_N = 25
STAGE_SIZES = {"probe": PROBE_N, "final": FINAL_N}
ARMS = ("mycode", "opencode")

MYCODE_AGENT_IMPORT_PATH = "eval.harbor.mycode_agent:MyCodeAgent"
PROXY_PORT = 7897
PIP_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple"
PIP_TRUSTED_HOST = "mirrors.aliyun.com"
NODE_MIRROR = "https://mirrors.aliyun.com/nodejs-release"

# 每一项都是量过的：直连快就直连，直连不通才走代理（宿主代理只有 ~110 KB/s）。
# 别随手增删，见 README 1.5b 的表。
NO_PROXY = ",".join(
    (
        "localhost",
        "127.0.0.1",
        "::1",
        "10.*",
        "172.16.*",
        "172.17.*",
        "172.18.*",
        "192.168.*",
        "cherygpt.com",
        ".cherygpt.com",
        "aliyuncs.com",
        ".aliyuncs.com",
        "mirrors.aliyun.com",
        "mirrors.tuna.tsinghua.edu.cn",
        "pypi.tuna.tsinghua.edu.cn",
        "registry.npmmirror.com",
        "archive.ubuntu.com",
        "security.ubuntu.com",
        # deadsnakes PPA：python 题的 sources.list 里有它，agent 安装期的
        # `apt-get update` 每次都会打这个域名。走代理实测会被 503 打断，
        # 整个 trial 记异常（agent 一行都没跑）；直连 200 / 7.2s，稳定。
        "launchpad.net",
        ".launchpad.net",
        "launchpadcontent.net",
        ".launchpadcontent.net",
        "keyserver.ubuntu.com",
        "registry.npmjs.org",
        "nodejs.org",
        "deb.nodesource.com",
        "npmjs.org",
        ".npmjs.org",
    )
)

_TASK_PREFIX = "polyglot"

# JVM 的 `http.nonProxyHosts` 用 `|` 分隔、通配只认前导 `*`，语法和 NO_PROXY 不一样，
# 所以单独列一份而不是做字符串转换：转错了不会报错，只会让某些请求悄悄走错路。
# 语义与 NO_PROXY 一致——国内和本机直连，境外（gradle.org / maven.apache.org）走代理。
JAVA_NON_PROXY_HOSTS = "|".join(
    (
        "localhost",
        "127.0.0.1",
        "*.cherygpt.com",
        "*.aliyun.com",
        "*.aliyuncs.com",
        "*.tsinghua.edu.cn",
        "*.npmmirror.com",
        "*.ubuntu.com",
        "*.launchpad.net",
        "*.launchpadcontent.net",
    )
)


# ── 选题 ──────────────────────────────────────────────────────────────


def task_language(task_name: str) -> str | None:
    """从 `polyglot_<lang>_<exercise>` 里取出语言；不符合这个形状就返回 None。"""
    parts = task_name.split("_")
    if len(parts) < 3 or parts[0] != _TASK_PREFIX:
        return None
    return parts[1]


def select_tasks(task_ids: Sequence[str], n: int, seed: int) -> list[str]:
    """按语言均衡轮转选出 n 道题。

    返回的是一个与 n 无关的固定序列的前 n 项，所以 `select(ids, 25, s)[:6]`
    必然等于 `select(ids, 6, s)` —— 探测批次是正式批次的真子集，两批数据可以合并统计。

    每轮从每种语言各取一道，语言顺序按「该语言总题数降序、同数按字母序」，
    于是凑不满一轮时多出来的名额落在题最多的语言上，接近按语言比例分配。
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")

    by_lang: dict[str, list[str]] = {}
    unknown: list[str] = []
    for task_id in task_ids:
        lang = task_language(task_id)
        if lang is None:
            unknown.append(task_id)
        else:
            by_lang.setdefault(lang, []).append(task_id)

    rng = random.Random(seed)
    # 按语言名排序后再洗牌：dict 的插入顺序取决于 task_ids 的顺序，
    # 直接遍历会让 rng 的消耗顺序随之改变，同一个 seed 就选不出同一批题。
    for lang in sorted(by_lang):
        pool = by_lang[lang]
        pool.sort()
        rng.shuffle(pool)

    ordered: list[str] = []
    for round_index in range(max((len(p) for p in by_lang.values()), default=0)):
        for lang in sorted(by_lang, key=lambda name: (-len(by_lang[name]), name)):
            pool = by_lang[lang]
            if round_index < len(pool):
                ordered.append(pool[round_index])

    ordered.extend(sorted(unknown))
    return ordered[:n]


def list_task_ids(dataset_dir: Path | str) -> list[str]:
    """数据集目录里每个含 task.toml 的子目录就是一道题。"""
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.is_dir():
        raise FileNotFoundError(
            f"数据集目录不存在：{dataset_dir}\n先跑 eval/harbor/setup_env.sh 把它下载下来"
        )
    return sorted(
        path.name for path in dataset_dir.iterdir() if (path / "task.toml").is_file()
    )


# ── 配置生成 ──────────────────────────────────────────────────────────


def detect_host_ip() -> str:
    """宿主（Windows）的 LAN IP —— WSL 是 mirrored 网络，这个地址在 WSL 里同样存在，
    容器靠它回到宿主的代理。DHCP 会变，所以每次跑重新探测，不写死。
    """
    result = subprocess.run(
        ["hostname", "-I"], capture_output=True, text=True, check=True
    )
    for token in result.stdout.split():
        if token.startswith(("10.", "192.168.")):
            return token
    raise RuntimeError(f"没找到 LAN IP：`hostname -I` 返回 {result.stdout!r}")


def container_env(host_ip: str, proxy_port: int = PROXY_PORT) -> dict[str, str]:
    """题目容器一起来就带上的环境变量（走 JobConfig 的 `environment.env`）。

    这是唯一能把变量送进容器的口子：宿主 env 不会漏进去，`--env-file` 只作用于
    harbor 宿主进程。见 README 1.5。
    """
    proxy = f"http://{host_ip}:{proxy_port}"
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": NO_PROXY,
        "no_proxy": NO_PROXY,
        "PIP_INDEX_URL": PIP_INDEX_URL,
        "PIP_TRUSTED_HOST": PIP_TRUSTED_HOST,
        "UV_DEFAULT_INDEX": PIP_INDEX_URL,
        "NVM_NODEJS_ORG_MIRROR": NODE_MIRROR,
        # JVM **不认** HTTP(S)_PROXY 这些环境变量，只认系统属性。java 题的 verifier
        # 用 Gradle wrapper 下发行版，不设这两个属性它就走直连，实测两种死法：
        # PKIX 验不过（拿到假证书）和 openServer 连不上。加上之后 gradle 8.7 正常下完。
        "JAVA_TOOL_OPTIONS": (
            f"-Dhttps.proxyHost={host_ip} -Dhttps.proxyPort={proxy_port} "
            f"-Dhttp.proxyHost={host_ip} -Dhttp.proxyPort={proxy_port} "
            f"-Dhttp.nonProxyHosts={JAVA_NON_PROXY_HOSTS}"
        ),
    }


def opencode_config(base_url: str, model: str = MODEL) -> dict[str, Any]:
    """opencode 臂的 provider 配置。

    三件事都是必须的（详见 README 2.1）：显式注册模型、把 baseURL 塞进
    `provider.<p>.options`、以及把 small_model 也指过来（否则它拿
    gpt-5.4-nano-flex 去生成会话标题 → 404）。apiKey 用 opencode 自己的
    `{env:...}` 占位符，不落明文：harbor 会把整个 JSON echo 进 trial.log。
    """
    return {
        "provider": {
            PROVIDER: {
                "options": {
                    "baseURL": base_url,
                    "apiKey": "{env:OPENROUTER_API_KEY}",
                },
                "models": {model: {"name": model}},
            }
        },
        "small_model": f"{PROVIDER}/{model}",
    }


def agent_spec(
    arm: str, base_url: str, model: str = MODEL, settings_env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """一条 agent 配置。model_name 两段式，provider 段决定 harbor 去哪查凭据。"""
    model_name = f"{PROVIDER}/{model}"
    if arm == "mycode":
        spec: dict[str, Any] = {
            "import_path": MYCODE_AGENT_IMPORT_PATH,
            "model_name": model_name,
        }
        # 凭据不在这里：适配器从 harbor 进程的 env 解析后按次注入容器 exec，
        # 这样明文 key 既不进配置文件也不进 harbor 存的 config.json。
        # 这里只放非机密的行为开关——harbor 会把 agents[].env 叠进 agent 阶段
        # 每一次容器 exec（trial.py:1718 scoped_exec_env）。
        if settings_env:
            spec["env"] = dict(settings_env)
        return spec
    if arm == "opencode":
        return {
            "name": "opencode",
            "model_name": model_name,
            "kwargs": {"opencode_config": opencode_config(base_url, model)},
        }
    raise ValueError(f"未知的臂：{arm!r}（可选 {', '.join(ARMS)}）")


def build_job_config(
    *,
    arm: str,
    tasks: Sequence[str],
    dataset_dir: Path | str,
    host_ip: str,
    base_url: str,
    model: str = MODEL,
    settings_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not tasks:
        raise ValueError("tasks 为空：没有题目可跑")
    return {
        "agents": [agent_spec(arm, base_url, model, settings_env)],
        "datasets": [{"path": str(dataset_dir), "task_names": list(tasks)}],
        "environment": {"env": container_env(host_ip)},
    }


def harbor_command(
    *,
    config_path: Path | str,
    jobs_dir: Path | str,
    n_concurrent: int,
    install_only: bool = False,
    dry_run: bool = False,
) -> list[str]:
    command = [
        "harbor",
        "run",
        "-c",
        str(config_path),
        "-o",
        str(jobs_dir),
        "-n",
        str(n_concurrent),
        # opencode 的安装（apt nodejs+npm → nvm 装 node22 → npm i -g opencode-ai）
        # 会超过 harbor 的 360s 默认值：实测 polyglot_rust_scale-generator 那次
        # 360.3s 撞上 AgentSetupTimeoutError，trial 直接没有判定。×3 = 1080s，
        # 留足余量又不至于把一个真卡住的安装拖成一小时。
        # MyCode 适配器自己声明了 900s，乘完是 2700s，无害。
        "--agent-setup-timeout-multiplier",
        "3",
        # 网络抖动值得重试一次；只重试网络类异常，别的失败重试也是白等
        "--max-retries",
        "1",
        "--retry-include",
        "NetworkConnectionError",
        "-y",
    ]
    if install_only:
        command.append("--install-only")
    if dry_run:
        command.append("--dry-run")
    return command


def harbor_env(*, api_key: str, base_url: str) -> dict[str, str]:
    """跑 harbor 那个进程的环境。

    凭据只走 env，不写进配置文件：harbor 会把 job 配置原样存成 config.json，
    也会把 agent 的启动命令印进 trial.log。
    `PYTHONPATH` 让 harbor 能 import 到仓库里的 MyCode 适配器。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    # mycode 臂读 MYCODE_*，opencode 臂由 harbor 的 openrouter 槽位读 OPENROUTER_*
    env["MYCODE_API_KEY"] = api_key
    env["MYCODE_BASE_URL"] = base_url
    env["OPENROUTER_API_KEY"] = api_key
    env["OPENROUTER_BASE_URL"] = base_url
    return env


def needs_docker_group() -> bool:
    """当前 shell 能不能直接访问 docker daemon。

    用户可能在 docker 组里但当前会话的组还没生效，这时候要用 `sg docker -c`。
    """
    return (
        subprocess.run(
            ["docker", "info"], capture_output=True, check=False
        ).returncode
        != 0
    )


def wrap_with_docker_group(command: Sequence[str]) -> list[str]:
    return ["sg", "docker", "-c", shlex.join(command)]


# ── 题目镜像预热 ──────────────────────────────────────────────────────


def prewarm_command(environment_dir: Path | str, host_ip: str, proxy_port: int = PROXY_PORT) -> list[str]:
    """构建一道题的镜像，但只写进 BuildKit 缓存、不产出镜像。

    为什么需要这一步：go 题要从 `golang.org/dl` 拉 66 MB、rust 题要从
    `sh.rustup.rs` 装工具链，这些域名直连不通；而 harbor 的 `docker compose build`
    不带代理，于是 `curl` 以 28（超时）失败，trial 直接记 RuntimeError。
    dockerd 的 systemd 代理 drop-in 只管镜像拉取，管不到 RUN 步骤。

    实测（README 2.7）：预定义的代理 build-arg 不进 BuildKit 的缓存键，所以这里
    带着代理构建一次之后，harbor 之后不带参数的构建会直接命中 `CACHED`。
    `--output type=cacheonly` 让它只落缓存，不留 25 个悬空镜像。
    """
    proxy = f"http://{host_ip}:{proxy_port}"
    return [
        "docker",
        "buildx",
        "build",
        "--output",
        "type=cacheonly",
        *(
            arg
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
            for arg in ("--build-arg", f"{name}={proxy}")
        ),
        *(
            arg
            for name in ("NO_PROXY", "no_proxy")
            for arg in ("--build-arg", f"{name}={NO_PROXY}")
        ),
        str(environment_dir),
    ]


def prewarm_images(
    *,
    tasks: Sequence[str],
    dataset_dir: Path | str,
    host_ip: str,
    jobs: int = 2,
    use_docker_group: bool = False,
) -> list[str]:
    """按题目顺序预热镜像，返回构建失败的题目名（失败不致命，harbor 会再试一次）。"""
    dataset_dir = Path(dataset_dir)

    def build(name: str) -> tuple[str, int, str]:
        environment_dir = dataset_dir / name / "environment"
        if not (environment_dir / "Dockerfile").is_file():
            return name, 1, f"{environment_dir} 里没有 Dockerfile"
        command = prewarm_command(environment_dir, host_ip)
        if use_docker_group:
            command = wrap_with_docker_group(command)
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return name, completed.returncode, tail[-1] if tail else ""

    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(build, name): name for name in tasks}
        for future in as_completed(futures):
            name, returncode, detail = future.result()
            if returncode == 0:
                print(f"  镜像就绪 {name}")
            else:
                failed.append(name)
                print(f"  镜像失败 {name}: {detail}", file=sys.stderr)
    return sorted(failed)


# ── 结果收集 ──────────────────────────────────────────────────────────


def _phase_seconds(phase: Any) -> float | None:
    if not isinstance(phase, dict):
        return None
    started, finished = phase.get("started_at"), phase.get("finished_at")
    if not started or not finished:
        return None
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    try:
        delta = datetime.strptime(finished, fmt) - datetime.strptime(started, fmt)
    except ValueError:
        return None
    return round(delta.total_seconds(), 1)


# aider-polyglot 的 cpp `tests/test.sh` 在 CMake 构建失败时直接 `exit 1`，跳过了脚本
# 末尾写 reward.txt 的那一段，harbor 于是抛 RewardFileNotFoundError。而 C++ 里
# 「构建不过」几乎总是因为 agent 给出的 API 对不上测试文件——那是**做错了**，
# 不是基础设施故障，必须按 reward 0 计分。
# 若把它当异常剔掉，做得差的那一臂反而会因为「难题被剔除」而通过率虚高。
NO_REWARD_FILE_EXCEPTIONS = frozenset({"RewardFileNotFoundError"})


def collect_trials(run_dir: Path | str) -> list[dict[str, Any]]:
    """把一个 job 目录下每个 trial 的 result.json 摊平成一行。

    `reward` 是唯一的判分依据：None = 这次 trial 没有可用判定（不计入分母），
    数值 = 判过分。出异常的 trial 只要 reward 还是 None 就不进分母，
    这样「跑了没通过」和「根本没跑起来」不会互相冒充。
    """
    run_dir = Path(run_dir)
    trials: list[dict[str, Any]] = []
    for result_path in sorted(run_dir.glob("*/result.json")):
        data = json.loads(result_path.read_text(encoding="utf-8"))
        verifier = data.get("verifier_result") or {}
        rewards = verifier.get("rewards") or {}
        agent_result = data.get("agent_result") or {}
        exception = data.get("exception_info") or {}
        exception_type = exception.get("exception_type")
        reward = rewards.get("reward")
        no_reward_file = exception_type in NO_REWARD_FILE_EXCEPTIONS
        if reward is None and no_reward_file:
            reward = 0.0
        trials.append(
            {
                "task_name": data.get("task_name"),
                "trial_name": data.get("trial_name"),
                "reward": reward,
                "no_reward_file": no_reward_file,
                "exception_type": exception_type,
                "exception_message": (exception.get("exception_message") or "")[:400],
                "environment_setup_s": _phase_seconds(data.get("environment_setup")),
                "agent_setup_s": _phase_seconds(data.get("agent_setup")),
                "agent_execution_s": _phase_seconds(data.get("agent_execution")),
                "verifier_s": _phase_seconds(data.get("verifier")),
                "n_input_tokens": agent_result.get("n_input_tokens"),
                "n_output_tokens": agent_result.get("n_output_tokens"),
                "n_cache_tokens": agent_result.get("n_cache_tokens"),
                "agent_metadata": agent_result.get("metadata"),
                "result_path": str(result_path),
            }
        )
    return trials


def summarize(
    arm: str, stage: str, trials: Sequence[dict[str, Any]], model: str = MODEL
) -> dict[str, Any]:
    scored = [t for t in trials if t["reward"] is not None]
    passed = [t for t in scored if (t["reward"] or 0) >= 1.0]
    return {
        "arm": arm,
        "stage": stage,
        "model": model,
        "n_trials": len(trials),
        "n_scored": len(scored),
        "n_unscored": len(trials) - len(scored),
        "n_build_failure": sum(1 for t in trials if t.get("no_reward_file")),
        "n_passed": len(passed),
        "pass_rate": round(len(passed) / len(scored), 4) if scored else None,
        "mean_reward": (
            round(sum(t["reward"] or 0 for t in scored) / len(scored), 4) if scored else None
        ),
        "total_input_tokens": sum(t["n_input_tokens"] or 0 for t in trials),
        "total_output_tokens": sum(t["n_output_tokens"] or 0 for t in trials),
        "total_cache_tokens": sum(t["n_cache_tokens"] or 0 for t in trials),
        "mean_agent_execution_s": (
            round(
                sum(t["agent_execution_s"] or 0 for t in scored) / len(scored), 1
            )
            if scored
            else None
        ),
    }


def render_markdown(summary: dict[str, Any], trials: Sequence[dict[str, Any]]) -> str:
    lines = [
        f"# harbor {summary['arm']} / {summary['stage']} / {summary['model']}",
        "",
        f"- 通过率：{summary['n_passed']}/{summary['n_scored']}"
        f"（{summary['pass_rate']}），另有 {summary['n_unscored']} 个 trial 没有可用判定",
        f"- 其中 {summary['n_build_failure']} 个是「verifier 没写出 reward 文件」"
        "（cpp 构建不过），按 0 分计入",
        f"- 平均 reward：{summary['mean_reward']}",
        f"- token：input {summary['total_input_tokens']}"
        f"（其中 cache {summary['total_cache_tokens']}）"
        f" / output {summary['total_output_tokens']}",
        f"- 平均 agent 执行时长：{summary['mean_agent_execution_s']}s",
        "",
    ]
    if variables := summary.get("variables"):
        lines.append("## 这轮的控制变量")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(variables, indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
    lines += [
        "| 题目 | reward | 异常 | agent 执行 | 装 agent | input | output |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for trial in trials:
        reward = trial["reward"]
        if trial.get("no_reward_file"):
            reward = f"{reward}（构建失败）"
        lines.append(
            "| {task} | {reward} | {exc} | {exec_s} | {setup_s} | {inp} | {out} |".format(
                task=trial["task_name"],
                reward=reward,
                exc=trial["exception_type"] or "",
                exec_s=trial["agent_execution_s"],
                setup_s=trial["agent_setup_s"],
                inp=trial["n_input_tokens"],
                out=trial["n_output_tokens"],
            )
        )
    return "\n".join(lines) + "\n"


def merge_trials(run_dirs: Sequence[Path | str]) -> list[dict[str, Any]]:
    """把多个 job 目录的 trial 合成一份，同名题目**后面的目录覆盖前面的**。

    用途：某一类题因为环境问题整批废掉（java 题的 Gradle 下载），修好之后只补跑
    那几道，再和之前那份大跑的结果合起来——不必为了 4 道题重跑 50 个 trial。
    覆盖而不是追加，否则同名题会被计两次。
    """
    merged: dict[str, dict[str, Any]] = {}
    for run_dir in run_dirs:
        for trial in collect_trials(run_dir):
            key = trial["task_name"] or trial["trial_name"] or trial["result_path"]
            merged[key] = trial
    return [merged[key] for key in sorted(merged)]


def latest_run_dir(jobs_dir: Path | str, since: float) -> Path | None:
    """harbor 自己在 jobs_dir 下建时间戳子目录，取这次跑出来的那个。"""
    jobs_dir = Path(jobs_dir)
    if not jobs_dir.is_dir():
        return None
    candidates = [
        path
        for path in jobs_dir.iterdir()
        if path.is_dir() and (path / "result.json").is_file() and path.stat().st_mtime >= since
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


# ── 入口 ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval.harbor.runner",
        description="aider-polyglot 上的 MyCode vs opencode 两臂对比（harbor 驱动）",
    )
    parser.add_argument("--arm", default="both", choices=[*ARMS, "both"])
    parser.add_argument(
        "--stage",
        default="probe",
        choices=sorted(STAGE_SIZES),
        help=f"probe={PROBE_N} 题（探测），final={FINAL_N} 题（正式数据，probe 是它的子集）",
    )
    parser.add_argument(
        "--tasks",
        default=None,
        help="显式题目名，逗号分隔；给了就忽略 --stage 的抽样",
    )
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument(
        "--thinking",
        default="default",
        choices=["default", "on", "off"],
        help="MyCode 臂的思考开关。default=不注入 MYCODE_THINKING，跟随模型默认"
        "（qwen3.6-27b 默认是开）——这才是和 opencode 臂对齐的设置；"
        "on/off=强制。别用宿主端点配置里的值：那曾让 MyCode 臂全程不思考、"
        "而 opencode 臂全程思考，把「快 1.9×」这个假结论测了出来（README §5.6）",
    )
    parser.add_argument("--dataset-dir", default=str(DEFAULT_DATASET_DIR))
    parser.add_argument("--jobs-dir", default=str(DEFAULT_JOBS_DIR))
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    parser.add_argument("--n-concurrent", type=int, default=2)
    parser.add_argument("--install-only", action="store_true", help="只装 agent，不跑题")
    parser.add_argument("--dry-run", action="store_true", help="只让 harbor 校验配置")
    parser.add_argument(
        "--host-ip",
        default=None,
        help="覆盖自动探测的宿主 LAN IP（容器靠它回到宿主代理）",
    )
    parser.add_argument(
        "--no-report", action="store_true", help="不写 eval/reports（配 --dry-run 用）"
    )
    parser.add_argument(
        "--no-prewarm",
        action="store_true",
        help="跳过题目镜像预热（go/rust 题的镜像构建要走代理，不预热会超时失败）",
    )
    parser.add_argument(
        "--prewarm-jobs", type=int, default=2, help="预热时并行构建几个镜像（默认 2）"
    )
    parser.add_argument(
        "--run-dir",
        action="append",
        default=None,
        metavar="DIR",
        help="不跑 harbor，只把这个已有的 job 目录重新出报告（判分口径改了之后用）。"
        "可重复给：多个目录合并，同名题目以后给的为准",
    )
    return parser


def _resolve_credentials(model: str) -> tuple[str, str]:
    """复用项目自己的端点解析，别再抄一遍 config.json 的结构。"""
    from eval.common.runner_base import resolve_model_config

    _, api_base, api_key = resolve_model_config(model)
    return api_base, api_key


def settings_env_from_endpoint(
    *, thinking: bool | None, thinking_feedback: bool, context_window: int | None
) -> dict[str, str]:
    """端点设置 → 容器 env。拆成纯函数是为了能单测（不依赖宿主的 config.json）。"""
    from eval.harbor import payload

    settings: dict[str, str] = {}
    if thinking is not None:
        settings[payload.THINKING_ENV] = str(thinking).lower()
    settings[payload.THINKING_FEEDBACK_ENV] = str(bool(thinking_feedback)).lower()
    if context_window:
        settings[payload.CONTEXT_WINDOW_ENV] = str(context_window)
    return settings


def mycode_settings_env(model: str, thinking_mode: str = "default") -> dict[str, str]:
    """MyCode 臂要注进容器的行为开关。

    容器里没有 `~/.my-code/config.json`，不注就退回内置默认值。除了思考开关，其余
    项仍从宿主端点配置搬（`thinking_feedback`、`context_window`）。

    思考开关**刻意不继承**宿主配置：qwen3.6-27b 的端点配置是 `thinking=False`，
    而 `enable_thinking:false` 是真的生效的（实测思考 token 从 3780 掉到 0、
    单次调用墙钟从 66.7s 掉到 7.8s）。opencode 臂不发这个参数、走模型默认（思考开），
    所以继承宿主配置会让两臂差一个「思考开关」变量——首轮 25 题就是这么翻车的，
    测出的「快 1.9×」全是思考差、不是 scaffold 差（README §5.6）。
    `default` = 不注 `MYCODE_THINKING` → `thinking=None` → 不发 `enable_thinking`
    → 跟 opencode 臂一样走模型默认。
    """
    from agents.config import get_endpoint_by_model

    endpoint = get_endpoint_by_model(model)
    if endpoint is None:
        return {}
    thinking = {"default": None, "on": True, "off": False}[thinking_mode]
    return settings_env_from_endpoint(
        thinking=thinking,
        thinking_feedback=endpoint.thinking_feedback,
        context_window=endpoint.context_window,
    )


def git_revision() -> str:
    """这轮跑的是哪个版本的代码——没有它，两次运行就没法当基线对比。"""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if head.returncode != 0:
        return "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    suffix = "-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else ""
    return head.stdout.strip() + suffix


def run_variables(
    args: argparse.Namespace, settings_env: Mapping[str, str] | None
) -> dict[str, Any]:
    """这一轮的控制变量，写进报告的 `summary.variables`。

    不记下来，两次运行就没法当基线对比、结论也没法复核：首轮 25 题的报告里没写
    「MyCode 臂的思考是关的」，于是「快 1.9×」这个假结论一路活到了汇报环节。
    """
    from eval.harbor import payload

    env = dict(settings_env or {})
    return {
        "model": args.model,
        "provider": PROVIDER,
        "seed": args.seed,
        "stage": args.stage,
        "thinking_mode": args.thinking,
        "thinking_effective": env.get(payload.THINKING_ENV) or "跟随模型默认（未注入）",
        "mycode_env": env,
        "git_revision": git_revision(),
    }


def write_report(
    *,
    arm: str,
    stage: str,
    model: str,
    trials: Sequence[dict[str, Any]],
    run_dirs: Sequence[Path | str],
    tasks: Sequence[str],
    seed: int,
    stamp: str,
    config_path: Path | None = None,
    variables: Mapping[str, Any] | None = None,
) -> Path:
    """把一批 trial 出成报告（.json 给统计用，.md 给人看）。"""
    summary = summarize(arm, stage, trials, model=model)
    summary["run_dir"] = ", ".join(str(d) for d in run_dirs)
    summary["config_path"] = str(config_path) if config_path else None
    summary["tasks"] = list(tasks)
    summary["seed"] = seed
    summary["variables"] = dict(variables or {})

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"harbor_{arm}_{stage}_{stamp}"
    (REPORTS_DIR / f"{stem}.json").write_text(
        json.dumps({"summary": summary, "trials": trials}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (REPORTS_DIR / f"{stem}.md").write_text(render_markdown(summary, trials), encoding="utf-8")

    print(
        "[{arm}] 通过率 {passed}/{scored}（无判定 {unscored}，其中构建失败 {build}）→ {report}".format(
            arm=arm,
            passed=summary["n_passed"],
            scored=summary["n_scored"],
            unscored=summary["n_unscored"],
            build=summary["n_build_failure"],
            report=REPORTS_DIR / f"{stem}.md",
        )
    )
    return REPORTS_DIR / f"{stem}.md"


def run_arm(
    *,
    arm: str,
    tasks: Sequence[str],
    args: argparse.Namespace,
    base_url: str,
    api_key: str,
    settings_env: Mapping[str, str] | None = None,
) -> int:
    stamp = datetime.now().strftime("%Y%m%d__%H%M%S")

    dataset_dir = Path(args.dataset_dir).expanduser()
    jobs_dir = Path(args.jobs_dir).expanduser()
    config_dir = Path(args.config_dir).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

    config_path = config_dir / f"{stamp}-{arm}-{args.stage}.json"
    config = build_job_config(
        arm=arm,
        tasks=tasks,
        dataset_dir=dataset_dir,
        host_ip=args.host_ip or detect_host_ip(),
        base_url=base_url,
        model=args.model,
        settings_env=settings_env if arm == "mycode" else None,
    )
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")

    command = harbor_command(
        config_path=config_path,
        jobs_dir=jobs_dir,
        n_concurrent=args.n_concurrent,
        install_only=args.install_only,
        dry_run=args.dry_run,
    )
    if needs_docker_group():
        command = wrap_with_docker_group(command)

    print(f"[{arm}] {len(tasks)} 题 → {config_path}")
    print(f"[{arm}] $ {shlex.join(command)}")
    started = time.time()
    completed = subprocess.run(command, env=harbor_env(api_key=api_key, base_url=base_url))
    if completed.returncode != 0:
        print(f"[{arm}] harbor 退出码 {completed.returncode}", file=sys.stderr)

    if args.dry_run or args.install_only or args.no_report:
        return completed.returncode

    run_dir = latest_run_dir(jobs_dir, since=started - 5)
    if run_dir is None:
        print(f"[{arm}] 在 {jobs_dir} 里没找到这次的结果目录", file=sys.stderr)
        return completed.returncode or 1

    write_report(
        arm=arm,
        stage=args.stage,
        model=args.model,
        trials=collect_trials(run_dir),
        run_dirs=[run_dir],
        tasks=tasks,
        seed=args.seed,
        stamp=stamp,
        config_path=config_path,
        variables=run_variables(args, settings_env),
    )
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 输出重定向到文件时 Python 是全缓冲的，一整轮跑一两个小时会一个字都看不到
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    if args.run_dir:
        return reprocess_run_dir(args)

    dataset_dir = Path(args.dataset_dir).expanduser()
    if args.tasks:
        tasks = [name.strip() for name in args.tasks.split(",") if name.strip()]
        unknown = [name for name in tasks if not (dataset_dir / name).is_dir()]
        if unknown:
            raise SystemExit(f"数据集里没有这些题目：{', '.join(unknown)}")
    else:
        tasks = select_tasks(list_task_ids(dataset_dir), STAGE_SIZES[args.stage], args.seed)

    base_url, api_key = _resolve_credentials(args.model)
    settings_env = mycode_settings_env(args.model, args.thinking)
    print(
        f"MyCode 臂行为开关：--thinking {args.thinking} → {settings_env or '（全不注入）'}"
    )
    arms = list(ARMS) if args.arm == "both" else [args.arm]

    if not args.dry_run and not args.no_prewarm:
        host_ip = args.host_ip or detect_host_ip()
        args.host_ip = host_ip
        print(f"预热 {len(tasks)} 个题目镜像（并发 {args.prewarm_jobs}）…")
        failed = prewarm_images(
            tasks=tasks,
            dataset_dir=dataset_dir,
            host_ip=host_ip,
            jobs=args.prewarm_jobs,
            use_docker_group=needs_docker_group(),
        )
        if failed:
            print(
                f"这些题目的镜像没构建出来，harbor 会再试一次（大概率还是失败）：{', '.join(failed)}",
                file=sys.stderr,
            )

    return_code = 0
    for arm in arms:
        return_code |= run_arm(
            arm=arm,
            tasks=tasks,
            args=args,
            base_url=base_url,
            api_key=api_key,
            settings_env=settings_env,
        )
    return return_code


def reprocess_run_dir(args: argparse.Namespace) -> int:
    """不跑 harbor，只把已有的 job 目录按当前判分口径重新出报告。

    `--run-dir` 可以重复给：多个目录会合并，同名题目以**后给的**为准。
    """
    run_dirs = [Path(d).expanduser() for d in args.run_dir]
    for run_dir in run_dirs:
        if not (run_dir / "result.json").is_file():
            raise SystemExit(f"{run_dir} 不像是一个 harbor job 目录（没有 result.json）")
    if args.arm == "both":
        raise SystemExit("--run-dir 只能配单个 --arm：一个 job 目录只属于一臂")

    trials = merge_trials(run_dirs)
    tasks = sorted(t["task_name"] for t in trials if t.get("task_name"))
    write_report(
        arm=args.arm,
        stage=args.stage,
        model=args.model,
        trials=trials,
        run_dirs=run_dirs,
        tasks=tasks,
        seed=args.seed,
        stamp=datetime.now().strftime("%Y%m%d__%H%M%S"),
        variables={
            # 重新出报告时并没有重跑，所以这轮的开关设置不代表数据是怎么跑出来的。
            # 真实的控制变量要看原始 run 目录里的 config.json。
            "reprocessed": True,
            "model": args.model,
            "seed": args.seed,
            "stage": args.stage,
            "git_revision": git_revision(),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
