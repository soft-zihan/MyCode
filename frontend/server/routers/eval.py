from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .websocket import broadcast_event

router = APIRouter(prefix="/api/eval", tags=["eval"])

# ── 每个 benchmark 自带的参数 schema ────────────────────────────────────────
# 每一项直接对应 runner 的一个 CLI flag（名字用下划线，`jobs.build_argv` 会转成
# 连字符），`default` 与 runner 的 argparse 默认值逐字相同——前端拿它当表单初值，
# 写错了就等于页面默认跑的不是命令行默认跑的那一组。
# 已核对的 argparse 出处：eval/gaia/runner.py:27-45、eval/hle/runner.py:26-36、
# eval/smoke/runner.py:941-952、eval/loca/runner.py:150-175、eval/harbor/runner.py:683-751。
# 三处**有意**与 argparse 不同，都是因为"表单初值"和"CLI 默认值"要的不是同一件事：
#   - harbor `arm`：argparse 默认 `both`（两臂 1.5 小时以上），表单默认只跑 mycode；
#   - harbor `dataset_dir` / `jobs_dir`、loca `loca_repo`：argparse 默认是本机绝对路径，
#     表单给 `None`——`build_argv` 会整个跳过 `None`，于是 runner 用自己的默认值，
#     效果相同，但页面不会把一个会过期的绝对路径当成初值塞给用户。
# `--resume-from` / `--run-dir` / `--no-report` / `--config-dir` / `--prewarm-jobs`
# 不在表里：它们是运维动作（续跑、重出报告），不是"起一次评测"的表单字段。

_GAIA_PARAMS = [
    {"name": "sample", "type": "int", "default": 10, "help": "抽样题数（GAIA Level 3 过滤媒体后共 22 题）"},
    {"name": "level", "type": "choice", "default": "3", "choices": ["1", "2", "3", "all"],
     "help": "难度档；3 是对比实验口径"},
    {"name": "seed", "type": "int", "default": 42, "help": "抽样种子"},
    {"name": "thinking", "type": "choice", "default": "default", "choices": ["on", "off", "default"],
     "help": "思考开关；default=跟随端点配置。**两臂对比时必须显式设定**，否则重演 harbor §5.6"},
    {"name": "thinking_feedback", "type": "choice", "default": "default",
     "choices": ["on", "off", "default"], "help": "历史思考是否以 reasoning_content 回传"},
    {"name": "compression_arm", "type": "choice", "default": None,
     "choices": ["truncate", "tool_only", "session_only", "full"], "help": "压缩消融臂"},
    {"name": "window", "type": "string", "default": None, "help": "显式上下文窗口（如 32k），压缩消融的压力维度"},
    {"name": "timeout", "type": "int", "default": 0, "help": "单题超时秒数，0=不超时"},
    {"name": "model", "type": "string", "default": None, "help": "覆盖模型（默认取第一个端点）"},
    {"name": "execution_mode", "type": "choice", "default": "in_process",
     "choices": ["backend_session", "in_process"], "help": "走真实后端会话还是进程内直调 Agent"},
    {"name": "judge", "type": "bool", "default": False, "help": "结束后跑 code evaluator + LLM judge"},
    {"name": "skip_langfuse", "type": "bool", "default": False, "help": "跳过 Langfuse"},
    {"name": "api_base", "type": "string", "default": None, "help": "覆盖 API base", "advanced": True},
    {"name": "no_dataset", "type": "bool", "default": False, "help": "不同步 Langfuse Dataset", "advanced": True},
]

_HLE_PARAMS = [
    {"name": "sample", "type": "int", "default": 10, "help": "抽样题数"},
    {"name": "seed", "type": "int", "default": 42, "help": "抽样种子"},
    {"name": "category", "type": "string", "default": None, "help": "只跑指定 category"},
    {"name": "include_image", "type": "bool", "default": False, "help": "包含带图片的题（默认跳过）"},
    {"name": "timeout", "type": "int", "default": 0, "help": "单题超时秒数，0=不超时"},
    {"name": "model", "type": "string", "default": None, "help": "覆盖模型"},
    {"name": "execution_mode", "type": "choice", "default": "in_process",
     "choices": ["backend_session", "in_process"], "help": "走真实后端会话还是进程内直调 Agent"},
    {"name": "judge", "type": "bool", "default": False, "help": "结束后跑 code evaluator + LLM judge"},
    {"name": "skip_langfuse", "type": "bool", "default": False, "help": "跳过 Langfuse"},
    {"name": "api_base", "type": "string", "default": None, "help": "覆盖 API base", "advanced": True},
    {"name": "no_dataset", "type": "bool", "default": False, "help": "不同步 Langfuse Dataset", "advanced": True},
]

