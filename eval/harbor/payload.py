"""把 MyCode 送进题目容器所需的东西算出来：payload 清单、安装命令、启动命令。

这个模块不 import harbor，所以单测在仓库 venv 里就能跑。
harbor 侧的适配在 `eval/harbor/mycode_agent.py`。
"""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

CONTAINER_ROOT = "/opt/mycode"
VENV_PYTHON = f"{CONTAINER_ROOT}/.venv/bin/python"
INSTRUCTION_PATH = "/tmp/mycode_instruction.md"
RESULT_PATH = "/tmp/mycode_agent_result.json"

# 不复用 OPENAI_API_KEY / OPENAI_BASE_URL：那两个名字是 harbor 给 opencode 臂解析
# 凭据用的，两臂共用会让「谁在读哪个凭据」变含糊。
API_KEY_ENV = "MYCODE_API_KEY"
BASE_URL_ENV = "MYCODE_BASE_URL"

# MyCode 的行为开关。容器里没有 ~/.my-code/config.json，端点上的 thinking /
# 窗口大小不会生效，会退回内置默认值——今天默认值恰好和 qwen3.6-27b 的端点配置
# 一样，但那是巧合，换个模型就会悄悄跑成另一套设置。所以由 runner 从宿主端点配置
# 里读出来，经 harbor 的 agents[].env 显式送进容器。
THINKING_ENV = "MYCODE_THINKING"
THINKING_FEEDBACK_ENV = "MYCODE_THINKING_FEEDBACK"
CONTEXT_WINDOW_ENV = "MYCODE_CONTEXT_WINDOW"

# agents/prompts 下的 .txt 和 SKILL.md 是运行时必需的数据文件，所以按目录整棵收，
# 不能只挑 .py。eval 是命名空间包（没有 eval/__init__.py），只收真正要用的两个子包。
PAYLOAD_DIRS: tuple[str, ...] = ("agents", "eval/common", "eval/harbor")
PAYLOAD_FILES: tuple[str, ...] = ("requirements.txt",)

_EXCLUDE_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _is_excluded(relative: Path) -> bool:
    return bool(_EXCLUDE_DIR_NAMES.intersection(relative.parts)) or (
        relative.suffix in _EXCLUDE_SUFFIXES
    )


def payload_members(root: Path | str) -> list[Path]:
    """列出要送进容器的文件（相对 root 的路径，按 posix 字符串排序）。

    声明过的目录或文件缺失就直接报错——少送一个 prompt 模板，容器里会以
    一个莫名其妙的运行时错误暴露出来，比这里报错难查得多。
    """
    root = Path(root)
    missing = [
        name
        for name in (*PAYLOAD_DIRS, *PAYLOAD_FILES)
        if not (root / name).exists()
    ]
    if missing:
        raise FileNotFoundError(
            f"payload 缺少必需条目: {', '.join(missing)}（root={root}）"
        )

    members: list[Path] = []
    for directory in PAYLOAD_DIRS:
        base = root / directory
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if not _is_excluded(relative):
                members.append(relative)
    for filename in PAYLOAD_FILES:
        members.append(Path(filename))

    return sorted(set(members), key=lambda p: p.as_posix())


def stage_payload(root: Path | str, dest: Path | str) -> int:
    """把 payload 复制成 dest 下的一棵树，返回文件数。

    先落到宿主的一个干净暂存目录再整棵上传，是为了不把 __pycache__ 和
    frontend/（406 MB）这类东西带进容器。
    """
    root, dest = Path(root), Path(dest)
    members = payload_members(root)
    for relative in members:
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
    return len(members)


def install_commands(container_root: str = CONTAINER_ROOT) -> list[str]:
    """容器内安装 MyCode 的命令序列。

    装进 venv 而不是系统 python：题目镜像自带的 python 是被测语言环境的一部分
    （python 题会把 python3.11 设成默认），往里塞 MyCode 的依赖会污染它。
    """
    requirements = f"{container_root}/requirements.txt"
    return [
        f"python3 -m venv {container_root}/.venv",
        f"{container_root}/.venv/bin/pip install --no-cache-dir -q -r {requirements}",
    ]


def run_command(
    *,
    model: str,
    workdir: str,
    timeout_s: int = 0,
    container_root: str = CONTAINER_ROOT,
    instruction_path: str = INSTRUCTION_PATH,
    result_path: str = RESULT_PATH,
) -> str:
    """容器内启动一次 agent 执行的命令。

    题目正文走文件传（--instruction-file），不塞进命令行：aider-polyglot 的
    instruction 最长有 12896 字符，走 argv 既容易撞上长度限制，也会把整道题
    原文印进 harbor 的 trial.log。
    """
    parts: Sequence[str] = (
        f"cd {shlex.quote(container_root)}",
        f"PYTHONPATH={shlex.quote(container_root)} {container_root}/.venv/bin/python"
        " -m eval.harbor.agent_main"
        f" --model {shlex.quote(model)}"
        f" --workdir {shlex.quote(workdir)}"
        f" --instruction-file {shlex.quote(instruction_path)}"
        f" --result {shlex.quote(result_path)}"
        + (f" --timeout-s {int(timeout_s)}" if timeout_s > 0 else ""),
    )
    return " && ".join(parts)


