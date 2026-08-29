#!/usr/bin/env python3
"""SkillsBench 自进化评测 - 基于论文调研的新方案

评测流程：
1. 训练阶段：在训练集上收集轨迹，BearCode 在线进化机制提取 Skill
2. 测试阶段：对比 no-skill / human-skill / evolved-skill 三种基线

参考论文：
- SkillOpt (Microsoft): 把 Skill 当外部状态训练
- Trace2Skill (Alibaba): 并行分析轨迹，归纳合并
- EvoSkill: 失败分析驱动进化
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


# 训练集任务（5 个）
TRAIN_TASKS = [
    "travel-planning",
    "offer-letter-generator", 
    "dialogue-parser",
    "citation-check",
    "software-dependency-audit",
]

# 测试集任务（5 个）
TEST_TASKS = [
    "debug-trl-grpo",
    "fix-build-agentops",
    "python-scala-translation",
    "lean4-proof",
    "parallel-tfidf-search",
]


class SkillsBenchEvolutionEval:
    """SkillsBench 自进化评测器"""
    
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
        self.trajectories = []
        
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
    
    def read_verifier(self, task_name: str) -> str:
        """读取验证脚本"""
        tasks_dir = Path(__file__).parent.parent.parent / "experiments" / "skillsbench" / "tasks"
        verifier_file = tasks_dir / task_name / "verifier" / "test_outputs.py"
        return verifier_file.read_text() if verifier_file.exists() else ""
    
    async def run_training_phase(self) -> dict:
        """训练阶段：收集轨迹，让 BearCode 自动提取 Skill"""
        print("\n" + "="*60)
        print("训练阶段：收集轨迹")
        print("="*60)
        
        results = []
        
        for task_name in TRAIN_TASKS:
            print(f"\n--- 训练任务: {task_name} ---")
            
            # 构建镜像并启动容器
            if not self.build_image(task_name):
                print(f"跳过任务 {task_name}（镜像构建失败）")
                continue
            
            self.start_container(task_name)
            workdir = self._detect_workdir()
            
            try:
                task = self.read_task(task_name)
                task_content = task["task_content"]
                skills = task["skills"]
                
                # 创建 Agent（启用在线进化，is_sub_agent=False 才能触发）
                agent = Agent(
                    model=self.model,
                    api_base=self.api_base,
                    api_key=self.api_key,
                    permission_mode='bypassPermissions',
                    is_sub_agent=False,  # 必须为 False 才能触发在线进化
                )
                
                # Round 1: 裸跑
                print(f"Round 1: 裸跑...")
                prompt = f"""You are solving a task in a Docker container. The workspace is at {workdir}.

Task description:
{task_content}

Please solve this task step by step. Use available tools as needed."""
                
                start_time = time.time()
                await agent.run_once(prompt)
                # 等待后台 Skill 进化任务完成
                await agent.drain_background_skill_tasks()
                round1_duration = time.time() - start_time
                
                # Round 2: 注入 skill 提示
                if skills:
                    print(f"Round 2: 注入 skill 提示...")
                    skill_content = "\n\n".join([s["content"] for s in skills[:2]])
                    hint = f"""你之前的尝试可能没有完全解决问题。

参考以下领域知识：

{skill_content}