_SMOKE_PARAMS = [
    {"name": "suite", "type": "choice", "default": "smoke", "choices": ["chain", "smoke"],
     "help": "chain=7 个端到端案例（15–25 分钟）；smoke=16 个单元冒烟（3–5 分钟）"},
    {"name": "sample", "type": "int", "default": None, "help": "只跑前 N 个任务"},
    {"name": "rerun_failed", "type": "bool", "default": False, "help": "只重跑上次失败的任务"},
    {"name": "thinking", "type": "choice", "default": "default", "choices": ["on", "off", "default"],
     "help": "思考开关"},
    {"name": "judge", "type": "bool", "default": False, "help": "结束后跑 code evaluator + LLM judge"},
    {"name": "cleanup", "type": "bool", "default": False, "help": "跑完删除后端 session/project；默认保留便于观察"},
    {"name": "skip_langfuse", "type": "bool", "default": False, "help": "跳过 Langfuse trace 校验"},
    {"name": "base_url", "type": "string", "default": "http://localhost:5555", "help": "后端地址", "advanced": True},
    {"name": "ws_url", "type": "string", "default": "ws://localhost:5555/ws/events", "help": "WS 地址", "advanced": True},
    {"name": "no_dataset", "type": "bool", "default": False, "help": "不同步 Langfuse Dataset", "advanced": True},
]

_LOCA_PARAMS = [
    {"name": "config_set", "type": "choice", "default": "64k",
     "choices": ["8k", "16k", "32k", "64k", "96k", "128k", "256k"], "help": "LOCA 配置集（决定上下文压力）"},
    {"name": "sample", "type": "int", "default": None, "help": "抽样配置数，默认全跑"},
    {"name": "select", "type": "choice", "default": "head", "choices": ["head", "random"], "help": "抽样方式"},
    {"name": "seed", "type": "int", "default": 42, "help": "random 抽样种子"},
    {"name": "arm", "type": "choice", "default": "full",
     "choices": ["full", "truncate", "tool_only", "session_only"], "help": "压缩消融臂"},
    {"name": "window", "type": "string", "default": "100k", "help": "显式窗口覆盖"},
    {"name": "thinking", "type": "choice", "default": "off", "choices": ["on", "off", "default"],
     "help": "思考开关（loca 的 runner 默认是 off，与 gaia 不同）"},
    {"name": "model", "type": "string", "default": None, "help": "覆盖模型"},
    {"name": "timeout", "type": "int", "default": 1800, "help": "单任务 agent 超时秒数"},
    {"name": "max_tool_uses", "type": "int", "default": 100, "help": "LOCA env wrapper 工具上限"},
    {"name": "parallel", "type": "int", "default": 1, "help": "并行任务数"},
    {"name": "skip_langfuse", "type": "bool", "default": False, "help": "跳过 Langfuse"},
    {"name": "indices", "type": "string", "default": None, "help": "显式配置下标，逗号分隔（覆盖 sample/select）", "advanced": True},
    {"name": "loca_repo", "type": "string", "default": None, "help": "LOCA-bench 仓库路径", "advanced": True},
    {"name": "api_base", "type": "string", "default": None, "help": "覆盖 API base", "advanced": True},
]

