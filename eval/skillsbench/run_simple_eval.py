#!/usr/bin/env python3
"""SkillsBench 评测 - no-skill vs human-skill 对比

评测流程：
1. 选择 5 个测试任务
2. 每个任务跑两种模式：no-skill / human-skill
3. 对比成功率和耗时
"""

import asyncio
import docker
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.agent import Agent
from agents.config import load_config
from agents.runtime import set_docker_runtime, set_runtime
from agents.trace import trace_event, set_trace_enabled


# 测试任务（5 个）
TEST_TASKS = [
    "travel-planning",
    "offer-letter-generator",
    "dialogue-parser",
    "citation-check",
    "software-dependency-audit",
]


class SkillsBenchSimpleEval:
    """SkillsBench 简单评测器"""
    
    def __init__(self, model: str = "deepseek-v4-flash"):
        self.model = model
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
        
        # Docker 客户端
        self.docker_client = docker.from_env()
        self.container = None
    
    def build_image(self, task_name: str) -> bool:
        """构建 Docker 镜像"""
        tasks_dir = Path(__file__).parent.parent.parent / "experiments" / "skillsbench" / "tasks"
        task_path = tasks_dir / task_name
        dockerfile = task_path / "environment" / "Dockerfile"
        
        if not dockerfile.exists():
            print(f"警告: Dockerfile 不存在 {dockerfile}")
            return False
        
        image_name = f"skillsbench/{task_name}"
        
        # 检查镜像是否已存在
        try:
            self.docker_client.images.get(image_name)
            print(f"镜像已存在: {image_name}")
            return True
        except docker.errors.ImageNotFound:
            pass
        
        print(f"构建镜像: {image_name}")
        try:
            self.docker_client.images.build(
                path=str(task_path / "environment"),
                tag=image_name,
                rm=True,
            )
            print(f"镜像构建成功: {image_name}")
            return True
        except Exception as e:
            print(f"镜像构建失败: {e}")
            return False
    
    def start_container(self, task_name: str):
        """启动容器"""
        image_name = f"skillsbench/{task_name}"
        
        print(f"启动容器: {image_name}")
        self.container = self.docker_client.containers.run(
            image_name,
            detach=True,
            tty=True,
            command="bash -c 'sleep 3600'",
        )
        print(f"容器已启动: {self.container.id[:12]}")
        
        # 检测容器的工作目录
        workdir = self._detect_workdir()
        
        # 设置 Docker 运行时
        set_docker_runtime(self.container.id[:12], workdir)
    
    def _detect_workdir(self) -> str:
        """检测容器的工作目录"""
        if not self.container:
            return "/app"
        
        # 尝试获取 WORKDIR
        try:
            result = self.container.exec_run("pwd")
            if result.exit_code == 0:
                return result.output.decode().strip()
        except:
            pass
        
        # 默认检查常见目录
        for dir in ["/app", "/root", "/workspace", "/home"]:
            try:
                result = self.container.exec_run(f"test -d {dir} && echo exists")
                if result.exit_code == 0 and "exists" in result.output.decode():
                    return dir
            except:
                continue
        
        return "/app"
    
    def stop_container(self):
        """停止容器"""
        if self.container:
            set_runtime(None)
            self.container.stop()
            self.container.remove()
            self.container = None
    
    def read_task(self, task_name: str) -> dict:
        """读取任务描述和 skill"""
        tasks_dir = Path(__file__).parent.parent.parent / "experiments" / "skillsbench" / "tasks"
        task_path = tasks_dir / task_name
        
        # 读取任务描述
        task_file = task_path / "task.md"
        task_content = task_file.read_text() if task_file.exists() else ""
        
        # 读取 skill 文档
        skills_dir = task_path / "environment" / "skills"
        skills = []
        if skills_dir.exists():
            for skill_dir in skills_dir.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({
                            "name": skill_dir.name,
                            "content": skill_file.read_text(),
                        })
        
        return {
            "name": task_name,
            "task_content": task_content,
            "skills": skills,
        }
    
    async def run_task(self, task_name: str, skill_mode: str = "no-skill") -> dict:
        """运行单个任务"""
        print(f"\n--- 任务: {task_name} | 模式: {skill_mode} ---")
        
        # 构建镜像并启动容器
        if not self.build_image(task_name):
            return {"task": task_name, "skill_mode": skill_mode, "error": "镜像构建失败"}
        
        self.start_container(task_name)
        workdir = self._detect_workdir()
        
        try:
            task = self.read_task(task_name)
            task_content = task["task_content"]
            skills = task["skills"]
            
            # 创建 Agent
            agent = Agent(
                model=self.model,
                api_base=self.api_base,
                api_key=self.api_key,
                permission_mode='bypassPermissions',
                is_sub_agent=False,
            )
            
            # 根据 skill_mode 构建 prompt
            if skill_mode == "no-skill":
                prompt = f"""You are solving a task in a Docker container. The workspace is at {workdir}.

Task description:
{task_content}

Please solve this task step by step. Use available tools as needed."""
            
            elif skill_mode == "human-skill":
                if skills:
                    skill_content = "\n\n".join([s["content"] for s in skills])
                    prompt = f"""You are solving a task in a Docker container. The workspace is at {workdir}.

Task description:
{task_content}

Here is relevant domain knowledge:

{skill_content}

Please solve this task step by step using the provided knowledge."""
                else:
                    prompt = f"""You are solving a task in a Docker container. The workspace is at {workdir}.

Task description:
{task_content}

Please solve this task step by step."""
            
            else:
                raise ValueError(f"Unknown skill_mode: {skill_mode}")
            
            # 运行 Agent
            start_time = time.time()
            result_data = await agent.run_once(prompt)
            duration = time.time() - start_time
            
            # 统计（暂时不跟踪 tool_calls）
            result = {
                "task": task_name,
                "skill_mode": skill_mode,
                "duration": duration,
                "skills_available": len(skills),
            }
            
            print(f"  耗时: {duration:.1f}s")
            
            return result
        
        finally:
            self.stop_container()


