# Trace 与 Token 成本分析

## 数据源分工

| 数据 | 来源 | 说明 |
|---|---|---|
| 每题 tokens（累计 input/output） | `eval/reports/*.json` results[].tokens | 快、离线、全量 |
| per-call cache 明细 | Langfuse `usageDetails` | `input`=新输入，`input_cached_tokens`=缓存命中，`output` |
| 工具调用时间线/原话 | `~/.mycode/sessions/{sid}.events.jsonl` | 失败归因第一现场 |
| 压缩触发计数 | report results[].compression_events | runner 自动统计（BC-16 触发验证） |
| 利用率轨迹 | runner stdout `[compressor] check:` 行 | tokens/utilization/should_compress |

## Langfuse 按臂聚合脚本模式

```python
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from agents.observability.langfuse_api import load_langfuse_env, get_client
load_langfuse_env(Path('<repo>').resolve())
c = get_client()

def fetch(tid):
    b = c.fetch_trace(tid)  # 慢（US cloud），用 ThreadPool(6) 并发 + 后台跑
    agg = {'new_in': 0, 'cached': 0, 'out': 0, 'calls': 0}
    for o in b.get('observations', []):
        if o.get('type') != 'GENERATION':
            continue
        u = o.get('usageDetails') or {}
        agg['new_in'] += u.get('input', 0) or 0
        agg['cached'] += u.get('input_cached_tokens', 0) or 0
        agg['out'] += u.get('output', 0) or 0
        agg['calls'] += 1
    return tid, agg
```

- trace_id 在 report results[].trace_id；80 traces 聚合约 3-5 分钟，**放后台**（120s 前台超时不够）
- cache 率 = cached / (cached + new_in)；生产目标 ≥80%
- 成本等效：计费 ≈ new_in + cached×折扣(通常 0.1-0.25) + output；报告时给 token 量与 cache 率，不虚构单价

## cache 率诊断（生产会话）

1. Langfuse session 维度拉 GENERATION observations 的 usageDetails
2. cache 率低的排查序：system prompt 是否会话内冻结（每 turn 重编译会破坏 prefix）→
   易变内容（时间戳/运行时状态）是否混进 system（应置尾部 ephemeral 消息）→
   工具结果是否插在中部（应只追加）
3. 修复验证：同工作区连续对话，per-call cache 率应 90%+；跨会话首 call 命中共享 prefix

## 利用率/触发分析（runner 日志）

```bash
# 峰值利用率（判断窗口选择是否合理）
grep -ho "utilization=[0-9.]*%" /tmp/run.log | sed 's/.*=//;s/%//' | sort -rn | head -3
# 触发次数
grep -c "should_compress=True" /tmp/run.log
# 认证/环境类错误（实验有效性红线）
grep -c "AuthenticationError\|circuit breaker\|429" /tmp/run.log
```

## 窗口-成本关系（写报告用）

- 每 call 重发全部可见上下文 → 无压缩时累计输入随调用次数 O(n²)
- 压缩把 per-call 上下文封顶 → 累计输入回到线性；cache 命中进一步把单价打到 10-25%
- 对比臂成本时同时报：累计输入、新输入、cache 率、调用次数——只看总量会漏掉结构差异
