#!/usr/bin/env python3
"""
模拟用户完整操作流程的测试脚本

测试场景：
1. 新建session
2. 发送第一条消息
3. 检查消息是否正确显示
4. 发送第二条消息
5. 检查消息是否正确显示
"""

import asyncio
import json
import websockets
import requests
import time
from pathlib import Path

BASE_URL = "http://localhost:5555"
WS_URL = "ws://localhost:5555/ws/events"


class UserFlowTest:
    def __init__(self):
        self.ws = None
        self.events = []
        self.session_id = None
        
    async def connect(self):
        """连接WebSocket"""
        self.ws = await websockets.connect(WS_URL, ping_interval=None, max_size=10*1024*1024)
        print("[WS] 已连接")
        
    async def recv_loop(self, duration):
        """接收事件"""
        start = time.time()
        try:
            while time.time() - start < duration:
                try:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=1)
                    ev = json.loads(msg)
                    self.events.append(ev)
                    
                    # 打印关键事件
                    et = ev.get('type', '?')
                    seq = ev.get('seq', '-')
                    sid = (ev.get('session_id') or '?')[:8]
                    ct = str(ev.get('content', ''))[:40] if ev.get('content') else ''
                    print(f"  [{len(self.events):3d}] {et:25s} seq={str(seq):>3s} sid={sid} {ct}")
                    
                except asyncio.TimeoutError:
                    continue
        except websockets.exceptions.ConnectionClosed as e:
            print(f"[WS] 连接关闭: {e}")
    
    def new_session(self):
        """新建session"""
        print("\n[TEST] 新建session")
        # 前端调用handleNewSession，这里模拟前端的行为
        # 实际上前端会先调用sessionStore.create()，然后发送消息
        
    def send_message(self, msg):
        """发送消息"""
        print(f"\n[TEST] 发送消息: {msg}")
        data = {
            "message": msg,
            "cwd": str(Path.cwd()),
            "session_id": self.session_id
        }
        r = requests.post(f"{BASE_URL}/api/chat/stream", json=data)
        result = r.json()
        print(f"[HTTP] POST /api/chat/stream -> {json.dumps(result, ensure_ascii=False)}")
        
        if result.get('is_new_session'):
            self.session_id = result.get('session_id')
            print(f"[TEST] 新session创建: {self.session_id}")
        
        return result
    
    def check_session(self):
        """检查session状态"""
        print(f"\n[TEST] 检查session {self.session_id}")
        
        # 检查events.jsonl
        p = Path.home() / ".mycode" / "sessions" / f"{self.session_id}.events.jsonl"
        if p.exists():
            with open(p) as f:
                events = [json.loads(l) for l in f if l.strip()]
            print(f"[CHECK] events.jsonl: {len(events)} events")
            for e in events[-5:]:  # 显示最后5个事件
                ct = str(e.get('content', ''))[:40] if e.get('content') else ''
                seq = str(e.get('seq', '-'))
                print(f"  seq={seq:>3s} {e.get('type'):25s} {ct}")
        else:
            print(f"[CHECK] events.jsonl 不存在")
        
        # 检查API
        r = requests.get(f"{BASE_URL}/api/sessions/{self.session_id}/events")
        if r.ok:
            data = r.json()
            print(f"[CHECK] API返回 {len(data.get('events', []))} events")
    
    def check_events_by_session(self):
        """按session_id分组检查事件"""
        print("\n[TEST] 按session_id分组检查事件")
        session_events = {}
        for e in self.events:
            sid = e.get('session_id', 'unknown')
            if sid not in session_events:
                session_events[sid] = []
            session_events[sid].append(e)
        
        for sid, events in session_events.items():
            print(f"  {sid[:8]}: {len(events)} events")
            # 显示关键事件
            for e in events:
                if e.get('type') in ['user_message', 'assistant_message', 'turn/start', 'turn/end']:
                    ct = str(e.get('content', ''))[:40] if e.get('content') else ''
                    seq = str(e.get('seq', '-'))
                    print(f"    seq={seq:>3s} {e.get('type'):25s} {ct}")


async def test_user_flow():
    test = UserFlowTest()
    await test.connect()
    
    # 测试1: 新建session并发送第一条消息
    print("\n" + "="*60)
    print("测试1: 新建session并发送第一条消息")
    print("="*60)
    test.send_message("你好，这是第一条消息")
    await test.recv_loop(10)
    test.check_session()
    
    # 测试2: 发送第二条消息
    print("\n" + "="*60)
    print("测试2: 发送第二条消息")
    print("="*60)
    test.send_message("这是第二条消息")
    await test.recv_loop(10)
    test.check_session()
    
    # 测试3: 检查所有事件
    print("\n" + "="*60)
    print("测试3: 检查所有事件")
    print("="*60)
    test.check_events_by_session()
    
    # 保存事件
    with open('/tmp/user_flow_events.json', 'w') as f:
        json.dump(test.events, f, ensure_ascii=False, indent=2)
    print(f"\n[SAVE] 事件已保存到 /tmp/user_flow_events.json")


if __name__ == "__main__":
    asyncio.run(test_user_flow())