_HARBOR_PARAMS = [
    {"name": "arm", "type": "choice", "default": "mycode", "choices": ["mycode", "opencode", "both"],
     "help": "跑哪一臂。both 要 1.5 小时以上，所以默认只跑 MyCode"},
    {"name": "stage", "type": "choice", "default": "probe", "choices": ["probe", "final"],
     "help": "probe=6 题（探测，约 15 分钟）；final=25 题（正式数据，约 45 分钟）。probe 是 final 的子集"},
    {"name": "thinking", "type": "choice", "default": "default", "choices": ["default", "on", "off"],
     "help": "MyCode 臂的思考开关。default=不注入 MYCODE_THINKING、跟随模型默认，"
             "这才是和 opencode 臂对齐的设置（README §5.6 那次假结论就是这里没对齐）"},
    {"name": "model", "type": "string", "default": "qwen3.6-27b", "help": "两臂共用的模型"},
    {"name": "seed", "type": "int", "default": 20261017, "help": "分层抽样种子"},
    {"name": "n_concurrent", "type": "int", "default": 2, "help": "harbor 并发容器数"},
    {"name": "tasks", "type": "string", "default": None,
     "help": "显式题目名，逗号分隔；给了就忽略 stage 的抽样"},
    {"name": "dry_run", "type": "bool", "default": False, "help": "只让 harbor 校验配置，不起容器"},
    {"name": "install_only", "type": "bool", "default": False, "help": "只装 agent，不跑题"},
    {"name": "no_prewarm", "type": "bool", "default": False,
     "help": "跳过题目镜像预热（go/rust 题的镜像构建要走代理，不预热会超时）"},
    {"name": "host_ip", "type": "string", "default": None, "help": "覆盖自动探测的宿主 LAN IP", "advanced": True},
    {"name": "dataset_dir", "type": "string", "default": None, "help": "aider-polyglot 数据集目录", "advanced": True},
    {"name": "jobs_dir", "type": "string", "default": None, "help": "harbor job 输出目录", "advanced": True},
]

