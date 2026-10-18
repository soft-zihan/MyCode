"""EvalService 的事件要同时落盘：这是"服务重启后还能回放这轮发生了什么"的唯一来源。"""

from __future__ import annotations

import json
import os

from eval.common.benchmarks import BenchmarkAdapter
from eval.common.events import EVENTS_PATH_ENV, RUN_ID_ENV
from eval.common.models import EvalRunOptions, EvalTask


class _OneTaskAdapter(BenchmarkAdapter):
    """最小适配器：一题、必过，用来驱动 EvalService.run() 的完整生命周期。

    setup/teardown 直接继承基类的 async no-op；run_task 必须按 `BenchmarkAdapter`
    的真实签名写（原地改 `result`，不返回新对象），否则 `await adapter.setup(...)`
    会 TypeError，run() 根本走不到发事件那一步。
    """

    def load_tasks(self, options):
        return [EvalTask(task_id="t1", benchmark=options.benchmark, prompt="p", name="n")]

    async def run_task(self, state, task, result, options, index, total, emit):
        result.status = "passed"
        result.passed = True
        result.correct = True
        result.duration_s = 0.1
        result.tokens = {"input": 1, "output": 1}


async def test_run_writes_events_to_jsonl_when_env_is_set(tmp_path, monkeypatch):
    from eval.common import service as svc

    events_path = tmp_path / "e.jsonl"
    monkeypatch.setenv(EVENTS_PATH_ENV, str(events_path))
    monkeypatch.setenv(RUN_ID_ENV, "fixed-run")
    monkeypatch.setattr(svc, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(svc, "get_adapter", lambda name: _OneTaskAdapter())
    monkeypatch.setattr(svc, "init_eval_tracing", lambda *a, **k: None)
    monkeypatch.setattr(
        svc, "resolve_model_config", lambda *a, **k: ("m", "https://x/v1", "k")
    )

    options = EvalRunOptions(benchmark="hle", sample=1, skip_langfuse=True,
                             sync_langfuse_dataset=False)
    state = svc.EvalService().create_state(options)
    await svc.EvalService().run(state, options)

    lines = [json.loads(x) for x in events_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    types = [e["type"] for e in lines]
    assert "eval/run_started" in types
    assert "eval/task_finished" in types
    assert "eval/run_finished" in types
    # 每条都带 run_id，否则多轮混在一个文件里就分不开了
    assert all(e["run_id"] for e in lines)


async def test_run_does_not_create_a_file_when_env_is_unset(tmp_path, monkeypatch):
    # 直接敲 CLI 的老路径必须一点不变
    from eval.common import service as svc

    monkeypatch.delenv(EVENTS_PATH_ENV, raising=False)
    monkeypatch.setattr(svc, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(svc, "get_adapter", lambda name: _OneTaskAdapter())
    monkeypatch.setattr(svc, "init_eval_tracing", lambda *a, **k: None)
    monkeypatch.setattr(
        svc, "resolve_model_config", lambda *a, **k: ("m", "https://x/v1", "k")
    )

    options = EvalRunOptions(benchmark="hle", sample=1, skip_langfuse=True,
                             sync_langfuse_dataset=False)
    service = svc.EvalService()
    state = service.create_state(options)
    await service.run(state, options)

    assert list(tmp_path.glob("*.jsonl")) == []


def test_create_state_uses_the_run_id_from_the_environment(tmp_path, monkeypatch):
    """被 start_job 起的子进程：run_id 由父进程定，索引/事件/报告三者必须同名。

    不钉这一条，报告落在自己造的 <Y>.json 而 finish_job 找的是 <X>.json，
    一轮成功的评测会被判成"退出码 0 但报告未落盘"。
    """
    from eval.common import service as svc

    monkeypatch.setenv(RUN_ID_ENV, "job-run-id-from-parent")
    monkeypatch.setattr(svc, "REPORTS_DIR", tmp_path)

    options = EvalRunOptions(benchmark="hle", sample=1, skip_langfuse=True)
    state = svc.EvalService().create_state(options)

    assert state.run_id == "job-run-id-from-parent"
    # eval_session_id 是跟着 run_id 走的，不能还是自造的那个
    assert state.eval_session_id == "eval-job-run-id-from-parent"


def test_create_state_still_makes_its_own_run_id_when_the_env_is_unset(tmp_path, monkeypatch):
    # CLI 直跑的老路径：没有父进程给 run_id，必须照旧自己造
    from eval.common import service as svc

    monkeypatch.delenv(RUN_ID_ENV, raising=False)
    monkeypatch.setattr(svc, "REPORTS_DIR", tmp_path)

    options = EvalRunOptions(benchmark="hle", sample=1, skip_langfuse=True)
    state = svc.EvalService().create_state(options)

    assert state.run_id.startswith("hle-")
    assert state.run_id != os.environ.get(RUN_ID_ENV)
