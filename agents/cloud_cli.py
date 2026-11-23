"""`mycode cloud` — 云端任务的命令行客户端。

行为形状对齐 codex cloud exec：提交成功打印任务 id 与可点开的 URL，失败退出码非 0
（脚本里能 `&&` 串起来）。地址与 token 走 agents.core.cloud_client 的三源解析，
与前端云端面板同一份配置，不需要再手填。

⚠️ trust_env=False：本机常驻 HTTP(S)_PROXY，云端地址却是 localhost。
让 httpx 读环境代理会把请求交给 Windows 侧再绕回来，失败时回一个假 502。
"""

from __future__ import annotations

import argparse
import sys

import httpx

from agents.core.cloud_client import cloud_config

TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0)


def _request(method: str, path: str, **kw) -> httpx.Response:
    url, token, _ = cloud_config()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.request(
        method, f"{url}{path}", headers=headers, timeout=TIMEOUT, trust_env=False, **kw
    )


def _die(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body)[:200]
    return str(body)[:200]


def cmd_exec(args: argparse.Namespace) -> int:
    payload = {"repo_url": args.repo, "prompt": args.prompt, "ref": args.ref,
               "setup_command": args.setup}
    try:
        r = _request("POST", "/api/cloud/tasks", json=payload)
    except httpx.HTTPError as e:
        return _die(f"提交失败：连不上云端（{e!r}）。先跑 mycode cloud-status")
    if r.status_code >= 400:
        return _die(f"提交失败（{r.status_code}）：{_detail(r)}")
    task = r.json()
    print(f"任务已提交：{task['id']}")
    # 不打印浏览器 URL：A 批没有任务表单，?task= 打开的是空页面（README §11.1）。
    print(f"跟日志：mycode cloud logs {task['id']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    try:
        r = _request("GET", "/api/cloud/tasks", params={"limit": args.limit})
    except httpx.HTTPError as e:
        return _die(f"拉列表失败：连不上云端（{e!r}）")
    if r.status_code >= 400:
        return _die(f"拉列表失败（{r.status_code}）：{_detail(r)}")
    tasks = r.json()
    if not tasks:
        print("还没有云端任务。提一个：mycode cloud exec --repo <URL> \"改点什么\"")
        return 0
    print(f"{'ID':<14}{'STATUS':<14}{'REPO':<40}PR")
    for t in tasks:
        print(f"{t['id']:<14}{t['status']:<14}{t['repo_url'][:38]:<40}{t['pr_url'] or '-'}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    try:
        r = _request("GET", f"/api/cloud/tasks/{args.task_id}")
    except httpx.HTTPError as e:
        return _die(f"查任务失败：连不上云端（{e!r}）")
    if r.status_code >= 400:
        return _die(f"查任务失败（{r.status_code}）：{_detail(r)}")
    t = r.json()
    print(f"{t['id']}  {t['status']}  {t['repo_url']}")
    if t["branch"]:
        print(f"分支：{t['branch']}    PR：{t['pr_url'] or '-'}")
    if t["error"]:
        print(f"错误：{t['error']}")
    try:
        er = _request("GET", f"/api/cloud/tasks/{args.task_id}/events", params={"limit": args.limit})
    except httpx.HTTPError:
        return 0      # 状态已经打出来了，事件拉不到不算失败（与下面 >=400 同一档）
    if er.status_code >= 400:
        return 0
    for ev in er.json().get("events", []):
        text = str(ev.get("content") or ev.get("text") or ev.get("message") or "").strip()
        line = text.replace("\n", " ")[:160]
        print(f"[{ev.get('type', '?')}] {line}" if line else f"[{ev.get('type', '?')}]")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    if not args.yes:
        try:
            r = _request("GET", "/api/cloud/tasks", params={"limit": 1000})
        except httpx.HTTPError as e:
            return _die(f"拉列表失败：连不上云端（{e!r}）")
        if r.status_code >= 400:
            return _die(f"拉列表失败（{r.status_code}）：{_detail(r)}")
        terminal = {"succeeded", "failed", "timeout", "cancelled"}
        gone = [t for t in r.json() if t["status"] in terminal and t["workspace_name"]]
        if not gone:
            print("没有可清理的终态任务工作区")
            return 0
        print("将被删除的工作区（加 --yes 真的删）：")
        for t in gone:
            print(f"  {t['workspace_name']}  ({t['id']} {t['status']})")
        return 0
    try:
        r = _request("POST", "/api/cloud/prune")
    except httpx.HTTPError as e:
        return _die(f"清理失败：连不上云端（{e!r}）")
    if r.status_code >= 400:
        return _die(f"清理失败（{r.status_code}）：{_detail(r)}")
    got = r.json()
    print(f"已清理 {got['pruned']} 个工作区，释放 {got['freed_bytes'] / 1024 / 1024:.1f} MiB")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mycode cloud", description="云端任务：提任务 / 看列表 / 看日志 / 清终态")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("exec", help="提一个云端任务")
    e.add_argument("--repo", required=True, help="仓库地址（https 或 git@）")
    e.add_argument("--ref", default=None, help="分支或 tag，缺省用仓库默认分支")
    e.add_argument("--setup", default="", help="跑 agent 之前先执行的准备命令")
    e.add_argument("prompt", help="要 agent 做什么")
    e.set_defaults(func=cmd_exec)

    l = sub.add_parser("list", help="看任务列表")
    l.add_argument("--limit", type=int, default=20)
    l.set_defaults(func=cmd_list)

    g = sub.add_parser("logs", help="看某个任务的状态与事件")
    g.add_argument("task_id")
    g.add_argument("--limit", type=int, default=200)
    g.set_defaults(func=cmd_logs)

    r = sub.add_parser("prune", help="清终态任务的工作区")
    r.add_argument("--yes", action="store_true", help="不加就只打印将被删的清单")
    r.set_defaults(func=cmd_prune)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())