BENCHMARKS = [
    {
        "id": "gaia",
        "name": "GAIA (Level 3)",
        "description": "通用 AI 助手基准，只保留最难的 Level 3（媒体过滤后 22 题）：文件解析、多步推理、工具使用。精确匹配 pass@1。",
        "detail": {
            "purpose": "衡量通用助手能力：文件解析、多步推理、网页检索、工具编排。Level 3 是最难档，长上下文多步推理密集，最能区分 agent 能力与压缩策略代价（compression-arm 消融实验的标准载体）。",
            "data_source": "data/GAIA/all.json（官方验证集 165 题；固定 Level 3，默认过滤媒体依赖题后 22 题，部分题带附件文件）",
            "scoring": "精确匹配 pass@1（数字/字符串归一化比较），可叠加 judge 评审",
            "execution": "backend_session（真实后端 HTTP+WebSocket 会话）或 in_process（进程内直调 Agent）",
            "duration": "单题约 5-15 分钟；全量 22 题约 2-4 小时",
            "commands": [
                "python -m eval.gaia.runner --sample 10 --seed 42",
                "python -m eval.gaia.runner --sample 10 --seed 42 --compression-arm none --thinking off",
            ],
        },
        "execution_modes": ["backend_session", "in_process"],
        "params": _GAIA_PARAMS,
        "default_options": {"sample": 5, "seed": 42, "timeout_s": 0, "execution_mode": "backend_session"},
    },
    {
        "id": "hle",
        "name": "HLE (Humanity's Last Exam)",
        "description": "极高难度多学科闭卷问答，考验前沿模型推理上限。精确匹配 pass@1。",
        "detail": {
            "purpose": "用极高难度多学科闭卷题衡量模型推理上限；无工具、无附件，适合对比 thinking 开关与不同模型的裸推理能力。",
            "data_source": "data/HLE/all_500.json（500 题子集；默认过滤图片题，可按 category 过滤）",
            "scoring": "精确匹配 pass@1（选择题按选项字母比较）",
            "execution": "backend_session 或 in_process",
            "duration": "单题约 1-5 分钟",
            "commands": [
                "python -m eval.hle.runner --sample 10 --seed 42",
            ],
        },
        "execution_modes": ["backend_session", "in_process"],
        "params": _HLE_PARAMS,
        "default_options": {"sample": 10, "seed": 42, "timeout_s": 0, "execution_mode": "backend_session"},
    },
    {
        "id": "smoke",
        "name": "Smoke / Chain（本项目端到端）",
        "description": "本项目自身的端到端评测（真实后端 HTTP + WebSocket）：smoke 单元冒烟 + chain 全链路案例。发布或大改后必跑。",
        "detail": {
            "purpose": "守护本项目自身功能链路：上下文压缩、错误恢复、多代理协作、Wiki 编译召回、skill 编译、remember 闭环、电商全链路。每个案例独立会话，可勾选单跑。",
            "data_source": "eval/smoke/conversations.jsonl（单元冒烟 16 例）、eval/smoke/chain.jsonl（全链路 7 例）",
            "scoring": "逐案例断言：文件产物 / 工具调用 / 事件流 / 关键词检查，pass/fail",
            "execution": "固定 backend_session（真实 HTTP+WS，走完整后端链路）",
            "duration": "smoke 全套约 3-5 分钟；chain 全套约 15-25 分钟（含长任务案例）",
            "commands": [
                "python -m eval.smoke.runner --suite chain",
                "python -m eval.smoke.runner --suite smoke --only read_file shell_exec",
            ],
        },
        "execution_modes": ["smoke_backend_http"],
        "params": _SMOKE_PARAMS,
        "default_options": {"suite": "chain", "timeout_s": 0, "keep_sessions": True},
        "suites": [
            {"id": "chain", "name": "chain（全链路评测）", "description": "7 个端到端案例：context_stress（压缩）、error_recovery（工具错误反馈）、multi_agent（子代理协作）、wiki_compile_and_recall（折叠→提取→召回）、workflow_skill_compile（模式→skill 编译）、remember_feedback_loop（feedback 写入+同会话召回）、ecommerce_full_chain（教学→编译→新会话召回→citation 不泄漏 一体案例）。"},
            {"id": "smoke", "name": "smoke（单元冒烟）", "description": "基础能力单测：文件读写、shell、搜索、多轮上下文等最小用例，快速验证后端无回归。"},
        ],
    },
    {
        "id": "loca",
        "name": "LOCA-bench（长上下文 agent）",
        "description": "长上下文下的 agent 能力：把关键信息埋在超长环境描述里，考压缩与检索。reward 连续值。",
        "detail": {
            "purpose": "衡量上下文压缩策略的代价与收益——这是 MyCode 的核心机制，也是唯一能把它逼出来的 benchmark。",
            "data_source": "projects/LOCA-bench（hkust-nlp），按 config_set 选窗口档",
            "scoring": "LOCA 自带 verifier 给连续 reward；`passed` = reward 达阈值",
            "execution": "进程内直调 Agent，可并行",
            "duration": "单任务数分钟；64k 集全量约 1–2 小时",
            "commands": [
                "python -m eval.loca.runner --config-set 64k --sample 5 --arm full",
                "python -m eval.loca.runner --config-set 32k --window 8k --arm tool_only",
            ],
        },
        "params": _LOCA_PARAMS,
        "default_options": {"config_set": "64k", "sample": 5, "arm": "full", "thinking": "off"},
    },
    {
        "id": "harbor",
        "name": "Harbor / aider-polyglot（编码 agent 两臂对比）",
        "description": "真容器里跑编码题（Exercism，6 语言），MyCode 与 opencode 两臂同模型对比。判分是测试全过。",
        "detail": {
            "purpose": "唯一能对比 scaffold 的 benchmark：两臂同题、同模型、同判分，差异只在 agent 本身。",
            "data_source": "aider-polyglot 本地副本（225 题 / 6 语言），按语言分层抽样",
            "scoring": "reward ≥ 1.0 算过（Exercism 测试全过）；构建失败按 0 分计入，不当异常剔除",
            "execution": "harbor 0.24.0 起 docker 容器；MyCode 臂在容器内进程直调 run_agent_task（不走 CLI）",
            "duration": "probe 6 题约 15 分钟；final 25 题约 45 分钟（单臂）。**需要 WSL + docker + 网关配额**",
            "commands": [
                "python -m eval.harbor.runner --arm mycode --stage probe",
                "python -m eval.harbor.runner --arm both --stage final",
                "python -m eval.harbor.compare --stage final --exclude-language java",
            ],
            "caveats": [
                "跑之前先确认 `eval/harbor/setup_env.sh` 已执行过（docker compose/buildx、pip 镜像、代理）",
                "java 题在本环境两臂都恒 0（JVM 不读 HTTPS_PROXY），对比时用 --exclude-language java",
                "k=1 的单题结果是噪声（实测自噪声 ±3 题），只有总体通过率有意义",
            ],
        },
        "params": _HARBOR_PARAMS,
        "default_options": {"arm": "mycode", "stage": "probe", "thinking": "default"},
    },
]


