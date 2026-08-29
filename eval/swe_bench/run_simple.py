#!/usr/bin/env python3
"""SWE-bench 评测 - 使用运行时抽象层"""

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
from agents.trace import trace_event, set_trace_enabled, trace_path


class SWEBenchRunner:
    def __init__(self, model: str = "deepseek-v4-flash"):
        self.model = model
        self.client = docker.from_env()
        self.container = None
        
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
        """启动容器，保持运行"""
        print(f"启动容器: {image_name}")
        self.container = self.client.containers.run(
            image_name,
            platform="linux/amd64",
            detach=True,
            tty=True,
            command="bash -c 'sleep 3600'",
        )
        print(f"容器已启动: {self.container.id[:12]}")
        
        # 设置 Docker 运行时
        set_docker_runtime(self.container.id[:12], "/testbed")

    def stop_container(self):
        if self.container:
            # 恢复本地运行时
            set_runtime(None)
            self.container.stop()
            self.container.remove()
            self.container = None

    def get_patch(self) -> str:
        """获取容器内的 git diff"""
        result = self.container.exec_run("bash -c 'cd /testbed && git diff'")
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

        # 获取镜像
        image_name = self.get_image_name(instance_id)
        if not self.ensure_image(image_name):
            return {"instance_id": instance_id, "success": False, "duration": time.time() - start_time, "patch": "", "error": "镜像获取失败"}

        # 启动容器（会自动设置 Docker 运行时）
        self.start_container(image_name)

        try:
            # 创建 Agent，在宿主机跑
            agent = Agent(
                model=self.model,
                api_base=self.api_base,
                api_key=self.api_key,
                permission_mode='bypassPermissions',
                is_sub_agent=True,
            )

            # 构建 prompt - 不需要提容器，工具会自动适配
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

            # 运行 Agent
            await agent.run_once(prompt)

            # 获取 patch
            patch = self.get_patch()

            success = len(patch.strip()) > 0
            error = None if success else "No changes made"

        except Exception as e:
            success = False
            patch = ""
            error = str(e)
            print(f"Error: {e}")
        finally:
            self.stop_container()

        duration = time.time() - start_time
        trace_event("eval.task.end", instance_id=instance_id, success=success, duration=duration, error=error)

        return {
            "instance_id": instance_id,
            "success": success,
            "duration": duration,
            "patch": patch,
            "error": error,
        }


async def main():
    from datasets import load_dataset
    
    task_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    
    print("Loading SWE-bench Lite dataset...")
    dataset = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    
    if task_index >= len(dataset):
        print(f"任务索引 {task_index} 超出范围")
        return
    
    task = dataset[task_index]
    task_dict = {
        "instance_id": task["instance_id"],
        "repo": task["repo"],
        "base_commit": task["base_commit"],
        "problem_statement": task["problem_statement"],
    }

    runner = SWEBenchRunner(model="deepseek-v4-flash")
    result = await runner.run_task(task_dict)

    print(f"\n{'='*60}")
    if result["success"]:
        print(f"✅ 成功! 耗时: {result['duration']:.1f}s")
        print(f"Patch size: {len(result['patch'])} bytes")
        print(f"Patch preview:\n{result['patch'][:500]}")
    else:
        print(f"❌ 失败! 耗时: {result['duration']:.1f}s")
        print(f"Error: {result['error']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
