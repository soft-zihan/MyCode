#!/usr/bin/env python3
"""运行单个 SWE-bench 任务"""

import asyncio
import docker
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.trace import trace_event, set_trace_enabled
from agents.config import get_endpoint_by_model


async def run_single_task(task_index: int = 0):
    """运行单个任务"""
    from datasets import load_dataset
    
    set_trace_enabled(True)
    
    print("Loading SWE-bench Lite dataset...")
    dataset = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    
    if task_index >= len(dataset):
        print(f"任务索引 {task_index} 超出范围")
        return
    
    task = dataset[task_index]
    instance_id = task["instance_id"]
    problem_statement = task["problem_statement"]
    
    print(f"\n{'='*60}")
    print(f"任务 {task_index}: {instance_id}")
    print(f"Problem: {problem_statement[:200]}...")
    print(f"{'='*60}")
    
    client = docker.from_env()
    model = "deepseek-v4-flash"
    
    # 获取镜像名
    parts = instance_id.split("__")
    org, repo_and_issue = parts
    image_name = f"swebench/sweb.eval.x86_64.{org}_1776_{repo_and_issue}"
    bear_image = image_name + "-bear"
    
    # 检查 bear 镜像
    try:
        client.images.get(bear_image)
        print(f"Bear 镜像已存在: {bear_image}")
    except docker.errors.ImageNotFound:
        print(f"创建 Bear 镜像...")
        # 创建临时容器
        container = client.containers.run(
            image_name,
            platform="linux/amd64",
            detach=True,
            tty=True,
            command="bash",
        )
        try:
            result = container.exec_run(
                "bash -c 'source /opt/miniconda3/bin/activate && conda create -n bear python=3.11 -y -q'"
            )
            if result.exit_code != 0:
                print(f"环境创建失败: {result.output.decode()[:500]}")
                return
            
            result = container.exec_run(
                "bash -c 'source /opt/miniconda3/bin/activate bear && pip install anthropic openai rich tqdm -q'"
            )
            if result.exit_code != 0:
                print(f"依赖安装失败: {result.output.decode()[:500]}")
                return
            
            container.commit(repository=bear_image, tag="latest")
            print(f"Bear 镜像创建成功")
        finally:
            container.stop()
            container.remove()
    
    # 运行 Agent
    container = None
    start_time = time.time()
    
    try:
        project_root = Path(__file__).parent.parent.parent
        
        container = client.containers.run(
            bear_image,
            platform="linux/amd64",
            detach=True,
            tty=True,
            command="bash",
            volumes={
                str(project_root): {'bind': '/workspace', 'mode': 'ro'},
                str(Path.home() / '.bear-code'): {'bind': '/root/.bear-code', 'mode': 'rw'},
            },
        )
        
        # Agent 脚本
        agent_script = f'''import sys
import asyncio
import json
import os
sys.path.insert(0, '/workspace')

# 禁用跨会话记忆注入（测试环境）
os.environ["BEAR_DISABLE_CROSS_SESSION_MEMORY"] = "1"

os.chdir('/testbed')

from agents.agent import Agent

async def main():
    # 直接从配置文件读取 API key
    config_path = os.path.expanduser('~/.bear-code/config.json')
    with open(config_path) as f:
        config = json.load(f)
    
    # 查找模型配置
    endpoint = None
    for ep in config.get('endpoints', {{}}).values():
        if ep.get('model') == '{model}':
            endpoint = ep
            break
    
    if not endpoint:
        print(f"Error: Model '{model}' not found in config")
        return
    
    agent = Agent(
        model='{model}',
        api_base=endpoint['base_url'],
        api_key=endpoint['api_key'],
        permission_mode='bypassPermissions',
        is_sub_agent=True,
    )

    prompt = """You are debugging a real GitHub issue. Please fix the following problem:

{problem_statement}

IMPORTANT:
1. First, understand the problem by reading relevant code in /testbed
2. Locate the bug or missing feature
3. Implement a fix
4. Test your fix if possible
5. Make sure your changes are minimal and focused

Do NOT modify test files. Only fix the source code."""

    await agent.run_once(prompt)
    print("Agent completed")

asyncio.run(main())
'''
        
        # 写入脚本
        import base64
        script_b64 = base64.b64encode(agent_script.encode()).decode()
        container.exec_run(f"bash -c 'echo {script_b64} | base64 -d > /tmp/agent_run.py'")
        
        # 运行
        print("\n开始运行 Agent...")
        result = container.exec_run(
            "bash -c 'source /opt/miniconda3/bin/activate bear && cd /testbed && python /tmp/agent_run.py'",
            stream=False,
        )
        
        output = result.output.decode()
        print(f"\nAgent output:\n{output}")
        
        # 获取 patch
        patch_result = container.exec_run("bash -c 'cd /testbed && git diff'")
        patch = patch_result.output.decode()
        
        duration = time.time() - start_time
        success = len(patch.strip()) > 0
        
        print(f"\n{'='*60}")
        if success:
            print(f"✅ 成功! 耗时: {duration:.1f}s")
            print(f"Patch size: {len(patch)} bytes")
            print(f"Patch preview:\n{patch[:500]}")
        else:
            print(f"❌ 失败! 耗时: {duration:.1f}s")
            print(f"Error: No changes made")
        print(f"{'='*60}")
        
    except Exception as e:
        duration = time.time() - start_time
        print(f"\n❌ 异常! 耗时: {duration:.1f}s")
        print(f"Error: {e}")
        
    finally:
        if container:
            container.stop()
            container.remove()


if __name__ == "__main__":
    task_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    asyncio.run(run_single_task(task_index))
