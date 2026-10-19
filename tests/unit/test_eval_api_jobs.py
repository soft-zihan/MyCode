"""评测的 HTTP 面。

钉住的四件事，每件都对应一个已经存在的缺陷：
1. harbor 进得了运行列表、也拿得到报告（原来 `/api/eval/reports/{id}` 对它恒 404）；
2. 事件是**增量**拉的（原来前端每收一条 WS 事件就 refetch 全量，25 题能刷上百次）；
3. 已有任务在跑时启动被拒且是 409 不是 500（评测很重且共享同一个网关配额）；
4. benchmark 自带参数 schema（原来表单字段写死在前端 DEFAULT_FORM，文档写在 router 字符串里）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eval.common.events import JsonlEventSink
from eval.common.jobs import MAX_CONCURRENT_JOBS, JobRecord, JobStatus, write_job

_server_dir = str(Path(__file__).parent.parent.parent / "frontend" / "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)


@pytest.fixture(autouse=True)
def _no_leaked_proc_handles():
    """`_PROCS` 是模块级状态：start_job 会把假 Popen 挂上去，漏一条就串到后面的测试。

    只清本条测试新加的键（快照差集），不动别的测试文件正在用的那些。
    """
    from eval.common.jobs import _PROCS

    before = set(_PROCS)
    yield
    for run_id in set(_PROCS) - before:
        _PROCS.pop(run_id, None)


@pytest.fixture
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr("eval.common.jobs.REPORTS_DIR", tmp_path)
    monkeypatch.setattr("eval.common.runner_base.REPORTS_DIR", tmp_path)
    monkeypatch.setattr("eval.common.compare.REPORTS_DIR", tmp_path)
    # service.py 在 import 时就把 REPORTS_DIR 绑成了自己的模块全局
    # （`from eval.common.runner_base import REPORTS_DIR`），所以补 runner_base 那一份
    # **不会**改到它——而 `/reports/{id}`、`/jobs/{id}` 走的正是 service 的
    # load_report / load_state / report_md_path。不补这一行，那两条路由会去读真的
    # eval/reports/（里面装着不可再生的基线数据），测试也会因为读不到 tmp 里的报告而红。
    monkeypatch.setattr("eval.common.service.REPORTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def api(reports):
    from main import app

    # **不用 `with TestClient(app)`**：那会跑 lifespan，于是 JobTailer 每 0.5s 在事件
    # 循环上扫一遍索引。tailer 的 `reap_finished` 会把"状态说在跑、pid 却查不到"的记录
    # 直接 finish_job 成 FAILED——而 `test_aborting_a_job_terminates_its_process_group`
    # 用的正是 pid=999_999 这种查不到的假 pid，于是它和 tailer 抢同一条记录：
    # tailer 先到，abort 就变成对一条 FAILED 记录中止 → 404，测试随机红。
    # 仓库里其余的 router 单测（test_task_endpoints / test_worktree / …）也都是裸
    # TestClient，不起 lifespan。附带好处：省掉每条 ~5.2s 的启动（MCP + tracing）。
    yield TestClient(app)


def _job(run_id="gaia-20261018_000000-abc123", benchmark="gaia",
         status=JobStatus.COMPLETED, **over) -> JobRecord:
    record = JobRecord(
        run_id=run_id, benchmark=benchmark,
        argv=[".venv/bin/python", "-m", f"eval.{benchmark}.runner"], params={"sample": 5},
        git_revision="ab6b631", status=status, pid=os.getpid(),
        started_at="2026-10-18T00:00:00+08:00", finished_at="2026-10-18T01:00:00+08:00",
        exit_code=0, report_path=None, error=None,
    )
    for key, value in over.items():
        setattr(record, key, value)
    write_job(record)
    return record


def _report(reports_dir, run_id, benchmark="harbor", rows=None, arm="mycode", **variables):
    rows = rows if rows is not None else [
        {"task_id": "polyglot_cpp_yacht", "name": "yacht", "status": "passed", "passed": True,
         "reward": 1.0, "duration_s": 30.2,
         "tokens": {"input": 47582, "output": 1405, "reasoning": 308, "cached": 0},
         "error": None, "session_id": None, "trace_id": None, "extra": {}},
    ]
    data = {
        "benchmark": benchmark, "run_id": run_id, "generated_at": "2026-10-18T02:53:51+08:00",
        # `arm` 只进 meta，**不进 variables**：盘上真报告的 `run_variables()` 返回的 8 个键里
        # 没有 arm（实测 harbor_mycode_full_20261018__224307.json 的 meta.variables 是
        # git_revision/model/mycode_env/provider/seed/stage/thinking_effective/thinking_mode），
        # 而 `eval/harbor/compare.py:69-78` 正因此把 `"variables.arm"` 从 ARM_KEYS 里删掉了
        # （"实测是死条目"）。把它塞进合成报告的 variables，两臂对比就会多出一项
        # `variables.arm` 差异，横幅永远挂着——而那正是本文件下面那条断言要防的。
        "meta": {"model": "qwen3.6-27b", "seed": 20261017, "git_revision": "ab6b631",
                 "arm": arm, "task_ids": [r["task_id"] for r in rows],
                 "variables": {"model": "qwen3.6-27b", "seed": 20261017, **variables}},
        "summary": {"n_tasks": len(rows), "n_scored": len(rows), "n_passed": sum(
            1 for r in rows if r["passed"]), "pass_rate": 1.0},
        "results": rows,
    }
    (reports_dir / f"{run_id}.json").write_text(json.dumps(data), encoding="utf-8")
    (reports_dir / f"{run_id}.md").write_text(f"# {run_id}\n", encoding="utf-8")
    return data


# ── benchmarks 自带参数 schema ──


def test_benchmarks_covers_all_five(api):
    ids = [b["id"] for b in api.get("/api/eval/benchmarks").json()["benchmarks"]]

    assert sorted(ids) == ["gaia", "harbor", "hle", "loca", "smoke"]


def test_every_benchmark_declares_its_own_params(api):
    # 表单字段写死在前端 DEFAULT_FORM，是"页面难用"的直接原因之一：
    # 后端加一个 CLI 参数，前端不会知道
    for bench in api.get("/api/eval/benchmarks").json()["benchmarks"]:
        assert bench["params"], f"{bench['id']} 没有声明参数 schema"
        for spec in bench["params"]:
            assert set(spec) >= {"name", "type", "default", "help"}
            assert spec["type"] in ("int", "string", "bool", "choice")
            if spec["type"] == "choice":
                assert spec["choices"]


def test_harbor_params_default_to_the_aligned_thinking_switch(api):
    bench = next(b for b in api.get("/api/eval/benchmarks").json()["benchmarks"]
                 if b["id"] == "harbor")
    params = {p["name"]: p for p in bench["params"]}

    # default = 不注入 MYCODE_THINKING = 跟随模型默认 = 与 opencode 臂对齐。
    # 这个默认值错了就会重演 README §5.6 那次事故，所以钉住它
    assert params["thinking"]["default"] == "default"
    assert params["thinking"]["choices"] == ["default", "on", "off"]
    assert params["arm"]["default"] == "mycode"  # both 要跑 1.5 小时，不该是默认
    # 三档，与 runner 的 STAGE_SIZES 逐键对齐（原简报只给两档，于是 API 表单起不了 full，
    # 而 full 正是盘上那批 225 题运行的档位——能看见结果、不能复现它）。
    # default **仍是 probe**：full 是 225 题、单臂一整夜，不该是表单的默认选项
    assert params["stage"]["choices"] == ["probe", "final", "full"]
    assert params["stage"]["default"] == "probe"


def test_harbor_params_are_real_runner_flags(api):
    """声明的每个参数都得是 runner 真有的 CLI flag，否则前端会把用户输入送给一个不存在的开关。

    只对 harbor 做**活的**核对：五个 runner 里只有它把 parser 提成了模块级的
    `build_parser()`（gaia/hle/smoke/loca 都是在 `main()` 里就地构造，没有可导入的单一
    来源）。其余四个的 default 是逐项人工核对 argparse 的，核对结果记在 task-13 报告里。
    """
    from eval.harbor.runner import STAGE_SIZES, build_parser

    actions = {a.dest for a in build_parser()._actions}
    bench = next(b for b in api.get("/api/eval/benchmarks").json()["benchmarks"]
                 if b["id"] == "harbor")

    declared = {p["name"] for p in bench["params"]}

    assert declared, "探针没接上：harbor 一个参数都没声明"
    assert declared <= actions, f"harbor 声明了 runner 没有的 flag：{sorted(declared - actions)}"
    # `--resume-from` / `--run-dir` / `--no-report` / `--config-dir` / `--prewarm-jobs`
    # 是运维动作，**有意**不进表单（前者还是 Task 15 刚加的断点续跑）
    assert "resume_from" not in declared and "run_dir" not in declared

    # `stage` 的取值域必须与 runner 的 STAGE_SIZES **逐键**对齐（CLI 侧是
    # `choices=sorted(STAGE_SIZES)`，eval/harbor/runner.py:692）。上一条只钉了
    # "声明的都是真 flag"，钉不住"漏了一档"——而漏档的后果正是 F3 要修的那个：
    # 表单起不了 --stage full，于是页面能列出这批 225 题的运行、却不能用同样的参数复现它。
    # 写成活核对而不是钉死三个字面量，是为了 runner 将来加第四档时这里**自动**跟着要改。
    declared_stage = next(p for p in bench["params"] if p["name"] == "stage")
    assert sorted(declared_stage["choices"]) == sorted(STAGE_SIZES), (
        f"harbor 的 stage 取值域与 runner 的 STAGE_SIZES 漂了："
        f"{sorted(declared_stage['choices'])} != {sorted(STAGE_SIZES)}"
    )


# ── 运行列表 ──


def test_jobs_lists_a_harbor_run_that_only_has_a_report(api, reports):
    _report(reports, "harbor_mycode_final_20261018__025351")

    body = api.get("/api/eval/jobs").json()

    assert [j["run_id"] for j in body["jobs"]] == ["harbor_mycode_final_20261018__025351"]
    assert body["jobs"][0]["benchmark"] == "harbor"
    assert body["max_concurrent"] == MAX_CONCURRENT_JOBS


def test_jobs_reports_which_ones_are_running(api, reports):
    _job(run_id="live", status=JobStatus.RUNNING)
    _job(run_id="done", status=JobStatus.COMPLETED)

    body = api.get("/api/eval/jobs").json()

    assert body["running"] == ["live"]
    assert len(body["jobs"]) == 2


def test_the_jobs_route_scans_only_the_default_reports_dir(api, reports, monkeypatch):
    """路由**一律不传** `reports_dir=`——这是"不让缓存剪枝缺陷可达"那个选择的看守。

    `_REPORT_RECORDS` 是模块全局的，而剪枝按"本轮扫到的路径"做差集，所以按请求传目录
    会把 tailer 那一侧（默认 root）的缓存整片踢掉；tailer 每 0.5s 扫一次，
    那就是每 tick 重新解析全部报告（本机实测一次全解析 +44ms）。
    """
    import eval.common.jobs as jobs

    seen: list[dict] = []
    real_list_jobs = jobs.list_jobs

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real_list_jobs(*args, **kwargs)

    monkeypatch.setattr(jobs, "list_jobs", spy)

    body = api.get("/api/eval/jobs").json()

    assert seen, "路由没调 list_jobs——这条探针根本没接上"
    assert all(kwargs.get("reports_dir") is None for kwargs in seen), (
        f"路由按请求传了 reports_dir，缓存会被跨 root 踢掉：{seen}"
    )
    assert "jobs" in body and "max_concurrent" in body


def test_the_harbor_report_is_reachable_through_the_reports_api(api, reports):
    # 这一条就是原来那个 404：run_id 等于文件名主干之后它必须能命中
    _report(reports, "harbor_mycode_final_20261018__025351")

    response = api.get("/api/eval/reports/harbor_mycode_final_20261018__025351")

    assert response.status_code == 200
    assert response.json()["benchmark"] == "harbor"
    assert api.get("/api/eval/reports/harbor_mycode_final_20261018__025351/markdown").status_code == 200


# ── 单个 job ──


def test_a_single_job_carries_its_summary_and_recent_events(api, reports):
    record = _job(run_id="r1")
    sink = JsonlEventSink(reports / "r1.events.jsonl", run_id="r1")
    for i in range(5):
        sink.emit("eval/task_finished", task_id=f"t{i}")
    sink.close()
    _report(reports, "r1", benchmark="gaia")

    body = api.get("/api/eval/jobs/r1").json()

    assert body["job"]["run_id"] == "r1" and body["status"] == "completed"
    assert body["summary"]["n_passed"] == 1
    assert len(body["events"]) == 2  # 默认只带最近 2 条，全量走 /events?since=
    assert body["events"][-1]["task_id"] == "t4"
    assert body["offset"] == (reports / "r1.events.jsonl").stat().st_size


def test_an_unknown_job_is_a_404(api):
    assert api.get("/api/eval/jobs/nope").status_code == 404


# ── 增量事件 ──


def test_events_are_incremental(api, reports):
    _job(run_id="r1")
    sink = JsonlEventSink(reports / "r1.events.jsonl", run_id="r1")
    sink.emit("eval/task_started", task_id="t1")

    first = api.get("/api/eval/jobs/r1/events").json()
    assert [e["task_id"] for e in first["events"]] == ["t1"]

    # 拿到的 offset 原样传回去，没有新事件就必须是空的——
    # 前端靠这个替代"每收一条 WS 就 refetch 全量"
    sink.emit("eval/task_started", task_id="t2")
    second = api.get(f"/api/eval/jobs/r1/events?since={first['offset']}").json()
    assert [e["task_id"] for e in second["events"]] == ["t2"]

    third = api.get(f"/api/eval/jobs/r1/events?since={second['offset']}").json()
    assert third["events"] == [] and third["offset"] == second["offset"]


def test_events_of_a_run_that_never_started_is_a_404(api):
    assert api.get("/api/eval/jobs/nope/events").status_code == 404


# ── 启动与中止 ──


def test_starting_an_unknown_benchmark_is_a_400_not_a_500(api):
    response = api.post("/api/eval/jobs", json={"benchmark": "swe-bench", "params": {}})

    assert response.status_code == 400
    assert "swe-bench" in response.json()["detail"]


def test_starting_while_busy_is_a_409(api, reports, monkeypatch):
    _job(run_id="live", status=JobStatus.RUNNING)
    monkeypatch.setattr("eval.common.jobs.subprocess.Popen",
                        lambda *a, **k: pytest.fail("不该起进程"))

    response = api.post("/api/eval/jobs", json={"benchmark": "gaia", "params": {"sample": 1}})

    assert response.status_code == 409
    assert "live" in response.json()["detail"]


def test_starting_a_job_spawns_a_detached_subprocess(api, reports, monkeypatch):
    started = {}

    class _Proc:
        pid = 4321

    def fake_popen(argv, **kwargs):
        started["argv"] = argv
        started["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr("eval.common.jobs.subprocess.Popen", fake_popen)
    # `jobs.subprocess` 就是 subprocess 模块本身，所以上面那一行 patch 的是**全局** Popen，
    # 连带把 `start_job → git_revision() → eval.harbor.runner.git_revision` 里那两次
    # `subprocess.run`（内部也是 `with Popen(...)`）一起接走了，而 `_Proc` 不是上下文
    # 管理器 → TypeError，而 `jobs.git_revision` 只兜 OSError/SubprocessError → 500。
    # git 版本不是这条测试的对象（它在 tests/unit/test_eval_jobs.py 里另有看守），
    # 所以直接把它替掉，让假 Popen 只扮演"评测子进程"这一个角色。
    monkeypatch.setattr("eval.common.jobs.git_revision", lambda: "ab6b631")

    response = api.post("/api/eval/jobs", json={
        "benchmark": "harbor",
        "params": {"arm": "mycode", "stage": "probe", "thinking": "default", "dry_run": True},
    })

    assert response.status_code == 202
    body = response.json()
    # `running` 而不是简报写的 `queued`：`start_job` 在 Popen 之后就直接把记录写成
    # `status=JobStatus.RUNNING`，QUEUED 这个值它从来不用；
    # 而 tests/unit/test_eval_jobs.py::test_start_job_writes_record_and_passes_event_env
    # 早就钉着 `rec.status == JobStatus.RUNNING`。路由只是原样返回 record.to_dict()，
    # 所以这里要改的是断言，不是生产代码（HTTP 202 表示"已受理"，与状态词表无关）。
    assert body["benchmark"] == "harbor" and body["status"] == "running"
    assert body["pid"] == 4321
    # 参数要变成 CLI flag：下划线转连字符，store_true 型的 bool 只出 flag
    assert started["argv"][:3] == [sys.executable, "-m", "eval.harbor.runner"]
    assert started["argv"][3:] == [
        "--arm", "mycode", "--stage", "probe", "--thinking", "default", "--dry-run",
    ]
    # 服务被杀不能连带杀掉跑了几小时的任务
    assert started["kwargs"]["start_new_session"] is True
    # stdout 必须落文件而不是 PIPE：PIPE 没人读，写满 64KB 缓冲区就把子进程堵死了
    import subprocess as _sp
    assert started["kwargs"]["stderr"] is _sp.STDOUT
    assert hasattr(started["kwargs"]["stdout"], "name")
    # run_id 与事件路径经 env 传给子进程
    assert started["kwargs"]["env"]["MYCODE_EVAL_RUN_ID"] == body["run_id"]
    assert started["kwargs"]["env"]["MYCODE_EVAL_EVENTS_PATH"].endswith(f"{body['run_id']}.events.jsonl")


def test_aborting_a_job_terminates_its_process_group(api, reports, monkeypatch):
    _job(run_id="live", status=JobStatus.RUNNING, pid=999_999)
    signals = []
    monkeypatch.setattr("eval.common.jobs.os.killpg", lambda pgid, sig: signals.append((pgid, sig)))

    response = api.post("/api/eval/jobs/live/abort")

    assert response.status_code == 200
    assert response.json()["status"] == "aborted"
    # 必须是**负的** pid = 发给整个进程组：harbor 会再起 docker 子进程，
    # 只杀父进程会留下一堆孤儿容器
    assert signals and signals[0][0] == 999_999


def test_aborting_a_finished_job_is_a_404(api, reports):
    _job(run_id="done", status=JobStatus.COMPLETED)

    assert api.post("/api/eval/jobs/done/abort").status_code == 404


# ── 对比 ──


def test_compare_returns_the_generic_result_plus_markdown(api, reports):
    _report(reports, "a", rows=[
        {"task_id": "polyglot_cpp_x", "name": "x", "status": "passed", "passed": True,
         "reward": 1.0, "duration_s": 30.0,
         "tokens": {"input": 100, "output": 50, "reasoning": 10, "cached": 0},
         "error": None, "session_id": None, "trace_id": None, "extra": {}},
    ], arm="mycode", thinking_mode="default")
    _report(reports, "b", rows=[
        {"task_id": "polyglot_cpp_x", "name": "x", "status": "failed", "passed": False,
         "reward": 0.0, "duration_s": 60.0,
         "tokens": {"input": 200, "output": 80, "reasoning": 40, "cached": 0},
         "error": None, "session_id": None, "trace_id": None, "extra": {}},
    ], arm="opencode", thinking_mode="default")

    body = api.get("/api/eval/compare?a=a&b=b").json()

    assert body["n_paired"] == 1 and body["only_a"] == ["polyglot_cpp_x"]
    assert body["means"]["duration_s"] == {"a": 30.0, "b": 60.0}
    assert "McNemar" in body["markdown"]
    # arm 不同是两臂对比的**前提**，不能报成"变量没对齐"，否则横幅永远挂着
    assert body["variable_mismatches"] == []
    # pairs 是投影过的：整行（含 extra）太大，前端只要判定和时长
    assert set(body["pairs"][0]) == {
        "task_id", "dimension", "a_passed", "b_passed", "a_duration_s", "b_duration_s",
    }
    assert body["pairs"][0]["dimension"] == "cpp"


def test_compare_labels_are_the_callers_own_run_ids_even_for_a_legacy_report(api, reports):
    """钉住裁定要求的那条显式 `labels=(a, b)`，也就是 `compare_reports` 里
    Task 11 报过"零测试覆盖"的那个分支的**第一个生产调用方**。

    判别力来自"旧形状"：盘上已发表那一对报告**没有 `run_id` 这个顶层键**，
    所以 `_label_of` 的回退链会一路退到 `meta.arm`/`summary.arm`。
    若不传 `labels=`，这一对的标题就渲染成 `# mycode vs opencode`；
    而已归档的 `harbor_compare_final_20261018__034229.json` 实测用的是文件名主干
    （`mycode_label = 'harbor_mycode_final_20261018__025351'`）。
    用 `_report` 原样写出的报告是**带** `run_id` 的，那种形状下两条路给出一样的答案、
    这条测试就没有判别力了——所以必须删掉它。
    """
    for name, arm in (("left-run", "mycode"), ("right-run", "opencode")):
        data = _report(reports, name, arm=arm)
        del data["run_id"]
        (reports / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")

    body = api.get("/api/eval/compare?a=left-run&b=right-run").json()

    # 调用方的原话原样到达，且没被 `_dedupe_labels` 改写成 `… (a)` / `… (b)`
    assert body["labels"] == {"a": "left-run", "b": "right-run"}
    assert body["markdown"].splitlines()[0] == "# left-run vs right-run"
    # headline 也要拿到调用方的原话（它由 render_markdown 嵌进 markdown 正文）：
    # 简报要求"标签原样出现在 headline 和 markdown 里"，只钉标题行会漏掉一半
    assert "left-run 1/1 vs right-run 1/1" in body["markdown"]
    assert "mycode" not in body["markdown"].splitlines()[0]


def test_compare_surfaces_a_real_variable_mismatch(api, reports):
    _report(reports, "a", arm="mycode", thinking_mode="default")
    _report(reports, "b", arm="mycode", thinking_mode="off")

    body = api.get("/api/eval/compare?a=a&b=b").json()

    assert {"field": "variables.thinking_mode", "a": "default", "b": "off"} in body["variable_mismatches"]
    assert "变量未对齐" in body["markdown"]


def test_compare_drops_the_expected_two_arm_git_revision_mismatch(api, reports):
    """两臂不是同一秒起跑的，meta.git_revision 必然不同；它不许被报成"变量没对齐"，
    否则横幅永远挂着、也就永远没人看——这正是 ARM_KEYS 存在的全部理由。
    钉住路由真的调了 drop_expected_mismatches：把那一行去掉，这条就红（实测）。"""
    _report(reports, "a", arm="mycode", thinking_mode="default")
    b = _report(reports, "b", arm="opencode", thinking_mode="default")
    b["meta"]["git_revision"] = "fffffff"
    (reports / "b.json").write_text(json.dumps(b), encoding="utf-8")

    body = api.get("/api/eval/compare?a=a&b=b").json()

    fields = [m["field"] for m in body["variable_mismatches"]]
    assert fields == [], f"meta.git_revision 没被摘掉，横幅会永远挂着：{fields}"
    assert "变量未对齐" not in body["markdown"]


def test_compare_of_a_missing_run_is_a_404(api, reports):
    _report(reports, "a")

    assert api.get("/api/eval/compare?a=a&b=nope").status_code == 404


def test_compare_accepts_excluded_dimensions(api, reports):
    rows = [
        {"task_id": f"polyglot_{lang}_x", "name": "x", "status": "passed", "passed": True,
         "reward": 1.0, "duration_s": 1.0,
         "tokens": {"input": 1, "output": 1, "reasoning": 0, "cached": 0},
         "error": None, "session_id": None, "trace_id": None, "extra": {}}
        for lang in ("java", "rust")
    ]
    _report(reports, "a", rows=rows, arm="mycode")
    _report(reports, "b", rows=rows, arm="opencode")

    body = api.get("/api/eval/compare?a=a&b=b&exclude=java").json()

    assert body["excluded"] == ["java"] and body["n_paired"] == 1


def test_compare_of_a_comparison_output_is_a_400_not_an_empty_200(api, reports):
    """`eval/reports/` 里混着 `harbor_compare_*.json`（对比产物，不是运行记录）。

    不挡的话：`load_run` 照样读进来、`rows_of` 返回 `[]`、于是路由给出
    **200 + n_paired=0** 的空对比——对一个根本不存在的运行记录回 200 是错的，
    而且它会静默地长得像"两次运行完全没有共同题目"。
    """
    _report(reports, "a")
    (reports / "harbor_compare_final_20261018__034229.json").write_text(
        json.dumps({"mcnemar_p": 0.0625, "n_paired": 24, "pairs": [],
                    "mycode_label": "harbor_mycode_final_20261018__025351",
                    "opencode_label": "harbor_opencode_final_20261017__232016"}),
        encoding="utf-8")

    response = api.get("/api/eval/compare?a=a&b=harbor_compare_final_20261018__034229")

    assert response.status_code == 400
    assert "harbor_compare_final_20261018__034229" in response.json()["detail"]
    assert "不是一次运行记录" in response.json()["detail"]


def test_compare_of_a_run_that_recorded_no_variables_is_a_200_with_the_guard_banner(api, reports):
    """守卫判瞎的那条分支必须从 HTTP 面上给出 200 + 守卫措辞，**不是 500**。

    `_guard_blind_reason()` 的第 4 条返回路径是 `raise AssertionError`（生产路径上可证
    不可达），而这条路由把它暴露了出去：一旦"不可达"被破坏，用户拿到的就是 500。
    所以这里走一遍真实的"一侧没记录控制变量"（盘上已发表那一对就是这个形状），
    确认它渲染出横幅而不是炸。
    """
    _report(reports, "with-vars", arm="mycode")
    data = _report(reports, "no-vars", arm="opencode")
    data["meta"]["variables"] = {}
    (reports / "no-vars.json").write_text(json.dumps(data), encoding="utf-8")

    response = api.get("/api/eval/compare?a=with-vars&b=no-vars")

    assert response.status_code == 200
    body = response.json()
    assert body["variables_seen"] == {"a": 2, "b": 0}   # 结构化字段：前端按它判"有没有校验过"
    assert body["variable_mismatches"] == []            # "没得比"不是"比过且一致"
    assert "⚠️ 本次对比未经过变量校验" in body["markdown"]
    assert "no-vars 一侧没有记录控制变量" in body["markdown"]
    assert "不构成结论" in body["markdown"]


def test_compare_accepts_a_legacy_trials_shaped_report(api, reports):
    """3d 让旧形状（只有 `summary` + `trials`）进得了运行索引，对比就必须也收得进去。

    否则页面列得出这两条、一点对比就 400——而已发表那一对正是这个形状。
    顺带钉住"benchmark 退到 harbor"这条口径在三处一致（rows_of / 索引 / 本路由）：
    旧报告没有 `benchmark` 顶层键，但按语言分层和 ARM_KEYS 的过滤都该照常生效。
    """
    for name, reward in (("legacy-a", 1.0), ("legacy-b", 0.0)):
        (reports / f"{name}.json").write_text(json.dumps({
            "summary": {"arm": "mycode", "n_trials": 1, "n_passed": int(reward),
                        "pass_rate": reward},
            "trials": [{"task_name": "polyglot_cpp_x", "reward": reward,
                        "agent_execution_s": 12.5}],
        }), encoding="utf-8")

    response = api.get("/api/eval/compare?a=legacy-a&b=legacy-b")

    assert response.status_code == 200
    body = response.json()
    assert body["n_paired"] == 1 and body["only_a"] == ["polyglot_cpp_x"]
    assert body["pairs"][0]["dimension"] == "cpp"       # 按语言分层照样生效
    assert body["means"]["duration_s"] == {"a": 12.5, "b": 12.5}
    assert body["variables_seen"] == {"a": 0, "b": 0}   # 两侧都没记变量 → 判瞎，但仍是 200
    assert "⚠️ 本次对比未经过变量校验" in body["markdown"]