class EvalRunRequest(BaseModel):
    benchmark: Literal["gaia", "hle", "smoke"]
    sample: int | None = None
    seed: int = 42
    category: str | None = None
    include_image: bool = False
    only: list[str] | None = None
    suite: str = "chain"
    timeout_s: int = 0
    model: str | None = None
    api_base: str | None = None
    execution_mode: Literal["backend_session", "in_process"] = "backend_session"
    sync_langfuse_dataset: bool = True
    judge_after_run: bool = False
    skip_langfuse: bool = False
    keep_sessions: bool = True
    base_url: str = "http://localhost:5555"
    ws_url: str = "ws://localhost:5555/ws/events"
    thinking: bool | None = None
    compression_arm: Literal["none", "tool_only", "session_only", "full"] | None = None


class JudgeRequest(BaseModel):
    judge: bool = True


def _options_from_request(data: EvalRunRequest) -> Any:
    from eval.common.models import EvalRunOptions

    return EvalRunOptions(**data.model_dump())


def broadcast_eval_event(payload: dict[str, Any]) -> None:
    """评测事件上 WS 的唯一出口。

    `session_id: None` 是必须的：评测不属于任何会话，而 broadcast_event 的
    订阅者过滤按 session_id 走；`eval/` 前缀已在 PUBLIC_EVENT_PREFIXES 里放行。
    """
    broadcast_event({"session_id": None, **payload})


@router.get("/benchmarks")
async def api_eval_benchmarks() -> dict[str, Any]:
    return {"benchmarks": BENCHMARKS}


@router.get("/tasks")
async def api_eval_tasks(benchmark: str, suite: str = "chain") -> dict[str, Any]:
    """列出评测集任务（用于 UI 浏览与单任务直跑）。"""
    if benchmark == "smoke":
        from eval.smoke.runner import load_tasks

        if suite not in ("smoke", "chain"):
            raise HTTPException(status_code=404, detail=f"unknown suite: {suite}")
        raw = load_tasks(suite=suite)
        items = [{"id": t["id"], "name": t.get("name", t["id"]), "meta": {}} for t in raw]
    elif benchmark == "gaia":
        from eval.common.benchmarks import GaiaBenchmark

        raw = GaiaBenchmark().load_raw_tasks(include_media=False)
        items = [{
            "id": str(t.get("task_id") or t.get("id")),
            "name": str(t.get("Question", ""))[:100],
            "meta": {"file_name": t.get("file_name") or ""},
        } for t in raw]
    elif benchmark == "hle":
        from eval.common.benchmarks import HleBenchmark

        raw = HleBenchmark().load_raw_tasks(include_image=False)
        items = [{
            "id": str(t.get("id")),
            "name": str(t.get("question", ""))[:100],
            "meta": {"category": t.get("category") or ""},
        } for t in raw]
    else:
        raise HTTPException(status_code=404, detail=f"unknown benchmark: {benchmark}")
    return {
        "benchmark": benchmark,
        "suite": suite if benchmark == "smoke" else None,
        "total": len(items),
        "tasks": items,
    }


