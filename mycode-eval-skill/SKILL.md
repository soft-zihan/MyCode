---
name: mycode-eval
description: MyCode 评测与实验系统操作手册。Use when 设计/运行/分析 MyCode 的评测(eval)或对比实验(experiment/ablation/消融)、跑 smoke/chain/GAIA/HLE/LOCA、分析评测报告(eval/reports)或 Langfuse trace、定位评测失败(bad case)、配置压缩臂(compression_arm)或窗口(context_window)、统计 token 成本/cache 命中率。触发词：评测、实验、消融、ablation、GAIA、LOCA、smoke、chain、trace 分析、pass@1、cache 率、token 成本、失败定位。
---

# MyCode 评测与实验系统

## 体系地图

| 套件 | 用途 | 命令 |
|---|---|---|
| smoke 单元 | 单能力点回归 | `python -m eval.smoke.runner --suite smoke` |
| chain 全链路 | 端到端回归（本项目打磨的主测试） | `python -m eval.smoke.runner --suite chain`，跑完 `python -m eval.langfuse.run_evals --judge` 评审 |
| GAIA | 通用推理对比实验（level 1/2/3/all，默认 3；注意其 `--sample` 默认 10，全量需显式给题量） | `python -m eval.gaia.runner [--level 2] [--model M] [--thinking off] [--window 100k] [--compression-arm X]` |
| HLE | 高难度推理 | `python -m eval.hle.runner` |
| LOCA | 可控上下文增长（压缩消融 v2 主战场；缺省全量跑完整个 config set） | `python -m eval.loca.runner --config-set 128k --window 100k --arm full` |

- 报告：`eval/reports/{benchmark}-{ts}.json/.md`（meta/summary/results 三段，meta 含 arm/window/thinking/model/seed）
- 事件日志：`~/.mycode/sessions/*.events.jsonl`（评测 sub-agent session 也会落盘）
- 评测工作区：`~/.mycode/eval_workspaces/`（**必须在仓库外**，见铁律 5）
- 前端：`/eval` 页双 Tab（评测集/运行），任务列表 `GET /api/eval/tasks?benchmark=`

## 实验设计铁律（每条都用真实事故换来，违反必翻车）

1. **触发验证先于全量**（BC-16）：消融开跑前用 1-2 题 smoke 断言操纵变量真实触发——
   压缩实验查 `compression_events > 0`（runner 已自动统计并在零触发时打印 ⚠️）或日志
   `should_compress=True`。**"跑了 80 次"≠"测到了机制"**：v1 消融四臂 80 runs 全程
   利用率峰值 6.3%（触发线 80%），四臂机制相同，"McNemar 5-0 全胜"全是采样噪声。
2. **能力区间校准**：题集 pass 率必须落在可测区间（约 20%-70%）。全 0（floor）或全 1
   （天花板）都没有区分度。换小模型先跑 3 题探测，再定 level/题集。
3. **触发线换算**：`effective_window = window - 20K`，工具折叠触发 = 利用率 > 80%，
   会话折叠目标水位 = 40%。压力窗口按"任务上下文峰值 × 0.8 以下"选：GAIA L3 峰值
   ~76K → 100k 窗口（触发线 64K）可触发，262k 窗口（触发线 ~194K）永不触发。
4. **配对设计**：同题集、双 seed（42/43）、臂间同模型同 thinking 状态；用 McNemar
   错配对计数 + sign test，不比较裸 pass 率点估计。**结果异常漂亮或异常符合预期时，
   先机制核查再下结论**（确认偏差是 v1 事故帮凶）。
5. **工作区即实验条件**（BC-15/17）：评测工作区模拟"干净用户项目"——在仓库内会泄漏
   任务产物到 git、且经 `load_agents_md` 向上遍历继承开发 AGENTS.md（"网络问题必须
   告知用户"在无人值守评测中=弃题，实测压低通过率）。规则向上收集是正确生产语义，
   错的是工作区位置。
6. **配置成对解析**（BC-13）：model/api_base/api_key 必须成对取自同一端点配置，
   跨源混配（env key + SDK 默认 base）会静默打错门（api.openai.com 401 伪装成限流）。
   `resolve_model_config` 解析不出 base 直接报错。跑前 1 题验证请求指向。
7. **meta 落盘自证**（BC-14）：报告 meta 必须记录全部实验变量（arm/window/thinking/
   model/seed/level），否则结果不可归因。开跑前查一次 meta。
8. **混杂因子记录**：臂串行跑在不同时段 → 网络/网关状况是混杂因子（GAIA v1 夜间
   web_search 大面积 ddgs TLS 失败 + exa 429）。报告 meta 记录起止时间；失败归因先排除环境。

## 失败定位工作流（不猜测，看证据）

```
report.json（error/predicted 一行画像）
  → meta.session_id / results[].session_id
  → ~/.mycode/sessions/{sid}.events.jsonl（工具调用时间线、原话、错误）
  → 工作区产物（mtime + 内容 = 等待类断言的第一证据）
  → Langfuse trace（子 Agent 细节、token、耗时）
```

- **先分"管线坏了"还是"断言错了"**：产物存在而断言失败 → 断言问题（关键词语言脆性，
  `contains` 支持 `list[str]` any-of）；产物缺失 → 顺事件时间线找断点。
- **看 agent 原话引用**：它会直接供出污染源（"According to the project rules…" = 规则泄漏）。
- **失败形态分类**：秒答（数据误读/推理浅）、马拉松后弃答（工具死胡同不换策略）、
  "network issues"（搜索后端崩溃，环境性）、格式对但值错（能力）。
- 文中的 BC-N 是内部事故编号，各条已在对应位置就地写明成因与规避方式。

## Trace / Token 成本分析

- Langfuse 为唯一常态诊断面：`usageDetails = {input(新), input_cached_tokens(缓存), output}`，
  per-call 语义；聚合脚本模式见 `references/trace-tokens.md`。
- cache 诊断：session 维度聚合 cache 率（生产设计目标 ≥80%；GAIA 评测实测 86%）。
- 窗口↔成本：无压缩时累计输入随调用数 O(n²)（每 call 重发历史），压缩封顶后回线性。
  成本对比必须同时报 cache 率与新输入量。

## 无人值守实验调度

小时级消融用 watcher 模式（完整脚本见 `references/experiment-ops.md`）：
done 标记必须校验**新鲜度**（mtime > watcher 启动时间，防陈旧标记误判）；
runner 死亡探测（pgrep）；认证类错误熔断（abort 而非续跑）；`caffeinate -i -w <watcher_pid>`
防休眠；首臂测时长 → 超时阈值决定余臂串行/并行。

## 压缩消融变量矩阵（当前口径）

- 臂：`full`(双层) / `truncate`(朴素硬截断：同触发点 80%/同目标水位 40%、无摘要、
  保首条指令、整组隐藏) / `tool_only`(仅工具折叠) / `session_only`(仅会话折叠)
- 窗口：`--window 100k|262k|1000k|整数`（压力窗口是消融维度，不是端点属性）
- v2 结论（LOCA 128k×100k，qwen3.6-27b）：full 66.7% > session_only 53.3% >
  tool_only 46.7% > truncate 33.3%；
- 模型/思维链：`--model` + `--thinking off`（小模型快，思考模型评测默认关思考）
- 批量：正式实验**全量跑完整个 config set**（`--sample` 缺省即全量）；`--select head`
  保证跨 run 确定性。`--sample N` 只用于探测与校准，不产出正式结论

细节：`references/experiment-design.md`（设计清单）、`references/trace-tokens.md`（聚合脚本）、
`references/experiment-ops.md`（watcher/重启/排错）。
