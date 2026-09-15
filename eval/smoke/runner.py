"""对话冒烟评测 runner — 走真实服务（HTTP + WebSocket），断言最终状态 + Langfuse trace 校验。

流程（对应实施计划 5.3）：
1. 前置：后端已启动（./start.sh restart），GET /api/health 通过
2. 逐任务：建临时工作区 → POST /api/chat/stream（bypassPermissions）
   → WS 收事件直到 turn/end → 断言（回复内容/工具/子智能体/文件产物）
3. Langfuse 校验：按 sessionId 拉 trace，断言 Span 树结构
   （turn span + event_range 完整 + GENERATION usage），跑 code evaluators 提交 score
4. 报告落盘 eval/reports/smoke_*.json/.md

用法：
    .venv/bin/python -m eval.smoke.runner                # 全部任务
    .venv/bin/python -m eval.smoke.runner --only read_file shell_exec
    .venv/bin/python -m eval.smoke.runner --skip-langfuse
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests  # noqa: E402
import websockets  # noqa: E402

from eval.common.runner_base import REPORTS_DIR  # noqa: E402

CONVERSATIONS_PATH = Path(__file__).parent / "conversations.jsonl"
COMPREHENSIVE_PATH = Path(__file__).parent / "comprehensive.jsonl"
DEFAULT_BASE_URL = "http://localhost:5555"
DEFAULT_WS_URL = "ws://localhost:5555/ws/events"


def load_tasks(only: list[str] | None = None, suite: str = "smoke") -> list[dict]:
    """加载评测任务。
    
    Args:
        only: 只加载指定 id 的任务
        suite: 测试套件 - "smoke"(默认 30 个单元) 或 "comprehensive"(5 个综合场景)
    """
    path = COMPREHENSIVE_PATH if suite == "comprehensive" else CONVERSATIONS_PATH
    tasks = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if only:
        tasks = [t for t in tasks if t["id"] in only]
        missing = set(only) - {t["id"] for t in tasks}
        if missing:
            raise SystemExit(f"未知任务: {missing}")
    return tasks


class EventListener:
    """WS 事件监听器：后台收事件，按 session_id 过滤查询。"""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.events: list[dict] = []
        self._ws = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._ws = await websockets.connect(self.ws_url, ping_interval=None, max_size=16 * 1024 * 1024)
        self._task = asyncio.create_task(self._recv_loop())

    async def _recv_loop(self) -> None:
        try:
            async for msg in self._ws:
                try:
                    self.events.append(json.loads(msg))
                except json.JSONDecodeError:
                    continue
        except websockets.exceptions.ConnectionClosed:
            pass

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._ws:
            await self._ws.close()

    def session_events(self, session_id: str) -> list[dict]:
        return [e for e in self.events if e.get("session_id") == session_id]

    def turn_end_count(self, session_id: str) -> int:
        return sum(1 for e in self.session_events(session_id) if e.get("type") == "turn/end")

    async def wait_turn_end(self, session_id: str, expected_count: int, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.turn_end_count(session_id) >= expected_count:
                return True
            await asyncio.sleep(0.5)
        return False


def setup_workspace(task: dict) -> Path:
    if task.get("use_real_workspace"):
        ws = PROJECT_ROOT / "eval" / "workspace"
        # 清理 wiki 目录，避免残留污染评测
        wiki_dir = ws / ".mycode" / "wiki"
        if wiki_dir.exists():
            import shutil
            # 清理 workflow_pattern 和 skills
            for subdir in ["workflow_pattern", "knowledge", "task_notes"]:
                p = wiki_dir / subdir
                if p.exists():
                    shutil.rmtree(p, ignore_errors=True)
            # 清理 WIKI.md 索引
            wiki_index = wiki_dir / "WIKI.md"
            if wiki_index.exists():
                wiki_index.unlink()
        # 清理 skills 目录
        skills_dir = ws / ".mycode" / "skills"
        if skills_dir.exists():
            import shutil
            shutil.rmtree(skills_dir, ignore_errors=True)
        return ws
    ws = Path(tempfile.mkdtemp(prefix=f"smoke_{task['id']}_"))
    for f in task.get("setup", []):
        p = ws / f["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"], encoding="utf-8")
    return ws


def send_chat(base_url: str, message: str, session_id: str | None, cwd: str) -> dict:
    payload: dict[str, Any] = {
        "message": message,
        "cwd": cwd,
        "permission_mode": "bypassPermissions",
    }
    if session_id:
        payload["session_id"] = session_id
    resp = requests.post(f"{base_url}/api/chat/stream", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def check_assertions_for_phase(phase: dict, workspace: Path, events: list[dict], turn_events: list[dict], window_events: list[dict]) -> list[str]:
    """为多阶段测试检查断言。"""
    expect = phase.get("expect", {})
    failures = []

    assistant_text = " ".join(
        str(e.get("content", "")) for e in turn_events if e.get("type") == "assistant_message"
    )
    
    for needle in expect.get("response_contains", []):
        if isinstance(needle, list):
            if not any(n in assistant_text for n in needle):
                failures.append(f"response 缺少任一 {needle}（实际: {assistant_text[:120]!r}）")
        else:
            if needle not in assistant_text:
                failures.append(f"response 缺少 {needle!r}（实际: {assistant_text[:120]!r}）")

    tools_used = {e.get("name") for e in events if e.get("type") == "tool_call"}
    
    for tool in expect.get("tools_used", []):
        if tool not in tools_used:
            failures.append(f"未调用工具 {tool}（实际: {sorted(t for t in tools_used if t)}）")
    
    for group in expect.get("tools_used_any", []):
        if not any(t in tools_used for t in group):
            failures.append(f"未调用任一工具 {group}（实际: {sorted(t for t in tools_used if t)}）")

    if expect.get("sub_agent_events"):
        if not any(e.get("sub_agent_id") for e in window_events):
            failures.append("未观测到子智能体事件（sub_agent_id）")

    for f in expect.get("files", []):
        p = workspace / f["path"]
        if not p.exists():
            failures.append(f"文件未创建: {f['path']}")
        elif "contains" in f and f["contains"] not in p.read_text(encoding="utf-8", errors="replace"):
            failures.append(f"文件 {f['path']} 内容缺少 {f['contains']!r}")

    errors = [e for e in turn_events if e.get("type") == "error"]
    if errors:
        failures.append(f"出现 error 事件: {str(errors[0].get('message'))[:100]}")
    reasons = [e.get("reason") for e in turn_events if e.get("type") == "turn/end"]
    if reasons and reasons[-1] != "completed":
        failures.append(f"turn/end reason={reasons[-1]}（期望 completed）")

    # wiki 断言
    wiki_injections = [e for e in events if e.get("type") == "memory_injection"]
    wiki_text = " ".join(str(e.get("content", "")) for e in wiki_injections)
    
    wiki_recalled_expect = expect.get("wiki_recalled")
    if wiki_recalled_expect is True:
        # wiki_recalled: true 表示只要有任何 wiki 召回即可
        if not wiki_text.strip():
            failures.append("wiki 未召回任何内容（期望有召回）")
    elif wiki_recalled_expect:
        # wiki_recalled: [paths] 表示必须召回指定路径
        for path in wiki_recalled_expect:
            if path not in wiki_text:
                failures.append(f"wiki 未召回 {path}（实际召回: {wiki_text[:200]!r}）")
    
    for needle in expect.get("wiki_content_contains", []):
        if needle not in wiki_text:
            failures.append(f"wiki 召回内容缺少 {needle!r}")
    
    for path in expect.get("wiki_not_recalled", []):
        if path in wiki_text:
            failures.append(f"wiki 不应召回 {path} 但被召回了")

    return failures


def check_assertions(task: dict, workspace: Path, events: list[dict], turn_events: list[dict], window_events: list[dict]) -> list[str]:
    """返回失败原因列表（空 = 通过）。

    events=本 session 全部事件，turn_events=最后一轮，
    window_events=任务期间到达的全局事件窗口（含子智能体事件——
    其 session_id 字段是子会话 id，仅投递目标是父会话，不能用父 id 过滤）。
    
    断言格式：
    - response_contains: list[str] 或 list[list[str]]
      - str: 必须包含该词
      - list[str]: 至少包含其中一个（OR 逻辑）
    - tools_used: list[str] 必须使用的工具（全部必须调用）
    - tools_used_any: list[list[str]] 每组至少调用一个
    """
    expect = task.get("expect", {})
    failures = []

    assistant_text = " ".join(
        str(e.get("content", "")) for e in turn_events if e.get("type") == "assistant_message"
    )
    
    # response_contains: 支持 str（必须包含）或 list[str]（任一包含）
    for needle in expect.get("response_contains", []):
        if isinstance(needle, list):
            # OR 逻辑：任一匹配即可
            if not any(n in assistant_text for n in needle):
                failures.append(f"response 缺少任一 {needle}（实际: {assistant_text[:120]!r}）")
        else:
            if needle not in assistant_text:
                failures.append(f"response 缺少 {needle!r}（实际: {assistant_text[:120]!r}）")

    tools_used = {e.get("name") for e in events if e.get("type") == "tool_call"}
    
    # tools_used: 必须全部调用
    for tool in expect.get("tools_used", []):
        if tool not in tools_used:
            failures.append(f"未调用工具 {tool}（实际: {sorted(t for t in tools_used if t)}）")
    
    # tools_used_any: 每组至少调用一个
    for group in expect.get("tools_used_any", []):
        if not any(t in tools_used for t in group):
            failures.append(f"未调用任一工具 {group}（实际: {sorted(t for t in tools_used if t)}）")

    if expect.get("sub_agent_events"):
        if not any(e.get("sub_agent_id") for e in window_events):
            failures.append("未观测到子智能体事件（sub_agent_id）")

    for f in expect.get("files", []):
        p = workspace / f["path"]
        if not p.exists():
            failures.append(f"文件未创建: {f['path']}")
        elif "contains" in f and f["contains"] not in p.read_text(encoding="utf-8", errors="replace"):
            failures.append(f"文件 {f['path']} 内容缺少 {f['contains']!r}")

    errors = [e for e in turn_events if e.get("type") == "error"]
    if errors:
        failures.append(f"出现 error 事件: {str(errors[0].get('message'))[:100]}")
    reasons = [e.get("reason") for e in turn_events if e.get("type") == "turn/end"]
    if reasons and reasons[-1] != "completed":
        failures.append(f"turn/end reason={reasons[-1]}（期望 completed）")

    # wiki 断言：验证 memory_injection 事件
    wiki_injections = [e for e in events if e.get("type") == "memory_injection"]
    wiki_text = " ".join(str(e.get("content", "")) for e in wiki_injections)
    
    # wiki_recalled: 验证指定路径被召回
    for path in expect.get("wiki_recalled", []):
        if path not in wiki_text:
            failures.append(f"wiki 未召回 {path}（实际召回: {wiki_text[:200]!r}）")
    
    # wiki_content_contains: 验证召回内容包含指定文本
    for needle in expect.get("wiki_content_contains", []):
        if needle not in wiki_text:
            failures.append(f"wiki 召回内容缺少 {needle!r}")
    
    # wiki_not_recalled: 验证指定路径未被召回
    for path in expect.get("wiki_not_recalled", []):
        if path in wiki_text:
            failures.append(f"wiki 不应召回 {path} 但被召回了")

    return failures


def _trace_structure_ok(bundle: dict[str, Any]) -> bool:
    """trace 是否同时具备 turn span（CHAIN + mycode.turn.id）与带 usage 的 GENERATION。"""
    obs = bundle.get("observations", [])
    has_turn = any(
        o.get("type") == "CHAIN"
        and ((o.get("metadata") or {}).get("attributes") or {}).get("mycode.turn.id")
        for o in obs
    )
    has_gen_usage = any(
        o.get("type") == "GENERATION" and (o.get("usageDetails") or {}).get("total", 0) > 0
        for o in obs
    )
    return has_turn and has_gen_usage


def verify_langfuse(session_id: str, expected_turns: int) -> dict[str, Any]:
    """校验 Langfuse trace 结构 + 跑 code evaluators 提交 score。

    trace 先于 observations 到达云端，结构校验纳入轮询（最长 90s）。
    """
    from agents.observability.evals import LangfuseClient, load_langfuse_env
    from eval.langfuse.code_evaluators import evaluate_bundle

    load_langfuse_env(PROJECT_ROOT)
    client = LangfuseClient()

    result: dict[str, Any] = {"ok": False, "traces": 0, "checks": [], "scores": {}, "trace_ids": []}

    deadline = time.time() + 90
    traces: list[dict] = []
    bundles: list[dict] = []
    structure_ok = False
    while time.time() < deadline:
        try:
            traces = client.fetch_traces(limit=20, session_id=session_id)
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            # 网络瞬断，重试
            time.sleep(3)
            continue
        result["traces"] = len(traces)
        result["trace_ids"] = [t["id"] for t in traces]
        if len(traces) >= expected_turns:
            try:
                bundles = [client.fetch_trace(t["id"]) for t in traces]
            except requests.HTTPError as e:
                # Langfuse Cloud 最终一致性：trace 先出现在列表，详情短暂 404
                if e.response is not None and e.response.status_code == 404:
                    time.sleep(5)
                    continue
                raise
            except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
                time.sleep(3)
                continue
            structure_ok = any(_trace_structure_ok(b) for b in bundles)
            if structure_ok:
                break
        time.sleep(5)

    if len(traces) < expected_turns:
        result["checks"].append(f"trace 数不足: {len(traces)} < {expected_turns}")
        return result
    if not structure_ok:
        result["checks"].append("无 trace 同时具备 turn span + GENERATION usage")

    # code evaluators：score 只提交一次（用最终 bundles）
    scores_all: dict[str, Any] = {}
    for t, bundle in zip(traces, bundles):
        for s in evaluate_bundle(bundle):
            try:
                client.create_score(
                    trace_id=t["id"], name=s["name"], value=s["value"],
                    data_type=s["data_type"], comment=s.get("comment"),
                )
            except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
                pass  # score 提交失败不影响整体结果
            scores_all[s["name"]] = s["value"]
    result["scores"] = scores_all
    if scores_all.get("event_range_complete") is False:
        result["checks"].append("event_range 不完整")

    result["ok"] = structure_ok and not result["checks"]
    return result


def _cleanup_backend(base_url: str, session_id: str | None, workspace: Path) -> None:
    """清理后端残留（session 文件 + 项目注册），避免污染前端侧栏。"""
    if session_id:
        try:
            requests.delete(f"{base_url}/api/sessions/{session_id}", timeout=10)
        except Exception:
            pass
    try:
        requests.delete(f"{base_url}/api/projects/{workspace}", timeout=10)
    except Exception:
        pass


async def run_task(listener: EventListener, task: dict, base_url: str, skip_langfuse: bool, keep: bool = False) -> dict:
    workspace = setup_workspace(task)
    phases = task.get("phases")
    messages = task["messages"] if not phases else []
    timeout_s = task.get("expect", {}).get("timeout_s", 180)
    record: dict[str, Any] = {
        "id": task["id"], "name": task.get("name", ""), "turns": len(messages) or sum(len(p.get("messages", [])) for p in (phases or [])),
        "session_id": None, "failures": [], "duration_s": 0.0, "langfuse": None,
    }
    t0 = time.time()
    session_id = None
    window_start = len(listener.events)
    try:
        if phases:
            # 多阶段测试：每个阶段可以是新 session
            for phase_idx, phase in enumerate(phases, 1):
                phase_messages = phase.get("messages", [])
                phase_expect = phase.get("expect", {})
                new_session = phase.get("new_session", False)
                
                if new_session:
                    # 等待异步 wiki 写入完成（compact_context 触发）
                    await asyncio.sleep(3)
                    session_id = None  # 强制新 session
                
                for i, msg in enumerate(phase_messages, 1):
                    baseline = listener.turn_end_count(session_id) if session_id else 0
                    resp = await asyncio.to_thread(send_chat, base_url, msg, session_id, str(workspace))
                    if resp.get("error"):
                        record["failures"].append(f"阶段{phase_idx} API error: {resp['error'][:150]}")
                        break
                    session_id = resp["session_id"]
                    record["session_id"] = session_id
                    ok = await listener.wait_turn_end(session_id, baseline + 1, timeout_s)
                    if not ok:
                        record["failures"].append(f"阶段{phase_idx} 第 {i} 轮等待 turn/end 超时（{timeout_s}s）")
                        break
                
                # 提取回答内容（取最后一个 assistant_message）
                events = listener.session_events(session_id)
                last_turn_start = max(
                    (idx for idx, e in enumerate(events) if e.get("type") == "turn/start"),
                    default=0,
                )
                turn_events = events[last_turn_start:]
                answer = ""
                for e in reversed(turn_events):
                    if e.get("type") == "assistant_message":
                        answer = e.get("content", "")
                        if answer:
                            break
                
                if "responses" not in record:
                    record["responses"] = []
                
                # 提取 expected_keywords 用于评测
                expected_keywords = []
                response_contains = phase_expect.get("response_contains", [])
                if response_contains:
                    # response_contains 格式: [["keyword1", "keyword2"], ...]
                    # 展平为单个列表
                    for group in response_contains:
                        if isinstance(group, list):
                            expected_keywords.extend(group)
                        else:
                            expected_keywords.append(group)
                
                record["responses"].append({
                    "phase": phase_idx,
                    "question": phase_messages[-1] if phase_messages else "",
                    "answer": answer,
                    "expected_keywords": expected_keywords,
                    "wiki_recalled": phase_expect.get("wiki_recalled", False),
                })
                
                # 阶段断言
                if not record["failures"]:
                    failures = check_assertions_for_phase(phase, workspace, events, turn_events, listener.events[window_start:])
                    record["failures"].extend(failures)
        else:
            # 单阶段测试（原有逻辑）
            for i, msg in enumerate(messages, 1):
                baseline = listener.turn_end_count(session_id) if session_id else 0
                resp = await asyncio.to_thread(send_chat, base_url, msg, session_id, str(workspace))
                if resp.get("error"):
                    record["failures"].append(f"API error: {resp['error'][:150]}")
                    break
                session_id = resp["session_id"]
                record["session_id"] = session_id
                ok = await listener.wait_turn_end(session_id, baseline + 1, timeout_s)
                if not ok:
                    record["failures"].append(f"第 {i} 轮等待 turn/end 超时（{timeout_s}s）")
                    break
                # 最后一轮做完整断言
                if i == len(messages):
                    events = listener.session_events(session_id)
                    window_events = listener.events[window_start:]
                    last_turn_start = max(
                        (idx for idx, e in enumerate(events) if e.get("type") == "turn/start"),
                        default=0,
                    )
                    failures = check_assertions(task, workspace, events, events[last_turn_start:], window_events)
                    record["failures"].extend(failures)
    except Exception as e:
        record["failures"].append(f"runner 异常: {type(e).__name__}: {e}")
    record["duration_s"] = round(time.time() - t0, 1)

    if session_id and not skip_langfuse:
        try:
            record["langfuse"] = await asyncio.to_thread(verify_langfuse, session_id, len(messages))
            if not record["langfuse"]["ok"]:
                record["failures"].extend(f"langfuse: {c}" for c in record["langfuse"]["checks"])
        except Exception as e:
            record["langfuse"] = {"ok": False, "checks": [f"查询异常: {e}"]}
            record["failures"].append(f"langfuse 查询异常: {e}")
        
        # 评测结果上报 Langfuse（多阶段任务且有 responses 时）
        if phases and len(phases) >= 2 and "responses" in record:
            try:
                from eval.langfuse.wiki_memory_evaluators import upload_eval_to_langfuse
                from agents.observability.evals import LangfuseClient, load_langfuse_env
                
                load_langfuse_env(PROJECT_ROOT)
                client = LangfuseClient()
                
                trace_ids = record.get("langfuse", {}).get("trace_ids", [])
                eval_result = await asyncio.to_thread(
                    upload_eval_to_langfuse,
                    client,
                    task.get("name", task["id"]),
                    task["id"],
                    record["responses"],
                    session_id,
                    trace_ids,
                )
                record["eval_upload"] = eval_result
            except Exception as e:
                record["eval_upload"] = {"error": str(e)}

    record["passed"] = not record["failures"]
    # 等待异步 wiki 写入完成
    await asyncio.sleep(2)
    if not keep:
        _cleanup_backend(base_url, session_id, workspace)
    if not task.get("use_real_workspace"):
        shutil.rmtree(workspace, ignore_errors=True)
    return record


def write_smoke_report(results: list[dict], base_url: str, skip_langfuse: bool) -> tuple[Path, Path]:
    ts = time.strftime("%Y%m%d_%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / f"smoke_{ts}.json"
    md_path = REPORTS_DIR / f"smoke_{ts}.md"

    passed = sum(1 for r in results if r["passed"])
    summary = {"total": len(results), "passed": passed, "failed": len(results) - passed}
    json_path.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": base_url, "skip_langfuse": skip_langfuse,
        "summary": summary, "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 对话冒烟评测报告", "",
        f"- 服务: {base_url}",
        f"- **通过: {passed}/{len(results)}**",
        f"- Langfuse 校验: {'跳过' if skip_langfuse else '开启'}", "",
        "| 任务 | 结果 | 轮数 | 耗时(s) | session | traces | 失败原因 |",
        "|------|------|------|---------|---------|--------|----------|",
    ]
    for r in results:
        lf = r.get("langfuse") or {}
        lines.append(
            f"| {r['id']} | {'✅' if r['passed'] else '❌'} | {r['turns']} | {r['duration_s']} "
            f"| `{str(r['session_id'])[:8]}` | {lf.get('traces', '-')} | {'; '.join(r['failures'])[:80] or '-'} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def get_failed_task_ids(suite: str) -> list[str]:
    """从最近的报告中读取失败的任务 id。"""
    reports = sorted(REPORTS_DIR.glob(f"smoke_*.json"), reverse=True)
    if not reports:
        return []
    
    import json as json_mod
    for report_path in reports[:5]:
        try:
            data = json_mod.loads(report_path.read_text())
            if data.get("results"):
                failed = [r["id"] for r in data["results"] if not r.get("passed", True)]
                if failed:
                    return failed
        except Exception:
            continue
    return []


async def run(only: list[str] | None, base_url: str, ws_url: str, skip_langfuse: bool, keep: bool = False, suite: str = "smoke", rerun_failed: bool = False) -> int:
    health = requests.get(f"{base_url}/api/health", timeout=5)
    health.raise_for_status()
    print(f"[smoke] 服务健康: {base_url}")

    if rerun_failed:
        failed_ids = get_failed_task_ids(suite)
        if not failed_ids:
            print(f"[smoke] 没有失败的任务需要重跑")
            return 0
        print(f"[smoke] 重跑失败任务: {failed_ids}")
        only = failed_ids

    tasks = load_tasks(only, suite)
    listener = EventListener(ws_url)
    await listener.start()
    print(f"[smoke] WS 已连接，任务数: {len(tasks)} (suite={suite})")

    results = []
    try:
        for i, task in enumerate(tasks, 1):
            print(f"  [{i}/{len(tasks)}] {task['id']} …", end=" ", flush=True)
            r = await run_task(listener, task, base_url, skip_langfuse, keep)
            results.append(r)
            mark = "✅" if r["passed"] else "❌"
            print(f"{mark} {r['duration_s']}s" + (f"  {r['failures'][:2]}" if r["failures"] else ""))
    finally:
        await listener.stop()

    json_path, md_path = write_smoke_report(results, base_url, skip_langfuse)
    passed = sum(1 for r in results if r["passed"])
    print(f"\n[smoke] 通过 {passed}/{len(results)}")
    print(f"[smoke] 报告: {json_path}")
    print(f"[smoke]       {md_path}")
    return 0 if passed == len(results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="对话冒烟评测")
    parser.add_argument("--only", nargs="*", default=None, help="只跑指定任务 id")
    parser.add_argument("--rerun-failed", action="store_true", help="只重跑上次失败的任务")
    parser.add_argument("--suite", choices=["smoke", "comprehensive"], default="smoke",
                        help="测试套件: smoke(30个单元) 或 comprehensive(5个综合场景)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL)
    parser.add_argument("--skip-langfuse", action="store_true", help="跳过 Langfuse trace 校验")
    parser.add_argument("--keep", action="store_true", help="保留后端会话与项目注册（排错用；默认自动清理）")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.only, args.base_url, args.ws_url, args.skip_langfuse, args.keep, args.suite, args.rerun_failed)))


if __name__ == "__main__":
    main()
