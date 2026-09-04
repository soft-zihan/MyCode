#!/usr/bin/env python3
"""Session 迁移脚本：将旧 JSON 格式转换为事件日志 JSONL 格式。

用法：
    python scripts/migrate_session.py [--dry-run] [--session-id ID]

功能：
- 扫描 ~/.mycode/sessions/*.json
- 将 openaiMessages 转换为事件日志
- 备份旧文件为 .json.bak
- 生成新的 .events.jsonl 文件
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any


def session_dir() -> Path:
    return Path.home() / ".mycode" / "sessions"


def migrate_session(session_path: Path, dry_run: bool = False) -> bool:
    """迁移单个 session 文件。返回是否成功。"""
    try:
        data = json.loads(session_path.read_text())
    except Exception as e:
        print(f"  Error reading {session_path}: {e}")
        return False
    
    session_id = data.get("metadata", {}).get("id") or session_path.stem
    
    # 检查是否已经有事件日志
    jsonl_path = session_path.with_suffix(".events.jsonl")
    if jsonl_path.exists():
        print(f"  Skipping {session_id}: already has events.jsonl")
        return True
    
    # 提取 openaiMessages
    openai_messages = data.get("openaiMessages", [])
    if not openai_messages:
        print(f"  Skipping {session_id}: no openaiMessages")
        return True
    
    # 转换为事件日志
    events = []
    seq = 0
    
    # 添加 turn/start 事件
    events.append({
        "seq": seq,
        "type": "turn/start",
        "time": 0,
        "session_id": session_id,
        "turn": 1,
    })
    seq += 1
    
    for msg in openai_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        if role == "system":
            # 系统提示词不写入事件日志
            continue
        elif role == "user":
            if isinstance(content, str):
                events.append({
                    "seq": seq,
                    "type": "user_message",
                    "time": 0,
                    "session_id": session_id,
                    "content": content,
                })
                seq += 1
        elif role == "assistant":
            event = {
                "seq": seq,
                "type": "assistant_message",
                "time": 0,
                "session_id": session_id,
                "content": content or "",
            }
            if msg.get("thinking"):
                event["thinking"] = msg["thinking"]
            if msg.get("tool_calls"):
                event["tool_calls"] = msg["tool_calls"]
            events.append(event)
            seq += 1
        elif role == "tool":
            events.append({
                "seq": seq,
                "type": "tool_result_msg",
                "time": 0,
                "session_id": session_id,
                "call_id": msg.get("tool_call_id", ""),
                "content": content or "",
            })
            seq += 1
    
    # 添加 turn/end 事件
    events.append({
        "seq": seq,
        "type": "turn/end",
        "time": 0,
        "session_id": session_id,
        "turn": 1,
        "reason": "migrated",
    })
    
    if dry_run:
        print(f"  Would migrate {session_id}: {len(events)} events")
        return True
    
    # 写入事件日志
    try:
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        print(f"  Migrated {session_id}: {len(events)} events -> {jsonl_path.name}")
    except Exception as e:
        print(f"  Error writing {jsonl_path}: {e}")
        return False
    
    # 备份旧文件
    backup_path = session_path.with_suffix(".json.bak")
    try:
        shutil.copy2(session_path, backup_path)
        print(f"  Backed up to {backup_path.name}")
    except Exception as e:
        print(f"  Warning: could not backup {session_path}: {e}")
    
    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate session files from JSON to JSONL format")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    parser.add_argument("--session-id", type=str, help="Migrate only this session ID")
    args = parser.parse_args()
    
    sessions_path = session_dir()
    if not sessions_path.exists():
        print(f"Session directory not found: {sessions_path}")
        sys.exit(1)
    
    print(f"Scanning {sessions_path}...")
    
    if args.session_id:
        session_path = sessions_path / f"{args.session_id}.json"
        if not session_path.exists():
            print(f"Session not found: {session_path}")
            sys.exit(1)
        success = migrate_session(session_path, args.dry_run)
        sys.exit(0 if success else 1)
    
    # Migrate all sessions
    migrated = 0
    failed = 0
    for session_path in sessions_path.glob("*.json"):
        print(f"Processing {session_path.name}...")
        if migrate_session(session_path, args.dry_run):
            migrated += 1
        else:
            failed += 1
    
    print(f"\nDone: {migrated} migrated, {failed} failed")
    if args.dry_run:
        print("(dry run - no changes made)")


if __name__ == "__main__":
    main()
