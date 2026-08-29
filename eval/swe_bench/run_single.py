#!/usr/bin/env python3
"""运行单个 SWE-bench 任务 - 正确的评测流程：
1. Agent 修复代码（在 bear 环境）
2. 应用 test_patch（添加测试用例）
3. 用 testbed 环境跑 FAIL_TO_PASS / PASS_TO_PASS 测试
4. 判断是否通过
"""

import asyncio
import docker
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.trace import set_trace_enabled


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
    test_patch = task["test_patch"]
    fail_to_pass = json.loads(task["FAIL_TO_PASS"]) if isinstance(task["FAIL_TO_PASS"], str) else task["FAIL_TO_PASS"]
    pass_to_pass = json.loads(task["PASS_TO_PASS"]) if isinstance(task["PASS_TO_PASS"], str) else task["PASS_TO_PASS"]
    
    print(f"\n{'='*60}")
    print(f"任务 {task_index}: {instance_id}")
    print(f"Problem: {problem_statement[:200]}...")
    print(f"FAIL_TO_PASS: {fail_to_pass[:3]}")
    print(f"PASS_TO_PASS: {pass_to_pass[:3]}")
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
                "bash -c 'source /opt/miniconda3/bin/activate bear && pip install openai rich -q'"
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
        
        # Agent 脚本 - 在 bear 环境运行
        agent_script = f'''import sys
import asyncio
import json
import os
sys.path.insert(0, '/workspace')

os.chdir('/testbed')

from agents.trace import set_trace_enabled
set_trace_enabled(True)

from agents.agent import Agent

async def main():
    config_path = os.path.expanduser('~/.bear-code/config.json')
    with open(config_path) as f:
        config = json.load(f)
    
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
3. Implement a fix by editing the source code
4. Make sure your changes are minimal and focused
5. Do NOT modify test files
6. Do NOT run any tests or verification scripts - the evaluation system will automatically run tests to verify your fix
7. Do NOT install any dependencies - they are already available in the test environment

Your ONLY job is to fix the source code. The testing and verification will be handled automatically."""

    await agent.run_once(prompt)
    print("Agent completed")

asyncio.run(main())
'''
        
        # 写入脚本
        import base64
        script_b64 = base64.b64encode(agent_script.encode()).decode()
        container.exec_run(f"bash -c 'echo {script_b64} | base64 -d > /tmp/agent_run.py'")
        
        # 运行 Agent
        print("\n开始运行 Agent...")
        result = container.exec_run(
            "bash -c 'source /opt/miniconda3/bin/activate bear && cd /testbed && python /tmp/agent_run.py'",
            stream=False,
        )
        
        output = result.output.decode()
        print(f"\nAgent output (last 500 chars):\n{output[-500:]}")
        
        # 获取 Agent 的 patch
        patch_result = container.exec_run("bash -c 'cd /testbed && git diff'")
        agent_patch = patch_result.output.decode()
        
        agent_duration = time.time() - start_time
        
        if not agent_patch.strip():
            print(f"\n❌ Agent 没有产生任何修改! Agent 耗时: {agent_duration:.1f}s")
            return
        
        print(f"\nAgent patch ({len(agent_patch)} bytes):")
        print(agent_patch[:500])
        print(f"\nAgent 耗时: {agent_duration:.1f}s")
        
        # ─── 评测阶段：应用 test_patch 并用 testbed 环境跑测试 ───
        eval_start = time.time()
        print(f"\n{'='*60}")
        print("评测阶段：应用 test_patch 并运行测试...")
        print(f"{'='*60}")
        
        # 应用 test_patch
        import base64
        test_patch_b64 = base64.b64encode(test_patch.encode()).decode()
        container.exec_run(f"bash -c 'echo {test_patch_b64} | base64 -d > /tmp/test_patch.diff'")
        
        apply_result = container.exec_run(
            "bash -c 'cd /testbed && git apply /tmp/test_patch.diff 2>&1'"
        )
        print(f"应用 test_patch: exit_code={apply_result.exit_code}")
        if apply_result.exit_code != 0:
            print(f"  错误: {apply_result.output.decode()[:500]}")
        
        # 用 testbed 环境跑测试
        all_tests = fail_to_pass + pass_to_pass
        
        # 检测是否是 Django 项目（使用 runtests.py）
        check_result = container.exec_run("bash -c 'ls /testbed/tests/runtests.py 2>/dev/null'")
        is_django = check_result.exit_code == 0
        
        if is_django:
            # Django 测试格式: "test_name (module.Class)" -> "module.Class.test_name"
            def convert_django_test(test_name):
                if '(' in test_name and ')' in test_name:
                    parts = test_name.split(' (')
                    method = parts[0].strip()
                    class_path = parts[1].rstrip(')')
                    return f"{class_path}.{method}"
                return test_name
            
            converted_tests = [convert_django_test(t) for t in all_tests]
            test_args = " ".join(converted_tests)
            test_cmd = f"bash -c 'source /opt/miniconda3/bin/activate testbed && cd /testbed/tests && python runtests.py {test_args} -v 2 2>&1'"
        else:
            # pytest 格式
            test_args = " ".join(f'"{t}"' for t in all_tests)
            test_cmd = f'bash -c \'source /opt/miniconda3/bin/activate testbed && cd /testbed && python -m pytest {test_args} -v --tb=short 2>&1\''
        
        print(f"\n运行测试 ({len(all_tests)} 个)...")
        test_result = container.exec_run(test_cmd)
        
        test_output = test_result.output.decode()
        test_success = test_result.exit_code == 0
        
        # 解析测试结果
        if is_django:
            # Django 格式: "Ran X tests" + "OK" or "FAILED"
            import re
            match = re.search(r'Ran (\d+) tests?', test_output)
            passed = int(match.group(1)) if match else 0
            failed = 0 if "OK" in test_output else passed - (int(re.search(r'failures=(\d+)', test_output).group(1)) if re.search(r'failures=(\d+)', test_output) else 0)
        else:
            # pytest 格式
            passed = test_output.count(" PASSED")
            failed = test_output.count(" FAILED")
        
        eval_duration = time.time() - eval_start
        total_duration = time.time() - start_time
        
        print(f"\n{'='*60}")
        if test_success:
            print(f"✅ 成功!")
            print(f"Agent 耗时: {agent_duration:.1f}s | 评测耗时: {eval_duration:.1f}s | 总耗时: {total_duration:.1f}s")
            print(f"测试: {passed} passed, {failed} failed")
        else:
            print(f"❌ 测试失败!")
            print(f"Agent 耗时: {agent_duration:.1f}s | 评测耗时: {eval_duration:.1f}s | 总耗时: {total_duration:.1f}s")
            print(f"测试: {passed} passed, {failed} failed")
            # 打印失败的测试
            for line in test_output.split("\n"):
                if "FAILED" in line or "ERROR" in line:
                    print(f"  {line.strip()}")
        print(f"{'='*60}")
        
        # 打印测试输出最后几行
        test_lines = test_output.strip().split("\n")
        print(f"\n测试输出 (最后 10 行):")
        for line in test_lines[-10:]:
            print(f"  {line}")
        
    except Exception as e:
        duration = time.time() - start_time
        print(f"\n❌ 异常! 耗时: {duration:.1f}s")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        if container:
            container.stop()
            container.remove()


if __name__ == "__main__":
    task_index = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    asyncio.run(run_single_task(task_index))
