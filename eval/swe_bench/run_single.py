#!/usr/bin/env python3
"""SWE-bench 评测脚本 - Agent 在宿主机运行，通过 Docker API 与 testbed 容器交互。

架构：
- Agent 运行在宿主机
- 通过 Docker API 调用容器执行命令（读写文件、运行测试）
- Trace 在宿主机生成，每次运行独立文件

用法：
    python eval/swe_bench/run_single.py <task_index>
"""

import asyncio
import docker
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.trace import set_trace_enabled, set_trace_session
from agents.agent import Agent
from agents.config import get_endpoint_by_model


class DockerExecutor:
    """通过 Docker API 执行命令的封装。"""
    
    def __init__(self, container):
        self.container = container
    
    def exec(self, cmd: str, workdir: str = "/testbed") -> tuple[int, str]:
        """在容器内执行命令，返回 (exit_code, output)。"""
        result = self.container.exec_run(
            f"bash -c 'cd {workdir} && {cmd}'",
            demux=False,
        )
        return result.exit_code, result.output.decode()
    
    def read_file(self, path: str) -> str:
        """读取容器内文件内容。"""
        exit_code, output = self.exec(f"cat {path}")
        if exit_code != 0:
            raise FileNotFoundError(f"Cannot read {path}: {output}")
        return output
    
    def write_file(self, path: str, content: str) -> None:
        """写入内容到容器内文件。"""
        import base64
        content_b64 = base64.b64encode(content.encode()).decode()
        self.exec(f"echo {content_b64} | base64 -d > {path}")
    
    def get_patch(self) -> str:
        """获取 /testbed 目录的 git diff。"""
        exit_code, output = self.exec("git diff")
        return output if exit_code == 0 else ""
    
    def apply_patch(self, patch: str) -> bool:
        """应用 patch 到 /testbed 目录。"""
        import base64
        patch_b64 = base64.b64encode(patch.encode()).decode()
        self.exec(f"echo {patch_b64} | base64 -d > /tmp/patch.diff")
        exit_code, output = self.exec("git apply /tmp/patch.diff")
        return exit_code == 0
    
    def run_tests(self, tests: list[str], framework: str = "auto") -> tuple[bool, str]:
        """运行测试，返回 (success, output)。"""
        if framework == "auto":
            # 检测是否是 Django 项目
            exit_code, _ = self.exec("ls /testbed/tests/runtests.py 2>/dev/null")
            framework = "django" if exit_code == 0 else "pytest"
        
        if framework == "django":
            # Django 格式: "test_name (module.Class)" -> "module.Class.test_name"
            def convert(t):
                if '(' in t and ')' in t:
                    parts = t.split(' (')
                    method = parts[0].strip()
                    class_path = parts[1].rstrip(')')
                    return f"{class_path}.{method}"
                return t
            
            test_args = " ".join(convert(t) for t in tests)
            exit_code, output = self.exec(
                f"source /opt/miniconda3/bin/activate testbed && cd /testbed/tests && python runtests.py {test_args} -v 2",
                workdir="/"
            )
        else:
            # pytest 格式
            test_args = " ".join(f'"{t}"' for t in tests)
            exit_code, output = self.exec(
                f"source /opt/miniconda3/bin/activate testbed && cd /testbed && python -m pytest {test_args} -v --tb=short",
                workdir="/"
            )
        
        return exit_code == 0, output


async def run_agent_with_docker(executor: DockerExecutor, prompt: str, model: str, container_id: str):
    """在宿主机运行 Agent，通过 DockerRuntime 与容器交互。"""
    endpoint = get_endpoint_by_model(model)
    if not endpoint:
        raise ValueError(f"Model {model} not found in config")
    
    # 设置 Docker 运行时
    from agents.runtime import set_docker_runtime
    set_docker_runtime(container_id, workdir="/testbed", conda_env="testbed")
    
    # 创建 Agent
    agent = Agent(
        model=model,
        api_base=endpoint.base_url,
        api_key=endpoint.api_key,
        permission_mode="bypassPermissions",
        is_sub_agent=True,
    )
    
    await agent.run_once(prompt)


