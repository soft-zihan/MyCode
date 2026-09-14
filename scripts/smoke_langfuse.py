#!/usr/bin/env python3
"""Langfuse 云端接入冒烟测试。

流程：
1. 加载 .env（Langfuse 密钥）
2. 初始化 OTel tracer（OTLP/HTTP → Langfuse Cloud）
3. 发送一棵模拟 span 树（turn → model_call → tool_call → compaction）
4. flush 后通过 Langfuse REST API 查询验证 trace 到达

运行：.venv/bin/python scripts/smoke_langfuse.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 1. 加载 .env
env_file = PROJECT_ROOT / ".env"
for line in env_file.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    k, _, v = line.partition("=")
    os.environ.setdefault(k.strip(), v.strip().strip('"'))

os.environ["MYCODE_OTEL"] = "1"

BASE_URL = os.environ["LANGFUSE_BASE_URL"]
PK = os.environ["LANGFUSE_PUBLIC_KEY"]
SK = os.environ["LANGFUSE_SECRET_KEY"]

# 2. 初始化 tracer
from agents.observability.tracer import (
    init_tracer,
    shutdown_tracer,
    set_current_session_id,
    tracer,
)

init_tracer()

# 3. 发送模拟 span 树
smoke_id = f"smoke-{uuid.uuid4().hex[:8]}"
session_id = f"smoke-session-{uuid.uuid4().hex[:8]}"
set_current_session_id(session_id)

print(f"[smoke] trace 标记: {smoke_id}, session: {session_id}")

with tracer.span("turn", {
    "langfuse.observation.type": "chain",
    "langfuse.trace.name": f"mycode-smoke-{smoke_id}",
    "mycode.turn.id": smoke_id,
    "langfuse.observation.input": "smoke test: 帮我读取 config.py",
    "mycode.event_range.start_seq": 1,
}) as turn_span_obj:
    with tracer.span("llm.test-model", {
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "test-model",
        "llm.token_count.prompt": 100,
        "llm.token_count.completion": 20,
    }):
        time.sleep(0.05)
    with tracer.span("tool.read_file", {
        "langfuse.observation.type": "tool",
        "tool.name": "read_file",
        "langfuse.observation.input": '{"file_path": "config.py"}',
    }):
        time.sleep(0.05)
    with tracer.span("context.compaction", {
        "langfuse.observation.type": "chain",
        "langfuse.observation.metadata.tokens_before": 5000,
        "langfuse.observation.metadata.tokens_after": 3000,
        "langfuse.observation.metadata.compression_ratio": 0.6,
    }):
        time.sleep(0.02)
    turn_span_obj.set_attribute("mycode.event_range.end_seq", 42)

# 4. flush
shutdown_tracer()
print("[smoke] span 已 flush，等待 Langfuse 摄取...")

# 5. 通过 REST API 验证
auth = base64.b64encode(f"{PK}:{SK}".encode()).decode()
deadline = time.time() + 60
found = None
while time.time() < deadline:
    req = urllib.request.Request(
        f"{BASE_URL}/api/public/traces?limit=20",
        headers={"Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    for t in data.get("data", []):
        if smoke_id in (t.get("name") or ""):
            found = t
            break
    if found:
        break
    time.sleep(5)

if found:
    print(f"[smoke] ✅ trace 已到达 Langfuse: id={found['id']} name={found['name']}")
    print(f"[smoke]    sessionId={found.get('sessionId')} observations={len(found.get('observations', []))}")
    print(f"[smoke]    UI: {BASE_URL}/project/*/traces/{found['id']}")
else:
    print(f"[smoke] ❌ 60 秒内未查询到 trace（标记 {smoke_id}），请检查密钥/网络")
    sys.exit(1)
