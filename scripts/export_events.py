#!/usr/bin/env python3
"""导出会话事件为 JSONL（规则 5 排错工作流）。

sqlite 后端下事件不再是 ~/.mycode/sessions/*.events.jsonl 文件，
本脚本把任意后端的事件导出为 JSONL 文本，保持 grep/jq 排错习惯。

用法:
    python scripts/export_events.py --list                 # 列出全部 session ID
    python scripts/export_events.py SESSION_ID             # 导出到 stdout（可管道 grep/jq）
    python scripts/export_events.py SESSION_ID -o out.jsonl
    python scripts/export_events.py --all -o dir/          # 全部会话，每个一个 .events.jsonl

后端由 MYCODE_SESSION_BACKEND 环境变量决定（与主程序一致）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.core.session import get_session_backend


def _dump(events: list[dict]) -> str:
    return "".join(
        json.dumps(ev, ensure_ascii=False, default=str) + "\n" for ev in events
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export session events as JSONL from the active SessionBackend.",
    )
    parser.add_argument("session_id", nargs="?", help="要导出的 session ID")
    parser.add_argument("-o", "--output", help="输出文件（单会话）或目录（--all）")
    parser.add_argument("--all", action="store_true", help="导出全部会话")
    parser.add_argument("--list", action="store_true", help="仅列出全部 session ID")
    args = parser.parse_args()

    backend = get_session_backend()

    if args.list:
        for sid in backend.list_session_ids():
            print(sid)
        return 0

    if args.all:
        out_dir = Path(args.output) if args.output else Path.cwd()
        out_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for sid in backend.list_session_ids():
            events = backend.load_all_events(sid)
            (out_dir / f"{sid}.events.jsonl").write_text(_dump(events), encoding="utf-8")
            count += 1
        print(f"exported {count} sessions -> {out_dir}", file=sys.stderr)
        return 0

    if not args.session_id:
        parser.error("session_id is required (or use --all / --list)")

    events = backend.load_all_events(args.session_id)
    if not events:
        print(f"session {args.session_id} 无事件", file=sys.stderr)
        return 1

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(events), encoding="utf-8")
        print(f"exported {len(events)} events -> {path}", file=sys.stderr)
    else:
        sys.stdout.write(_dump(events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
