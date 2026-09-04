#!/usr/bin/env python3
"""
WebSocket 连接层重构 - 完整测试

测试目标：
1. WebSocket 不会重复连接
2. 事件能正确接收
3. session/title 事件能正确接收
4. 子智能体事件可见
5. 后台 session 更新（切换 session 后后台继续运行）
6. Abort 后能继续对话
7. Fork 后新 session 事件正确
8. Truncate 后能继续对话

用法:
    python3 scripts/test_websocket_complete.py
    python3 scripts/test_websocket_complete.py --test fork
    python3 scripts/test_websocket_complete.py --test subagent
"""

import asyncio
import json
import websockets
import requests
import time
import argparse
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
        self.ws = await websockets.connect(WS_URL, ping_interval=None, max_size=10*1024*1024)
        self._running = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        print(f"[{self.client_id}] WebSocket 已连接")
    
    async def _recv_loop(self):
        while self._running:
            try:
                msg = await asyncio.wait_for(self.ws.recv(), timeout=1)
                event = json.loads(msg)
                self.events.append(event)
                
                event_type = event.get('type', '?')
                session_id = event.get('session_id', 'unknown')
                sub_agent_id = event.get('sub_agent_id', '')
                extra = f" sub_agent={sub_agent_id[:8]}" if sub_agent_id else ""
                print(f"[{self.client_id}] {event_type:25s} session={session_id[:8] if session_id else 'none'}{extra}")
                
            except asyncio.TimeoutError:
                continue
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception as e:
                print(f"[{self.client_id}] 接收错误: {e}")
                break
    
    async def disconnect(self):
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
        if self.ws:
            cmd = {"type": "subscribe", "session_id": session_id}
            await self.ws.send(json.dumps(cmd))
            print(f"[{self.client_id}] 订阅 session: {session_id[:8]}")
    
    async def unsubscribe_session(self, session_id: str):
        if self.ws:
            cmd = {"type": "unsubscribe", "session_id": session_id}
            await self.ws.send(json.dumps(cmd))
            print(f"[{self.client_id}] 取消订阅 session: {session_id[:8]}")
    
    def has_event_type(self, event_type: str, session_id: str = None) -> bool:
        for e in self.events:
            if e.get('type') == event_type:
                if session_id is None or e.get('session_id') == session_id:
                    return True
        return False
    
    def has_sub_agent_events(self, main_session_id: str = None) -> bool:
        """检查是否有子智能体事件（来自不同 session 的事件）"""
        if not main_session_id:
            return any(e.get('sub_agent_id') for e in self.events)
        session_ids = set(e.get('session_id') for e in self.events if e.get('session_id'))
        return len(session_ids) > 1 or main_session_id not in session_ids
    
    def get_event_count(self, session_id: str = None) -> int:
        if session_id:
            return len([e for e in self.events if e.get('session_id') == session_id])
        return len(self.events)
    
    def clear_events(self):
        self.events.clear()
    
    async def wait_for_event(self, event_type: str, session_id: str = None, timeout: float = 30) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self.has_event_type(event_type, session_id):
                return True
            await asyncio.sleep(0.5)
        return False


def send_message(message: str, session_id: str = None, cwd: str = None) -> dict:
    data = {"message": message, "cwd": cwd or str(Path.cwd())}
    if session_id:
        data["session_id"] = session_id
    r = requests.post(f"{BASE_URL}/api/chat/stream", json=data)
    return r.json()


def abort_session(session_id: str) -> dict:
    r = requests.post(f"{BASE_URL}/api/sessions/{session_id}/abort")
    return r.json()


def fork_session(session_id: str, keep: int = 1) -> dict:
    r = requests.post(f"{BASE_URL}/api/sessions/{session_id}/fork", json={"keep_user_messages": keep})
    return r.json()


def truncate_session(session_id: str, keep: int = 1) -> dict:
    r = requests.post(f"{BASE_URL}/api/sessions/{session_id}/truncate", json={"keep_user_messages": keep})
    return r.json()