请根据这些知识重新分析问题并修复。"""
                    
                    start_time = time.time()
                    await agent.run_once(hint)
                    # 等待后台 Skill 进化任务完成
                    await agent.drain_background_skill_tasks()
                    round2_duration = time.time() - start_time
                else:
                    round2_duration = 0
                
                # 记录轨迹
                trajectory = {
                    "task": task_name,
                    "round1_duration": round1_duration,
                    "round2_duration": round2_duration,
                    "skills_available": len(skills),
                }
                
                self.trajectories.append(trajectory)
                results.append(trajectory)
                
                print(f"  Round 1: {round1_duration:.1f}s")
                if round2_duration > 0:
                    print(f"  Round 2: {round2_duration:.1f}s")
                print(f"  Skills available: {len(skills)}")
            
            finally:
                self.stop_container()
        
        return {
            "phase": "training",
            "results": results,
            "trajectories_count": len(self.trajectories),
        }
    
    async def run_test_phase(self, skill_mode: str = "no-skill") -> dict:
        """测试阶段：评估不同 skill 模式的效果"""
        print(f"\n" + "="*60)
        print(f"测试阶段：{skill_mode}")
        print("="*60)
        
        results = []
        
        for task_name in TEST_TASKS:
            print(f"\n--- 测试任务: {task_name} ---")
            
            # 构建镜像并启动容器
            if not self.build_image(task_name):
                print(f"跳过任务 {task_name}（镜像构建失败）")
                continue
            
            self.start_container(task_name)
            workdir = self._detect_workdir()
            
            try:
                task = self.read_task(task_name)
                task_content = task["task_content"]
                skills = task["skills"]
                
                # 创建 Agent（is_sub_agent=False 才能触发 Skill 召回）
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

Please solve this task step by step."""
                
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
                
                elif skill_mode == "evolved-skill":
                    evolved_skills = self._load_evolved_skills()
                    if evolved_skills:
                        skill_content = "\n\n".join([s["content"] for s in evolved_skills[:3]])
                        prompt = f"""You are solving a task in a Docker container. The workspace is at {workdir}.

Task description:
{task_content}

Here is relevant knowledge learned from previous tasks:

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
                await agent.run_once(prompt)
                duration = time.time() - start_time
                
                # 统计
                tool_calls = len([e for e in agent.trace_events if e.get("type") == "tool_use"])
                
                result = {
                    "task": task_name,
                    "skill_mode": skill_mode,
                    "duration": duration,
                    "tool_calls": tool_calls,
                }
                
                results.append(result)
                
                print(f"  Duration: {duration:.1f}s")
                print(f"  Tool calls: {tool_calls}")
            
            finally:
                self.stop_container()
        
        return {
            "phase": "testing",
            "skill_mode": skill_mode,
            "results": results,
        }
    
    def _load_evolved_skills(self) -> list[dict]:
        """加载 BearCode 进化出的 Skill"""
        skill_evolution_dir = Path.cwd() / ".bear" / "skill-evolution"
        
        if not skill_evolution_dir.exists():
            return []
        
        skills = []
        skills_dir = skill_evolution_dir / "skills"
        
        if skills_dir.exists():
            for skill_file in skills_dir.glob("*.md"):
                content = skill_file.read_text()
                skills.append({
                    "name": skill_file.stem,
                    "content": content,
                })
        
        return skills
    
    async def run_full_evaluation(self) -> dict:
        """运行完整评测"""
        print("="*60)
        print("SkillsBench 自进化评测")
        print("="*60)
        
        # Phase 1: 训练阶段
        training_results = await self.run_training_phase()
        
        # Phase 2: 测试阶段（三种基线）
        no_skill_results = await self.run_test_phase("no-skill")
        human_skill_results = await self.run_test_phase("human-skill")
        evolved_skill_results = await self.run_test_phase("evolved-skill")
        
        # 汇总结果
        return {
            "training": training_results,
            "testing": {
                "no-skill": no_skill_results,
                "human-skill": human_skill_results,
                "evolved-skill": evolved_skill_results,
            },
            "evolved_skills_count": len(self._load_evolved_skills()),
            "timestamp": time.time(),
        }


async def main():
    eval = SkillsBenchEvolutionEval(model="deepseek-v4-flash")
    results = await eval.run_full_evaluation()
    
    # 保存结果
    output_dir = Path("eval/skillsbench")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = output_dir / "evolution_eval_results.json"
    output_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    
    print(f"\n{'='*60}")
    print("评测完成")
    print(f"{'='*60}")
    print(f"进化 Skill 数量: {results['evolved_skills_count']}")
    print(f"结果已保存到: {output_file}")
    
    # 生成报告
    report = generate_report(results)
    report_file = output_dir / "evolution_eval_report.md"
    report_file.write_text(report)
    print(f"报告已保存到: {report_file}")


def generate_report(results: dict) -> str:
    """生成评测报告"""
    report = []
    report.append("# SkillsBench 自进化评测报告")
    report.append("")
    
    # 训练阶段结果
    report.append("## 1. 训练阶段")
    report.append("")
    report.append(f"训练任务数: {len(results['training']['results'])}")
    report.append(f"收集轨迹数: {results['training']['trajectories_count']}")
    report.append("")
    
    # 测试阶段结果
    report.append("## 2. 测试阶段")
    report.append("")
    report.append("| 基线 | 平均耗时 | 平均工具调用 |")
    report.append("|------|----------|--------------|")
    
    for mode in ["no-skill", "human-skill", "evolved-skill"]:
        mode_results = results["testing"][mode]["results"]
        avg_duration = sum(r["duration"] for r in mode_results) / len(mode_results)
        avg_tools = sum(r["tool_calls"] for r in mode_results) / len(mode_results)
        report.append(f"| {mode} | {avg_duration:.1f}s | {avg_tools:.1f} |")
    
    report.append("")
    
    # 进化 Skill 分析
    report.append("## 3. 进化 Skill 分析")
    report.append("")
    report.append(f"进化 Skill 数量: {results['evolved_skills_count']}")
    report.append("")
    
    return "\n".join(report)


if __name__ == "__main__":
    asyncio.run(main())
