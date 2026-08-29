#!/usr/bin/env python3
"""SkillsBench 评测脚本 - 测试 Skill 自进化与召回能力"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_skillsbench_tasks(task_names: list[str]) -> list[dict]:
    """加载 SkillsBench 任务"""
    tasks_dir = PROJECT_ROOT / "experiments" / "skillsbench" / "tasks"
    tasks = []
    
    for name in task_names:
        task_path = tasks_dir / name
        if not task_path.exists():
            print(f"警告: 任务不存在 {name}")
            continue
        
        task = {
            "name": name,
            "path": task_path,
        }
        
        # 读取任务描述
        readme = task_path / "README.md"
        if readme.exists():
            task["description"] = readme.read_text()
        
        tasks.append(task)
    
    return tasks


def run_task(
    task: dict,
    *,
    enable_evolution: bool = True,
    enable_recall: bool = True,
    timeout: int = 600,
) -> dict[str, Any]:
    """运行单个任务"""
    task_name = task["name"]
    task_path = task["path"]
    
    print(f"\n{'='*60}")
    print(f"运行任务: {task_name}")
    print(f"进化: {'启用' if enable_evolution else '禁用'}")
    print(f"召回: {'启用' if enable_recall else '禁用'}")
    print(f"{'='*60}")
    
    # 设置环境变量
    env = os.environ.copy()
    if not enable_evolution:
        env["BEAR_AUTO_SKILL_EVOLUTION"] = "0"
    
    # 构建命令
    cmd = [
        sys.executable,
        "-m", "agents.main",
        "--task", str(task_path),
        "--no-interactive",
    ]
    
    start_time = time.time()
    
    try:
        result = subprocess.run(
            cmd,
            env=env,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        
        duration = time.time() - start_time
        
        # 解析输出
        output = result.stdout
        success = result.returncode == 0
        
        # 统计工具调用
        tool_calls = output.count("tool_use")
        
        return {
            "task": task_name,
            "success": success,
            "duration": duration,
            "tool_calls": tool_calls,
            "exit_code": result.returncode,
            "error": result.stderr if not success else None,
            "enable_evolution": enable_evolution,
            "enable_recall": enable_recall,
        }
    
    except subprocess.TimeoutExpired:
        duration = time.time() - start_time
        return {
            "task": task_name,
            "success": False,
            "duration": duration,
            "tool_calls": 0,
            "exit_code": -1,
            "error": "Timeout",
            "enable_evolution": enable_evolution,
            "enable_recall": enable_recall,
        }


def analyze_skills(output_dir: Path) -> dict:
    """分析生成的 Skill"""
    skill_evolution_dir = output_dir / ".bear" / "skill-evolution"
    
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
            })
    
    return {
        "count": len(skills),
        "skills": skills,
    }


def run_ablation_study(
    task_names: list[str],
    output_dir: Path,
) -> dict:
    """运行消融实验"""
    tasks = load_skillsbench_tasks(task_names)
    
    if not tasks:
        print("错误: 没有可用的任务")
        return {}
    
    results = {
        "baseline": [],      # 无 Skill 系统
        "no_evolution": [],  # 有 Skill 但不进化
        "no_recall": [],     # 可进化但不召回
        "full_system": [],   # 完整系统
    }
    
    # Phase 1: baseline (无 Skill 系统)
    print("\n" + "="*60)
    print("Phase 1: Baseline (无 Skill 系统)")
    print("="*60)
    
    for task in tasks:
        result = run_task(
            task,
            enable_evolution=False,
            enable_recall=False,
        )
        results["baseline"].append(result)
    
    # Phase 2: no_evolution (有 Skill 但不进化)
    print("\n" + "="*60)
    print("Phase 2: No Evolution (有 Skill 但不进化)")
    print("="*60)
    
    for task in tasks:
        result = run_task(
            task,
            enable_evolution=False,
            enable_recall=True,
        )
        results["no_evolution"].append(result)
    
    # Phase 3: no_recall (可进化但不召回)
    print("\n" + "="*60)
    print("Phase 3: No Recall (可进化但不召回)")
    print("="*60)
    
    for task in tasks:
        result = run_task(
            task,
            enable_evolution=True,
            enable_recall=False,
        )
        results["no_recall"].append(result)
    
    # Phase 4: full_system (完整系统)
    print("\n" + "="*60)
    print("Phase 4: Full System (完整系统)")
    print("="*60)
    
    for task in tasks:
        result = run_task(
            task,
            enable_evolution=True,
            enable_recall=True,
        )
        results["full_system"].append(result)
    
    # 分析 Skill 生成
    skill_analysis = analyze_skills(output_dir)
    
    return {
        "results": results,
        "skill_analysis": skill_analysis,
        "timestamp": datetime.now().isoformat(),
    }


def compute_stats(results: list[dict]) -> dict:
    """计算统计指标"""
    if not results:
        return {}
    
    total = len(results)
    success_count = sum(1 for r in results if r.get("success"))
    
    durations = [r.get("duration", 0) for r in results]
    tool_calls = [r.get("tool_calls", 0) for r in results]
    
    return {
        "total": total,
        "success_count": success_count,
        "success_rate": success_count / total if total > 0 else 0,
        "avg_duration": sum(durations) / total if total > 0 else 0,
        "avg_tool_calls": sum(tool_calls) / total if total > 0 else 0,
    }


def generate_report(data: dict, output_file: Path):
    """生成评测报告"""
    results = data.get("results", {})
    skill_analysis = data.get("skill_analysis", {})
    
    report = []
    report.append("# SkillsBench 评测报告")
    report.append("")
    report.append(f"**日期**: {datetime.now().strftime('%Y-%m-%d')}")
    report.append(f"**模型**: deepseek-v4-flash")
    report.append("")
    
    # 消融实验结果
    report.append("## 1. 消融实验结果")
    report.append("")
    report.append("| 变体 | 任务数 | 成功率 | 平均耗时 | 平均工具调用 |")
    report.append("|------|--------|--------|----------|--------------|")
    
    for variant in ["baseline", "no_evolution", "no_recall", "full_system"]:
        variant_results = results.get(variant, [])
        stats = compute_stats(variant_results)
        
        report.append(
            f"| {variant} | {stats.get('total', 0)} | "
            f"{stats.get('success_rate', 0) * 100:.1f}% | "
            f"{stats.get('avg_duration', 0):.1f}s | "
            f"{stats.get('avg_tool_calls', 0):.1f} |"
        )
    
    report.append("")
    
    # Skill 分析
    report.append("## 2. Skill 生成分析")
    report.append("")
    report.append(f"- **生成 Skill 数量**: {skill_analysis.get('count', 0)}")
    report.append("")
    
    skills = skill_analysis.get("skills", [])
    if skills:
        report.append("| Skill | 大小 | 有元数据 | 有描述 |")
        report.append("|-------|------|----------|--------|")
        
        for skill in skills:
            report.append(
                f"| {skill['name']} | {skill['size']} | "
                f"{'✅' if skill['has_metadata'] else '❌'} | "
                f"{'✅' if skill['has_description'] else '❌'} |"
            )
    
    report.append("")
    
    # 详细结果
    report.append("## 3. 详细结果")
    report.append("")
    
    for variant in ["baseline", "no_evolution", "no_recall", "full_system"]:
        report.append(f"### {variant}")
        report.append("")
        
        variant_results = results.get(variant, [])
        for r in variant_results:
            status = "✅" if r.get("success") else "❌"
            report.append(
                f"- {status} {r['task']}: "
                f"{r.get('duration', 0):.1f}s, "
                f"{r.get('tool_calls', 0)} 次调用"
            )
        
        report.append("")
    
    # 保存报告
    output_file.write_text("\n".join(report))
    print(f"\n报告已保存到: {output_file}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="SkillsBench 评测")
    parser.add_argument(
        "--tasks",
        nargs="+",
        default=[
            "debug-trl-grpo",
            "fix-build-agentops",
            "citation-check",
            "software-dependency-audit",
            "dialogue-parser",
            "travel-planning",
            "offer-letter-generator",
            "python-scala-translation",
            "lean4-proof",
            "parallel-tfidf-search",
        ],
        help="要评测的任务名称",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/skillsbench/results.json"),
        help="结果输出路径",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("eval/reports/skillsbench_report.md"),
        help="报告输出路径",
    )
    
    args = parser.parse_args()
    
    # 确保输出目录存在
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    
    print("="*60)
    print("SkillsBench 评测")
    print("="*60)
    print(f"任务: {args.tasks}")
    print(f"输出: {args.output}")
    print(f"报告: {args.report}")
    
    # 运行消融实验
    data = run_ablation_study(args.tasks, args.output.parent)
    
    # 保存结果
    args.output.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\n结果已保存到: {args.output}")
    
    # 生成报告
    generate_report(data, args.report)
    
    print("\n评测完成!")


if __name__ == "__main__":
    main()
