#!/usr/bin/env python3
"""
Web API 自动化测试脚本
用于测试前端后端的流式聊天功能
"""

import requests
import json
import time
from typing import Generator

BASE_URL = "http://localhost:5555"

def test_stream_chat(message: str, session_id: str = None) -> Generator[dict, None, None]:
    """测试流式聊天API"""
    url = f"{BASE_URL}/api/chat/stream"
    
    payload = {
        "message": message,
        "session_id": session_id,
        "context_files": [],
        "agent": "",
        "model": ""
    }
    
    print(f"\n{'='*60}")
    print(f"发送消息: {message}")
    print(f"Session ID: {session_id or '新建'}")
    print(f"{'='*60}\n")
    
    response = requests.post(url, json=payload, stream=True)
    
    if response.status_code != 200:
        print(f"❌ 错误: HTTP {response.status_code}")
        return
    
    # 解析SSE流
    for line in response.iter_lines():
        if line:
            line_str = line.decode('utf-8')
            if line_str.startswith('data: '):
                data_str = line_str[6:]  # 去掉 'data: '
                try:
                    data = json.loads(data_str)
                    yield data
                    
                    # 实时显示输出
                    if 'thinking' in data:
                        print(f"[思考] {data['thinking'][:100]}...", end='')
                    elif 'chunk' in data:
                        print(data['chunk'], end='', flush=True)
                    elif 'tool_call' in data:
                        tool = data['tool_call']
                        print(f"\n\n🔧 调用工具: {tool['name']}")
                        print(f"   参数: {json.dumps(tool['input'], ensure_ascii=False)[:200]}")
                    elif 'tool_result' in data:
                        tool = data['tool_result']
                        result_preview = tool['result'][:200] if len(tool['result']) > 200 else tool['result']
                        print(f"\n✅ 工具结果: {tool['name']}")
                        print(f"   {result_preview}...")
                    elif 'sub_agent_start' in data:
                        agent = data['sub_agent_start']
                        print(f"\n🤖 启动子Agent: {agent['agent_type']} - {agent['description']}")
                    elif 'sub_agent_end' in data:
                        agent = data['sub_agent_end']
                        print(f"\n✓ 子Agent完成: {agent['agent_type']}")
                    elif 'info' in data:
                        print(f"\nℹ️ 信息: {data['info']}")
                    elif 'done' in data:
                        print(f"\n\n{'='*60}")
                        print(f"✅ 完成! Session ID: {data.get('session_id', 'N/A')}")
                        print(f"{'='*60}\n")
                except json.JSONDecodeError as e:
                    print(f"\n⚠️ JSON解析错误: {e}")
                    print(f"   原始数据: {data_str[:100]}")

def test_session_save():
    """测试session是否正确保存"""
    print("\n" + "="*60)
    print("测试 Session 保存功能")
    print("="*60)
    
    # 发送第一条消息
    session_id = None
    for data in test_stream_chat("你好，请简单介绍一下你自己"):
        if 'done' in data:
            session_id = data.get('session_id')
    
    if not session_id:
        print("❌ 未获取到 session_id")
        return
    
    print(f"\n获取到 session_id: {session_id}")
    
    # 等待一下确保session保存完成
    time.sleep(1)
    
    # 查询session列表
    response = requests.get(f"{BASE_URL}/api/sessions")
    if response.status_code == 200:
        sessions = response.json()
        print(f"\n当前 session 列表 ({len(sessions)} 个):")
        for s in sessions[:5]:  # 只显示前5个
            print(f"  - {s.get('id', 'N/A')}: {s.get('name', '未命名')}")
        
        # 检查我们的session是否在列表中
        found = any(s.get('id') == session_id for s in sessions)
        if found:
            print(f"\n✅ Session {session_id} 已成功保存")
        else:
            print(f"\n❌ Session {session_id} 未找到")
    else:
        print(f"❌ 获取session列表失败: HTTP {response.status_code}")
    
    # 测试继续对话
    print(f"\n{'='*60}")
    print("测试继续对话")
    print(f"{'='*60}")
    
    for data in test_stream_chat("刚才你说了什么？", session_id):
        pass
    
    return session_id

def test_tool_calls():
    """测试工具调用显示"""
    print("\n" + "="*60)
    print("测试工具调用功能")
    print("="*60)
    
    # 发送需要工具调用的消息
    for data in test_stream_chat("请读取 README.md 文件的前10行"):
        pass

def test_thinking():
    """测试thinking内容显示"""
    print("\n" + "="*60)
    print("测试 Thinking 功能")
    print("="*60)
    
    # 发送需要思考的消息
    for data in test_stream_chat("分析一下这个项目的主要技术栈"):
        pass

if __name__ == "__main__":
    print("🚀 开始 Web API 自动化测试\n")
    
    # 测试1: Session保存
    test_session_save()
    
    # 测试2: 工具调用
    test_tool_calls()
    
    # 测试3: Thinking
    test_thinking()
    
    print("\n✅ 所有测试完成!")
