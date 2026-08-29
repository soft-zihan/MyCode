#!/usr/bin/env python3
"""AgentBench 评测 - 通用 Agent 能力评测"""

import asyncio
import json
import sys
import time
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.agent import Agent
from agents.config import load_config
from agents.trace import trace_event, set_trace_enabled


# AgentBench 任务定义（从 OS + Terminal + DB 环境选取）
AGENTBENCH_TASKS = [
    # OS 环境任务
    {
        "id": "ab_001",
        "env": "OS",
        "task": "查找当前目录下所有 .py 文件",
        "expected": "包含 .py 文件列表",
        "verify": lambda result: ".py" in result.lower() or "file" in result.lower(),
    },
    {
        "id": "ab_002",
        "env": "OS",
        "task": "统计当前目录下文件数量",
        "expected": "包含数字",
        "verify": lambda result: any(c.isdigit() for c in result),
    },
    {
        "id": "ab_003",
        "env": "OS",
        "task": "读取 README.md 前 10 行",
        "expected": "包含 README 内容",
        "verify": lambda result: len(result) > 0,
    },
    {
        "id": "ab_004",
        "env": "OS",
        "task": "创建 test_dir 目录并在其中创建 test.txt 文件，内容为 'Hello AgentBench'",
        "expected": "目录和文件存在",
        "verify": lambda result: "success" in result.lower() or "created" in result.lower(),
    },
    {
        "id": "ab_005",
        "env": "OS",
        "task": "查找包含 'def chat' 的代码行",
        "expected": "包含搜索结果",
        "verify": lambda result: "def" in result.lower() or "chat" in result.lower() or "found" in result.lower(),
    },
    # Terminal 环境任务
    {
        "id": "ab_006",
        "env": "Terminal",
        "task": "运行 echo 'Hello AgentBench'",
        "expected": "输出包含 'Hello AgentBench'",
        "verify": lambda result: "Hello AgentBench" in result,
    },
    {
        "id": "ab_007",
        "env": "Terminal",
        "task": "查看当前工作目录",
        "expected": "输出包含路径",
        "verify": lambda result: "/" in result or "\\" in result,
    },
    {
        "id": "ab_008",
        "env": "Terminal",
        "task": "查看系统信息（uname -a 或 ver）",
        "expected": "输出包含系统信息",
        "verify": lambda result: len(result) > 10,
    },
    # DB 环境任务（简化为文件操作）
    {
        "id": "ab_009",
        "env": "DB",
        "task": "查询当前目录下的 .py 文件数量",
        "expected": "输出包含数字",
        "verify": lambda result: any(c.isdigit() for c in result),
    },
    {
        "id": "ab_010",
        "env": "DB",
        "task": "统计 wiki 目录下的 md 文件数量",
        "expected": "输出包含数字",
        "verify": lambda result: any(c.isdigit() for c in result),
    },
]


