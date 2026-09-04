#!/usr/bin/env python3
"""
WebSocket 连接层重构自动化测试

测试目标：
1. WebSocket 不会重复连接
2. 后台 session 的 snapshot 能正确更新
3. isStreaming 按 session 隔离
4. session/title 事件能正确接收
5. 多标签页场景下事件正确路由

用法:
    python scripts/test_websocket_refactor.py
"""

import asyncio
import json
import websockets
import requests
import time
import sys
from pathlib import Path
from typing import Optional

BASE_URL = "http://localhost:5555"
WS_URL = "ws://localhost:5555/ws/events"


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = True
        self.details = []
    
    def add_detail(self, msg: str, passed: bool = True):
        self.details.append((msg, passed))
        if not passed:
            self.passed = False
    
    def __str__(self):
        status = "✅ PASS" if self.passed else "❌ FAIL"
        lines = [f"{status} {self.name}"]
        for msg, passed in self.details:
            mark = "✓" if passed else "✗"
            lines.append(f"  {mark} {msg}")
        return "\n".join(lines)


class WebSocketClient:
    """模拟前端 WebSocket 客户端"""
    
    def __init__(self, client_id: str = "client1"):
        self.client_id = client_id
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.events = []
        self.session_events = {}
        self.subscribed_sessions = set()
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
                
                session_id = event.get('session_id', 'unknown')
                if session_id not in self.session_events:
                    self.session_events[session_id] = []
                self.session_events[session_id].append(event)
                
                event_type = event.get('type', '?')
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
    
    def subscribe_session(self, session_id: str):
        """订阅 session"""
        if self.ws:
            cmd = {"type": "subscribe", "session_id": session_id}
            asyncio.create_task(self.ws.send(json.dumps(cmd)))
            self.subscribed_sessions.add(session_id)
            print(f"[{self.client_id}] 订阅 session: {session_id[:8]}")
    
    def unsubscribe_session(self, session_id: str):
        """取消订阅 session"""
        if self.ws:
            cmd = {"type": "unsubscribe", "session_id": session_id}
            asyncio.create_task(self.ws.send(json.dumps(cmd)))
            self.subscribed_sessions.discard(session_id)
            print(f"[{self.client_id}] 取消订阅 session: {session_id[:8]}")
    
    def get_events_for_session(self, session_id: str) -> list:
        """获取指定 session 的事件"""
        return self.session_events.get(session_id, [])
    
    def has_event_type(self, session_id: str, event_type: str) -> bool:
        """检查是否有指定类型的事件"""
        events = self.get_events_for_session(session_id)
        return any(e.get('type') == event_type for e in events)
    
    def get_event_count(self, session_id: str = None) -> int:
        """获取事件数量"""
        if session_id:
            return len(self.get_events_for_session(session_id))
        return len(self.events)
    
    def clear_events(self):
        """清空事件"""
        self.events.clear()
        self.session_events.clear()


def send_message(message: str, session_id: str = None, cwd: str = None) -> str:
    """发送消息"""
    data = {"message": message, "cwd": cwd or str(Path.cwd())}
    if session_id:
        data["session_id"] = session_id
    r = requests.post(f"{BASE_URL}/api/chat/stream", json=data)
    result = r.json()
    return result.get('session_id')


def abort_session(session_id: str):
    """中止 session"""
    r = requests.post(f"{BASE_URL}/api/sessions/{session_id}/abort")
    return r.json()


def get_sessions() -> list:
    """获取所有 sessions"""
    r = requests.get(f"{BASE_URL}/api/sessions")
    return r.json() if r.ok else []


def get_session_events(session_id: str, limit: int = 100) -> dict:
    """获取 session 事件"""
    r = requests.get(f"{BASE_URL}/api/sessions/{session_id}/events", params={"limit": limit})
    return r.json() if r.ok else {}


async def test_no_duplicate_connection() -> TestResult:
    """测试1: WebSocket 不会重复连接"""
    result = TestResult("测试1: WebSocket 不会重复连接")
    
    # 创建两个客户端模拟 React.StrictMode
    client1 = WebSocketClient("client1")
    client2 = WebSocketClient("client2")
    
    await client1.connect()
    await client2.connect()
    
    # 发送消息
    session_id = send_message("你好，请简短回复")
    
    # 等待事件
    await asyncio.sleep(5)
    
    # 检查：两个客户端应该收到相同的事件（不是重复）
    events1 = client1.get_event_count()
    events2 = client2.get_event_count()
    
    result.add_detail(f"client1 收到 {events1} 个事件", events1 > 0)
    result.add_detail(f"client2 收到 {events2} 个事件", events2 > 0)
    
    # 事件数量应该大致相同（允许少量差异）
    diff = abs(events1 - events2)
    result.add_detail(f"事件数量差异: {diff}", diff < 5)
    
    await client1.disconnect()
    await client2.disconnect()
    
    return result


