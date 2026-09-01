"""Phoenix LLM-as-Judge 评估集成

从 Phoenix Trace 中提取数据，运行评估，导出失败案例到 Dataset。

依赖：pip install arize-phoenix openinference-instrumentation-openai
"""

from __future__ import annotations

import json
import os
from typing import Any


def phoenix_available() -> bool:
    try:
        import phoenix  # noqa: F401
        return True
    except ImportError:
        return False


def eval_hallucination(output: str, context: str, model: str = "gpt-4o-mini") -> dict:
    """幻觉检测 — 检查输出中是否存在上下文中不存在的事实性陈述"""
    if not phoenix_available():
        return {"error": "phoenix not installed"}

    from phoenix.evals import OpenAIModel, HallucinationEvaluator

    llm = OpenAIModel(model=model)
    evaluator = HallucinationEvaluator(llm)
    result = evaluator.evaluate(
        output=output,
        input={"context": context[:3000]},
    )
    return {"score": result.get("score", 0), "label": result.get("label", "unknown")}


def eval_qa_quality(question: str, answer: str, context: str, model: str = "gpt-4o-mini") -> dict:
    """问答质量评估"""
    if not phoenix_available():
        return {"error": "phoenix not installed"}

    from phoenix.evals import OpenAIModel, QAEvaluator

    llm = OpenAIModel(model=model)
    evaluator = QAEvaluator(llm)
    result = evaluator.evaluate(
        output=answer,
        input={"question": question, "context": context[:3000]},
    )
    return {"score": result.get("score", 0), "label": result.get("label", "unknown")}


def eval_tool_selection(
    user_intent: str,
    tool_chosen: str,
    tool_input: dict,
    context: str,
    model: str = "gpt-4o-mini",
) -> dict:
    """工具选择正确性评估"""
    if not phoenix_available():
        return {"error": "phoenix not installed"}

    from phoenix.evals import OpenAIModel

    llm = OpenAIModel(model=model)
    prompt = f"""Given the following context and user intent, evaluate whether the chosen tool and its parameters are appropriate.

Context: {context[:2000]}
User Intent: {user_intent}
Tool Chosen: {tool_chosen}
Tool Input: {json.dumps(tool_input, ensure_ascii=False)}

Return JSON: {{"correct": true/false, "score": 0.0-1.0, "reason": "..."}}"""

    response = llm(prompt)
    try:
        return json.loads(response)
    except (json.JSONDecodeError, TypeError):
        return {"correct": None, "score": 0.5, "reason": f"parse error: {response[:200]}"}


def export_failures_to_dataset(
    trace_file: str,
    dataset_name: str = "failure-cases",
) -> dict:
    """从 JSONL trace 中提取失败案例，导出为 Phoenix Dataset"""
    if not phoenix_available():
        return {"error": "phoenix not installed"}

    import phoenix as px
    from pathlib import Path

    path = Path(trace_file)
    if not path.exists():
        return {"error": f"trace file not found: {trace_file}"}

    client = px.Client()
    dataset = client.create_dataset(dataset_name)

    examples = []
    with open(path) as f:
        for line in f:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("kind") == "tool.end" and not event.get("success", True):
                examples.append({
                    "input": {
                        "tool": event.get("tool", ""),
                        "input": str(event.get("input", ""))[:1000],
                    },
                    "output": str(event.get("error", ""))[:1000],
                    "metadata": {"ts": event.get("ts", ""), "kind": "tool_failure"},
                })

    if examples:
        client.append_to_dataset(dataset.id, examples)

    return {"exported": len(examples), "dataset": dataset_name}