class AgentBenchRunner:
    def __init__(self, model: str = "deepseek-v4-flash"):
        self.model = model
        
        # 加载配置
        config = load_config()
        endpoint = None
        for ep in config.endpoints.values():
            if ep.model == model:
                endpoint = ep
                break
        if not endpoint:
            raise ValueError(f"Model {model} not found in config")
        
        self.api_base = endpoint.base_url
        self.api_key = endpoint.api_key
        
        set_trace_enabled(True)
        
        # 创建临时工作目录
        self.work_dir = tempfile.mkdtemp(prefix="agentbench_")
        print(f"工作目录: {self.work_dir}")
        
        # 复制一些测试文件到工作目录
        self._setup_test_files()
    
    def _setup_test_files(self):
        """设置测试文件"""
        work = Path(self.work_dir)
        
        # 创建 README.md
        (work / "README.md").write_text("# Test Project\nThis is a test project for AgentBench.\n" * 10)
        
        # 创建一些 Python 文件
        (work / "main.py").write_text("def main():\n    print('Hello')\n\ndef chat():\n    pass\n")
        (work / "utils.py").write_text("def helper():\n    pass\n")
        (work / "test_main.py").write_text("def test_main():\n    assert True\n")
        
        # 创建 wiki 目录
        wiki = work / "wiki"
        wiki.mkdir()
        (wiki / "intro.md").write_text("# Introduction\n")
        (wiki / "guide.md").write_text("# Guide\n")
        (wiki / "faq.md").write_text("# FAQ\n")
    
    async def run_task(self, task: dict) -> dict:
        task_id = task["id"]
        task_desc = task["task"]
        env = task["env"]
        
        print(f"\n{'='*60}")
        print(f"任务: {task_id} ({env})")
        print(f"Task: {task_desc}")
        print(f"{'='*60}")

        trace_event("eval.task.start", task_id=task_id, env=env, model=self.model)
        start_time = time.time()

        try:
            agent = Agent(
                model=self.model,
                api_base=self.api_base,
                api_key=self.api_key,
                permission_mode='bypassPermissions',
                is_sub_agent=True,
            )

            prompt = f"""You are in directory: {self.work_dir}

Task: {task_desc}

Please complete this task using the available tools (run_shell, read_file, write_file, etc.).
After completing the task, summarize what you did."""

            await agent.run_once(prompt)

            # 收集结果
            result_text = getattr(agent, '_last_assistant_text', '') if 'agent' in dir() else ''
            success = task["verify"](result_text)
            error = None if success else "Verification failed"

        except Exception as e:
            success = False
            result_text = ""
            error = str(e)
            print(f"Error: {e}")

        duration = time.time() - start_time
        
        # 收集 token 信息
        input_tokens = getattr(agent, 'total_input_tokens', 0) if 'agent' in dir() else 0
        output_tokens = getattr(agent, 'total_output_tokens', 0) if 'agent' in dir() else 0
        tool_calls = getattr(agent, 'current_turns', 0) if 'agent' in dir() else 0
        
        trace_event(
            "eval.task.end",
            task_id=task_id,
            env=env,
            success=success,
            duration=duration,
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
        )

        return {
            "task_id": task_id,
            "env": env,
            "task": task_desc,
            "success": success,
            "duration": duration,
            "result_preview": result_text[:200] if result_text else "",
            "error": error,
            "tool_calls": tool_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    
    def cleanup(self):
        """清理临时目录"""
        if Path(self.work_dir).exists():
            shutil.rmtree(self.work_dir)
            print(f"已清理: {self.work_dir}")


async def main():
    # 解析参数：任务索引和数量
    start_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    
    # 选择任务
    task_indices = list(range(start_index, min(start_index + count, len(AGENTBENCH_TASKS))))
    print(f"将运行任务索引: {task_indices}")
    
    runner = AgentBenchRunner(model="deepseek-v4-flash")
    results = []
    
    try:
        for idx in task_indices:
            task = AGENTBENCH_TASKS[idx]
            result = await runner.run_task(task)
            results.append(result)
            
            # 保存中间结果
            output_file = Path(__file__).parent / f"results_batch_{start_index}_{count}.json"
            output_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    finally:
        runner.cleanup()
    
    # 汇总统计
    print(f"\n{'='*60}")
    print("评测汇总")
    print(f"{'='*60}")
    
    total = len(results)
    success_count = sum(1 for r in results if r["success"])
    total_duration = sum(r["duration"] for r in results)
    total_tool_calls = sum(r["tool_calls"] for r in results)
    total_input_tokens = sum(r["input_tokens"] for r in results)
    total_output_tokens = sum(r["output_tokens"] for r in results)
    
    print(f"总任务数: {total}")
    print(f"成功数: {success_count}")
    print(f"成功率: {success_count / total * 100:.1f}%")
    print(f"总耗时: {total_duration:.1f}s")
    print(f"平均耗时: {total_duration / total:.1f}s")
    print(f"总工具调用: {total_tool_calls}")
    print(f"总输入 Token: {total_input_tokens}")
    print(f"总输出 Token: {total_output_tokens}")
    
    # 按环境统计
    env_stats = {}
    for r in results:
        env = r["env"]
        if env not in env_stats:
            env_stats[env] = {"total": 0, "success": 0}
        env_stats[env]["total"] += 1
        if r["success"]:
            env_stats[env]["success"] += 1
    
    print(f"\n按环境统计:")
    for env, stats in env_stats.items():
        rate = stats["success"] / stats["total"] * 100 if stats["total"] > 0 else 0
        print(f"  {env}: {stats['success']}/{stats['total']} ({rate:.1f}%)")
    
    # 保存最终结果
    output_file = Path(__file__).parent / f"results_batch_{start_index}_{count}.json"
    output_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n结果已保存到: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
