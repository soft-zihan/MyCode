#!/usr/bin/env python3
"""
MyCode 全链路测试脚本

模拟真实用户操作，收集前后端数据用于debug。

用法:
    # 运行所有测试
    python scripts/test_full_flow.py
    
    # 运行特定测试
    python scripts/test_full_flow.py --test basic
    python scripts/test_full_flow.py --test subagent
    python scripts/test_full_flow.py --test fork
    python scripts/test_full_flow.py --test abort
    
    # 等待更长时间（用于观察MCP初始化等慢操作）
    python scripts/test_full_flow.py --wait 30
"""

import asyncio
import json
import websockets
import requests
import time
import sys
import os
import argparse
from pathlib import Path

BASE_URL = "http://localhost:5555"
WS_URL = "ws://localhost:5555/ws/events"
SESSION_DIR = Path.home() / ".mycode" / "sessions"


class EventCollector:
    """收集WebSocket事件"""
    
    def __init__(self):
        self.ws = None
        self.events = []
        self.session_events = {}  # 按session_id分组
        
    async def connect(self):
        """连接WebSocket"""
        self.ws = await websockets.connect(
            WS_URL, 
            ping_interval=None, 
            max_size=10*1024*1024
        )
        print(f"[WS] 已连接")
        
    async def recv_loop(self, duration):
        """接收事件"""
        start = time.time()
        try:
            while time.time() - start < duration:
                try:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=1)
                    ev = json.loads(msg)
                    self.events.append(ev)
                    
                    # 按session_id分组
                    sid = ev.get('session_id', 'unknown')
                    if sid not in self.session_events:
                        self.session_events[sid] = []
                    self.session_events[sid].append(ev)
                    
                    # 打印关键事件
                    et = ev.get('type', '?')
                    seq = ev.get('seq', '-')
                    ct = str(ev.get('content', ''))[:40] if ev.get('content') else ''
                    aid = ev.get('agent_id', '')
                    at = ev.get('agent_type', '')
                    extra = f" agent_id={aid} agent_type={at}" if aid else ''
                    print(f"  [{len(self.events):3d}] {et:25s} seq={str(seq):>3s} sid={sid[:8]}{extra} {ct}")
                    
                except asyncio.TimeoutError:
                    continue
        except websockets.exceptions.ConnectionClosed as e:
            print(f"[WS] 连接关闭: {e}")
    
    def send_msg(self, msg, sid=None):
        """发送消息"""
        data = {"message": msg, "cwd": str(Path.cwd())}
        if sid:
            data["session_id"] = sid
        r = requests.post(f"{BASE_URL}/api/chat/stream", json=data)
        result = r.json()
        print(f"[HTTP] POST /api/chat/stream -> {json.dumps(result, ensure_ascii=False)}")
        return result.get('session_id')
    
    def fork(self, sid, keep=1):
        """Fork session"""
        r = requests.post(f"{BASE_URL}/api/sessions/{sid}/fork", json={"keep_user_messages": keep})
        result = r.json()
        print(f"[HTTP] POST /api/sessions/{sid}/fork -> {json.dumps(result, ensure_ascii=False)}")
        return result
    
    def abort(self, sid):
        """Abort session"""
        r = requests.post(f"{BASE_URL}/api/sessions/{sid}/abort")
        result = r.json()
        print(f"[HTTP] POST /api/sessions/{sid}/abort -> {json.dumps(result, ensure_ascii=False)}")
        return result
    
    def get_session(self, sid):
        """获取session详情"""
        r = requests.get(f"{BASE_URL}/api/sessions/{sid}")
        return r.json() if r.ok else None
    
    def get_events(self, sid, limit=50):
        """获取session事件"""
        r = requests.get(f"{BASE_URL}/api/sessions/{sid}/events", params={"limit": limit})
        return r.json() if r.ok else None
    
    def check_jsonl(self, sid):
        """检查events.jsonl文件"""
        p = SESSION_DIR / f"{sid}.events.jsonl"
        if not p.exists():
            print(f"[CHECK] {sid}.events.jsonl 不存在")
            return []
        
        with open(p) as f:
            events = [json.loads(l) for l in f if l.strip()]
        
        print(f"[CHECK] {sid}.events.jsonl: {len(events)} events")
        for e in events:
            ct = str(e.get('content', ''))[:40] if e.get('content') else ''
            seq = str(e.get('seq', '-'))
            print(f"  seq={seq:>3s} {e.get('type'):25s} {ct}")
        
        return events
    
    def check_json(self, sid):
        """检查.json文件"""
        p = SESSION_DIR / f"{sid}.json"
        if not p.exists():
            print(f"[CHECK] {sid}.json 不存在")
            return None
        
        with open(p) as f:
            data = json.load(f)
        
        events = data.get('events', [])
        print(f"[CHECK] {sid}.json: {len(events)} events")
        
        return data
    
    def save(self, path='/tmp/ws_events.json'):
        """保存所有事件"""
        with open(path, 'w') as f:
            json.dump(self.events, f, ensure_ascii=False, indent=2)
        print(f"[SAVE] {len(self.events)} events -> {path}")
    
    def analyze(self):
        """分析事件"""
        print("\n" + "="*60)
        print("事件分析")
        print("="*60)
        
        # 统计每个session的事件数
        print("\nSession事件分布:")
        for sid, events in sorted(self.session_events.items(), key=lambda x: -len(x[1])):
            print(f"  {sid[:8]}: {len(events)} events")
        
        # 检查子智能体事件
        sub_events = [e for e in self.events if 'sub_agent' in e.get('type', '')]
        print(f"\n子智能体事件: {len(sub_events)}")
        for e in sub_events:
            print(f"  {e.get('type')} agent_id={e.get('agent_id','')} agent_type={e.get('agent_type','')}")
        
        # 检查重复事件
        print("\n检查重复事件:")
        event_seqs = {}
        for e in self.events:
            seq = e.get('seq')
            sid = e.get('session_id')
            if seq is not None:
                key = (sid, seq)
                if key not in event_seqs:
                    event_seqs[key] = []
                event_seqs[key].append(e)
        
        duplicates = {k: v for k, v in event_seqs.items() if len(v) > 1}
        if duplicates:
            print(f"  发现 {len(duplicates)} 个重复事件:")
            for (sid, seq), events in duplicates.items():
                print(f"    {sid[:8]} seq={seq}: {len(events)} 次")
        else:
            print("  没有重复事件")


