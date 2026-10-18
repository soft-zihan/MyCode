"""契约：不管哪个 benchmark，写出来的报告都得是同一份信封。

走真实路径而不是手搓字典——手搓的字典只能证明"我写的归一化能处理我想象的输入"，
而这五个 benchmark 的行字典是各自独立长出来的，形状差异正是要防的东西。
"""

from __future__ import annotations

import json
from typing import get_args

import pytest

from eval.common.benchmarks import report_rows, summarize_results, task_status
from eval.common.models import BenchmarkName, EvalRunOptions, EvalRunState, EvalTaskResult
from eval.common.runner_base import write_reports
from eval.common.schema import ROW_CORE, TOKEN_FIELDS, check_report
from eval.harbor.runner import collect_trials
from eval.loca.runner import _summarize

ALL_BENCHMARKS = ("gaia", "hle", "smoke", "loca", "harbor")


def test_the_contract_covers_every_benchmark_the_project_knows_about():
    """新加一个 benchmark 而忘了让它过契约，这条就红。

    `ALL_BENCHMARKS` 原来是手写元组：漏掉一个，这个文件对它就一个字都没断言，
    而闸门"看起来还是绿的"。评审实测过这个失效模式——把 harbor 形状的行以
    benchmark="sixth" 写出去，归一化产出 task_id='' / token 全 None 的垃圾行，
    而 check_report、ROW_CORE 键集、TOKEN_FIELDS 键集三条断言全部通过。
    """
    assert set(ALL_BENCHMARKS) == set(get_args(BenchmarkName)), (
        " benchmark 的取值域变了，契约测试没跟上：新增的那个 benchmark "
        "现在完全不受这个文件约束"
    )


def _state(benchmark: str) -> EvalRunState:
    state = EvalRunState(
        run_id=f"{benchmark}-20261018_000000-abc123",
        benchmark=benchmark,
        eval_session_id=f"eval-{benchmark}-x",
        options=EvalRunOptions(benchmark=benchmark),
        model="qwen3.6-27b",
    )
    state.tasks = [
        EvalTaskResult(
            task_id=f"{benchmark}-{i}",
            benchmark=benchmark,
            name=f"task {i}",
            status="passed" if i == 1 else "failed",
            expected="2019",
            predicted="2019" if i == 1 else "2020",
            correct=i == 1,
            passed=i == 1,
            duration_s=12.5 * i,
            tokens={"input": 1000 * i, "output": 100 * i, "reasoning": 10 * i, "cached": 0},
            trace_id=f"trace-{i}",
            session_id=f"sess-{i}",
            metadata={"level": 3, "category": "x"},
        )
        for i in (1, 2)
    ]
    state.summary = summarize_results(state.tasks)
    return state


def _loca_rows() -> list[dict]:
    return [
        {
            "task_id": "64k-1", "name": "config_1", "index": 1, "reward": 1.0,
            "passed": True, "duration_s": 40.0,
            "tokens": {"input": 900, "output": 200, "reasoning": 20, "cached": 0},
            "compression_events": {"tool_folded": 1, "session_folded": 0, "events_hidden": 2},
            "trace_id": "tr-1", "session_id": "s-1",
            "agent_error": None, "eval_error": None,
            "step_info": {"steps": 8}, "task_dir": "/tmp/a",
        }
    ]


