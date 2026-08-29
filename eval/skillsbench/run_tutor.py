#!/usr/bin/env python3
"""SkillsBench 评测 - 导师循环模式

设计思路：
1. Round 1: Agent 尝试任务（无提示）
2. Round 2: 读取 skill，给 Agent 提示
3. Round 3: 根据失败测试，给具体提示
4. 检查 Agent 是否提取了 skill
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


class SkillsBenchTutor:
    """导师循环评测器"""
    
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
    
    def build_image(self, task_name: str) -> bool:
        """构建 Docker 镜像"""
        tasks_dir = Path(__file__).parent.parent.parent / "experiments" / "skillsbench" / "tasks"
        task_path = tasks_dir / task_name
        dockerfile = task_path / "environment" / "Dockerfile"
        
        if not dockerfile.exists():
            print(f"错误: Dockerfile 不存在 {dockerfile}")
            return False
        
        image_name = f"skillsbench/{task_name}"
        
        print(f"构建镜像: {image_name}")
        try:
            self.client.images.build(
                path=str(task_path / "environment"),
                tag=image_name,
                rm=True,
            )
            print(f"镜像构建成功: {image_name}")
            return True
        except Exception as e:
            print(f"镜像构建失败: {e}")
            return False
    
    def start_container(self, image_name: str):
        """启动容器"""
        print(f"启动容器: {image_name}")
        self.container = self.client.containers.run(
            image_name,
            detach=True,
            tty=True,
            command="bash -c 'sleep 3600'",
        )
        print(f"容器已启动: {self.container.id[:12]}")
        
        # 设置 Docker 运行时
        set_docker_runtime(self.container.id[:12], "/workspace")
    
    def stop_container(self):
        """停止容器"""
        if self.container:
            set_runtime(None)
            self.container.stop()
            self.container.remove()
            self.container = None
    
    def run_verifier(self, task_name: str) -> dict:
        """运行验证器"""
        tasks_dir = Path(__file__).parent.parent.parent / "experiments" / "skillsbench" / "tasks"
        verifier_path = tasks_dir / task_name / "verifier"
        
        # 复制 verifier 到容器
        with open(verifier_path / "test_outputs.py", "rb") as f:
            self.container.put_archive("/tmp", f.read())
        
        # 运行测试
        result = self.container.exec_run("python /tmp/test_outputs.py")
        output = result.output.decode()
        
        # 解析 reward
        import re
        match = re.search(r'REWARD=([0-9.]+)', output)
        reward = float(match.group(1)) if match else 0.0
        
        return {
            "reward": reward,
            "output": output,
            "success": reward >= 1.0,
        }
    
    def read_skills(self, task_name: str) -> list[dict]:
        """读取任务提供的 skill"""
        tasks_dir = Path(__file__).parent.parent.parent / "experiments" / "skillsbench" / "tasks"
        skills_dir = tasks_dir / task_name / "environment" / "skills"
        
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
        
        return skills
    
    def generate_hint(self, round_num: int, verifier_output: str, skills: list[dict]) -> str:
        """根据轮次和失败情况生成提示"""
        if round_num == 1:
            # Round 1 失败后，给 skill 提示
            if skills:
                skill_names = ", ".join(s["name"] for s in skills)
                return f"""你之前的尝试没有完全解决问题。

这里有几个相关的 skill 可能对你有帮助：
- {skill_names}

请查看相关文档，重新分析问题。"""
            else:
                return "你之前的尝试没有完全解决问题。请重新检查代码。"
        
        elif round_num == 2:
            # Round 2 失败后，给具体提示
            if "selective_log_softmax" in verifier_output:
                return """Bug 1 的测试失败了。检查 trl/trainer/utils.py 中的 selective_log_softmax 函数。

提示：它应该等价于 F.log_softmax(logits, dim=-1).gather(...),但更高效。"""
            elif "advantage" in verifier_output:
                return """Bug 2 的测试失败了。检查 trl/trainer/grpo_trainer.py 中的 advantage scaling。

提示：epsilon 应该是一个很小的值（如 1e-4），用于数值稳定性。"""
            elif "decode_and_strip_padding" in verifier_output:
                return """Bug 3 的测试失败了。检查 trl/trainer/utils.py 中的 decode_and_strip_padding 函数。