async def run_evaluation():
    """运行完整评测"""
    print("="*60)
    print("SkillsBench 评测: no-skill vs human-skill")
    print("="*60)
    
    eval = SkillsBenchSimpleEval(model="deepseek-v4-flash")
    
    results = {
        "no-skill": [],
        "human-skill": [],
    }
    
    # 先跑 no-skill
    print("\n" + "="*60)
    print("模式: no-skill")
    print("="*60)
    
    for task_name in TEST_TASKS:
        result = await eval.run_task(task_name, "no-skill")
        results["no-skill"].append(result)
    
    # 再跑 human-skill
    print("\n" + "="*60)
    print("模式: human-skill")
    print("="*60)
    
    for task_name in TEST_TASKS:
        result = await eval.run_task(task_name, "human-skill")
        results["human-skill"].append(result)
    
    return results


async def main():
    results = await run_evaluation()
    
    # 保存结果
    output_dir = Path("eval/skillsbench")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = output_dir / "simple_eval_results.json"
    output_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    
    print(f"\n{'='*60}")
    print("评测完成")
    print(f"{'='*60}")
    print(f"结果已保存到: {output_file}")
    
    # 生成报告
    report = generate_report(results)
    report_file = output_dir / "simple_eval_report.md"
    report_file.write_text(report)
    print(f"报告已保存到: {report_file}")


def generate_report(results: dict) -> str:
    """生成评测报告"""
    report = []
    report.append("# SkillsBench 评测报告")
    report.append("")
    report.append("## 1. 结果对比")
    report.append("")
    report.append("| 模式 | 平均耗时 | 总任务数 |")
    report.append("|------|----------|----------|")
    
    for mode in ["no-skill", "human-skill"]:
        mode_results = [r for r in results[mode] if "error" not in r]
        if mode_results:
            avg_duration = sum(r["duration"] for r in mode_results) / len(mode_results)
            report.append(f"| {mode} | {avg_duration:.1f}s | {len(mode_results)} |")
        else:
            report.append(f"| {mode} | - | 0 |")
    
    report.append("")
    
    # 详细结果
    report.append("## 2. 详细结果")
    report.append("")
    
    for mode in ["no-skill", "human-skill"]:
        report.append(f"### {mode}")
        report.append("")
        report.append("| 任务 | 耗时 | Skills 可用 |")
        report.append("|------|------|-------------|")
        
        for r in results[mode]:
            if "error" in r:
                report.append(f"| {r['task']} | - | 错误: {r['error']} |")
            else:
                report.append(f"| {r['task']} | {r['duration']:.1f}s | {r['skills_available']} |")
        
        report.append("")
    
    return "\n".join(report)


if __name__ == "__main__":
    asyncio.run(main())