def get_session_events(session_id: str) -> dict:
    r = requests.get(f"{BASE_URL}/api/sessions/{session_id}/events", params={"limit": 100})
    return r.json() if r.ok else {}


async def test_no_duplicate_connection():
    """测试1: WebSocket 不会重复连接"""
    print("\n" + "="*60)
    print("测试1: WebSocket 不会重复连接")
    print("="*60)
    
    client1 = WebSocketClient("client1")
    client2 = WebSocketClient("client2")
    
    await client1.connect()
    await client2.connect()
    
    result = send_message("你好，请简短回复")
    print(f"发送消息结果: {result}")
    
    await asyncio.sleep(5)
    
    events1 = client1.get_event_count()
    events2 = client2.get_event_count()
    
    print(f"\nclient1 收到 {events1} 个事件")
    print(f"client2 收到 {events2} 个事件")
    
    diff = abs(events1 - events2)
    success = diff < 5 and events1 > 0
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    
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
    
    result = send_message("请帮我写一个 Python 函数计算斐波那契数列")
    session_id = result.get('session_id')
    print(f"发送消息结果: {result}")
    
    # 不订阅特定 session，接收所有事件
    await asyncio.sleep(10)
    
    has_session_created = client.has_event_type('session/created', session_id)
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
    
    result = send_message("请帮我写一个 Python 函数计算斐波那契数列")
    session_id = result.get('session_id')
    print(f"发送消息结果: {result}")
    
    # 不订阅特定 session，接收所有事件
    print("等待 title 生成...")
    has_title = await client.wait_for_event('session/title', timeout=60)
    
    print(f"\n收到 session/title: {has_title}")
    
    success = has_title
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    
    await client.disconnect()
    
    return success


async def test_sub_agent_visibility():
    """测试4: 子智能体事件可见"""
    print("\n" + "="*60)
    print("测试4: 子智能体事件可见")
    print("="*60)
    
    client = WebSocketClient("subagent_test")
    await client.connect()
    
    result = send_message("请并行调用两个子智能体向我打招呼")
    session_id = result.get('session_id')
    print(f"发送消息结果: {result}")
    
    # 不订阅特定 session，接收所有事件
    print("等待子智能体执行...")
    has_start = await client.wait_for_event('sub_agent/start', timeout=30)
    has_end = await client.wait_for_event('sub_agent/end', timeout=30)
    has_sub_events = client.has_sub_agent_events(session_id)
    
    print(f"\n收到 sub_agent/start: {has_start}")
    print(f"收到 sub_agent/end: {has_end}")
    print(f"收到子智能体事件: {has_sub_events}")
    
    # 打印子智能体相关事件（来自不同 session 的事件）
    print("\n子智能体相关事件（来自不同 session）:")
    session_ids = set()
    for e in client.events:
        sid = e.get('session_id')
        if sid and sid != session_id:
            session_ids.add(sid[:8])
            print(f"  {e.get('type'):25s} session={sid[:8]}")
    print(f"  涉及的 session: {session_ids}")
    
    success = has_sub_events
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    
    await client.disconnect()
    
    return success


async def test_background_session():
    """测试5: 后台 session 更新（切换 session 后后台继续运行）"""
    print("\n" + "="*60)
    print("测试5: 后台 session 更新")
    print("="*60)
    
    client = WebSocketClient("bg_test")
    await client.connect()
    
    # 创建 session A（长任务）
    result_a = send_message("请详细解释人工智能的各个方面，包括历史、技术、应用等")
    session_a = result_a.get('session_id')
    print(f"Session A: {session_a}")
    
    # 不订阅特定 session，接收所有事件
    # 等待 session A 开始
    await asyncio.sleep(3)
    events_a_before = client.get_event_count()
    print(f"初始事件数: {events_a_before}")
    
    # 模拟切换：同时创建 session B
    result_b = send_message("你好")
    session_b = result_b.get('session_id')
    print(f"Session B: {session_b}")
    
    # 等待两个 session 都完成
    print("等待两个 session 完成...")
    await asyncio.sleep(15)
    
    events_total = client.get_event_count()
    has_turn_end = client.has_event_type('turn/end')
    
    print(f"\n总事件数: {events_total}")
    print(f"收到 turn/end: {has_turn_end}")
    
    success = events_total > events_a_before and has_turn_end
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    
    await client.disconnect()
    
    return success