def model_id(model_name: str | None) -> str:
    """从 harbor 的 `provider/model` 两段式里取出网关认的裸模型名。

    harbor 只认两段式（靠 provider 段查凭据表），而网关的 /chat/completions
    只认 `qwen3.6-27b`，带 `openrouter/` 前缀会直接报 Unsupported model。
    """
    if not model_name:
        raise ValueError("model_name 为空：harbor 需要 -m provider/model 才能解析凭据")
    return model_name.split("/", 1)[1] if "/" in model_name else model_name


def container_env(*, api_key: str, base_url: str) -> dict[str, str]:
    """这次 exec 要往容器里带的环境变量——只有凭据这两项。

    走 per-exec env 而不是写进 job 配置或命令行：命令行会被 harbor 原样印进
    trial.log，而 env 里名字带 KEY 的值 harbor 会脱敏（`is_sensitive_env_key`）。
    """
    return {API_KEY_ENV: api_key, BASE_URL_ENV: base_url}


_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def parse_bool(value: str | None) -> bool | None:
    """三态解析：没给就是 None（跟随模型默认），给了才转成 True/False。"""
    if value is None or not value.strip():
        return None
    word = value.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError(f"解析不成布尔值：{value!r}")


def parse_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value.strip())


def settings_from_env(get: Callable[[str], str | None] = os.environ.get) -> dict[str, Any]:
    """从 env 读出 MyCode 的行为开关，键名与 `run_agent_task` 的形参一一对应。

    容器里没有 `~/.my-code/config.json`，这几项不送进来 MyCode 就按内置默认跑，
    而指标上看不出差别。runner 从宿主端点配置读出来，经 harbor 的 `agents[].env`
    送到这里。
    """
    return {
        "thinking": parse_bool(get(THINKING_ENV)),
        "thinking_feedback": parse_bool(get(THINKING_FEEDBACK_ENV)),
        "context_window": parse_int(get(CONTEXT_WINDOW_ENV)),
    }


def thinking_misalignment(
    result: Mapping[str, Any], settings: Mapping[str, Any]
) -> str | None:
    """思考开关可能没和对照臂对齐时返回一句警告，对齐则返回 None。

    `thinking` 是 None（跟随模型默认）却一个思考 token 都没有，说明网关那头的行为
    和我们以为的不一样。首轮 25 题就栽在这里：MyCode 臂继承了宿主端点配置的
    `thinking=False`、全程不思考，opencode 臂不发这个参数、全程思考，于是测出的
    「MyCode 快 1.9×」全是思考开关的差，而报告里完全看不出来。
    """
    if settings.get("thinking") is not None:
        return None
    if result.get("error"):
        # 本来就跑失败了，token 是 0 很正常，不必再报一次变量警告
        return None
    if (result.get("tokens") or {}).get("reasoning"):
        return None
    return (
        "thinking 未显式关闭但 reasoning_tokens=0：两臂的思考开关可能没对齐，"
        "这轮的速度 / token 指标不可用于对比"
    )


def context_updates(result: Mapping[str, Any]) -> dict[str, Any]:
    """把容器里跑出来的结果 JSON 映射成 harbor `AgentContext` 的字段。

    `n_input_tokens` 按 harbor 的口径是「含 cache 的总输入」，MyCode 的
    `tokens.input` 来自 `usage.prompt_tokens`，同样是含 cache 的，两边一致。
    """
    tokens = result.get("tokens") or {}
    text = result.get("text") or ""
    return {
        "n_input_tokens": tokens.get("input") or 0,
        "n_output_tokens": tokens.get("output") or 0,
        "n_cache_tokens": tokens.get("cached") or 0,
        "metadata": {
            "mycode_error": result.get("error"),
            "mycode_duration_s": result.get("duration_s"),
            "mycode_text_chars": len(text),
            "mycode_session_id": result.get("agent_session_id"),
            "mycode_trace_id": result.get("trace_id"),
            "mycode_compression_events": result.get("compression_events"),
            # n_output_tokens 含思考，单列出来才能和 opencode 的
            # completion/reasoning 两个字段对齐口径（opencode 的 completion 不含思考）
            "mycode_reasoning_tokens": tokens.get("reasoning") or 0,
        },
    }


def hard_failure(result: Mapping[str, Any]) -> str | None:
    """判断这次执行是「根本没跑起来」还是「跑了但没做对」，前者返回错误描述。

    判据：有 error 且一个字都没产出。这时候 trial 不含任何信号，必须让它显式
    失败（harbor 记 exception.txt，可按 --max-retries 重试），不能安静地按 0 分
    混进通过率——网关抖动和「模型真不会做」在指标上会变成同一件事。
    有 error 但已有产出（例如中途超时）则保留这次结果交给 verifier 判分，
    error 只记进 metadata。
    """
    error = result.get("error")
    if not error:
        return None
    if (result.get("text") or "").strip():
        return None
    return str(error)
