#!/usr/bin/env python3
"""SkillsBench 单任务评测 - 消融实验"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents.agent import Agent
from agents.config import load_config
from agents.trace import trace_event, set_trace_enabled


class SkillsBenchRunner:
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

    async def run_task(
        self,
        task_name: str,
        *,
        enable_evolution: bool = True,
        timeout: int = 600,
    ) -> dict[str, Any]:
        """运行单个任务"""
        tasks_dir = Path(__file__).parent.parent.parent / "experiments" / "skillsbench" / "tasks"
        task_path = tasks_dir / task_name
        
        if not task_path.exists():
            raise ValueError(f"任务不存在: {task_name}")
        
        # 读取任务描述
        task_file = task_path / "task.md"
        if not task_file.exists():
            raise ValueError(f"任务文件不存在: {task_file}")
        
        task_content = task_file.read_text()
        
        print(f"\n{'='*60}")
        print(f"任务: {task_name}")
        print(f"进化: {'启用' if enable_evolution else '禁用'}")
        print(f"{'='*60}")
        
        # 设置环境变量
        if not enable_evolution:
            os.environ["BEAR_AUTO_SKILL_EVOLUTION"] = "0"
        else:
            os.environ.pop("BEAR_AUTO_SKILL_EVOLUTION", None)
        
        # 创建 Agent
        agent = Agent(
            model=self.model,
            api_base=self.api_base,
            api_key=self.api_key,
        )
        
        trace_event("eval.task.start", task=task_name, enable_evolution=enable_evolution)
        start_time = time.time()
        
        try:
            # 运行任务
            result = await asyncio.wait_for(
                agent.run(task_content),
                timeout=timeout,
            )
            
            duration = time.time() - start_time
            
            # 统计
            tool_calls = len([e for e in agent.trace_events if e.get("type") == "tool_use"])
            
            # 分析 Skill 召回
            skill_recalls = self._analyze_skill_recalls(agent.trace_events)
            
            return {
                "task": task_name,
                "success": result.get("success", False),
                "duration": duration,
                "tool_calls": tool_calls,
                "enable_evolution": enable_evolution,
                "skill_recalls": skill_recalls,
                "output": result,
            }
        
        except asyncio.TimeoutError:
            duration = time.time() - start_time
            return {
                "task": task_name,
                "success": False,
                "duration": duration,
                "tool_calls": 0,
                "enable_evolution": enable_evolution,
                "error": "Timeout",
            }
        
        finally:
            agent.close()

    def _analyze_skill_recalls(self, trace_events: list) -> list[dict]:
        """从 trace 分析 Skill 召回情况"""
        recalls = []
        
        for event in trace_events:
            if event.get("type") == "skill_recall":
                recalls.append({
                    "skill": event.get("skill_name"),
                    "reason": event.get("reason"),
                    "timestamp": event.get("timestamp"),
                })
        
        return recalls


async def run_ablation_single(
    task_name: str,
    output_dir: Path,
) -> dict:
    """运行单任务消融实验"""
    runner = SkillsBenchRunner()
    
    results = {
        "baseline": None,
        "full_system": None,
    }
    
    # Phase 1: baseline (禁用 Skill 进化)
    print("\n" + "="*60)
    print("Phase 1: Baseline (禁用 Skill 进化)")
    print("="*60)
    
    results["baseline"] = await runner.run_task(
        task_name,
        enable_evolution=False,
    )
    
    # 保存 baseline 结果
    baseline_file = output_dir / f"{task_name}_baseline.json"
    baseline_file.write_text(json.dumps(results["baseline"], indent=2, ensure_ascii=False))
    print(f"\nBaseline 结果已保存到: {baseline_file}")
    
    # Phase 2: full_system (启用 Skill 进化)
    print("\n" + "="*60)
    print("Phase 2: Full System (启用 Skill 进化)")
    print("="*60)
    
    results["full_system"] = await runner.run_task(
        task_name,
        enable_evolution=True,
    )
    
    # 保存 full_system 结果
    full_file = output_dir / f"{task_name}_full_system.json"
    full_file.write_text(json.dumps(results["full_system"], indent=2, ensure_ascii=False))
    print(f"\nFull System 结果已保存到: {full_file}")
    
    # 分析 Skill 生成
    skill_analysis = analyze_generated_skills()
    results["skill_analysis"] = skill_analysis
    
    # 保存完整结果
    results_file = output_dir / f"{task_name}_results.json"
    results_file.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    
    return results


def analyze_generated_skills() -> dict:
    """分析生成的 Skill"""
    skill_evolution_dir = Path.cwd() / ".bear" / "skill-evolution"
    
    if not skill_evolution_dir.exists():
        return {"count": 0, "skills": []}
    
    skills = []
    
    # 读取 skill 文件
    skills_dir = skill_evolution_dir / "skills"
    if skills_dir.exists():
        for skill_file in skills_dir.glob("*.md"):
            content = skill_file.read_text()
            skills.append({
                "name": skill_file.stem,
                "path": str(skill_file),
                "size": len(content),
                "has_metadata": "---" in content[:100],
                "has_description": "description:" in content[:500],
                "content_preview": content[:500],
            })
    
    return {
        "count": len(skills),
        "skills": skills,
    }


def generate_comparison_report(results: dict, task_name: str, output_file: Path):
    """生成对比报告"""
    baseline = results.get("baseline", {})
    full_system = results.get("full_system", {})
    skill_analysis = results.get("skill_analysis", {})
    
    report = []
    report.append(f"# SkillsBench 消融实验报告: {task_name}")
    report.append("")
    report.append(f"**日期**: {datetime.now().strftime('%Y-%m-%d')}")
    report.append(f"**模型**: deepseek-v4-flash")
    report.append("")
    
    report.append("## 1. 结果对比")
    report.append("")
    report.append("| 指标 | Baseline (无进化) | Full System (有进化) |")
    report.append("|------|-------------------|----------------------|")
    report.append(f"| 成功率 | {'✅' if baseline.get('success') else '❌'} | {'✅' if full_system.get('success') else '❌'} |")
    report.append(f"| 耗时 | {baseline.get('duration', 0):.1f}s | {full_system.get('duration', 0):.1f}s |")
    report.append(f"| 工具调用 | {baseline.get('tool_calls', 0)} | {full_system.get('tool_calls', 0)} |")
    report.append("")
    
    # Skill 召回分析
    report.append("## 2. Skill 召回分析")
    report.append("")
    
    recalls = full_system.get("skill_recalls", [])
    if recalls:
        report.append(f"共召回 {len(recalls)} 个 Skill:")
        report.append("")
        for r in recalls:
            report.append(f"- **{r['skill']}**: {r.get('reason', 'N/A')}")
    else:
        report.append("未检测到 Skill 召回")
    
    report.append("")
    
    # Skill 生成分析
    report.append("## 3. Skill 生成分析")
    report.append("")
    report.append(f"生成 Skill 数量: {skill_analysis.get('count', 0)}")
    report.append("")
    
    skills = skill_analysis.get("skills", [])
    if skills:
        for skill in skills:
            report.append(f"### {skill['name']}")
            report.append(f"- 大小: {skill['size']} 字节")
            report.append(f"- 有元数据: {'✅' if skill['has_metadata'] else '❌'}")
            report.append(f"- 有描述: {'✅' if skill['has_description'] else '❌'}")
            report.append("")
            report.append("**内容预览**:")
            report.append("```")
            report.append(skill['content_preview'])
            report.append("```")
            report.append("")
    
    # 保存报告
    output_file.write_text("\n".join(report))
    print(f"\n报告已保存到: {output_file}")


async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="SkillsBench 单任务消融实验")
    parser.add_argument(
        "task_name",
        help="任务名称（如 debug-trl-grpo）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/skillsbench"),
        help="输出目录",
    )
    
    args = parser.parse_args()
    
    # 确保输出目录存在
    args.output.mkdir(parents=True, exist_ok=True)
    
    print("="*60)
    print("SkillsBench 单任务消融实验")
    print("="*60)
    print(f"任务: {args.task_name}")
    print(f"输出: {args.output}")
    
    # 运行消融实验
    results = await run_ablation_single(args.task_name, args.output)
    
    # 生成报告
    report_file = args.output / f"{args.task_name}_report.md"
    generate_comparison_report(results, args.task_name, report_file)
    
    print("\n消融实验完成!")


if __name__ == "__main__":
    asyncio.run(main())
