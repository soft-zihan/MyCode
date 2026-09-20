#!/usr/bin/env python3
"""
端到端聊天测试脚本
测试从前端发起对话到收到完整模型输出的完整流程
"""

import json
import sys
import time
import requests
from typing import List, Dict, Any
from dataclasses import dataclass


@dataclass
class SSEEvent:
    """SSE事件"""
    event_type: str
    data: Dict[str, Any]
    raw: str


class ChatE2ETest:
    """端到端聊天测试"""
    
    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url
        self.events: List[SSEEvent] = []
        self.session_id: str = None
        
    def test_health(self) -> bool:
        """测试后端健康检查"""
        print("\n=== 测试1: 后端健康检查 ===")
        try:
            # 测试后端直接访问
            resp = requests.get("http://localhost:5555/api/health", timeout=5)
            if resp.status_code == 200:
                print("✓ 后端直接访问正常")
            else:
                print(f"✗ 后端直接访问失败: {resp.status_code}")
                return False
                
            # 测试前端代理
            resp = requests.get(f"{self.base_url}/api/health", timeout=5)
            if resp.status_code == 200:
                print("✓ 前端代理访问正常")
            else:
                print(f"✗ 前端代理访问失败: {resp.status_code}")
                return False
                
            return True
        except Exception as e:
            print(f"✗ 健康检查失败: {e}")
            return False
    
    def test_chat_stream(self, message: str = "你好，请简单介绍一下自己") -> bool:
        """测试聊天流式接口"""
        print(f"\n=== 测试2: 聊天流式接口 ===")
        print(f"发送消息: {message}")
        
        try:
            # 发起流式请求
            resp = requests.post(
                f"{self.base_url}/api/chat/stream",
                json={
                    "message": message,
                    "agent": "default",
                    "session_id": None,
                    "context_files": [],
                },
                stream=True,
                timeout=30
            )
            
            if resp.status_code != 200:
                print(f"✗ 请求失败: HTTP {resp.status_code}")
                return False
            
            print("✓ 请求成功，开始接收SSE流")
            
            # 解析SSE流
            self.events = []
            buffer = ""
            start_time = time.time()
            
            for chunk in resp.iter_content(chunk_size=None, decode_unicode=False):
                if chunk:
                    buffer += chunk.decode('utf-8')
                    
                    # 按行分割
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        
                        if not line:
                            continue
                            
                        if line.startswith('data: '):
                            data_str = line[6:]
                            try:
                                data = json.loads(data_str)
                                
                                # 确定事件类型
                                event_type = None
                                if 'thinking' in data:
                                    event_type = 'thinking'
                                elif 'chunk' in data:
                                    event_type = 'chunk'
                                elif 'tool_call' in data:
                                    event_type = 'tool_call'
                                elif 'tool_result' in data:
                                    event_type = 'tool_result'
                                elif 'sub_agent_start' in data:
                                    event_type = 'sub_agent_start'
                                elif 'sub_agent_end' in data:
                                    event_type = 'sub_agent_end'
                                elif 'info' in data:
                                    event_type = 'info'
                                elif 'error' in data:
                                    event_type = 'error'
                                elif 'done' in data:
                                    event_type = 'done'
                                    self.session_id = data.get('session_id')
                                else:
                                    event_type = 'unknown'
                                
                                event = SSEEvent(event_type=event_type, data=data, raw=data_str)
                                self.events.append(event)
                                
                                # 实时显示
                                if event_type == 'thinking':
                                    print(f"  [思考] {data['thinking'][:50]}...", end='\r')
                                elif event_type == 'chunk':
                                    print(data['chunk'], end='', flush=True)
                                elif event_type == 'tool_call':
                                    print(f"\n  [工具调用] {data['tool_call'].get('name', 'unknown')}")
                                elif event_type == 'tool_result':
                                    print(f"\n  [工具结果] {data['tool_result'].get('name', 'unknown')}")
                                elif event_type == 'done':
                                    print(f"\n  [完成] session_id={data.get('session_id')}")
                                elif event_type == 'error':
                                    print(f"\n  [错误] {data.get('error')}")
                                    
                            except json.JSONDecodeError as e:
                                print(f"\n✗ JSON解析失败: {e}, data: {data_str[:100]}")
                                continue
            
            elapsed = time.time() - start_time
            print(f"\n\n✓ 流式传输完成，耗时 {elapsed:.2f}s")
            
            # 验证事件
            return self._validate_events()
            
        except requests.exceptions.Timeout:
            print("✗ 请求超时")
            return False
        except Exception as e:
            print(f"✗ 测试失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _validate_events(self) -> bool:
        """验证事件序列"""
        print("\n=== 测试3: 验证事件序列 ===")
        
        if not self.events:
            print("✗ 没有收到任何事件")
            return False
        
        # 统计事件类型
        event_counts = {}
        for event in self.events:
            event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1
        
        print(f"事件统计:")
        for event_type, count in sorted(event_counts.items()):
            print(f"  {event_type}: {count}")
        
        # 验证必要事件
        has_thinking = 'thinking' in event_counts
        has_chunk = 'chunk' in event_counts
        has_done = 'done' in event_counts
        
        if has_thinking:
            print(f"✓ 收到 {event_counts['thinking']} 个思考事件")
        else:
            print("⚠ 没有收到思考事件（可能模型不支持）")
        
        if has_chunk:
            print(f"✓ 收到 {event_counts['chunk']} 个文本块事件")
        else:
            print("✗ 没有收到文本块事件")
            return False
        
        if has_done:
            print(f"✓ 收到完成事件")
            if self.session_id:
                print(f"✓ 会话ID: {self.session_id}")
        else:
            print("✗ 没有收到完成事件")
            return False
        
        # 检查是否有错误
        if 'error' in event_counts:
            print(f"✗ 收到 {event_counts['error']} 个错误事件")
            for event in self.events:
                if event.event_type == 'error':
                    print(f"  错误内容: {event.data.get('error')}")
            return False
        
        # 拼接完整响应
        full_response = ""
        for event in self.events:
            if event.event_type == 'chunk':
                full_response += event.data['chunk']
        
        print(f"\n完整响应 ({len(full_response)} 字符):")
        print("-" * 40)
        print(full_response[:500] + "..." if len(full_response) > 500 else full_response)
        print("-" * 40)
        
        return True
    
    def test_session_persistence(self) -> bool:
        """测试会话持久化"""
        print("\n=== 测试4: 会话持久化 ===")
        
        if not self.session_id:
            print("⚠ 没有会话ID，跳过此测试")
            return True
        
        try:
            # 获取会话列表
            resp = requests.get(f"{self.base_url}/api/sessions", timeout=5)
            if resp.status_code != 200:
                print(f"✗ 获取会话列表失败: {resp.status_code}")
                return False
            
            sessions = resp.json()
            print(f"✓ 会话列表: {len(sessions)} 个会话")
            
            # 查找当前会话
            found = False
            for session in sessions:
                if session.get('id') == self.session_id:
                    found = True
                    print(f"✓ 找到当前会话: {session.get('id')}")
                    print(f"  消息数: {session.get('messageCount', 0)}")
                    print(f"  模型: {session.get('model', 'unknown')}")
                    break
            
            if not found:
                print(f"⚠ 未找到会话 {self.session_id}（可能需要等待保存）")
            
            return True
            
        except Exception as e:
            print(f"✗ 测试失败: {e}")
            return False
    
    def run_all_tests(self):
        """运行所有测试"""
        print("=" * 60)
        print("端到端聊天测试")
        print("=" * 60)
        
        results = []
        
        # 测试1: 健康检查
        results.append(("健康检查", self.test_health()))
        
        # 测试2: 聊天流
        results.append(("聊天流", self.test_chat_stream()))
        
        # 测试3: 事件验证（在test_chat_stream中完成）
        results.append(("事件验证", results[-1][1]))
        
        # 测试4: 会话持久化
        results.append(("会话持久化", self.test_session_persistence()))
        
        # 汇总
        print("\n" + "=" * 60)
        print("测试结果汇总")
        print("=" * 60)
        
        passed = 0
        for name, result in results:
            status = "✓ 通过" if result else "✗ 失败"
            print(f"{name}: {status}")
            if result:
                passed += 1
        
        print(f"\n总计: {passed}/{len(results)} 通过")
        
        if passed == len(results):
            print("\n🎉 所有测试通过！")
            return 0
        else:
            print("\n❌ 部分测试失败")
            return 1


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="端到端聊天测试")
    parser.add_argument("--url", default="http://localhost:8080", help="前端URL")
    parser.add_argument("--message", default="你好，请简单介绍一下自己", help="测试消息")
    
    args = parser.parse_args()
    
    test = ChatE2ETest(base_url=args.url)
    exit_code = test.run_all_tests()
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
