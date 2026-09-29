#!/usr/bin/env python3
"""
工具并行执行端到端测试
测试：并行读取、编辑、删除等操作，并根据 trace 分析运行是否正常
"""

import json
import sys
import time
import os
import requests
from pathlib import Path
from typing import List, Dict, Any
from dataclasses import dataclass


@dataclass
class SSEEvent:
    """SSE事件"""
    event_type: str
    data: Dict[str, Any]
    raw: str
    timestamp: float


class ToolParallelTest:
    """工具并行执行测试"""
    
    def __init__(self, base_url: str = "http://localhost:5555"):
        self.base_url = base_url
        self.events: List[SSEEvent] = []
        self.session_id: str = None
        self.test_dir = Path("/tmp/mycode_test_parallel")
        self.trace_events: List[Dict] = []
        
    def setup(self):
        """准备测试环境"""
        print("\n=== 准备测试环境 ===")
        self.test_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建测试文件
        for i in range(5):
            f = self.test_dir / f"test_file_{i}.txt"
            f.write_text(f"这是测试文件 {i}\n内容行 1\n内容行 2\n内容行 3\n")
            print(f"  创建: {f}")
        
        print(f"✓ 测试目录: {self.test_dir}")
    
    def cleanup(self):
        """清理测试环境"""
        print("\n=== 清理测试环境 ===")
        import shutil
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
            print(f"✓ 已删除: {self.test_dir}")
    
    def send_message(self, message: str, timeout: int = 60) -> bool:
        """发送消息并收集 SSE 事件"""
        print(f"\n>>> 发送: {message[:80]}...")
        
        try:
            resp = requests.post(
                f"{self.base_url}/api/chat/stream",
                json={
                    "message": message,
                    "agent": "default",
                    "session_id": self.session_id,
                    "context_files": [],
                },
                stream=True,
                timeout=timeout
            )
            
            if resp.status_code != 200:
                print(f"✗ 请求失败: HTTP {resp.status_code}")
                return False
            
            buffer = ""
            start_time = time.time()
            
            for chunk in resp.iter_content(chunk_size=None, decode_unicode=False):
                if chunk:
                    buffer += chunk.decode('utf-8')
                    
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        
                        if not line or not line.startswith('data: '):
                            continue
                            
                        data_str = line[6:]
                        try:
                            data = json.loads(data_str)
                            event_type = self._get_event_type(data)
                            event = SSEEvent(
                                event_type=event_type,
                                data=data,
                                raw=data_str,
                                timestamp=time.time() - start_time
                            )
                            self.events.append(event)
                            
                            if event_type == 'done':
                                self.session_id = data.get('session_id')
                            elif event_type == 'tool_call':
                                tc = data.get('tool_call', {})
                                print(f"  [{event.timestamp:.2f}s] 工具调用: {tc.get('name', '?')}")
                                # 显示工具输入参数
                                inp = tc.get('input', {})
                                if inp:
                                    for key, value in inp.items():
                                        val_str = str(value)
                                        if len(val_str) > 100:
                                            val_str = val_str[:100] + "..."
                                        print(f"      {key}: {val_str}")
                            elif event_type == 'tool_result':
                                tr = data.get('tool_result', {})
                                status = tr.get('status', 'ok')
                                print(f"  [{event.timestamp:.2f}s] 工具结果: {tr.get('name', '?')} ({status})")
                                # 显示工具结果
                                result = tr.get('result', '')
                                if result:
                                    res_str = str(result)
                                    if len(res_str) > 200:
                                        res_str = res_str[:200] + "..."
                                    print(f"      结果: {res_str}")
                            elif event_type == 'error':
                                print(f"  [{event.timestamp:.2f}s] ✗ 错误: {data.get('error', {}).get('message', '?')}")
                                
                        except json.JSONDecodeError:
                            continue
            
            return True
            
        except Exception as e:
            print(f"✗ 请求失败: {e}")
            return False
    
    def _get_event_type(self, data: Dict) -> str:
        if 'thinking' in data: return 'thinking'
        if 'text' in data: return 'text'
        if 'tool_call' in data: return 'tool_call'
        if 'tool_result' in data: return 'tool_result'
        if 'sub_agent_start' in data: return 'sub_agent_start'
        if 'sub_agent_end' in data: return 'sub_agent_end'
        if 'error' in data: return 'error'
        if 'done' in data: return 'done'
        return 'unknown'
    
    def fetch_trace(self) -> List[Dict]:
        """获取 trace 事件"""
        try:
            resp = requests.get(f"{self.base_url}/api/trace?n=200", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get('events', [])
        except Exception as e:
            print(f"  获取 trace 失败: {e}")
        return []
    
    def test_parallel_read(self) -> bool:
        """测试1: 并行读取多个文件"""
        print("\n" + "=" * 60)
        print("测试1: 并行读取多个文件")
        print("=" * 60)
        
        self.events.clear()
        
        files = [f"test_file_{i}.txt" for i in range(5)]
        message = f"请并行读取以下文件并显示内容: {', '.join(files)}，目录是 {self.test_dir}"
        
        if not self.send_message(message):
            return False
        
        # 分析结果
        tool_calls = [e for e in self.events if e.event_type == 'tool_call']
        tool_results = [e for e in self.events if e.event_type == 'tool_result']
        
        read_calls = [e for e in tool_calls if e.data.get('tool_call', {}).get('name') == 'read_file']
        
        print(f"\n  工具调用统计:")
        print(f"    read_file 调用: {len(read_calls)} 次")
        print(f"    总工具调用: {len(tool_calls)} 次")
        print(f"    总工具结果: {len(tool_results)} 次")
        
        # 检查并行执行（通过时间戳分析）
        if len(read_calls) >= 3:
            timestamps = [e.timestamp for e in read_calls]
            time_span = max(timestamps) - min(timestamps) if timestamps else 0
            print(f"    read_file 时间跨度: {time_span:.3f}s")
            
            if time_span < 0.5:
                print(f"  ✓ 并行执行正常（时间跨度 < 0.5s）")
            else:
                print(f"  ⚠ 可能串行执行（时间跨度 > 0.5s）")
        
        # 检查所有结果是否成功
        failed = [e for e in tool_results if e.data.get('tool_result', {}).get('status') not in ('ok', 'success')]
        if failed:
            print(f"  ✗ 有 {len(failed)} 个工具调用失败")
            return False
        
        print(f"  ✓ 所有工具调用成功")
        return True
    
    def test_edit_file(self) -> bool:
        """测试2: 编辑文件"""
        print("\n" + "=" * 60)
        print("测试2: 编辑文件")
        print("=" * 60)
        
        self.events.clear()
        
        target = self.test_dir / "test_file_0.txt"
        message = f"请编辑文件 {target}，将第一行改为'已修改的标题'"
        
        if not self.send_message(message):
            return False
        
        tool_calls = [e for e in self.events if e.event_type == 'tool_call']
        tool_results = [e for e in self.events if e.event_type == 'tool_result']
        
        edit_calls = [e for e in tool_calls if e.data.get('tool_call', {}).get('name') == 'edit_file']
        
        print(f"\n  工具调用统计:")
        print(f"    edit_file 调用: {len(edit_calls)} 次")
        
        # 验证文件确实被修改
        content = target.read_text()
        if "已修改的标题" in content:
            print(f"  ✓ 文件修改成功")
            return True
        else:
            print(f"  ✗ 文件未被修改")
            return False
    
    def test_delete_file(self) -> bool:
        """测试3: 删除文件"""
        print("\n" + "=" * 60)
        print("测试3: 删除文件（测试权限系统）")
        print("=" * 60)
        
        self.events.clear()
        
        target = self.test_dir / "test_file_4.txt"
        message = f"请删除文件 {target}"
        
        if not self.send_message(message):
            return False
        
        tool_calls = [e for e in self.events if e.event_type == 'tool_call']
        tool_results = [e for e in self.events if e.event_type == 'tool_result']
        
        shell_calls = [e for e in tool_calls if e.data.get('tool_call', {}).get('name') == 'run_shell']
        shell_results = [e for e in tool_results if e.data.get('tool_result', {}).get('name') == 'run_shell']
        
        print(f"\n  工具调用统计:")
        print(f"    run_shell 调用: {len(shell_calls)} 次")
        print(f"    run_shell 结果: {len(shell_results)} 次")
        
        # 检查权限系统是否正确拦截
        denied = any(e.data.get('tool_result', {}).get('status') == 'denied' for e in shell_results)
        
        if denied:
            print(f"  ✓ 权限系统正确拦截了 rm 命令")
            print(f"  ℹ 注意: rm 是危险操作，需要用户确认")
            # 手动删除测试文件
            if target.exists():
                target.unlink()
            return True
        elif not target.exists():
            print(f"  ✓ 文件删除成功")
            return True
        else:
            print(f"  ✗ 文件未被删除，且权限系统未拦截")
            return False
    
    def test_mixed_operations(self) -> bool:
        """测试4: 混合操作（读取+编辑+读取）"""
        print("\n" + "=" * 60)
        print("测试4: 混合操作（读取→编辑→读取验证）")
        print("=" * 60)
        
        self.events.clear()
        
        target = self.test_dir / "test_file_1.txt"
        message = f"请读取文件 {target}，然后在末尾添加一行'测试追加内容'，最后再次读取验证修改"
        
        if not self.send_message(message):
            return False
        
        tool_calls = [e for e in self.events if e.event_type == 'tool_call']
        
        read_calls = [e for e in tool_calls if e.data.get('tool_call', {}).get('name') == 'read_file']
        edit_calls = [e for e in tool_calls if e.data.get('tool_call', {}).get('name') == 'edit_file']
        
        print(f"\n  工具调用统计:")
        print(f"    read_file 调用: {len(read_calls)} 次")
        print(f"    edit_file 调用: {len(edit_calls)} 次")
        
        # 验证
        content = target.read_text()
        if "测试追加内容" in content:
            print(f"  ✓ 混合操作成功")
            return True
        else:
            print(f"  ✗ 修改未生效")
            return False
    
    def analyze_trace(self) -> bool:
        """分析 trace 事件"""
        print("\n" + "=" * 60)
        print("Trace 分析")
        print("=" * 60)
        
        self.trace_events = self.fetch_trace()
        
        if not self.trace_events:
            print("  ⚠ 没有 trace 事件")
            return True
        
        # 统计事件类型
        kind_counts: Dict[str, int] = {}
        for e in self.trace_events:
            kind = e.get('kind', 'unknown')
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        
        print(f"\n  Trace 事件统计 (共 {len(self.trace_events)} 条):")
        for kind, count in sorted(kind_counts.items()):
            print(f"    {kind}: {count}")
        
        # 检查关键事件
        tool_starts = kind_counts.get('tool.start', 0)
        tool_ends = kind_counts.get('tool.end', 0)
        stream_calls = kind_counts.get('stream.tool_call', 0)
        stream_results = kind_counts.get('stream.tool_result', 0)
        
        print(f"\n  工具执行追踪:")
        print(f"    tool.start: {tool_starts}")
        print(f"    tool.end: {tool_ends}")
        print(f"    stream.tool_call: {stream_calls}")
        print(f"    stream.tool_result: {stream_results}")
        
        # 检查是否有未完成的工具调用
        if tool_starts > tool_ends:
            print(f"  ⚠ 有 {tool_starts - tool_ends} 个工具调用未完成")
        elif tool_starts == tool_ends and tool_starts > 0:
            print(f"  ✓ 所有工具调用已完成")
        
        # 检查错误
        error_events = [e for e in self.trace_events if 'error' in e.get('kind', '').lower()]
        if error_events:
            print(f"  ✗ 有 {len(error_events)} 个错误事件")
            for e in error_events[:3]:
                print(f"    - {e.get('kind')}: {e.get('error', 'unknown')}")
        else:
            print(f"  ✓ 无错误事件")
        
        # 检查 model 调用
        model_starts = kind_counts.get('model.start', 0)
        model_ends = kind_counts.get('model.end', 0)
        print(f"\n  模型调用追踪:")
        print(f"    model.start: {model_starts}")
        print(f"    model.end: {model_ends}")
        
        # 检查 token 统计
        total_input = sum(e.get('input_tokens', 0) for e in self.trace_events if e.get('kind') == 'model.end')
        total_output = sum(e.get('output_tokens', 0) for e in self.trace_events if e.get('kind') == 'model.end')
        print(f"    总输入 tokens: {total_input}")
        print(f"    总输出 tokens: {total_output}")
        
        return True
    
    def run_all_tests(self) -> int:
        """运行所有测试"""
        print("=" * 60)
        print("工具并行执行端到端测试")
        print("=" * 60)
        
        self.setup()
        
        results = []
        
        try:
            # 测试1: 并行读取
            results.append(("并行读取", self.test_parallel_read()))
            
            # 测试2: 编辑文件
            results.append(("编辑文件", self.test_edit_file()))
            
            # 测试3: 删除文件
            results.append(("删除文件", self.test_delete_file()))
            
            # 测试4: 混合操作
            results.append(("混合操作", self.test_mixed_operations()))
            
            # Trace 分析
            self.analyze_trace()
            
        finally:
            self.cleanup()
        
        # 汇总
        print("\n" + "=" * 60)
        print("测试结果汇总")
        print("=" * 60)
        
        passed = sum(1 for _, r in results if r)
        for name, result in results:
            status = "✓ 通过" if result else "✗ 失败"
            print(f"  {name}: {status}")
        
        print(f"\n  总计: {passed}/{len(results)} 通过")
        
        return 0 if passed == len(results) else 1


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="工具并行执行端到端测试")
    parser.add_argument("--url", default="http://localhost:5555", help="后端URL")
    
    args = parser.parse_args()
    
    test = ToolParallelTest(base_url=args.url)
    exit_code = test.run_all_tests()
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