def _harbor_trials(tmp_path) -> list[dict]:
    trial = tmp_path / "polyglot_cpp_yacht__1"
    trial.mkdir(parents=True, exist_ok=True)
    # 时间戳必须带小数秒：_phase_seconds 用 "%Y-%m-%dT%H:%M:%S.%fZ" 解析，%f 是必需位，
    # 写成 "02:54:00Z" 会 ValueError → agent_execution_s=None → duration_s 被归一化成 0.0，
    # harbor 这一支就再也没走到时长那条路（同 test_harbor_report_envelope.py 的夹具注释）
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "polyglot_cpp_yacht",
                "verifier_result": {"rewards": {"reward": 1.0}},
                "agent_result": {"n_input_tokens": 47582, "n_output_tokens": 1405,
                                 "n_cache_tokens": 0,
                                 "metadata": {"mycode_reasoning_tokens": 308}},
                "agent_execution": {"started_at": "2026-10-18T02:54:00.000Z",
                                    "finished_at": "2026-10-18T02:54:30.000Z"},
            }
        ),
        encoding="utf-8",
    )
    # 第二条：一条**无判定**的 trial，照抄探测批 opencode 臂的实测形状
    # （eval/reports/harbor_opencode_probe_20261018__220217.json 里那条真实的
    # polyglot_rust_gigasecond：agent_setup 正好撞上 1080s 超时上限）。
    # 没有 verifier_result（rewards 里自然也没有 reward）、没有 agent_execution 键、
    # 没有 agent_result。collect_trials 实测产出：reward=None、agent_execution_s=None、
    # agent_setup_s=1080.0、environment_setup_s=46.1、no_reward_file=False；
    # normalize_row 实测：status="error"、passed=None、duration_s=0.0、tokens 四键全 None。
    # 没有这条夹具，harbor 在这个文件里永远产不出无判定行，
    # "n_unscored 是 harbor 的命门"就只是一句没有断言支撑的注释
    unscored = tmp_path / "polyglot_rust_gigasecond__1"
    unscored.mkdir(parents=True, exist_ok=True)
    (unscored / "result.json").write_text(
        json.dumps(
            {
                "task_name": "polyglot_rust_gigasecond",
                "trial_name": "polyglot_rust_gigasecond__1",
                "exception_info": {
                    "exception_type": "AgentSetupTimeoutError",
                    "exception_message": "Agent setup timed out after 1080.0 seconds",
                },
                "environment_setup": {"started_at": "2026-10-18T02:59:00.000Z",
                                      "finished_at": "2026-10-18T02:59:46.100Z"},
                "agent_setup": {"started_at": "2026-10-18T03:00:00.000Z",
                                "finished_at": "2026-10-18T03:18:00.000Z"},
            }
        ),
        encoding="utf-8",
    )
    return collect_trials(tmp_path)


def _rows_for(benchmark: str, tmp_path) -> list[dict]:
    if benchmark in ("gaia", "hle", "smoke"):
        return report_rows(_state(benchmark))
    if benchmark == "loca":
        return _loca_rows()
    return _harbor_trials(tmp_path)


def _summary_for(benchmark: str, rows: list[dict]) -> dict:
    if benchmark in ("gaia", "hle", "smoke"):
        return summarize_results(_state(benchmark).tasks)
    if benchmark == "loca":
        return _summarize(rows, len(rows))
    from eval.harbor.runner import summarize

    return summarize("mycode", "final", rows)