async def test_abort_and_continue():
    """测试6: Abort 后能继续对话"""
    print("\n" + "="*60)
    print("测试6: Abort 后能继续对话")
    print("="*60)
    
    client = WebSocketClient("abort_test")
    await client.connect()
    
    # 创建 session 并开始长任务
    result = send_message("请详细解释人工智能的各个方面，包括历史、技术、应用、伦理等")
    session_id = result.get('session_id')
    print(f"Session: {session_id}")
    
    # 不订阅特定 session，接收所有事件
    # 等待一段时间
    await asyncio.sleep(3)
    
    # 记录 abort 前的事件数
    events_before_abort = client.get_event_count()
    print(f"Abort 前事件数: {events_before_abort}")
    
    # Abort
    abort_result = abort_session(session_id)
    print(f"Abort 结果: {abort_result}")
    
    # 等待 abort 生效
    await asyncio.sleep(2)
    
    has_cancel = client.has_event_type('turn/cancel') or client.has_event_type('turn/end')
    events_after_abort = client.get_event_count()
    print(f"收到 cancel/end 事件: {has_cancel}")
    print(f"Abort 后事件数: {events_after_abort}")
    
    # 继续对话
    result2 = send_message("请简短回复", session_id)
    print(f"继续对话结果: {result2}")
    
    # 继续对话后，后端会开始新的 turn，但 WebSocket 可能已经断开（因为之前的 session 被 abort）
    # 需要重新连接并订阅新 session
    await asyncio.sleep(1)
    
    # 重新连接 WebSocket
    await client.disconnect()
    await asyncio.sleep(0.5)
    await client.connect()
    
    # 订阅新 session
    await client.subscribe_session(session_id)
    
    # 等待新事件
    print("等待新事件...")
    has_new_end = await client.wait_for_event('turn/end', session_id, timeout=20)
    
    events_after_continue = client.get_event_count()
    has_new_start = client.has_event_type('turn/start', session_id)
    
    print(f"\n继续对话后事件数: {events_after_continue}")
    print(f"收到 turn/start: {has_new_start}")
    print(f"收到 turn/end: {has_new_end}")
    
    # 检查是否有新事件（abort 后继续对话的事件）
    new_events_count = events_after_continue
    print(f"新增事件数: {new_events_count}")
    
    success = has_new_start and has_new_end and new_events_count > 0
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    
    await client.disconnect()
    
    return success


async def test_fork():
    """测试7: Fork 后新 session 事件正确"""
    print("\n" + "="*60)
    print("测试7: Fork 后新 session 事件正确")
    print("="*60)
    
    client = WebSocketClient("fork_test")
    await client.connect()
    
    # 先创建一个有内容的 session
    result1 = send_message("你好，请简短回复")
    session_a = result1.get('session_id')
    print(f"原始 Session: {session_a}")
    
    # 不订阅特定 session，接收所有事件
    # 等待完成
    await client.wait_for_event('turn/end', timeout=15)
    
    # 再发一条消息
    send_message("第二条消息", session_a)
    await asyncio.sleep(5)
    
    events_before = client.get_event_count()
    print(f"原始 Session 事件数: {events_before}")
    
    # Fork（保留 1 条用户消息）
    fork_result = fork_session(session_a, keep=1)
    new_session = fork_result.get('new_session_id')
    print(f"Fork 结果: {fork_result}")
    print(f"新 Session: {new_session}")
    
    if not new_session:
        print("\n结果: ❌ FAIL (fork 失败)")
        await client.disconnect()
        return False
    
    # 在新 session 上继续对话
    result2 = send_message("请简短回复", new_session)
    print(f"新 Session 对话结果: {result2}")
    
    await asyncio.sleep(10)
    
    has_new_turn = client.has_event_type('turn/start')
    has_new_end = client.has_event_type('turn/end')
    
    # 检查新 session 的事件文件
    new_events_data = get_session_events(new_session)
    new_events_count = len(new_events_data.get('events', []))
    
    print(f"\n新 Session 事件:")
    print(f"  turn/start: {has_new_turn}")
    print(f"  turn/end: {has_new_end}")
    print(f"  事件文件中的事件数: {new_events_count}")
    
    success = has_new_turn and has_new_end and new_events_count > 0
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    
    await client.disconnect()
    
    return success


