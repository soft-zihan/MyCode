"""loca 的报告要带上它的控制变量，否则两次 loca 运行根本没法比。

loca 的变量比别的 benchmark 多：窗口、压缩臂、思考开关、LOCA 仓库 commit、并行度。
上一轮 harbor 的假结论就是栽在"报告里没写思考是关的"，同一个坑不能再挖一次。
"""

from __future__ import annotations

import json

from eval.common.runner_base import write_reports
from eval.common.schema import check_report
from eval.loca.runner import _summarize, loca_variables

LOCA_ROW = {
    "task_id": "64k-3",
    "name": "config_3",
    "index": 3,
    "reward": 0.75,
    "passed": False,
    "duration_s": 88.0,
    "tokens": {"input": 900, "output": 200, "reasoning": 30, "cached": 0},
    "compression_events": {"tool_folded": 2, "session_folded": 0, "events_hidden": 5},
    "trace_id": "tr-2",
    "session_id": "s-2",
    "agent_error": None,
    "eval_error": "verifier timeout",
    "step_info": {"steps": 12},
    "task_dir": "/tmp/x",
}

VARIABLES = loca_variables(
    model="qwen3.6-27b", window=65536, arm="full", thinking="off", seed=42,
    config_set="64k", select="random", max_tool_uses=40, parallel=2,
    timeout=600, loca_commit="deadbeef", api_base="http://127.0.0.1:8000/v1",
)


def test_variables_carry_everything_a_comparison_must_guard_on():
    # 少一个，变量守卫就会漏掉一个能改变结论的维度
    assert VARIABLES == {
        "model": "qwen3.6-27b",
        "window": 65536,
        "arm": "full",
        "thinking": "off",
        "seed": 42,
        "config_set": "64k",
        "select": "random",
        "max_tool_uses": 40,
        "parallel": 2,
        "timeout_s": 600,
        "loca_commit": "deadbeef",
        "api_base": "http://127.0.0.1:8000/v1",
    }


def test_a_report_written_the_way_loca_writes_it_is_conformant(tmp_path):
    rows = [LOCA_ROW]
    meta = {
        "benchmark_detail": "LOCA-bench (hkust-nlp)",
        "config_set": "64k",
        "model": "qwen3.6-27b",
        "arm": "full",
        "seed": 42,
        "git_revision": "ab6b631",
        "task_ids": [r["task_id"] for r in rows],
        "variables": VARIABLES,
    }

    json_path, md_path = write_reports(
        "loca", "loca-20261018_000000-abc123", meta, _summarize(rows, 1), rows,
        reports_dir=tmp_path, report_stem="loca-20261018_000000-abc123",
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))

    assert check_report(report) == []
    assert report["meta"]["variables"]["window"] == 65536
    assert md_path.exists()


def test_loca_specific_fields_survive_in_extra(tmp_path):
    rows = [LOCA_ROW]
    json_path, _ = write_reports(
        "loca", "r", {"variables": VARIABLES}, _summarize(rows, 1), rows,
        reports_dir=tmp_path, report_stem="r",
    )

    row = json.loads(json_path.read_text(encoding="utf-8"))["results"][0]

    # 压缩事件计数是 loca 消融实验的**因变量**，归一化不能把它吃掉
    assert row["extra"]["compression_events"]["tool_folded"] == 2
    assert row["extra"]["step_info"] == {"steps": 12}
    assert row["extra"]["index"] == 3
    assert row["reward"] == 0.75
    assert row["error"] == "verifier timeout"
    assert row["status"] == "failed"


def test_the_event_stream_and_the_report_share_one_variable_list():
    """事件流和报告必须给同一份控制变量。

    原来 main() 里有两份手写清单，10 键那份有 api_base 没有 arm/loca_commit，
    11 键那份反过来——实时 UI 因此看不到压缩臂，而压缩臂正是 loca 消融实验的自变量。
    这条测试钉不住 main() 的接线（它不可测），但钉得住 loca_variables 的键集：
    任何一维从这份清单里掉出去，两个出口就同时瞎掉。
    """
    assert set(loca_variables(
        model="m", window=1, arm="full", thinking="off", seed=1, config_set="64k",
        select="random", max_tool_uses=1, parallel=1, timeout=1,
        loca_commit="c", api_base="http://x",
    )) == {
        "model", "window", "arm", "thinking", "seed", "config_set", "select",
        "max_tool_uses", "parallel", "timeout_s", "loca_commit", "api_base",
    }
