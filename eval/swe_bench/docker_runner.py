#!/usr/bin/env python3
"""SWE-bench Docker 化评测运行器"""

import asyncio
import docker
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.trace import trace_event, set_trace_enabled, trace_path


class SWEBenchDockerRunner:
    """SWE-bench Docker 化评测运行器"""

    # SWE-bench 官方镜像前缀
    IMAGE_PREFIX = "swebench/sweb.eval.x86_64"
    
    def __init__(self, model: str = "deepseek-v4-flash"):
        self.model = model
        self.client = docker.from_env()
        
        # 启用 trace
        set_trace_enabled(True)

    def get_image_name(self, instance_id: str) -> str:
        """获取实例对应的 Docker 镜像名"""
        # instance_id 格式: astropy__astropy-12907
        # 镜像名格式: swebench/sweb.eval.x86_64.astropy_1776_astropy-12907
        parts = instance_id.split("__")
        if len(parts) == 2:
            org, repo_and_issue = parts  # org=astropy, repo_and_issue=astropy-12907
            image_name = f"{self.IMAGE_PREFIX}.{org}_1776_{repo_and_issue}"
            return image_name
        return None

    def ensure_image(self, image_name: str) -> bool:
        """确保镜像存在，不存在则拉取"""
        try:
            self.client.images.get(image_name)
            print(f"镜像已存在: {image_name}")
            return True
        except docker.errors.ImageNotFound:
            print(f"拉取镜像: {image_name}")
            try:
                self.client.images.pull(image_name, platform="linux/amd64")
                print(f"镜像拉取成功: {image_name}")
                return True
            except Exception as e:
                print(f"镜像拉取失败: {e}")
                return False

    def ensure_bear_image(self, base_image: str) -> str:
        """确保包含 bear 环境的镜像存在"""
        import subprocess
        bear_image = base_image + "-bear"
        try:
            self.client.images.get(bear_image)
            print(f"Bear 镜像已存在: {bear_image}")
            return bear_image
        except docker.errors.ImageNotFound:
            print(f"创建 Bear 镜像: {bear_image}")
            
            cid = subprocess.check_output(
                ["docker", "run", "-d", "--platform", "linux/amd64", base_image, "bash", "-c", "sleep 3600"]
            ).decode().strip()
            
            try:
                subprocess.run(
                    ["docker", "exec", cid, "bash", "-c",
                     "source /opt/miniconda3/bin/activate && conda create -n bear python=3.11 -y -q"],
                    check=True, capture_output=True
                )
                subprocess.run(
                    ["docker", "exec", cid, "bash", "-c",
                     "source /opt/miniconda3/bin/activate bear && pip install anthropic openai rich tqdm -q"],
                    check=True, capture_output=True
                )
                
                # 验证
                r = subprocess.run(
                    ["docker", "exec", cid, "bash", "-c",
                     "source /opt/miniconda3/bin/activate bear && python -c 'import tqdm; print(\"OK\")'"],
                    capture_output=True, text=True
                )
                if "OK" not in r.stdout:
                    print(f"环境验证失败: {r.stderr}")
                    return None
                
                subprocess.run(
                    ["docker", "commit", cid, bear_image],
                    check=True, capture_output=True
                )
                print(f"Bear 镜像创建成功: {bear_image}")
                return bear_image
            finally:
                subprocess.run(["docker", "stop", cid], capture_output=True)
                subprocess.run(["docker", "rm", cid], capture_output=True)

    async def run_task(self, task: dict) -> dict:
        """在 Docker 容器内运行单个任务"""
        instance_id = task["instance_id"]
        problem_statement = task["problem_statement"]
        
        print(f"\n{'='*60}")
        print(f"Task: {instance_id}")
        print(f"Problem: {problem_statement[:100]}...")
        print(f"{'='*60}")

        # 记录任务开始
        trace_event(
            "eval.task.start",
            instance_id=instance_id,
            model=self.model,
        )

        start_time = time.time()

        # 获取镜像
        image_name = self.get_image_name(instance_id)
        if not image_name or not self.ensure_image(image_name):
            return {
                "instance_id": instance_id,
                "success": False,
                "duration": time.time() - start_time,
                "patch": "",
                "error": f"Failed to get image for {instance_id}",
            }

        # 获取 bear 镜像
        bear_image = self.ensure_bear_image(image_name)
        if not bear_image:
            return {
                "instance_id": instance_id,
                "success": False,
                "duration": time.time() - start_time,
                "patch": "",
                "error": "Failed to create bear image",
            }

        # 创建容器，挂载本地代码
        container = None
        try:
            # 获取项目根目录
            project_root = Path(__file__).parent.parent.parent
            
            container = self.client.containers.run(
                bear_image,
                platform="linux/amd64",
                detach=True,
                tty=True,
                command="bash",
                volumes={
                    str(project_root): {'bind': '/workspace', 'mode': 'ro'},
                },
            )
            
            # 准备 Agent 运行脚本
            agent_script = '''import sys
import asyncio
sys.path.insert(0, '/workspace')

# 切换到 testbed 目录
import os
os.chdir('/testbed')

# 导入 Agent
from agents.agent import Agent
from agents.trace import trace_event

async def main():
    # 创建 Agent
    agent = Agent(
        model='MODEL_PLACEHOLDER',
        permission_mode='bypassPermissions',
        is_sub_agent=True,
    )

    # 运行 Agent
    prompt = """You are debugging a real GitHub issue. Please fix the following problem:

PROBLEM_PLACEHOLDER

IMPORTANT:
1. First, understand the problem by reading relevant code in /testbed
2. Locate the bug or missing feature
3. Implement a fix
4. Test your fix if possible
5. Make sure your changes are minimal and focused

Do NOT modify test files. Only fix the source code."""

    # 运行
    await agent.run_once(prompt)
    print("Agent completed")

asyncio.run(main())
'''
            
            # 替换占位符
            agent_script = agent_script.replace('MODEL_PLACEHOLDER', self.model)
            agent_script = agent_script.replace('PROBLEM_PLACEHOLDER', problem_statement)
            
            # 写入脚本到容器
            import base64
            script_b64 = base64.b64encode(agent_script.encode()).decode()
            container.exec_run(f"bash -c 'echo {script_b64} | base64 -d > /tmp/agent_run.py'")
            
            # 运行 Agent
            result = container.exec_run(
                "bash -c 'source /opt/miniconda3/bin/activate bear && cd /testbed && python /tmp/agent_run.py'",
                stream=False,
            )
            
            output = result.output.decode()
            print(f"Agent output:\n{output[:500]}")
            
            # 获取 patch
            patch_result = container.exec_run("bash -c 'cd /testbed && git diff'")
            patch = patch_result.output.decode()
            
            success = len(patch.strip()) > 0
            error = None if success else "No changes made"
            
        except Exception as e:
            success = False
            patch = ""
            error = str(e)
            print(f"Error: {e}")
        finally:
            if container:
                container.stop()
                container.remove()

        end_time = time.time()
        duration = end_time - start_time

        # 记录任务结束
        trace_event(
            "eval.task.end",
            instance_id=instance_id,
            success=success,
            duration=duration,
            patch_size=len(patch) if patch else 0,
            error=error,
        )

        return {
            "instance_id": instance_id,
            "success": success,
            "duration": duration,
            "patch": patch,
            "error": error,
        }

    async def run_batch(self, tasks: list[dict]) -> list[dict]:
        """批量运行任务"""
        results = []
        total = len(tasks)
        
        for i, task in enumerate(tasks, 1):
            task_start = time.time()
            print(f"\n{'='*60}")
            print(f"[{i}/{total}] 开始任务: {task['instance_id']}")
            print(f"{'='*60}")
            
            try:
                result = await asyncio.wait_for(
                    self.run_task(task),
                    timeout=1800  # 30 分钟
                )
                results.append(result)
                
                # 打印进度
                task_duration = time.time() - task_start
                status = "✅ 成功" if result["success"] else "❌ 失败"
                print(f"\n[{i}/{total}] {status} | 耗时: {task_duration:.1f}s")
                
            except asyncio.TimeoutError:
                print(f"\n[{i}/{total}] ⏰ 超时（超过30分钟）")
                results.append({
                    "instance_id": task["instance_id"],
                    "success": False,
                    "duration": 1800,
                    "patch": "",
                    "error": "Task timeout (30 minutes)",
                })
            except Exception as e:
                print(f"\n[{i}/{total}] 💥 异常: {str(e)[:100]}")
                results.append({
                    "instance_id": task["instance_id"],
                    "success": False,
                    "duration": time.time() - task_start,
                    "patch": "",
                    "error": str(e),
                })

        return results


