"""端到端评测 runner — 走真实服务，事件从磁盘读取。

流程：
1. 发消息到 /api/chat/stream
2. 轮询磁盘上的事件文件，等待 turn/end
3. 从磁盘读取事件进行断言
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

from agents.core.session import session_dir  # noqa: E402

CONVERSATIONS_PATH = Path(__file__).parent / "conversations.jsonl"
COMPREHENSIVE_PATH = Path(__file__).parent / "comprehensive.jsonl"
DEFAULT_BASE_URL = "http://localhost:5555"


def load_tasks(only: list[str] | None = None, suite: str = "smoke") -> list[dict]:
    path = COMPREHENSIVE_PATH if suite == "comprehensive" else CONVERSATIONS_PATH
    tasks = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if only:
        tasks = [t for t in tasks if t["id"] in only]
        missing = set(only) - {t["id"] for t in tasks}
        if missing:
            raise SystemExit(f"未知任务: {missing}")
    return tasks


def setup_workspace(task: dict) -> Path:
    if task.get("use_real_workspace"):
        ws = PROJECT_ROOT / "eval" / "workspace"
        wiki_dir = ws / ".mycode" / "wiki"
        if wiki_dir.exists():
            for subdir in ["workflow_pattern", "knowledge", "session_notes"]:
                p = wiki_dir / subdir
                if p.exists():
                    shutil.rmtree(p, ignore_errors=True)
            wiki_index = wiki_dir / "WIKI.md"
            if wiki_index.exists():
                wiki_index.unlink()
        skills_dir = ws / ".mycode" / "skills"
        if skills_dir.exists():
            shutil.rmtree(skills_dir, ignore_errors=True)
        return ws
    ws = Path(tempfile.mkdtemp(prefix=f"e2e_{task['id']}_"))
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


def load_session_events(session_id: str) -> list[dict]:
    """从磁盘加载 session 的所有事件。"""
    path = session_dir() / f"{session_id}.events.jsonl"
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def turn_end_count(events: list[dict]) -> int:
    return sum(1 for e in events if e.get("type") == "turn/end")


async def wait_turn_end(session_id: str, expected_count: int, timeout_s: float) -> bool:
    """轮询磁盘事件文件，等待 turn/end 达到预期数量。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        events = load_session_events(session_id)
        if turn_end_count(events) >= expected_count:
            return True
        await asyncio.sleep(0.5)
    return False


def check_assertions(task: dict, workspace: Path, events: list[dict]) -> list[str]:
    """返回失败原因列表（空 = 通过）。"""
    expect = task.get("expect", {})
    failures = []

    turn_events = [e for e in events if e.get("type") in ("turn/start", "turn/end", "assistant_message", "tool_call", "error")]
    last_turn_start_idx = max((i for i, e in enumerate(turn_events) if e.get("type") == "turn/start"), default=0)
    last_turn_events = turn_events[last_turn_start_idx:]

    assistant_text = " ".join(
        str(e.get("content", "")) for e in last_turn_events if e.get("type") == "assistant_message"
    )
    
    for needle in expect.get("response_contains", []):
        if isinstance(needle, list):
            if not any(n in assistant_text for n in needle):
                failures.append(f"response 缺少任一 {needle}（实际: {assistant_text[:120]!r}）")
        else:
            if needle not in assistant_text:
                failures.append(f"response 缺少 {needle!r}（实际: {assistant_text[:120]!r}）")

    # 提取工具调用：从 assistant_message 的 tool_calls 字段
    tools_used = set()
    for e in events:
        if e.get("type") == "assistant_message" and e.get("tool_calls"):
            for tc in e["tool_calls"]:
                func = tc.get("function", {})
                if func.get("name"):
                    tools_used.add(func["name"])
    
    for tool in expect.get("tools_used", []):
        if tool not in tools_used:
            failures.append(f"未调用工具 {tool}（实际: {sorted(t for t in tools_used if t)}）")
    
    for group in expect.get("tools_used_any", []):
        if not any(t in tools_used for t in group):
            failures.append(f"未调用任一工具 {group}（实际: {sorted(t for t in tools_used if t)}）")

    errors = [e for e in last_turn_events if e.get("type") == "error"]
    if errors:
        failures.append(f"出现 error 事件: {str(errors[0].get('message'))[:100]}")
    
    reasons = [e.get("reason") for e in last_turn_events if e.get("type") == "turn/end"]
    if reasons and reasons[-1] != "completed":
        failures.append(f"turn/end reason={reasons[-1]}（期望 completed）")

    return failures