async def test_basic(collector, wait_time=15):
    """测试基本聊天"""
    print("\n" + "="*60)
    print("测试1: 基本聊天")
    print("="*60)
    
    sid = collector.send_msg("你好，请简短回复")
    await collector.recv_loop(wait_time)
    
    print(f"\n--- 检查events.jsonl ---")
    collector.check_jsonl(sid)
    
    print(f"\n--- 检查GET /api/sessions/{sid}/events ---")
    api_events = collector.get_events(sid)
    if api_events:
        print(f"  API返回 {len(api_events.get('events', []))} events, total={api_events.get('total_count')}")
    
    return sid


async def test_subagent(collector, sid, wait_time=30):
    """测试子智能体"""
    print("\n" + "="*60)
    print("测试2: 子智能体")
    print("="*60)
    
    collector.send_msg("请并行调用两个子智能体向我打招呼", sid)
    await collector.recv_loop(wait_time)
    
    print(f"\n--- 检查events.jsonl ---")
    events = collector.check_jsonl(sid)
    
    sub_events = [e for e in events if 'sub_agent' in e.get('type', '')]
    print(f"  子智能体事件数: {len(sub_events)}")
    
    return sid


async def test_fork(collector, sid, wait_time=2):
    """测试Fork"""
    print("\n" + "="*60)
    print("测试3: Fork")
    print("="*60)
    
    fork_result = collector.fork(sid, keep=1)
    new_sid = fork_result.get('new_session_id')
    
    await collector.recv_loop(wait_time)
    
    print(f"\n--- 检查新session的events.jsonl ---")
    if new_sid:
        collector.check_jsonl(new_sid)
        
        print(f"\n--- 检查GET /api/sessions/{new_sid}/events ---")
        fork_api = collector.get_events(new_sid)
        if fork_api:
            print(f"  API返回 {len(fork_api.get('events', []))} events")
    
    return new_sid


async def test_abort(collector, wait_time=5):
    """测试Abort"""
    print("\n" + "="*60)
    print("测试4: Abort")
    print("="*60)
    
    sid = collector.send_msg("请详细解释人工智能的各个方面，包括历史技术应用场景等")
    await asyncio.sleep(3)
    
    collector.abort(sid)
    await collector.recv_loop(wait_time)
    
    print(f"\n--- 检查abort后的events.jsonl ---")
    collector.check_jsonl(sid)
    
    return sid


async def main():
    parser = argparse.ArgumentParser(description='MyCode全链路测试')
    parser.add_argument('--test', choices=['basic', 'subagent', 'fork', 'abort', 'all'], 
                       default='all', help='运行特定测试')
    parser.add_argument('--wait', type=int, default=15, help='每个测试等待时间（秒）')
    args = parser.parse_args()
    
    collector = EventCollector()
    await collector.connect()
    
    try:
        sid1 = None
        sid2 = None
        
        if args.test in ['basic', 'all']:
            sid1 = await test_basic(collector, args.wait)
        
        if args.test in ['subagent', 'all'] and sid1:
            await test_subagent(collector, sid1, args.wait * 2)
        
        if args.test in ['fork', 'all'] and sid1:
            await test_fork(collector, sid1, args.wait)
        
        if args.test in ['abort', 'all']:
            sid2 = await test_abort(collector, args.wait)
        
        # 分析结果
        collector.analyze()
        
        # 保存事件
        collector.save('/tmp/MYCODE_test_events.json')
        
    finally:
        if collector.ws:
            await collector.ws.close()


if __name__ == "__main__":
    asyncio.run(main())