@pytest.mark.parametrize("benchmark", ALL_BENCHMARKS)
def test_every_benchmark_writes_a_conformant_report(benchmark, tmp_path):
    rows = _rows_for(benchmark, tmp_path)
    meta = {"model": "qwen3.6-27b", "seed": 42, "git_revision": "ab6b631", "variables": {}}

    json_path, md_path = write_reports(
        benchmark, f"{benchmark}-20261018_000000-abc123", meta,
        _summary_for(benchmark, rows), rows, reports_dir=tmp_path,
        report_stem=f"{benchmark}-20261018_000000-abc123",
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    assert check_report(report) == []
    assert report["benchmark"] == benchmark
    assert report["run_id"] == json_path.stem
    assert md_path.exists()


# 每个 benchmark 的 extra 里必然有的溯源键：只有正确那个适配器跑过才会长成这样。
# ROW_CORE / TOKEN_FIELDS 那两条断言对任何输入都不可能失败（normalize_row 返回的是
# 写死这 11 个键的字面量），所以它们锁的是"写入前归一化过"，不是"各臂形状一致"。
EXPECTED_EXTRA_KEYS = {
    "gaia": {"expected", "predicted"},
    "hle": {"expected", "predicted"},
    "smoke": {"expected", "predicted"},
    "loca": {"compression_events"},
    "harbor": {"trial_name", "agent_setup_s"},
}
ABSENT_EXTRA_KEYS = {
    "gaia": {"correct", "benchmark"},   # 被 consumed 吃掉：correct 映射进 passed
    "hle": {"correct", "benchmark"},
    "smoke": {"correct", "benchmark"},
    "loca": set(),
    "harbor": {"task_name", "n_input_tokens"},  # 被 consumed 吃掉：改名进 task_id / tokens
}


@pytest.mark.parametrize("benchmark", ALL_BENCHMARKS)
def test_every_benchmark_lands_on_the_same_row_shape(benchmark, tmp_path):
    rows = _rows_for(benchmark, tmp_path)

    json_path, _ = write_reports(
        benchmark, "r", {"variables": {}}, _summary_for(benchmark, rows), rows,
        reports_dir=tmp_path, report_stem="r",
    )
    written = json.loads(json_path.read_text(encoding="utf-8"))["results"]

    for row in written:
        assert set(row) == set(ROW_CORE), f"{benchmark} 的行多/少了字段"
        assert set(row["tokens"]) == set(TOKEN_FIELDS)
        # 下面这两条才是真有判别力的：extra 的键集只有正确那个适配器跑过
        # 才会长成这样——走错适配器，该在的键不在、该被消费掉的键反而留在 extra 里
        assert EXPECTED_EXTRA_KEYS[benchmark] <= set(row["extra"]), (
            f"{benchmark} 的 extra 缺溯源键 {sorted(EXPECTED_EXTRA_KEYS[benchmark])}："
            "正确的适配器没跑过"
        )
        assert set(row["extra"]).isdisjoint(ABSENT_EXTRA_KEYS[benchmark]), (
            f"{benchmark} 的 extra 里混进了本该被适配器消费掉的键 "
            f"{sorted(ABSENT_EXTRA_KEYS[benchmark])}"
        )


@pytest.mark.parametrize("benchmark", ALL_BENCHMARKS)
def test_passed_is_the_only_scoring_field(benchmark, tmp_path):
    # gaia/hle 原本用 correct、harbor 用 reward、loca 用 passed+reward。
    # 对比层只认 passed——这条钉住"三个口径都映射过来了"
    rows = _rows_for(benchmark, tmp_path)

    json_path, _ = write_reports(
        benchmark, "r", {"variables": {}}, _summary_for(benchmark, rows), rows,
        reports_dir=tmp_path, report_stem="r",
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    assert any(r["passed"] is True for r in report["results"])
    assert report["summary"]["n_passed"] >= 1
    assert report["summary"]["n_passed"] == sum(
        1 for r in report["results"] if r["passed"] is True
    )


def test_the_summary_core_means_the_same_thing_across_benchmarks(tmp_path):
    # n_unscored 是 harbor 的命门（"跑了没通过"和"没跑起来"不能互相冒充），
    # 但对 gaia 也必须存在且为 0，否则对比层要为每个 benchmark 分支。
    # harbor 那一档现在是**正面断言**：夹具里有一条 AgentSetupTimeoutError 的
    # 无判定 trial，命门由这个文件自己验到，不再只靠 test_harbor_report_envelope.py
    seen = {}
    counts = {}
    for benchmark in ALL_BENCHMARKS:
        rows = _rows_for(benchmark, tmp_path / benchmark)
        counts[benchmark] = len(rows)
        json_path, _ = write_reports(
            benchmark, "r", {"variables": {}}, _summary_for(benchmark, rows), rows,
            reports_dir=tmp_path / benchmark, report_stem="r",
        )
        seen[benchmark] = json.loads(json_path.read_text(encoding="utf-8"))

    for benchmark, report in seen.items():
        summary = report["summary"]
        # 用写进去的那份 rows 的行数，不要重新调一次 _rows_for：
        # harbor 那一支会在 tmp_path 里建目录、再 collect_trials 一遍，
        # 而"写进去的"和"重新算的"本来就该是同一批行——重算只会掩盖两者的分歧
        assert summary["n_tasks"] == counts[benchmark]
        assert summary["n_scored"] + summary["n_unscored"] == summary["n_tasks"]
        if benchmark == "harbor":
            assert summary["n_unscored"] == 1
            unscored = [r for r in report["results"] if r["passed"] is None]
            assert len(unscored) == 1, "n_unscored==1 就得恰好有一条 passed is None 的行"
            assert unscored[0]["status"] == "error"
        else:
            assert summary["n_unscored"] == 0


def test_an_error_row_is_scored_as_a_failure_not_as_unscored(tmp_path):
    """钉住口径：gaia/hle/smoke 的 error 行**进分母**，算没过，不算"没判分"。

    这是 Task 7 评审提出、控制器裁定的那一处跨 benchmark 差异：harbor 一条根本没有
    reward 的 trial 是 `passed=None` / `unscored`（不进分母），而 gaia 系的 error 行是
    `passed=False`（进分母）。两者不一致，但**故意**保留：
    (a) 同一个 benchmark 的两条臂走同一个适配器，口径在臂间一致——对比层要的就是这个；
    (b) 改掉会追溯性地移动所有既有 gaia/hle 基线数字，而那些数字是回归基线；
    (c) 方向保守：只会压低通过率，不会虚高。
    所以这条测试不是"顺手加的覆盖"，它是**不许再改回去**的锁。要改口径，先改这条测试，
    并且接受所有历史 pass_rate 作废。
    """
    state = EvalRunState(
        run_id="gaia-20261018_000000-abc123",
        benchmark="gaia",
        eval_session_id="eval-gaia-x",
        options=EvalRunOptions(benchmark="gaia"),
        model="qwen3.6-27b",
    )
    state.tasks = [
        EvalTaskResult(
            task_id="gaia-ok", benchmark="gaia", name="ok",
            status=task_status(None, True), correct=True, passed=True, duration_s=10.0,
        ),
        EvalTaskResult(
            task_id="gaia-boom", benchmark="gaia", name="boom",
            status=task_status("tool call timed out", False),
            correct=False, passed=False, duration_s=30.0,
            error="tool call timed out",
        ),
    ]
    rows = report_rows(state)

    json_path, _ = write_reports(
        "gaia", state.run_id, {"variables": {}}, summarize_results(state.tasks), rows,
        reports_dir=tmp_path, report_stem="r",
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    boom = next(r for r in report["results"] if r["task_id"] == "gaia-boom")

    assert boom["status"] == "error", "已有的 status 不能被推导覆盖"
    assert boom["passed"] is False, "error 行是'做错了'，不是'没判分'"
    assert report["summary"]["n_scored"] == 2
    assert report["summary"]["n_unscored"] == 0
    assert report["summary"]["n_passed"] == 1
    assert report["summary"]["pass_rate"] == 0.5


def test_schema_module_stays_a_leaf():
    """schema.py 不许 import 项目内任何模块——一个反向依赖就成了导入环。

    runner_base 要 import schema，而 jobs.py 又要 import runner_base。
    环的症状不是"某个函数不对"，是 `import eval.common.jobs` 直接失败、
    整个执行层没了，所以这条约束值得一条测试看着。
    用 AST 而不是真的去 import：真 import 只能证明"今天没环"，
    证明不了"schema 没有项目内依赖"（后者是前者的充分条件，也更好定位）。
    """
    import ast
    from pathlib import Path

    from eval.common import schema as schema_module

    # 用 __file__ 而不是 CWD 相对路径：断言的必须是"真的被 import 进来的那一份"，
    # 而且 tests/unit 里既有的路径写法一律锚在 __file__ 上（test_event_manifest.py 等）
    source = Path(schema_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # 相对 import 也是项目内依赖，而且正是最容易写出环的那种
                # （`from .jobs import x`）。只看 level == 0 会把它整类放行，
                # 闸门就漏了一整个口子——已实测：注入 `from . import benchmarks`
                # 后 offenders 是空的。
                imported.append("." * node.level + (node.module or ""))
            elif node.module:
                imported.append(node.module)

    allowed = {"__future__", "collections.abc", "typing"}
    offenders = [m for m in imported if m.split(".")[0] not in
                 {a.split(".")[0] for a in allowed}]
    assert offenders == [], f"schema.py 不再是叶子模块，多出了这些 import: {offenders}"