def check_assertions_for_phase(phase: dict, workspace: Path, events: list[dict]) -> list[str]:
    """为多阶段测试检查断言。"""
    expect = phase.get("expect", {})
    failures = []

    turn_events = [e for e in events if e.get("type") in ("turn/start", "turn/end", "assistant_message", "tool_call", "error")]
    last_turn_start_idx = max((i for i, e in enumerate(turn_events) if e.get("type") == "turn/start"), default=0)
    last_turn_events = turn_events[last_turn_start_idx:]

    assistant_text = " ".join(
        str(e.get("content", "")) for e in last_turn_events if e.get("type") == "assistant_message"
    )
    
    for needle in expect.get("response_contains", []):
        if isinstance(needle, list):
            if not any(n in assistant_text for n in needle):
                failures.append(f"response 缺少任一 {needle}（实际: {assistant_text[:120]!r}）")
        else:
            if needle not in assistant_text:
                failures.append(f"response 缺少 {needle!r}（实际: {assistant_text[:120]!r}）")

    tools_used = set()
    for e in events:
        if e.get("type") == "assistant_message" and e.get("tool_calls"):
            for tc in e["tool_calls"]:
                func = tc.get("function", {})
                if func.get("name"):
                    tools_used.add(func["name"])
    
    for tool in expect.get("tools_used", []):
        if tool not in tools_used:
            failures.append(f"未调用工具 {tool}（实际: {sorted(t for t in tools_used if t)}）")
    
    for group in expect.get("tools_used_any", []):
        if not any(t in tools_used for t in group):
            failures.append(f"未调用任一工具 {group}（实际: {sorted(t for t in tools_used if t)}）")

    errors = [e for e in last_turn_events if e.get("type") == "error"]
    if errors:
        failures.append(f"出现 error 事件: {str(errors[0].get('message'))[:100]}")
    
    reasons = [e.get("reason") for e in last_turn_events if e.get("type") == "turn/end"]
    if reasons and reasons[-1] != "completed":
        failures.append(f"turn/end reason={reasons[-1]}（期望 completed）")

    for f in expect.get("files", []):
        path_pattern = f["path"]
        contains = f.get("contains")
        
        if "*" in path_pattern or "?" in path_pattern:
            matches = list(workspace.glob(path_pattern))
            if not matches:
                failures.append(f"文件未创建（匹配模式 {path_pattern}）")
            elif contains:
                found = False
                for p in matches:
                    try:
                        if contains in p.read_text(encoding="utf-8", errors="replace"):
                            found = True
                            break
                    except Exception:
                        continue
                if not found:
                    failures.append(f"匹配 {path_pattern} 的文件内容缺少 {contains!r}")
        else:
            p = workspace / path_pattern
            if not p.exists():
                failures.append(f"文件未创建: {path_pattern}")
            elif contains and contains not in p.read_text(encoding="utf-8", errors="replace"):
                failures.append(f"文件 {path_pattern} 内容缺少 {contains!r}")

    return failures


