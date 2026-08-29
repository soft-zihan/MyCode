#!/usr/bin/env python3
"""SWE-bench 批量评测 - 运行多个任务并汇总结果"""

import asyncio
import docker
import json
import sys
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.agent import Agent
from agents.config import load_config
from agents.runtime import set_docker_runtime, set_runtime
from agents.trace import trace_event, set_trace_enabled


class SWEBenchBatchRunner:
    def __init__(self, model: str = "deepseek-v4-flash"):
        self.model = model
        self.client = docker.from_env()
        
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

    def get_image_name(self, instance_id: str) -> str:
        parts = instance_id.split("__")
        org, repo_and_issue = parts
        return f"swebench/sweb.eval.x86_64.{org}_1776_{repo_and_issue}"

    def ensure_image(self, image_name: str) -> bool:
        try:
            self.client.images.get(image_name)
            return True
        except docker.errors.ImageNotFound:
            print(f"拉取镜像: {image_name}")
            try:
                self.client.images.pull(image_name, platform="linux/amd64")
                return True
            except Exception as e:
                print(f"镜像拉取失败: {e}")
                return False

    def start_container(self, image_name: str):
        print(f"启动容器: {image_name}")
        container = self.client.containers.run(
            image_name,
            platform="linux/amd64",
            detach=True,
            tty=True,
            command="bash -c 'sleep 3600'",
        )
        print(f"容器已启动: {container.id[:12]}")
        set_docker_runtime(container.id[:12], "/testbed")
        return container

    def stop_container(self, container):
        if container:
            set_runtime(None)
            container.stop()
            container.remove()

    def get_patch(self, container) -> str:
        result = container.exec_run("bash -c 'cd /testbed && git diff'")
        return result.output.decode()

    async def run_task(self, task: dict) -> dict:
        instance_id = task["instance_id"]
        problem_statement = task["problem_statement"]
        
        print(f"\n{'='*60}")
        print(f"任务: {instance_id}")
        print(f"Problem: {problem_statement[:200]}...")
        print(f"{'='*60}")

        trace_event("eval.task.start", instance_id=instance_id, model=self.model)
        start_time = time.time()

        image_name = self.get_image_name(instance_id)
        if not self.ensure_image(image_name):
            return {
                "instance_id": instance_id,
                "success": False,
                "duration": time.time() - start_time,
                "patch": "",
                "error": "镜像获取失败",
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }

        container = self.start_container(image_name)

        try:
            agent = Agent(
                model=self.model,
                api_base=self.api_base,
                api_key=self.api_key,
                permission_mode='bypassPermissions',
                is_sub_agent=True,
            )

            prompt = f"""You are debugging a real GitHub issue.

The code is at /testbed. Your task:

Problem:
{problem_statement}

IMPORTANT:
1. First, understand the problem by reading relevant code
2. Locate the bug or missing feature
3. Implement a fix
4. Test your fix if possible (use the testbed environment)
5. Make sure your changes are minimal and focused

Do NOT modify test files. Only fix the source code."""

            await agent.run_once(prompt)

            patch = self.get_patch(container)
            success = len(patch.strip()) > 0
            error = None if success else "No changes made"

        except Exception as e:
            success = False
            patch = ""
            error = str(e)
            print(f"Error: {e}")
        finally:
            self.stop_container(container)

        duration = time.time() - start_time
        
        # 收集 token 信息
        input_tokens = getattr(agent, 'total_input_tokens', 0) if 'agent' in dir() else 0
        output_tokens = getattr(agent, 'total_output_tokens', 0) if 'agent' in dir() else 0
        tool_calls = getattr(agent, 'current_turns', 0) if 'agent' in dir() else 0
        
        trace_event(
            "eval.task.end",
            instance_id=instance_id,
            success=success,
            duration=duration,
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
        )

        return {
            "instance_id": instance_id,
            "success": success,
            "duration": duration,
            "patch": patch,
            "error": error,
            "tool_calls": tool_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }


async def main():
    from datasets import load_dataset
    
    # 解析参数：起始索引和任务数量
    start_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    
    print(f"Loading SWE-bench Lite dataset...")
    dataset = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    
    # 选择任务
    task_indices = list(range(start_index, start_index + count))
    print(f"将运行任务索引: {task_indices}")
    
    runner = SWEBenchBatchRunner(model="deepseek-v4-flash")
    results = []
    
    for idx in task_indices:
        if idx >= len(dataset):
            print(f"任务索引 {idx} 超出范围，跳过")
            continue
        
        task = dataset[idx]
        task_dict = {
            "instance_id": task["instance_id"],
            "repo": task["repo"],
            "base_commit": task["base_commit"],
            "problem_statement": task["problem_statement"],
        }
        
        result = await runner.run_task(task_dict)
        results.append(result)
        
        # 保存中间结果
        output_file = Path(__file__).parent / f"results_batch_{start_index}_{count}.json"
        output_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    
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
    
    # 保存最终结果
    output_file = Path(__file__).parent / f"results_batch_{start_index}_{count}.json"
    output_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n结果已保存到: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