async def test_truncate():
    """测试8: Truncate 后能继续对话"""
    print("\n" + "="*60)
    print("测试8: Truncate 后能继续对话")
    print("="*60)
    
    client = WebSocketClient("truncate_test")
    await client.connect()
    
    # 创建 session 并发送多条消息
    result = send_message("第一条消息，请简短回复")
    session_id = result.get('session_id')
    print(f"Session: {session_id}")
    
    # 不订阅特定 session，接收所有事件
    await client.wait_for_event('turn/end', timeout=15)
    
    send_message("第二条消息，请简短回复", session_id)
    await asyncio.sleep(5)
    
    events_before = client.get_event_count()
    print(f"Truncate 前事件数: {events_before}")
    
    # Truncate（保留 1 条用户消息）
    truncate_result = truncate_session(session_id, keep=1)
    print(f"Truncate 结果: {truncate_result}")
    
    # 继续对话
    client.clear_events()
    result2 = send_message("第三条消息，请简短回复", session_id)
    print(f"继续对话结果: {result2}")
    
    await asyncio.sleep(10)
    
    has_new_turn = client.has_event_type('turn/start')
    has_new_end = client.has_event_type('turn/end')
    new_events = client.get_event_count()
    
    print(f"\nTruncate 后:")
    print(f"  新事件数: {new_events}")
    print(f"  turn/start: {has_new_turn}")
    print(f"  turn/end: {has_new_end}")
    
    success = has_new_turn and has_new_end and new_events > 0
    
    print(f"\n结果: {'✅ PASS' if success else '❌ FAIL'}")
    
    await client.disconnect()
    
    return success


async def main():
    parser = argparse.ArgumentParser(description='WebSocket 连接层重构测试')
    parser.add_argument('--test', choices=[
        'basic', 'event', 'title', 'subagent', 'background', 
        'abort', 'fork', 'truncate', 'all'
    ], default='all', help='运行特定测试')
    args = parser.parse_args()
    
    print("="*60)
    print("WebSocket 连接层重构 - 完整测试")
    print("="*60)
    
    test_map = {
        'basic': ("WebSocket 不会重复连接", test_no_duplicate_connection),
        'event': ("事件能正确接收", test_event_reception),
        'title': ("session/title 事件", test_session_title),
        'subagent': ("子智能体事件可见", test_sub_agent_visibility),
        'background': ("后台 session 更新", test_background_session),
        'abort': ("Abort 后继续对话", test_abort_and_continue),
        'fork': ("Fork 后新 session", test_fork),
        'truncate': ("Truncate 后继续对话", test_truncate),
    }
    
    results = []
    
    if args.test == 'all':
        tests_to_run = list(test_map.keys())
    else:
        tests_to_run = [args.test]
    
    for test_key in tests_to_run:
        name, func = test_map[test_key]
        try:
            result = await func()
            results.append((name, result))
        except Exception as e:
            print(f"\n测试异常: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
    
    # 打印结果
    print("\n" + "="*60)
    print("测试结果汇总")
    print("="*60)
    
    passed = 0
    failed = 0
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} {name}")
        if result:
            passed += 1
        else:
            failed += 1
    
    print(f"\n通过: {passed}")
    print(f"失败: {failed}")
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    import sys
    sys.exit(0 if success else 1)
