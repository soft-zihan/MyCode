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

        # 每个任务使用独立的仓库目录
        repo_dir = self.workspace / f"{repo.replace('/', '__')}_{instance_id}"
        
        # 清理已存在的目录
        if repo_dir.exists():
            import shutil
            shutil.rmtree(repo_dir)

        print(f"Cloning {repo} at {base_commit[:8]}...")
        for attempt in range(3):
            try:
                subprocess.run(
                    ["git", "clone", "--depth=1", f"https://github.com/{repo}.git", str(repo_dir)],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                if attempt < 2:
                    print(f"  Clone attempt {attempt+1} failed, retrying...")
                    if repo_dir.exists():
                        import shutil
                        shutil.rmtree(repo_dir)
                    import time as _time
                    _time.sleep(2)
                else:
                    raise

        # 获取指定 commit
        subprocess.run(
            ["git", "fetch", "--depth=1", "origin", base_commit],
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

        # 运行 Agent（带重试）
        print(f"Running agent...")
        max_retries = 3
        result = None
        success = False
        error = None

        for attempt in range(max_retries):
            try:
                # 每次重试创建新的 Agent
                agent = Agent(
                    model=self.model,
                    api_base=self.api_base,
                    api_key=self.api_key,
                    permission_mode="bypassPermissions",
                    is_sub_agent=True,  # 抑制 UI 输出（spinner/thinking 动画）
                )
                await agent.run_once(prompt)
                result = "Agent completed"
                success = True
                error = None
                break
            except Exception as e:
                error_msg = str(e)
                print(f"Attempt {attempt + 1}/{max_retries} failed: {error_msg[:100]}")

                # 记录错误
                trace_event(
                    "error",
                    error_type="agent_error",
                    error_message=error_msg[:300],
                    attempt=attempt + 1,
                    instance_id=instance_id,
                )

                # 如果是网络错误，等待后重试
                if "Broken pipe" in error_msg or "Connection" in error_msg or "timeout" in error_msg.lower():
                    wait_time = 5 * (attempt + 1)
                    print(f"Network error, waiting {wait_time}s before retry...")
                    await asyncio.sleep(wait_time)
                else:
                    # 非网络错误，不重试
                    break
            finally:
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

        # 恢复原始目录
        os.chdir(original_cwd)

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
        total = len(tasks)
        
        for i, task in enumerate(tasks, 1):
            task_start = time.time()
            print(f"\n{'='*60}")
            print(f"[{i}/{total}] 开始任务: {task['instance_id']}")
            print(f"{'='*60}")
            
            try:
                result = await asyncio.wait_for(
                    self.run_task(task),
                    timeout=1800  # 每个任务最多 30 分钟
                )
                results.append(result)
                
                # 立即保存 checkpoint
                self._save_checkpoint(results)
                
                # 打印进度
                task_duration = time.time() - task_start
                status = "✅ 成功" if result["success"] else "❌ 失败"
                print(f"\n[{i}/{total}] {status} | 耗时: {task_duration:.1f}s | Patch: {len(result.get('patch', ''))} bytes")
                
                # 打印汇总
                success_count = sum(1 for r in results if r["success"])
                print(f"进度: {success_count}/{i} 成功 | 平均耗时: {sum(r['duration'] for r in results)/i:.1f}s")
                
            except asyncio.TimeoutError:
                print(f"\n[{i}/{total}] ⏰ 超时（超过10分钟）")
                results.append({
                    "instance_id": task["instance_id"],
                    "repo": task["repo"],
                    "success": False,
                    "duration": 600,
                    "patch": "",
                    "error": "Task timeout (10 minutes)",
                    "agent_result": "Timeout",
                })
                self._save_checkpoint(results)
            except Exception as e:
                print(f"\n[{i}/{total}] 💥 异常: {str(e)[:100]}")
                results.append({
                    "instance_id": task["instance_id"],
                    "repo": task["repo"],
                    "success": False,
                    "duration": time.time() - task_start,
                    "patch": "",
                    "error": str(e),
                    "agent_result": str(e),
                })
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
    
    try:
        results = await runner.run_batch(tasks)
    except Exception as e:
        print(f"\n💥 评测中断: {e}")
        # 从 checkpoint 恢复结果
        checkpoint_path = Path(__file__).parent / "checkpoint.json"
        if checkpoint_path.exists():
            results = json.loads(checkpoint_path.read_text())
            print(f"从 checkpoint 恢复 {len(results)} 个任务结果")
        else:
            results = []

    # 生成报告
    report = {
        "timestamp": datetime.now().isoformat(),
        "model": "deepseek-v4-flash",
        "benchmark": "SWE-bench Lite",
        "total_tasks": len(tasks),
        "completed_tasks": len(results),
        "results": results,
        "summary": {
            "success_count": sum(1 for r in results if r["success"]),
            "failure_count": sum(1 for r in results if not r["success"]),
            "avg_duration": sum(r["duration"] for r in results) / len(results) if results else 0,
            "total_duration": sum(r["duration"] for r in results),
        },
    }

    report_path = Path(__file__).parent.parent / "reports" / "swe_bench_phase0.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print(f"\n{'='*60}")
    print(f"评测完成!")
    print(f"成功: {report['summary']['success_count']}/{len(results)}")
    print(f"平均耗时: {report['summary']['avg_duration']:.1f}s")
    print(f"总耗时: {report['summary']['total_duration']:.1f}s")
    print(f"报告: {report_path}")
    print(f"Trace: {trace_path()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