@router.get("/langfuse")
async def api_eval_langfuse() -> dict[str, Any]:
    from agents.observability.langfuse_api import get_client

    try:
        client = get_client()
        project = await asyncio.to_thread(client.fetch_project)
        return {
            "configured": True,
            "base_url": client.base_url,
            "project_id": project.get("id"),
            "project_name": project.get("name"),
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Langfuse 不可用: {type(e).__name__}: {e}")


@router.get("/runs")
async def api_eval_runs(limit: int = 100) -> dict[str, Any]:
    from eval.common.service import get_eval_service, list_report_runs

    service = get_eval_service()
    active = [state.to_dict() for state in service.runs.values() if state.status in ("pending", "running")]
    return {"active": active, "runs": list_report_runs(limit)}


@router.post("/runs")
async def api_eval_start_run(data: EvalRunRequest) -> dict[str, Any]:
    from eval.common.service import get_eval_service

    service = get_eval_service()
    options = _options_from_request(data)
    if options.benchmark == "smoke":
        options.execution_mode = "backend_session"
    state = service.start_run(options, emitter=broadcast_eval_event)
    return state.to_dict()


@router.get("/runs/{run_id}")
async def api_eval_run(run_id: str) -> dict[str, Any]:
    from eval.common.service import get_eval_service

    service = get_eval_service()
    run = service.get_run_dict(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"未知 run_id: {run_id}")
    return run


@router.post("/runs/{run_id}/abort")
async def api_eval_abort_run(run_id: str) -> dict[str, Any]:
    from eval.common.service import get_eval_service

    service = get_eval_service()
    aborted = service.abort_run(run_id)
    if not aborted:
        raise HTTPException(status_code=404, detail=f"没有可中止的 run: {run_id}")
    return {"run_id": run_id, "aborted": True}


@router.post("/runs/{run_id}/judge")
async def api_eval_judge_run(run_id: str, data: JudgeRequest) -> dict[str, Any]:
    from eval.common.service import get_eval_service

    service = get_eval_service()
    try:
        return await service.judge_run(run_id, judge=data.judge)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@router.get("/reports")
async def api_eval_reports(limit: int = 100) -> dict[str, Any]:
    from eval.common.service import list_report_runs

    return {"runs": list_report_runs(limit)}


@router.get("/reports/{run_id}")
async def api_eval_report(run_id: str) -> dict[str, Any]:
    from eval.common.service import load_report, load_state

    report = load_report(run_id)
    if report:
        return report
    state = load_state(run_id)
    if state:
        return state
    raise HTTPException(status_code=404, detail=f"未知报告: {run_id}")


@router.get("/reports/{run_id}/markdown", response_class=PlainTextResponse)
async def api_eval_report_markdown(run_id: str) -> str:
    from eval.common.service import report_md_path

    path = report_md_path(run_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"未知 Markdown 报告: {run_id}")
    return path.read_text(encoding="utf-8")


# ── 子进程执行层（Task 2/3/8）的 HTTP 面 ────────────────────────────────────
# `/runs*` 那组是进程内 asyncio 的旧执行路径，响应形状与下面这组不同，**原样保留**：
# 现在这个前端还能用，改成薄别名会当场打断它。P4 把 EvalPage 切到 `/jobs*` 之后再删。


class JobStartRequest(BaseModel):
    benchmark: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.get("/jobs")
async def api_eval_jobs(limit: int = 100) -> dict[str, Any]:
    """运行索引：新 job.json + 旧 state.json + 标准信封报告，三个来源合并。"""
    from eval.common.jobs import MAX_CONCURRENT_JOBS, list_jobs, running_jobs

    def _scan() -> tuple[list, list]:
        # **一次扫描喂两个结果**，不要 gather 两个 to_thread：
        # `running_jobs()` 不带 records= 时内部会再调一次 list_jobs，
        # 于是两个线程同时解析同一个报告目录、同时改 Task 8 加的
        # `_REPORT_RECORDS` 缓存。就算缓存本身已经安全（剪枝是 `pop(key, None)`，
        # 幂等），两个线程扫同一个 DrvFs 目录也是白扔一倍 stat。
        # 顺带还把索引扫描从两次压成一次（本机实测一次 24.9ms）。
        #
        # **一律不传 `reports_dir=`**：`_REPORT_RECORDS` 是模块全局的，而剪枝按
        # "本轮扫到的路径"做差集，所以按请求传目录会把 tailer 那一侧（默认 root）
        # 的缓存整片踢掉——tailer 每 0.5s 扫一次，那就是每 tick 重新解析全部报告。
        # 进程内只留一个 root，这条缺陷就不可达（见 tests/unit/test_eval_jobs.py
        # 与 tests/unit/test_eval_api_jobs.py 里钉住它的两条测试）。
        records = list_jobs(limit=1000)
        return records[:limit], running_jobs(records=records)

    jobs, running = await asyncio.to_thread(_scan)
    return {
        "jobs": [j.to_dict() for j in jobs],
        "running": [j.run_id for j in running],
        "max_concurrent": MAX_CONCURRENT_JOBS,
    }


@router.post("/jobs", status_code=202)
async def api_eval_start_job(data: JobStartRequest) -> dict[str, Any]:
    from eval.common.jobs import BENCHMARK_MODULES, JobStartError, start_job

    # 未知 benchmark 必须在起进程之前拦住：它进到 start_job 也是 JobStartError，
    # 而那个异常在这里映射成 409（"已有任务在跑"），语义就错了
    if data.benchmark not in BENCHMARK_MODULES:
        raise HTTPException(
            status_code=400,
            detail=f"未知 benchmark：{data.benchmark}"
                   f"（可选：{', '.join(sorted(BENCHMARK_MODULES))}）",
        )
    try:
        record = await asyncio.to_thread(start_job, data.benchmark, data.params)
    except JobStartError as e:
        # 409 不是 500：这是"资源被占着"，前端要能提示用户先中止，而不是显示服务器错误
        raise HTTPException(status_code=409, detail=str(e))
    return record.to_dict()


@router.get("/jobs/{run_id}")
async def api_eval_job(run_id: str, events: int = 2) -> dict[str, Any]:
    from eval.common.events import read_events
    from eval.common.jobs import events_path, get_job
    from eval.common.service import load_report, load_state

    job, report, state = await asyncio.gather(
        asyncio.to_thread(get_job, run_id),
        asyncio.to_thread(load_report, run_id),
        asyncio.to_thread(load_state, run_id),
    )
    if job is None and report is None and state is None:
        raise HTTPException(status_code=404, detail=f"未知 run_id: {run_id}")

    path = events_path(run_id)
    every, offset = (await asyncio.to_thread(read_events, path)) if path.exists() else ([], 0)
    return {
        "run_id": run_id,
        "job": job.to_dict() if job else None,
        "benchmark": (job.benchmark if job else None)
                     or (report or {}).get("benchmark") or (state or {}).get("benchmark"),
        "status": (job.status.value if job else None) or (state or {}).get("status")
                  or ("completed" if report else None),
        "meta": (report or {}).get("meta") or {},
        "summary": (report or {}).get("summary") or (state or {}).get("summary") or {},
        # 默认只带最近 2 条：详情页要的是"现在卡在哪一题"，
        # 完整事件流走 /events?since= 增量拉，不然 25 题能把响应撑到几百 KB
        "events": every[-events:] if events > 0 else [],
        "offset": offset,
    }


@router.get("/jobs/{run_id}/events")
async def api_eval_job_events(run_id: str, since: int = 0) -> dict[str, Any]:
    """增量拉事件。前端拿上一次返回的 offset 原样传回来即可。"""
    from eval.common.events import read_events
    from eval.common.jobs import events_path

    path = events_path(run_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"没有事件流: {run_id}")
    events, offset = await asyncio.to_thread(read_events, path, since)
    return {"run_id": run_id, "events": events, "offset": offset}


@router.post("/jobs/{run_id}/abort")
async def api_eval_abort_job(run_id: str) -> dict[str, Any]:
    from eval.common.jobs import JobStatus, abort_job

    record = await asyncio.to_thread(abort_job, run_id)
    # 判据是**状态**而不是 `record is None`：`abort_job` 对已经跑完的记录是原样返回的
    # （它只在状态还是 RUNNING/QUEUED 且有 pid 时才发 SIGTERM 并改状态），
    # 所以"拿到了记录"不等于"中止了"。只有真被标成 ABORTED 的那次才是中止成功，
    # 其余（记录不存在 / 已完成 / 已失败）都是"没有可中止的"→ 404。
    if record is None or record.status is not JobStatus.ABORTED:
        raise HTTPException(status_code=404, detail=f"没有可中止的评测: {run_id}")
    return record.to_dict()


def _is_run_record(data: Any) -> bool:
    """这份 JSON 是不是一次运行的记录（而不是对比产物）。

    比 `looks_like_run_report` **宽一档，而且必须宽**：后者要 `{benchmark, run_id,
    results}` 三个顶层键，而
    - 2026-10-18 之前落盘的 harbor 报告只有 `summary` + `trials`（`jobs.py` 的
      `_looks_like_legacy_run_report` 刚让它们进得了运行索引），
    - 已发表那一对连 `run_id` 都没有（`compare._label_of` 的回退链就是为此存在的）。
    拿原函数当闸门会造出"索引里列得出来、一对比就 400"的自相矛盾。
    要挡的是 `harbor_compare_*.json` 这类对比产物：它既没有 `results` 也没有 `trials`，
    `rows_of` 对它返回 `[]`，于是 `compare_reports` 会给出 **200 + n_paired=0** 的空对比
    ——对一个根本不存在的运行记录回 200 是错的。
    """
    if not isinstance(data, Mapping):
        return False
    return isinstance(data.get("results"), list) or isinstance(data.get("trials"), list)


@router.get("/compare")
async def api_eval_compare(a: str, b: str, exclude: str | None = None) -> dict[str, Any]:
    """任意两次运行对比。变量没对齐时结果里会带 variable_mismatches，markdown 顶部有横幅。"""
    from eval.common.compare import compare_reports, load_run, render_markdown

    try:
        report_a, report_b = await asyncio.gather(
            asyncio.to_thread(load_run, a), asyncio.to_thread(load_run, b)
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    for run_id, report in ((a, report_a), (b, report_b)):
        if not _is_run_record(report):
            raise HTTPException(
                status_code=400,
                detail=f"{run_id} 不是一次运行记录（既没有 results 也没有 trials）："
                       "eval/reports/ 里还躺着对比产物 harbor_compare_*.json，"
                       "拿它当输入只会得到一份 n_paired=0 的空对比",
            )

    # 旧形状报告（`summary` + `trials`）没有 `benchmark` 这个顶层键，而 `rows_of` 对它们的
    # 默认值**已经是** "harbor"（eval/common/compare.py:96），3d 让运行索引用了同一个默认值。
    # 这里不用同一把尺子的话，已发表那一对在页面上就会丢掉按语言分层、也丢掉 ARM_KEYS 的
    # 过滤（git_revision 差异会让横幅永远挂着），而 harbor 自己的 CLI 一直是分层的。
    # `results is None` 就是 `rows_of` 走默认值的那个条件；上面 `_is_run_record` 已经保证了
    # 这种情况下 `trials` 一定是个 list。
    legacy_harbor = report_a.get("benchmark") is None and report_a.get("results") is None
    dimension = None
    if report_a.get("benchmark") == "harbor" or legacy_harbor:
        # 按语言分层只有 harbor 有语义；两臂对比里 arm/git_revision 不同是前提，
        # 不能报成"变量没对齐"，否则横幅永远挂着、也就永远没人看
        from eval.harbor.compare import drop_expected_mismatches, language_of

        dimension = language_of
    result = compare_reports(
        # 显式标签 = 调用方传进来的两个 run_id 原话。裁定见 task-13 简报开头那段：
        # 已发表那一份对比（harbor_compare_final_20261018__034229.json）用的就是文件名主干，
        # 而旧报告没有 run_id 键、回退链会退到 arm 名，与已归档产物对不上。
        # 传了就不会被 `_dedupe_labels` 改写——`compare_reports` 的 `if labels:` 分支
        # "不去重"正是为调用方的原话准备的（eval/common/compare.py:394-398）。
        report_a, report_b, labels=(a, b), dimension=dimension,
        exclude=tuple(x.strip() for x in (exclude or "").split(",") if x.strip()),
    )
    if dimension is not None:
        result = drop_expected_mismatches(result)

    # markdown 在裁剪 pairs **之前**渲染：它要读逐题判定。
    # 守卫横幅由 `render_markdown` 内部的 `guard_banner` 出，这里不拼第二份措辞
    # （Task 12 花了一整轮修掉的就是"安全关键措辞存两份"）。
    result["markdown"] = render_markdown(result)
    result["pairs"] = [
        {
            "task_id": p["task_id"],
            "dimension": p["dimension"],
            "a_passed": p["a_passed"],
            "b_passed": p["b_passed"],
            "a_duration_s": (p["a"] or {}).get("duration_s"),
            "b_duration_s": (p["b"] or {}).get("duration_s"),
        }
        for p in result["pairs"]
    ]
    return result
