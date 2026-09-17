"""ask_user 工具 — Agent 主动向用户提问。

复用 permission_gate 的阻塞等待模式。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any


ASK_USER_TOOL = {
    "name": "ask_user",
    "description": "向用户提问并等待回答。用于澄清需求、确认决策、或获取批准。",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要问的问题",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选的选项列表。提供选项时优先使用。",
            },
            "context": {
                "type": "string",
                "description": "问题的背景信息",
            },
        },
        "required": ["question"],
    },
}


async def handle_ask_user(session: Any, inp: dict, *, abort_fn=None) -> str:
    """处理 ask_user 工具调用。"""
    request_id = str(uuid.uuid4())[:8]
    
    question = inp.get("question", "")
    options = inp.get("options", [])
    context = inp.get("context", "")
    
    if not question:
        return "Error: question is required"
    
    session.append("question/request", {
        "request_id": request_id,
        "question": question,
        "options": options if isinstance(options, list) else [],
        "context": context,
    })
    
    for _ in range(3000):
        await asyncio.sleep(0.1)
        if request_id in session.question_responses:
            answer = session.question_responses.pop(request_id).get("answer", "")
            session.append("question/resolved", {
                "request_id": request_id,
                "answer": answer,
            })
            return answer
        if abort_fn and abort_fn():
            return "[ERROR] Question cancelled by abort."
    
    return "[ERROR] Timeout waiting for user response (300s). Proceed with your best judgment or ask again."
