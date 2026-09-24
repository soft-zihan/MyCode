"""对话冒烟评测 runner — 走真实服务（HTTP + WebSocket），断言最终状态 + Langfuse trace 校验。

流程（对应实施计划 5.3）：
1. 前置：后端已启动（./start.sh restart），GET /api/health 通过
2. 逐任务：建临时工作区 → POST /api/chat/stream（bypassPermissions）
   → WS 收事件直到 turn/end → 断言（回复内容/工具/子智能体/文件产物）
3. Langfuse 校验：按 sessionId 拉 trace，断言 Span 树结构
   （turn span + event_range 完整 + GENERATION usage），跑 code evaluators 提交 score
4. 报告落盘 eval/reports/{run_id}.json/.md/.state.json

用法：
    .venv/bin/python -m eval.smoke.runner --suite chain
    .venv/bin/python -m eval.smoke.runner --only read_file shell_exec
    .venv/bin/python -m eval.smoke.runner --skip-langfuse
    .venv/bin/python -m eval.smoke.runner --cleanup
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

import httpx  # noqa: E402
import requests  # noqa: E402
import websockets  # noqa: E402
from langfuse.api.core import ApiError  # noqa: E402

from eval.common.runner_base import REPORTS_DIR  # noqa: E402

SUITE_DIR = Path(__file__).parent
SUITES = {
    "smoke": SUITE_DIR / "conversations.jsonl",
    "chain": SUITE_DIR / "chain.jsonl",
}
DEFAULT_BASE_URL = "http://localhost:5555"

# BC-19：chain 套件持久工作区必须在仓库外（BC-17 同族）。仓库内时 AGENTS.md
# 向上遍历 + system prompt {{cwd}} 绝对路径会把"真项目"泄漏给评测 agent，
# 导致其跑到仓库根干活/失控探索（gate6 task3、gate7 leg2 task1）。
SMOKE_CHAIN_WORKSPACE = Path.home() / ".mycode" / "eval_workspaces" / "smoke-chain"
DEFAULT_WS_URL = "ws://localhost:5555/ws/events"


def _assistant_text_from_public(turn_events: list[dict]) -> str:
    """BC-25：U8 起 assistant_message 为 INTERNAL 事件（落盘不广播），WS 客户端
    收不到。从公开的流式 text 事件重建助手文本（前端渲染同源），过滤带
    sub_agent_id 的子代理文本。"""
    return "".join(
        str(e.get("content", ""))
        for e in turn_events
        if e.get("type") == "text" and not e.get("sub_agent_id")
    ).strip()


def load_tasks(only: list[str] | None = None, suite: str = "smoke") -> list[dict]:
    """加载评测任务。
    
    Args:
        only: 只加载指定 id 的任务
        suite: 测试套件 - smoke(单元) / chain(全链路)；GAIA 用独立 benchmark（固定 Level 3，--sample 10）
    """
    path = SUITES[suite]
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

    def auto_wake_turn_numbers(self, session_id: str) -> set:
        """U3b：trigger=auto_wake 的轮号集合（唤醒轮非用户触发，不参与轮次记账）。"""
        return {
            e.get("turn")
            for e in self.session_events(session_id)
            if e.get("type") == "turn/start" and e.get("trigger") == "auto_wake"
        }

    def turn_end_count(self, session_id: str) -> int:
        wake_turns = self.auto_wake_turn_numbers(session_id)
        return sum(
            1 for e in self.session_events(session_id)
            if e.get("type") == "turn/end" and e.get("turn") not in wake_turns
        )

    async def wait_turn_end(self, session_id: str, expected_count: int, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.turn_end_count(session_id) >= expected_count:
                return True
            await asyncio.sleep(0.5)
        return False


def _wipe_except(directory: Path, keep: set[Path]) -> None:
    """递归清空 directory，保留 keep 中的路径（含其父目录链）。"""
    import shutil
    for child in directory.iterdir():
        if child in keep:
            continue
        if child.is_dir():
            child_prefix = str(child) + "/"
            if any(str(k).startswith(child_prefix) for k in keep):
                _wipe_except(child, keep)
            else:
                shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def setup_workspace(task: dict) -> Path:
    """评测 workspace 准备：全量清空隔离（BC-5：残留 1.3G 曾导致 grep 超时与跨用例污染）。

    保留项（BC-6）：
    - .extract_state.json：提取水位线。清掉会让全局 session 存储里所有旧会话
      （同 cwd 的历史评测会话）重新变成"待补编译"，backfill 把它们编译进新
      用例的 wiki 造成跨用例污染并拖慢当前用例的折叠编译。
    （embedding 缓存已全局化 ~/.mycode/embed-cache（BC-30），不在 workspace 内。）
    """
    if task.get("use_real_workspace"):
        ws = SMOKE_CHAIN_WORKSPACE
        keep = {
            ws / ".mycode" / "wiki" / ".extract_state.json",
        }
        if ws.exists():
            _wipe_except(ws, keep)
        ws.mkdir(parents=True, exist_ok=True)
    else:
        ws = Path(tempfile.mkdtemp(prefix=f"smoke_{task['id']}_"))
    for f in task.get("setup", []):
        p = ws / f["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        if "copy_from" in f:
            shutil.copyfile(PROJECT_ROOT / f["copy_from"], p)
        else:
            p.write_text(f["content"], encoding="utf-8")
        if f["path"].endswith(".sh"):
            p.chmod(0o755)
    return ws


def check_backend_health(base_url: str) -> None:
    response = requests.get(f"{base_url}/api/health", timeout=5)
    response.raise_for_status()


def fetch_audit_events(base_url: str, session_id: str) -> list[dict]:
    """BC-29：审计类断言（memory_injection、sub_agent/resume 等 INTERNAL 事件）
    不走 WS 公开通道（U8 白名单），改为 HTTP 拉持久化全量事件日志。
    WS=流式/存活通道，HTTP=审计通道，与 U8 事件治理设计一致。"""
    resp = requests.get(f"{base_url}/api/sessions/{session_id}", timeout=15)
    resp.raise_for_status()
    return resp.json().get("events") or []


def send_chat(base_url: str, message: str, session_id: str | None, cwd: str, thinking: bool | None = None) -> dict:
    payload: dict[str, Any] = {
        "message": message,
        "cwd": cwd,
        "permission_mode": "bypassPermissions",
    }
    if thinking is not None:
        payload["thinking"] = thinking
    if session_id:
        payload["session_id"] = session_id
    resp = requests.post(f"{base_url}/api/chat/stream", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _check_subagent_background(expect: dict, events: list[dict]) -> list[str]:
    """U3a 后台子代理断言（phase 级 + task 级共用）。"""
    failures: list[str] = []
    if expect.get("subagent_background_launched"):
        # background=true 生效：agent 工具结果带 state="running" 标签（发起即返回）
        if not any(
            e.get("type") == "tool_result" and 'state="running"' in str(e.get("result", ""))
            for e in events
        ):
            failures.append('未观测到 agent 工具 state="running" 后台发起结果（background=true 未生效）')
    if expect.get("subagent_completed"):
        # 完成通知：synthetic subagent/completed 事件落父会话且带幂等键
        completed = [e for e in events if e.get("type") == "subagent/completed"]
        if not completed:
            failures.append("未观测到 subagent/completed 事件（后台完成通知未送达）")
        elif not completed[-1].get("notification_id"):
            failures.append("subagent/completed 缺少 notification_id（幂等键未落盘）")
    if expect.get("subagent_cancelled"):
        # U4 硬中止：审计事件 + cancelled 终态 + 幂等取消通知三件套
        # U8 后 sub_agent/cancel 为 INTERNAL（落盘不广播）；WS 侧以公开的
        # tool_call(name=subagent_cancel) 作为"工具确实执行"的证据
        if not any(
            e.get("type") == "tool_call" and e.get("name") == "subagent_cancel" for e in events
        ):
            failures.append("未观测到 subagent_cancel 工具调用（tool_call 公开事件缺失）")
        if not any(e.get("type") == "sub_agent/end" and e.get("status") == "cancelled" for e in events):
            failures.append("未观测到 sub_agent/end(status=cancelled)（取消终态未落账）")
        if not any(
            e.get("type") == "subagent/completed" and e.get("status") == "cancelled"
            for e in events
        ):
            failures.append("未观测到 subagent/completed(state=cancelled) 取消通知")
    return failures


def check_assertions_for_phase(phase: dict, workspace: Path, events: list[dict], turn_events: list[dict], window_events: list[dict], audit_events: list[dict]) -> list[str]:
    """为多阶段测试检查断言。"""
    expect = phase.get("expect", {})
    failures = []

    assistant_text = _assistant_text_from_public(turn_events)
    
    for needle in expect.get("response_contains", []):
        if isinstance(needle, list):
            if not any(n in assistant_text for n in needle):
                failures.append(f"response 缺少任一 {needle}（实际: {assistant_text[:120]!r}）")
        else:
            if needle not in assistant_text:
                failures.append(f"response 缺少 {needle!r}（实际: {assistant_text[:120]!r}）")

    for needle in expect.get("response_not_contains", []):
        if needle in assistant_text:
            failures.append(f"response 不应包含 {needle!r}（泄漏: {assistant_text[:120]!r}）")

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

    if expect.get("sub_agent_resumed"):
        # U2 续跑断言：sub_agent/resume 为 INTERNAL（BC-29），从持久化审计日志取证
        if not any(e.get("type") == "sub_agent/resume" for e in audit_events):
            failures.append("未观测到 sub_agent/resume 事件（第二次调用未带 session_id 续跑）")

    failures.extend(_check_subagent_background(expect, events))

    for f in expect.get("files", []):
        path_pattern = f["path"]
        contains = f.get("contains")
        
        # Support glob patterns (e.g., "**/main.py")
        if "*" in path_pattern or "?" in path_pattern:
            matches = list(workspace.glob(path_pattern))
            if not matches:
                failures.append(f"文件未创建（匹配模式 {path_pattern}）")
            elif contains:
                needles = [contains] if isinstance(contains, str) else list(contains or [])
                found = False
                for p in matches:
                    try:
                        text = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    if any(n in text for n in needles):
                        found = True
                        break
                if not found:
                    failures.append(f"匹配 {path_pattern} 的文件内容缺少 {contains!r}")
        else:
            p = workspace / path_pattern
            if not p.exists():
                failures.append(f"文件未创建: {path_pattern}")
            elif contains:
                needles = [contains] if isinstance(contains, str) else list(contains or [])
                text = p.read_text(encoding="utf-8", errors="replace")
                if not any(n in text for n in needles):
                    failures.append(f"文件 {path_pattern} 内容缺少 {contains!r}")

    errors = [e for e in turn_events if e.get("type") == "error"]
    if errors:
        failures.append(f"出现 error 事件: {str(errors[0].get('message'))[:100]}")
    reasons = [e.get("reason") for e in turn_events if e.get("type") == "turn/end"]
    if reasons and reasons[-1] != "completed":
        failures.append(f"turn/end reason={reasons[-1]}（期望 completed）")

    # wiki 断言（memory_injection 为 INTERNAL：BC-29，从持久化审计日志取证）
    wiki_injections = [e for e in audit_events if e.get("type") == "memory_injection"]
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


def check_assertions(task: dict, workspace: Path, events: list[dict], turn_events: list[dict], window_events: list[dict], audit_events: list[dict]) -> list[str]:
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

    assistant_text = _assistant_text_from_public(turn_events)
    
    # response_contains: 支持 str（必须包含）或 list[str]（任一包含）
    for needle in expect.get("response_contains", []):
        if isinstance(needle, list):
            # OR 逻辑：任一匹配即可
            if not any(n in assistant_text for n in needle):
                failures.append(f"response 缺少任一 {needle}（实际: {assistant_text[:120]!r}）")
        else:
            if needle not in assistant_text:
                failures.append(f"response 缺少 {needle!r}（实际: {assistant_text[:120]!r}）")

    for needle in expect.get("response_not_contains", []):
        if needle in assistant_text:
            failures.append(f"response 不应包含 {needle!r}（泄漏: {assistant_text[:120]!r}）")

    # response_format: 检查回复格式
    response_format = expect.get("response_format")
    if response_format == "plain_text":
        # 检查是否包含 markdown code block
        if "```" in assistant_text:
            failures.append(f"response_format: 期望纯文本，实际返回 markdown code block（实际: {assistant_text[:120]!r}）")
        # 检查是否包含 markdown 标题
        if assistant_text.strip().startswith("#"):
            failures.append(f"response_format: 期望纯文本，实际返回 markdown 标题（实际: {assistant_text[:120]!r}）")
        # 检查是否包含 markdown 列表
        if any(line.strip().startswith(("- ", "* ", "+ ")) for line in assistant_text.split("\n")):
            failures.append(f"response_format: 期望纯文本，实际返回 markdown 列表（实际: {assistant_text[:120]!r}）")

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

    if expect.get("sub_agent_resumed"):
        # U2 续跑断言：sub_agent/resume 为 INTERNAL（BC-29），从持久化审计日志取证
        if not any(e.get("type") == "sub_agent/resume" for e in audit_events):
            failures.append("未观测到 sub_agent/resume 事件（第二次调用未带 session_id 续跑）")

    failures.extend(_check_subagent_background(expect, events))

    for f in expect.get("files", []):
        path_pattern = f["path"]
        contains = f.get("contains")
        
        # Support glob patterns (e.g., "**/main.py")
        if "*" in path_pattern or "?" in path_pattern:
            matches = list(workspace.glob(path_pattern))
            if not matches:
                failures.append(f"文件未创建（匹配模式 {path_pattern}）")
            elif contains:
                needles = [contains] if isinstance(contains, str) else list(contains or [])
                found = False
                for p in matches:
                    try:
                        text = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    if any(n in text for n in needles):
                        found = True
                        break
                if not found:
                    failures.append(f"匹配 {path_pattern} 的文件内容缺少 {contains!r}")
        else:
            p = workspace / path_pattern
            if not p.exists():
                failures.append(f"文件未创建: {path_pattern}")
            elif contains:
                needles = [contains] if isinstance(contains, str) else list(contains or [])
                text = p.read_text(encoding="utf-8", errors="replace")
                if not any(n in text for n in needles):
                    failures.append(f"文件 {path_pattern} 内容缺少 {contains!r}")

    errors = [e for e in turn_events if e.get("type") == "error"]
    if errors:
        failures.append(f"出现 error 事件: {str(errors[0].get('message'))[:100]}")
    reasons = [e.get("reason") for e in turn_events if e.get("type") == "turn/end"]
    if reasons and reasons[-1] != "completed":
        failures.append(f"turn/end reason={reasons[-1]}（期望 completed）")

    # wiki 断言：验证 memory_injection 事件（INTERNAL：BC-29，持久化审计日志取证）
    wiki_injections = [e for e in audit_events if e.get("type") == "memory_injection"]
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
    """trace 是否同时具备 turn span（CHAIN + turn_id）与带 usage 的 GENERATION。"""
    obs = bundle.get("observations", [])
    has_turn = any(
        o.get("type") == "CHAIN"
        and o.get("name") == "turn"
        and (o.get("metadata") or {}).get("turn_id")
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
    from agents.observability.langfuse_api import LangfuseApiClient, load_langfuse_env
    from eval.langfuse.code_evaluators import evaluate_bundle

    load_langfuse_env(PROJECT_ROOT)
    client = LangfuseApiClient()

    result: dict[str, Any] = {"ok": False, "traces": 0, "checks": [], "scores": {}, "trace_ids": []}

    deadline = time.time() + 90
    traces: list[dict] = []
    bundles: list[dict] = []
    structure_ok = False
    while time.time() < deadline:
        try:
            traces = client.fetch_traces(limit=20, session_id=session_id)
        except (httpx.HTTPError, ApiError) as exc:
            print(f"[smoke] Langfuse trace list failed, retrying: {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(3)
            continue
        result["traces"] = len(traces)
        result["trace_ids"] = [t["id"] for t in traces]
        if len(traces) >= expected_turns:
            try:
                bundles = [client.fetch_trace(t["id"]) for t in traces]
            except ApiError as exc:
                if exc.status_code == 404:
                    print(f"[smoke] Langfuse trace detail not ready, retrying: {exc}", file=sys.stderr)
                    time.sleep(5)
                    continue
                raise
            except httpx.HTTPError as exc:
                print(f"[smoke] Langfuse trace detail failed, retrying: {type(exc).__name__}: {exc}", file=sys.stderr)
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
            except (httpx.HTTPError, ApiError) as exc:
                result["checks"].append(
                    f"score 提交失败 {s['name']}: {type(exc).__name__}: {exc}"
                )
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


async def run_task(
    listener: EventListener,
    task: dict,
    base_url: str,
    skip_langfuse: bool,
    keep: bool = False,
    on_event: Any | None = None,
    thinking: bool | None = None,
) -> dict:
    workspace = setup_workspace(task)
    phases = task.get("phases")
    messages = task["messages"] if not phases else []
    timeout_s = task.get("expect", {}).get("timeout_s", 180)
    case_budget = task.get("expect", {}).get("case_timeout_s", 900)
    record: dict[str, Any] = {
        "id": task["id"], "name": task.get("name", ""), "turns": len(messages) or sum(len(p.get("messages", [])) for p in (phases or [])),
        "session_id": None, "failures": [], "duration_s": 0.0, "langfuse": None,
        "workspace": str(workspace),
    }

    def emit(event_type: str, **data: Any) -> None:
        if on_event is None:
            return
        on_event({"type": event_type, "task_id": task["id"], **data})

    t0 = time.time()
    session_id = None
    window_start = len(listener.events)

    def observe_session(sid: str | None) -> None:
        nonlocal session_id
        if sid and sid != session_id:
            session_id = sid
            record["session_id"] = sid
            emit("smoke_session_started", session_id=sid, workspace=str(workspace))

    emit("smoke_task_started", name=record["name"], workspace=str(workspace), turns=record["turns"])
    try:
        budget_blown = False
        if phases:
            # 多阶段测试：每个阶段可以是新 session
            for phase_idx, phase in enumerate(phases, 1):
                if budget_blown:
                    break
                phase_messages = phase.get("messages", [])
                phase_expect = phase.get("expect", {})
                new_session = phase.get("new_session", False)
                
                if new_session:
                    # 等待异步 wiki 写入完成（compact_context 触发）
                    await asyncio.sleep(3)
                    session_id = None  # 强制新 session

                for i, msg in enumerate(phase_messages, 1):
                    if time.time() - t0 > case_budget:
                        record["failures"].append(f"case 预算超限（{case_budget}s），熔断于阶段{phase_idx}第{i}轮")
                        budget_blown = True
                        break
                    baseline = listener.turn_end_count(session_id) if session_id else 0
                    resp = await asyncio.to_thread(send_chat, base_url, msg, session_id, str(workspace), thinking)
                    if resp.get("error"):
                        record["failures"].append(f"阶段{phase_idx} API error: {resp['error'][:150]}")
                        break
                    observe_session(resp["session_id"])
                    ok = await listener.wait_turn_end(session_id, baseline + 1, timeout_s)
                    if not ok:
                        record["failures"].append(f"阶段{phase_idx} 第 {i} 轮等待 turn/end 超时（{timeout_s}s）")
                        break

                # wait_for_files：异步产物（编译/整理/skill）轮询等待，超时按缺失断言。
                # 必须在本阶段消息发送之后执行——产物由消息触发（如 compact_context
                # 折叠后的提取编译），放在消息前会把等待预算烧在还不存在的文件上。
                wait_specs = phase_expect.get("wait_for_files", [])
                if wait_specs and not budget_blown:
                    wait_deadline = time.time() + phase_expect.get("wait_timeout_s", 240)
                    pending = list(wait_specs)
                    while pending and time.time() < wait_deadline:
                        still = []
                        for spec in pending:
                            matches = [m for m in workspace.glob(spec["path"]) if m.is_file()]
                            needle = spec.get("contains")
                            needles = [needle] if isinstance(needle, str) else list(needle or [])
                            ok = bool(matches) and (
                                not needles or any(
                                    any(n in text for n in needles)
                                    for text in (
                                        m.read_text(encoding="utf-8", errors="replace")
                                        for m in matches
                                    )
                                )
                            )
                            if not ok:
                                still.append(spec)
                        pending = still
                        if pending:
                            await asyncio.sleep(3)
                    for spec in pending:
                        record["failures"].append(
                            f"wait_for_files 超时: {spec['path']} contains={spec.get('contains')!r}"
                        )

                # U3a：wait_for_events——后台异步事件（如 subagent/completed）轮询等待，
                # 确保 synthetic 通知已落盘注入，下一阶段模型上下文才确定性可见。
                # U3b：条目支持 str（按 type 匹配）或 dict（字段子集匹配，
                # 如 {"type": "turn/start", "trigger": "auto_wake"}）；按声明顺序
                # 匹配（后一条在前一条命中位置之后查找），可表达"通知→唤醒轮
                # 开始→唤醒轮结束"的因果链。
                search_from = 0
                for spec in phase_expect.get("wait_for_events", []):
                    if budget_blown:
                        break

                    def _event_matches(e: dict, spec=spec) -> bool:
                        if isinstance(spec, str):
                            return e.get("type") == spec
                        return all(e.get(k) == v for k, v in spec.items())

                    ev_deadline = time.time() + phase_expect.get("wait_timeout_s", 240)
                    arrived = False
                    while time.time() < ev_deadline:
                        events_now = listener.session_events(session_id)
                        idx = next(
                            (i for i, e in enumerate(events_now) if i >= search_from and _event_matches(e)),
                            None,
                        )
                        if idx is not None:
                            search_from = idx + 1
                            arrived = True
                            break
                        await asyncio.sleep(1)
                    if not arrived:
                        record["failures"].append(f"wait_for_events 超时: {spec}")

                # 提取回答内容（公开 text 流事件重建，BC-25）
                events = listener.session_events(session_id)
                ended_turns = {e.get("turn") for e in events if e.get("type") == "turn/end"}
                last_turn_start = max(
                    (idx for idx, e in enumerate(events)
                     if e.get("type") == "turn/start" and e.get("turn") in ended_turns),
                    default=0,
                )
                turn_events = events[last_turn_start:]
                answer = _assistant_text_from_public(turn_events)
                
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
                
                # 阶段断言（审计事件走 HTTP 持久化日志，BC-29）
                if not record["failures"]:
                    audit_events = await asyncio.to_thread(fetch_audit_events, base_url, session_id)
                    failures = check_assertions_for_phase(phase, workspace, events, turn_events, listener.events[window_start:], audit_events)
                    record["failures"].extend(failures)
        else:
            # 单阶段测试（原有逻辑）
            for i, msg in enumerate(messages, 1):
                if time.time() - t0 > case_budget:
                    record["failures"].append(f"case 预算超限（{case_budget}s），熔断于第{i}轮")
                    break
                baseline = listener.turn_end_count(session_id) if session_id else 0
                resp = await asyncio.to_thread(send_chat, base_url, msg, session_id, str(workspace), thinking)
                if resp.get("error"):
                    record["failures"].append(f"API error: {resp['error'][:150]}")
                    break
                observe_session(resp["session_id"])
                ok = await listener.wait_turn_end(session_id, baseline + 1, timeout_s)
                if not ok:
                    record["failures"].append(f"第 {i} 轮等待 turn/end 超时（{timeout_s}s）")
                    break
                # 最后一轮做完整断言
                if i == len(messages):
                    events = listener.session_events(session_id)
                    window_events = listener.events[window_start:]
                    ended_turns = {e.get("turn") for e in events if e.get("type") == "turn/end"}
                    last_turn_start = max(
                        (idx for idx, e in enumerate(events)
                         if e.get("type") == "turn/start" and e.get("turn") in ended_turns),
                        default=0,
                    )
                    audit_events = await asyncio.to_thread(fetch_audit_events, base_url, session_id)
                    failures = check_assertions(task, workspace, events, events[last_turn_start:], window_events, audit_events)
                    record["failures"].extend(failures)
    except Exception as e:
        record["failures"].append(f"runner 异常: {type(e).__name__}: {e}")
    record["duration_s"] = round(time.time() - t0, 1)

    if session_id and "responses" not in record:
        events = listener.session_events(session_id)
        ended_turns = {e.get("turn") for e in events if e.get("type") == "turn/end"}
        last_turn_start = max(
            (idx for idx, e in enumerate(events)
             if e.get("type") == "turn/start" and e.get("turn") in ended_turns),
            default=0,
        )
        answer = _assistant_text_from_public(events[last_turn_start:])
        expect = task.get("expect", {})
        expected_keywords: list[Any] = []
        for group in expect.get("response_contains", []):
            if isinstance(group, list):
                expected_keywords.extend(group)
            else:
                expected_keywords.append(group)
        record["responses"] = [{
            "phase": 1,
            "question": messages[-1] if messages else "",
            "answer": answer,
            "expected_keywords": expected_keywords,
            "wiki_recalled": expect.get("wiki_recalled", False),
        }]

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
                from agents.observability.langfuse_api import LangfuseApiClient, load_langfuse_env
                
                load_langfuse_env(PROJECT_ROOT)
                client = LangfuseApiClient()
                
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

    # 失败用例自动标记为 bad case
    if not record["passed"] and record.get("session_id"):
        try:
            import uuid
            from agents.observability.bad_cases import (
                BadCase,
                BadCaseSource,
                BadCaseStatus,
                BadCaseSeverity,
                create_bad_case,
            )
            
            bad_case = BadCase(
                id=uuid.uuid4().hex[:8],
                session_id=record["session_id"],
                source=BadCaseSource.EVAL,
                status=BadCaseStatus.PENDING,
                severity=BadCaseSeverity.HIGH,
                signal_type="eval_failure",
                reason="; ".join(record["failures"][:3]),  # 只记录前 3 个失败原因
                comment=f"Task: {task['id']}",
                created_at=time.time(),
                updated_at=time.time(),
            )
            created = create_bad_case(bad_case)
            record["bad_case_id"] = created.id
            print(f"[smoke] 失败用例 {task['id']} 已标记为 bad case: {created.id}")
        except Exception as e:
            record["bad_case_error"] = str(e)
    
    # 等待异步 wiki 写入完成
    await asyncio.sleep(2)
    emit(
        "smoke_task_finished",
        session_id=session_id,
        passed=record["passed"],
        failures=record["failures"][:10],
        duration_s=record["duration_s"],
    )
    if not keep:
        _cleanup_backend(base_url, session_id, workspace)
    if not task.get("use_real_workspace"):
        shutil.rmtree(workspace, ignore_errors=True)
    return record


def get_failed_task_ids(suite: str) -> list[str]:
    """从最近的 EvalService smoke run 状态中读取失败任务 id。"""
    reports = sorted(REPORTS_DIR.glob("smoke-*.state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for report_path in reports[:5]:
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("benchmark") != "smoke":
            continue
        if (data.get("options") or {}).get("suite") != suite:
            continue
        failed = [
            task["task_id"]
            for task in data.get("tasks", [])
            if task.get("status") in ("failed", "error", "aborted")
        ]
        if failed:
            return failed
    return []


def main() -> None:
    from eval.common.cli import run_eval_cli_blocking
    from eval.common.models import EvalRunOptions

    parser = argparse.ArgumentParser(description="对话冒烟评测")
    parser.add_argument("--only", nargs="*", default=None, help="只跑指定任务 id")
    parser.add_argument("--sample", type=int, default=None, help="只跑前 N 个任务")
    parser.add_argument("--rerun-failed", action="store_true", help="只重跑上次失败的任务")
    parser.add_argument("--suite", choices=sorted(SUITES), default="smoke",
                        help="测试套件: smoke(单元) / chain(全链路)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL)
    parser.add_argument("--skip-langfuse", action="store_true", help="跳过 Langfuse trace 校验")
    parser.add_argument("--cleanup", action="store_true", help="跑完删除后端 session / project；默认保留便于前端观察")
    parser.add_argument("--no-dataset", action="store_true", help="不同步 Langfuse Dataset")
    parser.add_argument("--judge", action="store_true", help="结束后运行 code evaluator + LLM judge")
    parser.add_argument("--thinking", choices=["on", "off", "default"], default="default",
                        help="推理模型 thinking 开关（default=跟随全局配置）")
    args = parser.parse_args()

    only = args.only
    if args.rerun_failed:
        only = get_failed_task_ids(args.suite)
        if not only:
            print("[smoke] 没有失败的任务需要重跑")
            return
        print(f"[smoke] 重跑失败任务: {only}")

    check_backend_health(args.base_url)
    print(f"[smoke] 服务健康: {args.base_url}")

    options = EvalRunOptions(
        benchmark="smoke",
        sample=args.sample,
        only=only,
        suite=args.suite,
        skip_langfuse=args.skip_langfuse,
        keep_sessions=not args.cleanup,
        sync_langfuse_dataset=not args.no_dataset and not args.skip_langfuse,
        judge_after_run=args.judge,
        base_url=args.base_url,
        ws_url=args.ws_url,
        thinking={"on": True, "off": False, "default": None}[args.thinking],
    )
    result = run_eval_cli_blocking(options, shutdown_tracing=False)
    summary = result.get("summary", {})
    failed = summary.get("failed", 0) + summary.get("errors", 0) + summary.get("aborted", 0)
    if result.get("status") == "failed" or failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
