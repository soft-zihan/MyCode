"""评测结果上报 Langfuse — 评估回答质量和知识召回。

评估对象：评测报告中的 responses 列表
指标：
- response_match      BOOLEAN  回答是否包含预期关键词
- wiki_recalled       BOOLEAN  是否成功召回 wiki 条目
- answer_length       NUMERIC  回答长度（用于分析）
"""

from __future__ import annotations

import re
from typing import Any


def normalize_text(text: str) -> str:
    """标准化文本：小写、去标点、合并空格。"""
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def check_response_match(response: str, expected_keywords: list[str]) -> bool:
    """检查回答是否包含至少一个预期关键词。"""
    if not response or not expected_keywords:
        return False
    response_norm = normalize_text(response)
    for keyword in expected_keywords:
        keyword_norm = normalize_text(keyword)
        if keyword_norm and keyword_norm in response_norm:
            return True
    return False


def eval_response_match(response: dict[str, Any]) -> dict[str, Any] | None:
    """评估回答匹配度。"""
    answer = response.get("answer", "")
    expected = response.get("expected_keywords", [])
    
    if not answer:
        return {
            "name": "response_match",
            "value": False,
            "data_type": "BOOLEAN",
            "comment": "empty response",
        }
    
    if not expected:
        return None
    
    match = check_response_match(answer, expected)
    return {
        "name": "response_match",
        "value": match,
        "data_type": "BOOLEAN",
        "comment": f"matched {sum(1 for k in expected if normalize_text(k) in normalize_text(answer))}/{len(expected)} keywords",
    }


def eval_wiki_recalled(response: dict[str, Any]) -> dict[str, Any] | None:
    """评估 wiki 是否被召回。"""
    wiki_recalled = response.get("wiki_recalled", False)
    if not wiki_recalled:
        return None
    return {
        "name": "wiki_recalled",
        "value": wiki_recalled,
        "data_type": "BOOLEAN",
        "comment": "wiki entries recalled" if wiki_recalled else "no wiki recall",
    }


def eval_answer_length(response: dict[str, Any]) -> dict[str, Any] | None:
    """评估回答长度（用于分析是否过于冗长）。"""
    answer = response.get("answer", "")
    length = len(answer)
    return {
        "name": "answer_length",
        "value": length,
        "data_type": "NUMERIC",
        "comment": f"response length: {length} chars",
    }


def evaluate_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    """对单个 response 运行全部 evaluators。"""
    results = []
    for fn in [eval_response_match, eval_wiki_recalled, eval_answer_length]:
        r = fn(response)
        if r is not None:
            results.append(r)
    return results


def upload_eval_to_langfuse(
    client: Any,
    task_name: str,
    task_id: str,
    responses: list[dict[str, Any]],
    session_id: str,
    trace_ids: list[str],
) -> dict[str, Any]:
    """上传评测结果到 Langfuse。
    
    1. 创建 dataset（幂等）
    2. 上传 dataset items（每个问题一个）
    3. 提交 scores 到对应的 traces
    
    Returns: {"dataset": name, "items": N, "scores": N}
    """
    dataset_name = f"eval-{task_id.lower().replace('_', '-')}"
    
    # 创建 dataset
    client.create_dataset(
        dataset_name,
        description=f"评测: {task_name}",
    )
    
    items_uploaded = 0
    scores_submitted = 0
    
    for i, resp in enumerate(responses):
        phase = resp.get("phase", i + 1)
        question = resp.get("question", "")
        answer = resp.get("answer", "")
        expected = resp.get("expected_keywords", [])
        
        # 上传 dataset item
        item_id = f"{session_id}-q{phase}"
        client.upsert_dataset_item(
            dataset_name=dataset_name,
            input={"question": question, "phase": phase},
            expected_output={"answer_keywords": expected} if expected else None,
            metadata={
                "session_id": session_id,
                "trace_ids": trace_ids,
                "wiki_recalled": resp.get("wiki_recalled", False),
            },
            item_id=item_id,
        )
        items_uploaded += 1
        
        # 运行 evaluators 并提交 scores
        scores = evaluate_response(resp)
        
        # 找到对应的 trace（简化：使用最后一个 trace）
        if trace_ids:
            trace_id = trace_ids[-1]
            for score in scores:
                try:
                    client.create_score(
                        trace_id=trace_id,
                        name=f"{score['name']}_q{phase}",
                        value=score["value"],
                        data_type=score["data_type"],
                        comment=f"Q{phase}: {score.get('comment', '')}",
                    )
                    scores_submitted += 1
                except Exception:
                    pass
    
    return {
        "dataset": dataset_name,
        "items": items_uploaded,
        "scores": scores_submitted,
    }
