from __future__ import annotations

import asyncio
import json

import eval.common.service as service_module
from eval.common.benchmarks import BenchmarkAdapter
from eval.common.models import EvalRunOptions, EvalRunState, EvalTask, EvalTaskResult


class FakeAdapter(BenchmarkAdapter):
    benchmark = "smoke"

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.setup_called = False
        self.teardown_called = False

    def load_tasks(self, options: EvalRunOptions) -> list[EvalTask]:
        return [
            EvalTask(
                task_id="task-1",
                benchmark="smoke",
                prompt="hello",
                name="Task 1",
                expected_output="ok",
            )
        ]

    async def setup(self, state: EvalRunState, options: EvalRunOptions) -> None:
        self.setup_called = True

    async def run_task(
        self,
        state: EvalRunState,
        task: EvalTask,
        result: EvalTaskResult,
        options: EvalRunOptions,
        index: int,
        total: int,
        emit,
    ) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        result.passed = True
        result.correct = True
        result.status = "passed"
        result.predicted = "ok"
        result.duration_s = 0.1
        emit({"type": "eval/task_event", "task_id": task.task_id, "payload": {"type": "fake"}})

    async def teardown(self, state: EvalRunState, options: EvalRunOptions) -> None:
        self.teardown_called = True


def test_eval_service_runs_fake_benchmark(monkeypatch, tmp_path):
    monkeypatch.setattr(service_module, "REPORTS_DIR", tmp_path)
    adapter = FakeAdapter()
    monkeypatch.setattr(service_module, "get_adapter", lambda benchmark: adapter)

    service = service_module.EvalService()
    events: list[dict] = []
    options = EvalRunOptions(
        benchmark="smoke",
        skip_langfuse=True,
        sync_langfuse_dataset=False,
        execution_mode="backend_session",
    )

    state = asyncio.run(service.run(service.create_state(options), options))

    assert state["status"] == "completed"
    assert state["summary"]["passed"] == 1
    assert state["summary"]["pass_at_1"] == 1.0
    assert adapter.setup_called
    assert adapter.teardown_called

    run_id = state["run_id"]
    state_file = tmp_path / f"{run_id}.state.json"
    report_file = tmp_path / f"{run_id}.json"
    md_file = tmp_path / f"{run_id}.md"
    assert state_file.exists()
    assert report_file.exists()
    assert md_file.exists()

    saved_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved_state["tasks"][0]["status"] == "passed"
    assert "api_key" not in saved_state
    assert "api_key" not in json.dumps(saved_state)

    runs = service_module.list_report_runs()
    assert any(run["run_id"] == run_id for run in runs)


def test_eval_service_emits_progress_events(monkeypatch, tmp_path):
    monkeypatch.setattr(service_module, "REPORTS_DIR", tmp_path)
    adapter = FakeAdapter()
    monkeypatch.setattr(service_module, "get_adapter", lambda benchmark: adapter)

    service = service_module.EvalService()
    events: list[dict] = []
    options = EvalRunOptions(
        benchmark="smoke",
        skip_langfuse=True,
        sync_langfuse_dataset=False,
    )
    state = service.create_state(options)
    service.attach_emitter(state.run_id, events.append)

    asyncio.run(service.run(state, options))

    types = [event["type"] for event in events]
    assert "eval/run_started" in types
    assert "eval/task_started" in types
    assert "eval/task_event" in types
    assert "eval/task_finished" in types
    assert "eval/run_progress" in types
    assert "eval/run_finished" in types


def test_eval_service_abort_marks_task_aborted(monkeypatch, tmp_path):
    monkeypatch.setattr(service_module, "REPORTS_DIR", tmp_path)
    adapter = FakeAdapter(delay=0.2)
    monkeypatch.setattr(service_module, "get_adapter", lambda benchmark: adapter)

    async def scenario():
        service = service_module.EvalService()
        options = EvalRunOptions(
            benchmark="smoke",
            skip_langfuse=True,
            sync_langfuse_dataset=False,
        )
        state = service.create_state(options)
        task = asyncio.create_task(service.run(state, options))
        service.tasks[state.run_id] = task
        await asyncio.sleep(0)
        assert service.abort_run(state.run_id) is True
        try:
            await task
        except asyncio.CancelledError:
            pass
        return state

    state = asyncio.run(scenario())
    assert state.status == "aborted"
    assert state.tasks[0].status == "aborted"


class FakeLangfuseClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def create_dataset(self, name, description=""):
        self.calls.append(("create_dataset", {"name": name, "description": description}))
        return {"name": name}

    def upsert_dataset_item(self, **kwargs):
        self.calls.append(("upsert_dataset_item", kwargs))
        return {"id": kwargs.get("item_id", "item")}

    def create_dataset_run_item(self, **kwargs):
        self.calls.append(("create_dataset_run_item", kwargs))
        return {"id": "run-item"}

    def create_score(self, **kwargs):
        self.calls.append(("create_score", kwargs))
        return {"id": "score"}


def test_sync_task_to_dataset_creates_item_run_and_score():
    from eval.common.langfuse_dataset import sync_task_to_dataset
    from eval.common.models import EvalRunOptions, EvalRunState, EvalTask, EvalTaskResult

    client = FakeLangfuseClient()
    options = EvalRunOptions(benchmark="gaia", sample=1, sync_langfuse_dataset=True)
    state = EvalRunState(
        run_id="gaia-test-run",
        benchmark="gaia",
        eval_session_id="eval-gaia-test-run",
        options=options,
        model="test-model",
    )
    task = EvalTask(
        task_id="task-123",
        benchmark="gaia",
        prompt="What is 2+2?",
        expected_output="4",
        metadata={"question": "What is 2+2?"},
    )
    result = EvalTaskResult(
        task_id="task-123",
        benchmark="gaia",
        status="passed",
        expected="4",
        predicted="4",
        correct=True,
        passed=True,
        trace_id="trace-123",
        session_id="session-123",
    )

    status = sync_task_to_dataset(client, task, result, state, options)

    assert status["synced"] is True
    assert status["scored"] is True
    assert status["dataset"] == "gaia-eval"
    assert status["item_id"] == "gaia-task-123"
    assert status["run_name"] == "gaia-test-run"

    call_names = [name for name, _ in client.calls]
    assert call_names == ["create_dataset", "upsert_dataset_item", "create_dataset_run_item", "create_score"]

    run_item_kwargs = dict(client.calls[2][1])
    assert run_item_kwargs["trace_id"] == "trace-123"
    assert run_item_kwargs["dataset_item_id"] == "gaia-task-123"

    score_kwargs = dict(client.calls[3][1])
    assert score_kwargs["name"] == "benchmark_correct"
    assert score_kwargs["value"] is True
    assert score_kwargs["trace_id"] == "trace-123"