async def run_single_task(task_index: int = 0):
    """运行单个 SWE-bench 任务。"""
    from datasets import load_dataset
    
    # 生成独立 session ID
    session_id = f"swe-bench-task{task_index}-{uuid.uuid4().hex[:8]}"
    set_trace_session(session_id)
    set_trace_enabled(True)
    
    print(f"Trace session: {session_id}")
    print(f"Trace file: ~/.bear-code/trace/{session_id}.jsonl")
    print()
    
    # 加载任务
    print("Loading SWE-bench Lite dataset...")
    dataset = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    
    if task_index >= len(dataset):
        print(f"任务索引 {task_index} 超出范围")
        return
    
    task = dataset[task_index]
    instance_id = task["instance_id"]
    problem_statement = task["problem_statement"]
    test_patch = task["test_patch"]
    fail_to_pass = json.loads(task["FAIL_TO_PASS"]) if isinstance(task["FAIL_TO_PASS"], str) else task["FAIL_TO_PASS"]
    pass_to_pass = json.loads(task["PASS_TO_PASS"]) if isinstance(task["PASS_TO_PASS"], str) else task["PASS_TO_PASS"]
    
    print(f"\n{'='*60}")
    print(f"任务 {task_index}: {instance_id}")
    print(f"Problem: {problem_statement[:200]}...")
    print(f"FAIL_TO_PASS: {fail_to_pass[:3]}")
    print(f"{'='*60}")
    
    # 获取 Docker 镜像
    client = docker.from_env()
    parts = instance_id.split("__")
    org, repo_and_issue = parts
    image_name = f"swebench/sweb.eval.x86_64.{org}_1776_{repo_and_issue}"
    
    try:
        client.images.get(image_name)
        print(f"镜像已存在: {image_name}")
    except docker.errors.ImageNotFound:
        print(f"镜像不存在: {image_name}")
        return
    
    # 启动容器
    container = client.containers.run(
        image_name,
        platform="linux/amd64",
        detach=True,
        tty=True,
        command="bash",
    )
    
    executor = DockerExecutor(container)
    model = "deepseek-v4-flash"
    
    try:
        # 构建 prompt
        prompt = f"""You are debugging a real GitHub issue. Please fix the following problem:

{problem_statement}

IMPORTANT:
1. First, understand the problem by reading relevant code in /testbed
2. Locate the bug or missing feature
3. Implement a fix by editing the source code
4. Make sure your changes are minimal and focused
5. Do NOT modify test files
6. Do NOT run any tests - the evaluation system will automatically run tests to verify your fix

Your ONLY job is to fix the source code."""
        
        # 运行 Agent（在宿主机）
        print("\n开始运行 Agent（宿主机）...")
        start_time = time.time()
        
        await run_agent_with_docker(executor, prompt, model, container.id)
        
        agent_duration = time.time() - start_time
        
        # 获取 Agent 的 patch
        agent_patch = executor.get_patch()
        
        if not agent_patch.strip():
            print(f"\n❌ Agent 没有产生任何修改! Agent 耗时: {agent_duration:.1f}s")
            return
        
        print(f"\nAgent patch ({len(agent_patch)} bytes):")
        print(agent_patch[:500])
        print(f"\nAgent 耗时: {agent_duration:.1f}s")
        
        # 评测阶段
        eval_start = time.time()
        print(f"\n{'='*60}")
        print("评测阶段：应用 test_patch 并运行测试...")
        print(f"{'='*60}")
        
        # 应用 test_patch
        if not executor.apply_patch(test_patch):
            print("❌ 应用 test_patch 失败")
            return
        print("应用 test_patch: 成功")
        
        # 运行测试
        all_tests = fail_to_pass + pass_to_pass
        print(f"\n运行测试 ({len(all_tests)} 个)...")
        test_success, test_output = executor.run_tests(all_tests)
        
        eval_duration = time.time() - eval_start
        total_duration = time.time() - start_time
        
        print(f"\n{'='*60}")
        if test_success:
            print(f"✅ 成功!")
        else:
            print(f"❌ 测试失败!")
        print(f"Agent 耗时: {agent_duration:.1f}s | 评测耗时: {eval_duration:.1f}s | 总耗时: {total_duration:.1f}s")
        print(f"{'='*60}")
        
        # 打印测试输出最后几行
        test_lines = test_output.strip().split("\n")
        print(f"\n测试输出 (最后 10 行):")
        for line in test_lines[-10:]:
            print(f"  {line}")
        
    finally:
        container.stop()
        container.remove()


if __name__ == "__main__":
    task_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    asyncio.run(run_single_task(task_index))
