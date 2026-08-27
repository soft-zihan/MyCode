#!/usr/bin/env python3
"""SWE-bench Lite 评测运行器 - Phase 0.1 基础能力验证"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.agent import Agent
from agents.trace import trace_event, set_trace_enabled, trace_path
from agents.config import load_config


class SWEBenchRunner:
    """SWE-bench Lite 评测运行器"""

    def __init__(self, model: str = "deepseek-v4-flash"):
        self.model = model
        self.workspace = Path(__file__).parent.parent.parent / "experiments" / "swe_bench_workspace"
        self.workspace.mkdir(exist_ok=True)

        # 启用 trace
        set_trace_enabled(True)

        # 加载配置
        self.config = load_config()
        # 找到指定模型的端点
        self.api_key = None
        self.api_base = None
        for endpoint_id, endpoint in self.config.endpoints.items():
            if endpoint.model == model:
                self.api_key = endpoint.api_key
                self.api_base = endpoint.base_url
                break

        if not self.api_key:
            raise ValueError(f"Model {model} not found in config")

    async def run_task(self, task: dict) -> dict:
        """运行单个 SWE-bench 任务"""
        instance_id = task["instance_id"]
        repo = task["repo"]
        base_commit = task["base_commit"]
        problem_statement = task["problem_statement"]

        print(f"\n{'='*60}")
        print(f"Task: {instance_id}")
        print(f"Repo: {repo}")
        print(f"{'='*60}")

        # 记录任务开始
        trace_event(
            "eval.task.start",
            instance_id=instance_id,
            repo=repo,
            model=self.model,
        )

        start_time = time.time()

        # 克隆并切换到指定 commit
        repo_dir = self.workspace / repo.replace("/", "__")
        if not repo_dir.exists():
            print(f"Cloning {repo}...")
            subprocess.run(
                ["git", "clone", f"https://github.com/{repo}.git", str(repo_dir)],
                check=True,
                capture_output=True,
            )

        # 切换到指定 commit
        try:
            subprocess.run(
                ["git", "checkout", base_commit],
                cwd=repo_dir,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            # 如果 checkout 失败，尝试 fetch 后再 checkout
            print(f"Fetching {base_commit}...")
            subprocess.run(
                ["git", "fetch", "origin", base_commit],
                cwd=repo_dir,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", base_commit],
                cwd=repo_dir,
                check=True,
                capture_output=True,
            )

        # 保存当前目录并切换到仓库目录
        original_cwd = os.getcwd()
        os.chdir(repo_dir)

        # 创建 Agent
        agent = Agent(
            model=self.model,
            api_base=self.api_base,
            api_key=self.api_key,
            permission_mode="bypassPermissions",  # 评测模式，跳过权限确认
        )

        # 构建 prompt
        prompt = f"""You are debugging a real GitHub issue. Please fix the following problem:

{problem_statement}

IMPORTANT:
1. First, understand the problem by reading relevant code
2. Locate the bug or missing feature
3. Implement a fix
4. Test your fix if possible
5. Make sure your changes are minimal and focused

Do NOT modify test files. Only fix the source code."""

        # 运行 Agent
        print(f"Running agent...")
        try:
            await agent.run_once(prompt)
            result = "Agent completed"
            success = True
            error = None
        except Exception as e:
            result = str(e)
            success = False
            error = str(e)
            print(f"Error: {e}")
        finally:
            # 恢复原始目录
            os.chdir(original_cwd)
            # 清理 Agent 资源，避免后台任务异常
            try:
                agent.abort()
            except Exception:
                pass

        end_time = time.time()
        duration = end_time - start_time

        # 生成 patch
        patch = self._generate_patch(repo_dir, base_commit)

        # 记录任务结束
        trace_event(
            "eval.task.end",
            instance_id=instance_id,
            repo=repo,
            success=success,
            duration=duration,
            patch_size=len(patch) if patch else 0,
            error=error,
        )

        return {
            "instance_id": instance_id,
            "repo": repo,
            "success": success,
            "duration": duration,
            "patch": patch,
            "error": error,
            "agent_result": result[:500] if isinstance(result, str) else str(result)[:500],
        }

    def _generate_patch(self, repo_dir: Path, base_commit: str) -> str:
        """生成 git diff patch"""
        try:
            result = subprocess.run(
                ["git", "diff", base_commit],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except Exception as e:
            return f"Error generating patch: {e}"

    async def run_batch(self, tasks: list[dict]) -> list[dict]:
        """批量运行任务"""
        results = []
        for i, task in enumerate(tasks, 1):
            print(f"\n[{i}/{len(tasks)}] Running task...")
            result = await self.run_task(task)
            results.append(result)

            # 保存中间结果
            self._save_checkpoint(results)

        return results

    def _save_checkpoint(self, results: list[dict]):
        """保存检查点"""
        checkpoint_path = Path(__file__).parent / "checkpoint.json"
        checkpoint_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))


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
            "patch": item["patch"],
            "test_patch": item["test_patch"],
            "FAIL_TO_PASS": item["FAIL_TO_PASS"],
            "PASS_TO_PASS": item["PASS_TO_PASS"],
        })

    print(f"Selected {len(tasks)} tasks:")
    for i, task in enumerate(tasks, 1):
        print(f"  {i}. {task['instance_id']} ({task['repo']})")

    # 运行评测
    runner = SWEBenchRunner(model="deepseek-v4-flash")
    results = await runner.run_batch(tasks)

    # 生成报告
    report = {
        "timestamp": datetime.now().isoformat(),
        "model": "deepseek-v4-flash",
        "benchmark": "SWE-bench Lite",
        "total_tasks": len(tasks),
        "results": results,
        "summary": {
            "success_count": sum(1 for r in results if r["success"]),
            "failure_count": sum(1 for r in results if not r["success"]),
            "avg_duration": sum(r["duration"] for r in results) / len(results),
        },
    }

    report_path = Path(__file__).parent.parent / "reports" / "swe_bench_phase0.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print(f"\n{'='*60}")
    print(f"Evaluation complete!")
    print(f"Success: {report['summary']['success_count']}/{len(tasks)}")
    print(f"Avg duration: {report['summary']['avg_duration']:.1f}s")
    print(f"Report: {report_path}")
    print(f"Trace: {trace_path()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