提示：它需要正确处理 <think> 标签。"""
            else:
                return "测试仍然失败。请仔细检查 verifier 的输出，定位问题。"
        
        else:
            return "请继续尝试修复问题。"
    
    async def run_tutor_loop(self, task_name: str, max_rounds: int = 3) -> dict:
        """运行导师循环"""
        print(f"\n{'='*60}")
        print(f"任务: {task_name}")
        print(f"{'='*60}")
        
        # 构建镜像
        image_name = f"skillsbench/{task_name}"
        if not self.build_image(task_name):
            return {"task": task_name, "success": False, "error": "镜像构建失败"}
        
        # 启动容器
        self.start_container(image_name)
        
        try:
            # 读取任务描述
            tasks_dir = Path(__file__).parent.parent.parent / "experiments" / "skillsbench" / "tasks"
            task_file = tasks_dir / task_name / "task.md"
            task_content = task_file.read_text()
            
            # 读取 skill
            skills = self.read_skills(task_name)
            print(f"找到 {len(skills)} 个 skill: {[s['name'] for s in skills]}")
            
            # 创建 Agent
            agent = Agent(
                model=self.model,
                api_base=self.api_base,
                api_key=self.api_key,
                permission_mode='bypassPermissions',
                is_sub_agent=True,
            )
            
            results = []
            
            for round_num in range(1, max_rounds + 1):
                print(f"\n--- Round {round_num} ---")
                
                # 构建 prompt
                if round_num == 1:
                    prompt = f"""You are debugging a real GitHub issue.

The code is at /workspace. Your task:

{task_content}

IMPORTANT:
1. First, understand the problem by reading relevant code
2. Locate the bug or missing feature
3. Implement a fix
4. Test your fix if possible
5. Make sure your changes are minimal and focused

Do NOT modify test files. Only fix the source code."""
                else:
                    # 运行 verifier 检查当前状态
                    verifier_result = self.run_verifier(task_name)
                    print(f"Verifier reward: {verifier_result['reward']}")
                    
                    if verifier_result['success']:
                        print(f"✅ 任务完成!")
                        results.append({
                            "round": round_num,
                            "reward": verifier_result['reward'],
                            "success": True,
                        })
                        break
                    
                    # 生成提示
                    hint = self.generate_hint(round_num - 1, verifier_result['output'], skills)
                    prompt = hint
                
                # 运行 Agent
                trace_event("eval.round.start", task=task_name, round=round_num)
                start_time = time.time()
                
                await agent.run_once(prompt)
                
                duration = time.time() - start_time
                
                # 运行 verifier
                verifier_result = self.run_verifier(task_name)
                print(f"Verifier reward: {verifier_result['reward']}")
                
                results.append({
                    "round": round_num,
                    "reward": verifier_result['reward'],
                    "duration": duration,
                    "success": verifier_result['success'],
                })
                
                if verifier_result['success']:
                    print(f"✅ Round {round_num} 成功!")
                    break
            
            # 检查 Agent 是否提取了 skill
            skill_evolution_dir = Path.cwd() / ".bear" / "skill-evolution"
            extracted_skills = []
            if skill_evolution_dir.exists():
                skills_subdir = skill_evolution_dir / "skills"
                if skills_subdir.exists():
                    for skill_file in skills_subdir.glob("*.md"):
                        extracted_skills.append({
                            "name": skill_file.stem,
                            "size": skill_file.stat().st_size,
                        })
            
            return {
                "task": task_name,
                "rounds": results,
                "final_reward": results[-1]['reward'] if results else 0.0,
                "success": results[-1]['success'] if results else False,
                "extracted_skills": extracted_skills,
            }
        
        finally:
            self.stop_container()


async def main():
    task_name = sys.argv[1] if len(sys.argv) > 1 else "debug-trl-grpo"
    
    tutor = SkillsBenchTutor(model="deepseek-v4-flash")
    result = await tutor.run_tutor_loop(task_name)
    
    print(f"\n{'='*60}")
    print("评测结果")
    print(f"{'='*60}")
    print(f"任务: {result['task']}")
    print(f"最终 reward: {result['final_reward']}")
    print(f"成功: {'✅' if result['success'] else '❌'}")
    print(f"提取的 skill: {len(result['extracted_skills'])} 个")
    
    if result['extracted_skills']:
        for skill in result['extracted_skills']:
            print(f"  - {skill['name']} ({skill['size']} bytes)")
    
    # 保存结果
    output_file = Path(f"eval/skillsbench/{task_name}_tutor_result.json")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n结果已保存到: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