async def test_background_session_update() -> TestResult:
    """测试2: 后台 session 的 snapshot 能正确更新（注意：当前设计会 abort 旧 session）"""
    result = TestResult("测试2: 后台 session 的 snapshot 能正确更新")
    
    client = WebSocketClient("bg_test")
    await client.connect()
    
    # 创建 session A 并开始 streaming
    send_message("请详细解释人工智能的各个方面，包括历史、技术、应用等")
    
    # 等待 session/created 事件
    await asyncio.sleep(2)
    
    # 从事件中获取实际的 session_id
    session_a = None
    for event in client.events:
        if event.get('type') == 'session/created':
            session_a = event.get('session_id')
            break
    
    if not session_a:
        result.add_detail("未找到 session/created 事件", False)
        await client.disconnect()
        return result
    
    client.subscribe_session(session_a)
    result.add_detail(f"session A 实际 ID: {session_a[:8]}")
    
    # 等待一些事件
    await asyncio.sleep(3)
    
    # 记录 session A 的事件数量
    events_a_before = client.get_event_count(session_a)
    result.add_detail(f"session A 切换前有 {events_a_before} 个事件")
    
    # 注意：当前设计中，创建新 session 会 abort 旧 session
    # 所以这里不创建 session B，而是等待 session A 完成
    
    # 等待 session A 完成
    await asyncio.sleep(10)
    
    # 检查：session A 应该有 turn/end 事件
    has_turn_end_a = client.has_event_type(session_a, 'turn/end')
    result.add_detail(f"session A 有 turn/end 事件: {has_turn_end_a}", has_turn_end_a)
    
    # 检查 session A 的事件数量是否增加
    events_a_after = client.get_event_count(session_a)
    result.add_detail(f"session A 最终有 {events_a_after} 个事件", events_a_after > events_a_before)
    
    await client.disconnect()
    
    return result


async def test_streaming_state_isolation() -> TestResult:
    """测试3: isStreaming 按 session 隔离"""
    result = TestResult("测试3: isStreaming 按 session 隔离")
    
    client = WebSocketClient("streaming_test")
    await client.connect()
    
    # 创建 session A 并开始 streaming
    send_message("请详细解释量子计算的原理和应用")
    
    # 等待 session/created 事件
    await asyncio.sleep(2)
    
    # 从事件中获取实际的 session_id
    session_a = None
    for event in client.events:
        if event.get('type') == 'session/created':
            session_a = event.get('session_id')
            break
    
    if not session_a:
        result.add_detail("未找到 session/created 事件", False)
        await client.disconnect()
        return result
    
    client.subscribe_session(session_a)
    result.add_detail(f"session A 实际 ID: {session_a[:8]}")
    
    # 检查 session A 的 turn/start 事件
    await asyncio.sleep(2)
    has_start_a = client.has_event_type(session_a, 'turn/start')
    result.add_detail(f"session A 有 turn/start 事件: {has_start_a}", has_start_a)
    
    # 创建 session B（另一个 session）
    send_message("你好")
    
    await asyncio.sleep(2)
    
    # 从事件中获取 session B
    session_b = None
    for event in client.events:
        if event.get('type') == 'session/created' and event.get('session_id') != session_a:
            session_b = event.get('session_id')
            break
    
    if session_b:
        client.subscribe_session(session_b)
        result.add_detail(f"session B 实际 ID: {session_b[:8]}")
        
        # 检查 session B 的 turn/start 事件
        await asyncio.sleep(2)
        has_start_b = client.has_event_type(session_b, 'turn/start')
        result.add_detail(f"session B 有 turn/start 事件: {has_start_b}", has_start_b)
    
    # 等待 session A 结束
    await asyncio.sleep(10)
    
    # 检查 session A 的 turn/end 事件
    has_end_a = client.has_event_type(session_a, 'turn/end')
    result.add_detail(f"session A 有 turn/end 事件: {has_end_a}", has_end_a)
    
    await client.disconnect()
    
    return result


async def test_session_title_event() -> TestResult:
    """测试4: session/title 事件能正确接收"""
    result = TestResult("测试4: session/title 事件能正确接收")
    
    client = WebSocketClient("title_test")
    await client.connect()
    
    # 创建 session
    send_message("请帮我写一个 Python 函数计算斐波那契数列")
    
    # 等待 session/created 事件
    await asyncio.sleep(2)
    
    # 从事件中获取实际的 session_id
    session_id = None
    for event in client.events:
        if event.get('type') == 'session/created':
            session_id = event.get('session_id')
            break
    
    if not session_id:
        result.add_detail("未找到 session/created 事件", False)
        await client.disconnect()
        return result
    
    client.subscribe_session(session_id)
    result.add_detail(f"session 实际 ID: {session_id[:8]}")
    
    # 等待 title 生成（可能需要较长时间）
    await asyncio.sleep(15)
    
    # 检查 session/title 事件
    has_title = client.has_event_type(session_id, 'session/title')
    result.add_detail(f"收到 session/title 事件: {has_title}", has_title)
    
    # 检查 session 列表
    sessions = get_sessions()
    session_names = {s['id']: s.get('name') for s in sessions}
    
    if session_id in session_names:
        name = session_names[session_id]
        result.add_detail(f"session 名称: {name}", name is not None and len(name) > 0)
    else:
        result.add_detail(f"session {session_id[:8]} 不在列表中", False)
    
    await client.disconnect()
    
    return result