async def main():
    """主函数"""
    from datasets import load_dataset

    # 加载 SWE-bench Lite
    print("Loading SWE-bench Lite dataset...")
    dataset = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")

    # 选取前 5 个任务
    tasks = []
    for i in range(min(5, len(dataset))):
        item = dataset[i]
        tasks.append({
            "instance_id": item["instance_id"],
            "repo": item["repo"],
            "base_commit": item["base_commit"],
            "problem_statement": item["problem_statement"],
        })

    print(f"Selected {len(tasks)} tasks:")
    for i, task in enumerate(tasks, 1):
        print(f"  {i}. {task['instance_id']}")

    # 运行评测
    runner = SWEBenchDockerRunner(model="deepseek-v4-flash")
    results = await runner.run_batch(tasks)

    # 生成报告
    report = {
        "timestamp": datetime.now().isoformat(),
        "model": "deepseek-v4-flash",
        "benchmark": "SWE-bench Lite (Docker)",
        "total_tasks": len(tasks),
        "results": results,
        "summary": {
            "success_count": sum(1 for r in results if r["success"]),
            "failure_count": sum(1 for r in results if not r["success"]),
            "avg_duration": sum(r["duration"] for r in results) / len(results) if results else 0,
        },
    }

    report_path = Path(__file__).parent.parent / "reports" / "swe_bench_docker_phase0.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print(f"\n{'='*60}")
    print(f"评测完成!")
    print(f"成功: {report['summary']['success_count']}/{len(results)}")
    print(f"报告: {report_path}")
    print(f"Trace: {trace_path()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