async def run_task(task: dict, base_url: str) -> dict:
    workspace = setup_workspace(task)
    phases = task.get("phases")
    messages = task.get("messages", []) if not phases else []
    timeout_s = task.get("expect", {}).get("timeout_s", 180)
    
    record: dict[str, Any] = {
        "id": task["id"],
        "name": task.get("name", ""),
        "session_id": None,
        "failures": [],
        "duration_s": 0.0,
    }
    
    t0 = time.time()
    session_id = None
    
    try:
        if phases:
            for phase_idx, phase in enumerate(phases, 1):
                phase_messages = phase.get("messages", [])
                phase_expect = phase.get("expect", {})
                new_session = phase.get("new_session", False)
                
                if new_session:
                    await asyncio.sleep(3)
                    session_id = None
                
                for i, msg in enumerate(phase_messages, 1):
                    baseline = turn_end_count(load_session_events(session_id)) if session_id else 0
                    resp = await asyncio.to_thread(send_chat, base_url, msg, session_id, str(workspace))
                    
                    if resp.get("error"):
                        record["failures"].append(f"阶段{phase_idx} API error: {resp['error'][:150]}")
                        break
                    
                    session_id = resp["session_id"]
                    record["session_id"] = session_id
                    
                    ok = await wait_turn_end(session_id, baseline + 1, timeout_s)
                    if not ok:
                        record["failures"].append(f"阶段{phase_idx} 第 {i} 轮等待 turn/end 超时（{timeout_s}s）")
                        break
                
                if not record["failures"]:
                    events = load_session_events(session_id)
                    failures = check_assertions_for_phase(phase, workspace, events)
                    record["failures"].extend(failures)
        else:
            for i, msg in enumerate(messages, 1):
                baseline = turn_end_count(load_session_events(session_id)) if session_id else 0
                resp = await asyncio.to_thread(send_chat, base_url, msg, session_id, str(workspace))
                
                if resp.get("error"):
                    record["failures"].append(f"API error: {resp['error'][:150]}")
                    break
                
                session_id = resp["session_id"]
                record["session_id"] = session_id
                
                ok = await wait_turn_end(session_id, baseline + 1, timeout_s)
                if not ok:
                    record["failures"].append(f"第 {i} 轮等待 turn/end 超时（{timeout_s}s）")
                    break
                
                if i == len(messages):
                    events = load_session_events(session_id)
                    failures = check_assertions(task, workspace, events)
                    record["failures"].extend(failures)
    
    except Exception as e:
        record["failures"].append(f"runner 异常: {type(e).__name__}: {e}")
    
    record["duration_s"] = round(time.time() - t0, 1)
    record["passed"] = not record["failures"]
    
    # 保存事件文件到报告目录（用于事后分析）
    if session_id:
        events = load_session_events(session_id)
        events_path = Path(__file__).parent.parent / "reports" / f"{session_id}.events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        record["events_file"] = str(events_path)
    
    # 清理
    try:
        if session_id:
            requests.delete(f"{base_url}/api/sessions/{session_id}", timeout=10)
        requests.delete(f"{base_url}/api/projects/{workspace}", timeout=10)
    except Exception:
        pass
    
    if not task.get("use_real_workspace"):
        shutil.rmtree(workspace, ignore_errors=True)
    return record


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="只跑指定任务")
    parser.add_argument("--suite", default="smoke", choices=["smoke", "comprehensive"], help="测试套件")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    
    tasks = load_tasks(only=args.only, suite=args.suite)
    print(f"[e2e] 开始评测 {len(tasks)} 个任务")
    
    results = []
    for task in tasks:
        print(f"[e2e] 运行任务: {task['id']} ({task.get('name', '')})")
        record = await run_task(task, args.base_url)
        results.append(record)
        
        status = "✓" if record["passed"] else "✗"
        print(f"[e2e] {status} {task['id']} ({record['duration_s']}s)")
        if record["failures"]:
            for f in record["failures"]:
                print(f"    - {f}")
    
    passed = sum(1 for r in results if r["passed"])
    print(f"\n[e2e] 完成: {passed}/{len(results)} 通过")
    
    # 保存报告
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_path = Path(__file__).parent.parent / "reports" / f"e2e_{ts}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[e2e] 报告已保存: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