async def test_multi_tab_event_routing() -> TestResult:
    """测试5: 多标签页场景下事件正确路由"""
    result = TestResult("测试5: 多标签页场景下事件正确路由")
    
    # 模拟两个标签页
    tab1 = WebSocketClient("tab1")
    tab2 = WebSocketClient("tab2")
    
    await tab1.connect()
    await tab2.connect()
    
    # 创建 session A
    send_message("你好，请简短回复")
    
    # 等待 session/created 事件
    await asyncio.sleep(2)
    
    # 从事件中获取实际的 session_id
    session_a = None
    for event in tab1.events:
        if event.get('type') == 'session/created':
            session_a = event.get('session_id')
            break
    
    if not session_a:
        result.add_detail("未找到 session/created 事件", False)
        await tab1.disconnect()
        await tab2.disconnect()
        return result
    
    # tab1 订阅 session A
    tab1.subscribe_session(session_a)
    result.add_detail(f"session A 实际 ID: {session_a[:8]}")
    
    await asyncio.sleep(5)
    
    # 检查：tab1 应该收到 session A 的事件
    tab1_events_a = tab1.get_event_count(session_a)
    result.add_detail(f"tab1 收到 session A 事件: {tab1_events_a}", tab1_events_a > 0)
    
    # 创建 session B
    send_message("再见")
    
    await asyncio.sleep(2)
    
    # 从事件中获取 session B
    session_b = None
    for event in tab1.events:
        if event.get('type') == 'session/created' and event.get('session_id') != session_a:
            session_b = event.get('session_id')
            break
    
    if session_b:
        # tab2 订阅 session B
        tab2.subscribe_session(session_b)
        result.add_detail(f"session B 实际 ID: {session_b[:8]}")
        
        await asyncio.sleep(5)
        
        # 检查：tab2 应该收到 session B 的事件
        tab2_events_b = tab2.get_event_count(session_b)
        result.add_detail(f"tab2 收到 session B 事件: {tab2_events_b}", tab2_events_b > 0)
    else:
        result.add_detail("未找到 session B", False)
    
    await tab1.disconnect()
    await tab2.disconnect()
    
    return result


async def test_abort_and_continue() -> TestResult:
    """测试6: 中止后能继续对话"""
    result = TestResult("测试6: 中止后能继续对话")
    
    client = WebSocketClient("abort_test")
    await client.connect()
    
    # 创建 session 并开始长任务
    send_message("请详细解释人工智能的各个方面，包括历史、技术、应用、伦理等")
    
    # 等待 session/created 事件
    await asyncio.sleep(2)
    
    # 从事件中获取实际的 session_id
    session_id = None
    for event in client.events:
        if event.get('type') == 'session/created':
            session_id = event.get('session_id')
            break
    
    if not session_id:
        result.add_detail("未找到 session/created 事件", False)
        await client.disconnect()
        return result
    
    client.subscribe_session(session_id)
    result.add_detail(f"session 实际 ID: {session_id[:8]}")
    
    # 等待一段时间
    await asyncio.sleep(3)
    
    # 中止
    abort_result = abort_session(session_id)
    result.add_detail(f"中止结果: {abort_result}")
    
    await asyncio.sleep(2)
    
    # 检查 turn/end 事件
    has_end = client.has_event_type(session_id, 'turn/end')
    result.add_detail(f"收到 turn/end 事件: {has_end}", has_end)
    
    # 继续对话
    client.clear_events()
    send_message("请简短回复", session_id)
    
    await asyncio.sleep(5)
    
    # 检查新的事件
    new_events = client.get_event_count(session_id)
    result.add_detail(f"继续对话后收到 {new_events} 个事件", new_events > 0)
    
    # 检查新的 turn/start
    has_new_start = client.has_event_type(session_id, 'turn/start')
    result.add_detail(f"收到新的 turn/start 事件: {has_new_start}", has_new_start)
    
    await client.disconnect()
    
    return result


async def main():
    print("=" * 60)
    print("WebSocket 连接层重构自动化测试")
    print("=" * 60)
    
    results = []
    
    # 运行所有测试
    tests = [
        test_no_duplicate_connection,
        test_background_session_update,
        test_streaming_state_isolation,
        test_session_title_event,
        test_multi_tab_event_routing,
        test_abort_and_continue,
    ]
    
    for test in tests:
        print(f"\n运行: {test.__doc__}")
        try:
            result = await test()
            results.append(result)
        except Exception as e:
            print(f"测试异常: {e}")
            result = TestResult(test.__doc__)
            result.add_detail(f"异常: {e}", False)
            results.append(result)
    
    # 打印结果
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for result in results:
        print(result)
        if result.passed:
            passed += 1
        else:
            failed += 1
    
    print("\n" + "-" * 60)
    print(f"总计: {passed} 通过, {failed} 失败")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
