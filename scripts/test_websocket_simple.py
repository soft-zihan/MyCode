#!/usr/bin/env python3
"""
WebSocket 连接层重构 - 核心功能测试

测试目标：
1. WebSocket 不会重复连接
2. 事件能正确接收
3. session/title 事件能正确接收

用法:
    python3 scripts/test_websocket_simple.py
"""

import asyncio
import json
import websockets
import requests
import time
from pathlib import Path
from typing import Optional

BASE_URL = "http://localhost:5555"
WS_URL = "ws://localhost:5555/ws/events"


class WebSocketClient:
    """模拟前端 WebSocket 客户端"""
    
    def __init__(self, client_id: str = "client1"):
        self.client_id = client_id
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.events = []
        self._recv_task = None
        self._running = False
    
    async def connect(self):
        """连接 WebSocket"""
        self.ws = await websockets.connect(WS_URL, ping_interval=None)
        self._running = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        print(f"[{self.client_id}] WebSocket 已连接")
    
    async def _recv_loop(self):
        """接收事件循环"""
        while self._running:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=1)
                event = json.loads(msg)
                self.events.append(event)
                
                event_type = event.get('type', '?')
                session_id = event.get('session_id', 'unknown')
                print(f"[{self.client_id}] 收到事件: {event_type} session={session_id[:8] if session_id else 'none'}")
                
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception as e:
                print(f"[{self.client_id}] 接收错误: {e}")
                break
    
    async def disconnect(self):
        """断开连接"""
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
        print(f"[{self.client_id}] WebSocket 已断开")
    
    async def subscribe_session(self, session_id: str):
        """订阅 session"""
        if self.ws:
            cmd = {"type": "subscribe", "session_id": session_id}
            await self.ws.send(json.dumps(cmd))
            print(f"[{self.client_id}] 订阅 session: {session_id[:8]}")
    
    def has_event_type(self, event_type: str) -> bool:
        """检查是否有指定类型的事件"""
        return any(e.get('type') == event_type for e in self.events)
    
    def get_event_count(self) -> int:
        """获取事件数量"""
        return len(self.events)


def send_message(message: str, session_id: str = None, cwd: str = None) -> dict:
    """发送消息"""
    data = {"message": message, "cwd": cwd or str(Path.cwd())}
    if session_id:
        data["session_id"] = session_id
    r = requests.post(f"{BASE_URL}/api/chat/stream", json=data)
    return r.json()


async def test_no_duplicate_connection():
    """测试1: WebSocket 不会重复连接"""
    print("\n" + "="*60)
    print("测试1: WebSocket 不会重复连接")
    print("="*60)
    
    # 创建两个客户端模拟 React.StrictMode
    client1 = WebSocketClient("client1")
    client2 = WebSocketClient("client2")
    
    await client1.connect()
    await client2.connect()
    
    # 发送消息
    result = send_message("你好，请简短回复")
    print(f"发送消息结果: {result}")
    
    # 等待事件
    await asyncio.sleep(5)
    
    # 检查：两个客户端应该收到相同的事件（不是重复）
    events1 = client1.get_event_count()
    events2 = client2.get_event_count()
    
    print(f"\nclient1 收到 {events1} 个事件")
    print(f"client2 收到 {events2} 个事件")
    
    # 事件数量应该大致相同
    diff = abs(events1 - events2)
    success = diff < 5 and events1 > 0
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    print(f"事件数量差异: {diff}")
    
    await client1.disconnect()
    await client2.disconnect()
    
    return success


async def test_event_reception():
    """测试2: 事件能正确接收"""
    print("\n" + "="*60)
    print("测试2: 事件能正确接收")
    print("="*60)
    
    client = WebSocketClient("event_test")
    await client.connect()
    
    # 发送消息
    result = send_message("请帮我写一个 Python 函数计算斐波那契数列")
    print(f"发送消息结果: {result}")
    
    # 等待事件
    await asyncio.sleep(10)
    
    # 检查事件类型
    has_session_created = client.has_event_type('session/created')
    has_turn_start = client.has_event_type('turn/start')
    has_user_message = client.has_event_type('user_message')
    
    print(f"\n收到 session/created: {has_session_created}")
    print(f"收到 turn/start: {has_turn_start}")
    print(f"收到 user_message: {has_user_message}")
    
    success = has_session_created and has_turn_start and has_user_message
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    
    await client.disconnect()
    
    return success


async def test_session_title():
    """测试3: session/title 事件能正确接收"""
    print("\n" + "="*60)
    print("测试3: session/title 事件能正确接收")
    print("="*60)
    
    client = WebSocketClient("title_test")
    await client.connect()
    
    # 发送消息
    result = send_message("请帮我写一个 Python 函数计算斐波那契数列")
    session_id = result.get('session_id')
    print(f"发送消息结果: {result}")
    print(f"当前 session ID: {session_id}")
    
    # 订阅 session（关键！）
    await client.subscribe_session(session_id)
    
    # 等待 title 生成（可能需要较长时间）
    print("等待 title 生成...")
    await asyncio.sleep(20)
    
    # 检查 session/title 事件
    has_title = client.has_event_type('session/title')
    
    print(f"\n收到 session/title: {has_title}")
    
    # 打印所有事件类型和 session_id
    print("\n所有事件:")
    for e in client.events:
        event_type = e.get('type')
        event_session = e.get('session_id', 'none')
        print(f"  {event_type} -> session={event_session[:8] if event_session else 'none'}")
    
    # 检查是否有当前 session 的事件
    current_session_events = [e for e in client.events if e.get('session_id') == session_id]
    print(f"\n当前 session 的事件数: {len(current_session_events)}")
    
    success = has_title
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    if not success:
        print("注意: title 生成可能需要更长时间，或者 title 生成服务未启动")
    
    await client.disconnect()
    
    return success


async def main():
    print("="*60)
    print("WebSocket 连接层重构 - 核心功能测试")
    print("="*60)
    
    results = []
    
    # 运行测试
    results.append(await test_no_duplicate_connection())
    results.append(await test_event_reception())
    results.append(await test_session_title())
    
    # 打印结果
    print("\n" + "="*60)
    print("测试结果汇总")
    print("="*60)
    
    passed = sum(results)
    failed = len(results) - passed
    
    print(f"通过: {passed}")
    print(f"失败: {failed}")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    import sys
    sys.exit(0 if success else 1)
